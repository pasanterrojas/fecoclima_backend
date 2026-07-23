from __future__ import annotations

import uuid
from datetime import date
from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AnalystUser, DbSession
from app.db.models import AlertEvaluation, SatelliteObservation, WeatherForecast
from app.services.serialization import model_dict

router = APIRouter(tags=["Consultas analíticas"])


@router.get("/alerts")
async def alerts(db: DbSession, _: AnalystUser, station_id: uuid.UUID | None = None, crop_id: uuid.UUID | None = None, date_from: date | None = None, date_to: date | None = None, limit: int = 200) -> dict:
    query = select(AlertEvaluation)
    if station_id: query = query.where(AlertEvaluation.station_id == station_id)
    if crop_id: query = query.where(AlertEvaluation.crop_id == crop_id)
    if date_from: query = query.where(AlertEvaluation.evaluation_date >= date_from)
    if date_to: query = query.where(AlertEvaluation.evaluation_date <= date_to)
    rows = (await db.scalars(query.order_by(AlertEvaluation.evaluation_date.desc()).limit(min(limit, 1000)))).all()
    return {"items": [model_dict(row) for row in rows]}


@router.get("/forecasts")
async def forecasts(db: DbSession, _: AnalystUser, station_id: uuid.UUID | None = None, limit: int = 100) -> dict:
    query = select(WeatherForecast)
    if station_id: query = query.where(WeatherForecast.station_id == station_id)
    rows = (await db.scalars(query.order_by(WeatherForecast.forecast_date.desc()).limit(min(limit, 500)))).all()
    return {"items": [model_dict(row, exclude={"raw_payload"}) for row in rows]}


@router.get("/satellite")
async def satellite(db: DbSession, _: AnalystUser, plot_id: uuid.UUID | None = None, limit: int = 100) -> dict:
    query = select(SatelliteObservation)
    if plot_id: query = query.where(SatelliteObservation.plot_id == plot_id)
    rows = (await db.scalars(query.order_by(SatelliteObservation.captured_at.desc()).limit(min(limit, 500)))).all()
    return {"items": [model_dict(row, exclude={"raw_payload"}) for row in rows]}
