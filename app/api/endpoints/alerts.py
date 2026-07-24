from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import AnalystUser, DbSession
from app.db.models import AlertConditionResult, AlertEvaluation, Crop, Station
from app.schemas.actions import AlertEvaluatePayload
from app.services.alert_engine import maximum_level
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict
from app.services.weather import aggregate_range, evaluate_alert_for_day

router = APIRouter(prefix="/alerts", tags=["Alertas"])


async def _detail(db: DbSession, row: AlertEvaluation | None) -> dict | None:
    if not row:
        return None
    conditions = list(
        (
            await db.scalars(
                select(AlertConditionResult)
                .where(AlertConditionResult.alert_evaluation_id == row.id)
                .order_by(AlertConditionResult.key)
            )
        ).all()
    )
    return {
        **model_dict(row),
        "conditions": [model_dict(item) for item in conditions],
    }


@router.get("")
async def list_alerts(
    db: DbSession,
    _: AnalystUser,
    station_id: UUID | None = None,
    crop_id: UUID | None = None,
    planting_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    level: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    query = select(AlertEvaluation)
    if station_id:
        query = query.where(AlertEvaluation.station_id == station_id)
    if crop_id:
        query = query.where(AlertEvaluation.crop_id == crop_id)
    if planting_date:
        query = query.where(AlertEvaluation.planting_date == planting_date)
    if date_from:
        query = query.where(AlertEvaluation.evaluation_date >= date_from)
    if date_to:
        query = query.where(AlertEvaluation.evaluation_date <= date_to)
    if level:
        query = query.where(AlertEvaluation.level == level.upper())
    rows = list(
        (
            await db.scalars(
                query.order_by(
                    AlertEvaluation.evaluation_date.desc(),
                    AlertEvaluation.created_at.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return {
        "items": [await _detail(db, row) for row in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/{alert_id}")
async def alert_detail(alert_id: UUID, db: DbSession, _: AnalystUser) -> dict:
    row = await db.get(AlertEvaluation, alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")
    return await _detail(db, row)


@router.get("/latest/72h")
async def latest_72h(
    station_id: UUID,
    crop_id: UUID,
    planting_date: date,
    db: DbSession,
    _: AnalystUser,
) -> dict:
    cutoff = date.today() - timedelta(days=2)
    rows = list(
        (
            await db.scalars(
                select(AlertEvaluation)
                .where(
                    AlertEvaluation.station_id == station_id,
                    AlertEvaluation.crop_id == crop_id,
                    AlertEvaluation.planting_date == planting_date,
                    AlertEvaluation.evaluation_date >= cutoff,
                )
                .order_by(AlertEvaluation.evaluation_date.desc())
            )
        ).all()
    )
    current = rows[0] if rows else None
    return {
        "current": await _detail(db, current),
        "maximum_level_72h": maximum_level([{"level": row.level} for row in rows]),
        "evaluations": [await _detail(db, row) for row in rows],
    }


@router.post("/evaluate")
async def evaluate(payload: AlertEvaluatePayload, db: DbSession, actor: AnalystUser) -> dict:
    station = await db.get(Station, payload.station_id)
    crop = await db.get(Crop, payload.crop_id)
    if not station or station.deleted_at or not station.active:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    if not crop or crop.deleted_at or not crop.active:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    if payload.run_in_background:
        job = await enqueue_job(
            db,
            "AGGREGATE_ALERTS",
            {
                "station_id": str(station.id),
                "crop_id": str(crop.id),
                "planting_date": payload.planting_date.isoformat(),
                "date_from": payload.date_from.isoformat(),
                "date_to": payload.date_to.isoformat(),
            },
            priority=40,
            created_by=actor.id,
        )
        return {"status": "PENDING", "job_id": str(job.id)}
    await aggregate_range(db, station.id, payload.date_from, payload.date_to)
    results = []
    current = payload.date_from
    while current <= payload.date_to:
        result = await evaluate_alert_for_day(
            db,
            station.id,
            crop.id,
            current,
            planting_date=payload.planting_date,
        )
        results.append({"date": current.isoformat(), **result})
        current += timedelta(days=1)
    return {"status": "COMPLETED", "items": results}
