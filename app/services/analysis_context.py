from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertConditionResult,
    AlertEvaluation,
    BaseCondition,
    Crop,
    CropRuleVersion,
    DiseaseRule,
    PhenologyStage,
    Station,
    WeatherForecast,
    WeatherReading,
)
from app.services.disease_forecast import build_forecast_risk
from app.services.phenology import calculate_stage
from app.services.serialization import model_dict
from app.services.satellite import latest_satellite_observation, satellite_observation_payload

_PERIOD_HOURS = {"24h": 24, "72h": 72, "7d": 168, "30d": 720}


def _safe_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(parsed) else round(parsed, 3)


def _series_summary(readings: list[WeatherReading]) -> dict[str, Any]:
    if not readings:
        return {"samples": 0, "from": None, "to": None, "variables": {}}
    rows = [
        {
            "sampled_at": row.sampled_at,
            "temperature_c": row.temperature_c,
            "humidity_pct": row.humidity_pct,
            "rain_mm": row.rain_mm,
            "soil_temperature_c": row.soil_temperature_c,
            "soil_moisture_pct": row.soil_moisture_pct,
            "leaf_sensor_value": row.leaf_sensor_value,
            "solar_wm2": row.solar_wm2,
            "evapotranspiration_mm": row.evapotranspiration_mm,
        }
        for row in readings
    ]
    frame = pd.DataFrame(rows)
    variables: dict[str, Any] = {}
    for column in [name for name in frame.columns if name != "sampled_at"]:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if numeric.notna().any():
            variables[column] = {
                "min": _safe_number(numeric.min()),
                "max": _safe_number(numeric.max()),
                "mean": _safe_number(numeric.mean()),
                "last": _safe_number(numeric.dropna().iloc[-1]),
                "available_samples": int(numeric.notna().sum()),
            }
    if "rain_mm" in frame:
        variables.setdefault("rain_mm", {})["sum"] = _safe_number(
            pd.to_numeric(frame["rain_mm"], errors="coerce").sum(min_count=1)
        )
    if "evapotranspiration_mm" in frame:
        variables.setdefault("evapotranspiration_mm", {})["sum"] = _safe_number(
            pd.to_numeric(frame["evapotranspiration_mm"], errors="coerce").sum(min_count=1)
        )
    return {
        "samples": len(readings),
        "from": readings[0].sampled_at.isoformat(),
        "to": readings[-1].sampled_at.isoformat(),
        "variables": variables,
    }


async def build_complete_analysis_context(
    db: AsyncSession,
    alert_id: UUID,
    *,
    period: str = "72h",
) -> dict[str, Any]:
    alert = await db.get(AlertEvaluation, alert_id)
    if not alert:
        raise ValueError("Evaluación de alerta no encontrada")
    station = await db.get(Station, alert.station_id)
    crop = await db.get(Crop, alert.crop_id)
    if not station or not crop:
        raise ValueError("El contexto de estación o cultivo ya no está disponible")

    condition_rows = list(
        (
            await db.scalars(
                select(AlertConditionResult)
                .where(AlertConditionResult.alert_evaluation_id == alert.id)
                .order_by(AlertConditionResult.created_at)
            )
        ).all()
    )
    rule = await db.get(CropRuleVersion, alert.rule_version_id) if alert.rule_version_id else None
    base_conditions: list[BaseCondition] = []
    disease_rules: list[DiseaseRule] = []
    if rule:
        base_conditions = list(
            (
                await db.scalars(
                    select(BaseCondition)
                    .where(BaseCondition.rule_version_id == rule.id, BaseCondition.active.is_(True))
                    .order_by(BaseCondition.order_index)
                )
            ).all()
        )
        disease_rules = list(
            (
                await db.scalars(
                    select(DiseaseRule)
                    .where(DiseaseRule.rule_version_id == rule.id, DiseaseRule.active.is_(True))
                    .order_by(DiseaseRule.name)
                )
            ).all()
        )

    stage_rows = list(
        (
            await db.scalars(
                select(PhenologyStage)
                .where(PhenologyStage.crop_id == crop.id, PhenologyStage.active.is_(True))
                .order_by(PhenologyStage.order_index)
            )
        ).all()
    )
    phenology = calculate_stage(
        alert.planting_date,
        alert.evaluation_date,
        [
            {
                "code": item.code,
                "name": item.name,
                "description": item.description,
                "order_index": item.order_index,
                "start_day": item.start_day,
                "end_day": item.end_day,
            }
            for item in stage_rows
        ],
    ) if alert.planting_date else {}

    forecasts = list(
        (
            await db.scalars(
                select(WeatherForecast)
                .where(
                    WeatherForecast.station_id == station.id,
                    WeatherForecast.forecast_date >= alert.evaluation_date,
                )
                .order_by(WeatherForecast.forecast_date)
                .limit(7)
            )
        ).all()
    )
    forecast_data = [
        {
            "date": item.forecast_date.isoformat(),
            "temperature_min_c": item.temperature_min_c,
            "temperature_max_c": item.temperature_max_c,
            "precipitation_mm": item.precipitation_mm,
            "precipitation_probability_pct": item.precipitation_probability_pct,
            "humidity_pct": item.humidity_pct,
            "wind_speed_ms": item.wind_speed_ms,
            "evapotranspiration_mm": item.evapotranspiration_mm,
            "estimated_soil_moisture_pct": item.estimated_soil_moisture_pct,
            "soil_temperature_avg_c": (item.raw_payload or {}).get("soil_temperature_avg_c"),
            "leaf_wetness_hours": (item.raw_payload or {}).get("leaf_wetness_hours"),
            "provider": item.provider,
        }
        for item in forecasts
    ]
    forecast_risk = await build_forecast_risk(
        db,
        crop.id,
        alert.planting_date or alert.evaluation_date,
        forecasts,
    )

    satellite = None
    satellite_context_status: dict[str, Any] = {"status": "NOT_AVAILABLE"}
    try:
        plot, observation = await latest_satellite_observation(db, station.id, crop.id)
        if plot and observation:
            satellite = await satellite_observation_payload(db, observation, plot)
            satellite_context_status = {"status": "COMPLETED"}
    except Exception as exc:
        # Copernicus is complementary evidence. A malformed or unavailable
        # scene must not prevent the station/rules analysis from answering.
        satellite_context_status = {"status": "FAILED", "error": str(exc)[:1000]}

    hours = _PERIOD_HOURS.get(period, 72)
    latest_reading = await db.scalar(
        select(WeatherReading)
        .where(WeatherReading.station_id == station.id)
        .order_by(WeatherReading.sampled_at.desc())
        .limit(1)
    )
    period_end = latest_reading.sampled_at if latest_reading else datetime.now(UTC)
    start = period_end - timedelta(hours=hours)
    readings = list(
        (
            await db.scalars(
                select(WeatherReading)
                .where(
                    WeatherReading.station_id == station.id,
                    WeatherReading.sampled_at >= start,
                    WeatherReading.sampled_at <= period_end,
                )
                .order_by(WeatherReading.sampled_at)
            )
        ).all()
    )
    reading_age_minutes = (
        round((datetime.now(UTC) - latest_reading.sampled_at).total_seconds() / 60, 1)
        if latest_reading
        else None
    )
    stale_threshold_minutes = max(60, station.expected_interval_minutes * 12)

    return {
        "query": {
            "station_id": str(station.id),
            "station": station.name,
            "crop_id": str(crop.id),
            "crop": crop.name,
            "crop_code": crop.code,
            "planting_date": alert.planting_date.isoformat() if alert.planting_date else None,
            "evaluation_date": alert.evaluation_date.isoformat(),
            "period": period,
        },
        "phenology": phenology,
        "deterministic_result": {
            "alert_id": str(alert.id),
            "level": alert.level,
            "conditions_met": alert.conditions_met,
            "conditions_total": alert.conditions_total,
            "completion_percentage": alert.completion_pct,
            "confidence": alert.confidence,
            "values_used": alert.values_used,
            "diseases": alert.diseases,
            "simple_explanation": alert.explanation,
            "conditions": [
                {
                    "key": item.key,
                    "label": item.label,
                    "status": item.status,
                    "value": item.observed_value,
                    "unit": item.unit,
                    "explanation": item.explanation,
                }
                for item in condition_rows
            ],
        },
        "rule_version": None if not rule else {
            "id": str(rule.id),
            "name": rule.name,
            "version": rule.version,
            "configuration": rule.configuration,
            "base_conditions": [model_dict(item, exclude={"deleted_at"}) for item in base_conditions],
            "disease_rules": [
                {
                    "name": item.name,
                    "conditions": item.conditions,
                    "valid_stages": item.valid_stages,
                    "validation_status": item.validation_status,
                    "enabled_for_evaluation": item.enabled_for_evaluation,
                    "source_reference": item.source_reference,
                }
                for item in disease_rules
            ],
        },
        "observed_series_summary": _series_summary(readings),
        "data_freshness": {
            "latest_reading_at": latest_reading.sampled_at.isoformat() if latest_reading else None,
            "reading_age_minutes": reading_age_minutes,
            "expected_interval_minutes": station.expected_interval_minutes,
            "stale_threshold_minutes": stale_threshold_minutes,
            "stale": reading_age_minutes is None or reading_age_minutes > stale_threshold_minutes,
            "period_anchored_to": period_end.isoformat(),
            "source": latest_reading.source if latest_reading else None,
            "source_reference": latest_reading.source_reference if latest_reading else None,
        },
        "forecast": forecast_data,
        "forecast_risk": forecast_risk,
        "satellite": satellite,
        "satellite_context_status": satellite_context_status,
        "analysis_instructions": {
            "satellite": (
                "Usar Copernicus como evidencia complementaria de la geozona. "
                "Relacionar NDVI, NDMI, NDWI, tendencias, nubosidad, antigüedad y tipo de geometría "
                "con la fenología, los datos meteorológicos y las reglas publicadas. No inferir una "
                "enfermedad ni una causa por un índice aislado y declarar las limitaciones de la escena."
            ),
            "observed_vs_forecast": (
                "Distinguir siempre observaciones de estación, proyecciones meteorológicas y evidencia satelital."
            ),
            "grounding": (
                "Usar exclusivamente los valores, reglas, cálculos, fragmentos documentales y fuentes incluidos "
                "en este contexto. No completar datos ausentes ni atribuir causas no demostradas."
            ),
        },
        "context_generated_at": datetime.now(UTC).isoformat(),
    }
