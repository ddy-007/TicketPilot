import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import MetaData, Table, select
from sqlalchemy.dialects.postgresql import insert


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class Database:
    def __init__(self, engine):
        self.engine = engine
        metadata = MetaData(schema="app")
        for name in ("runs", "run_jobs", "approvals", "operations", "tool_calls", "run_events"):
            setattr(self, name, Table(name, metadata, autoload_with=engine))

    def run(self, run_id):
        with self.engine.connect() as conn:
            return conn.execute(select(self.runs).where(self.runs.c.id == run_id)).mappings().one()

    def approval(self, approval_id):
        with self.engine.connect() as conn:
            return (
                conn.execute(select(self.approvals).where(self.approvals.c.id == approval_id))
                .mappings()
                .one()
            )

    def event(self, conn, run_id, kind, payload=None, key=None):
        conn.execute(
            insert(self.run_events)
            .values(
                run_id=run_id,
                event_type=kind,
                payload_json=payload or {},
                created_at=datetime.now(UTC),
                dedupe_key=key,
            )
            .on_conflict_do_nothing()
        )

    def enqueue(self, conn, run_id, kind, now):
        conn.execute(
            self.run_jobs.insert().values(
                id=uuid4(),
                run_id=run_id,
                kind=kind,
                status="QUEUED",
                attempts=0,
                available_at=now,
            )
        )

    def evidence_writer(self, run_id):
        def save(record):
            with self.engine.begin() as conn:
                attempts = record["attempts"] or [{"attempt_no": 0}]
                for attempt in attempts:
                    conn.execute(
                        insert(self.tool_calls)
                        .values(
                            id=uuid4(),
                            run_id=run_id,
                            logical_call_id=record["meta"].get(
                                "logical_call_id", record["evidence_id"]
                            ),
                            attempt_no=attempt["attempt_no"],
                            tool_name=record["tool_name"],
                            record_json={"evidence": record, "attempt": attempt},
                        )
                        .on_conflict_do_nothing()
                    )
                self.event(
                    conn,
                    run_id,
                    "TOOL_ATTEMPT_COMPLETED"
                    if "logical_call_id" in record["meta"]
                    else "TOOL_CALL_COMPLETED",
                    {
                        "evidence_id": record["evidence_id"],
                        "tool_name": record["tool_name"],
                        "ok": record["ok"],
                    },
                    key=record["evidence_id"],
                )

        return save
