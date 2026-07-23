from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import Crop, CropCycle, PhenologyHistory, PhenologyStage
from app.schemas.actions import CycleStageUpdatePayload, PhenologyStagePayload
from app.services.audit import write_audit
from app.services.serialization import model_dict

router = APIRouter(prefix="/phenology", tags=["Fenología"])


@router.get("/stages")
async def list_stages(
    db: DbSession,
    _: AnalystUser,
    crop_id: UUID | None = None,
    cycle_type: str | None = None,
) -> dict:
    query = select(PhenologyStage).where(PhenologyStage.deleted_at.is_(None))
    if crop_id:
        query = query.where(PhenologyStage.crop_id == crop_id)
    if cycle_type:
        query = query.where(PhenologyStage.cycle_type == cycle_type)
    rows = list((await db.scalars(query.order_by(PhenologyStage.crop_id, PhenologyStage.order_index))).all())
    return {"items": [model_dict(row, exclude={"deleted_at"}) for row in rows]}


@router.post("/stages")
async def create_stage(payload: PhenologyStagePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if not await db.get(Crop, payload.crop_id):
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    exists = await db.scalar(
        select(PhenologyStage).where(
            PhenologyStage.crop_id == payload.crop_id,
            PhenologyStage.code == payload.code.upper(),
            PhenologyStage.cycle_type == payload.cycle_type,
            PhenologyStage.deleted_at.is_(None),
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="La etapa ya existe para el cultivo y tipo de ciclo")
    row = PhenologyStage(**payload.model_dump(exclude={"code"}), code=payload.code.upper())
    db.add(row)
    await db.flush()
    await write_audit(db, action="PHENOLOGY_STAGE_CREATE", user=actor, request=request, entity_type="phenology_stage", entity_id=str(row.id), after=model_dict(row))
    await db.commit()
    return model_dict(row, exclude={"deleted_at"})


@router.put("/stages/{stage_id}")
async def update_stage(stage_id: UUID, payload: PhenologyStagePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(PhenologyStage, stage_id)
    if not row or row.deleted_at:
        raise HTTPException(status_code=404, detail="Etapa no encontrada")
    before = model_dict(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value.upper() if key == "code" else value)
    await db.flush()
    await write_audit(db, action="PHENOLOGY_STAGE_UPDATE", user=actor, request=request, entity_type="phenology_stage", entity_id=str(row.id), before=before, after=model_dict(row))
    await db.commit()
    return model_dict(row, exclude={"deleted_at"})


@router.delete("/stages/{stage_id}")
async def delete_stage(stage_id: UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(PhenologyStage, stage_id)
    if not row or row.deleted_at:
        raise HTTPException(status_code=404, detail="Etapa no encontrada")
    row.active = False
    row.deleted_at = datetime.now(UTC)
    await write_audit(db, action="PHENOLOGY_STAGE_DELETE", user=actor, request=request, entity_type="phenology_stage", entity_id=str(row.id))
    await db.commit()
    return {"ok": True}


@router.get("/cycles/{cycle_id}/history")
async def cycle_history(cycle_id: UUID, db: DbSession, _: AnalystUser) -> dict:
    if not await db.get(CropCycle, cycle_id):
        raise HTTPException(status_code=404, detail="Ciclo no encontrado")
    rows = list((await db.scalars(select(PhenologyHistory).where(PhenologyHistory.crop_cycle_id == cycle_id).order_by(PhenologyHistory.effective_at.desc()))).all())
    return {"items": [model_dict(row) for row in rows]}


@router.post("/cycles/{cycle_id}/stage")
async def update_cycle_stage(cycle_id: UUID, payload: CycleStageUpdatePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    cycle = await db.get(CropCycle, cycle_id)
    if not cycle or cycle.deleted_at:
        raise HTTPException(status_code=404, detail="Ciclo no encontrado")
    stage = await db.scalar(
        select(PhenologyStage).where(
            PhenologyStage.crop_id == cycle.crop_id,
            PhenologyStage.code == payload.stage_code.upper(),
            PhenologyStage.active.is_(True),
            PhenologyStage.deleted_at.is_(None),
        )
    )
    if not stage:
        raise HTTPException(status_code=422, detail="La etapa no pertenece al cultivo del ciclo")
    previous = cycle.current_stage
    cycle.current_stage = stage.code
    cycle.stage_method = payload.method
    db.add(PhenologyHistory(crop_cycle_id=cycle.id, previous_stage=previous, new_stage=stage.code, method=payload.method, notes=payload.notes, changed_by=actor.id))
    await write_audit(db, action="CYCLE_STAGE_UPDATE", user=actor, request=request, entity_type="crop_cycle", entity_id=str(cycle.id), before={"stage": previous}, after={"stage": stage.code, "method": payload.method})
    await db.commit()
    return {"cycle_id": str(cycle.id), "previous_stage": previous, "current_stage": stage.code, "method": payload.method}
