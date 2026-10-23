import argparse
import json
import sys
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.agent.graph import Agent
from app.agent.llm import Model
from app.agent.schemas import TicketArgs
from app.agent.store import Store, load
from app.agent.tools import ReadTools
from app.config import ConfigurationError, get_settings
from app.db import create_app_engine


def main():
    parser = argparse.ArgumentParser(description="M2 read-only diagnosis: no writes or approvals")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--ticket-id")
    parser.add_argument("--show-run")
    parser.add_argument(
        "--details", action="store_true", help="Include sanitized evidence and proposal JSON"
    )
    args = parser.parse_args()
    engine = None
    model = None
    try:
        # CLI is a trusted local developer interface, not browser authentication.
        TicketArgs(ticket_id=args.tenant_id)
        settings = get_settings()
        engine = create_app_engine(settings)
        if args.show_run:
            result = load(engine, args.show_run, args.tenant_id)
            if result is None:
                raise ConfigurationError("Diagnostic run not found in this tenant.")
        else:
            TicketArgs(ticket_id=args.ticket_id)
            if not settings.simulator_read_token.get_secret_value():
                raise ConfigurationError("Missing SIMULATOR_READ_TOKEN")
            model = Model(settings)
            run_id = str(uuid4())
            store = Store(engine, run_id, args.tenant_id, args.ticket_id)
            with httpx.Client(
                base_url=settings.simulator_base_url,
                headers={
                    "Authorization": "Bearer " + settings.simulator_read_token.get_secret_value()
                },
                follow_redirects=False,
                trust_env=False,
            ) as http:
                reader = ReadTools(
                    http,
                    args.tenant_id,
                    args.ticket_id,
                    limit=settings.max_read_calls,
                    save=store.save_evidence,
                    secrets=(
                        settings.llm_api_key.get_secret_value(),
                        settings.simulator_read_token.get_secret_value(),
                    ),
                )
                result = Agent(
                    reader,
                    model,
                    run_id=run_id,
                    save_result=store.finish,
                    max_model_calls=settings.max_model_calls,
                ).run()
        # Avoid echoing arbitrary ticket/free text and upstream response content in terminal.
        if args.details:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(
                json.dumps(
                    {
                        key: result.get(key)
                        for key in (
                            "milestone",
                            "run_id",
                            "ticket_id",
                            "status",
                            "reason_code",
                            "summary",
                            "executed",
                            "model_call_count",
                            "read_call_count",
                            "http_attempt_count",
                            "active_elapsed_ms",
                            "known_total_tokens",
                            "usage_unknown",
                        )
                    },
                    ensure_ascii=False,
                )
            )
        if result["status"] == "FAILED":
            sys.exit(1)
    except ConfigurationError as exc:
        print(json.dumps({"error": "CONFIGURATION_ERROR", "message": str(exc)}))
        sys.exit(1)
    except (ValidationError, ValueError):
        print(json.dumps({"error": "INVALID_INPUT"}))
        sys.exit(1)
    except SQLAlchemyError:
        print(json.dumps({"error": "DIAGNOSTIC_DATABASE_ERROR"}))
        sys.exit(1)
    finally:
        if model:
            model.close()
        if engine:
            engine.dispose()


if __name__ == "__main__":
    main()
