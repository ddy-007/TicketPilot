import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "pricing", Path(__file__).parents[2] / "scripts/price_model_reports.py"
)
pricing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pricing)


def call():
    return {
        "started_at": "2026-10-23T13:00:00+00:00",
        "finished_at": "2026-10-23T13:00:02+00:00",
        "usage": {
            "prompt_cache_hit_tokens": 1000,
            "prompt_cache_miss_tokens": 2000,
            "prompt_tokens": 3000,
            "completion_tokens": 500,
        },
    }


def test_cache_classes_reconcile_and_are_priced_separately():
    assert pricing.price_call(call()) == Decimal("0.00402")


@pytest.mark.parametrize(
    "kind", ["unknown", "missing_cache", "bad_cache", "negative", "peak", "other_date", "naive"]
)
def test_missing_usage_and_unverified_rate_windows_are_unknown(kind):
    measured = call()
    if kind == "unknown":
        measured["usage"] = None
    elif kind == "missing_cache":
        measured["usage"].pop("prompt_cache_hit_tokens")
    elif kind == "bad_cache":
        measured["usage"]["prompt_tokens"] = 3001
    elif kind == "negative":
        measured["usage"]["completion_tokens"] = -1
    elif kind == "peak":
        measured.update(
            started_at="2026-10-23T00:59:59+00:00", finished_at="2026-10-23T01:00:01+00:00"
        )
    elif kind == "other_date":
        measured["started_at"] = "2026-10-24T13:00:00+00:00"
    elif kind == "naive":
        measured["started_at"] = "2026-10-23T13:00:00"
    assert pricing.price_call(measured) is None


def test_partial_batch_and_unverified_provider_never_get_complete_cost():
    report = {
        "source_sha256": "synthetic",
        "provider": "deepseek_official",
        "configuration_model": "deepseek-flash",
        "complete": False,
        "rows": [{"model_calls": [call()]}],
    }
    assert pricing.price_report(report)["amount"] is None
    report["complete"] = True
    assert pricing.price_report(report)["amount"] == "0.00402"
    report["provider"] = None
    assert pricing.price_report(report)["amount"] is None
