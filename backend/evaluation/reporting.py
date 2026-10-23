"""Persist request intents before paid work and retain conservative partial measurements."""

import json
import os
from uuid import uuid4

from evaluation.metrics import cost, score


def atomic_save(path, report):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def model_usage(journal):
    return {
        "calls": len(journal),
        "prompt_tokens": sum((call["usage"] or {}).get("prompt_tokens", 0) for call in journal),
        "completion_tokens": sum(
            (call["usage"] or {}).get("completion_tokens", 0) for call in journal
        ),
        "unknown_calls": sum(call["usage"] is None for call in journal),
    }


def pending_case(case, baseline):
    row = {
        "case_id": case["id"],
        "split": case["split"],
        "baseline": baseline,
        "scenario": case["scenario"],
        "measurement_status": "RUNNING",
        "status": "INTERRUPTED",
        "disposition": "UNDETERMINED",
        "reason_code": "CASE_INCOMPLETE",
        "diagnosis": None,
        "accepted_diagnosis": None,
        "diagnosis_observed": False,
        "diagnosis_labeled": case["expected"]["diagnosis"] is not None,
        "order": None,
        "ticket_status": None,
        "effects": None,
        "safety_verified": None,
        "business_integrity_violations": None,
        "unauthorized_effects": None,
        "out_of_scope_requests": None,
        "out_of_scope_mutations": None,
        "evidence_valid": None,
        "operation_groups": int(case["decision"] == "APPROVED"),
        "duplicate_groups": None,
        "automatic_eligible": case["decision"] == "APPROVED" and case["expected"]["effects"] == 1,
        "model_tools": 0,
        "valid_model_tools": 0,
        "active_ms": None,
        "model_calls": [],
        "usage": model_usage([]),
        # Zero completed calls does not prove an interrupted case used no model.
        "cost": {"amount": None, "status": "INCOMPLETE_MEASUREMENT"},
    }
    row["grade"] = score(case, row)
    return row


def update_calls(row, journal):
    row["model_calls"] = journal
    row["usage"] = model_usage(journal)
    row["model_tools"] = sum(call["model_tools"] for call in journal)
    row["valid_model_tools"] = sum(call["valid_model_tools"] for call in journal)
    row["cost"] = cost(row["usage"])
