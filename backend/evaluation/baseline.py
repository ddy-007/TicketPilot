import json

from app.agent.graph import Agent
from app.agent.policy import SUMMARIES, facts
from app.agent.schemas import Diagnosis


class FixedFlow(Agent):
    """Query every applicable read tool once, derive a diagnosis from rules, then reuse M3."""

    def load_context(self, state):
        context = super().load_context(state)
        if context.get("result"):
            return context
        if self.reader.bound_order_id:
            self.reader.execute("get_order", json.dumps({"order_id": self.reader.bound_order_id}))
            self.reader.execute(
                "get_fulfillment_events",
                json.dumps({"order_id": self.reader.bound_order_id, "limit": 10}),
            )
        code, critical = facts(self.reader, self.clock())
        action = (
            "RETRY_FULFILLMENT"
            if code == "QUEUE_PUBLISH_TIMEOUT"
            else "NO_ACTION"
            if code in {"ALREADY_QUEUED", "ALREADY_SHIPPED"}
            else "MANUAL_REVIEW"
        )
        diagnosis = Diagnosis(
            diagnosis_code=code,
            summary=SUMMARIES[code],
            suggested_action=action,
            evidence_ids=critical or [self.reader.evidence[0]["evidence_id"]],
        )
        return self.validate({"diagnosis": diagnosis.model_dump()})
