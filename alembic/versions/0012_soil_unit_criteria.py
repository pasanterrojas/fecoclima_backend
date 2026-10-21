"""Independent percentage and tension criteria within one crop condition."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_soil_unit_criteria"
down_revision = "0011_event_calendar"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("base_conditions")}
    if "soil_alternative" not in columns:
        op.add_column("base_conditions", sa.Column("soil_alternative", JSONB, nullable=True))


def downgrade():
    op.drop_column("base_conditions", "soil_alternative")
