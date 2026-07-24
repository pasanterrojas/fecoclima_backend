"""Replace crop-cycle analysis context with planting date.

Revision ID: 0002_planting_date_context
Revises: 0001_initial
Create Date: 2026-07-23

The initial project migration builds from current SQLAlchemy metadata. For that
reason this migration is deliberately idempotent: it works both on an existing
0001 database and on a clean installation where 0001 already created the new
columns.
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_planting_date_context"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _column(table: str, name: str):
    return next((item for item in _inspector().get_columns(table) if item["name"] == name), None)


def _constraint_names(table: str) -> set[str]:
    return {item["name"] for item in _inspector().get_unique_constraints(table) if item.get("name")}


def _index_names(table: str) -> set[str]:
    return {item["name"] for item in _inspector().get_indexes(table) if item.get("name")}


def upgrade() -> None:
    # A parcel is a geographic unit. Crop is optional so the same geometry can
    # be reused for crop rotation while station remains the spatial reference.
    crop_column = _column("plots", "crop_id")
    if crop_column and not crop_column.get("nullable", True):
        op.alter_column("plots", "crop_id", existing_type=sa.UUID(), nullable=True)

    crop_fk = next(
        (fk for fk in _inspector().get_foreign_keys("plots") if fk.get("constrained_columns") == ["crop_id"]),
        None,
    )
    if crop_fk and (crop_fk.get("options") or {}).get("ondelete", "").upper() != "SET NULL":
        if crop_fk.get("name"):
            op.drop_constraint(crop_fk["name"], "plots", type_="foreignkey")
        op.create_foreign_key(
            "plots_crop_id_fkey", "plots", "crops", ["crop_id"], ["id"], ondelete="SET NULL"
        )

    if not _column("alert_evaluations", "planting_date"):
        op.add_column("alert_evaluations", sa.Column("planting_date", sa.Date(), nullable=True))
    if "ix_alert_evaluations_planting_date" not in _index_names("alert_evaluations"):
        op.create_index(
            "ix_alert_evaluations_planting_date", "alert_evaluations", ["planting_date"]
        )

    if not _column("reports", "planting_date"):
        op.add_column("reports", sa.Column("planting_date", sa.Date(), nullable=True))
    if "ix_reports_planting_date" not in _index_names("reports"):
        op.create_index("ix_reports_planting_date", "reports", ["planting_date"])

    # Preserve context from legacy cycle-linked records.
    op.execute(
        """
        UPDATE alert_evaluations AS ae
        SET planting_date = cc.planting_date
        FROM crop_cycles AS cc
        WHERE ae.crop_cycle_id = cc.id
          AND ae.planting_date IS NULL
        """
    )
    op.execute(
        """
        UPDATE reports AS r
        SET planting_date = cc.planting_date
        FROM crop_cycles AS cc
        WHERE r.crop_cycle_id = cc.id
          AND r.planting_date IS NULL
        """
    )

    constraints = _constraint_names("alert_evaluations")
    if "uq_alert_context_date" in constraints:
        op.drop_constraint("uq_alert_context_date", "alert_evaluations", type_="unique")
    if "uq_alert_context_planting_date" not in _constraint_names("alert_evaluations"):
        op.create_unique_constraint(
            "uq_alert_context_planting_date",
            "alert_evaluations",
            ["station_id", "crop_id", "planting_date", "evaluation_date"],
        )


def downgrade() -> None:
    constraints = _constraint_names("alert_evaluations")
    if "uq_alert_context_planting_date" in constraints:
        op.drop_constraint("uq_alert_context_planting_date", "alert_evaluations", type_="unique")
    if "uq_alert_context_date" not in _constraint_names("alert_evaluations"):
        op.create_unique_constraint(
            "uq_alert_context_date",
            "alert_evaluations",
            ["station_id", "crop_id", "crop_cycle_id", "evaluation_date"],
        )
    if "ix_reports_planting_date" in _index_names("reports"):
        op.drop_index("ix_reports_planting_date", table_name="reports")
    if _column("reports", "planting_date"):
        op.drop_column("reports", "planting_date")
    if "ix_alert_evaluations_planting_date" in _index_names("alert_evaluations"):
        op.drop_index("ix_alert_evaluations_planting_date", table_name="alert_evaluations")
    if _column("alert_evaluations", "planting_date"):
        op.drop_column("alert_evaluations", "planting_date")
