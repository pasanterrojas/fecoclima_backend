"""Parcel intelligence, provider selection, conversations and alert outbox.

Revision ID: 0006_plot_intelligence
Revises: 0005_telegram_subscribers
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_plot_intelligence"
down_revision = "0005_telegram_subscribers"
branch_labels = None
depends_on = None


def _timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]


def _id():
    return sa.Column("id", sa.UUID(), primary_key=True)


def _fk(name, target, delete="CASCADE", nullable=False):
    return sa.Column(name, sa.UUID(), sa.ForeignKey(target, ondelete=delete), nullable=nullable, index=True)


def upgrade() -> None:
    # 0001 historically creates current metadata. Guard additions so both a
    # clean installation and an upgrade from 0005 work without data loss.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    def add(table, column):
        if column.name not in {item["name"] for item in inspector.get_columns(table)}:
            op.add_column(table, column)
    add("stations", sa.Column("endpoint_url", sa.String(1000), nullable=True))
    add("plots", sa.Column("planting_date", sa.Date(), nullable=True))
    op.alter_column("plots", "station_id", existing_type=sa.UUID(), nullable=True)
    add("ai_agent_versions", sa.Column("provider", sa.String(20), nullable=False, server_default="OPENAI"))
    add("ai_agent_versions", sa.Column("html_template", sa.Text(), nullable=True))
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    if "plot_stations" not in tables:
        op.create_table("plot_stations", _id(), _fk("plot_id", "plots.id"), _fk("station_id", "stations.id"),
                        sa.Column("coverage_pct", sa.Float(), nullable=False),
                        sa.Column("distance_m", sa.Float(), nullable=False), *_timestamps(),
                        sa.UniqueConstraint("plot_id", "station_id", name="uq_plot_station"))
    if "plot_analyses" not in tables:
        op.create_table("plot_analyses", _id(), _fk("plot_id", "plots.id"),
                        sa.Column("reference_at", sa.DateTime(timezone=True), nullable=False),
                        sa.Column("context", json_type, nullable=False),
                        sa.Column("agent_results", json_type, nullable=False),
                        _fk("created_by", "users.id", "SET NULL", True), *_timestamps())
    if "plot_conversations" not in tables:
        op.create_table("plot_conversations", _id(), _fk("analysis_id", "plot_analyses.id"),
                        _fk("user_id", "users.id"), sa.Column("messages", json_type, nullable=False),
                        *_timestamps(), sa.UniqueConstraint("analysis_id", "user_id", name="uq_plot_conversation_user"))
    if "plot_alert_rules" not in tables:
        op.create_table("plot_alert_rules", _id(), _fk("plot_id", "plots.id"),
                        sa.Column("natural_language", sa.Text(), nullable=False),
                        sa.Column("expression", json_type, nullable=False),
                        sa.Column("channel", sa.String(20), nullable=False),
                        sa.Column("recipient", sa.String(255), nullable=False),
                        sa.Column("cooldown_minutes", sa.Integer(), nullable=False),
                        sa.Column("last_triggered_at", sa.DateTime(timezone=True)),
                        sa.Column("active", sa.Boolean(), nullable=False, index=True),
                        sa.Column("deleted_at", sa.DateTime(timezone=True)),
                        _fk("created_by", "users.id", "SET NULL", True), *_timestamps())
    if "plot_alert_deliveries" not in tables:
        op.create_table("plot_alert_deliveries", _id(), _fk("rule_id", "plot_alert_rules.id"),
                        _fk("analysis_id", "plot_analyses.id"),
                        sa.Column("status", sa.String(20), nullable=False),
                        sa.Column("attempts", sa.Integer(), nullable=False),
                        sa.Column("error", sa.Text()), sa.Column("sent_at", sa.DateTime(timezone=True)),
                        *_timestamps(), sa.UniqueConstraint("rule_id", "analysis_id", name="uq_plot_alert_delivery"))
    if "plot_reports" not in tables:
        op.create_table("plot_reports", _id(), _fk("analysis_id", "plot_analyses.id"),
                        _fk("created_by", "users.id"), sa.Column("status", sa.String(20), nullable=False),
                        sa.Column("storage_path", sa.String(500)), sa.Column("error", sa.Text()), *_timestamps())


def downgrade() -> None:
    for name in ("plot_reports", "plot_alert_deliveries", "plot_alert_rules", "plot_conversations",
                 "plot_analyses", "plot_stations"):
        op.drop_table(name)
    op.drop_column("ai_agent_versions", "html_template")
    op.drop_column("ai_agent_versions", "provider")
    op.drop_column("plots", "planting_date")
    op.drop_column("stations", "endpoint_url")
    # Keep nullable station_id: uncovered parcels cannot be mapped safely to an
    # arbitrary station during rollback.
