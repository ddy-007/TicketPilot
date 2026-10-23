"""Opt-in real PostgreSQL checks. Every run creates its own test database."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sqlalchemy import text
from sqlalchemy.engine import URL

from app.config import Settings
from app.db import check_checkpoint_database, check_database, create_app_engine

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def database_env():
    admin_url = os.getenv("M0_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set M0_TEST_ADMIN_URL for isolated PostgreSQL integration tests.")
    name = "ticketpilot_test_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            params = conninfo_to_dict(admin_url)
            checkpoint_url = make_conninfo(admin_url, dbname=name)
            app_url = URL.create(
                "postgresql+psycopg",
                username=params.get("user"),
                password=params.get("password"),
                host=params.get("host"),
                port=int(params.get("port", 5432)),
                database=name,
            ).render_as_string(hide_password=False)
            env = {
                **os.environ,
                "APP_DATABASE_URL": app_url,
                "CHECKPOINT_DATABASE_URL": checkpoint_url,
            }
            yield env
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def child(env, *args, ok=True):
    completed = subprocess.run(
        [sys.executable, "-m", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    # Do not expose stderr: failed drivers may include a connection string.
    assert (completed.returncode == 0) == ok, f"Child exit={completed.returncode}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_migration_and_cross_process_recovery(database_env):
    env = database_env
    settings = Settings(
        _env_file=None,
        app_database_url=env["APP_DATABASE_URL"],
        checkpoint_database_url=env["CHECKPOINT_DATABASE_URL"],
    )
    engine = create_app_engine(settings)
    try:
        assert not check_database(engine)
        assert not check_checkpoint_database(settings)
        assert child(env, "app.migrate")["status"] == "initialized"
        assert child(env, "app.migrate")["status"] == "initialized"
        assert check_database(engine)
        assert check_checkpoint_database(settings)

        thread = "m0-" + uuid4().hex
        pause = child(env, "app.experiments.recovery", "--phase", "pause", "--thread-id", thread)
        assert pause["status"] == "interrupted"
        assert pause["interrupt_count"] == 1
        resume = child(env, "app.experiments.recovery", "--phase", "resume", "--thread-id", thread)
        assert resume["status"] == "resumed"
        assert resume["decision"] == {"approved": True}
        assert resume["interrupt_count"] == 0
        assert resume["pending_nodes"] == []
        assert pause["pid"] != resume["pid"]

        for phase, target in [("resume", "missing"), ("resume", thread), ("pause", thread)]:
            assert (
                child(
                    env,
                    "app.experiments.recovery",
                    "--phase",
                    phase,
                    "--thread-id",
                    target,
                    ok=False,
                )["error"]
                == "RECOVERY_STATE_ERROR"
            )
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE app.m0_metadata"))
        assert not check_database(engine)
        with psycopg.connect(env["CHECKPOINT_DATABASE_URL"], autocommit=True) as connection:
            connection.execute("DELETE FROM checkpoint.checkpoint_migrations WHERE v=0")
        assert not check_checkpoint_database(settings)
    finally:
        engine.dispose()
