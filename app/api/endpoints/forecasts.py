from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import Station, StationForecastVerification, WeatherForecast
from app.schemas.actions import ForecastRefreshPayload
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict

router = APIRouter(prefix="/forecasts", tags=["Pronóstico meteorológico"])


@router.get("")
async def list_forecasts(
    db: DbSession,
    _: AnalystUser,
    station_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    provider: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    query = select(WeatherForecast)
    if station_id:
        query = query.where(WeatherForecast.station_id == station_id)
    if date_from:
        query = query.where(WeatherForecast.forecast_date >= date_from)
    if date_to:
        query = query.where(WeatherForecast.forecast_date <= date_to)
    if provider:
        query = query.where(WeatherForecast.provider == provider)
    rows = list((await db.scalars(query.order_by(WeatherForecast.forecast_date, WeatherForecast.generated_at.desc()).limit(limit))).all())
    return {"items": [model_dict(row, exclude={"raw_payload"}) for row in rows]}


@router.get("/verification")
async def prediction_verification(db: DbSession, _: AdminUser,
    station_id: UUID | None = None, date_from: date | None = None, date_to: date | None = None,
    limit: int = Query(default=60, ge=1, le=200), offset: int = Query(default=0, ge=0)) -> dict:
    from app.core.config import settings
    from sqlalchemy import func
    filters = []
    if station_id:
        filters.append(StationForecastVerification.station_id == station_id)
    if date_from:
        filters.append(StationForecastVerification.target_date >= date_from)
    if date_to:
        filters.append(StationForecastVerification.target_date <= date_to)
    query = select(StationForecastVerification, Station.name).join(Station).where(*filters)
    rows = (await db.execute(query.order_by(StationForecastVerification.target_date.desc(), Station.name)
                            .offset(offset).limit(limit))).all()
    total = await db.scalar(select(func.count()).select_from(StationForecastVerification).where(*filters))
    return {"items": [{**model_dict(row), "station_name": name} for row, name in rows], "total": total,
            "schedule": {"hour": settings.prediction_daily_hour, "minute": settings.prediction_daily_minute,
                         "timezone": settings.prediction_timezone}}


@router.post("/refresh")
async def refresh_forecast(payload: ForecastRefreshPayload, db: DbSession, actor: AnalystUser) -> dict:
    station = await db.get(Station, payload.station_id)
    if not station or station.deleted_at or not station.active:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    if station.latitude is None or station.longitude is None:
        raise HTTPException(status_code=422, detail="La estación requiere latitud y longitud")
    job = await enqueue_job(db, "FORECAST_REFRESH", {"station_id": str(station.id)}, priority=70, created_by=actor.id)
    return {"status": job.status, "job_id": str(job.id)}
