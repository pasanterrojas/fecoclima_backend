"""Direcciones con doble consentimiento y notificaciones por condiciones verificables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0008_optin_notification_events"
down_revision = "0007_parcel_identity"
branch_labels = None
depends_on = None


def upgrade():
    # Revision 0001 creates current ORM metadata for fresh installations. Keep
    # this incremental revision safe for both existing and empty databases.
    def create_table_if_missing(name, *columns):
        if not sa.inspect(op.get_bind()).has_table(name):
            op.create_table(name, *columns)

    def create_index_if_missing(name, table, columns):
        indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
        if name not in indexes:
            op.create_index(name, table, columns)

    create_table_if_missing("email_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("crop_id", UUID(as_uuid=True), sa.ForeignKey("crops.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("station_id", UUID(as_uuid=True), sa.ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("confirm_hash", sa.String(64)),
        sa.Column("confirm_expires_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True)),
        sa.Column("manage_secret", sa.String(64), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True)),
        sa.Column("telegram_chat_id", sa.String(80)),
        sa.Column("telegram_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("email", "crop_id", "station_id", name="uq_email_subscription_scope"),
    )
    for col in ("email", "crop_id", "station_id", "status", "confirm_hash", "telegram_chat_id"):
        create_index_if_missing(f"ix_email_subscriptions_{col}", "email_subscriptions", [col])
    create_table_if_missing("notification_request_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ip_digest", sa.String(64), nullable=False),
        sa.Column("email_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for col in ("ip_digest", "email_digest"):
        create_index_if_missing(f"ix_notification_request_logs_{col}", "notification_request_logs", [col])
    create_table_if_missing("notification_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("compiled_rule", JSONB, nullable=False),
        sa.Column("crop_id", UUID(as_uuid=True), sa.ForeignKey("crops.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("station_id", UUID(as_uuid=True), sa.ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False),
        sa.Column("cadence", sa.String(30), nullable=False),
        sa.Column("every_minutes", sa.Integer(), nullable=False),
        sa.Column("schedule_hour", sa.Integer(), nullable=False),
        sa.Column("schedule_day", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True)),
        sa.Column("last_match", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("trigger_number", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_status", sa.String(30)),
        sa.Column("last_detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for col in ("crop_id", "station_id", "active"):
        create_index_if_missing(f"ix_notification_events_{col}", "notification_events", [col])
    create_index_if_missing("ix_notify_event_active_station", "notification_events", ["active", "station_id"])
    create_table_if_missing("notification_event_deliveries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", UUID(as_uuid=True), sa.ForeignKey("notification_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", UUID(as_uuid=True), sa.ForeignKey("email_subscriptions.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("destination_hint", sa.String(255), nullable=False),
        sa.Column("trigger_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_id", "subscription_id", "trigger_number", "channel", name="uq_notification_delivery_once"),
    )
    for col in ("event_id", "subscription_id"):
        create_index_if_missing(f"ix_notification_event_deliveries_{col}", "notification_event_deliveries", [col])


def downgrade():
    op.drop_table("notification_event_deliveries")
    op.drop_table("notification_events")
    op.drop_table("notification_request_logs")
    op.drop_table("email_subscriptions")
