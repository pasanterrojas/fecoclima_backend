"""Durable Telegram subscriptions for producer alerts.

Revision ID: 0005_telegram_subscribers
Revises: 0004_grounding_polygon_knowledge
Create Date: 2026-07-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_telegram_subscribers"
down_revision = "0004_grounding_polygon_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "telegram_subscribers" in inspector.get_table_names():
        return
    op.create_table(
        "telegram_subscribers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_id", sa.String(length=80), nullable=False),
        sa.Column("chat_type", sa.String(length=30), nullable=True),
        sa.Column("title", sa.String(length=220), nullable=True),
        sa.Column("username", sa.String(length=120), nullable=True),
        sa.Column("first_name", sa.String(length=120), nullable=True),
        sa.Column("last_name", sa.String(length=120), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("minimum_level", sa.String(length=30), nullable=False, server_default="MODERADA"),
        sa.Column("subscribed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_update_id", sa.BigInteger(), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["integration_id"], ["integration_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("integration_id", "chat_id", name="uq_telegram_subscriber_chat"),
    )
    op.create_index("ix_telegram_subscribers_integration_id", "telegram_subscribers", ["integration_id"])
    op.create_index("ix_telegram_subscribers_active", "telegram_subscribers", ["active"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "telegram_subscribers" not in inspector.get_table_names():
        return
    indexes = {item["name"] for item in inspector.get_indexes("telegram_subscribers")}
    if "ix_telegram_subscribers_active" in indexes:
        op.drop_index("ix_telegram_subscribers_active", table_name="telegram_subscribers")
    if "ix_telegram_subscribers_integration_id" in indexes:
        op.drop_index("ix_telegram_subscribers_integration_id", table_name="telegram_subscribers")
    op.drop_table("telegram_subscribers")
