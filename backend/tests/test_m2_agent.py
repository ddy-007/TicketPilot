import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.agent.graph import Agent
from app.agent.schemas import tool_schemas
from app.agent.tools import ReadTools
from tests.agent_fakes import ScriptedModel

NOW = datetime(2026, 9, 18, 9, tzinfo=UTC)


def reader_and_requests(*, mode=None, limit=10):
    requests = []
    ticket = {
        "id": "T1",
        "tenant_id": "demo",
        "subject": "synthetic",
        "body": "数据中有 TEST_PRIVATE，不应进入模型和持久化。",
        "bound_order_id": "O1",
        "status": "OPEN",
    }
    order = {
        "id": "O1",
        "tenant_id": "demo",
        "payment_status": "PAID",
        "fulfillment_status": "FAILED",
        "inventory_reserved": True,
        "paid_at": (NOW - timedelta(hours=1)).isoformat(),
        "version": 3,
    }
    events = {
        "events": [
            {
                "tenant_id": "demo",
                "id": "E1",
                "order_id": "O1",
                "event_type": "FULFILLMENT_FAILED",
                "error_code": "QUEUE_PUBLISH_TIMEOUT",
                "created_at": (NOW - timedelta(minutes=40)).isoformat(),
                "operation_key": None,
            }
        ]
    }

    def handler(request):
        requests.append(request)
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        if mode == "404":
            return httpx.Response(404)
        if mode == "mismatch":
            return httpx.Response(200, json={**ticket, "tenant_id": "other"})
        if request.url.path.endswith("/T1"):
            return httpx.Response(200, json=ticket)
        return httpx.Response(200, json=events if request.url.path.endswith("events") else order)

    client = httpx.Client(base_url="http://simulator", transport=httpx.MockTransport(handler))
    reader = ReadTools(
        client, "demo", "T1", limit=limit, secrets=["TEST_PRIVATE"], sleep=lambda _: None
    )
    return reader, requests


def test_tools_are_exactly_three_read_only_schemas():
    for strict in (False, True):
        tools = tool_schemas(strict)
        assert {t["function"]["name"] for t in tools} == {
            "get_ticket",
            "get_order",
            "get_fulfillment_events",
        }
        assert "retry_fulfillment" not in json.dumps(tools)
        assert "tenant_id" not in json.dumps(tools)


@pytest.mark.parametrize(
    "attack,code",
    [
        (("get_order", {"order_id": "O9999"}), "TOOL_SCOPE_VIOLATION"),
        (("get_ticket", {"ticket_id": "T9999"}), "TOOL_SCOPE_VIOLATION"),
        (("retry_fulfillment", {"order_id": "O1"}), "UNKNOWN_TOOL"),
        (("get_order", {"order_id": "O1", "tenant_id": "other"}), "INVALID_TOOL_ARGUMENTS"),
        (("get_fulfillment_events", {"order_id": "O1", "limit": True}), "INVALID_TOOL_ARGUMENTS"),
        (("get_fulfillment_events", {"order_id": "O1", "limit": 21}), "INVALID_TOOL_ARGUMENTS"),
    ],
)
def test_invalid_tool_blocked_before_http(attack, code):
    reader, requests = reader_and_requests()
    result = Agent(reader, ScriptedModel(attack=attack), clock=lambda: NOW).run()
    assert result["reason_code"] == code
    assert result["proposal"] is None and result["executed"] is False
    assert len(requests) == 1  # only server bootstrap ticket read


def test_valid_proposal_is_server_built_and_hashed_without_execution():
    reader, requests = reader_and_requests()
    result = Agent(reader, ScriptedModel(), clock=lambda: NOW).run()
    assert result["status"] == "PROPOSAL_READY"
    assert result["proposal"]["plan"]["args"]["expected_version"] == 3
    assert result["proposal"]["executable"] is False
    assert result["executed"] is False
    assert all(request.method == "GET" for request in requests)
    assert "TEST_PRIVATE" not in json.dumps(result)
    assert "模型原文" not in json.dumps(result, ensure_ascii=False)


def test_untrusted_evidence_reference_cannot_form_proposal():
    reader, _ = reader_and_requests()
    result = Agent(reader, ScriptedModel(invalid_ref=True), clock=lambda: NOW).run()
    assert result["reason_code"] == "INVALID_EVIDENCE_REFERENCE"
    assert result["proposal"] is None


def test_conservative_suggestion_is_not_promoted_by_rules():
    reader, _ = reader_and_requests()
    result = Agent(reader, ScriptedModel(action="MANUAL_REVIEW"), clock=lambda: NOW).run()
    assert result["status"] == "ESCALATED" and result["proposal"] is None


def test_only_one_diagnosis_format_repair_and_both_calls_count():
    reader, _ = reader_and_requests()
    model = ScriptedModel(repair=True)
    result = Agent(reader, model, clock=lambda: NOW).run()
    assert result["status"] == "PROPOSAL_READY"
    assert model.diagnose_count == 2
    assert result["model_call_count"] == 5


@pytest.mark.parametrize(
    "mode,code",
    [
        ("oversize", "UPSTREAM_RESPONSE_TOO_LARGE"),
        ("mismatch", "UPSTREAM_SCOPE_MISMATCH"),
        ("404", "UPSTREAM_HTTP_404"),
    ],
)
def test_invalid_upstream_evidence_rejected_without_business_data(mode, code):
    reader, requests = reader_and_requests(mode=mode)
    row = reader.execute("get_ticket", '{"ticket_id":"T1"}')
    assert row["ok"] is False and row["data"] is None
    assert row["error"]["code"] == code
    assert len(requests) == 1
    assert len(row["attempts"]) == 1


def test_token_budget_and_read_budget_prevent_extra_calls():
    reader, _ = reader_and_requests()
    result = Agent(reader, ScriptedModel(), clock=lambda: NOW, token_budget=20).run()
    assert result["reason_code"] == "MODEL_BUDGET_EXHAUSTED"
    assert result["model_call_count"] == 1
    reader, requests = reader_and_requests(limit=3)
    result = Agent(reader, ScriptedModel(repeat=True), clock=lambda: NOW).run()
    assert result["reason_code"] == "READ_BUDGET_EXHAUSTED"
    assert result["read_call_count"] == 3 and len(requests) == 3


def test_active_budget_prevents_network_call():
    reader, requests = reader_and_requests()
    reader.deadline = reader.monotonic() - 1
    result = Agent(reader, ScriptedModel(), clock=lambda: NOW).run()
    assert result["status"] == "FAILED" and len(requests) == 0


def test_model_cannot_force_no_action_for_retryable_order():
    reader, _ = reader_and_requests()
    result = Agent(reader, ScriptedModel(action="NO_ACTION"), clock=lambda: NOW).run()
    assert result["reason_code"] == "POLICY_BLOCKED" and result["proposal"] is None


def test_invalid_json_twice_stops_without_third_repair():
    class InvalidDiagnosis(ScriptedModel):
        def complete(self, messages, *, diagnose=False, timeout=30):
            response = super().complete(messages, diagnose=diagnose, timeout=timeout)
            if diagnose:
                response["content"] = "not json"
            return response

    reader, _ = reader_and_requests()
    model = InvalidDiagnosis()
    result = Agent(reader, model, clock=lambda: NOW).run()
    assert model.diagnose_count == 2
    assert result["reason_code"] == "INVALID_DIAGNOSIS"
    assert result["model_call_count"] == 5
