from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Column, DateTime, ForeignKey, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

metadata = MetaData(schema="app")
runs = Table(
    "diagnostic_runs",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("ticket_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("result_json", JSONB),
)
evidence = Table(
    "diagnostic_evidence",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("run_id", PGUUID(as_uuid=True), ForeignKey(runs.c.id), nullable=False),
    Column("record_json", JSONB, nullable=False),
)


class Store:
    def __init__(self, engine, run_id, tenant_id, ticket_id):
        self.engine = engine
        self.run_id = UUID(run_id)
        with engine.begin() as conn:
            conn.execute(
                runs.insert().values(
                    id=self.run_id,
                    tenant_id=tenant_id,
                    ticket_id=ticket_id,
                    created_at=datetime.now(UTC),
                )
            )

    def save_evidence(self, record):
        with self.engine.begin() as conn:
            conn.execute(
                evidence.insert().values(
                    id=record["evidence_id"],
                    run_id=self.run_id,
                    record_json=record,
                )
            )

    def finish(self, result):
        with self.engine.begin() as conn:
            conn.execute(
                runs.update()
                .where(runs.c.id == self.run_id)
                .values(
                    finished_at=datetime.now(UTC),
                    result_json=result,
                )
            )


def load(engine, run_id, tenant_id):
    with engine.connect() as conn:
        return conn.execute(
            select(runs.c.result_json).where(
                runs.c.id == UUID(run_id),
                runs.c.tenant_id == tenant_id,
            )
        ).scalar()
