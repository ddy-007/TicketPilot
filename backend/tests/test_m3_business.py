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
