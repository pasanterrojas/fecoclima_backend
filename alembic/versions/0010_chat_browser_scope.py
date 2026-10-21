"""Isolate parcel conversations by browser without exposing unscoped history.

Existing conversations retain their data with a NULL browser identifier and are
not returned to a newly scoped browser. No legacy history is assigned by guess.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0010_chat_browser_scope"
down_revision = "0009_crop_planting_months"
branch_labels = None
depends_on = None


def upgrade():
    # Revision 0001 uses the current ORM metadata on fresh installations.
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("plot_conversations")}
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("plot_conversations")}
    if "browser_session_id" not in columns:
        op.add_column("plot_conversations", sa.Column("browser_session_id", UUID(as_uuid=True), nullable=True))
    if "uq_plot_conversation_user" in constraints:
        op.drop_constraint("uq_plot_conversation_user", "plot_conversations", type_="unique")
    if "uq_plot_conversation_browser" not in constraints:
        op.create_unique_constraint("uq_plot_conversation_browser", "plot_conversations", ["analysis_id", "user_id", "browser_session_id"])


def downgrade():
    # Multiple browser histories cannot safely collapse back to one row. A
    # failing uniqueness check rolls the transaction back, preserving all data.
    op.create_unique_constraint("uq_plot_conversation_user", "plot_conversations", ["analysis_id", "user_id"])
    op.drop_constraint("uq_plot_conversation_browser", "plot_conversations", type_="unique")
    op.drop_column("plot_conversations", "browser_session_id")
