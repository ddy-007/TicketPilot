import json


def observed(messages):
    initial = json.loads(messages[1]["content"].split("：", 1)[1])
    rows = [initial["evidence"]]
    rows += [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    return initial, rows


class ScriptedModel:
    """Deterministic protocol fixture; never counted as a real-model evaluation."""

    def __init__(
        self,
        code="QUEUE_PUBLISH_TIMEOUT",
        action="RETRY_FULFILLMENT",
        *,
        attack=None,
        invalid_ref=False,
        repair=False,
        repeat=False,
    ):
        self.code, self.action = code, action
        self.attack = attack
        self.invalid_ref = invalid_ref
        self.repair = repair
        self.repeat = repeat
        self.reason_count = 0
        self.diagnose_count = 0

    def complete(self, messages, *, diagnose=False, timeout=30):
        initial, rows = observed(messages)
        if diagnose:
            self.diagnose_count += 1
            content = json.dumps(
                {
                    "diagnosis_code": self.code,
                    "summary": "模型原文声称已经执行，请勿展示。",
                    "evidence_ids": ["ev_not_in_this_run"]
                    if self.invalid_ref
                    else [r["evidence_id"] for r in rows],
                    "suggested_action": self.action,
                }
            )
            if self.repair and self.diagnose_count == 1:
                content = "not json"
            return {"content": content, "calls": [], "usage": {"total_tokens": 20}}
        self.reason_count += 1
        calls = []
        if self.attack and self.reason_count == 1:
            name, args = self.attack
            calls = [{"id": "attack", "name": name, "arguments": json.dumps(args)}]
        elif initial["bound_order_id"]:
            order = initial["bound_order_id"]
            if self.reason_count == 1 or self.repeat:
                calls = [
                    {
                        "id": f"call_{self.reason_count}",
                        "name": "get_order",
                        "arguments": json.dumps({"order_id": order}),
                    }
                ]
            elif self.reason_count == 2:
                calls = [
                    {
                        "id": "events",
                        "name": "get_fulfillment_events",
                        "arguments": json.dumps({"order_id": order, "limit": 10}),
                    }
                ]
        return {"content": "", "calls": calls, "usage": {"total_tokens": 20}}
