"""One-time application migration followed by checkpoint setup."""

import json
import sys
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError

from app.config import ConfigurationError, get_settings
from app.db import CHECKPOINT_SCHEMA, open_checkpointer


def initialize() -> None:
    settings = get_settings()
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    command.upgrade(config, "head")
    with psycopg.connect(
        settings.checkpoint_database_url, autocommit=True, connect_timeout=3
    ) as connection:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}")
    with open_checkpointer(settings) as saver:
        saver.setup()


def main() -> None:
    try:
        initialize()
    except ConfigurationError as exc:
        print(json.dumps({"error": "CONFIGURATION_ERROR", "message": str(exc)}))
        sys.exit(1)
    except (psycopg.Error, SQLAlchemyError):
        print(json.dumps({"error": "MIGRATION_ERROR", "message": "Check database connectivity."}))
        sys.exit(1)
    print(json.dumps({"status": "initialized", "checkpoint_schema": CHECKPOINT_SCHEMA}))


if __name__ == "__main__":
    main()
