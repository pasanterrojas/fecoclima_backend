"""Crop planting calendar without hard-coded crop restrictions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_crop_planting_months"
down_revision = "0008_optin_notification_events"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("crops")}
    if "planting_months" not in columns:
        op.add_column("crops", sa.Column("planting_months", JSONB, nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("crops", "planting_months")
