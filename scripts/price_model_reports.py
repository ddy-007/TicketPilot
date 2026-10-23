"""Price observed DeepSeek Flash usage using the verified 2026-10-23 off-peak tariff.

This narrow calculator refuses other dates, peak periods, incomplete usage and
unverified providers. Estimates are not invoices. It never calls a model.
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
DATE = "2026-10-23"
RATES = {"cache_hit": "0.02", "cache_miss": "1", "output": "4"}


def price_call(call):
    try:
        start, end = (datetime.fromisoformat(call[key]) for key in ("started_at", "finished_at"))
        if start.tzinfo is None or end.tzinfo is None:
            return None
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if not 0 <= (end - start).total_seconds() <= 180:
            return None
        for point in (start, end):
            if point.date().isoformat() != DATE:
                return None
            # Outside these windows is off-peak regardless of holiday calendar.
            if point.weekday() < 5 and (1 <= point.hour < 4 or 6 <= point.hour < 10):
                return None
        usage = call["usage"]
        hit, miss, output, prompt = (
            usage[key]
            for key in (
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
                "completion_tokens",
                "prompt_tokens",
            )
        )
        if any(type(value) is not int or value < 0 for value in (hit, miss, output, prompt)):
            return None
        if hit + miss != prompt:
            return None
        return (
            Decimal(hit) * Decimal(RATES["cache_hit"])
            + Decimal(miss) * Decimal(RATES["cache_miss"])
            + Decimal(output) * Decimal(RATES["output"])
        ) / 1_000_000
    except (KeyError, TypeError, ValueError):
        return None


def price_report(report):
    calls = [call for row in report["rows"] for call in row["model_calls"]]
    verified = (
        report.get("provider") == "deepseek_official"
        and report.get("configuration_model") == "deepseek-flash"
    )
    priced = [price_call(call) if verified else None for call in calls]
    known = sum((value for value in priced if value is not None), Decimal(0))
    unknown = sum(value is None for value in priced)
    complete = report.get("complete") is True and not unknown
    return {
        "source_sha256": report["source_sha256"],
        "complete": report.get("complete"),
        "calls": len(calls),
        "unknown_price_calls": unknown,
        "amount": str(known) if complete else None,
        "known_amount_lower_bound": str(known),
        "currency": "CNY",
        "status": "ESTIMATE_NOT_INVOICE" if complete else "UNKNOWN_PRICING_OR_USAGE",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = {
        path.name: price_report(json.loads(path.read_text(encoding="utf-8")))
        for path in args.inputs
    }
    result = {
        "rate_source": SOURCE,
        "rate_checked_date": DATE,
        "window": "off_peak",
        "cny_per_million_tokens": RATES,
        "calculator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reports": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
