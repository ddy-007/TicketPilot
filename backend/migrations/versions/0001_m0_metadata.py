"""Create the minimal M0 application metadata table."""

import sqlalchemy as sa
from alembic import op

revision = "0001_m0_metadata"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.create_table(
        "m0_metadata",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.String(length=500), nullable=False),
        schema="app",
    )
    op.execute(
        "INSERT INTO app.m0_metadata (key, value) VALUES ('schema_version', 'm0') "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("m0_metadata", schema="app")
