from __future__ import annotations

import uuid
from datetime import UTC, datetime
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.core.config import settings
from app.db.models import (
    AIAgent, AIAgentVersion, AgentKnowledgeDocument, Crop, CropRuleSet, CropRuleVersion, KnowledgeDocument, Plot, Station,
)
from app.schemas.resources import AgentPayload, CropPayload, PlotPayload, RulePayload
from app.services.agent_defaults import (
    AGENT_PURPOSES,
    build_default_agent,
    default_agent_prompt,
    merge_agent_output_schema,
    normalize_response_policy,
)
from app.services.alert_engine import DEFAULT_ALERT_CONFIGURATION
from app.services.ai import list_available_chat_models, test_chat_model
from app.services.audit import write_audit
from app.services.serialization import model_dict

router = APIRouter(tags=["Recursos agronómicos"])


def _not_found(label: str):
    raise HTTPException(status_code=404, detail=f"{label} no encontrado")


async def _audit_commit(db, actor, request, action, entity_type, entity_id, before=None, after=None):
    await write_audit(db, action=action, user=actor, request=request, entity_type=entity_type, entity_id=str(entity_id), before=before, after=after)
    await db.commit()


def _response_policy_overrides(payload: AgentPayload) -> dict:
    return {
        "detail_level": payload.response_detail_level,
        "max_words": payload.response_max_words,
        "max_sections": payload.response_max_sections,
        "tone": payload.response_tone,
        "include_observed": payload.response_include_observed,
        "include_forecast": payload.response_include_forecast,
        "include_satellite": payload.response_include_satellite,
        "include_year_comparison": payload.response_include_year_comparison,
        "include_sources": payload.response_include_sources,
        "show_technical_values": payload.response_show_technical_values,
        "show_system_details": payload.response_show_system_details,
        "use_emojis": payload.response_use_emojis,
        "custom_instructions": payload.response_custom_instructions or "",
    }


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
    created_agents = []
    for purpose in AGENT_PURPOSES:
        agent, version = build_default_agent(crop, purpose=purpose, created_by=actor.id)
        db.add_all([agent, version])
        created_agents.append({"id": str(agent.id), "code": agent.code, "purpose": purpose})
    await db.flush()
    after = {**model_dict(crop, exclude={"deleted_at"}), "agents": created_agents}
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
    crop = await db.get(Crop, row.crop_id) if row.crop_id else None
    station = await db.get(Station, row.station_id)
    return {**model_dict(row, exclude={"deleted_at"}), "crop_name": crop.name if crop else None, "station_name": station.name if station else None}


@router.get("/plots")
async def list_plots(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(Plot).where(Plot.deleted_at.is_(None)).order_by(Plot.created_at.desc()).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _plot_dict(db, row) for row in rows]}


@router.post("/plots")
async def create_plot(payload: PlotPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if not await db.get(Station, payload.station_id):
        raise HTTPException(status_code=400, detail="Estación inválida")
    if payload.crop_id and not await db.get(Crop, payload.crop_id):
        raise HTTPException(status_code=400, detail="Cultivo inválido")
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


_CYCLE_RETIRED_MESSAGE = (
    "Los ciclos productivos fueron retirados del flujo operativo. "
    "Use estación, cultivo y fecha de siembra directamente en el portal."
)


@router.get("/crop-cycles", deprecated=True)
async def list_cycles_legacy(_: AnalystUser) -> dict:
    return {"items": [], "deprecated": True, "message": _CYCLE_RETIRED_MESSAGE}


@router.post("/crop-cycles", deprecated=True)
async def create_cycle_legacy(_: AdminUser) -> dict:
    raise HTTPException(status_code=410, detail=_CYCLE_RETIRED_MESSAGE)


@router.put("/crop-cycles/{cycle_id}", deprecated=True)
async def update_cycle_legacy(cycle_id: uuid.UUID, _: AdminUser) -> dict:
    del cycle_id
    raise HTTPException(status_code=410, detail=_CYCLE_RETIRED_MESSAGE)


@router.delete("/crop-cycles/{cycle_id}", deprecated=True)
async def delete_cycle_legacy(cycle_id: uuid.UUID, _: AdminUser) -> dict:
    del cycle_id
    raise HTTPException(status_code=410, detail=_CYCLE_RETIRED_MESSAGE)


def _rule_configuration(value: dict | None) -> dict:
    configuration = dict(value or {})
    if not isinstance(configuration.get("alert"), dict):
        configuration["alert"] = DEFAULT_ALERT_CONFIGURATION
    return configuration


async def _rule_dict(db: DbSession, rule: CropRuleVersion) -> dict:
    crop = await db.get(Crop, rule.crop_id)
    return {**model_dict(rule), "crop_name": crop.name if crop else None}


@router.get("/rules")
async def list_rules(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (
        await db.execute(
            select(CropRuleVersion, Crop.name.label("crop_name"))
            .join(Crop, Crop.id == CropRuleVersion.crop_id)
            .order_by(CropRuleVersion.created_at.desc())
            .offset(offset)
            .limit(min(limit, 250))
        )
    ).all()
    return {
        "items": [
            {**model_dict(rule), "crop_name": crop_name}
            for rule, crop_name in rows
        ]
    }


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
    rule = CropRuleVersion(rule_set_id=rule_set.id, crop_id=payload.crop_id, name=payload.name, description=payload.description, version=payload.version, effective_from=payload.effective_from, status=status, configuration=_rule_configuration(payload.configuration), author_id=actor.id)
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    after = await _rule_dict(db, rule)
    await _audit_commit(db, actor, request, "RULE_CREATE", "crop_rule_version", rule.id, after=after)
    return after


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: uuid.UUID, payload: RulePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule:
        _not_found("Regla")

    before = await _rule_dict(db, rule)
    published = rule.status == "PUBLISHED"

    if published:
        # En la interfaz operativa una regla publicada puede ajustar sus valores
        # sin obligar al administrador a crear otra versión. Se conservan el
        # cultivo, el identificador de versión y la fecha de vigencia para evitar
        # cambios estructurales accidentales; cada ajuste queda en auditoría.
        if payload.crop_id != rule.crop_id:
            raise HTTPException(status_code=409, detail="No se puede cambiar el cultivo de una regla publicada")
        if payload.version != rule.version:
            raise HTTPException(status_code=409, detail="No se puede cambiar el número de una regla publicada")
        rule.name = payload.name
        rule.description = payload.description
        rule.configuration = _rule_configuration(payload.configuration)
        rule.approved_by = actor.id
        rule.approved_at = datetime.now(UTC)
    else:
        for key in ["crop_id", "name", "description", "version", "effective_from"]:
            setattr(rule, key, getattr(payload, key))
        rule.configuration = _rule_configuration(payload.configuration)
        rule.status = payload.status.upper() if payload.status.upper() != "PUBLISHED" else "DRAFT"

    await db.flush()
    if published:
        from app.services.rule_validation import validate_rule_version

        validation = await validate_rule_version(db, rule.id)
        if not validation["valid"]:
            await db.rollback()
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Los cambios no se aplicaron porque dejarían inválida la regla publicada",
                    **validation,
                },
            )
    await db.refresh(rule)
    after = await _rule_dict(db, rule)
    await _audit_commit(
        db,
        actor,
        request,
        "RULE_PUBLISHED_UPDATE" if published else "RULE_UPDATE",
        "crop_rule_version",
        rule.id,
        before,
        after,
    )
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
async def delete_rule(rule_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule:
        _not_found("Regla")
    before = await _rule_dict(db, rule)
    crop_id = rule.crop_id
    await write_audit(
        db,
        action="RULE_DELETE",
        user=actor,
        request=request,
        entity_type="crop_rule_version",
        entity_id=str(rule.id),
        before=before,
    )
    await db.delete(rule)
    await db.flush()
    remaining_published = await db.scalar(
        select(CropRuleVersion.id).where(
            CropRuleVersion.crop_id == crop_id,
            CropRuleVersion.status == "PUBLISHED",
        ).limit(1)
    )
    crop = await db.get(Crop, crop_id)
    if crop and not remaining_published:
        crop.rules_status = "pending"
    await db.commit()
    return {"ok": True, "deleted": True}


async def _agent_dict(db: DbSession, agent: AIAgent) -> dict:
    crop = await db.get(Crop, agent.crop_id) if agent.crop_id else None
    version = await db.scalar(
        select(AIAgentVersion)
        .where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True))
        .order_by(AIAgentVersion.created_at.desc())
        .limit(1)
    )
    documents = list(
        (
            await db.scalars(
                select(KnowledgeDocument)
                .join(
                    AgentKnowledgeDocument,
                    AgentKnowledgeDocument.document_id == KnowledgeDocument.id,
                )
                .where(
                    AgentKnowledgeDocument.agent_id == agent.id,
                    KnowledgeDocument.active.is_(True),
                    KnowledgeDocument.deleted_at.is_(None),
                )
                .order_by(KnowledgeDocument.title)
            )
        ).all()
    )
    knowledge_documents = [
        {
            "id": str(document.id),
            "title": document.title,
            "filename": document.filename,
            "index_status": document.index_status,
            "index_error": document.index_error,
            "institution": document.institution,
            "year": document.year,
        }
        for document in documents
    ]
    result = {
        **model_dict(agent, exclude={"deleted_at"}),
        "crop_name": crop.name if crop else "General",
        "purpose_label": AGENT_PURPOSES.get(agent.purpose or "", agent.purpose),
        "version": version.version if version else None,
        "documents_count": len(documents),
        "knowledge_documents": knowledge_documents,
    }
    if version:
        response_policy = normalize_response_policy(version.output_schema, agent.purpose or "CROP_ANALYSIS")
        result.update({
            "system_prompt": version.system_prompt,
            "model": version.model,
            "temperature": version.temperature,
            "max_tokens": version.max_tokens,
            "output_schema": version.output_schema,
            "rag_collection": version.rag_collection,
            "response_detail_level": response_policy["detail_level"],
            "response_max_words": response_policy["max_words"],
            "response_max_sections": response_policy["max_sections"],
            "response_tone": response_policy["tone"],
            "response_include_observed": response_policy["include_observed"],
            "response_include_forecast": response_policy["include_forecast"],
            "response_include_satellite": response_policy["include_satellite"],
            "response_include_year_comparison": response_policy["include_year_comparison"],
            "response_include_sources": response_policy["include_sources"],
            "response_show_technical_values": response_policy["show_technical_values"],
            "response_show_system_details": response_policy["show_system_details"],
            "response_use_emojis": response_policy["use_emojis"],
            "response_custom_instructions": response_policy["custom_instructions"],
        })
    return result


@router.get("/agents")
async def list_agents(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(AIAgent).where(AIAgent.deleted_at.is_(None)).order_by(AIAgent.name).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _agent_dict(db, row) for row in rows]}


@router.get("/agents/model-options")
async def agent_model_options(_: AnalystUser) -> dict:
    """Modelos de texto visibles para la llave del proveedor configurado."""
    return await list_available_chat_models()


@router.post("/agents/{agent_id}/test")
async def test_agent(agent_id: uuid.UUID, db: DbSession, _: AdminUser) -> dict:
    agent = await db.get(AIAgent, agent_id)
    if not agent or agent.deleted_at:
        _not_found("Agente")
    version = await db.scalar(
        select(AIAgentVersion)
        .where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True))
        .order_by(AIAgentVersion.created_at.desc())
        .limit(1)
    )
    if not version:
        raise HTTPException(status_code=409, detail="El agente no tiene una versión activa")
    model = version.model or settings.ai_default_model
    if not model:
        raise HTTPException(status_code=409, detail="El agente no tiene un modelo configurado")
    try:
        result = await test_chat_model(
            model,
            temperature=version.temperature,
            max_tokens=min(max(version.max_tokens, 100), 500),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:4000]) from exc
    return {
        "status": "OK",
        "detail": f"El agente {agent.name} respondió correctamente con {model}.",
        "data": result,
    }


@router.post("/agents")
async def create_agent(payload: AgentPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    crop = await db.get(Crop, payload.crop_id) if payload.crop_id else None
    if payload.crop_id and (not crop or crop.deleted_at):
        raise HTTPException(status_code=400, detail="Cultivo inválido")
    existing_scope = await db.scalar(
        select(AIAgent).where(
            AIAgent.crop_id == payload.crop_id,
            AIAgent.purpose == payload.purpose,
            AIAgent.deleted_at.is_(None),
        )
    )
    if existing_scope:
        raise HTTPException(status_code=409, detail="Ya existe un agente para este cultivo y propósito")
    if await db.scalar(select(AIAgent).where(AIAgent.code == payload.code.upper(), AIAgent.deleted_at.is_(None))):
        raise HTTPException(status_code=409, detail="El código de agente ya existe")
    crop_name = crop.name if crop else "todos los cultivos"
    agent = AIAgent(
        name=payload.name,
        code=payload.code.upper(),
        crop_id=payload.crop_id,
        purpose=payload.purpose,
        description=payload.description,
        current_version="1.0.0",
        active=payload.active,
        created_by=actor.id,
    )
    db.add(agent)
    await db.flush()
    db.add(AIAgentVersion(
        agent_id=agent.id,
        version="1.0.0",
        system_prompt=payload.system_prompt or default_agent_prompt(crop_name, payload.purpose),
        model=payload.model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        output_schema=merge_agent_output_schema(
            payload.output_schema,
            payload.purpose,
            _response_policy_overrides(payload),
        ),
        rag_collection=payload.rag_collection or (crop.code if crop else "GENERAL"),
        active=payload.active,
    ))
    await db.flush()
    await db.refresh(agent)
    after = await _agent_dict(db, agent)
    await _audit_commit(db, actor, request, "AGENT_CREATE", "ai_agent", agent.id, after=after)
    return after


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: uuid.UUID, payload: AgentPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    agent = await db.get(AIAgent, agent_id)
    if not agent or agent.deleted_at:
        _not_found("Agente")
    before = await _agent_dict(db, agent)
    crop = await db.get(Crop, payload.crop_id) if payload.crop_id else None
    if payload.crop_id and (not crop or crop.deleted_at):
        raise HTTPException(status_code=400, detail="Cultivo inválido")
    other = await db.scalar(
        select(AIAgent).where(
            AIAgent.crop_id == payload.crop_id,
            AIAgent.purpose == payload.purpose,
            AIAgent.id != agent.id,
            AIAgent.deleted_at.is_(None),
        )
    )
    if other:
        raise HTTPException(status_code=409, detail="El cultivo ya tiene otro agente para ese propósito")
    duplicate_code = await db.scalar(
        select(AIAgent).where(
            AIAgent.code == payload.code.upper(),
            AIAgent.id != agent.id,
            AIAgent.deleted_at.is_(None),
        )
    )
    if duplicate_code:
        raise HTTPException(status_code=409, detail="El código de agente ya existe")
    incompatible_document = await db.scalar(
        select(KnowledgeDocument)
        .join(
            AgentKnowledgeDocument,
            AgentKnowledgeDocument.document_id == KnowledgeDocument.id,
        )
        .where(
            AgentKnowledgeDocument.agent_id == agent.id,
            KnowledgeDocument.crop_id.is_not(None),
            KnowledgeDocument.crop_id != payload.crop_id,
            KnowledgeDocument.deleted_at.is_(None),
        )
        .limit(1)
    )
    if incompatible_document:
        raise HTTPException(
            status_code=422,
            detail=(
                "Desvincule primero los documentos de otro cultivo antes de cambiar "
                "el cultivo del agente"
            ),
        )
    for key in ["name", "crop_id", "description", "active"]:
        setattr(agent, key, getattr(payload, key))
    agent.purpose = payload.purpose
    agent.code = payload.code.upper()
    version = await db.scalar(select(AIAgentVersion).where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True)).order_by(AIAgentVersion.created_at.desc()).limit(1))
    if version:
        version.system_prompt = payload.system_prompt or default_agent_prompt(crop.name if crop else "todos los cultivos", payload.purpose)
        version.model = payload.model
        version.temperature = payload.temperature
        version.max_tokens = payload.max_tokens
        version.output_schema = merge_agent_output_schema(
            version.output_schema,
            payload.purpose,
            _response_policy_overrides(payload),
        )
        version.rag_collection = payload.rag_collection or (crop.code if crop else "GENERAL")
        version.active = payload.active
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
