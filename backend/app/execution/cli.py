"""Local human-facing API client. Tokens are read from .env and never printed."""

import argparse
import json
from uuid import uuid4

import httpx

from app.config import ConfigurationError, get_settings


def main():
    parser = argparse.ArgumentParser(description="M3 API control (explicit approval only)")
    parser.add_argument("action", choices=["start", "get", "approve", "reject", "events"])
    parser.add_argument("--ticket-id", default="T3001")
    parser.add_argument("--run-id")
    parser.add_argument("--approval-id")
    parser.add_argument("--plan-hash")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    if args.action in {"approve", "reject"} and not (args.approval_id and args.plan_hash):
        parser.error("approval-id and the exact displayed plan-hash are required")
    if args.action in {"get", "events"} and not args.run_id:
        parser.error("run-id is required")
    # This demo client must not send bearer credentials to arbitrary external hosts.
    from urllib.parse import urlparse

    if urlparse(args.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("base-url must point to the local API")
    try:
        settings = get_settings()
        token = (
            settings.demo_approver_token
            if args.action in {"approve", "reject"}
            else settings.demo_operator_token
        )
        with httpx.Client(
            base_url=args.base_url,
            follow_redirects=False,
            trust_env=False,
            timeout=15,
            headers={"Authorization": "Bearer " + token.get_secret_value()},
        ) as client:
            if args.action == "start":
                response = client.post(
                    f"/api/tickets/{args.ticket_id}/runs",
                    json={"auto_note": False},
                    headers={"Idempotency-Key": args.idempotency_key or uuid4().hex},
                )
            elif args.action in {"get", "events"}:
                response = client.get(
                    f"/api/runs/{args.run_id}" + ("/events" if args.action == "events" else "")
                )
            else:
                response = client.post(
                    f"/api/approvals/{args.approval_id}/decision",
                    json={
                        "decision": "APPROVED" if args.action == "approve" else "REJECTED",
                        "plan_hash": args.plan_hash,
                    },
                )
            print(
                json.dumps(
                    {"http_status": response.status_code, "data": response.json()},
                    ensure_ascii=True,
                    default=str,
                )
            )
            if response.is_error:
                raise SystemExit(1)
    except ConfigurationError as exc:
        print(json.dumps({"error": "CONFIGURATION_ERROR", "message": str(exc)}))
        raise SystemExit(1) from None
    except (httpx.HTTPError, ValueError):
        print(json.dumps({"error": "API_UNAVAILABLE_OR_INVALID_RESPONSE"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
