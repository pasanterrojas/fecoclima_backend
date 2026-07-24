from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertEvaluation, Crop, PhenologyStage, Station, WeatherDailySummary
from app.services.agent_runtime import run_agent_for_alert
from app.services.analysis_context import build_complete_analysis_context
from app.services.phenology import calculate_stage
from app.services.source_refresh import refresh_station_source_if_needed
from app.services.weather import aggregate_range, evaluate_alert_for_day


def _safe(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(parsed) else round(parsed, 2)


def _window_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"days": 0}
    result: dict[str, Any] = {"days": int(len(frame))}
    mapping = {
        "temperature_avg_c": "temperature_c",
        "humidity_avg_pct": "humidity_pct",
        "soil_temperature_avg_c": "soil_temperature_c",
        "soil_moisture_avg_pct": "soil_moisture_pct",
        "completeness_pct": "completeness_pct",
    }
    for source, target in mapping.items():
        values = pd.to_numeric(frame[source], errors="coerce")
        if values.notna().any():
            result[target] = {
                "min": _safe(values.min()),
                "mean": _safe(values.mean()),
                "max": _safe(values.max()),
            }
    for source, target in {
        "rain_sum_mm": "rain_total_mm",
        "leaf_wetness_hours": "leaf_wetness_total_hours",
        "evapotranspiration_sum_mm": "evapotranspiration_total_mm",
    }.items():
        values = pd.to_numeric(frame[source], errors="coerce")
        result[target] = _safe(values.sum(min_count=1))
    rain = pd.to_numeric(frame["rain_sum_mm"], errors="coerce").fillna(0)
    result["rainy_days"] = int((rain > 0.1).sum())
    return result


async def build_phenology_environment_context(
    db: AsyncSession,
    station: Station,
    crop: Crop,
    planting_date: date,
    on_date: date,
) -> tuple[dict[str, Any], dict[str, Any], AlertEvaluation]:
    await aggregate_range(db, station.id, planting_date, on_date)
    evaluation = await evaluate_alert_for_day(
        db,
        station.id,
        crop.id,
        on_date,
        planting_date=planting_date,
    )
    if not isinstance(evaluation, AlertEvaluation):
        evaluation = await db.scalar(
            select(AlertEvaluation).where(
                AlertEvaluation.station_id == station.id,
                AlertEvaluation.crop_id == crop.id,
                AlertEvaluation.planting_date == planting_date,
                AlertEvaluation.evaluation_date == on_date,
            ).order_by(AlertEvaluation.created_at.desc()).limit(1)
        )
    if not evaluation:
        raise ValueError("No fue posible crear la evaluación fenológica")

    stages = list((await db.scalars(
        select(PhenologyStage).where(
            PhenologyStage.crop_id == crop.id,
            PhenologyStage.active.is_(True),
            PhenologyStage.deleted_at.is_(None),
        ).order_by(PhenologyStage.order_index)
    )).all())
    phenology = calculate_stage(
        planting_date,
        on_date,
        [{
            "code": item.code,
            "name": item.name,
            "description": item.description,
            "order_index": item.order_index,
            "start_day": item.start_day,
            "end_day": item.end_day,
        } for item in stages],
    )
    summaries = list((await db.scalars(
        select(WeatherDailySummary).where(
            WeatherDailySummary.station_id == station.id,
            WeatherDailySummary.summary_date >= planting_date,
            WeatherDailySummary.summary_date <= on_date,
        ).order_by(WeatherDailySummary.summary_date)
    )).all())
    frame = pd.DataFrame([{
        "date": item.summary_date,
        "temperature_avg_c": item.temperature_avg_c,
        "humidity_avg_pct": item.humidity_avg_pct,
        "rain_sum_mm": item.rain_sum_mm,
        "soil_temperature_avg_c": item.soil_temperature_avg_c,
        "soil_moisture_avg_pct": item.soil_moisture_avg_pct,
        "leaf_wetness_hours": item.leaf_wetness_hours,
        "evapotranspiration_sum_mm": item.evapotranspiration_sum_mm,
        "completeness_pct": item.completeness_pct,
    } for item in summaries])
    cumulative = {
        "from": planting_date.isoformat(),
        "to": on_date.isoformat(),
        "overall": _window_summary(frame),
        "last_30_days": _window_summary(frame.tail(30)),
        "last_7_days": _window_summary(frame.tail(7)),
        "data_days_available": int(len(frame)),
        "expected_calendar_days": int((on_date - planting_date).days + 1),
    }
    if not frame.empty:
        for column, key in [
            ("temperature_avg_c", "hottest_day"),
            ("rain_sum_mm", "rainiest_day"),
            ("humidity_avg_pct", "most_humid_day"),
        ]:
            numeric = pd.to_numeric(frame[column], errors="coerce")
            if numeric.notna().any():
                idx = numeric.idxmax()
                cumulative[key] = {
                    "date": frame.loc[idx, "date"].isoformat(),
                    "value": _safe(numeric.loc[idx]),
                }
    return phenology, cumulative, evaluation


async def analyze_phenology_environment(
    db: AsyncSession,
    station_id: UUID,
    crop_code: str,
    planting_date: date,
    *,
    force: bool = False,
) -> dict[str, Any]:
    station = await db.get(Station, station_id)
    crop = await db.scalar(select(Crop).where(Crop.code == crop_code.upper(), Crop.deleted_at.is_(None)))
    if not station or station.deleted_at:
        raise ValueError("Estación no encontrada")
    if not crop:
        raise ValueError("Cultivo no encontrado")
    timezone = ZoneInfo(station.timezone or "America/Asuncion")
    today = datetime.now(timezone).date()
    if planting_date > today:
        raise ValueError("La fecha de siembra no puede ser futura")
    if (today - planting_date).days > 700:
        raise ValueError("El análisis fenológico no puede superar 700 días")
    source_sync = await refresh_station_source_if_needed(db, station, requested=True, force=True)
    phenology, cumulative, evaluation = await build_phenology_environment_context(
        db, station, crop, planting_date, today
    )
    context = await build_complete_analysis_context(db, evaluation.id, period="30d")
    context["phenology"] = phenology
    context["environment_since_planting"] = cumulative
    recommendation = await run_agent_for_alert(
        db,
        evaluation.id,
        period="30d",
        purpose="PHENOLOGY_ANALYSIS",
        full_context=context,
        force=force,
    )
    return {
        "phenology": phenology,
        "environment_since_planting": cumulative,
        "recommendation": recommendation,
        "source_sync": source_sync,
        "alert_id": str(evaluation.id),
    }
