"""Allow public parcel conversations and reports without creating user accounts."""
from alembic import op
import sqlalchemy as sa

revision = "0014_public_parcel_browser"
down_revision = "0013_parcel_map_color"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("plot_conversations", "user_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("plot_reports", "created_by", existing_type=sa.UUID(), nullable=True)


def downgrade():
    # Keep public records intact. A rollback needs an explicit archival decision.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS(SELECT 1 FROM plot_conversations WHERE user_id IS NULL) OR EXISTS(SELECT 1 FROM plot_reports WHERE created_by IS NULL)")).scalar():
        raise RuntimeError("Archive anonymous conversations/reports before reverting public access")
    op.alter_column("plot_conversations", "user_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("plot_reports", "created_by", existing_type=sa.UUID(), nullable=False)
