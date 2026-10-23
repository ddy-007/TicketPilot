from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TicketArgs(StrictModel):
    ticket_id: Identifier


class OrderArgs(StrictModel):
    order_id: Identifier


class EventsArgs(OrderArgs):
    limit: int = Field(default=10, ge=1, le=20)


class TicketData(StrictModel):
    id: Identifier
    tenant_id: Identifier
    subject: str = Field(max_length=200)
    body: str = Field(max_length=2000)
    bound_order_id: Identifier | None
    status: Literal["OPEN", "CLOSED"]


class OrderData(StrictModel):
    id: Identifier
    tenant_id: Identifier
    payment_status: Literal["PENDING", "PAID", "REFUNDED"]
    fulfillment_status: Literal["NOT_STARTED", "FAILED", "QUEUED", "SHIPPED", "CANCELLED"]
    inventory_reserved: bool
    paid_at: AwareDatetime | None
    version: int = Field(ge=1)


class EventData(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    tenant_id: Identifier
    order_id: Identifier
    event_type: str = Field(min_length=1, max_length=40)
    error_code: str | None = Field(max_length=80)
    created_at: AwareDatetime
    operation_key: str | None = Field(max_length=240)


class EventsData(StrictModel):
    events: list[EventData] = Field(max_length=20)


class Diagnosis(StrictModel):
    diagnosis_code: Literal[
        "QUEUE_PUBLISH_TIMEOUT",
        "INVENTORY_UNAVAILABLE",
        "ALREADY_QUEUED",
        "ALREADY_SHIPPED",
        "PAYMENT_NOT_READY",
        "INSUFFICIENT_EVIDENCE",
        "OTHER",
    ]
    summary: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    suggested_action: Literal["RETRY_FULFILLMENT", "NO_ACTION", "MANUAL_REVIEW"]


class PlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str
    expected_version: int
    expected_error_code: Literal["QUEUE_PUBLISH_TIMEOUT"] = "QUEUE_PUBLISH_TIMEOUT"


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    tenant_id: str
    plan_version: Literal[1] = 1
    tool: Literal["retry_fulfillment"] = "retry_fulfillment"
    args: PlanArgs
    evidence_ids: tuple[str, ...]
    reason: str
    expected_effect: str = "重新入队，不代表已经发货"
    expires_at: str


TOOLS = {
    "get_ticket": (TicketArgs, TicketData, "Read the current bound ticket."),
    "get_order": (OrderArgs, OrderData, "Read only the current ticket's bound order."),
    "get_fulfillment_events": (EventsArgs, EventsData, "Read recent bound-order events."),
}


def tool_schemas(strict: bool) -> list[dict]:
    tools = []
    for name, (args, _, description) in TOOLS.items():
        schema = args.model_json_schema()
        if strict:
            # Strict function schemas require every declared field, including limit.
            schema["required"] = list(schema["properties"])
            for value in schema["properties"].values():
                value.pop("default", None)
        function = {"name": name, "description": description, "parameters": schema}
        if strict:
            function["strict"] = True
        tools.append({"type": "function", "function": function})
    return tools
