import json

import httpx
import pytest
from openai import OpenAI

from app.agent import llm
from app.config import Settings


def make_model(monkeypatch, handler, base_url="https://example.invalid/v1"):
    sdk = OpenAI
    monkeypatch.setattr(
        llm,
        "OpenAI",
        lambda **kwargs: sdk(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
    )
    return llm.Model(
        Settings(
            _env_file=None,
            app_database_url="postgresql+psycopg://unused",
            checkpoint_database_url="postgresql://unused",
            llm_base_url=base_url,
            llm_api_key="TEST_MODEL_SECRET",
            llm_model="mock-model",
        )
    )


def completion(message, finish="stop"):
    return httpx.Response(
        200,
        json={
            "id": "mock",
            "object": "chat.completion",
            "created": 0,
            "model": "mock-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        },
    )


@pytest.mark.parametrize("deepseek", [False, True])
def test_model_protocol_never_exposes_write_tools_or_secret_messages(monkeypatch, deepseek):
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if "tools" in payload:
            return completion(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "read1",
                            "function": {"name": "get_order", "arguments": '{"order_id":"O1"}'},
                        }
                    ],
                },
                "tool_calls",
            )
        return completion(
            {
                "content": '{"diagnosis_code":"OTHER","summary":"synthetic",'
                '"evidence_ids":["ev_test"],"suggested_action":"MANUAL_REVIEW"}'
            }
        )

    model = make_model(
        monkeypatch,
        handler,
        "https://api.deepseek.com/v1" if deepseek else "https://example.invalid/v1",
    )
    try:
        assert (
            model.complete([{"role": "user", "content": "synthetic"}])["calls"][0]["name"]
            == "get_order"
        )
        response = model.complete([{"role": "user", "content": "synthetic"}], diagnose=True)
        assert response["usage"]["total_tokens"] == 25
    finally:
        model.close()
    assert seen[0]["tool_choice"] == "auto"
    assert len(seen[0]["tools"]) == 3
    assert "retry_fulfillment" not in json.dumps(seen)
    assert "TEST_MODEL_SECRET" not in json.dumps(seen)
    assert "tools" not in seen[1]
    assert seen[1]["response_format"]["type"] == ("json_object" if deepseek else "json_schema")


def test_truncated_model_diagnosis_is_a_protocol_failure(monkeypatch):
    model = make_model(monkeypatch, lambda request: completion({"content": "{}"}, "length"))
    try:
        with pytest.raises(llm.ModelError, match="LLM_PROTOCOL_ERROR"):
            model.complete([], diagnose=True)
    finally:
        model.close()


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500])
def test_upstream_auth_error_body_is_not_exposed(monkeypatch, status):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "PRIVATE_ERROR_MARKER"}})

    model = make_model(monkeypatch, handler)
    try:
        with pytest.raises(llm.ModelError) as exc:
            model.complete([])
        assert str(exc.value) == "LLM_API_ERROR"
        assert exc.value.status_code == status
        assert "PRIVATE_ERROR_MARKER" not in str(exc.value)
    finally:
        model.close()
