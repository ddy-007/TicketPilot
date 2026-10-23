"""M3 application runs, durable jobs, approvals, operations and audit."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004_m3_execution"
down_revision = "0003_m2_diagnostics"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("thread_id", sa.String(36), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("bound_order_id", sa.String(64)),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("start_idempotency_key", sa.String(128), nullable=False),
        sa.Column("start_request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("disposition", sa.String(32)),
        sa.Column("auto_note", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approval_id", UUID(as_uuid=True)),
        sa.Column("result_json", JSONB),
        sa.Column("note_sync_status", sa.String(16), nullable=False, server_default="SKIPPED"),
        sa.Column("failure_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "created_by", "start_idempotency_key", name="run_start_key"
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','WAITING_APPROVAL','SUCCEEDED','ESCALATED','REJECTED','FAILED')",
            name="run_status",
        ),
        sa.CheckConstraint("auto_note=false AND note_sync_status='SKIPPED'", name="notes_disabled"),
        schema="app",
    )
    op.create_index(
        "one_active_run_per_ticket",
        "runs",
        ["tenant_id", "ticket_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING','WAITING_APPROVAL')"),
        schema="app",
    )
    op.create_table(
        "run_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("app.runs.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(80)),
        sa.CheckConstraint(
            "kind IN ('START','RESUME') AND status IN ('QUEUED','RUNNING','DONE','FAILED')",
            name="job_enums",
        ),
        schema="app",
    )
    op.create_index(
        "one_active_job_per_run",
        "run_jobs",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"),
        schema="app",
    )
    op.create_table(
        "approvals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id", UUID(as_uuid=True), sa.ForeignKey("app.runs.id"), unique=True, nullable=False
        ),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("plan_json", JSONB, nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(500)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('PENDING','APPROVED','REJECTED','EXPIRED')", name="approval_decision"
        ),
        schema="app",
    )
    op.create_foreign_key(
        "run_approval_fk",
        "runs",
        "approvals",
        ["approval_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
    )
    op.create_table(
        "operations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "origin_run_id", UUID(as_uuid=True), sa.ForeignKey("app.runs.id"), nullable=False
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(240), unique=True, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("post_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_json", JSONB),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('PREPARED','UNKNOWN','CONFIRMED','FAILED') AND post_attempts BETWEEN 0 AND 2",
            name="operation_state",
        ),
        schema="app",
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("app.runs.id"), nullable=False),
        sa.Column("logical_call_id", sa.String(40), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(40), nullable=False),
        sa.Column("record_json", JSONB, nullable=False),
        sa.UniqueConstraint("run_id", "logical_call_id", "attempt_no", name="tool_attempt_key"),
        schema="app",
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("app.runs.id"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("dedupe_key", sa.String(160)),
        sa.UniqueConstraint("run_id", "dedupe_key", name="event_dedupe"),
        schema="app",
    )
    op.create_index("ix_run_events_cursor", "run_events", ["run_id", "id"], schema="app")
    # Prevent application code from accidentally editing an already approved plan.
    op.execute("""CREATE FUNCTION app.prevent_plan_update() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.plan_json IS DISTINCT FROM OLD.plan_json OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash
         OR NEW.expires_at IS DISTINCT FROM OLD.expires_at OR NEW.run_id IS DISTINCT FROM OLD.run_id
         OR NEW.plan_version IS DISTINCT FROM OLD.plan_version THEN
        RAISE EXCEPTION 'immutable approval plan';
      END IF;
      RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER immutable_approval BEFORE UPDATE ON app.approvals FOR EACH ROW EXECUTE FUNCTION app.prevent_plan_update()"
    )


def downgrade():
    op.drop_constraint("run_approval_fk", "runs", schema="app", type_="foreignkey")
    for table in ("run_events", "tool_calls", "operations", "approvals", "run_jobs", "runs"):
        op.drop_table(table, schema="app")
    op.execute("DROP FUNCTION app.prevent_plan_update()")
