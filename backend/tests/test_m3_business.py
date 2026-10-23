from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.execution.business import BusinessClient, RemoteError


def test_execution_http_retries_share_logical_id_and_never_store_headers():
    calls = []
    audit = []

    def handle(request):
        calls.append(request)
        return (
            httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"status": "ok"})
        )

    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
    )
    with httpx.Client(base_url="http://simulator", transport=httpx.MockTransport(handle)) as http:
        remote = BusinessClient(settings, read=http)
        remote.audit = audit.append
        assert remote.request("GET", "/test", "demo", "T1") == {"status": "ok"}
    assert len(audit) == 2
    assert audit[0]["meta"]["logical_call_id"] == audit[1]["meta"]["logical_call_id"]
    assert [row["attempts"][0]["attempt_no"] for row in audit] == [1, 2]
    assert audit[0]["ok"] is False and audit[1]["ok"] is True
    assert all("headers" not in row for row in audit)


def test_pre_dispatch_approval_failure_has_correct_safe_audit_code():
    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
    )
    requests = []
    records = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(base_url="http://simulator", transport=httpx.MockTransport(handle)) as http:
        remote = BusinessClient(settings, read=http, write=http)
        remote.audit = records.append

        def unauthorized():
            raise RemoteError("APPROVAL_EXPIRED")

        remote.before_post = unauthorized
        with pytest.raises(RemoteError, match="APPROVAL_EXPIRED"):
            remote.request("POST", "/write", "demo")
    assert requests == []
    assert records[0]["error"]["code"] == "APPROVAL_EXPIRED"
    assert records[0]["meta"]["started_at"] is None
    assert records[0]["meta"]["duration_ms"] == 0
    assert records[0]["meta"]["backoff_ms"] == 0


@pytest.mark.parametrize(
    "method,outcomes", [("GET", [503, 503, 200]), ("GET", ["network", 200]), ("POST", [503])]
)
def test_attempt_timing_separates_http_from_backoff_and_authorization(method, outcomes):
    elapsed = [0.0]
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    records, waits, requests = [], [], []

    def handle(request):
        outcome = outcomes[len(requests)]
        requests.append(request)
        elapsed[0] += 0.025
        if outcome == "network":
            raise httpx.ConnectError("synthetic", request=request)
        return httpx.Response(outcome, json={"status": "ok"})

    def sleep(seconds):
        waits.append(seconds)
        elapsed[0] += seconds

    def authorize():
        elapsed[0] += 0.7

    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
    )
    with httpx.Client(base_url="http://simulator", transport=httpx.MockTransport(handle)) as http:
        remote = BusinessClient(
            settings,
            read=http,
            write=http,
            clock=lambda: elapsed[0],
            sleep=sleep,
            now=lambda: epoch + timedelta(seconds=elapsed[0]),
        )
        remote.audit = records.append
        remote.before_post = authorize
        if method == "POST":
            with pytest.raises(RemoteError, match="BUSINESS_HTTP_ERROR"):
                remote.request(method, "/test", "demo")
        else:
            assert remote.request(method, "/test", "demo") == {"status": "ok"}
    assert len(requests) == len(outcomes)
    assert [r["meta"]["duration_ms"] for r in records] == [25] * len(outcomes)
    expected_waits = [0.1, 0.2] if outcomes == [503, 503, 200] else []
    assert waits == expected_waits
    assert [r["meta"]["backoff_ms"] for r in records] == (
        [100, 200, 0] if expected_waits else [0] * len(outcomes)
    )
    expected_start = 0.7 if method == "POST" else 0
    for record in records:
        meta = record["meta"]
        assert datetime.fromisoformat(meta["started_at"]) == epoch + timedelta(
            seconds=expected_start
        )
        assert datetime.fromisoformat(meta["observed_at"]) == epoch + timedelta(
            seconds=expected_start + 0.025
        )
        expected_start += (meta["duration_ms"] + meta["backoff_ms"]) / 1000
