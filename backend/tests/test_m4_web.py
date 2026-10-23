from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from tests.test_health import offline_settings


@pytest.mark.parametrize("role,token", [("operator", "o" * 40), ("approver", "a" * 40)])
def test_identity_is_derived_from_server_and_never_echoes_token(role, token):
    settings = offline_settings().model_copy(
        update={
            "demo_operator_token": SecretStr("o" * 40),
            "demo_approver_token": SecretStr("a" * 40),
        }
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json() == {"user": role, "role": role, "tenant": "demo"}
    assert token not in response.text


def test_workbench_serves_assets_with_security_headers_without_hiding_api_errors(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>TicketPilot</title>", encoding="utf-8"
    )
    (tmp_path / "assets" / "app.js").write_text("void 0;", encoding="utf-8")
    with TestClient(create_app(offline_settings(), frontend_dir=tmp_path)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["referrer-policy"] == "no-referrer"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/api/not-a-route").status_code == 404
        assert client.get("/assets/does-not-exist.js").status_code == 404
        for path in ("/.env", "/tests/ui_server.py", "/assets/..%2Findex.html"):
            assert client.get(path).status_code == 404
