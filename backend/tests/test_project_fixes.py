import json
from datetime import datetime

import httpx
import pytest

from app.agent.tools import ReadTools
from app.experiments import llm_smoke
from tests.test_llm_smoke import mock_provider


def test_read_attempt_durations_exclude_backoff_and_survive_persistence():
    clock, saved, count = [0.0], [], [0]

    def handler(request):
        count[0] += 1
        clock[0] += (0.2, 0.05, 0.08)[count[0] - 1]
        if count[0] < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "id": "T1",
                "tenant_id": "demo",
                "subject": "Synthetic",
                "body": "Synthetic",
                "bound_order_id": None,
                "status": "OPEN",
            },
        )

    def sleep(seconds):
        clock[0] += seconds

    with httpx.Client(
        base_url="http://simulator", transport=httpx.MockTransport(handler)
    ) as client:
        reader = ReadTools(
            client, "demo", "T1", save=saved.append, monotonic=lambda: clock[0], sleep=sleep
        )
        result = reader.execute("get_ticket", '{"ticket_id":"T1"}')
    assert result["ok"] and saved == [result]
    assert [a["duration_ms"] for a in result["attempts"]] == [200, 50, 80]
    assert result["meta"]["duration_ms"] == 1830
    assert all(datetime.fromisoformat(a["started_at"]).tzinfo for a in result["attempts"])


def test_smoke_second_request_failure_keeps_first_usage(monkeypatch, capsys):
    mock_provider(monkeypatch)
    original = llm_smoke.client

    def client(settings):
        llm = original(settings)
        create = llm.chat.completions.create
        count = 0

        def fail_second(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                from openai import APIConnectionError

                raise APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
            return create(**kwargs)

        llm.chat.completions.create = fail_second
        return llm

    monkeypatch.setattr(llm_smoke, "client", client)
    monkeypatch.setattr("sys.argv", ["llm_smoke", "--kind", "tool"])
    with pytest.raises(SystemExit) as exited:
        llm_smoke.main()
    result = json.loads(capsys.readouterr().out)
    assert exited.value.code == 1 and result["error"] == "LLM_CONNECTION_ERROR"
    assert result["model_call_count"] == 2 and result["known_total_tokens"] == 14
    assert result["calls"][0]["usage"]["total_tokens"] == 14
    assert result["calls"][1]["usage"] is None and result["usage_unknown"] is True
    assert [call["state"] for call in result["calls"]] == ["COMPLETED", "FAILED"]
    assert all(call["duration_ms"] >= 0 for call in result["calls"])
    assert "test-only-secret" not in json.dumps(result)


def test_smoke_protocol_failure_retains_returned_usage(monkeypatch, capsys):
    mock_provider(monkeypatch, finish_reason="length")
    monkeypatch.setattr("sys.argv", ["llm_smoke", "--kind", "text"])
    with pytest.raises(SystemExit):
        llm_smoke.main()
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "LLM_PROTOCOL_ERROR" and result["known_total_tokens"] == 14
    assert result["model_call_count"] == 1 and result["usage_unknown"] is False
