"""M1 acceptance: synthetic HTTP operations, no LLM and no public write API."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx

from simulator.config import get_simulator_settings


class SmokeError(ValueError):
    pass


def require(condition: bool) -> None:
    if not condition:
        raise SmokeError("M1 acceptance condition failed.")


def main() -> None:
    settings = get_simulator_settings()
    common = {"X-Tenant-ID": "demo", "X-Ticket-ID": "T1001"}
    read = {**common, "Authorization": "Bearer " + settings.simulator_read_token.get_secret_value()}
    write = {
        **common,
        "Authorization": "Bearer " + settings.simulator_write_token.get_secret_value(),
        "Idempotency-Key": "fulfillment-retry:demo:O2001:v3",
    }
    path = "/internal/orders/O2001/retry-fulfillment"
    payload = {"expected_version": 3, "expected_error_code": "QUEUE_PUBLISH_TIMEOUT"}
    try:
        with httpx.Client(base_url="http://simulator:8001", timeout=15, trust_env=False) as client:
            ticket = client.get("/internal/tickets/T1001", headers=read)
            require(ticket.status_code == 200)
            require(ticket.json()["bound_order_id"] == "O2001")
            require(client.get("/internal/orders/O2001", headers=read).status_code == 200)
            require(
                client.get("/internal/orders/O2001/fulfillment-events", headers=read).status_code
                == 200
            )
            require(
                client.post(
                    path,
                    headers={**read, "Idempotency-Key": write["Idempotency-Key"]},
                    json=payload,
                ).status_code
                == 403
            )

            def retry(_):
                return client.post(path, headers=write, json=payload)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(retry, range(2)))
            require(all(r.status_code == 200 for r in results))
            require(results[0].json() == results[1].json())
            result = results[0].json()
            require(result["order_id"] == "O2001")
            require(result["fulfillment_status"] == "QUEUED" and result["version"] == 4)
            require(retry(None).json() == result)
            queried = client.get("/internal/operations/" + write["Idempotency-Key"], headers=read)
            require(queried.status_code == 200 and queried.json() == result)
            current = client.get("/internal/orders/O2001", headers=read)
            require(current.status_code == 200)
            current = current.json()
            require(current["fulfillment_status"] == "QUEUED")
            require(current["version"] >= result["version"])
            events = client.get("/internal/orders/O2001/fulfillment-events", headers=read).json()[
                "events"
            ]
            count = sum(
                e["event_type"] == "RETRY_ENQUEUED"
                and e["order_id"] == "O2001"
                and (e["operation_key"] == write["Idempotency-Key"])
                for e in events
            )
            require(count == 1)
            require(client.get("/internal/tickets/T1001", headers=read).json()["status"] == "OPEN")
            print(
                json.dumps(
                    {
                        "milestone": "M1",
                        "status": "passed",
                        "state": current["fulfillment_status"],
                        "version": current["version"],
                        "retry_events": count,
                        "concurrent_requests": 2,
                        "read_token_write_status": 403,
                        "ticket_status": "OPEN",
                        "llm_calls": 0,
                    }
                )
            )
    except (httpx.HTTPError, ValueError, KeyError):
        print(json.dumps({"milestone": "M1", "status": "failed", "error": "SMOKE_FAILED"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
