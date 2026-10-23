import json
import os
from datetime import datetime

import pytest

from app.config import Settings
from evaluation.run import DATASET, isolated, run_case


def test_fixed_baseline_reuses_real_approval_and_write_guards_without_models():
    admin = os.getenv("M0_TEST_ADMIN_URL")
    if not admin:
        pytest.skip("Set M0_TEST_ADMIN_URL for isolated M5 evaluation.")
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
        llm_api_key="",
    )
    with isolated(admin, dataset["fixture_now"], settings) as (configured, engine):
        for case_id in ("test-01", "test-02", "test-07", "test-09", "test-10"):
            case = next(c for c in dataset["cases"] if c["id"] == case_id)
            result = run_case(
                configured,
                engine,
                case,
                "fixed",
                datetime.fromisoformat(dataset["fixture_now"]),
                {"used": 0, "limit": 1},
            )
            assert result["grade"]["passed"], result["reason_code"]
            assert result["usage"]["calls"] == 0
