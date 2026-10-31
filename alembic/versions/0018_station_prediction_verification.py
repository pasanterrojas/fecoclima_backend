"""Daily pre-day predictions and sensor verification for future training."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018_station_predictions"
down_revision = "0017_station_parcel_audience"
branch_labels = None
depends_on = None


def upgrade():
    # The initial migration creates current ORM metadata on a fresh install.
    # Preserve that table (and any audit rows) when reaching this revision.
    if sa.inspect(op.get_bind()).has_table("station_forecast_verifications"):
        return
    op.create_table("station_forecast_verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("station_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False),
        sa.Column("method_revision", sa.String(80), nullable=False),
        sa.Column("predicted", postgresql.JSONB(), nullable=False),
        sa.Column("observed", postgresql.JSONB(), nullable=False),
        sa.Column("observation_coverage", postgresql.JSONB(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("station_id", "target_date", name="uq_station_prediction_day"))
    op.create_index("ix_station_forecast_verifications_station_id", "station_forecast_verifications", ["station_id"])
    op.create_index("ix_station_forecast_verifications_target_date", "station_forecast_verifications", ["target_date"])


def downgrade():
    op.drop_table("station_forecast_verifications")
