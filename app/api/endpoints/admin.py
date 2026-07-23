from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.db.models import AIAgent, AlertEvaluation, AuditLog, BackgroundJob, Report, Station, SystemSetting, User, WeatherReading
from app.schemas.resources import SettingPayload
from app.services.audit import write_audit
from app.services.serialization import model_dict

router = APIRouter(tags=["Administración"])


@router.get("/admin/summary")
async def summary(db: DbSession, _: AdminUser) -> dict:
    async def count(query): return int((await db.scalar(query)) or 0)
    return {
        "stations_active": await count(select(func.count(Station.id)).where(Station.active.is_(True), Station.deleted_at.is_(None))),
        "users_active": await count(select(func.count(User.id)).where(User.active.is_(True), User.deleted_at.is_(None))),
        "readings_total": await count(select(func.count(WeatherReading.id))),
        "alerts_72h": await count(select(func.count(AlertEvaluation.id)).where(AlertEvaluation.evaluated_at >= datetime.now(UTC) - timedelta(hours=72))),
        "agents_active": await count(select(func.count(AIAgent.id)).where(AIAgent.active.is_(True), AIAgent.deleted_at.is_(None))),
        "reports_total": await count(select(func.count(Report.id))),
    }


@router.get("/admin/jobs")
async def jobs(db: DbSession, _: AdminUser, limit: int = 50) -> list[dict]:
    rows = (await db.scalars(select(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(min(limit, 250)))).all()
    return [model_dict(row) for row in rows]


@router.get("/audit")
async def audit(db: DbSession, _: AdminUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).offset(offset).limit(min(limit, 500)))).all()
    return {"items": [model_dict(row, exclude={"before_data", "after_data", "extra_data"}) for row in rows]}


@router.get("/settings")
async def settings_list(db: DbSession, _: AdminUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(SystemSetting).order_by(SystemSetting.key).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [model_dict(row) for row in rows]}


@router.post("/settings")
async def setting_create(payload: SettingPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if await db.scalar(select(SystemSetting).where(SystemSetting.key == payload.key)):
        raise HTTPException(status_code=409, detail="La clave ya existe")
    row = SystemSetting(**payload.model_dump(), updated_by=actor.id); db.add(row); await db.flush()
    await write_audit(db, action="SETTING_CREATE", user=actor, request=request, entity_type="system_setting", entity_id=str(row.id), after=model_dict(row))
    await db.commit(); return model_dict(row)


@router.put("/settings/{setting_id}")
async def setting_update(setting_id: uuid.UUID, payload: SettingPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(SystemSetting, setting_id)
    if not row: raise HTTPException(status_code=404, detail="Configuración no encontrada")
    before = model_dict(row)
    for key, value in payload.model_dump().items(): setattr(row, key, value)
    row.updated_by = actor.id; await db.flush()
    await write_audit(db, action="SETTING_UPDATE", user=actor, request=request, entity_type="system_setting", entity_id=str(row.id), before=before, after=model_dict(row))
    await db.commit(); return model_dict(row)


@router.delete("/settings/{setting_id}")
async def setting_delete(setting_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(SystemSetting, setting_id)
    if not row: raise HTTPException(status_code=404, detail="Configuración no encontrada")
    await db.delete(row)
    await write_audit(db, action="SETTING_DELETE", user=actor, request=request, entity_type="system_setting", entity_id=str(setting_id))
    await db.commit(); return {"ok": True}
