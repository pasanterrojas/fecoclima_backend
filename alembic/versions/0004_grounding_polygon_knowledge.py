"""Grounded agents, station polygons and per-agent knowledge.

Revision ID: 0004_grounding_polygon_knowledge
Revises: 0003_forecast_satellite
Create Date: 2026-07-24

The initial migration builds the current SQLAlchemy metadata on a clean
installation. This migration is therefore idempotent so it also works when the
new columns/table were already created by 0001_initial.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_grounding_polygon_knowledge"
down_revision = "0003_forecast_satellite"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return any(item["name"] == column for item in _inspector().get_columns(table))


def upgrade() -> None:
    # gen_random_uuid() is used by the association backfill below. Existing
    # deployments only enabled pgvector, so ensure pgcrypto is available.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    if not _has_column("stations", "influence_geojson"):
        op.add_column(
            "stations",
            sa.Column("influence_geojson", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )
    if not _has_column("stations", "influence_radius_m"):
        op.add_column("stations", sa.Column("influence_radius_m", sa.Integer(), nullable=True))
    op.execute("UPDATE stations SET influence_radius_m = 750 WHERE influence_radius_m IS NULL")

    if "agent_knowledge_documents" not in _inspector().get_table_names():
        op.create_table(
            "agent_knowledge_documents",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["ai_agents.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("agent_id", "document_id", name="uq_agent_knowledge_document"),
        )
        op.create_index("ix_agent_knowledge_documents_agent_id", "agent_knowledge_documents", ["agent_id"])
        op.create_index("ix_agent_knowledge_documents_document_id", "agent_knowledge_documents", ["document_id"])

    # Preserve prior behavior during the transition: existing crop documents
    # become linked to every existing agent of that crop. Administrators can
    # later unlink them individually from /admin/agentes.
    op.execute(
        """
        INSERT INTO agent_knowledge_documents (id, agent_id, document_id, created_by)
        SELECT gen_random_uuid(), a.id, d.id, a.created_by
        FROM ai_agents a
        JOIN knowledge_documents d ON d.crop_id = a.crop_id
        WHERE a.deleted_at IS NULL AND d.deleted_at IS NULL
        ON CONFLICT (agent_id, document_id) DO NOTHING
        """
    )


def downgrade() -> None:
    inspector = _inspector()
    if "agent_knowledge_documents" in inspector.get_table_names():
        index_names = {item["name"] for item in inspector.get_indexes("agent_knowledge_documents")}
        if "ix_agent_knowledge_documents_document_id" in index_names:
            op.drop_index("ix_agent_knowledge_documents_document_id", table_name="agent_knowledge_documents")
        if "ix_agent_knowledge_documents_agent_id" in index_names:
            op.drop_index("ix_agent_knowledge_documents_agent_id", table_name="agent_knowledge_documents")
        op.drop_table("agent_knowledge_documents")
    if _has_column("stations", "influence_radius_m"):
        op.drop_column("stations", "influence_radius_m")
    if _has_column("stations", "influence_geojson"):
        op.drop_column("stations", "influence_geojson")
