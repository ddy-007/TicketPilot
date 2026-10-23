"""M0 only: no business writes and no model calls."""

import argparse
import json
import os
import sys
from typing import TypedDict

import psycopg
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import ConfigurationError, get_settings
from app.db import open_checkpointer


class RecoveryError(ValueError):
    pass


class RecoveryState(TypedDict, total=False):
    thread_id: str
    decision: dict[str, bool] | None
    status: str


def wait_for_approval(state: RecoveryState) -> dict[str, object]:
    decision = interrupt({"kind": "m0_approval", "message": "M0 checkpoint experiment only"})
    if not isinstance(decision, dict) or set(decision) != {"approved"}:
        raise RecoveryError("Invalid M0 resume signal.")
    if type(decision["approved"]) is not bool:
        raise RecoveryError("Invalid M0 resume signal.")
    return {"decision": decision, "status": "resumed"}


def build_graph(checkpointer):
    graph = StateGraph(RecoveryState)
    graph.add_node("wait_for_approval", wait_for_approval)
    graph.add_edge(START, "wait_for_approval")
    graph.add_edge("wait_for_approval", END)
    return graph.compile(checkpointer=checkpointer)


def run(phase: str, thread_id: str) -> dict[str, object]:
    config = {"configurable": {"thread_id": thread_id}}
    with open_checkpointer(get_settings()) as checkpointer:
        graph = build_graph(checkpointer)
        before = graph.get_state(config)
        if phase == "pause":
            if before.created_at is not None:
                raise RecoveryError("thread_id already exists; choose a new experiment ID.")
            graph.invoke(
                {"thread_id": thread_id, "decision": None, "status": "waiting"}, config=config
            )
        else:
            if before.created_at is None or not before.interrupts:
                raise RecoveryError("No pending interrupt for this thread_id.")
            graph.invoke(Command(resume={"approved": True}), config=config)

        # Re-read persisted state instead of trusting only the invoke response.
        after = graph.get_state(config)
        if phase == "pause" and (len(after.interrupts) != 1 or not after.next):
            raise RecoveryError("Expected a persisted interrupt.")
        if phase == "resume" and (
            after.interrupts or after.next or after.values.get("status") != "resumed"
        ):
            raise RecoveryError("Expected a terminal checkpoint after resume.")
        return {
            "phase": phase,
            "pid": os.getpid(),
            "thread_id": thread_id,
            "status": "interrupted" if phase == "pause" else "resumed",
            "interrupt_count": len(after.interrupts),
            "pending_nodes": list(after.next),
            "decision": after.values.get("decision"),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="M0 PostgreSQL recovery experiment (no writes)")
    parser.add_argument("--phase", choices=("pause", "resume"), required=True)
    parser.add_argument("--thread-id", required=True)
    args = parser.parse_args()
    try:
        result = run(args.phase, args.thread_id)
    except ConfigurationError as exc:
        result = {"error": "CONFIGURATION_ERROR", "message": str(exc)}
    except RecoveryError as exc:
        result = {"error": "RECOVERY_STATE_ERROR", "message": str(exc)}
    except psycopg.Error:
        result = {
            "error": "DATABASE_UNAVAILABLE",
            "message": "Run app.migrate and check PostgreSQL.",
        }
    print(json.dumps(result, ensure_ascii=False))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
