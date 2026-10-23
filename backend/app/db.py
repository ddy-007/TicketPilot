from contextlib import contextmanager
from pathlib import Path

import psycopg
from alembic.script import ScriptDirectory
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import make_conninfo
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings

CHECKPOINT_SCHEMA = "checkpoint"
REQUIRED_TABLES = (
    "app.m0_metadata",
    "app.diagnostic_runs",
    "app.diagnostic_evidence",
    "app.runs",
    "app.run_jobs",
    "app.approvals",
    "app.operations",
    "app.tool_calls",
    "app.run_events",
    "sim.orders",
    "sim.tickets",
    "sim.fulfillment_events",
    "sim.idempotency_records",
)


def migration_heads() -> set[str]:
    return set(ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations")).get_heads())


def create_app_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.app_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
    )


def check_database(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            if any(
                connection.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
                is None
                for name in REQUIRED_TABLES
            ):
                return False
            return set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            ) == migration_heads() and (
                connection.execute(
                    text("SELECT value FROM app.m0_metadata WHERE key='schema_version'")
                ).scalar()
                == "m0"
            )
    except SQLAlchemyError:
        return False


def checkpoint_conninfo(settings: Settings) -> str:
    # Fixed schema for every initializer and experiment process.
    return make_conninfo(
        settings.checkpoint_database_url,
        connect_timeout=3,
        options=f"-c search_path={CHECKPOINT_SCHEMA} -c statement_timeout=10000",
    )


@contextmanager
def open_checkpointer(settings: Settings):
    with PostgresSaver.from_conn_string(checkpoint_conninfo(settings)) as saver:
        yield saver


def check_checkpoint_database(settings: Settings) -> bool:
    try:
        with psycopg.connect(checkpoint_conninfo(settings)) as connection:
            tables = (
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
                "checkpoint_migrations",
            )
            for table in tables:
                if connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
                    return False
            versions = {r[0] for r in connection.execute("SELECT v FROM checkpoint_migrations")}
            return versions == set(range(len(PostgresSaver.MIGRATIONS)))
    except (psycopg.Error, ValueError):
        return False
