"""Seasonal and single-date notification schedules."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_event_calendar"
down_revision = "0010_chat_browser_scope"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("notification_events")}
    if "schedule_months" not in columns:
        op.add_column("notification_events", sa.Column("schedule_months", JSONB, nullable=False, server_default="[]"))
    if "schedule_date" not in columns:
        op.add_column("notification_events", sa.Column("schedule_date", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("notification_events", "schedule_date")
    op.drop_column("notification_events", "schedule_months")
