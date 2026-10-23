import argparse
import json
import time
from datetime import UTC, datetime
from uuid import UUID

import psycopg
from fastapi import HTTPException
from langgraph.types import Command
from sqlalchemy import or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.agent.graph import Agent
from app.config import ConfigurationError, get_settings
from app.db import create_app_engine, open_checkpointer
from app.execution.api import DecisionRequest, decide
from app.execution.business import BusinessClient
from app.execution.db import Database
from app.execution.workflow import Workflow

LOCK_ID = 9081701


class WorkerLockLost(RuntimeError):
    pass


class WorkerBusy(RuntimeError):
    pass


class Worker:
    def __init__(
        self,
        engine,
        settings,
        *,
        model_factory=None,
        business_factory=None,
        clock=None,
        fault=None,
        agent_factory=Agent,
    ):
        self.engine, self.settings = engine, settings
        self.db = Database(engine)
        self.model_factory = model_factory
        self.agent_factory = agent_factory
        self.business_factory = business_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.fault = fault or (lambda point: None)  # dependency injection in tests only
        self.lock = None
        self.backend_pid = None

    def start(self):
        self.lock = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        acquired = self.lock.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}
        ).scalar()
        if not acquired:
            self.lock.close()
            self.lock = None
            raise WorkerBusy("A worker already owns the session lock.")
        self.backend_pid = self.lock.execute(text("SELECT pg_backend_pid()")).scalar()
        with self.engine.begin() as conn:
            conn.execute(
                self.db.run_jobs.update()
                .where(self.db.run_jobs.c.status == "RUNNING")
                .values(status="QUEUED")
            )

    def guard(self):
        if self.lock is None:
            raise WorkerLockLost()
        try:
            pid = self.lock.execute(text("SELECT pg_backend_pid()")).scalar()
            owned = self.lock.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted AND objid=:key)"
                ),
                {"key": LOCK_ID},
            ).scalar()
        except SQLAlchemyError:
            raise WorkerLockLost() from None
        if pid != self.backend_pid or not owned:
            raise WorkerLockLost()

    def close(self):
        if self.lock:
            try:
                self.lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})
            except SQLAlchemyError:
                self.lock.invalidate()
            self.lock.close()
            self.lock = None

    def expire(self):
        self.guard()
        with self.engine.connect() as conn:
            candidates = conn.execute(
                select(
                    self.db.approvals.c.id, self.db.runs.c.tenant_id, self.db.approvals.c.plan_hash
                )
                .join(self.db.runs, self.db.runs.c.id == self.db.approvals.c.run_id)
                .where(
                    self.db.approvals.c.decision == "PENDING",
                    self.db.runs.c.status == "WAITING_APPROVAL",
                    self.db.approvals.c.expires_at <= self.clock(),
                )
                .limit(100)
            ).all()
        for approval_id, tenant, plan_hash in candidates:
            try:
                decide(
                    self.db,
                    approval_id,
                    tenant,
                    "expiry-scanner",
                    DecisionRequest(
                        decision="REJECTED",
                        plan_hash=plan_hash,
                    ),
                    self.clock,
                )
            except HTTPException as exc:
                if exc.status_code not in {404, 409}:
                    raise

    def claim(self):
        self.guard()
        with self.engine.begin() as conn:
            job = (
                conn.execute(
                    select(self.db.run_jobs)
                    .where(
                        self.db.run_jobs.c.status == "QUEUED",
                        self.db.run_jobs.c.available_at <= self.clock(),
                    )
                    .order_by(self.db.run_jobs.c.available_at, self.db.run_jobs.c.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not job:
                return None
            job = dict(job)
            job["attempts"] += 1
            conn.execute(
                self.db.run_jobs.update()
                .where(self.db.run_jobs.c.id == job["id"])
                .values(
                    status="RUNNING",
                    attempts=job["attempts"],
                    started_at=self.clock(),
                )
            )
            conn.execute(
                self.db.runs.update()
                .where(self.db.runs.c.id == job["run_id"])
                .values(status="RUNNING", updated_at=self.clock())
            )
            self.db.event(conn, job["run_id"], "RUN_STARTED", {"attempt": job["attempts"]})
            return job

    def synchronize(self, job, snapshot):
        self.guard()
        if snapshot.interrupts:
            status = "WAITING_APPROVAL"
            values = {"status": status, "updated_at": self.clock()}
        else:
            result = snapshot.values.get("final_result")
            if snapshot.next or not result:
                raise ValueError("INVALID_CHECKPOINT_STATE")
            values = {
                "status": result["status"],
                "result_json": result,
                "disposition": result["disposition"],
                "failure_code": result["reason_code"]
                if result["status"] in {"FAILED", "ESCALATED"}
                else None,
                "updated_at": self.clock(),
            }
        with self.engine.begin() as conn:
            # Same lock order as approval submission; readiness only after checkpoint is saved.
            conn.execute(
                select(self.db.runs.c.id)
                .where(self.db.runs.c.id == job["run_id"])
                .with_for_update()
            ).one()
            conn.execute(
                self.db.runs.update().where(self.db.runs.c.id == job["run_id"]).values(**values)
            )
            conn.execute(
                self.db.run_jobs.update()
                .where(self.db.run_jobs.c.id == job["id"])
                .values(status="DONE", finished_at=self.clock())
            )
            self.db.event(
                conn,
                job["run_id"],
                "RUN_WAITING_APPROVAL" if snapshot.interrupts else "RUN_FINISHED",
                {"status": values["status"]},
                "waiting" if snapshot.interrupts else "finished",
            )

    def exhausted(self, job):
        with self.engine.begin() as conn:
            approval = conn.execute(
                select(self.db.approvals.c.plan_json).where(
                    self.db.approvals.c.run_id == job["run_id"],
                )
            ).scalar()
            key = None
            if approval:
                key = f"fulfillment-retry:{approval['tenant_id']}:{approval['args']['order_id']}:v{approval['args']['expected_version']}"
            possible = (
                conn.execute(
                    select(self.db.operations).where(
                        or_(
                            self.db.operations.c.origin_run_id == job["run_id"],
                            self.db.operations.c.idempotency_key == key,
                        ),
                        self.db.operations.c.status.in_(["PREPARED", "UNKNOWN", "CONFIRMED"]),
                    )
                )
                .mappings()
                .first()
            )
            result = {
                "run_id": str(job["run_id"]),
                "status": "FAILED",
                "disposition": "UNDETERMINED",
                "reason_code": "JOB_ATTEMPTS_EXHAUSTED",
                "summary": "恢复次数已耗尽，需要人工核对。",
                "executed": None if possible else False,
                "note_sync_status": "SKIPPED",
                "warnings": ["历史请求可能已执行，需要人工核对。"] if possible else [],
            }
            conn.execute(
                self.db.runs.update()
                .where(self.db.runs.c.id == job["run_id"])
                .values(
                    status="FAILED",
                    disposition="UNDETERMINED",
                    result_json=result,
                    failure_code="JOB_ATTEMPTS_EXHAUSTED",
                    updated_at=self.clock(),
                )
            )
            conn.execute(
                self.db.run_jobs.update()
                .where(self.db.run_jobs.c.id == job["id"])
                .values(
                    status="FAILED", last_error="JOB_ATTEMPTS_EXHAUSTED", finished_at=self.clock()
                )
            )

    def once(self):
        self.expire()
        job = self.claim()
        if job is None:
            return False
        remote, workflow, saved_snapshot = None, None, None
        try:
            remote = (
                self.business_factory()
                if self.business_factory
                else BusinessClient(self.settings, guard=self.guard)
            )
            remote.guard = self.guard
            workflow = Workflow(
                self.db,
                job["run_id"],
                self.settings,
                remote,
                model=self.model_factory() if self.model_factory else None,
                clock=self.clock,
                guard=self.guard,
                agent_factory=self.agent_factory,
            )
            with open_checkpointer(self.settings) as cp:
                graph = workflow.build(cp)
                config = {"configurable": {"thread_id": str(job["run_id"])}, "recursion_limit": 50}
                snapshot = graph.get_state(config)
                if (
                    snapshot.created_at
                    and not snapshot.next
                    and snapshot.values.get("final_result")
                ):
                    self.synchronize(job, snapshot)
                    return True
                if snapshot.interrupts:
                    approval = self.db.approval(UUID(snapshot.values["approval_id"]))
                    if approval["decision"] == "PENDING":
                        self.synchronize(job, snapshot)
                        return True
                    command = Command(resume={"wake": True})
                else:
                    command = None if snapshot.created_at else {}
                if job["attempts"] > 3:
                    self.exhausted(job)
                    return True
                self.guard()
                graph.invoke(command, config)
                snapshot = graph.get_state(config)
                saved_snapshot = snapshot
                self.fault("after_checkpoint")
                self.synchronize(job, snapshot)
        except WorkerLockLost:
            raise
        except (SQLAlchemyError, psycopg.Error, ValueError, RuntimeError, KeyError):
            self.guard()
            if saved_snapshot and (
                saved_snapshot.interrupts
                or (not saved_snapshot.next and saved_snapshot.values.get("final_result"))
            ):
                # A saved waiting/terminal checkpoint wins over an application sync fault,
                # including on the last allowed computation attempt. Do not rerun the graph.
                try:
                    self.synchronize(job, saved_snapshot)
                    return True
                except SQLAlchemyError:
                    # Keep the job recoverable for checkpoint-only synchronization.
                    with self.engine.begin() as conn:
                        conn.execute(
                            self.db.run_jobs.update()
                            .where(self.db.run_jobs.c.id == job["id"])
                            .values(
                                status="QUEUED",
                                last_error="CHECKPOINT_SYNC_ERROR",
                            )
                        )
                    return True
            if job["attempts"] >= 3:
                self.exhausted(job)
            else:
                with self.engine.begin() as conn:
                    conn.execute(
                        self.db.run_jobs.update()
                        .where(self.db.run_jobs.c.id == job["id"])
                        .values(status="QUEUED", last_error="WORKER_EXECUTION_ERROR")
                    )
        finally:
            if remote is not None:
                remote.close()
            if (
                workflow is not None
                and workflow.model is not None
                and hasattr(workflow.model, "close")
            ):
                workflow.model.close()
        return True


def main():
    parser = argparse.ArgumentParser(description="Single durable TicketPilot worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    engine = None
    worker = None
    try:
        settings = get_settings()
        if (
            not settings.simulator_read_token.get_secret_value()
            or not settings.simulator_write_token.get_secret_value()
        ):
            raise ConfigurationError("Missing simulator read/write token configuration.")
        engine = create_app_engine(settings)
        worker = Worker(engine, settings)
        worker.start()
        print(json.dumps({"worker": "ready"}), flush=True)
        while True:
            worked = worker.once()
            if args.once:
                break
            if not worked:
                time.sleep(0.5)
    except ConfigurationError as exc:
        print(json.dumps({"error": "CONFIGURATION_ERROR", "message": str(exc)}), flush=True)
        raise SystemExit(1) from None
    except (WorkerBusy, WorkerLockLost, SQLAlchemyError, psycopg.Error):
        print(json.dumps({"error": "WORKER_UNAVAILABLE"}), flush=True)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    finally:
        if worker:
            worker.close()
        if engine:
            engine.dispose()


if __name__ == "__main__":
    main()
