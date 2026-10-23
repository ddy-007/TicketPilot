import warnings

import pytest

from app.agent.graph import Agent
from app.agent.llm import ModelError
from tests.agent_fakes import ScriptedModel
from tests.test_m2_agent import NOW, reader_and_requests
from tests.test_m2_protocol import completion, make_model


def test_exact_read_budget_still_allows_diagnosis_with_complete_evidence():
    reader, requests = reader_and_requests(limit=3)
    result = Agent(reader, ScriptedModel(), clock=lambda: NOW).run()
    assert result["status"] == "PROPOSAL_READY"
    assert result["read_call_count"] == 3 and len(requests) == 3


def test_expired_zero_deadline_does_not_start_http():
    reader, requests = reader_and_requests()
    from app.agent.tools import ReadTools

    reader = ReadTools(reader.client, "demo", "T1", monotonic=lambda: 0.0, deadline=0.0)
    result = Agent(reader, ScriptedModel(), clock=lambda: NOW).run()
    assert result["status"] == "FAILED"
    assert len(requests) == 0


@pytest.mark.parametrize(
    "message",
    [
        {"content": None, "tool_calls": [{"id": "broken", "type": "function"}]},
        {
            "content": None,
            "tool_calls": [{"id": "broken", "type": "function", "function": {"name": "get_order"}}],
        },
    ],
)
def test_malformed_model_tool_response_is_a_safe_protocol_failure(monkeypatch, message):
    model = make_model(monkeypatch, lambda request: completion(message, "tool_calls"))
    try:
        with pytest.raises(ModelError, match="LLM_PROTOCOL_ERROR"):
            model.complete([])
    finally:
        model.close()


def test_rejected_truncated_response_still_counts_returned_usage(monkeypatch):
    model = make_model(monkeypatch, lambda request: completion({"content": "{}"}, "length"))
    reader, _ = reader_and_requests()
    result = Agent(reader, model, clock=lambda: NOW).run()
    model.close()
    assert result["status"] == "FAILED"
    assert result["reason_code"] == "LLM_PROTOCOL_ERROR"
    assert result["known_total_tokens"] == 25
    assert result["usage_unknown"] is False


def test_conflicting_model_evidence_cannot_form_proposal_even_at_read_budget():
    reader, _ = reader_and_requests(limit=3)
    result = Agent(
        reader, ScriptedModel(code="ALREADY_SHIPPED", action="NO_ACTION"), clock=lambda: NOW
    ).run()
    assert result["status"] == "ESCALATED"
    assert result["reason_code"] == "DIAGNOSIS_CONFLICT"
    assert result["proposal"] is None


def test_malformed_sdk_response_never_echoes_private_values_in_warnings(monkeypatch):
    private = "SYNTHETIC_PRIVATE_RESPONSE_MARKER"
    model = make_model(
        monkeypatch,
        lambda request: completion(
            {
                "content": None,
                "tool_calls": [{"id": "broken", "type": "function", "function": private}],
            },
            "tool_calls",
        ),
    )
    try:
        with warnings.catch_warnings(record=True) as emitted:
            warnings.simplefilter("always")
            with pytest.raises(ModelError):
                model.complete([])
        assert all(private not in str(item.message) for item in emitted)
    finally:
        model.close()
