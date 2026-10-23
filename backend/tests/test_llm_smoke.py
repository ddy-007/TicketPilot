import json

import httpx
import pytest
from openai import BadRequestError, OpenAI
from pydantic import ValidationError

from app.config import ConfigurationError, Settings
from app.experiments import llm_smoke


def test_model_configuration_is_only_required_for_model_operations():
    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
    )
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        settings.require_llm()


def test_model_schema_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        llm_smoke.SmokeDiagnosis.model_validate(
            {
                "code": "QUEUE_PUBLISH_TIMEOUT",
                "summary": "test",
                "approved": True,
            }
        )


def mock_provider(
    monkeypatch,
    *,
    tool="get_demo_status",
    arguments='{"probe_id":"m0"}',
    base_url="https://example.invalid/v1",
    finish_reason=None,
):
    requests = []
    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
        llm_base_url=base_url,
        llm_model="mock-m0",
        llm_api_key="test-only-secret",
    )

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        message = {"role": "assistant", "content": "OK"}
        finish = "stop"
        if "tools" in body:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_m0",
                        "type": "function",
                        "function": {"name": tool, "arguments": arguments},
                    }
                ],
            }
            finish = "tool_calls"
        elif "response_format" in body:
            message["content"] = json.dumps(
                {"code": "QUEUE_PUBLISH_TIMEOUT", "summary": "Synthetic queue timeout."}
            )
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "created": 0,
                "object": "chat.completion",
                "model": "mock-m0",
                "choices": [
                    {"index": 0, "message": message, "finish_reason": finish_reason or finish}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )

    monkeypatch.setattr(llm_smoke, "get_settings", lambda: settings)
    monkeypatch.setattr(
        llm_smoke,
        "client",
        lambda settings: OpenAI(
            api_key="test-only-secret",
            base_url="https://example.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
    )
    return requests


def test_mock_probes_include_tool_result_round_trip(monkeypatch):
    requests = mock_provider(monkeypatch)
    assert llm_smoke.run_model_smoke()["status"] == "passed"
    assert llm_smoke.run_tool_smoke()["status"] == "passed"
    assert llm_smoke.run_structured_smoke()["status"] == "passed"
    assert len(requests) == 4
    tool_message = next(m for m in requests[2]["messages"] if m["role"] == "tool")
    assert tool_message["tool_call_id"] == "call_m0"
    assert json.loads(tool_message["content"]) == {"probe_id": "m0", "status": "OK"}
    assert "test-only-secret" not in json.dumps(requests)


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("retry_fulfillment", '{"probe_id":"m0"}'),
        ("get_demo_status", '{"probe_id":"other"}'),
        ("get_demo_status", '{"probe_id":"m0","approved":true}'),
    ],
)
def test_invalid_tool_request_is_blocked_before_result_round_trip(monkeypatch, tool, arguments):
    requests = mock_provider(monkeypatch, tool=tool, arguments=arguments)
    with pytest.raises((llm_smoke.ProbeError, ValidationError)):
        llm_smoke.run_tool_smoke()
    assert len(requests) == 1


def test_deepseek_probe_uses_verified_non_thinking_and_json_modes(monkeypatch):
    requests = mock_provider(monkeypatch, base_url="https://api.deepseek.com/v1")
    assert llm_smoke.run_tool_smoke()["status"] == "passed"
    result = llm_smoke.run_structured_smoke()
    assert result["validation_mode"] == "json_object+pydantic"
    assert all(r["thinking"] == {"type": "disabled"} for r in requests)
    assert "strict" not in requests[0]["tools"][0]["function"]
    assert "parallel_tool_calls" not in requests[0]
    assert requests[-1]["response_format"] == {"type": "json_object"}
    assert "JSON" in requests[-1]["messages"][0]["content"]


def test_cli_does_not_log_sensitive_upstream_error(monkeypatch, capsys):
    private_text = "TEST_ONLY_PRIVATE_CONTENT"

    def fail(*, calls=None):
        request = httpx.Request(
            "POST", "https://example.invalid/private", headers={"Authorization": private_text}
        )
        raise BadRequestError(
            private_text,
            response=httpx.Response(400, request=request),
            body={"error": {"message": private_text}},
        )

    monkeypatch.setattr(llm_smoke, "run_model_smoke", fail)
    monkeypatch.setattr("sys.argv", ["llm_smoke", "--kind", "text"])
    with pytest.raises(SystemExit) as exc:
        llm_smoke.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert private_text not in captured.out + captured.err
    assert "example.invalid" not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["error"] == "LLM_API_ERROR"
    assert result["status_code"] == 400


@pytest.mark.parametrize("runner", [llm_smoke.run_model_smoke, llm_smoke.run_structured_smoke])
def test_truncated_response_cannot_pass_even_when_content_is_valid(monkeypatch, runner):
    mock_provider(monkeypatch, finish_reason="length")
    with pytest.raises(llm_smoke.ProbeError):
        runner()


@pytest.mark.parametrize(
    "address",
    [
        "http://example.invalid/v1",
        "https://user:private@example.invalid/v1",
        "https://example.invalid/v1?key=private",
        "https://example.invalid/v1#private",
    ],
)
def test_invalid_model_address_is_rejected_without_echoing_values(address):
    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
        llm_base_url=address,
        llm_model="mock",
        llm_api_key="test-only-secret",
    )
    with pytest.raises(ConfigurationError) as exc:
        settings.require_llm()
    assert address not in str(exc.value)
    assert "private" not in str(exc.value)
