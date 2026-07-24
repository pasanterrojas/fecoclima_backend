from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import BackgroundJob, IntegrationConfig, IntegrationLog, Station, WeatherReading
from app.schemas.resources import StationPayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict
from app.services.satellite import ensure_station_plot

router = APIRouter(prefix="/stations", tags=["Estaciones"])


async def _mark_copernicus_retest_required(db: DbSession, station: Station, detail: str) -> None:
    integrations = list((await db.scalars(
        select(IntegrationConfig).where(
            IntegrationConfig.provider == "COPERNICUS_DATA_SPACE",
            IntegrationConfig.active.is_(True),
            IntegrationConfig.deleted_at.is_(None),
        )
    )).all())
    now = datetime.now(UTC)
    for integration in integrations:
        integration.last_result = "RETEST_REQUIRED"
        db.add(
            IntegrationLog(
                integration_id=integration.id,
                station_id=station.id,
                operation="STATION_GEOZONE_CHANGED",
                status="RETEST_REQUIRED",
                started_at=now,
                finished_at=now,
                duration_ms=0,
                extra_data={
                    "detail": detail,
                    "station_id": str(station.id),
                    "station": station.name,
                },
            )
        )


async def _sync_station_geozone(db: DbSession, station: Station) -> None:
    if station.influence_geojson or (station.latitude is not None and station.longitude is not None):
        await ensure_station_plot(db, station)


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

    # Los valores generados por PostgreSQL (created_at/updated_at) pueden
    # quedar expirados después del flush. En AsyncSession no se debe permitir
    # que model_dict dispare una carga implícita, porque produce MissingGreenlet.
    await db.refresh(station)
    station_data = model_dict(station, exclude={"deleted_at"})
    await _sync_station_geozone(db, station)
    await _mark_copernicus_retest_required(
        db,
        station,
        "La estación fue creada o actualizada. Ejecute una prueba Copernicus para esta estación.",
    )

    await write_audit(
        db,
        action="STATION_CREATE",
        user=actor,
        request=request,
        entity_type="station",
        entity_id=str(station.id),
        after=station_data,
    )
    await db.commit()
    return station_data


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

    # updated_at usa una expresión SQL onupdate. El flush expira ese atributo y
    # accederlo de forma síncrona desde model_dict provoca MissingGreenlet.
    # refresh realiza la lectura de forma explícita y esperada dentro del flujo async.
    await db.refresh(station)
    after = model_dict(station, exclude={"deleted_at"})
    geozone_fields = {"influence_geojson", "latitude", "longitude", "influence_radius_m"}
    geozone_changed = any(before.get(field) != after.get(field) for field in geozone_fields)
    if geozone_changed:
        await _sync_station_geozone(db, station)
        await _mark_copernicus_retest_required(
            db,
            station,
            "Cambió el polígono, las coordenadas o el radio. La prueba anterior ya no representa esta geozona.",
        )

    await write_audit(
        db,
        action="STATION_UPDATE",
        user=actor,
        request=request,
        entity_type="station",
        entity_id=str(station.id),
        before=before,
        after=after,
    )
    await db.commit()
    return after


@router.delete("/{station_id}")
async def delete_station(station_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    station = await db.get(Station, station_id)
    if not station or station.deleted_at:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
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
