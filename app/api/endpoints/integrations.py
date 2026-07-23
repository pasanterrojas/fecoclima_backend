from __future__ import annotations

import uuid
from datetime import UTC, datetime
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession
from app.core.crypto import encrypt_secret, mask_secret
from app.db.models import IntegrationConfig, IntegrationLog
from app.schemas.resources import IntegrationPayload
from app.services.audit import write_audit
from app.services.serialization import model_dict

router = APIRouter(prefix="/integrations", tags=["Integraciones"])


def _serialize(row: IntegrationConfig) -> dict:
    return {**model_dict(row, exclude={"encrypted_secret", "deleted_at"}), "secret_masked": ("••••••" + (row.secret_hint or "")) if row.encrypted_secret else None, "has_secret": bool(row.encrypted_secret)}


@router.get("")
async def list_integrations(db: DbSession, _: AdminUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(IntegrationConfig).where(IntegrationConfig.deleted_at.is_(None)).order_by(IntegrationConfig.name).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [_serialize(row) for row in rows]}


@router.post("")
async def create_integration(payload: IntegrationPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if await db.scalar(select(IntegrationConfig).where(IntegrationConfig.name == payload.name, IntegrationConfig.deleted_at.is_(None))):
        raise HTTPException(status_code=409, detail="Ya existe una integración con ese nombre")
    data = payload.model_dump(exclude={"secret_value"})
    row = IntegrationConfig(**data, created_by=actor.id)
    if payload.secret_value:
        row.encrypted_secret = encrypt_secret(payload.secret_value); row.secret_hint = payload.secret_value[-4:]
    db.add(row)
    await db.flush()
    await db.refresh(row)
    await write_audit(db, action="INTEGRATION_CREATE", user=actor, request=request, entity_type="integration", entity_id=str(row.id), after=_serialize(row))
    await db.commit(); return _serialize(row)


@router.put("/{integration_id}")
async def update_integration(integration_id: uuid.UUID, payload: IntegrationPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(IntegrationConfig, integration_id)
    if not row or row.deleted_at: raise HTTPException(status_code=404, detail="Integración no encontrada")
    before = _serialize(row)
    for key, value in payload.model_dump(exclude={"secret_value"}).items(): setattr(row, key, value)
    if payload.secret_value:
        row.encrypted_secret = encrypt_secret(payload.secret_value); row.secret_hint = payload.secret_value[-4:]
    await db.flush()
    await db.refresh(row)
    after = _serialize(row)
    await write_audit(db, action="INTEGRATION_UPDATE", user=actor, request=request, entity_type="integration", entity_id=str(row.id), before=before, after=after)
    await db.commit(); return after


@router.post("/{integration_id}/test")
async def test_integration(integration_id: uuid.UUID, db: DbSession, _: AdminUser) -> dict:
    row = await db.get(IntegrationConfig, integration_id)
    if not row or row.deleted_at: raise HTTPException(status_code=404, detail="Integración no encontrada")
    started = datetime.now(UTC); status = "COMPLETED"; detail = "Configuración válida"
    try:
        if row.integration_type in {"FORECAST", "WEATHER"} and not row.base_url:
            raise ValueError("Falta URL base")
        if row.integration_type == "AI" and not row.encrypted_secret:
            raise ValueError("Falta credencial del proveedor de IA")
        if row.integration_type == "SMTP":
            detail = "La conexión SMTP se valida con las variables de entorno del backend"
    except Exception as exc:
        status = "FAILED"; detail = str(exc)
    row.last_run_at = datetime.now(UTC); row.last_result = status
    db.add(IntegrationLog(integration_id=row.id, operation="TEST_CONNECTION", status=status, started_at=started, finished_at=datetime.now(UTC), duration_ms=int((datetime.now(UTC)-started).total_seconds()*1000), error_message=detail if status == "FAILED" else None, extra_data={"detail": detail}))
    await db.commit()
    if status == "FAILED": raise HTTPException(status_code=422, detail=detail)
    return {"ok": True, "detail": detail}


@router.get("/{integration_id}/logs")
async def integration_logs(integration_id: uuid.UUID, db: DbSession, _: AdminUser, limit: int = 100) -> dict:
    rows = (await db.scalars(select(IntegrationLog).where(IntegrationLog.integration_id == integration_id).order_by(IntegrationLog.created_at.desc()).limit(min(limit, 250)))).all()
    return {"items": [model_dict(row) for row in rows]}


@router.delete("/{integration_id}")
async def delete_integration(integration_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(IntegrationConfig, integration_id)
    if not row or row.deleted_at: raise HTTPException(status_code=404, detail="Integración no encontrada")
    row.active = False; row.deleted_at = datetime.now(UTC)
    await write_audit(db, action="INTEGRATION_DELETE", user=actor, request=request, entity_type="integration", entity_id=str(row.id))
    await db.commit(); return {"ok": True}
