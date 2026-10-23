"""Test-only subprocess entrypoints. No production environment fault switches."""

import os
import sys
from datetime import UTC, datetime

from app.config import get_settings
from app.db import create_app_engine
from app.execution.business import BusinessClient
from app.execution.worker import Worker
from simulator.main import create_simulator_app
from tests.agent_fakes import ScriptedModel

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)


def create_simulator():
    return create_simulator_app(clock=lambda: NOW)


class CrashAfterWrite(BusinessClient):
    def request(self, method, *args, **kwargs):
        result = super().request(method, *args, **kwargs)
        if method == "POST":
            os._exit(92)
        return result


def main():
    settings = get_settings()
    engine = create_app_engine(settings)
    mode = sys.argv[1]

    def fault(point):
        if mode == "after_checkpoint" and point == "after_checkpoint":
            os._exit(91)

    worker = Worker(
        engine,
        settings,
        model_factory=ScriptedModel,
        clock=lambda: NOW,
        fault=fault,
        business_factory=(lambda: CrashAfterWrite(settings)) if mode == "after_write" else None,
    )
    worker.start()
    try:
        worker.once()
    finally:
        worker.close()
        engine.dispose()


if __name__ == "__main__":
    main()
