from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import AnalystUser, DbSession
from app.db.models import AlertConditionResult, AlertEvaluation, Crop, CropCycle, Station
from app.schemas.actions import AlertEvaluatePayload
from app.services.alert_engine import maximum_level
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict
from app.services.weather import aggregate_range, evaluate_alert_for_day

router = APIRouter(prefix="/alerts", tags=["Alertas determinísticas"])


async def _detail(db: DbSession, row: AlertEvaluation) -> dict:
    conditions = list(
        (
            await db.scalars(
                select(AlertConditionResult)
                .where(AlertConditionResult.alert_evaluation_id == row.id)
                .order_by(AlertConditionResult.created_at)
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
    crop_cycle_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    level: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    query = select(AlertEvaluation)
    if station_id:
        query = query.where(AlertEvaluation.station_id == station_id)
    if crop_id:
        query = query.where(AlertEvaluation.crop_id == crop_id)
    if crop_cycle_id:
        query = query.where(AlertEvaluation.crop_cycle_id == crop_cycle_id)
    if date_from:
        query = query.where(AlertEvaluation.evaluation_date >= date_from)
    if date_to:
        query = query.where(AlertEvaluation.evaluation_date <= date_to)
    if level:
        query = query.where(AlertEvaluation.level == level.upper())
    rows = list(
        (
            await db.scalars(
                query.order_by(AlertEvaluation.evaluation_date.desc(), AlertEvaluation.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return {"items": [await _detail(db, row) for row in rows], "limit": limit, "offset": offset}


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
    db: DbSession,
    _: AnalystUser,
    crop_cycle_id: UUID | None = None,
) -> dict:
    cutoff = date.today() - timedelta(days=3)
    query = select(AlertEvaluation).where(
        AlertEvaluation.station_id == station_id,
        AlertEvaluation.crop_id == crop_id,
        AlertEvaluation.evaluation_date >= cutoff,
    )
    query = query.where(
        AlertEvaluation.crop_cycle_id == crop_cycle_id
        if crop_cycle_id
        else AlertEvaluation.crop_cycle_id.is_(None)
    )
    rows = list((await db.scalars(query.order_by(AlertEvaluation.evaluation_date.desc()))).all())
    current = rows[0] if rows else None
    return {
        "current": await _detail(db, current) if current else None,
        "maximum_level_72h": maximum_level([{"level": row.level} for row in rows]),
        "evaluations": [await _detail(db, row) for row in rows],
    }


@router.post("/evaluate")
async def evaluate(payload: AlertEvaluatePayload, db: DbSession, actor: AnalystUser) -> dict:
    station = await db.get(Station, payload.station_id)
    crop = await db.get(Crop, payload.crop_id)
    cycle = await db.get(CropCycle, payload.crop_cycle_id) if payload.crop_cycle_id else None
    if not station or station.deleted_at or not station.active:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    if not crop or crop.deleted_at or not crop.active:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    if cycle and (cycle.station_id != station.id or cycle.crop_id != crop.id):
        raise HTTPException(status_code=400, detail="El ciclo no corresponde a la estación y cultivo")
    if payload.run_in_background:
        job = await enqueue_job(
            db,
            "AGGREGATE_ALERTS",
            {
                "station_id": str(station.id),
                "crop_id": str(crop.id),
                "cycle_id": str(cycle.id) if cycle else None,
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
        result = await evaluate_alert_for_day(db, station.id, crop.id, current, cycle_id=cycle.id if cycle else None)
        results.append({"date": current.isoformat(), **result})
        current += timedelta(days=1)
    return {"status": "COMPLETED", "items": results}
