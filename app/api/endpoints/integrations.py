from __future__ import annotations

import uuid
from datetime import UTC, datetime
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import case, select

from app.api.deps import AdminUser, DbSession
from app.core.crypto import encrypt_secret, mask_secret
from app.db.models import IntegrationConfig, IntegrationLog, Station
from app.schemas.resources import IntegrationPayload
from app.services.audit import write_audit
from app.services.serialization import model_dict
from app.services.forecast import fetch_open_meteo
from app.services.satellite import test_copernicus_integration

router = APIRouter(prefix="/integrations", tags=["Integraciones"])


def _serialize(row: IntegrationConfig) -> dict:
    return {**model_dict(row, exclude={"encrypted_secret", "deleted_at"}), "secret_masked": ("••••••" + (row.secret_hint or "")) if row.encrypted_secret else None, "has_secret": bool(row.encrypted_secret)}


@router.get("")
async def list_integrations(db: DbSession, _: AdminUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (
        await db.scalars(
            select(IntegrationConfig)
            .where(IntegrationConfig.deleted_at.is_(None))
            .order_by(IntegrationConfig.name)
            .offset(offset)
            .limit(min(limit, 250))
        )
    ).all()
    items = []
    for row in rows:
        latest_log = await db.scalar(
            select(IntegrationLog)
            .where(IntegrationLog.integration_id == row.id)
            .order_by(IntegrationLog.created_at.desc())
            .limit(1)
        )
        item = _serialize(row)
        item["last_detail"] = (
            ((latest_log.extra_data or {}).get("detail") if latest_log else None)
            or (latest_log.error_message if latest_log else None)
        )
        item["last_diagnostics"] = (
            ((latest_log.extra_data or {}).get("test_data") if latest_log else None)
            or {}
        )
        items.append(item)
    return {"items": items}


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
async def test_integration(integration_id: uuid.UUID, db: DbSession, _: AdminUser, station_id: uuid.UUID | None = None) -> dict:
    row = await db.get(IntegrationConfig, integration_id)
    if not row or row.deleted_at:
        raise HTTPException(status_code=404, detail="Integración no encontrada")
    started = datetime.now(UTC)
    status = "COMPLETED"
    detail = "Configuración válida"
    test_data: dict = {}
    try:
        if row.provider == "OPEN_METEO":
            if station_id:
                station = await db.get(Station, station_id)
                if station and (
                    station.deleted_at
                    or not station.active
                    or station.latitude is None
                    or station.longitude is None
                ):
                    station = None
            else:
                station = await db.scalar(
                    select(Station).where(
                        Station.active.is_(True),
                        Station.deleted_at.is_(None),
                        Station.latitude.is_not(None),
                        Station.longitude.is_not(None),
                    ).order_by(Station.name, Station.id).limit(1)
                )
            if not station:
                raise ValueError("No existe una estación activa con coordenadas")
            forecast = await fetch_open_meteo(station.latitude, station.longitude, days=2)
            if not forecast:
                raise RuntimeError("Open-Meteo no devolvió días de pronóstico")
            detail = f"Open-Meteo respondió correctamente para {station.name}"
            test_data = {
                "station": station.name,
                "days": len(forecast),
                "first_day": forecast[0],
                "soil_forecast": forecast[0].get("soil_temperature_avg_c") is not None,
                "leaf_wetness_proxy": forecast[0].get("leaf_wetness_hours") is not None,
            }
        elif row.provider == "COPERNICUS_DATA_SPACE":
            test_data = await test_copernicus_integration(db, station_id=station_id)
            copernicus_status = str(test_data.get("status") or "FAILED")
            processing_error = test_data.get("processing_error")
            if copernicus_status == "PROCESSED":
                status = "COMPLETED"
                detail = "Copernicus STAC, OAuth, imagen RGB y estadísticas espectrales están operativos."
            elif copernicus_status == "FAILED":
                status = "FAILED"
                detail = processing_error or "Falló la prueba Copernicus; revise el diagnóstico de fases."
            elif copernicus_status == "CATALOG_ONLY":
                status = "PARTIAL"
                detail = (
                    "El catálogo STAC respondió, pero faltan client_id/client_secret OAuth para generar "
                    "la imagen RGB y los índices espectrales."
                )
            else:
                status = "PARTIAL"
                detail = (
                    "Copernicus respondió parcialmente. "
                    + (f"Detalle: {processing_error}" if processing_error else "Revise el diagnóstico de fases y la disponibilidad de píxeles válidos.")
                )
        elif row.integration_type in {"FORECAST", "WEATHER"} and not (row.base_url or row.provider == "FECOCLIMA"):
            raise ValueError("Falta URL base")
        elif row.integration_type == "AI":
            from app.core.config import settings
            if not settings.ai_api_key:
                raise ValueError("Falta AI_API_KEY en el .env del backend")
            detail = "La credencial de IA está disponible en el entorno."
            test_data = {"model": settings.ai_default_model, "embedding_model": settings.ai_embedding_model}
        elif row.integration_type == "SMTP":
            from app.core.config import settings
            if not settings.smtp_host or not settings.smtp_username or not settings.smtp_password:
                raise ValueError("Faltan SMTP_HOST, SMTP_USERNAME o SMTP_PASSWORD en el .env")
            detail = "Variables SMTP disponibles; el envío real se valida con recuperación de contraseña."
            test_data = {"host": settings.smtp_host, "port": settings.smtp_port, "starttls": settings.smtp_starttls}
    except Exception as exc:
        status = "FAILED"
        detail = str(exc)
    finished = datetime.now(UTC)
    row.last_run_at = finished
    row.last_result = status
    db.add(
        IntegrationLog(
            integration_id=row.id,
            operation="TEST_CONNECTION",
            status=status,
            started_at=started,
            finished_at=finished,
            duration_ms=int((finished - started).total_seconds() * 1000),
            error_message=detail if status == "FAILED" else None,
            extra_data={"detail": detail, "test_data": test_data},
        )
    )
    await db.commit()
    # Return phase diagnostics even on failure so the CMS can show whether
    # STAC, OAuth, Process or Statistics caused the persisted FAILED result.
    return {
        "ok": status != "FAILED",
        "status": status,
        "detail": detail,
        "data": test_data,
    }


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


@router.get("/{integration_id}")
async def get_integration(integration_id: uuid.UUID, db: DbSession, _: AdminUser) -> dict:
    row = await db.get(IntegrationConfig, integration_id)
    if not row or row.deleted_at:
        raise HTTPException(status_code=404, detail="Integración no encontrada")
    return _serialize(row)
