"""Fixed-clock HTTP simulator factory used only by the isolated evaluation command."""

import os
from datetime import datetime

from simulator.main import create_simulator_app


def create_app():
    now = datetime.fromisoformat(os.environ["EVAL_FIXTURE_NOW"])
    return create_simulator_app(clock=lambda: now)
