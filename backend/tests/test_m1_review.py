import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.config import ConfigurationError
from simulator import smoke
from simulator.config import SimulatorSettings


def settings(read="r" * 40, write="w" * 40):
    return SimulatorSettings(
        _env_file=None,
        simulator_database_url="postgresql+psycopg://unused",
        simulator_read_token=read,
        simulator_write_token=write,
    )


@pytest.mark.parametrize(
    "read,write",
    [
        (" " * 40, "w" * 40),
        ("r" * 39 + "\n", "w" * 40),
        ("replace-with-at-least-32-random-characters", "w" * 40),
        ("r" * 40, "replace-with-a-different-at-least-32-character-value"),
    ],
)
def test_simulator_rejects_unusable_or_documented_placeholder_tokens(read, write):
    with pytest.raises(ConfigurationError):
        settings(read, write).require_tokens()


def test_smoke_rejects_historical_success_when_current_order_has_failed(monkeypatch, capsys):
    result = {
        "accepted": True,
        "order_id": "O2001",
        "fulfillment_status": "QUEUED",
        "version": 4,
        "operation_key": "fulfillment-retry:demo:O2001:v3",
    }

    def handler(request):
        if request.method == "POST":
            if request.headers["Authorization"] == "Bearer " + "r" * 40:
                return httpx.Response(403)
            return httpx.Response(200, json=result)
        path = request.url.path
        if path.endswith("/T1001"):
            body = {"bound_order_id": "O2001", "status": "OPEN"}
        elif path.endswith("/O2001"):
            body = {"fulfillment_status": "FAILED", "version": 5}
        elif path.endswith("fulfillment-events"):
            body = {
                "events": [
                    {"event_type": "RETRY_ENQUEUED", "operation_key": result["operation_key"]}
                ]
            }
        else:
            body = result
        return httpx.Response(200, json=body)

    real_client = httpx.Client
    monkeypatch.setattr(smoke, "get_simulator_settings", settings)
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SystemExit) as exc:
        smoke.main()
    assert exc.value.code == 1
    assert '"status": "passed"' not in capsys.readouterr().out


def test_smoke_checks_are_active_with_python_optimization():
    script = """
import httpx
from simulator import smoke
from simulator.config import SimulatorSettings
smoke.get_simulator_settings = lambda: SimulatorSettings(
    _env_file=None, simulator_database_url="postgresql+psycopg://unused",
    simulator_read_token="r"*40, simulator_write_token="w"*40,
)
real_client = httpx.Client
def handler(request):
    if request.url.path.endswith("/T1001"):
        body={"bound_order_id":"O2001","status":"OPEN"}
    elif request.url.path.endswith("/O2001"):
        body={"fulfillment_status":"FAILED","version":5}
    elif request.url.path.endswith("fulfillment-events"):
        body={"events":[]}
    else:
        body={"order_id":"O2001","fulfillment_status":"QUEUED","version":4}
    return httpx.Response(200,json=body)
smoke.httpx.Client = lambda **kwargs: real_client(
    **kwargs, transport=httpx.MockTransport(handler)
)
smoke.main()
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert '"status": "passed"' not in result.stdout
    assert '"status": "failed"' in result.stdout
