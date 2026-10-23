"""Recompute an immutable M5 report without HTTP, database access, or model calls."""

import argparse
import hashlib
import json
from pathlib import Path

from evaluation.metrics import METRICS_VERSION, aggregate, cost, score

DATASET = Path(__file__).with_name("cases.json")


def rescore(report, dataset):
    cases = {case["id"]: case for case in dataset["cases"]}
    # JSON round-trip leaves the original measurement object unchanged.
    result = json.loads(json.dumps(report))
    for row in result["rows"]:
        case = cases[row["case_id"]]
        row["diagnosis_labeled"] = case["expected"]["diagnosis"] is not None
        row["diagnosis_observed"] = row["diagnosis"] is not None
        row["automatic_eligible"] = (
            case["decision"] == "APPROVED" and case["expected"]["effects"] == 1
        )
        row["operation_groups"] = int(case["decision"] == "APPROVED")
        row["grade"] = score(case, row)
        row["cost"] = (
            {"amount": None, "status": "INCOMPLETE_MEASUREMENT"}
            if row.get("measurement_status", "COMPLETE") != "COMPLETE"
            else cost(row["usage"])
            if row["usage"]["calls"]
            else {"amount": "0", "status": "NO_MODEL_CALLS"}
        )
    result["metrics_version"] = METRICS_VERSION
    result["revalidation_required"] = any(
        row.get("safety_verified") is not True for row in result["rows"]
    )
    result["summary"] = {
        f"{base}/{split}": aggregate(
            [row for row in result["rows"] if row["baseline"] == base and row["split"] == split]
        )
        for base in ("fixed", "agent")
        for split in ("dev", "test")
        if any(row["baseline"] == base and row["split"] == split for row in result["rows"])
    }
    if (
        all(
            not row["usage"]["calls"] and row.get("measurement_status", "COMPLETE") == "COMPLETE"
            for row in result["rows"]
        )
        and result["rows"]
    ):
        result["billing"] = {"amount": "0", "status": "NO_MODEL_CALLS"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.input.read_text(encoding="utf-8"))
    dataset_bytes = DATASET.read_bytes()
    if report["dataset_sha256"] != hashlib.sha256(dataset_bytes).hexdigest():
        parser.error("dataset hash differs from the measurement; refusing to change its labels")
    result = rescore(report, json.loads(dataset_bytes))
    result["rescored_from_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for name in ("metrics.py", "rescore.py"):
        digest.update(Path(__file__).with_name(name).read_bytes())
    result["scoring_sha256"] = digest.hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    except FileExistsError:
        parser.error("output exists; choose a new path")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
