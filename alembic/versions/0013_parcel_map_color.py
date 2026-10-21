"""Persist each parcel's map color without changing its geometry or identity."""
from alembic import op
import sqlalchemy as sa

revision = "0013_parcel_map_color"
down_revision = "0012_soil_unit_criteria"
branch_labels = None
depends_on = None


def upgrade():
    # The initial migration creates current metadata on a fresh installation.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("plots")}
    if "map_color" not in columns:
        op.add_column("plots", sa.Column("map_color", sa.String(7), nullable=False, server_default="#22a680"))


def downgrade():
    op.drop_column("plots", "map_color")
