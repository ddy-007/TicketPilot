import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.agent.schemas import TOOLS


class ToolError(ValueError):
    pass


class ReadTools:
    def __init__(
        self,
        client,
        tenant_id,
        ticket_id,
        *,
        limit=10,
        save=None,
        secrets=(),
        sleep=time.sleep,
        monotonic=time.monotonic,
        deadline=None,
        guard=None,
    ):
        self.client = client
        self.tenant_id = tenant_id
        self.ticket_id = ticket_id
        self.bound_order_id = None
        self.limit = limit
        self.evidence = []
        self.save = save or (lambda evidence: None)
        self.secrets = tuple(value for value in secrets if value)
        self.sleep = sleep
        self.monotonic = monotonic
        self.deadline = monotonic() + 120 if deadline is None else deadline
        self.guard = guard or (lambda: None)

    def clean(self, value):
        # Persist/model only allowlisted fields, with configured secrets removed.
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        return value

    def validate(self, name, arguments):
        if name not in TOOLS:
            raise ToolError("UNKNOWN_TOOL")
        try:
            if not isinstance(arguments, str) or len(arguments) > 2048:
                raise ValueError
            args = TOOLS[name][0].model_validate_json(arguments).model_dump()
        except (ValidationError, ValueError):
            raise ToolError("INVALID_TOOL_ARGUMENTS") from None
        if name == "get_ticket":
            if args["ticket_id"] != self.ticket_id:
                raise ToolError("TOOL_SCOPE_VIOLATION")
        elif not self.bound_order_id or args["order_id"] != self.bound_order_id:
            raise ToolError("TOOL_SCOPE_VIOLATION")
        return args

    def execute(self, name, arguments):
        if len(self.evidence) >= self.limit:
            raise ToolError("READ_BUDGET_EXHAUSTED")
        started = self.monotonic()
        record = {
            "evidence_id": "ev_" + uuid4().hex,
            "tool_name": name if name in TOOLS else "unknown_tool",
            "arguments": {},
            "ok": False,
            "data": None,
            "error": None,
            "meta": {
                "observed_at": datetime.now(UTC).isoformat(),
                "duration_ms": 0,
                "truncated": False,
            },
            "attempts": [],
        }
        try:
            args = self.validate(name, arguments)
            record["arguments"] = args
            path = {
                "get_ticket": f"/internal/tickets/{self.ticket_id}",
                "get_order": f"/internal/orders/{self.bound_order_id}",
                "get_fulfillment_events": f"/internal/orders/{self.bound_order_id}/fulfillment-events",
            }[name]
            headers = {"X-Tenant-ID": self.tenant_id, "X-Ticket-ID": self.ticket_id}
            params = {"limit": args["limit"]} if name == "get_fulfillment_events" else None
            for attempt in range(1, 4):
                remaining = self.deadline - self.monotonic()
                if remaining <= 0:
                    raise ToolError("ACTIVE_TIME_EXHAUSTED")
                code = None
                status = None
                retryable = False
                self.guard()
                attempt_started = self.monotonic()
                attempt_started_at = datetime.now(UTC).isoformat()
                try:
                    with self.client.stream(
                        "GET",
                        path,
                        headers=headers,
                        params=params,
                        timeout=httpx.Timeout(min(5, remaining), connect=min(2, remaining)),
                    ) as response:
                        status = response.status_code
                        if status != 200:
                            code = f"UPSTREAM_HTTP_{status}"
                            retryable = status in {429, 500, 502, 503, 504}
                        else:
                            body = bytearray()
                            for chunk in response.iter_bytes():
                                if self.monotonic() >= self.deadline:
                                    raise ToolError("ACTIVE_TIME_EXHAUSTED")
                                body.extend(chunk)
                                if len(body) > 65536:
                                    raise ToolError("UPSTREAM_RESPONSE_TOO_LARGE")
                            data = (
                                TOOLS[name][1]
                                .model_validate_json(bytes(body))
                                .model_dump(mode="json")
                            )
                            self._validate_data(name, data)
                            record["data"] = self.clean(data)
                            record["ok"] = True
                except (httpx.TimeoutException, httpx.NetworkError):
                    code, retryable = "UPSTREAM_UNAVAILABLE", True
                except ValidationError:
                    code = "UPSTREAM_INVALID_DATA"
                except ToolError as exc:
                    code = str(exc)
                except httpx.HTTPError:
                    code = "UPSTREAM_PROTOCOL_ERROR"
                record["attempts"].append(
                    {
                        "attempt_no": attempt,
                        "started_at": attempt_started_at,
                        "duration_ms": round((self.monotonic() - attempt_started) * 1000),
                        "status_code": status,
                        "error_code": code,
                        "retryable": retryable,
                    }
                )
                if record["ok"]:
                    break
                if not retryable or attempt == 3:
                    raise ToolError(code or "UPSTREAM_INVALID_DATA")
                self.sleep(min(0.5 * attempt, max(0, self.deadline - self.monotonic())))
        except ToolError as exc:
            record["error"] = {"code": str(exc), "retryable": False}
        except httpx.HTTPError:
            record["error"] = {"code": "UPSTREAM_PROTOCOL_ERROR", "retryable": False}
        record["meta"]["duration_ms"] = round((self.monotonic() - started) * 1000)
        self.save(record)
        self.evidence.append(record)
        return record

    def _validate_data(self, name, data):
        if name == "get_ticket":
            if data["tenant_id"] != self.tenant_id or data["id"] != self.ticket_id:
                raise ToolError("UPSTREAM_SCOPE_MISMATCH")
            if self.evidence and data["bound_order_id"] != self.bound_order_id:
                raise ToolError("TICKET_BINDING_CHANGED")
        elif name == "get_order":
            if data["tenant_id"] != self.tenant_id or data["id"] != self.bound_order_id:
                raise ToolError("UPSTREAM_SCOPE_MISMATCH")
        elif any(
            event["tenant_id"] != self.tenant_id or event["order_id"] != self.bound_order_id
            for event in data["events"]
        ):
            raise ToolError("UPSTREAM_SCOPE_MISMATCH")


def tool_message(record):
    return {key: record[key] for key in ("ok", "evidence_id", "data", "error", "meta")}
