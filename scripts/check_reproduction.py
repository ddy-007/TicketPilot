"""Verify the documented disposable Compose deployment without model calls."""

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "ticketpilot-m5-repro",
    "-f",
    "compose.yaml",
    "-f",
    "docs/compose.reproduce.yaml",
]


def recovery(phase, thread):
    result = subprocess.run(
        [*COMPOSE, "run", "--rm", "recovery", "--phase", phase, "--thread-id", thread],
        cwd=ROOT,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise SystemExit("Disposable recovery failed; private process output suppressed.")
    rows = [line for line in result.stdout.decode("utf-8").splitlines() if line.startswith("{")]
    return json.loads(rows[-1])


def main():
    with httpx.Client(base_url="http://127.0.0.1:18010", trust_env=False, timeout=10) as client:
        ready = client.get("/health/ready")
        ready.raise_for_status()
        assert ready.json() == {"status": "ready", "database": "ok", "checkpoint": "ok"}
        index = client.get("/")
        index.raise_for_status()
        assert "TicketPilot" in index.text
        script = re.search(r'src="(/assets/[^\"]+\.js)"', index.text)
        assert script and client.get(script[1]).status_code == 200
        headers = {"Authorization": "Bearer " + "o" * 40}
        response = client.get("/api/tickets", headers=headers)
        response.raise_for_status()
        tickets = response.json()["tickets"]
        assert len(tickets) == 7 and all("body" not in ticket for ticket in tickets)
        response = client.get("/api/tickets/T1001", headers=headers)
        response.raise_for_status()
        assert response.json()["bound_order_id"] == "O2001"
    thread = "repro-" + uuid4().hex
    paused = recovery("pause", thread)
    resumed = recovery("resume", thread)
    assert paused["status"] == "interrupted" and paused["interrupt_count"] == 1
    assert resumed["status"] == "resumed" and resumed["interrupt_count"] == 0
    assert resumed["pending_nodes"] == []
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "git_revision": os.getenv("GITHUB_SHA"),
        "runner_os": os.getenv("RUNNER_OS"),
        "ready": True,
        "static_bundle": True,
        "seeded_tickets": len(tickets),
        "ticket_detail": True,
        "separate_recovery_containers": True,
        "pause": paused,
        "resume": resumed,
        "model_calls": 0,
    }
    output = ROOT / "artifacts/reproduction.json"
    output.parent.mkdir(exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
