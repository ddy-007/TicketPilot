from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def offline_settings():
    return Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://demo:demo@127.0.0.1:1/test",
        checkpoint_database_url="postgresql://demo:demo@127.0.0.1:1/test",
    )


def test_live_health_does_not_require_database(monkeypatch):
    def unexpected_query(*args):
        raise AssertionError("Live health must not query PostgreSQL.")

    monkeypatch.setattr("app.main.check_database", unexpected_query)
    monkeypatch.setattr("app.main.check_checkpoint_database", unexpected_query)
    with TestClient(create_app(offline_settings())) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_unavailable_database_without_secrets():
    with TestClient(create_app(offline_settings())) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "error", "checkpoint": "error"}
    assert "demo" not in response.text
