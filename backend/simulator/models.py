from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
)

metadata = MetaData(schema="sim")

orders = Table(
    "orders",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("payment_status", String(20), nullable=False),
    Column("fulfillment_status", String(20), nullable=False),
    Column("inventory_reserved", Boolean, nullable=False),
    Column("paid_at", DateTime(timezone=True)),
    Column("version", Integer, nullable=False),
)

tickets = Table(
    "tickets",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("subject", String(200), nullable=False),
    Column("body", String(2000), nullable=False),
    Column("bound_order_id", String(64)),
    Column("status", String(20), nullable=False),
    ForeignKeyConstraint(["tenant_id", "bound_order_id"], [orders.c.tenant_id, orders.c.id]),
)

fulfillment_events = Table(
    "fulfillment_events",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("order_id", String(64), nullable=False),
    Column("event_type", String(40), nullable=False),
    Column("error_code", String(80)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("operation_key", String(240)),
    ForeignKeyConstraint(["tenant_id", "order_id"], [orders.c.tenant_id, orders.c.id]),
)

idempotency_records = Table(
    "idempotency_records",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("idempotency_key", String(240), primary_key=True),
    Column("order_id", String(64), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("operation_type", String(40), nullable=False),
    Column("response_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["tenant_id", "order_id"], [orders.c.tenant_id, orders.c.id]),
)
