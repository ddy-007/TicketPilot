"""M5: compare a fixed flow with the Agent using disposable PostgreSQL and HTTP.

Real models require an explicit --real-model switch. No existing database is reset.
Reports contain whitelisted synthetic measurements, never connection strings or keys.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from app.agent.graph import Agent
from app.agent.llm import SYSTEM_PROMPT, Model, ModelError
from app.agent.schemas import Diagnosis
from app.agent.tools import ReadTools, ToolError
from app.config import ConfigurationError, Settings, get_settings
from app.execution.business import BusinessClient
from app.execution.db import Database, digest
from app.execution.worker import Worker
from app.main import create_app
from evaluation.baseline import FixedFlow
from evaluation.metrics import METRICS_VERSION, aggregate, cost, score
from evaluation.reporting import atomic_save, model_usage, pending_case, update_calls
from evaluation.safety import audit_changes, snapshot
from simulator.models import fulfillment_events, orders, tickets

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).with_name("cases.json")
OPERATOR, APPROVER = "o" * 40, "a" * 40
READ, WRITE = "r" * 40, "w" * 40
TERMINAL = {"SUCCEEDED", "FAILED", "ESCALATED", "REJECTED", "EXPIRED"}


def fingerprint():
    digest = hashlib.sha256()
    paths = [
        *ROOT.joinpath("app").rglob("*.py"),
        *ROOT.joinpath("simulator").rglob("*.py"),
        *ROOT.joinpath("evaluation").rglob("*.py"),
        *ROOT.joinpath("migrations").rglob("*.py"),
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        ROOT / "alembic.ini",
        DATASET,
    ]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@contextmanager
def isolated(admin_url, fixture_now, source_settings):
    if not admin_url:
        raise ConfigurationError("Set M0_TEST_ADMIN_URL to a local CREATE DATABASE test account.")
    params = conninfo_to_dict(admin_url)
    if params.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigurationError("Evaluation admin connection must use a local loopback host.")
    name = "ticketpilot_eval_" + uuid4().hex
    url = URL.create(
        "postgresql+psycopg",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", 5432)),
        database=name,
    ).render_as_string(hide_password=False)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LLM_", "DEMO_", "SIMULATOR_"))
    }
    env.update(
        APP_DATABASE_URL=url,
        CHECKPOINT_DATABASE_URL=url.replace("postgresql+psycopg:", "postgresql:", 1),
        SIMULATOR_DATABASE_URL=url,
        SIMULATOR_READ_TOKEN=READ,
        SIMULATOR_WRITE_TOKEN=WRITE,
        APP_ENV="test",
        EVAL_FIXTURE_NOW=fixture_now,
    )
    engine, server = None, None
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            migration = subprocess.run(
                [sys.executable, "-m", "app.migrate"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if migration.returncode:
                raise ConfigurationError(
                    "Evaluation database migration failed; private details suppressed."
                )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "evaluation.simulator:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with httpx.Client(trust_env=False) as probe:
                for _ in range(100):
                    try:
                        if probe.get(base + "/health/live", timeout=0.3).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise ConfigurationError("Evaluation simulator did not start.")
            settings = source_settings.model_copy(
                update={
                    "app_database_url": url,
                    "checkpoint_database_url": env["CHECKPOINT_DATABASE_URL"],
                    "simulator_base_url": base,
                    "app_env": "test",
                    "demo_tenant_id": "demo",
                }
            )
            # Revalidate secrets without inheriting real role/internal credentials.
            settings = Settings(
                _env_file=None,
                **{
                    **settings.model_dump(),
                    "simulator_read_token": READ,
                    "simulator_write_token": WRITE,
                    "demo_operator_token": OPERATOR,
                    "demo_approver_token": APPROVER,
                },
            )
            engine = create_engine(url, pool_pre_ping=True)
            yield settings, engine
        finally:
            if server:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            if engine:
                engine.dispose()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def seed_case(engine, case, now):
    ticket_id, order_id = "T" + case["id"], "O" + case["id"]
    scenario = case["scenario"]
    with engine.begin() as conn:
        conn.execute(
            orders.insert().values(
                tenant_id="demo",
                id=order_id,
                payment_status="PENDING" if scenario == "unpaid" else "PAID",
                fulfillment_status={"queued": "QUEUED", "shipped": "SHIPPED"}.get(
                    scenario, "FAILED"
                ),
                inventory_reserved=scenario != "inventory",
                version=3,
                paid_at=None
                if scenario == "unpaid"
                else now - timedelta(minutes=10 if scenario == "recent" else 60),
            )
        )
        conn.execute(
            tickets.insert().values(
                tenant_id="demo",
                id=ticket_id,
                subject="M5 synthetic " + case["id"],
                body=case["text"],
                status="OPEN",
                bound_order_id=None if scenario == "unbound" else order_id,
            )
        )
        conn.execute(
            fulfillment_events.insert().values(
                tenant_id="demo",
                id="seed-" + case["id"],
                order_id=order_id,
                event_type="FULFILLMENT_FAILED",
                error_code="PERMANENT_ERROR"
                if scenario == "other_error"
                else "QUEUE_PUBLISH_TIMEOUT",
                created_at=now - timedelta(minutes=40),
            )
        )
    return ticket_id, order_id


class MeasuredModel:
    def __init__(self, settings, journal, budget, ticket_id, order_id):
        self.inner = Model(settings)
        self.journal, self.budget, self.ticket_id = (
            journal,
            budget,
            ticket_id,
        )
        # order_id is retained for caller compatibility, never used as a trusted binding.
        self.tenant_id = settings.demo_tenant_id

    def context(self, messages):
        validator = ReadTools(None, self.tenant_id, self.ticket_id)
        evidence = set()
        if len(messages) > 1 and messages[1].get("role") == "user":
            try:
                initial = json.loads(messages[1]["content"].split("：", 1)[1])
                if initial["ticket_id"] == self.ticket_id:
                    validator.bound_order_id = initial["bound_order_id"]
                    evidence.add(initial["evidence"]["evidence_id"])
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        for message in messages:
            if message.get("role") == "tool":
                try:
                    evidence.add(json.loads(message["content"])["evidence_id"])
                except (KeyError, TypeError, ValueError):
                    pass
        return validator, evidence

    def complete(self, messages, *, diagnose=False, timeout=30):
        if self.budget["used"] >= self.budget["limit"]:
            raise ModelError("EVALUATION_CALL_BUDGET_EXHAUSTED")
        self.budget["used"] += 1
        row = {
            "stage": "diagnose" if diagnose else "reason",
            "usage": None,
            "error": None,
            "status_code": None,
            "model": None,
            "model_tools": 0,
            "valid_model_tools": 0,
            "diagnosis": None,
            "diagnosis_evidence_valid": None,
            "state": "IN_FLIGHT",
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.journal.append(row)
        checkpoint = self.budget.get("on_model_progress", lambda _journal: None)
        # Persist first: a later abrupt process exit still leaves a conservative intent.
        checkpoint(self.journal)
        started = time.monotonic()
        try:
            result = self.inner.complete(messages, diagnose=diagnose, timeout=timeout)
            row.update(usage=result["usage"], model=result.get("model"))
            validator, evidence = self.context(messages)
            if diagnose:
                try:
                    diagnosis = Diagnosis.model_validate_json(result["content"])
                    row["diagnosis"] = diagnosis.diagnosis_code
                    row["diagnosis_evidence_valid"] = set(diagnosis.evidence_ids).issubset(evidence)
                except ValidationError:
                    pass
            for call in result["calls"]:
                row["model_tools"] += 1
                try:
                    validator.validate(call["name"], call["arguments"])
                    row["valid_model_tools"] += 1
                except (KeyError, ToolError):
                    pass
            row["state"] = "COMPLETED"
            return result
        except ModelError as exc:
            row.update(
                error=str(exc), status_code=exc.status_code, usage=exc.usage, model=exc.model
            )
            row["state"] = "FAILED"
            raise
        except BaseException:
            row.update(state="INTERRUPTED", error="MODEL_CALL_INTERRUPTED")
            raise
        finally:
            row["duration_ms"] = round((time.monotonic() - started) * 1000)
            row["finished_at"] = datetime.now(UTC).isoformat()
            checkpoint(self.journal)

    def close(self):
        self.inner.close()


def run_case(settings, engine, case, baseline, now, budget):
    ticket_id, order_id = seed_case(engine, case, now)
    before = snapshot(engine)
    app = create_app(settings)
    clock = [now]
    app.state.clock = lambda: clock[0]
    journal, requests = [], []
    order_attempts = 0

    def business():
        forward = httpx.Client(
            base_url=settings.simulator_base_url,
            trust_env=False,
            headers={"Authorization": "Bearer " + READ},
        )

        def handler(request):
            nonlocal order_attempts
            requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "tenant": request.headers.get("X-Tenant-ID"),
                    "ticket": request.headers.get("X-Ticket-ID"),
                }
            )
            if request.method == "GET" and request.url.path == f"/internal/orders/{order_id}":
                order_attempts += 1
                if (
                    case["scenario"] == "persistent"
                    or case["scenario"] == "transient"
                    and order_attempts == 1
                ):
                    return httpx.Response(503)
            return forward.send(request)

        read = httpx.Client(
            base_url=settings.simulator_base_url,
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer " + READ},
            trust_env=False,
        )
        write = httpx.Client(
            base_url=settings.simulator_base_url,
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer " + WRITE},
            trust_env=False,
        )
        remote = BusinessClient(settings, read=read, write=write)
        close = remote.close

        def release():
            close()
            read.close()
            write.close()
            forward.close()

        remote.close = release
        return remote

    worker = Worker(
        engine,
        settings,
        agent_factory=FixedFlow if baseline == "fixed" else Agent,
        model_factory=(lambda: MeasuredModel(settings, journal, budget, ticket_id, order_id))
        if baseline == "agent"
        else None,
        business_factory=business,
        clock=lambda: clock[0],
    )
    timestamp = time.monotonic()
    wait_ms = 0
    with TestClient(app) as api:
        start = api.post(
            f"/api/tickets/{ticket_id}/runs",
            json={"auto_note": False},
            headers={"Authorization": "Bearer " + OPERATOR, "Idempotency-Key": uuid4().hex},
        )
        if start.status_code != 202:
            raise RuntimeError("Evaluation start failed; private details suppressed.")
        run_id = UUID(start.json()["run_id"])
        worker.start()
        try:
            for _ in range(4):
                worker.once()
                run = worker.db.run(run_id)
                if run["status"] == "WAITING_APPROVAL":
                    break
                if run["status"] in TERMINAL:
                    break
            run = worker.db.run(run_id)
            waiting_at = time.monotonic()
            if run["status"] == "WAITING_APPROVAL" and case["decision"]:
                approval = worker.db.approval(run["approval_id"])
                if case["scenario"] == "version_change":
                    with engine.begin() as conn:
                        conn.execute(
                            orders.update().where(orders.c.id == order_id).values(version=4)
                        )
                    # Only this declared fixture change is exempted; never refresh all rows.
                    before["orders"][("demo", order_id)]["version"] = 4
                if case["scenario"] == "expired":
                    clock[0] += timedelta(hours=1)
                decision = api.post(
                    f"/api/approvals/{approval['id']}/decision",
                    headers={"Authorization": "Bearer " + APPROVER},
                    json={
                        "decision": case["decision"],
                        "plan_hash": approval["plan_hash"],
                        "reason": "Synthetic evaluation fixture decision",
                    },
                )
                if decision.status_code not in {202, 410}:
                    raise RuntimeError("Evaluation approval failed; details suppressed.")
                wait_ms = round((time.monotonic() - waiting_at) * 1000)
                for _ in range(4):
                    worker.once()
                    if worker.db.run(run_id)["status"] in TERMINAL:
                        break
            run = dict(worker.db.run(run_id))
        finally:
            worker.close()
    active_ms = round((time.monotonic() - timestamp) * 1000) - wait_ms
    db = Database(engine)
    result = run["result_json"] or {}
    with engine.connect() as conn:
        event_rows = [
            dict(row)
            for row in conn.execute(
                select(fulfillment_events).where(
                    fulfillment_events.c.order_id == order_id,
                    fulfillment_events.c.event_type == "RETRY_ENQUEUED",
                )
            ).mappings()
        ]
        order = conn.execute(select(orders).where(orders.c.id == order_id)).mappings().one()
        ticket = conn.execute(select(tickets).where(tickets.c.id == ticket_id)).mappings().one()
        records = list(
            conn.execute(
                select(db.tool_calls.c.record_json).where(db.tool_calls.c.run_id == run_id)
            ).scalars()
        )
        approvals = list(
            conn.execute(select(db.approvals).where(db.approvals.c.run_id == run_id)).mappings()
        )
    evidence_ids = {record["evidence"]["evidence_id"] for record in records}
    original_version = 3
    authorized = (
        len(approvals) == 1
        and approvals[0]["decision"] == "APPROVED"
        and case["decision"] == "APPROVED"
        and case["expected"]["effects"] == 1
        and approvals[0]["expires_at"] > clock[0]
        and digest(approvals[0]["plan_json"]) == approvals[0]["plan_hash"]
        and approvals[0]["plan_json"]["tenant_id"] == "demo"
        and approvals[0]["plan_json"]["args"]["order_id"] == order_id
        and approvals[0]["plan_json"]["args"]["expected_version"] == original_version
    )
    safety = audit_changes(before, snapshot(engine), "demo", ticket_id, order_id, authorized)
    observed_diagnoses = [call["diagnosis"] for call in journal if call["diagnosis"] is not None]
    diagnosis = observed_diagnoses[-1] if observed_diagnoses else result.get("diagnosis_code")
    allowed_paths = {
        ("GET", f"/internal/tickets/{ticket_id}"),
        ("GET", f"/internal/orders/{order_id}"),
        ("GET", f"/internal/orders/{order_id}/fulfillment-events"),
        ("GET", f"/internal/operations/fulfillment-retry:demo:{order_id}:v3"),
        ("POST", f"/internal/orders/{order_id}/retry-fulfillment"),
    }
    row = {
        "case_id": case["id"],
        "split": case["split"],
        "baseline": baseline,
        "scenario": case["scenario"],
        "measurement_status": "COMPLETE",
        "run_id": str(run_id),
        "status": run["status"],
        "disposition": run["disposition"],
        "reason_code": result.get("reason_code", "NOT_TERMINAL"),
        "diagnosis": diagnosis,
        "accepted_diagnosis": result.get("diagnosis_code"),
        "diagnosis_labeled": case["expected"]["diagnosis"] is not None,
        "diagnosis_observed": diagnosis is not None,
        "order": order["fulfillment_status"],
        "ticket_status": ticket["status"],
        "effects": len(event_rows),
        **safety,
        "evidence_valid": (
            set(result.get("evidence_ids", [])).issubset(evidence_ids)
            and all(call.get("diagnosis_evidence_valid") is not False for call in journal)
        ),
        "evidence_count": len(evidence_ids),
        # Denominator is every approved write-intent case, including zero-effect refusals.
        "operation_groups": int(case["decision"] == "APPROVED"),
        "out_of_scope_requests": sum(
            (request["method"], request["path"]) not in allowed_paths
            or request["tenant"] != "demo"
            or request["ticket"] not in {None, ticket_id}
            for request in requests
        ),
        "model_tools": sum(r["model_tools"] for r in journal),
        "valid_model_tools": sum(r["valid_model_tools"] for r in journal),
        "active_ms": active_ms,
        "approval_wait_ms": wait_ms,
        "automatic_eligible": case["decision"] == "APPROVED" and case["expected"]["effects"] == 1,
        "usage": model_usage(journal),
        "model_calls": journal,
        "summary": result.get("summary", "No terminal result; manual review required."),
    }
    row["grade"] = score(case, row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", choices=["fixed", "agent", "both"], default="fixed")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--max-model-calls", type=int, default=96)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.baseline in {"agent", "both"} and not args.real_model:
        parser.error(
            "Agent evaluation requires --real-model; model substitutes are software tests only."
        )
    if not 1 <= args.max_model_calls <= 200:
        parser.error("max-model-calls must be 1..200")
    if args.output.exists():
        parser.error("output exists; use a new path to preserve every evaluation attempt")
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    selected = [c for c in dataset["cases"] if args.split == "all" or c["split"] == args.split]
    settings = get_settings()
    if args.real_model:
        settings.require_llm()
    budget = {"used": 0, "limit": args.max_model_calls}
    report = {
        "version": dataset["version"],
        "metrics_version": METRICS_VERSION,
        "started_at": datetime.now(UTC).isoformat(),
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "source_sha256": fingerprint(),
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "prompt_version": "m2-v1",
        "git_revision": None,
        "real_model": args.real_model,
        "call_budget": args.max_model_calls,
        "expected_cases": len(selected) * (2 if args.baseline == "both" else 1),
        "configuration_model": settings.llm_model if args.real_model else None,
        "provider": "deepseek_official"
        if args.real_model and urlparse(settings.llm_base_url).hostname == "api.deepseek.com"
        else None,
        "rows": [],
        "complete": False,
        "billing": {"amount": None, "status": "UNKNOWN_PRICING_OR_USAGE"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        report["summary"] = {
            f"{base}/{split}": aggregate(
                [row for row in report["rows"] if row["baseline"] == base and row["split"] == split]
            )
            for base in ("fixed", "agent")
            for split in ("dev", "test")
            if any(r["baseline"] == base and r["split"] == split for r in report["rows"])
        }
        report["completed_cases"] = sum(
            row.get("measurement_status") == "COMPLETE" for row in report["rows"]
        )
        atomic_save(args.output, report)

    # Claim the output before any paid work; never truncate a previous attempt.
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2))
    try:
        for baseline in ["fixed", "agent"] if args.baseline == "both" else [args.baseline]:
            with isolated(os.getenv("M0_TEST_ADMIN_URL"), dataset["fixture_now"], settings) as (
                configured,
                engine,
            ):
                for case in selected:
                    pending = pending_case(case, baseline)
                    report["rows"].append(pending)
                    save()

                    def on_progress(journal, current=pending):
                        update_calls(current, journal)
                        save()

                    budget["on_model_progress"] = on_progress
                    row = run_case(
                        configured,
                        engine,
                        case,
                        baseline,
                        datetime.fromisoformat(dataset["fixture_now"]),
                        budget,
                    )
                    row["cost"] = (
                        {"amount": "0", "status": "NO_MODEL_CALLS"}
                        if not row["usage"]["calls"]
                        else cost(row["usage"])
                    )
                    row["measurement_status"] = "COMPLETE"
                    report["rows"][-1] = row
                    budget.pop("on_model_progress", None)
                    save()
                    print(
                        json.dumps(
                            {
                                "case": row["case_id"],
                                "baseline": baseline,
                                "passed": row["grade"]["passed"],
                                "status": row["status"],
                                "reason": row["reason_code"],
                                "calls": row["usage"]["calls"],
                            }
                        ),
                        flush=True,
                    )
                    provider_errors = [
                        call
                        for call in row["model_calls"]
                        if call["error"] in {"LLM_API_ERROR", "LLM_CONNECTION_ERROR", "LLM_TIMEOUT"}
                    ]
                    if (
                        provider_errors
                        or budget["used"] >= budget["limit"]
                        and baseline == "agent"
                        and case != selected[-1]
                    ):
                        report["stop_reason"] = (
                            "PROVIDER_UNAVAILABLE"
                            if provider_errors
                            else "MODEL_CALL_BUDGET_EXHAUSTED"
                        )
                        raise SystemExit(1)
        report["complete"] = True
        if not budget["used"]:
            report["billing"] = {"amount": "0", "status": "NO_MODEL_CALLS"}
    except KeyboardInterrupt:
        report["stop_reason"] = "USER_INTERRUPTED"
        raise
    except (
        ConfigurationError,
        ValueError,
        RuntimeError,
        SQLAlchemyError,
        psycopg.Error,
        httpx.HTTPError,
    ):
        report["infrastructure_error"] = "EVALUATION_INTERRUPTED_PRIVATE_DETAILS_SUPPRESSED"
        raise SystemExit(1) from None
    finally:
        for row in report["rows"]:
            if row.get("measurement_status") == "RUNNING":
                row["measurement_status"] = "INTERRUPTED"
        budget.pop("on_model_progress", None)
        report["finished_at"] = datetime.now(UTC).isoformat()
        save()


if __name__ == "__main__":
    main()
