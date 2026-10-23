"""M1 HTTP acceptance checks against a newly created, isolated PostgreSQL database."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from simulator.config import SimulatorSettings
from simulator.main import create_simulator_app
from simulator.models import fulfillment_events, idempotency_records, orders, tickets
from simulator.seed import seed_demo

NOW = datetime(2026, 9, 18, 9, tzinfo=UTC)
READ = "test-read-" + "r" * 40
WRITE = "test-write-" + "w" * 40
BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def simulator_database_url():
    admin_url = os.getenv("M0_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set M0_TEST_ADMIN_URL to create an isolated M1 test database.")
    name = "ticketpilot_test_" + uuid4().hex
    params = conninfo_to_dict(admin_url)
    url = URL.create(
        "postgresql+psycopg",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", 5432)),
        database=name,
    ).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            result = subprocess.run(
                [sys.executable, "-m", "app.migrate"],
                cwd=BACKEND,
                env={
                    **os.environ,
                    "APP_DATABASE_URL": url,
                    "CHECKPOINT_DATABASE_URL": url.replace("postgresql+psycopg:", "postgresql:", 1),
                },
                capture_output=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
            assert result.returncode == 0, "Isolated database migration failed (output suppressed)."
            yield url
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def case(simulator_database_url):
    engine = create_engine(simulator_database_url)
    # This URL is created by this fixture, never from the demonstration environment.
    with engine.begin() as conn:
        for table in (idempotency_records, fulfillment_events, tickets, orders):
            conn.execute(table.delete())
    seed_demo(engine, NOW)
    settings = SimulatorSettings(
        _env_file=None,
        simulator_database_url=simulator_database_url,
        simulator_read_token=READ,
        simulator_write_token=WRITE,
    )
    app = create_simulator_app(settings, clock=lambda: NOW)
    with TestClient(app) as client:
        try:
            yield client, engine, app
        finally:
            engine.dispose()


def headers(ticket="T1001", tenant="demo", *, write=False, key=None):
    value = {
        "Authorization": "Bearer " + (WRITE if write else READ),
        "X-Tenant-ID": tenant,
        "X-Ticket-ID": ticket,
    }
    if key is not None:
        value["Idempotency-Key"] = key
    return value


def post(
    client,
    order="O2001",
    ticket="T1001",
    version=3,
    code="QUEUE_PUBLISH_TIMEOUT",
    tenant="demo",
    key=None,
    write=True,
    extra=None,
):
    payload = {"expected_version": version, "expected_error_code": code, **(extra or {})}
    return client.post(
        f"/internal/orders/{order}/retry-fulfillment",
        headers=headers(
            ticket, tenant, write=write, key=key or f"fulfillment-retry:{tenant}:{order}:v{version}"
        ),
        json=payload,
    )


def effect_count(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM sim.fulfillment_events WHERE event_type='RETRY_ENQUEUED'")
        ).scalar()


def test_s01_full_http_read_write_verify_and_replay(case):
    client, engine, _ = case
    ticket = client.get("/internal/tickets/T1001", headers=headers())
    assert ticket.status_code == 200
    assert ticket.json()["bound_order_id"] == "O2001"
    assert client.get("/internal/orders/O2001", headers=headers()).json()["version"] == 3
    observed = client.get("/internal/orders/O2001/fulfillment-events", headers=headers())
    assert observed.json()["events"][0]["error_code"] == "QUEUE_PUBLISH_TIMEOUT"

    first = post(client)
    assert first.status_code == 200
    expected = {
        "accepted": True,
        "order_id": "O2001",
        "fulfillment_status": "QUEUED",
        "version": 4,
        "operation_key": "fulfillment-retry:demo:O2001:v3",
    }
    assert first.json() == expected
    assert post(client).json() == expected
    assert (
        client.get("/internal/operations/fulfillment-retry:demo:O2001:v3", headers=headers()).json()
        == expected
    )
    order = client.get("/internal/orders/O2001", headers=headers()).json()
    assert (order["fulfillment_status"], order["version"]) == ("QUEUED", 4)
    assert client.get("/internal/tickets/T1001", headers=headers()).json()["status"] == "OPEN"
    assert effect_count(engine) == 1


@pytest.mark.parametrize(
    "number,code,status",
    [
        (2, "INVENTORY_UNAVAILABLE", 409),
        (3, "FULFILLMENT_NOT_FAILED", 409),
        (4, "FULFILLMENT_NOT_FAILED", 409),
        (5, "PAYMENT_NOT_READY", 409),
        (6, "NOT_FOUND", 404),
    ],
)
def test_s02_to_s06_reject_without_effect(case, number, code, status):
    client, engine, _ = case
    response = post(client, order=f"O{2000 + number}", ticket=f"T{1000 + number}")
    assert response.status_code == status
    assert response.json()["error"] == code
    assert effect_count(engine) == 0


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"version": 4}, "ORDER_VERSION_CHANGED"),
        ({"payment_status": "REFUNDED"}, "PAYMENT_NOT_READY"),
        ({"fulfillment_status": "CANCELLED"}, "FULFILLMENT_NOT_FAILED"),
        ({"fulfillment_status": "NOT_STARTED"}, "FULFILLMENT_NOT_FAILED"),
        (
            {"paid_at": NOW - timedelta(minutes=30) + timedelta(microseconds=1)},
            "PAYMENT_TOO_RECENT",
        ),
        ({"paid_at": None}, "PAYMENT_TOO_RECENT"),
        ({"paid_at": NOW + timedelta(minutes=1)}, "PAYMENT_TOO_RECENT"),
    ],
)
def test_rule_branches_use_fixed_clock(case, changes, code):
    client, engine, _ = case
    with engine.begin() as conn:
        conn.execute(orders.update().where(orders.c.id == "O2001").values(**changes))
    response = post(client)
    assert response.status_code == 409
    assert response.json()["error"] == code
    assert effect_count(engine) == 0


def test_exactly_thirty_minutes_is_allowed(case):
    client, engine, _ = case
    with engine.begin() as conn:
        conn.execute(
            orders.update()
            .where(orders.c.id == "O2001")
            .values(paid_at=NOW - timedelta(minutes=30))
        )
    assert post(client).status_code == 200


def test_latest_failure_code_and_missing_evidence_are_checked(case):
    client, engine, _ = case
    with engine.begin() as conn:
        conn.execute(
            fulfillment_events.insert().values(
                tenant_id="demo",
                id="new-failure",
                order_id="O2001",
                event_type="FULFILLMENT_FAILED",
                error_code="PERMANENT_FAILURE",
                created_at=NOW - timedelta(minutes=1),
            )
        )
    assert post(client).json()["error"] == "ERROR_CODE_NOT_ALLOWED"
    with engine.begin() as conn:
        conn.execute(fulfillment_events.delete().where(fulfillment_events.c.order_id == "O2001"))
    assert post(client).json()["error"] == "ERROR_CODE_NOT_ALLOWED"
    assert effect_count(engine) == 0


def test_request_validation_and_unknown_fields_do_not_echo_input(case):
    client, engine, _ = case
    invalid = post(client, extra={"private_field": "PRIVATE_TEST_MARKER"})
    assert invalid.status_code == 422
    assert "PRIVATE_TEST_MARKER" not in invalid.text
    for value in [True, "3", 0]:
        assert post(client, version=value).status_code == 422
    assert effect_count(engine) == 0


def test_permission_tenant_and_binding_precede_operation_replay(case):
    client, engine, _ = case
    assert post(client, write=False).status_code == 403
    assert client.get("/internal/tickets/T1001").status_code == 403
    assert post(client, tenant="other").status_code == 404
    assert post(client, ticket="T1002").status_code == 404
    assert post(client).status_code == 200
    assert post(client, tenant="other", key="fulfillment-retry:demo:O2001:v3").status_code == 404
    assert post(client, ticket="T1002").status_code == 404
    assert (
        client.get(
            "/internal/operations/fulfillment-retry:demo:O2001:v3", headers=headers(ticket="T1002")
        ).status_code
        == 404
    )
    assert client.get("/internal/orders/O2002", headers=headers()).status_code == 404
    assert effect_count(engine) == 1


def test_conflicting_or_unstable_key_cannot_create_extra_effect(case):
    client, engine, _ = case
    assert post(client, key="random-key").status_code == 422
    assert post(client).status_code == 200
    conflict = post(client, code="OTHER")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "IDEMPOTENCY_CONFLICT"
    assert post(client, key="fulfillment-retry:demo:O2001:v4", version=4).status_code == 409
    assert effect_count(engine) == 1


def test_concurrent_distinct_tickets_same_operation_only_commit_once(case):
    client, engine, app = case
    with engine.begin() as conn:
        conn.execute(
            tickets.insert().values(
                tenant_id="demo",
                id="T1001-alias",
                bound_order_id="O2001",
                subject="synthetic alias",
                body="synthetic",
                status="OPEN",
            )
        )
    barrier = Barrier(2)

    def before_lock(conn, cursor, statement, params, context, executemany):
        # Both requests have missed the initial idempotency lookup before reaching this point.
        if "sim.tickets" in statement and "FOR UPDATE" in statement:
            barrier.wait(timeout=8)

    event.listen(app.state.engine, "before_cursor_execute", before_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda t: post(client, ticket=t), ["T1001", "T1001-alias"]))
    finally:
        event.remove(app.state.engine, "before_cursor_execute", before_lock)
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert effect_count(engine) == 1
    with engine.connect() as conn:
        assert conn.execute(select(idempotency_records.c.idempotency_key)).all() == [
            ("fulfillment-retry:demo:O2001:v3",)
        ]


@pytest.mark.parametrize("failed_table", ["fulfillment_events", "idempotency_records"])
def test_transaction_rolls_back_if_insert_fails(case, failed_table):
    client, engine, app = case

    def fail_event(conn, cursor, statement, params, context, executemany):
        if statement.startswith("INSERT INTO sim." + failed_table):
            raise RuntimeError("synthetic transaction insert failure")

    event.listen(app.state.engine, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(RuntimeError, match="synthetic transaction insert"):
            post(client)
    finally:
        event.remove(app.state.engine, "before_cursor_execute", fail_event)
    assert client.get("/internal/orders/O2001", headers=headers()).json()["version"] == 3
    assert (
        client.get("/internal/orders/O2001", headers=headers()).json()["fulfillment_status"]
        == "FAILED"
    )
    assert effect_count(engine) == 0
    with engine.connect() as conn:
        assert conn.execute(select(idempotency_records.c.idempotency_key)).first() is None


def test_seed_rerun_preserves_committed_effect(case):
    client, engine, _ = case
    assert post(client).status_code == 200
    seed_demo(engine, NOW + timedelta(days=1))
    assert client.get("/internal/orders/O2001", headers=headers()).json()["version"] == 4
    assert effect_count(engine) == 1


def test_seed_does_not_invent_failure_evidence_for_existing_order(case):
    client, engine, _ = case
    with engine.begin() as conn:
        conn.execute(fulfillment_events.delete().where(fulfillment_events.c.order_id == "O2001"))
    seed_demo(engine, NOW + timedelta(days=1))
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(fulfillment_events.c.id).where(fulfillment_events.c.order_id == "O2001")
            ).first()
            is None
        )
    response = post(client)
    assert response.status_code == 409
    assert response.json()["error"] == "ERROR_CODE_NOT_ALLOWED"
    assert effect_count(engine) == 0


def test_composite_foreign_key_rejects_cross_tenant_binding(case):
    _, engine, _ = case
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            tickets.insert().values(
                tenant_id="other",
                id="cross",
                subject="cross",
                body="cross",
                bound_order_id="O2001",
                status="OPEN",
            )
        )


def test_event_limit_and_operation_not_found(case):
    client, _, _ = case
    assert (
        client.get(
            "/internal/orders/O2001/fulfillment-events?limit=21", headers=headers()
        ).status_code
        == 422
    )
    assert client.get("/internal/operations/missing", headers=headers()).status_code == 404
    listed = client.get("/internal/tickets", headers=headers()).json()
    assert len(listed["tickets"]) == 6
