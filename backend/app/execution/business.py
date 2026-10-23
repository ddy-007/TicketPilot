import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.agent.schemas import TicketData


class RemoteError(ValueError):
    def __init__(self, code, status=None):
        self.status = status
        super().__init__(code)


class BusinessClient:
    def __init__(
        self, settings, *, read=None, write=None, guard=None, clock=None, sleep=None, now=None
    ):
        self.read = read or httpx.Client(
            base_url=settings.simulator_base_url,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": "Bearer " + settings.simulator_read_token.get_secret_value()},
        )
        self.write = write
        self.owned_read = read is None
        self.owned_write = False
        if write is None and settings.simulator_write_token.get_secret_value():
            self.write = httpx.Client(
                base_url=settings.simulator_base_url,
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": "Bearer " + settings.simulator_write_token.get_secret_value()
                },
            )
            self.owned_write = True
        self.guard = guard or (lambda: None)
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.now = now or (lambda: datetime.now(UTC))
        self.audit = None
        self.before_post = None

    def close(self):
        if self.owned_read:
            self.read.close()
        if self.owned_write:
            self.write.close()

    def request(self, method, path, tenant, ticket=None, *, payload=None, key=None):
        headers = {"X-Tenant-ID": tenant}
        if ticket:
            headers["X-Ticket-ID"] = ticket
        if key:
            headers["Idempotency-Key"] = key
        client = self.write if method == "POST" else self.read
        if client is None:
            raise RemoteError("WRITE_CLIENT_UNAVAILABLE")
        logical_call_id = "call_" + uuid4().hex
        for attempt in range(3 if method == "GET" else 1):
            self.guard()
            started, started_at = None, None
            retry_delay = 0
            status, result, error = None, None, None
            try:
                if method == "POST" and self.before_post is not None:
                    self.before_post()
                started_at = self.now().isoformat()
                started = self.clock()
                with client.stream(
                    method, path, headers=headers, json=payload, timeout=httpx.Timeout(5, connect=2)
                ) as response:
                    status = response.status_code
                    if status != 200:
                        error = "BUSINESS_HTTP_ERROR"
                        if method == "GET" and status in {429, 500, 502, 503, 504} and attempt < 2:
                            retry_delay = 0.1 * (attempt + 1)
                            continue
                        raise RemoteError("BUSINESS_HTTP_ERROR", status)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise RemoteError("BUSINESS_RESPONSE_TOO_LARGE")
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise RemoteError("BUSINESS_INVALID_RESPONSE")
                    return result
            except httpx.HTTPError:
                error = "BUSINESS_UNAVAILABLE"
                if method == "GET" and attempt < 2:
                    continue
                raise RemoteError("BUSINESS_UNAVAILABLE") from None
            except RemoteError as exc:
                error = str(exc)
                raise
            except (ValueError, UnicodeError):
                error = "BUSINESS_INVALID_RESPONSE"
                raise RemoteError("BUSINESS_INVALID_RESPONSE") from None
            finally:
                # Freeze HTTP timing after the stream closes, before any retry wait.
                duration_ms = 0 if started is None else round((self.clock() - started) * 1000)
                observed_at = self.now().isoformat()
                backoff_started = self.clock()
                try:
                    if retry_delay:
                        self.sleep(retry_delay)
                finally:
                    backoff_ms = (
                        round((self.clock() - backoff_started) * 1000) if retry_delay else 0
                    )
                    if self.audit:
                        self.audit(
                            {
                                "evidence_id": "ev_" + uuid4().hex,
                                "tool_name": "retry_fulfillment"
                                if method == "POST"
                                else "execution_read",
                                "arguments": {"path": path, "operation_key": key},
                                "ok": status == 200 and isinstance(result, dict) and error is None,
                                "data": result if error is None else None,
                                "error": {"code": error} if error else None,
                                "meta": {
                                    "started_at": started_at,
                                    "observed_at": observed_at,
                                    "logical_call_id": logical_call_id,
                                    "duration_ms": duration_ms,
                                    "backoff_ms": backoff_ms,
                                    "truncated": False,
                                },
                                "attempts": [
                                    {
                                        "attempt_no": attempt + 1,
                                        "status_code": status,
                                        "error_code": error,
                                    }
                                ],
                            }
                        )

        raise RemoteError("BUSINESS_UNAVAILABLE")

    def ticket(self, tenant, ticket_id):
        data = self.request("GET", f"/internal/tickets/{ticket_id}", tenant, ticket_id)
        try:
            parsed = TicketData.model_validate_json(json.dumps(data)).model_dump(mode="json")
        except ValidationError:
            raise RemoteError("BUSINESS_INVALID_RESPONSE") from None
        if parsed["tenant_id"] != tenant or parsed["id"] != ticket_id:
            raise RemoteError("BUSINESS_SCOPE_MISMATCH")
        return parsed
