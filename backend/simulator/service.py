import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from simulator.models import fulfillment_events, idempotency_records, orders, tickets


class BusinessError(ValueError):
    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: int = Field(ge=1)
    expected_error_code: str = Field(min_length=1, max_length=80)


def bound_order(
    conn: Connection, tenant: str, ticket_id: str, order_id: str, *, lock: bool = False
):
    ticket_query = select(tickets).where(
        tickets.c.tenant_id == tenant,
        tickets.c.id == ticket_id,
        tickets.c.bound_order_id == order_id,
    )
    # Lock the trusted binding before locking the order on write paths.
    if lock:
        ticket_query = ticket_query.with_for_update()
    if conn.execute(ticket_query).mappings().first() is None:
        raise BusinessError("NOT_FOUND", 404)
    query = select(orders).where(orders.c.tenant_id == tenant, orders.c.id == order_id)
    if lock:
        query = query.with_for_update()
    order = conn.execute(query).mappings().first()
    if order is None:
        raise BusinessError("NOT_FOUND", 404)
    return order


def previous_result(conn: Connection, tenant: str, key: str, request_hash: str):
    row = (
        conn.execute(
            select(idempotency_records).where(
                idempotency_records.c.tenant_id == tenant,
                idempotency_records.c.idempotency_key == key,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise BusinessError("IDEMPOTENCY_CONFLICT")
    return row["response_json"]


def retry_fulfillment(
    engine: Engine,
    tenant: str,
    ticket_id: str,
    order_id: str,
    key: str,
    request: RetryRequest,
    now: datetime,
) -> dict:
    semantic = {
        "operation_type": "retry_fulfillment",
        "tenant_id": tenant,
        "order_id": order_id,
        **request.model_dump(),
    }
    digest = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    expected_key = f"fulfillment-retry:{tenant}:{order_id}:v{request.expected_version}"
    with engine.begin() as conn:
        # Authorization precedes every lookup, including replay of a successful operation.
        bound_order(conn, tenant, ticket_id, order_id)
        result = previous_result(conn, tenant, key, digest)
        if result is not None:
            return result
        if key != expected_key:
            raise BusinessError("INVALID_IDEMPOTENCY_KEY", 422)
        order = bound_order(conn, tenant, ticket_id, order_id, lock=True)
        # A concurrent writer may have committed while this request waited for the lock.
        result = previous_result(conn, tenant, key, digest)
        if result is not None:
            return result
        checks = [
            (order["version"] == request.expected_version, "ORDER_VERSION_CHANGED"),
            (order["payment_status"] == "PAID", "PAYMENT_NOT_READY"),
            (order["fulfillment_status"] == "FAILED", "FULFILLMENT_NOT_FAILED"),
            (order["inventory_reserved"], "INVENTORY_UNAVAILABLE"),
            (
                order["paid_at"] is not None and now - order["paid_at"] >= timedelta(minutes=30),
                "PAYMENT_TOO_RECENT",
            ),
        ]
        for allowed, reason in checks:
            if not allowed:
                raise BusinessError(reason)
        latest = conn.execute(
            select(fulfillment_events.c.error_code)
            .where(
                fulfillment_events.c.tenant_id == tenant,
                fulfillment_events.c.order_id == order_id,
                fulfillment_events.c.event_type == "FULFILLMENT_FAILED",
            )
            .order_by(fulfillment_events.c.created_at.desc(), fulfillment_events.c.id.desc())
            .limit(1)
        ).scalar()
        if latest != "QUEUE_PUBLISH_TIMEOUT" or request.expected_error_code != latest:
            raise BusinessError("ERROR_CODE_NOT_ALLOWED")
        version = order["version"] + 1
        updated = conn.execute(
            update(orders)
            .where(
                orders.c.tenant_id == tenant,
                orders.c.id == order_id,
                orders.c.version == request.expected_version,
            )
            .values(fulfillment_status="QUEUED", version=version)
        )
        if updated.rowcount != 1:
            raise BusinessError("ORDER_VERSION_CHANGED")
        conn.execute(
            fulfillment_events.insert().values(
                tenant_id=tenant,
                id=str(uuid4()),
                order_id=order_id,
                event_type="RETRY_ENQUEUED",
                error_code=None,
                created_at=now,
                operation_key=key,
            )
        )
        result = {
            "accepted": True,
            "order_id": order_id,
            "fulfillment_status": "QUEUED",
            "version": version,
            "operation_key": key,
        }
        conn.execute(
            idempotency_records.insert().values(
                tenant_id=tenant,
                idempotency_key=key,
                order_id=order_id,
                request_hash=digest,
                operation_type="retry_fulfillment",
                response_json=result,
                created_at=now,
            )
        )
    return result
