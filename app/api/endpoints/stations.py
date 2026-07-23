from __future__ import annotations

import uuid
from datetime import date, timedelta
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import BackgroundJob, Station, WeatherReading
from app.schemas.resources import StationPayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict

router = APIRouter(prefix="/stations", tags=["Estaciones"])


@router.get("")
async def list_stations(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(Station).where(Station.deleted_at.is_(None)).order_by(Station.name).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [model_dict(row, exclude={"deleted_at"}) for row in rows], "limit": limit, "offset": offset}


@router.post("")
async def create_station(payload: StationPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if await db.scalar(select(Station).where(Station.code == payload.code.upper(), Station.deleted_at.is_(None))):
        raise HTTPException(status_code=409, detail="El código de estación ya existe")
    data = payload.model_dump()
    data["code"] = data["code"].upper()
    station = Station(**data, created_by=actor.id)
    db.add(station)
    await db.flush()
    await write_audit(db, action="STATION_CREATE", user=actor, request=request, entity_type="station", entity_id=str(station.id), after=model_dict(station, exclude={"deleted_at"}))
    await db.commit()
    return model_dict(station, exclude={"deleted_at"})


@router.put("/{station_id}")
async def update_station(station_id: uuid.UUID, payload: StationPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    station = await db.get(Station, station_id)
    if not station or station.deleted_at:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    duplicate = await db.scalar(select(Station).where(Station.code == payload.code.upper(), Station.id != station.id, Station.deleted_at.is_(None)))
    if duplicate:
        raise HTTPException(status_code=409, detail="El código de estación ya existe")
    before = model_dict(station, exclude={"deleted_at"})
    for key, value in payload.model_dump(include=payload.model_fields_set).items():
        setattr(station, key, value.upper() if key == "code" else value)
    await db.flush()
    after = model_dict(station, exclude={"deleted_at"})
    await write_audit(db, action="STATION_UPDATE", user=actor, request=request, entity_type="station", entity_id=str(station.id), before=before, after=after)
    await db.commit()
    return after


@router.delete("/{station_id}")
async def delete_station(station_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    station = await db.get(Station, station_id)
    if not station or station.deleted_at:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    from datetime import UTC, datetime
    station.active = False
    station.deleted_at = datetime.now(UTC)
    await write_audit(db, action="STATION_DELETE", user=actor, request=request, entity_type="station", entity_id=str(station.id))
    await db.commit()
    return {"ok": True}


@router.get("/{station_id}/readings")
async def readings(station_id: uuid.UUID, db: DbSession, _: AnalystUser, date_from: date | None = None, date_to: date | None = None, limit: int = 1000) -> dict:
    query = select(WeatherReading).where(WeatherReading.station_id == station_id)
    if date_from:
        query = query.where(WeatherReading.sampled_at >= date_from)
    if date_to:
        query = query.where(WeatherReading.sampled_at < date_to + timedelta(days=1))
    rows = (await db.scalars(query.order_by(WeatherReading.sampled_at.desc()).limit(min(limit, 10000)))).all()
    return {"items": [model_dict(row, exclude={"raw_payload"}) for row in rows]}


@router.post("/{station_id}/sync")
async def sync_station(station_id: uuid.UUID, db: DbSession, actor: AnalystUser, date_from: date | None = None, date_to: date | None = None) -> dict:
    station = await db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    if station.source_type != "FECOCLIMA_API" or not station.external_guid:
        raise HTTPException(status_code=400, detail="La estación no está configurada para API FECOCLIMA")
    end = date_to or date.today()
    start = date_from or end - timedelta(days=7)
    if start > end:
        raise HTTPException(status_code=400, detail="Rango inválido")
    job = await enqueue_job(db, "STATION_SYNC", {"station_id": str(station.id), "date_from": start.isoformat(), "date_to": end.isoformat()}, priority=20, created_by=actor.id)
    return {"job_id": str(job.id), "status": job.status, "message": "Sincronización encolada"}


@router.post("/{station_id}/test-connection")
async def test_connection(station_id: uuid.UUID, db: DbSession, _: AnalystUser) -> dict:
    from app.services.fecoclima_adapter import FecoclimaAdapter
    station = await db.get(Station, station_id)
    if not station or not station.external_guid:
        raise HTTPException(status_code=400, detail="Estación sin GUID externo")
    today = date.today()
    rows = await FecoclimaAdapter().fetch_range(station.external_guid, today.replace(day=1), today)
    return {"ok": True, "records_received": len(rows), "latest_sample": rows[-1]["sampled_at"].isoformat() if rows else None}
