import secrets
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import valid_token
from app.execution.business import BusinessClient, RemoteError
from app.execution.db import Database, digest

router = APIRouter(prefix="/api")


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    auto_note: Literal[False] = False


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["APPROVED", "REJECTED"]
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(default="", max_length=500)


def identity(request: Request, authorization: str | None = Header(default=None)):
    settings = request.app.state.settings
    read = settings.demo_operator_token.get_secret_value()
    approve = settings.demo_approver_token.get_secret_value()
    if not valid_token(read) or not valid_token(approve) or read == approve:
        raise HTTPException(503, "DEMO_AUTH_NOT_CONFIGURED")
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if secrets.compare_digest(token.encode(), read.encode()):
        return {"user": "operator", "role": "operator", "tenant": settings.demo_tenant_id}
    if secrets.compare_digest(token.encode(), approve.encode()):
        return {"user": "approver", "role": "approver", "tenant": settings.demo_tenant_id}
    raise HTTPException(401, "UNAUTHORIZED")


IDENTITY_DEPENDENCY = Depends(identity)


@router.get("/me")
def current_identity(who=IDENTITY_DEPENDENCY):
    return who


def database(request: Request):
    return Database(request.app.state.engine)


def now(request: Request):
    return getattr(request.app.state, "clock", lambda: datetime.now(UTC))()


def business(request: Request):
    factory = getattr(request.app.state, "business_factory", None)
    return factory() if factory else BusinessClient(request.app.state.settings)


def get_run(db, run_id, tenant):
    with db.engine.connect() as conn:
        row = (
            conn.execute(
                select(db.runs).where(
                    db.runs.c.id == run_id,
                    db.runs.c.tenant_id == tenant,
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(404, "NOT_FOUND")
    return dict(row)


def public_approval(row):
    return {
        "approval_id": row["id"],
        "run_id": row["run_id"],
        "plan": row["plan_json"],
        "plan_hash": row["plan_hash"],
        "decision": row["decision"],
        "expires_at": row["expires_at"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "reason": row["reason"],
    }


@router.get("/tickets")
def list_tickets(
    request: Request,
    after_id: str = Query("", max_length=64, pattern=r"^[A-Za-z0-9_-]*$"),
    limit: int = Query(25, ge=1, le=25),
    who=IDENTITY_DEPENDENCY,
):
    remote = business(request)
    try:
        result = remote.request(
            "GET",
            "/internal/tickets?" + urlencode({"after_id": after_id, "limit": limit}),
            who["tenant"],
        )
    except RemoteError:
        raise HTTPException(503, "BUSINESS_UNAVAILABLE") from None
    finally:
        remote.close()
    db = database(request)
    with db.engine.connect() as conn:
        active = dict(
            conn.execute(
                select(db.runs.c.ticket_id, db.runs.c.id).where(
                    db.runs.c.tenant_id == who["tenant"],
                    db.runs.c.status.in_(["QUEUED", "RUNNING", "WAITING_APPROVAL"]),
                )
            ).all()
        )
    for ticket in result["tickets"]:
        ticket["active_run_id"] = active.get(ticket["id"])
    return result


@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: str, request: Request, who=IDENTITY_DEPENDENCY):
    remote = business(request)
    try:
        result = remote.ticket(who["tenant"], ticket_id)
    except RemoteError as exc:
        raise HTTPException(404 if exc.status == 404 else 503, "TICKET_UNAVAILABLE") from None
    finally:
        remote.close()
    db = database(request)
    with db.engine.connect() as conn:
        result["active_run_id"] = conn.execute(
            select(db.runs.c.id).where(
                db.runs.c.tenant_id == who["tenant"],
                db.runs.c.ticket_id == ticket_id,
                db.runs.c.status.in_(["QUEUED", "RUNNING", "WAITING_APPROVAL"]),
            )
        ).scalar()
    return result


@router.post("/tickets/{ticket_id}/runs")
def start(
    ticket_id: str,
    payload: StartRequest,
    request: Request,
    who=IDENTITY_DEPENDENCY,
    key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    if who["role"] != "operator":
        raise HTTPException(403, "FORBIDDEN")
    db = database(request)
    request_hash = digest({"ticket_id": ticket_id, **payload.model_dump()})

    def duplicate(conn):
        return (
            conn.execute(
                select(db.runs).where(
                    db.runs.c.tenant_id == who["tenant"],
                    db.runs.c.created_by == who["user"],
                    db.runs.c.start_idempotency_key == key,
                )
            )
            .mappings()
            .first()
        )

    def response(row):
        if row["start_request_hash"] != request_hash:
            raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
        return JSONResponse(
            status_code=202, content={"run_id": str(row["id"]), "status": row["status"]}
        )

    with db.engine.connect() as conn:
        existing = duplicate(conn)
        if existing:
            return response(existing)
    remote = business(request)
    try:
        ticket = remote.ticket(who["tenant"], ticket_id)
    except RemoteError as exc:
        raise HTTPException(404 if exc.status == 404 else 503, "TICKET_UNAVAILABLE") from None
    finally:
        remote.close()
    run_id, timestamp = uuid4(), now(request)
    row = {
        "id": run_id,
        "thread_id": str(run_id),
        "tenant_id": who["tenant"],
        "ticket_id": ticket_id,
        "bound_order_id": ticket["bound_order_id"],
        "created_by": who["user"],
        "start_idempotency_key": key,
        "start_request_hash": request_hash,
        "status": "QUEUED",
        "auto_note": False,
        "note_sync_status": "SKIPPED",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        with db.engine.begin() as conn:
            conn.execute(db.runs.insert().values(**row))
            db.enqueue(conn, run_id, "START", timestamp)
            db.event(conn, run_id, "RUN_CREATED", key="created")
    except IntegrityError:
        with db.engine.connect() as conn:
            existing = duplicate(conn)
            if existing:
                return response(existing)
            active = conn.execute(
                select(db.runs.c.id).where(
                    db.runs.c.tenant_id == who["tenant"],
                    db.runs.c.ticket_id == ticket_id,
                    db.runs.c.status.in_(["QUEUED", "RUNNING", "WAITING_APPROVAL"]),
                )
            ).scalar()
        raise HTTPException(
            409, {"code": "ACTIVE_RUN_EXISTS", "run_id": str(active) if active else None}
        ) from None
    return response(row)


@router.get("/runs/{run_id}")
def run_detail(run_id: UUID, request: Request, who=IDENTITY_DEPENDENCY):
    db = database(request)
    row = get_run(db, run_id, who["tenant"])
    row["run_id"] = row["id"]
    if row["approval_id"]:
        approval = dict(db.approval(row["approval_id"]))
        row["approval"] = public_approval(approval)
    with db.engine.connect() as conn:
        records = conn.execute(
            select(db.tool_calls.c.record_json)
            .where(
                db.tool_calls.c.run_id == run_id,
            )
            .limit(300)
        ).scalars()
        row["evidence"] = list(
            {item["evidence"]["evidence_id"]: item["evidence"] for item in records}.values()
        )
    return row


@router.get("/runs/{run_id}/events")
def events(
    run_id: UUID,
    request: Request,
    after_id: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    who=IDENTITY_DEPENDENCY,
):
    db = database(request)
    get_run(db, run_id, who["tenant"])
    with db.engine.connect() as conn:
        return {
            "events": [
                dict(row)
                for row in conn.execute(
                    select(db.run_events)
                    .where(
                        db.run_events.c.run_id == run_id,
                        db.run_events.c.id > after_id,
                    )
                    .order_by(db.run_events.c.id)
                    .limit(limit)
                ).mappings()
            ]
        }


@router.get("/approvals/{approval_id}")
def approval_detail(approval_id: UUID, request: Request, who=IDENTITY_DEPENDENCY):
    db = database(request)
    with db.engine.connect() as conn:
        row = (
            conn.execute(
                select(db.approvals)
                .join(db.runs, db.approvals.c.run_id == db.runs.c.id)
                .where(
                    db.approvals.c.id == approval_id,
                    db.runs.c.tenant_id == who["tenant"],
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(404, "NOT_FOUND")
    return public_approval(row)


def redact_settings(value, settings):
    for secret in (
        settings.demo_operator_token.get_secret_value(),
        settings.demo_approver_token.get_secret_value(),
        settings.simulator_read_token.get_secret_value(),
        settings.simulator_write_token.get_secret_value(),
        settings.llm_api_key.get_secret_value(),
        settings.app_database_url,
        settings.checkpoint_database_url,
    ):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def decide(db, approval_id, tenant, actor, payload, clock):
    with db.engine.begin() as conn:
        target = conn.execute(
            select(db.approvals.c.run_id).where(db.approvals.c.id == approval_id)
        ).scalar()
        if target is None:
            raise HTTPException(404, "NOT_FOUND")
        run = (
            conn.execute(
                select(db.runs)
                .where(
                    db.runs.c.id == target,
                    db.runs.c.tenant_id == tenant,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if run is None:
            raise HTTPException(404, "NOT_FOUND")
        approval = (
            conn.execute(
                select(db.approvals).where(db.approvals.c.id == approval_id).with_for_update()
            )
            .mappings()
            .one()
        )
        if (
            approval["plan_hash"] != payload.plan_hash
            or digest(approval["plan_json"]) != approval["plan_hash"]
        ):
            raise HTTPException(409, "PLAN_HASH_MISMATCH")
        if approval["decision"] != "PENDING":
            if approval["decision"] == "EXPIRED":
                return 410, {"decision": "EXPIRED"}
            if approval["decision"] != payload.decision:
                raise HTTPException(409, "DECISION_CONFLICT")
            return 202, {"decision": approval["decision"], "approval_id": str(approval_id)}
        if run["status"] != "WAITING_APPROVAL":
            raise HTTPException(409, "RUN_NOT_WAITING")
        # Evaluate after both locks: the request may have waited across expiration.
        timestamp = clock()
        decision = "EXPIRED" if timestamp >= approval["expires_at"] else payload.decision
        conn.execute(
            db.approvals.update()
            .where(db.approvals.c.id == approval_id)
            .values(
                decision=decision,
                decided_by=actor,
                decided_at=timestamp,
                reason=payload.reason,
            )
        )
        conn.execute(
            db.runs.update()
            .where(db.runs.c.id == target)
            .values(status="QUEUED", updated_at=timestamp)
        )
        db.enqueue(conn, target, "RESUME", timestamp)
        db.event(
            conn,
            target,
            "APPROVAL_DECIDED",
            {"decision": decision, "actor": actor},
            "approval-decided",
        )
    return (410 if decision == "EXPIRED" else 202), {
        "approval_id": str(approval_id),
        "decision": decision,
    }


@router.post("/approvals/{approval_id}/decision")
def decision(
    approval_id: UUID, payload: DecisionRequest, request: Request, who=IDENTITY_DEPENDENCY
):
    if who["role"] != "approver":
        raise HTTPException(403, "FORBIDDEN")
    code, body = decide(
        database(request),
        approval_id,
        who["tenant"],
        who["user"],
        payload.model_copy(
            update={"reason": redact_settings(payload.reason, request.app.state.settings)}
        ),
        lambda: now(request),
    )
    return JSONResponse(status_code=code, content=body)
