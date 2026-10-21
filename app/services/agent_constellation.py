"""A small thematic constellation, with a single agronomic expert per crop."""
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIAgent, AIAgentVersion, Crop
from app.services.agent_defaults import SHARED_PARCEL_PURPOSES, build_default_agent
from app.services.crop_knowledge import sync_crop_knowledge


async def ensure_shared_agents(db: AsyncSession) -> int:
    created = 0
    for purpose in SHARED_PARCEL_PURPOSES:
        existing = await db.scalar(select(AIAgent).where(AIAgent.purpose == purpose, AIAgent.crop_id.is_(None), AIAgent.deleted_at.is_(None)))
        if existing:
            continue
        template = await db.scalar(select(AIAgentVersion).join(AIAgent, AIAgent.id == AIAgentVersion.agent_id).where(
            AIAgent.purpose == purpose, AIAgent.deleted_at.is_(None), AIAgent.active.is_(True), AIAgentVersion.version == AIAgent.current_version
        ).order_by(AIAgent.updated_at.desc()).limit(1))
        agent, version = build_default_agent(Crop(code="GENERAL", name="todos los cultivos"), purpose=purpose)
        agent.crop_id = None
        agent.name = {"WEATHER_ANALYSIS":"Datos y predicción", "SATELLITE_ANALYSIS":"GIS y satélites", "ORCHESTRATOR":"Síntesis de parcela", "CROP_CHAT":"Chat contextual"}[purpose]
        if template:
            for key in ("provider", "model", "temperature", "max_tokens", "output_schema"):
                setattr(version, key, getattr(template, key))
        db.add_all([agent, version])
        created += 1
    await db.flush()
    return created


async def compact_generated_agents(db: AsyncSession) -> dict:
    created = await ensure_shared_agents(db)
    crops = list((await db.scalars(select(Crop).where(Crop.deleted_at.is_(None)))).all())
    crop_codes = {crop.id: crop.code for crop in crops}
    retired = []
    agents = list((await db.scalars(select(AIAgent).where(AIAgent.deleted_at.is_(None), AIAgent.crop_id.is_not(None)))).all())
    obsolete = set(SHARED_PARCEL_PURPOSES) | {"ALERT_ANALYSIS", "DISEASE_ANALYSIS", "PHENOLOGY_ANALYSIS", "FORECAST_ANALYSIS", "YEAR_COMPARISON", "PDF_REPORT", "CROP_ANALYSIS"}
    for agent in agents:
        # Only generated redundant agents are archived. User-created specialist
        # configurations remain available; runs and versions are never erased.
        if agent.purpose in obsolete and agent.code.startswith(f"AGENT_{crop_codes.get(agent.crop_id, 'NONE')}_"):
            agent.active = False
            agent.deleted_at = datetime.now(UTC)
            retired.append(agent.code)
    for crop in crops:
        await sync_crop_knowledge(db, crop.id)
    return {"shared_created": created, "archived": retired}
