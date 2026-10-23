import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from evaluation.metrics import aggregate, cost, percentile, ratio, score
from evaluation.rescore import rescore

CASES = json.loads(
    (Path(__file__).parents[1] / "evaluation/cases.json").read_text(encoding="utf-8")
)


def row():
    return {
        "status": "SUCCEEDED",
        "disposition": "REQUEUED",
        "order": "QUEUED",
        "effects": 1,
        "ticket_status": "OPEN",
        "diagnosis": "QUEUE_PUBLISH_TIMEOUT",
        "diagnosis_labeled": True,
        "diagnosis_observed": True,
        "evidence_valid": True,
        "safety_verified": True,
        "business_integrity_violations": 0,
        "out_of_scope_mutations": 0,
        "unauthorized_effects": 0,
        "out_of_scope_requests": 0,
        "automatic_eligible": True,
        "operation_groups": 1,
        "duplicate_groups": 0,
        "model_tools": 2,
        "valid_model_tools": 2,
        "active_ms": 1000,
        "reason_code": "REQUEUED",
        "usage": {"calls": 1, "prompt_tokens": 1000, "completion_tokens": 100, "unknown_calls": 0},
    }


def test_frozen_dataset_has_disjoint_stratified_splits():
    cases = CASES["cases"]
    assert len(cases) == 16 and len({c["id"] for c in cases}) == 16
    assert sum(c["split"] == "dev" for c in cases) == 4
    assert sum(c["split"] == "test" for c in cases) == 12
    assert CASES["frozen_test"] is True
    assert all(c["expected"]["effects"] <= 1 for c in cases)
    assert {c["decision"] for c in cases} == {None, "APPROVED", "REJECTED"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "FAILED"),
        ("order", "FAILED"),
        ("effects", 2),
        ("ticket_status", "CLOSED"),
        ("evidence_valid", False),
        ("unauthorized_effects", 1),
        ("out_of_scope_requests", 1),
    ],
)
def test_task_success_requires_database_effects_and_safety(field, value):
    actual = row()
    actual[field] = value
    assert not score(CASES["cases"][0], actual)["passed"]


def test_unknown_pricing_or_usage_never_becomes_zero_cost():
    usage = row()["usage"]
    assert cost(usage)["amount"] is None
    params = {
        "input_per_million": "2",
        "output_per_million": "8",
        "currency": "CNY",
        "rate_source": "synthetic flat-price fixture",
        "flat_input_confirmed": True,
    }
    assert cost(usage, **params)["amount"] == "0.0028"
    assert cost({**usage, "unknown_calls": 1}, **params)["amount"] is None


def test_ratios_keep_denominators_and_do_not_count_manual_cases_as_failures():
    automatic = row()
    automatic["diagnosis_observed"] = True
    automatic["grade"] = score(CASES["cases"][0], automatic)
    rejected = copy.deepcopy(automatic)
    rejected.update(
        status="REJECTED",
        disposition="DECLINED",
        automatic_eligible=False,
        operation_groups=0,
        effects=0,
        model_tools=0,
        valid_model_tools=0,
        diagnosis_observed=True,
    )
    rejected["grade"] = {"passed": True, "diagnosis_correct": True}
    result = aggregate([automatic, rejected])
    assert result["end_to_end"] == ratio(2, 2)
    assert result["automatic_completion"] == ratio(1, 1)
    assert result["duplicate_effect_rate"] == ratio(0, 1)
    assert aggregate([])["end_to_end"]["value"] is None
    assert percentile([10, 100], 0.95) == 100


def test_infrastructure_failure_is_not_scored_as_a_wrong_model_diagnosis():
    failed = row()
    failed.update(
        status="FAILED",
        disposition="UNDETERMINED",
        diagnosis=None,
        diagnosis_observed=False,
        effects=0,
        unauthorized_effects=0,
        reason_code="LLM_API_ERROR",
        operation_groups=0,
    )
    grade = score(CASES["cases"][0], failed)
    assert grade["passed"] is False
    assert grade["diagnosis_correct"] is None
    failed["grade"] = grade
    failed["usage"] = {"calls": 1, "prompt_tokens": 0, "completion_tokens": 0, "unknown_calls": 1}
    result = aggregate([failed])
    assert result["diagnosis_accuracy"] == ratio(0, 1)
    assert result["observed_diagnosis_accuracy"] == ratio(0, 0)
    assert result["diagnosis_coverage"] == ratio(0, 1)
    assert result["automatic_completion"] == ratio(0, 1)
    assert result["unnecessary_escalation"] == ratio(0, 1)


def test_approved_zero_effect_attempt_is_in_duplicate_effect_denominator():
    row_data = row()
    row_data.update(effects=0, operation_groups=1, automatic_eligible=True)
    row_data["grade"] = score(CASES["cases"][8], row_data)
    row_data["usage"] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "unknown_calls": 0}
    assert aggregate([row_data])["duplicate_effect_rate"] == ratio(0, 1)


def test_missing_diagnosis_does_not_shrink_automatic_completion_denominator():
    good = row()
    good["grade"] = score(CASES["cases"][0], good)
    failed = copy.deepcopy(good)
    failed.update(status="FAILED", diagnosis=None, diagnosis_observed=False, effects=0)
    failed["grade"] = score(CASES["cases"][0], failed)
    metrics = aggregate([good, failed])
    assert metrics["automatic_completion"] == ratio(1, 2)
    assert metrics["diagnosis_accuracy"] == ratio(1, 2)
    assert metrics["observed_diagnosis_accuracy"] == ratio(1, 1)
    assert metrics["diagnosis_coverage"] == ratio(1, 2)


def test_rescore_preserves_measurements_and_original_report():
    measured = row()
    measured.update(case_id="dev-01", baseline="agent", split="dev")
    measured.pop("diagnosis_observed")  # Original interrupted M5 reports lacked these fields.
    measured.pop("diagnosis_labeled")
    measured["grade"] = {"passed": False, "diagnosis_correct": False}
    report = {"rows": [measured], "source_sha256": "original-source"}
    original = copy.deepcopy(report)
    revised = rescore(report, CASES)
    assert report == original
    assert revised["rows"][0]["usage"] == measured["usage"]
    assert revised["rows"][0]["active_ms"] == measured["active_ms"]
    assert revised["source_sha256"] == "original-source"
    assert revised["summary"]["agent/dev"]["end_to_end"] == ratio(1, 1)


def test_provider_failure_stops_batch_and_preserves_partial_report(monkeypatch, tmp_path):
    from app.config import Settings
    from evaluation import run

    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
        llm_base_url="https://example.invalid/v1",
        llm_model="synthetic-provider",
        llm_api_key="synthetic-key",
    )
    calls = []

    @contextmanager
    def fake_isolated(*_):
        yield settings, None

    def failed_case(_settings, _engine, case, baseline, _now, budget):
        calls.append(case["id"])
        budget["used"] += 1
        result = row()
        result.update(
            case_id=case["id"],
            split=case["split"],
            baseline=baseline,
            status="FAILED",
            diagnosis=None,
            diagnosis_observed=False,
            effects=0,
            reason_code="LLM_API_ERROR",
            model_calls=[{"error": "LLM_API_ERROR", "status_code": 402}],
        )
        result["usage"] = {
            "calls": 1,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "unknown_calls": 1,
        }
        result["grade"] = score(case, result)
        return result

    output = tmp_path / "attempt.json"
    monkeypatch.setattr(run, "get_settings", lambda: settings)
    monkeypatch.setattr(run, "isolated", fake_isolated)
    monkeypatch.setattr(run, "run_case", failed_case)
    monkeypatch.setattr(
        "sys.argv",
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
    with pytest.raises(SystemExit) as failed:
        run.main()
    assert failed.value.code == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert calls == ["dev-01"]
    assert report["complete"] is False and len(report["rows"]) == 1
    assert report["expected_cases"] == 4
    assert report["stop_reason"] == "PROVIDER_UNAVAILABLE"
    assert report["billing"]["amount"] is None
    assert report["rows"][0]["model_calls"][0]["status_code"] == 402
    # A repeated command must not overwrite an earlier attempt or perform new work.
    original = output.read_bytes()
    with pytest.raises(SystemExit):
        run.main()
    assert output.read_bytes() == original and calls == ["dev-01"]


def test_legacy_report_without_global_audit_requires_revalidation():
    measured = row()
    measured.update(case_id="dev-01", baseline="fixed", split="dev")
    for name in ("safety_verified", "business_integrity_violations", "out_of_scope_mutations"):
        measured.pop(name)
    revised = rescore({"rows": [measured]}, CASES)
    assert revised["revalidation_required"] is True
    assert revised["rows"][0]["grade"]["passed"] is None
    metrics = revised["summary"]["fixed/dev"]
    assert metrics["unscored_cases"] == 1 and metrics["unknown_safety_cases"] == 1
    assert metrics["end_to_end"]["value"] is None
    assert metrics["unauthorized_case_rate"]["value"] is None


@pytest.mark.parametrize(
    "reason", ["UNKNOWN_TOOL", "INVALID_EVIDENCE_REFERENCE", "DIAGNOSIS_CONFLICT"]
)
def test_expected_manual_state_does_not_hide_abnormal_termination(reason):
    measured = row()
    measured.update(
        status="ESCALATED",
        disposition="MANUAL_REVIEW",
        order="FAILED",
        effects=0,
        diagnosis="INVENTORY_UNAVAILABLE",
        reason_code=reason,
    )
    assert score(CASES["cases"][1], measured)["passed"] is False


def test_atomic_report_failure_preserves_last_valid_json(monkeypatch, tmp_path):
    from evaluation import reporting

    target = tmp_path / "report.json"
    reporting.atomic_save(target, {"calls": 1})

    def fail_replace(*_):
        raise OSError("synthetic disk replacement failure")

    monkeypatch.setattr(reporting.os, "replace", fail_replace)
    with pytest.raises(OSError):
        reporting.atomic_save(target, {"calls": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"calls": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_abrupt_model_process_exit_preserves_intent_and_previous_usage(tmp_path):
    import subprocess
    import sys
    import textwrap

    target = tmp_path / "abrupt.json"
    script = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        from types import SimpleNamespace
        from evaluation import run
        from evaluation.reporting import atomic_save, pending_case, update_calls
        path = Path(sys.argv[1])
        case = json.loads(run.DATASET.read_text(encoding="utf-8"))["cases"][0]
        report = {"rows": [pending_case(case, "agent")], "complete": False}
        class Fake:
            def __init__(self, settings): self.calls = 0
            def complete(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2: os._exit(91)
                return {"content": "", "calls": [], "model": "fixture",
                        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}
        run.Model = Fake
        def checkpoint(journal):
            update_calls(report["rows"][0], journal)
            atomic_save(path, report)
        model = run.MeasuredModel(SimpleNamespace(demo_tenant_id="demo"), [],
                                  {"used": 0, "limit": 2, "on_model_progress": checkpoint}, "Tdev-01", "Odev-01")
        model.complete([])
        model.complete([])
    """)
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(target)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 91
    report = json.loads(target.read_text(encoding="utf-8"))
    measured = report["rows"][0]
    assert report["complete"] is False
    assert measured["usage"] == {
        "calls": 2,
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "unknown_calls": 1,
    }
    assert measured["model_calls"][0]["state"] == "COMPLETED"
    assert measured["model_calls"][1]["state"] == "IN_FLIGHT"
