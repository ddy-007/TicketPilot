"""M5 review regressions. Real isolated PostgreSQL; every model is a local fixture."""

import copy
import json
import os
import sys
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app.agent.tools import ReadTools, ToolError
from app.config import Settings
from evaluation import run
from simulator.models import fulfillment_events, orders
from tests.agent_fakes import ScriptedModel

DATA = json.loads(run.DATASET.read_text(encoding="utf-8"))
NOW = datetime.fromisoformat(DATA["fixture_now"])


def isolation():
    admin = os.getenv("M0_TEST_ADMIN_URL")
    if not admin:
        pytest.skip("Set M0_TEST_ADMIN_URL for isolated M5 review regressions.")
    return run.isolated(admin, DATA["fixture_now"], settings())


def settings():
    return Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
        llm_base_url="https://example.invalid/v1",
        llm_model="m5-audit-fake",
        llm_api_key="AUDIT_FAKE_KEY",
        demo_tenant_id="demo",
    )


def case(case_id):
    return copy.deepcopy(next(c for c in DATA["cases"] if c["id"] == case_id))


def install_model(monkeypatch, **kwargs):
    class Fixture(ScriptedModel):
        def __init__(self, _settings):
            super().__init__(**kwargs)

        def complete(self, *args, **kw):
            result = super().complete(*args, **kw)
            result["usage"] = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
            result["model"] = "m5-audit-fake"
            return result

        def close(self):
            pass

    monkeypatch.setattr(run, "Model", Fixture)


def test_invalid_evidence_must_not_pass_manual_case(monkeypatch):
    install_model(
        monkeypatch, code="INVENTORY_UNAVAILABLE", action="MANUAL_REVIEW", invalid_ref=True
    )
    with isolation() as (configured, engine):
        measured = run.run_case(
            configured, engine, case("dev-02"), "agent", NOW, {"used": 0, "limit": 24}
        )
    print(
        json.dumps(
            {k: measured[k] for k in ("reason_code", "diagnosis", "evidence_valid", "grade")},
            ensure_ascii=True,
        )
    )
    assert measured["grade"]["passed"] is False, "Invalid evidence was reported as task success."


def test_forbidden_tool_must_not_pass_manual_case(monkeypatch):
    install_model(monkeypatch, attack=("retry_fulfillment", {"order_id": "Odev-02"}))
    with isolation() as (configured, engine):
        measured = run.run_case(
            configured, engine, case("dev-02"), "agent", NOW, {"used": 0, "limit": 24}
        )
    print(
        json.dumps(
            {
                k: measured[k]
                for k in ("reason_code", "diagnosis", "model_tools", "valid_model_tools", "grade")
            },
            ensure_ascii=True,
        )
    )
    assert measured["grade"]["passed"] is False, (
        "A forbidden tool produced a passing manual-case grade."
    )


def test_unbound_order_request_is_invalid_in_both_meter_and_runtime(monkeypatch):
    class GuessedOrder:
        def __init__(self, _settings):
            pass

        def complete(self, *_args, **_kwargs):
            return {
                "content": "",
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                "model": "m5-audit-fake",
                "calls": [
                    {"name": "get_order", "arguments": '{"order_id":"Otest-05"}', "id": "guess"}
                ],
            }

        def close(self):
            pass

    monkeypatch.setattr(run, "Model", GuessedOrder)
    journal = []
    model = run.MeasuredModel(settings(), journal, {"used": 0, "limit": 1}, "Ttest-05", "Otest-05")
    response = model.complete([])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("No HTTP permitted"))
    ) as client:
        reader = ReadTools(client, "demo", "Ttest-05")
        with pytest.raises(ToolError, match="TOOL_SCOPE_VIOLATION"):
            reader.validate("get_order", response["calls"][0]["arguments"])
    print(
        json.dumps(
            {"meter_valid": journal[0]["valid_model_tools"], "runtime": "TOOL_SCOPE_VIOLATION"}
        )
    )
    model.close()
    assert journal[0]["valid_model_tools"] == 0, (
        "Meter accepted an order with no trusted ticket binding."
    )


def test_cross_order_side_effect_is_detected(monkeypatch):
    real_worker = run.Worker
    with isolation() as (configured, engine):
        witness = case("dev-01")
        witness["id"] = "audit-witness"
        _, other_order = run.seed_case(engine, witness, NOW)

        class MutatingWorker(real_worker):
            changed = False

            def once(self):
                worked = super().once()
                if worked and not self.changed:
                    self.changed = True
                    # Inject a software regression's unauthorized effect on another bound order.
                    with engine.begin() as conn:
                        conn.execute(
                            orders.update()
                            .where(orders.c.id == other_order)
                            .values(fulfillment_status="QUEUED", version=4)
                        )
                        conn.execute(
                            fulfillment_events.insert().values(
                                tenant_id="demo",
                                id="audit-unrelated-effect",
                                order_id=other_order,
                                event_type="RETRY_ENQUEUED",
                                operation_key="audit-unapproved-key",
                                created_at=NOW,
                            )
                        )
                return worked

        monkeypatch.setattr(run, "Worker", MutatingWorker)
        measured = run.run_case(
            configured, engine, case("dev-02"), "fixed", NOW, {"used": 0, "limit": 1}
        )
        with engine.connect() as conn:
            actual = conn.execute(
                select(orders.c.fulfillment_status).where(orders.c.id == other_order)
            ).scalar_one()
        assert actual == "QUEUED"
    print(
        json.dumps(
            {
                "unrelated_order": actual,
                **{
                    k: measured[k]
                    for k in ("unauthorized_effects", "out_of_scope_requests", "grade")
                },
            }
        )
    )
    assert (
        measured["unauthorized_effects"] > 0
        or measured["out_of_scope_requests"] > 0
        or not measured["grade"]["passed"]
    ), "Out-of-scope mutation was invisible to every safety check."


def test_interrupted_case_keeps_model_request_accounting(monkeypatch, tmp_path):
    sent = []

    class Interrupted(ScriptedModel):
        def __init__(self, _settings):
            super().__init__()

        def complete(self, *args, **kw):
            sent.append(True)
            persisted = json.loads(output.read_text(encoding="utf-8"))
            assert len(persisted["rows"][0]["model_calls"]) == len(sent)
            assert persisted["rows"][0]["model_calls"][-1]["state"] == "IN_FLIGHT"
            if len(sent) == 2:
                raise KeyboardInterrupt()
            result = super().complete(*args, **kw)
            result.update(
                model="m5-audit-fake",
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            )
            return result

        def close(self):
            pass

    monkeypatch.setattr(run, "Model", Interrupted)
    monkeypatch.setattr(run, "get_settings", settings)
    admin = os.getenv("M0_TEST_ADMIN_URL")
    if not admin:
        pytest.skip("Set M0_TEST_ADMIN_URL for isolated interruption regression.")
    output = tmp_path / "interrupted.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluation",
            "--baseline",
            "agent",
            "--real-model",
            "--split",
            "dev",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(KeyboardInterrupt):
        run.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "attempted_fake_requests": len(sent),
                "saved_rows": len(report["rows"]),
                "complete": report["complete"],
            }
        )
    )
    assert len(sent) == 2
    assert sum(r["usage"]["calls"] for r in report["rows"]) >= 2, (
        "Both request intents and successful first usage were lost."
    )

    saved = report["rows"][0]
    assert saved["usage"] == {
        "calls": 2,
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "unknown_calls": 1,
    }
    assert saved["measurement_status"] == "INTERRUPTED"
    assert report["completed_cases"] == 0
    assert report["stop_reason"] == "USER_INTERRUPTED"
    assert report["summary"]["agent/dev"]["unauthorized_case_rate"]["value"] is None
    assert report["billing"]["amount"] is None
