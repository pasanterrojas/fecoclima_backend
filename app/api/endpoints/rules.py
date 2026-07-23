from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import BaseCondition, CropRuleVersion, DiseaseRule
from app.schemas.actions import BaseConditionPayload, DiseaseRulePayload, RuleSimulationPayload
from app.services.audit import write_audit
from app.services.rule_validation import compare_rule_versions, simulate_rule, validate_rule_version
from app.services.serialization import model_dict

router = APIRouter(prefix="/rules", tags=["Reglas configurables"])


@router.get("/{rule_id}/conditions")
async def list_conditions(rule_id: UUID, db: DbSession, _: AnalystUser) -> dict:
    rows = list((await db.scalars(select(BaseCondition).where(BaseCondition.rule_version_id == rule_id, BaseCondition.deleted_at.is_(None)).order_by(BaseCondition.order_index))).all())
    return {"items": [model_dict(row, exclude={"deleted_at"}) for row in rows]}


@router.post("/{rule_id}/conditions")
async def create_condition(rule_id: UUID, payload: BaseConditionPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Regla no encontrada")
    if rule.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Una versión publicada es inmutable")
    row = BaseCondition(rule_version_id=rule.id, **payload.model_dump())
    db.add(row)
    await db.flush()
    await write_audit(db, action="RULE_CONDITION_CREATE", user=actor, request=request, entity_type="base_condition", entity_id=str(row.id), after=model_dict(row))
    await db.commit()
    return model_dict(row, exclude={"deleted_at"})


@router.put("/{rule_id}/conditions/{condition_id}")
async def update_condition(rule_id: UUID, condition_id: UUID, payload: BaseConditionPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    row = await db.get(BaseCondition, condition_id)
    if not rule or not row or row.rule_version_id != rule.id or row.deleted_at:
        raise HTTPException(status_code=404, detail="Condición no encontrada")
    if rule.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Una versión publicada es inmutable")
    before = model_dict(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await write_audit(db, action="RULE_CONDITION_UPDATE", user=actor, request=request, entity_type="base_condition", entity_id=str(row.id), before=before, after=model_dict(row))
    await db.commit()
    return model_dict(row, exclude={"deleted_at"})


@router.delete("/{rule_id}/conditions/{condition_id}")
async def delete_condition(rule_id: UUID, condition_id: UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    row = await db.get(BaseCondition, condition_id)
    if not rule or not row or row.rule_version_id != rule.id or row.deleted_at:
        raise HTTPException(status_code=404, detail="Condición no encontrada")
    if rule.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Una versión publicada es inmutable")
    row.active = False
    from datetime import UTC, datetime
    row.deleted_at = datetime.now(UTC)
    await write_audit(db, action="RULE_CONDITION_DELETE", user=actor, request=request, entity_type="base_condition", entity_id=str(row.id))
    await db.commit()
    return {"ok": True}


@router.get("/{rule_id}/diseases")
async def list_diseases(rule_id: UUID, db: DbSession, _: AnalystUser) -> dict:
    rows = list((await db.scalars(select(DiseaseRule).where(DiseaseRule.rule_version_id == rule_id, DiseaseRule.deleted_at.is_(None)).order_by(DiseaseRule.name))).all())
    return {"items": [model_dict(row, exclude={"deleted_at"}) for row in rows]}


@router.post("/{rule_id}/diseases")
async def create_disease(rule_id: UUID, payload: DiseaseRulePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Regla no encontrada")
    if rule.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Una versión publicada es inmutable")
    row = DiseaseRule(rule_version_id=rule.id, **payload.model_dump())
    db.add(row)
    await db.flush()
    await write_audit(db, action="DISEASE_RULE_CREATE", user=actor, request=request, entity_type="disease_rule", entity_id=str(row.id), after=model_dict(row))
    await db.commit()
    return model_dict(row, exclude={"deleted_at"})


@router.put("/{rule_id}/diseases/{disease_id}")
async def update_disease(rule_id: UUID, disease_id: UUID, payload: DiseaseRulePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    row = await db.get(DiseaseRule, disease_id)
    if not rule or not row or row.rule_version_id != rule.id or row.deleted_at:
        raise HTTPException(status_code=404, detail="Regla de enfermedad no encontrada")
    if rule.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Una versión publicada es inmutable")
    before = model_dict(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await write_audit(db, action="DISEASE_RULE_UPDATE", user=actor, request=request, entity_type="disease_rule", entity_id=str(row.id), before=before, after=model_dict(row))
    await db.commit()
    return model_dict(row, exclude={"deleted_at"})


@router.delete("/{rule_id}/diseases/{disease_id}")
async def delete_disease(rule_id: UUID, disease_id: UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    rule = await db.get(CropRuleVersion, rule_id)
    row = await db.get(DiseaseRule, disease_id)
    if not rule or not row or row.rule_version_id != rule.id or row.deleted_at:
        raise HTTPException(status_code=404, detail="Regla de enfermedad no encontrada")
    if rule.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Una versión publicada es inmutable")
    from datetime import UTC, datetime
    row.active = False
    row.deleted_at = datetime.now(UTC)
    await write_audit(db, action="DISEASE_RULE_DELETE", user=actor, request=request, entity_type="disease_rule", entity_id=str(row.id))
    await db.commit()
    return {"ok": True}


@router.post("/{rule_id}/validate")
async def validate(rule_id: UUID, db: DbSession, _: AnalystUser) -> dict:
    try:
        return await validate_rule_version(db, rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{rule_id}/simulate")
async def simulate(rule_id: UUID, payload: RuleSimulationPayload, db: DbSession, _: AnalystUser) -> dict:
    try:
        return await simulate_rule(db, rule_id, payload.station_id, payload.date_from, payload.date_to, payload.crop_cycle_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{first_rule_id}/compare/{second_rule_id}")
async def compare(first_rule_id: UUID, second_rule_id: UUID, payload: RuleSimulationPayload, db: DbSession, _: AnalystUser) -> dict:
    try:
        return await compare_rule_versions(db, first_rule_id, second_rule_id, payload.station_id, payload.date_from, payload.date_to, payload.crop_cycle_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
