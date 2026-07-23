from __future__ import annotations

import uuid
from datetime import UTC, datetime
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import (
    AIAgent, AIAgentVersion, Crop, CropCycle, CropRuleSet, CropRuleVersion,
    PhenologyHistory, Plot, Station,
)
from app.schemas.resources import AgentPayload, CropCyclePayload, CropPayload, PlotPayload, RulePayload
from app.services.audit import write_audit
from app.services.serialization import model_dict

router = APIRouter(tags=["Recursos agronómicos"])


def _not_found(label: str):
    raise HTTPException(status_code=404, detail=f"{label} no encontrado")


async def _audit_commit(db, actor, request, action, entity_type, entity_id, before=None, after=None):
    await write_audit(db, action=action, user=actor, request=request, entity_type=entity_type, entity_id=str(entity_id), before=before, after=after)
    await db.commit()


@router.get("/crops")
async def list_crops(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(Crop).where(Crop.deleted_at.is_(None)).order_by(Crop.name).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [model_dict(row, exclude={"deleted_at"}) for row in rows]}


@router.post("/crops")
async def create_crop(payload: CropPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    code = payload.code.upper()
    if await db.scalar(select(Crop).where(Crop.code == code, Crop.deleted_at.is_(None))):
        raise HTTPException(status_code=409, detail="El código de cultivo ya existe")
    crop = Crop(**payload.model_dump(exclude={"code"}), code=code, rules_status="pending", created_by=actor.id)
    db.add(crop); await db.flush()
    after = model_dict(crop, exclude={"deleted_at"})
    await _audit_commit(db, actor, request, "CROP_CREATE", "crop", crop.id, after=after)
    return after


@router.put("/crops/{crop_id}")
async def update_crop(crop_id: uuid.UUID, payload: CropPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    crop = await db.get(Crop, crop_id)
    if not crop or crop.deleted_at: _not_found("Cultivo")
    before = model_dict(crop, exclude={"deleted_at"})
    for key, value in payload.model_dump().items(): setattr(crop, key, value.upper() if key == "code" else value)
    await db.flush()
    await db.refresh(crop)
    after = model_dict(crop, exclude={"deleted_at"})
    await _audit_commit(db, actor, request, "CROP_UPDATE", "crop", crop.id, before, after)
    return after


@router.delete("/crops/{crop_id}")
async def delete_crop(crop_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    crop = await db.get(Crop, crop_id)
    if not crop or crop.deleted_at: _not_found("Cultivo")
    crop.active = False; crop.deleted_at = datetime.now(UTC)
    await _audit_commit(db, actor, request, "CROP_DELETE", "crop", crop.id)
    return {"ok": True}


async def _plot_dict(db: DbSession, row: Plot) -> dict:
    crop, station = await db.get(Crop, row.crop_id), await db.get(Station, row.station_id)
    return {**model_dict(row, exclude={"deleted_at"}), "crop_name": crop.name if crop else None, "station_name": station.name if station else None}


@router.get("/plots")
async def list_plots(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(Plot).where(Plot.deleted_at.is_(None)).order_by(Plot.created_at.desc()).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _plot_dict(db, row) for row in rows]}


@router.post("/plots")
async def create_plot(payload: PlotPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if not await db.get(Crop, payload.crop_id) or not await db.get(Station, payload.station_id):
        raise HTTPException(status_code=400, detail="Cultivo o estación inválida")
    plot = Plot(**payload.model_dump(), created_by=actor.id); db.add(plot); await db.flush()
    after = await _plot_dict(db, plot); await _audit_commit(db, actor, request, "PLOT_CREATE", "plot", plot.id, after=after)
    return after


@router.put("/plots/{plot_id}")
async def update_plot(plot_id: uuid.UUID, payload: PlotPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    plot = await db.get(Plot, plot_id)
    if not plot or plot.deleted_at: _not_found("Parcela")
    before = await _plot_dict(db, plot)
    for key, value in payload.model_dump().items(): setattr(plot, key, value)
    await db.flush()
    await db.refresh(plot)
    after = await _plot_dict(db, plot)
    await _audit_commit(db, actor, request, "PLOT_UPDATE", "plot", plot.id, before, after)
    return after


@router.delete("/plots/{plot_id}")
async def delete_plot(plot_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    plot = await db.get(Plot, plot_id)
    if not plot or plot.deleted_at: _not_found("Parcela")
    plot.active = False; plot.deleted_at = datetime.now(UTC)
    await _audit_commit(db, actor, request, "PLOT_DELETE", "plot", plot.id)
    return {"ok": True}


async def _cycle_dict(db: DbSession, cycle: CropCycle) -> dict:
    crop, station = await db.get(Crop, cycle.crop_id), await db.get(Station, cycle.station_id)
    plot = await db.get(Plot, cycle.plot_id) if cycle.plot_id else None
    return {**model_dict(cycle), "crop_name": crop.name if crop else None, "crop_code": crop.code if crop else None, "station_name": station.name if station else None, "plot_name": plot.name if plot else None}


@router.get("/crop-cycles")
async def list_cycles(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(CropCycle).order_by(CropCycle.planting_date.desc()).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _cycle_dict(db, row) for row in rows]}


@router.post("/crop-cycles")
async def create_cycle(payload: CropCyclePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    cycle = CropCycle(**payload.model_dump(), created_by=actor.id); db.add(cycle); await db.flush()
    db.add(PhenologyHistory(crop_cycle_id=cycle.id, previous_stage=None, new_stage=cycle.current_stage or "PENDIENTE", method=cycle.stage_method, changed_by=actor.id))
    after = await _cycle_dict(db, cycle); await _audit_commit(db, actor, request, "CYCLE_CREATE", "crop_cycle", cycle.id, after=after)
    return after


@router.put("/crop-cycles/{cycle_id}")
async def update_cycle(cycle_id: uuid.UUID, payload: CropCyclePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    cycle = await db.get(CropCycle, cycle_id)
    if not cycle: _not_found("Ciclo")
    before = await _cycle_dict(db, cycle); previous_stage = cycle.current_stage
    for key, value in payload.model_dump().items(): setattr(cycle, key, value)
    if previous_stage != cycle.current_stage and cycle.current_stage:
        db.add(PhenologyHistory(crop_cycle_id=cycle.id, previous_stage=previous_stage, new_stage=cycle.current_stage, method=cycle.stage_method, changed_by=actor.id))
    await db.flush()
    await db.refresh(cycle)
    after = await _cycle_dict(db, cycle)
    await _audit_commit(db, actor, request, "CYCLE_UPDATE", "crop_cycle", cycle.id, before, after)
    return after


@router.delete("/crop-cycles/{cycle_id}")
async def delete_cycle(cycle_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    cycle = await db.get(CropCycle, cycle_id)
    if not cycle: _not_found("Ciclo")
    cycle.status = "INACTIVE"
    await _audit_commit(db, actor, request, "CYCLE_DISABLE", "crop_cycle", cycle.id)
    return {"ok": True}


async def _rule_dict(db: DbSession, rule: CropRuleVersion) -> dict:
    crop = await db.get(Crop, rule.crop_id)
    return {**model_dict(rule), "crop_name": crop.name if crop else None}


@router.get("/rules")
async def list_rules(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(CropRuleVersion).order_by(CropRuleVersion.created_at.desc()).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _rule_dict(db, row) for row in rows]}


@router.post("/rules")
async def create_rule(payload: RulePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if await db.scalar(select(CropRuleVersion).where(CropRuleVersion.crop_id == payload.crop_id, CropRuleVersion.version == payload.version)):
        raise HTTPException(status_code=409, detail="La versión ya existe para el cultivo")
    rule_set = await db.scalar(select(CropRuleSet).where(CropRuleSet.crop_id == payload.crop_id, CropRuleSet.active.is_(True)))
    if not rule_set:
        rule_set = CropRuleSet(crop_id=payload.crop_id, name=f"Reglas {payload.name}", description=payload.description, created_by=actor.id); db.add(rule_set); await db.flush()
    status = payload.status.upper()
    if status == "PUBLISHED":
        raise HTTPException(status_code=400, detail="Cree la regla como borrador y publíquela con el endpoint /rules/{id}/publish")
    rule = CropRuleVersion(rule_set_id=rule_set.id, crop_id=payload.crop_id, name=payload.name, description=payload.description, version=payload.version, effective_from=payload.effective_from, status=status, configuration=payload.configuration, author_id=actor.id)
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    after = await _rule_dict(db, rule)
    await _audit_commit(db, actor, request, "RULE_CREATE", "crop_rule_version", rule.id, after=after)
    return after


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: uuid.UUID, payload: RulePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule: _not_found("Regla")
    if rule.status == "PUBLISHED": raise HTTPException(status_code=409, detail="Una versión publicada es inmutable; cree una nueva versión")
    before = await _rule_dict(db, rule)
    for key in ["crop_id", "name", "description", "version", "effective_from", "configuration"]: setattr(rule, key, getattr(payload, key))
    rule.status = payload.status.upper() if payload.status.upper() != "PUBLISHED" else "DRAFT"
    await db.flush()
    await db.refresh(rule)
    after = await _rule_dict(db, rule)
    await _audit_commit(db, actor, request, "RULE_UPDATE", "crop_rule_version", rule.id, before, after)
    return after


@router.post("/rules/{rule_id}/publish")
async def publish_rule(rule_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule: _not_found("Regla")
    from app.services.rule_validation import validate_rule_version
    validation = await validate_rule_version(db, rule.id)
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail={"message": "La versión no puede publicarse", **validation})
    active = (await db.scalars(select(CropRuleVersion).where(CropRuleVersion.crop_id == rule.crop_id, CropRuleVersion.status == "PUBLISHED", CropRuleVersion.id != rule.id))).all()
    for item in active: item.status = "INACTIVE"; item.effective_to = rule.effective_from
    rule.status = "PUBLISHED"; rule.approved_by = actor.id; rule.approved_at = datetime.now(UTC)
    crop = await db.get(Crop, rule.crop_id); crop.rules_status = "active"
    await _audit_commit(db, actor, request, "RULE_PUBLISH", "crop_rule_version", rule.id, after=await _rule_dict(db, rule))
    return await _rule_dict(db, rule)


@router.delete("/rules/{rule_id}")
async def disable_rule(rule_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule: _not_found("Regla")
    rule.status = "INACTIVE"; rule.effective_to = datetime.now(UTC).date()
    await _audit_commit(db, actor, request, "RULE_DISABLE", "crop_rule_version", rule.id)
    return {"ok": True}


async def _agent_dict(db: DbSession, agent: AIAgent) -> dict:
    crop = await db.get(Crop, agent.crop_id) if agent.crop_id else None
    version = await db.scalar(select(AIAgentVersion).where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True)).order_by(AIAgentVersion.created_at.desc()).limit(1))
    result = {**model_dict(agent, exclude={"deleted_at"}), "crop_name": crop.name if crop else None, "version": version.version if version else None}
    if version:
        result.update({"system_prompt": version.system_prompt, "model": version.model, "temperature": version.temperature, "max_tokens": version.max_tokens, "output_schema": version.output_schema, "rag_collection": version.rag_collection})
    return result


@router.get("/agents")
async def list_agents(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(AIAgent).where(AIAgent.deleted_at.is_(None)).order_by(AIAgent.name).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _agent_dict(db, row) for row in rows]}


@router.post("/agents")
async def create_agent(payload: AgentPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if await db.scalar(select(AIAgent).where(AIAgent.code == payload.code.upper(), AIAgent.deleted_at.is_(None))):
        raise HTTPException(status_code=409, detail="El código de agente ya existe")
    agent = AIAgent(name=payload.name, code=payload.code.upper(), crop_id=payload.crop_id, purpose=payload.purpose, description=payload.description, current_version="1.0.0", active=payload.active, created_by=actor.id)
    db.add(agent); await db.flush()
    db.add(AIAgentVersion(agent_id=agent.id, version="1.0.0", system_prompt=payload.system_prompt or "Explica el resultado determinístico sin modificarlo.", model=payload.model, temperature=payload.temperature, max_tokens=payload.max_tokens, output_schema=payload.output_schema, rag_collection=payload.rag_collection, active=payload.active))
    await db.flush()
    await db.refresh(agent)
    after = await _agent_dict(db, agent)
    await _audit_commit(db, actor, request, "AGENT_CREATE", "ai_agent", agent.id, after=after)
    return after


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: uuid.UUID, payload: AgentPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    agent = await db.get(AIAgent, agent_id)
    if not agent or agent.deleted_at: _not_found("Agente")
    before = await _agent_dict(db, agent)
    for key in ["name", "crop_id", "purpose", "description", "active"]: setattr(agent, key, getattr(payload, key))
    agent.code = payload.code.upper()
    version = await db.scalar(select(AIAgentVersion).where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True)).order_by(AIAgentVersion.created_at.desc()).limit(1))
    if version:
        for key in ["system_prompt", "model", "temperature", "max_tokens", "output_schema", "rag_collection", "active"]: setattr(version, key, getattr(payload, key) if key != "active" else payload.active)
    await db.flush()
    await db.refresh(agent)
    after = await _agent_dict(db, agent)
    await _audit_commit(db, actor, request, "AGENT_UPDATE", "ai_agent", agent.id, before, after)
    return after


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    agent = await db.get(AIAgent, agent_id)
    if not agent or agent.deleted_at: _not_found("Agente")
    agent.active = False; agent.deleted_at = datetime.now(UTC)
    await _audit_commit(db, actor, request, "AGENT_DELETE", "ai_agent", agent.id)
    return {"ok": True}
