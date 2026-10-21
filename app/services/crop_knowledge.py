"""Crop-owned knowledge, inherited by all its specialized agents."""
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIAgent, AgentKnowledgeDocument, KnowledgeDocument


async def sync_crop_knowledge(db: AsyncSession, crop_id: UUID | None, agent_id: UUID | None = None) -> int:
    query = select(AIAgent).where(AIAgent.crop_id == crop_id, AIAgent.deleted_at.is_(None))
    if agent_id:
        query = query.where(AIAgent.id == agent_id)
    agents = list((await db.scalars(query)).all())
    document_ids = set((await db.scalars(select(KnowledgeDocument.id).where(
        KnowledgeDocument.crop_id == crop_id, KnowledgeDocument.active.is_(True), KnowledgeDocument.deleted_at.is_(None)
    ))).all()) if crop_id else set()
    for agent in agents:
        # Remove obsolete crop links when the agent's crop was changed. Global
        # reference documents explicitly linked in previous versions are retained.
        await db.execute(delete(AgentKnowledgeDocument).where(
            AgentKnowledgeDocument.agent_id == agent.id,
            AgentKnowledgeDocument.document_id.in_(select(KnowledgeDocument.id).where(
                KnowledgeDocument.crop_id.is_not(None), KnowledgeDocument.crop_id != crop_id
            ))
        ))
        existing = set((await db.scalars(select(AgentKnowledgeDocument.document_id).where(AgentKnowledgeDocument.agent_id == agent.id))).all())
        for document_id in document_ids - existing:
            db.add(AgentKnowledgeDocument(agent_id=agent.id, document_id=document_id))
    await db.flush()
    return len(document_ids)
