"""M3 acceptance against isolated PostgreSQL and a real local simulator process."""

import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import Settings
from app.execution.business import BusinessClient, RemoteError
from app.execution.db import Database, digest
from app.execution.worker import Worker, WorkerBusy, WorkerLockLost
from app.main import create_app
from simulator.models import fulfillment_events, idempotency_records, orders, tickets
from simulator.seed import seed_demo
from tests.agent_fakes import ScriptedModel

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)
READ, WRITE = "r" * 40, "w" * 40
OPERATOR, APPROVER = "o" * 40, "a" * 40


@pytest.fixture(scope="module")
def infrastructure():
    admin_uri = os.getenv("M0_TEST_ADMIN_URL")
    if not admin_uri:
        pytest.skip("Set M0_TEST_ADMIN_URL for isolated M3 tests.")
    name = "ticketpilot_test_" + uuid4().hex
    params = conninfo_to_dict(admin_uri)
    url = URL.create(
        "postgresql+psycopg",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", 5432)),
        database=name,
    ).render_as_string(hide_password=False)
    env = {
        **os.environ,
        "APP_DATABASE_URL": url,
        "CHECKPOINT_DATABASE_URL": url.replace("postgresql+psycopg:", "postgresql:", 1),
        "SIMULATOR_DATABASE_URL": url,
        "SIMULATOR_READ_TOKEN": READ,
        "SIMULATOR_WRITE_TOKEN": WRITE,
        "DEMO_OPERATOR_TOKEN": OPERATOR,
        "DEMO_APPROVER_TOKEN": APPROVER,
    }
    with psycopg.connect(admin_uri, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        server = None
        engine = None
        try:
            migration = subprocess.run(
                [sys.executable, "-m", "app.migrate"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert migration.returncode == 0, "Isolated M3 migration failed; details suppressed."
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env["SIMULATOR_BASE_URL"] = f"http://127.0.0.1:{port}"
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "tests.m3_process:create_simulator",
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
                        if (
                            probe.get(
                                env["SIMULATOR_BASE_URL"] + "/health/live", timeout=0.3
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("Isolated simulator did not start.")
            engine = create_engine(url)
            yield env, engine
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


class Case:
    def __init__(self, env, engine):
        self.env, self.engine = env, engine
        self.db = Database(engine)
        self.now = NOW
        self.settings = Settings(
            _env_file=None,
            app_database_url=env["APP_DATABASE_URL"],
            checkpoint_database_url=env["CHECKPOINT_DATABASE_URL"],
            simulator_base_url=env["SIMULATOR_BASE_URL"],
            simulator_read_token=READ,
            simulator_write_token=WRITE,
            demo_operator_token=OPERATOR,
            demo_approver_token=APPROVER,
        )
        self.app = create_app(self.settings)
        self.app.state.clock = lambda: self.now

    def worker(self, **kwargs):
        return Worker(
            self.engine,
            self.settings,
            model_factory=ScriptedModel,
            clock=lambda: self.now,
            **kwargs,
        )

    def tick(self, **kwargs):
        worker = self.worker(**kwargs)
        worker.start()
        try:
            return worker.once()
        finally:
            worker.close()

    def start(self, client, ticket="T1001", key=None):
        r = client.post(
            f"/api/tickets/{ticket}/runs",
            json={"auto_note": False},
            headers={"Authorization": "Bearer " + OPERATOR, "Idempotency-Key": key or uuid4().hex},
        )
        assert r.status_code == 202, r.status_code
        return UUID(r.json()["run_id"])

    def waiting(self, client):
        run_id = self.start(client)
        assert self.tick()
        row = self.db.run(run_id)
        assert row["status"] == "WAITING_APPROVAL"
        return run_id, self.db.approval(row["approval_id"])

    def decision(self, client, approval, value="APPROVED", token=APPROVER, plan_hash=None):
        return client.post(
            f"/api/approvals/{approval['id']}/decision",
            json={"decision": value, "plan_hash": plan_hash or approval["plan_hash"]},
            headers={"Authorization": "Bearer " + token},
        )

    def effects(self):
        with self.engine.connect() as conn:
            return conn.execute(
                select(fulfillment_events.c.id).where(
                    fulfillment_events.c.event_type == "RETRY_ENQUEUED"
                )
            ).all()


@pytest.fixture
def case(infrastructure):
    env, engine = infrastructure
    db = Database(engine)
    with engine.begin() as conn:
        conn.execute(db.runs.update().values(approval_id=None))
        for table in (
            db.run_events,
            db.tool_calls,
            db.operations,
            db.approvals,
            db.run_jobs,
            db.runs,
            idempotency_records,
            fulfillment_events,
            tickets,
            orders,
        ):
            conn.execute(table.delete())
    seed_demo(engine, NOW)
    value = Case(env, engine)
    with TestClient(value.app) as client:
        yield value, client


def test_approved_round_trip_keeps_ticket_open(case):
    c, api = case
    run_id, approval = c.waiting(api)
    assert c.effects() == []
    assert c.decision(api, approval).status_code == 202
    assert c.tick()
    run = c.db.run(run_id)
    assert run["status"] == "SUCCEEDED", run["result_json"]
    assert run["disposition"] == "REQUEUED"
    assert run["result_json"]["operation"]["status"] == "CONFIRMED"
    assert len(c.effects()) == 1
    with c.engine.connect() as conn:
        assert conn.execute(select(orders.c.version).where(orders.c.id == "O2001")).scalar() == 4
        assert (
            conn.execute(select(tickets.c.status).where(tickets.c.id == "T1001")).scalar() == "OPEN"
        )
    view = api.get(f"/api/runs/{run_id}", headers={"Authorization": "Bearer " + OPERATOR})
    assert view.status_code == 200 and view.json()["result_json"]["executed"] is True
    assert view.json()["approval"]["plan"]["args"]["expected_version"] == 3
    approval_view = api.get(
        f"/api/approvals/{approval['id']}", headers={"Authorization": "Bearer " + APPROVER}
    )
    assert approval_view.status_code == 200
    assert approval_view.json()["approval_id"] == str(approval["id"])
    assert approval_view.json()["plan_hash"] == digest(approval_view.json()["plan"])


def test_rejection_has_zero_writes(case):
    c, api = case
    run_id, approval = c.waiting(api)
    assert c.decision(api, approval, "REJECTED").status_code == 202
    c.tick()
    assert c.db.run(run_id)["status"] == "REJECTED"
    assert c.effects() == []


def test_version_change_blocks_old_proposal(case):
    c, api = case
    run_id, approval = c.waiting(api)
    with c.engine.begin() as conn:
        conn.execute(orders.update().where(orders.c.id == "O2001").values(version=4))
    assert c.decision(api, approval).status_code == 202
    c.tick()
    assert c.db.run(run_id)["status"] == "ESCALATED"
    assert c.effects() == []


def test_operator_hash_conflict_and_repeated_decision(case):
    c, api = case
    run_id, approval = c.waiting(api)
    assert c.decision(api, approval, token=OPERATOR).status_code == 403
    assert c.decision(api, approval, plan_hash="0" * 64).status_code == 409
    assert c.decision(api, approval).status_code == 202
    assert c.decision(api, approval).status_code == 202
    assert c.decision(api, approval, "REJECTED").status_code == 409
    with c.engine.connect() as conn:
        assert (
            len(
                conn.execute(
                    select(c.db.run_jobs.c.id).where(
                        c.db.run_jobs.c.run_id == run_id, c.db.run_jobs.c.kind == "RESUME"
                    )
                ).all()
            )
            == 1
        )
    c.tick()
    assert c.decision(api, approval).status_code == 202
    assert len(c.effects()) == 1


def test_start_idempotency_and_active_uniqueness(case):
    c, api = case
    key = uuid4().hex
    run_id = c.start(api, key=key)
    assert c.start(api, key=key) == run_id
    assert (
        api.post(
            "/api/tickets/T1002/runs",
            json={"auto_note": False},
            headers={"Authorization": "Bearer " + OPERATOR, "Idempotency-Key": key},
        ).status_code
        == 409
    )
    assert (
        api.post(
            "/api/tickets/T1001/runs",
            json={"auto_note": False},
            headers={"Authorization": "Bearer " + OPERATOR, "Idempotency-Key": "different"},
        ).status_code
        == 409
    )
    with c.engine.connect() as conn:
        assert len(conn.execute(select(c.db.run_jobs.c.id)).all()) == 1


def test_expiry_closes_and_releases_ticket(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.now = approval["expires_at"] + timedelta(seconds=1)
    assert c.decision(api, approval).status_code == 410
    c.tick()
    assert c.db.run(run_id)["status"] == "ESCALATED"
    assert c.effects() == []
    assert c.start(api) != run_id


def test_idle_worker_expiry_scan(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.now = approval["expires_at"] + timedelta(seconds=1)
    c.tick()
    assert c.db.approval(approval["id"])["decision"] == "EXPIRED"
    assert c.db.run(run_id)["status"] == "ESCALATED"
    assert c.effects() == []


def test_concurrent_approval_decisions_only_one_wins(case):
    c, api = case
    run_id, approval = c.waiting(api)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda value: c.decision(api, approval, value), ["APPROVED", "REJECTED"])
        )
    assert sorted(r.status_code for r in responses) == [202, 409]
    with c.engine.connect() as conn:
        assert (
            len(
                conn.execute(
                    select(c.db.run_jobs.c.id).where(
                        c.db.run_jobs.c.run_id == run_id, c.db.run_jobs.c.kind == "RESUME"
                    )
                ).all()
            )
            == 1
        )


def test_plan_cannot_be_updated_in_database(case):
    c, api = case
    _, approval = c.waiting(api)
    with pytest.raises(DBAPIError), c.engine.begin() as conn:
        conn.execute(
            c.db.approvals.update()
            .where(c.db.approvals.c.id == approval["id"])
            .values(plan_hash="0" * 64)
        )


def test_single_worker_lock(case):
    c, _ = case
    first, second = c.worker(), c.worker()
    first.start()
    try:
        with pytest.raises(WorkerBusy):
            second.start()
    finally:
        first.close()


def child_worker(c, mode):
    result = subprocess.run(
        [sys.executable, "-m", "tests.m3_process", mode],
        cwd=ROOT,
        env=c.env,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return result.returncode


def test_process_crash_after_interrupt_checkpoint_recovers_waiting(case):
    c, api = case
    run_id = c.start(api)
    assert child_worker(c, "after_checkpoint") == 91
    assert c.db.run(run_id)["status"] == "RUNNING"
    assert c.tick()
    row = c.db.run(run_id)
    assert row["status"] == "WAITING_APPROVAL"
    assert c.effects() == []
    approval = c.db.approval(row["approval_id"])
    assert c.decision(api, approval).status_code == 202
    assert child_worker(c, "normal") == 0
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1


def test_committed_approval_survives_before_worker_resume(case):
    c, api = case
    run_id, approval = c.waiting(api)
    assert c.decision(api, approval).status_code == 202
    assert child_worker(c, "normal") == 0
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1


def test_process_crash_after_remote_commit_queries_before_replay(case):
    c, api = case
    run_id, approval = c.waiting(api)
    assert c.decision(api, approval).status_code == 202
    assert child_worker(c, "after_write") == 92
    assert len(c.effects()) == 1
    with c.engine.connect() as conn:
        assert conn.execute(select(c.db.operations.c.status)).scalar() == "PREPARED"
    assert child_worker(c, "normal") == 0
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1
    with c.engine.connect() as conn:
        assert conn.execute(select(c.db.operations.c.post_attempts)).scalar() == 1


def test_terminal_checkpoint_job_sync_does_not_repeat_write(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    assert child_worker(c, "after_checkpoint") == 91
    assert c.db.run(run_id)["status"] == "RUNNING"
    assert len(c.effects()) == 1
    c.tick()
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1


def test_lost_write_response_is_confirmed_by_remote_query(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)

    class LostResponse(BusinessClient):
        def request(self, method, *args, **kwargs):
            result = super().request(method, *args, **kwargs)
            if method == "POST":
                raise RemoteError("BUSINESS_UNAVAILABLE")
            return result

    c.tick(business_factory=lambda: LostResponse(c.settings))
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1


def test_lost_response_and_unavailable_query_preserves_unknown(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)

    class UnknownResponse(BusinessClient):
        def request(self, method, path, *args, **kwargs):
            if method == "GET" and path.startswith("/internal/operations/"):
                raise RemoteError("BUSINESS_UNAVAILABLE")
            result = super().request(method, path, *args, **kwargs)
            if method == "POST":
                raise RemoteError("BUSINESS_UNAVAILABLE")
            return result

    c.tick(business_factory=lambda: UnknownResponse(c.settings))
    result = c.db.run(run_id)["result_json"]
    assert result["operation"]["status"] == "UNKNOWN"
    assert result["executed"] is None and result["warnings"]
    assert len(c.effects()) == 1


def prepare_operation(c, run_id, approval, *, attempts=1):
    plan = approval["plan_json"]
    key = f"fulfillment-retry:demo:O2001:v{plan['args']['expected_version']}"
    semantic = {
        "operation_type": "retry_fulfillment",
        "tenant_id": "demo",
        "order_id": "O2001",
        "expected_version": plan["args"]["expected_version"],
        "expected_error_code": "QUEUE_PUBLISH_TIMEOUT",
    }
    with c.engine.begin() as conn:
        conn.execute(
            c.db.operations.insert().values(
                id=uuid4(),
                origin_run_id=run_id,
                tenant_id="demo",
                tool_name="retry_fulfillment",
                idempotency_key=key,
                request_hash=digest(semantic),
                status="PREPARED",
                post_attempts=attempts,
                started_at=c.now,
            )
        )
    return key


def test_404_then_old_request_commits_before_recheck_is_confirmed(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    key = prepare_operation(c, run_id, approval)

    class LateCommit(BusinessClient):
        missing_once = True

        def request(self, method, path, *args, **kwargs):
            if self.missing_once and method == "GET" and path.startswith("/internal/operations/"):
                self.missing_once = False
                super().request(
                    "POST",
                    "/internal/orders/O2001/retry-fulfillment",
                    "demo",
                    "T1001",
                    payload={"expected_version": 3, "expected_error_code": "QUEUE_PUBLISH_TIMEOUT"},
                    key=key,
                )
                raise RemoteError("BUSINESS_HTTP_ERROR", 404)
            return super().request(method, path, *args, **kwargs)

    c.tick(business_factory=lambda: LateCommit(c.settings))
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1
    with c.engine.connect() as conn:
        assert conn.execute(select(c.db.operations.c.post_attempts)).scalar() == 1


def test_404_and_unrelated_version_change_remains_unknown(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    prepare_operation(c, run_id, approval)
    with c.engine.begin() as conn:
        conn.execute(orders.update().where(orders.c.id == "O2001").values(version=4))
    c.tick()
    result = c.db.run(run_id)["result_json"]
    assert result["operation"]["status"] == "UNKNOWN"
    assert result["executed"] is None
    assert c.effects() == []


def test_recovery_can_resend_same_key_when_old_effect_not_observed(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    prepare_operation(c, run_id, approval)
    c.tick()
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1
    with c.engine.connect() as conn:
        assert conn.execute(select(c.db.operations.c.post_attempts)).scalar() == 2


def test_expired_approved_plan_cannot_start_new_post(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    c.now = approval["expires_at"] + timedelta(seconds=1)
    c.tick()
    assert c.db.run(run_id)["status"] == "ESCALATED"
    assert c.effects() == []


def test_expired_approval_still_confirms_previously_committed_effect(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    assert child_worker(c, "after_write") == 92
    c.now = approval["expires_at"] + timedelta(seconds=1)
    c.tick()
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1


def test_resume_payload_cannot_authorize_a_pending_approval(case):
    c, api = case
    run_id, approval = c.waiting(api)
    from langgraph.types import Command

    from app.db import open_checkpointer
    from app.execution.workflow import Workflow

    remote = BusinessClient(c.settings)
    workflow = Workflow(
        c.db, run_id, c.settings, remote, model=ScriptedModel(), clock=lambda: c.now
    )
    try:
        with open_checkpointer(c.settings) as cp:
            graph = workflow.build(cp)
            graph.invoke(
                Command(resume={"approved": True, "approval_id": str(uuid4())}),
                {"configurable": {"thread_id": str(run_id)}},
            )
    finally:
        remote.close()
    assert c.db.approval(approval["id"])["decision"] == "PENDING"
    assert c.effects() == []


def test_worker_stops_when_session_lock_is_lost(case):
    c, api = case
    c.start(api)
    worker = c.worker()
    worker.start()
    try:
        with c.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": worker.backend_pid})
        with pytest.raises(WorkerLockLost):
            worker.once()
        assert c.effects() == []
    finally:
        worker.close()


def test_run_and_start_job_rollback_together(case):
    c, api = case

    def fail_job(conn, cursor, statement, params, context, executemany):
        if statement.startswith("INSERT INTO app.run_jobs"):
            raise IntegrityError("synthetic job failure", None, Exception("synthetic"))

    event.listen(c.engine, "before_cursor_execute", fail_job)
    # API lifespan uses its own engine for the same isolated database.
    event.listen(c.app.state.engine, "before_cursor_execute", fail_job)
    try:
        response = api.post(
            "/api/tickets/T1001/runs",
            json={"auto_note": False},
            headers={
                "Authorization": "Bearer " + OPERATOR,
                "Idempotency-Key": uuid4().hex,
            },
        )
        assert response.status_code == 409
    finally:
        event.remove(c.engine, "before_cursor_execute", fail_job)
        event.remove(c.app.state.engine, "before_cursor_execute", fail_job)
    with c.engine.connect() as conn:
        assert conn.execute(select(c.db.runs.c.id)).first() is None
        assert conn.execute(select(c.db.run_jobs.c.id)).first() is None


def test_approval_and_resume_job_rollback_together(case):
    c, api = case
    run_id, approval = c.waiting(api)

    def fail_job(conn, cursor, statement, params, context, executemany):
        if statement.startswith("INSERT INTO app.run_jobs"):
            raise IntegrityError("synthetic resume failure", None, Exception("synthetic"))

    event.listen(c.app.state.engine, "before_cursor_execute", fail_job)
    try:
        assert c.decision(api, approval).status_code == 503
    finally:
        event.remove(c.app.state.engine, "before_cursor_execute", fail_job)
    assert c.db.approval(approval["id"])["decision"] == "PENDING"
    assert c.db.run(run_id)["status"] == "WAITING_APPROVAL"


def test_tenant_filter_validation_and_event_cursor(case):
    c, api = case
    run_id, approval = c.waiting(api)
    auth = {"Authorization": "Bearer " + OPERATOR}
    assert api.get(f"/api/runs/{run_id}").status_code == 401
    assert api.get("/api/runs/not-a-uuid", headers=auth).status_code == 422
    all_ids = []
    after = 0
    while True:
        response = api.get(f"/api/runs/{run_id}/events?after_id={after}&limit=2", headers=auth)
        rows = response.json()["events"]
        if not rows:
            break
        all_ids.extend(row["id"] for row in rows)
        after = rows[-1]["id"]
    assert all_ids == sorted(set(all_ids)) and len(all_ids) > 2
    with c.engine.begin() as conn:
        conn.execute(c.db.runs.update().where(c.db.runs.c.id == run_id).values(tenant_id="other"))
    assert api.get(f"/api/runs/{run_id}", headers=auth).status_code == 404
    assert api.get(f"/api/approvals/{approval['id']}", headers=auth).status_code == 404
    assert api.get(f"/api/runs/{run_id}/events", headers=auth).status_code == 404
    assert c.decision(api, approval).status_code == 404


def test_rebinding_between_start_and_load_is_rejected(case):
    c, api = case
    run_id = c.start(api)
    with c.engine.begin() as conn:
        conn.execute(tickets.update().where(tickets.c.id == "T1001").values(bound_order_id="O2002"))
    c.tick()
    assert c.db.run(run_id)["status"] == "ESCALATED"
    assert c.effects() == []


def test_three_technical_failures_stop_automatic_job_retry(case, monkeypatch):
    c, api = case
    run_id = c.start(api)
    from app.execution.workflow import Workflow

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic checkpoint error")

    monkeypatch.setattr(Workflow, "build", fail)
    for _ in range(3):
        c.tick()
    run = c.db.run(run_id)
    assert run["status"] == "FAILED"
    assert run["failure_code"] == "JOB_ATTEMPTS_EXHAUSTED"
    assert c.tick() is False


def test_same_key_post_budget_exhaustion_preserves_unknown(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    prepare_operation(c, run_id, approval, attempts=2)
    c.tick()
    result = c.db.run(run_id)["result_json"]
    assert result["executed"] is None
    assert result["operation"]["status"] == "UNKNOWN"
    assert c.effects() == []


def test_durable_model_intents_prevent_budget_reset_after_lost_checkpoints(case):
    c, api = case
    run_id = c.start(api)
    with c.engine.begin() as conn:
        for index in range(6):
            c.db.event(
                conn,
                run_id,
                "LLM_CALL_STARTED",
                {
                    "call_id": str(index),
                    "usage": None,
                    "usage_unknown": True,
                },
                f"lost:{index}",
            )
    c.tick()
    result = c.db.run(run_id)["result_json"]
    assert result["status"] == "FAILED"
    assert result["reason_code"] == "MODEL_BUDGET_EXHAUSTED"
    assert result["model_call_count"] == 6
    assert result["usage_unknown"] is True
    assert c.effects() == []


def test_unsupported_note_and_untrusted_role_are_rejected_without_side_effects(case):
    c, api = case
    auth = {"Authorization": "Bearer " + OPERATOR, "Idempotency-Key": uuid4().hex}
    assert (
        api.post("/api/tickets/T1001/runs", headers=auth, json={"auto_note": True}).status_code
        == 422
    )
    assert (
        api.post(
            "/api/tickets/T1001/runs", headers=auth, json={"auto_note": False, "role": "approver"}
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/api/tickets/T1001/runs",
            headers={**auth, "Authorization": "Bearer " + APPROVER},
            json={"auto_note": False},
        ).status_code
        == 403
    )
    with c.engine.connect() as conn:
        assert conn.execute(select(c.db.runs.c.id)).first() is None


def test_review_approval_clock_is_checked_after_row_lock(case):
    c, api = case
    _, approval = c.waiting(api)
    c.now = approval["expires_at"] - timedelta(seconds=1)

    def advance_after_wait(conn, cursor, statement, params, context, executemany):
        if "app.approvals" in statement and "FOR UPDATE" in statement:
            c.now = approval["expires_at"] + timedelta(seconds=1)

    event.listen(c.app.state.engine, "after_cursor_execute", advance_after_wait)
    try:
        response = c.decision(api, approval)
    finally:
        event.remove(c.app.state.engine, "after_cursor_execute", advance_after_wait)
    assert response.status_code == 410
    assert c.db.approval(approval["id"])["decision"] == "EXPIRED"


def test_review_expiry_during_post_intent_commit_starts_no_post(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)

    def advance(conn, cursor, statement, params, context, executemany):
        if statement.startswith("UPDATE app.operations SET post_attempts="):
            c.now = approval["expires_at"] + timedelta(seconds=1)

    event.listen(c.engine, "after_cursor_execute", advance)
    try:
        c.tick()
    finally:
        event.remove(c.engine, "after_cursor_execute", advance)
    assert c.effects() == []
    assert c.db.run(run_id)["status"] == "ESCALATED"


def test_review_worker_retries_client_initialization_failures_bounded(case):
    c, api = case
    run_id = c.start(api)

    def fail():
        raise ValueError("synthetic private client configuration")

    for _ in range(3):
        c.tick(business_factory=fail)
    assert c.db.run(run_id)["status"] == "FAILED"
    assert c.db.run(run_id)["failure_code"] == "JOB_ATTEMPTS_EXHAUSTED"
    assert c.tick() is False


def test_review_third_attempt_saved_terminal_still_syncs_success(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)
    with c.engine.begin() as conn:
        conn.execute(
            c.db.run_jobs.update()
            .where(c.db.run_jobs.c.run_id == run_id, c.db.run_jobs.c.kind == "RESUME")
            .values(attempts=2)
        )

    def fail_after_saved(point):
        if point == "after_checkpoint":
            raise RuntimeError("synthetic state sync error")

    c.tick(fault=fail_after_saved)
    if c.db.run(run_id)["status"] != "SUCCEEDED":
        c.tick()
    assert c.db.run(run_id)["status"] == "SUCCEEDED"
    assert len(c.effects()) == 1


def test_review_sensitive_reason_is_not_stored_or_reflected(case):
    c, api = case
    _, approval = c.waiting(api)
    private = c.settings.demo_approver_token.get_secret_value()
    response = api.post(
        f"/api/approvals/{approval['id']}/decision",
        headers={
            "Authorization": "Bearer " + APPROVER,
        },
        json={
            "decision": "REJECTED",
            "plan_hash": approval["plan_hash"],
            "reason": "test " + private,
        },
    )
    assert response.status_code == 202
    view = api.get(
        f"/api/approvals/{approval['id']}", headers={"Authorization": "Bearer " + OPERATOR}
    )
    assert private not in view.text
    assert private not in c.db.approval(approval["id"])["reason"]


def test_review_business_failure_is_not_relabelled_stale_plan(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)

    def handle(request):
        return httpx.Response(503, json={"error": "synthetic"})

    def remote():
        client = BusinessClient(c.settings)
        client.read.close()
        client.read = httpx.Client(
            base_url=c.settings.simulator_base_url, transport=httpx.MockTransport(handle)
        )
        return client

    c.tick(business_factory=remote)
    result = c.db.run(run_id)["result_json"]
    assert result["reason_code"] == "BUSINESS_UNAVAILABLE"
    assert result["status"] == "FAILED"
    assert c.effects() == []


def test_review_readiness_detects_missing_m3_business_table(case):
    c, api = case
    with c.engine.begin() as conn:
        conn.execute(text("ALTER TABLE app.operations RENAME TO operations_missing_probe"))
    try:
        response = api.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "error"
    finally:
        with c.engine.begin() as conn:
            conn.execute(text("ALTER TABLE app.operations_missing_probe RENAME TO operations"))


def test_review_expiry_inside_remote_dispatch_guard_starts_no_post(case):
    c, api = case
    run_id, approval = c.waiting(api)
    c.decision(api, approval)

    class DelayedDispatch(BusinessClient):
        def request(self, method, *args, **kwargs):
            if method == "POST":
                c.now = approval["expires_at"] + timedelta(seconds=1)
            return super().request(method, *args, **kwargs)

    c.tick(business_factory=lambda: DelayedDispatch(c.settings))
    assert c.effects() == []
    assert c.db.run(run_id)["status"] == "ESCALATED"


def test_workbench_lists_only_current_tenant_active_runs(case):
    c, api = case
    run_id, approval = c.waiting(api)
    other = dict(c.db.run(run_id))
    other_id = uuid4()
    other.update(id=other_id, thread_id=str(other_id), tenant_id="other", approval_id=None)
    with c.engine.begin() as conn:
        conn.execute(c.db.runs.insert().values(**other))
    headers = {"Authorization": "Bearer " + OPERATOR}
    response = api.get("/api/tickets", headers=headers)
    assert response.status_code == 200
    current = next(row for row in response.json()["tickets"] if row["id"] == "T1001")
    assert current["active_run_id"] == str(run_id)
    assert str(other_id) not in response.text
    assert c.decision(api, approval, "REJECTED").status_code == 202
    c.tick()
    response = api.get("/api/tickets", headers=headers)
    current = next(row for row in response.json()["tickets"] if row["id"] == "T1001")
    assert current["active_run_id"] is None
