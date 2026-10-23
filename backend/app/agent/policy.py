import hashlib
import json
from datetime import timedelta

from app.agent.schemas import PlanArgs, Proposal

SUMMARIES = {
    "QUEUE_PUBLISH_TIMEOUT": "订单已支付且库存已预留，队列发布超时；可提出履约重试，尚未执行。",
    "INVENTORY_UNAVAILABLE": "库存未预留，需要人工处理；未执行任何操作。",
    "ALREADY_QUEUED": "订单当前已在履约队列，未确认发货，无需重复提交。",
    "ALREADY_SHIPPED": "查询确认订单当前已发货，无需履约重试。",
    "PAYMENT_NOT_READY": "支付状态不满足重试条件，需要人工处理。",
    "INSUFFICIENT_EVIDENCE": "可信绑定或业务证据不足，需要人工核对；未执行任何操作。",
    "OTHER": "当前业务条件不在自动重试范围，需要人工处理。",
}


def latest_evidence(reader, name):
    rows = [row for row in reader.evidence if row["tool_name"] == name]
    return rows[-1] if rows and rows[-1]["ok"] else None


def facts(reader, now):
    ticket = latest_evidence(reader, "get_ticket")
    order = latest_evidence(reader, "get_order")
    events = latest_evidence(reader, "get_fulfillment_events")
    if not ticket or not reader.bound_order_id or not order:
        return "INSUFFICIENT_EVIDENCE", []
    data = order["data"]
    critical = [order["evidence_id"]]
    if data["payment_status"] != "PAID":
        return "PAYMENT_NOT_READY", critical
    if data["fulfillment_status"] == "QUEUED":
        return "ALREADY_QUEUED", critical
    if data["fulfillment_status"] == "SHIPPED":
        return "ALREADY_SHIPPED", critical
    if not data["inventory_reserved"]:
        return "INVENTORY_UNAVAILABLE", critical
    if data["fulfillment_status"] != "FAILED":
        return "OTHER", critical
    if not events:
        return "INSUFFICIENT_EVIDENCE", []
    critical.append(events["evidence_id"])
    # limit-limited history: only claim a failure visible in the returned evidence.
    failures = [
        event for event in events["data"]["events"] if event["event_type"] == "FULFILLMENT_FAILED"
    ]
    if not failures:
        return "INSUFFICIENT_EVIDENCE", []
    from datetime import datetime

    failures.sort(
        key=lambda event: (datetime.fromisoformat(event["created_at"]), event["id"]), reverse=True
    )
    if failures[0]["error_code"] != "QUEUE_PUBLISH_TIMEOUT":
        return "OTHER", critical
    paid = datetime.fromisoformat(data["paid_at"]) if data["paid_at"] else None
    if not paid or now - paid < timedelta(minutes=30):
        return "OTHER", critical
    return "QUEUE_PUBLISH_TIMEOUT", critical


def proposal(reader, run_id, diagnosis, critical, now):
    order = latest_evidence(reader, "get_order")["data"]
    plan = Proposal(
        run_id=run_id,
        tenant_id=reader.tenant_id,
        args=PlanArgs(order_id=reader.bound_order_id, expected_version=order["version"]),
        evidence_ids=tuple(critical),
        reason=SUMMARIES[diagnosis.diagnosis_code],
        expires_at=(now + timedelta(minutes=30)).isoformat(),
    )
    payload = plan.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "plan": payload,
        "plan_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "executable": False,
        "approval_required": True,
    }
