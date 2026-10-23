"""Create the isolated synthetic business schema with database constraints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_simulator"
down_revision = "0001_m0_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS sim")
    op.create_table(
        "orders",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("payment_status", sa.String(20), nullable=False),
        sa.Column("fulfillment_status", sa.String(20), nullable=False),
        sa.Column("inventory_reserved", sa.Boolean(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("payment_status IN ('PENDING','PAID','REFUNDED')", name="payment_enum"),
        sa.CheckConstraint(
            "fulfillment_status IN ('NOT_STARTED','FAILED','QUEUED','SHIPPED','CANCELLED')",
            name="fulfillment_enum",
        ),
        sa.CheckConstraint("version >= 1", name="positive_version"),
        schema="sim",
    )
    op.create_table(
        "tickets",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.String(2000), nullable=False),
        sa.Column("bound_order_id", sa.String(64)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bound_order_id"],
            ["sim.orders.tenant_id", "sim.orders.id"],
            name="ticket_order_tenant_fk",
        ),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="ticket_status_enum"),
        schema="sim",
    )
    op.create_table(
        "fulfillment_events",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation_key", sa.String(240)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["sim.orders.tenant_id", "sim.orders.id"],
            name="event_order_tenant_fk",
        ),
        sa.UniqueConstraint("tenant_id", "operation_key", name="one_event_per_operation"),
        schema="sim",
    )
    op.create_index(
        "ix_event_order_time",
        "fulfillment_events",
        ["tenant_id", "order_id", "created_at", "id"],
        schema="sim",
    )
    op.create_table(
        "idempotency_records",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("idempotency_key", sa.String(240), primary_key=True),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("operation_type", sa.String(40), nullable=False),
        sa.Column("response_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["sim.orders.tenant_id", "sim.orders.id"],
            name="operation_order_tenant_fk",
        ),
        schema="sim",
    )


def downgrade() -> None:
    for name in ("idempotency_records", "fulfillment_events", "tickets", "orders"):
        op.drop_table(name, schema="sim")
