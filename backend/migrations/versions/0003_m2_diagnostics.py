"""Persist M2 read-only diagnostic results and bounded evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003_m2_diagnostics"
down_revision = "0002_simulator"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "diagnostic_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", JSONB),
        schema="app",
    )
    op.create_index(
        "ix_diagnostic_tenant_ticket", "diagnostic_runs", ["tenant_id", "ticket_id"], schema="app"
    )
    op.create_table(
        "diagnostic_evidence",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("record_json", JSONB, nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["app.diagnostic_runs.id"]),
        schema="app",
    )
    op.create_index("ix_diagnostic_evidence_run", "diagnostic_evidence", ["run_id"], schema="app")


def downgrade():
    op.drop_table("diagnostic_evidence", schema="app")
    op.drop_table("diagnostic_runs", schema="app")
