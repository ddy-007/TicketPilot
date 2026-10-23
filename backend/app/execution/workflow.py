import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agent.graph import Agent, State
from app.agent.llm import Model, ModelError
from app.agent.policy import facts
from app.agent.tools import ReadTools
from app.execution.business import RemoteError
from app.execution.db import digest


class FlowState(State, total=False):
    approval_id: str
    final_result: dict


class OperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    accepted: bool
    order_id: str
    fulfillment_status: str
    version: int
    operation_key: str


class Workflow:
    def __init__(
        self,
        db,
        run_id,
        settings,
        remote,
        *,
        model=None,
        clock=None,
        guard=None,
        agent_factory=Agent,
    ):
        self.db, self.run_id, self.settings, self.remote = db, run_id, settings, remote
        self.run = db.run(run_id)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.guard = guard or (lambda: None)
        self.model = model
        self.agent_factory = agent_factory
        self.save = db.evidence_writer(run_id)
        self.remote.audit = lambda record: self.save(self.reader().clean(record))

    def reader(self):
        reader = ReadTools(
            self.remote.read,
            self.run["tenant_id"],
            self.run["ticket_id"],
            limit=self.settings.max_read_calls,
            save=self.save,
            guard=self.guard,
            secrets=[
                self.settings.llm_api_key.get_secret_value(),
                self.settings.simulator_read_token.get_secret_value(),
                self.settings.simulator_write_token.get_secret_value(),
                self.settings.app_database_url,
                self.settings.checkpoint_database_url,
            ],
        )
        return reader

    def terminal(self, status, disposition, code, *, operation=None, summary=None):
        summaries = {
            "REQUEUED": "已确认履约任务重新入队，尚未确认发货；工单保持打开。",
            "DECLINED": "审批已拒绝，未执行履约重试。",
            "APPROVAL_EXPIRED": "审批已过期，不启动新的履约重试，需要重新诊断。",
            "STALE_PLAN": "订单状态、版本或工单绑定已变化，旧提案未获执行。",
            "UNKNOWN_OPERATION": "历史请求可能已经执行，目前无法确认；需要人工核对。",
            "VERIFICATION_FAILED": "重试已确认提交，但当前状态未通过验证，需要人工核对。",
        }
        uncertain = operation and operation["status"] in {"PREPARED", "UNKNOWN"}
        return {
            "final_result": {
                "run_id": str(self.run_id),
                "status": status,
                "disposition": disposition,
                "reason_code": code,
                "summary": summary or summaries.get(code, "本轮未能可靠完成，需要人工核对。"),
                "operation": operation,
                "executed": None
                if uncertain
                else bool(operation and operation["status"] == "CONFIRMED"),
                "note_sync_status": "SKIPPED",
                "warnings": ["历史请求可能已执行，不能认定未执行。"] if uncertain else [],
            }
        }

    def after_diagnosis(self, state):
        result = state["result"]
        with self.db.engine.begin() as conn:
            self.db.event(
                conn,
                self.run_id,
                "DIAGNOSIS_READY",
                {
                    "diagnosis": result["diagnosis"],
                    "reason_code": result["reason_code"],
                },
                "diagnosis-ready",
            )
        if result["status"] == "PROPOSAL_READY":
            plan = result["proposal"]["plan"]
            if plan["args"]["order_id"] != self.run["bound_order_id"]:
                return self.terminal("ESCALATED", "MANUAL_REVIEW", "STALE_PLAN")
            return {}
        status = result["status"]
        disposition = (
            "NO_ACTION_REQUIRED"
            if status == "SUCCEEDED"
            else "MANUAL_REVIEW"
            if status == "ESCALATED"
            else "UNDETERMINED"
        )
        final = self.terminal(status, disposition, result["reason_code"], summary=result["summary"])
        final["final_result"]["diagnosis"] = result["diagnosis"]
        return final

    def model_audit(self, kind, trace):
        with self.db.engine.begin() as conn:
            conn.execute(
                select(self.db.runs.c.id).where(self.db.runs.c.id == self.run_id).with_for_update()
            ).one()
            if kind == "LLM_CALL_STARTED":
                starts = conn.execute(
                    select(self.db.run_events.c.id).where(
                        self.db.run_events.c.run_id == self.run_id,
                        self.db.run_events.c.event_type == "LLM_CALL_STARTED",
                    )
                ).all()
                completed = (
                    conn.execute(
                        select(self.db.run_events.c.payload_json).where(
                            self.db.run_events.c.run_id == self.run_id,
                            self.db.run_events.c.event_type == "LLM_CALL_COMPLETED",
                        )
                    )
                    .scalars()
                    .all()
                )
                tokens = sum((row.get("usage") or {}).get("total_tokens", 0) for row in completed)
                if len(starts) >= self.settings.max_model_calls or tokens >= 12000:
                    raise ModelError("MODEL_BUDGET_EXHAUSTED")
            self.db.event(
                conn, self.run_id, kind, self.reader().clean(trace), trace["call_id"] + ":" + kind
            )

    def prepare(self, state):
        plan = state["result"]["proposal"]["plan"]
        with self.db.engine.begin() as conn:
            run = (
                conn.execute(
                    select(self.db.runs).where(self.db.runs.c.id == self.run_id).with_for_update()
                )
                .mappings()
                .one()
            )
            existing = (
                conn.execute(
                    select(self.db.approvals).where(self.db.approvals.c.run_id == self.run_id)
                )
                .mappings()
                .first()
            )
            if existing:
                return {"approval_id": str(existing["id"])}
            approval_id = uuid4()
            conn.execute(
                self.db.approvals.insert().values(
                    id=approval_id,
                    run_id=self.run_id,
                    plan_version=1,
                    plan_json=plan,
                    plan_hash=digest(plan),
                    decision="PENDING",
                    expires_at=datetime.fromisoformat(plan["expires_at"]),
                )
            )
            conn.execute(
                self.db.runs.update()
                .where(self.db.runs.c.id == run["id"])
                .values(
                    approval_id=approval_id,
                    updated_at=self.clock(),
                )
            )
            self.db.event(
                conn,
                self.run_id,
                "DIAGNOSIS_READY",
                {
                    "diagnosis": state["result"]["diagnosis"],
                    "model_call_count": state["result"]["model_call_count"],
                    "model_calls": state["result"]["model_calls"],
                },
                "diagnosis-ready",
            )
            self.db.event(
                conn,
                self.run_id,
                "APPROVAL_REQUIRED",
                {
                    "approval_id": str(approval_id),
                    "plan_hash": digest(plan),
                },
                "approval-required",
            )
        return {"approval_id": str(approval_id)}

    def wait(self, state):
        interrupt({"approval_id": state["approval_id"]})
        # Resume payload is deliberately ignored. Execute always reads the formal decision.
        return {}

    def operation(self, key):
        with self.db.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.db.operations).where(self.db.operations.c.idempotency_key == key)
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def op_view(self, operation):
        if operation is None:
            return None
        return {
            "tool": "retry_fulfillment",
            "status": operation["status"],
            "idempotency_key": operation["idempotency_key"],
            "result": operation["result_json"],
        }

    def set_operation(self, key, status, result=None):
        values = {"status": status}
        if result:
            values.update(result_json=result, confirmed_at=self.clock())
        with self.db.engine.begin() as conn:
            conn.execute(
                self.db.operations.update()
                .where(self.db.operations.c.idempotency_key == key)
                .values(**values)
            )
            self.db.event(
                conn,
                self.run_id,
                "OPERATION_" + status,
                {"operation_key": key},
                "operation-" + status,
            )
        return self.operation(key)

    def validate_remote(self, result, plan, key):
        parsed = OperationResult.model_validate(result)
        if not (
            parsed.accepted
            and parsed.order_id == plan["args"]["order_id"]
            and parsed.operation_key == key
            and parsed.fulfillment_status == "QUEUED"
            and parsed.version == plan["args"]["expected_version"] + 1
        ):
            raise RemoteError("BUSINESS_INVALID_OPERATION")
        return parsed.model_dump()

    def lookup(self, key, plan):
        try:
            result = self.remote.request(
                "GET", f"/internal/operations/{key}", self.run["tenant_id"], self.run["ticket_id"]
            )
            return "found", self.validate_remote(result, plan, key)
        except RemoteError as exc:
            return ("absent" if exc.status == 404 else "unavailable"), None
        except ValueError:
            return "unavailable", None

    def check_approval(self, approval, plan):
        return (
            approval["decision"] == "APPROVED"
            and self.clock() < approval["expires_at"]
            and digest(plan) == approval["plan_hash"]
            and plan["run_id"] == str(self.run_id)
            and plan["tenant_id"] == self.run["tenant_id"]
            and plan["tool"] == "retry_fulfillment"
            and plan["args"]["order_id"] == self.run["bound_order_id"]
        )

    def live_check(self, plan):
        reader = self.reader()
        first = reader.execute("get_ticket", json.dumps({"ticket_id": self.run["ticket_id"]}))
        if not first["ok"] or first["data"]["bound_order_id"] != self.run["bound_order_id"]:
            return False, reader
        reader.bound_order_id = self.run["bound_order_id"]
        reader.execute("get_order", json.dumps({"order_id": reader.bound_order_id}))
        reader.execute(
            "get_fulfillment_events", json.dumps({"order_id": reader.bound_order_id, "limit": 20})
        )
        code, _ = facts(reader, self.clock())
        order = next(
            (e["data"] for e in reader.evidence if e["tool_name"] == "get_order" and e["ok"]), None
        )
        return (
            code == "QUEUE_PUBLISH_TIMEOUT"
            and order is not None
            and order["version"] == plan["args"]["expected_version"]
        ), reader

    def execute(self, state):
        approval = self.db.approval(UUID(state["approval_id"]))
        plan = approval["plan_json"]
        if digest(plan) != approval["plan_hash"] or plan["run_id"] != str(self.run_id):
            return self.terminal("FAILED", "UNDETERMINED", "PLAN_HASH_MISMATCH")
        key = f"fulfillment-retry:{self.run['tenant_id']}:{plan['args']['order_id']}:v{plan['args']['expected_version']}"
        existing = self.operation(key)
        semantic = {
            "operation_type": "retry_fulfillment",
            "tenant_id": self.run["tenant_id"],
            "order_id": plan["args"]["order_id"],
            **{k: plan["args"][k] for k in ("expected_version", "expected_error_code")},
        }
        request_hash = digest(semantic)
        if existing and existing["request_hash"] != request_hash:
            return self.terminal(
                "FAILED",
                "UNDETERMINED",
                "OPERATION_HASH_CONFLICT",
                operation=self.op_view(existing),
            )
        if approval["decision"] == "REJECTED" and (
            existing is None or existing["origin_run_id"] != self.run_id
        ):
            # A shared business key does not transfer another run's approval or outcome.
            # Keep same-run uncertain recovery below: its old request may have committed.
            outcome = self.terminal("REJECTED", "DECLINED", "DECLINED")
            if existing:
                final = outcome["final_result"]
                final["summary"] = "本轮审批已拒绝，本轮未发起履约重试；其他任务存在历史操作记录。"
                final["historical_operation"] = {
                    **self.op_view(existing),
                    "origin_run_id": str(existing["origin_run_id"]),
                }
                final["warnings"] = [
                    "其他任务的同键操作已确认提交，请结合历史记录核对。"
                    if existing["status"] == "CONFIRMED"
                    else "其他任务存在同键历史操作，请核对其结果；本轮拒绝不撤销历史请求。"
                ]
            return outcome
        if existing and existing["status"] == "CONFIRMED":
            return self.verify(key, plan, existing)
        if existing and existing["status"] in {"PREPARED", "UNKNOWN"}:
            status, result = self.lookup(key, plan)
            if status == "found":
                return self.verify(key, plan, self.set_operation(key, "CONFIRMED", result))
            if status == "unavailable":
                return self.terminal(
                    "ESCALATED",
                    "MANUAL_REVIEW",
                    "UNKNOWN_OPERATION",
                    operation=self.op_view(self.set_operation(key, "UNKNOWN")),
                )
        if not self.check_approval(approval, plan):
            if existing and existing["status"] in {"PREPARED", "UNKNOWN"}:
                return self.terminal(
                    "ESCALATED",
                    "MANUAL_REVIEW",
                    "UNKNOWN_OPERATION",
                    operation=self.op_view(self.set_operation(key, "UNKNOWN")),
                )
            if approval["decision"] == "REJECTED":
                return self.terminal("REJECTED", "DECLINED", "DECLINED")
            return self.terminal("ESCALATED", "MANUAL_REVIEW", "APPROVAL_EXPIRED")
        live, _reader = self.live_check(plan)
        if not live:
            if existing and existing["status"] in {"PREPARED", "UNKNOWN"}:
                # A late old request may have committed between the first lookup and recheck.
                status, result = self.lookup(key, plan)
                if status == "found":
                    return self.verify(key, plan, self.set_operation(key, "CONFIRMED", result))
                return self.terminal(
                    "ESCALATED",
                    "MANUAL_REVIEW",
                    "UNKNOWN_OPERATION",
                    operation=self.op_view(self.set_operation(key, "UNKNOWN")),
                )
            errors = [row["error"]["code"] for row in _reader.evidence if not row["ok"]]
            if errors:
                unavailable = any(
                    code != "TICKET_BINDING_CHANGED" and code != "UPSTREAM_HTTP_404"
                    for code in errors
                )
                if unavailable:
                    return self.terminal("FAILED", "UNDETERMINED", "BUSINESS_UNAVAILABLE")
            return self.terminal("ESCALATED", "MANUAL_REVIEW", "STALE_PLAN")
        with self.db.engine.begin() as conn:
            conn.execute(
                insert(self.db.operations)
                .values(
                    id=uuid4(),
                    origin_run_id=self.run_id,
                    tenant_id=self.run["tenant_id"],
                    tool_name="retry_fulfillment",
                    idempotency_key=key,
                    request_hash=request_hash,
                    status="PREPARED",
                    post_attempts=0,
                    started_at=self.clock(),
                )
                .on_conflict_do_nothing()
            )
        operation = self.operation(key)
        if operation["status"] == "FAILED":
            return self.terminal(
                "ESCALATED", "MANUAL_REVIEW", "WRITE_REJECTED", operation=self.op_view(operation)
            )
        if operation["post_attempts"] >= 2:
            return self.terminal(
                "ESCALATED",
                "MANUAL_REVIEW",
                "UNKNOWN_OPERATION",
                operation=self.op_view(self.set_operation(key, "UNKNOWN")),
            )
        # Check formal approval again immediately before starting a new POST.
        if not self.check_approval(self.db.approval(approval["id"]), plan):
            return self.terminal(
                "ESCALATED",
                "MANUAL_REVIEW",
                "UNKNOWN_OPERATION",
                operation=self.op_view(self.set_operation(key, "UNKNOWN")),
            )
        self.guard()
        with self.db.engine.begin() as conn:
            conn.execute(
                self.db.operations.update()
                .where(self.db.operations.c.idempotency_key == key)
                .values(
                    post_attempts=self.db.operations.c.post_attempts + 1,
                )
            )
        try:

            def authorize_post():
                if not self.check_approval(self.db.approval(approval["id"]), plan):
                    raise RemoteError("APPROVAL_EXPIRED")

            # Run after attempt registration and after the remote client's lock guard,
            # immediately before dispatch. A delayed commit must not extend authorization.
            self.remote.before_post = authorize_post
            authorize_post()
            result = self.remote.request(
                "POST",
                f"/internal/orders/{plan['args']['order_id']}/retry-fulfillment",
                self.run["tenant_id"],
                self.run["ticket_id"],
                payload={k: plan["args"][k] for k in ("expected_version", "expected_error_code")},
                key=key,
            )
            result = self.validate_remote(result, plan, key)
        except (RemoteError, ValueError) as exc:
            status, known = self.lookup(key, plan)
            if status == "found":
                return self.verify(key, plan, self.set_operation(key, "CONFIRMED", known))
            if not existing and isinstance(exc, RemoteError) and exc.status in {403, 404, 409, 422}:
                return self.terminal(
                    "ESCALATED",
                    "MANUAL_REVIEW",
                    "WRITE_REJECTED",
                    operation=self.op_view(self.set_operation(key, "FAILED")),
                )
            # A 404 is just not observed yet; never claim the old write cannot commit.
            return self.terminal(
                "ESCALATED",
                "MANUAL_REVIEW",
                "UNKNOWN_OPERATION",
                operation=self.op_view(self.set_operation(key, "UNKNOWN")),
            )
        return self.verify(key, plan, self.set_operation(key, "CONFIRMED", result))

    def verify(self, key, plan, operation):
        observed, result = self.lookup(key, plan)
        if observed != "found":
            return self.terminal(
                "FAILED", "UNDETERMINED", "VERIFICATION_FAILED", operation=self.op_view(operation)
            )
        try:
            order = self.remote.request(
                "GET",
                f"/internal/orders/{plan['args']['order_id']}",
                self.run["tenant_id"],
                self.run["ticket_id"],
            )
            events = self.remote.request(
                "GET",
                f"/internal/orders/{plan['args']['order_id']}/fulfillment-events?limit=20",
                self.run["tenant_id"],
                self.run["ticket_id"],
            )
            valid = (
                order.get("tenant_id") == self.run["tenant_id"]
                and order.get("id") == plan["args"]["order_id"]
                and order.get("fulfillment_status") in {"QUEUED", "SHIPPED"}
                and type(order.get("version")) is int
                and order["version"] >= result["version"]
                and any(
                    e.get("event_type") == "RETRY_ENQUEUED"
                    and e.get("operation_key") == key
                    and e.get("tenant_id") == self.run["tenant_id"]
                    and e.get("order_id") == plan["args"]["order_id"]
                    for e in events.get("events", [])
                )
            )
        except (RemoteError, TypeError, AttributeError):
            valid = False
        if not valid:
            return self.terminal(
                "ESCALATED",
                "MANUAL_REVIEW",
                "VERIFICATION_FAILED",
                operation=self.op_view(operation),
            )
        return self.terminal("SUCCEEDED", "REQUEUED", "REQUEUED", operation=self.op_view(operation))

    def build(self, checkpointer):
        # Lazy model creation allows pending/terminal recovery without an LLM call.
        workflow = self

        class LazyModel:
            def complete(self, *args, **kwargs):
                if workflow.model is None:
                    workflow.model = Model(workflow.settings)
                return workflow.model.complete(*args, **kwargs)

        agent = self.agent_factory(
            self.reader(),
            LazyModel(),
            run_id=str(self.run_id),
            clock=self.clock,
            max_model_calls=self.settings.max_model_calls,
            expected_binding=self.run["bound_order_id"],
            model_observer=self.model_audit,
        )
        graph = agent.add_nodes(StateGraph(FlowState), end="m2_complete", guard=self.guard)
        graph.add_edge(START, "load")

        def guarded(node):
            def call(state):
                self.guard()
                return node(state)

            return call

        graph.add_node("m2_complete", guarded(self.after_diagnosis))
        graph.add_node("prepare", guarded(self.prepare))
        graph.add_node("await_approval", guarded(self.wait))
        graph.add_node("execute", guarded(self.execute))

        def finalize(state):
            final = dict(state["final_result"])
            diagnosis = state.get("result", {}).get("diagnosis")
            final["diagnosis_code"] = diagnosis["diagnosis_code"] if diagnosis else None
            final["evidence_ids"] = diagnosis["evidence_ids"] if diagnosis else []
            with self.db.engine.connect() as conn:
                audit = conn.execute(
                    select(
                        self.db.run_events.c.event_type, self.db.run_events.c.payload_json
                    ).where(
                        self.db.run_events.c.run_id == self.run_id,
                        self.db.run_events.c.event_type.in_(
                            ["LLM_CALL_STARTED", "LLM_CALL_COMPLETED"]
                        ),
                    )
                ).all()
            started = [row for kind, row in audit if kind == "LLM_CALL_STARTED"]
            completed = [row for kind, row in audit if kind == "LLM_CALL_COMPLETED"]
            final["model_call_count"] = (
                len(started) if started else state.get("result", {}).get("model_call_count", 0)
            )
            final["known_total_tokens"] = sum(
                (row.get("usage") or {}).get("total_tokens", 0) for row in completed
            )
            final["usage_unknown"] = len(completed) < len(started) or any(
                row.get("usage") is None for row in completed
            )
            return {"final_result": final}

        graph.add_node("finalize", guarded(finalize))
        graph.add_conditional_edges(
            "m2_complete",
            lambda s: "end" if s.get("final_result") else "prepare",
            {"end": "finalize", "prepare": "prepare"},
        )
        graph.add_edge("prepare", "await_approval")
        graph.add_edge("await_approval", "execute")
        graph.add_edge("execute", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=checkpointer)
