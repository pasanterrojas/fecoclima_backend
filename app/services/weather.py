from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertConditionResult,
    AlertEvaluation,
    BaseCondition,
    Crop,
    CropRuleVersion,
    DiseaseRule,
    IntegrationLog,
    Station,
    WeatherDailySummary,
    WeatherReading,
)
from app.services.alert_engine import ConditionSpec, evaluate_daily_alert
from app.services.fecoclima_adapter import FecoclimaAdapter
from app.services.phenology import calculate_stage


VALID_RANGES = {
    "temperature_c": (-60, 65),
    "humidity_pct": (0, 100),
    "rain_mm": (0, 1000),
    "soil_temperature_c": (-20, 80),
    "soil_moisture_pct": (0, 100),
    "leaf_sensor_value": (0, 100000),
    "wind_speed_ms": (0, 120),
    "pressure_hpa": (800, 1150),
    "solar_wm2": (0, 1800),
}


def quality_flags(reading: dict) -> list[str]:
    flags: list[str] = []
    for key, (lower, upper) in VALID_RANGES.items():
        value = reading.get(key)
        if value is not None and not lower <= float(value) <= upper:
            flags.append(f"OUT_OF_RANGE:{key}")
    return flags


async def upsert_readings(db: AsyncSession, station_id, readings: list[dict]) -> tuple[int, int]:
    if not readings:
        return 0, 0
    # Deduplicación en memoria y conteo idempotente antes del upsert.
    unique = {reading["sampled_at"]: reading for reading in readings}
    timestamps = list(unique)
    existing = set(
        (
            await db.scalars(
                select(WeatherReading.sampled_at).where(
                    WeatherReading.station_id == station_id,
                    WeatherReading.sampled_at.in_(timestamps),
                )
            )
        ).all()
    )
    created = sum(timestamp not in existing for timestamp in timestamps)
    updated = len(timestamps) - created
    now = datetime.now(UTC)
    for reading in unique.values():
        reading = dict(reading)
        reading["quality_flags"] = quality_flags(reading)
        values = {"station_id": station_id, **reading}
        statement = insert(WeatherReading).values(**values)
        excluded = statement.excluded
        update_values = {
            key: getattr(excluded, key)
            for key in values
            if key not in {"station_id", "sampled_at", "created_at"}
        }
        update_values["updated_at"] = now
        statement = statement.on_conflict_do_update(
            index_elements=[WeatherReading.station_id, WeatherReading.sampled_at],
            set_=update_values,
        )
        await db.execute(statement)
    await db.commit()
    return created, updated


async def sync_fecoclima_station(db: AsyncSession, station_id, date_from: date, date_to: date) -> dict:
    station = await db.get(Station, station_id)
    if not station or not station.active:
        raise ValueError("Estación no disponible")
    if not station.external_guid:
        raise ValueError("La estación no tiene GUID externo")
    started = datetime.now(UTC)
    log = IntegrationLog(station_id=station.id, operation="FECOCLIMA_SYNC", status="RUNNING", started_at=started)
    db.add(log)
    await db.commit()
    try:
        readings = await FecoclimaAdapter().fetch_range(station.external_guid, date_from, date_to)
        created, updated = await upsert_readings(db, station.id, readings)
        station.last_synced_at = datetime.now(UTC)
        log.status = "COMPLETED"
        log.records_received = len(readings)
        log.records_created = created
        log.records_updated = updated
        log.finished_at = datetime.now(UTC)
        log.duration_ms = int((log.finished_at - started).total_seconds() * 1000)
        await db.commit()
        return {"received": len(readings), "created": created, "updated": updated}
    except Exception as exc:
        log.status = "FAILED"
        log.error_message = str(exc)[:2000]
        log.finished_at = datetime.now(UTC)
        log.duration_ms = int((log.finished_at - started).total_seconds() * 1000)
        await db.commit()
        raise


RAUL_PENA_GUID = "C2694D32-5A4B-48C0-A3B6-8345D9E4935B"


def effective_leaf_sensor_config(station: Station) -> dict:
    """Return validated station semantics for the raw leaf sensor.

    The supplied crop-alert sheets use 6 as the operational leaf-sensor
    threshold. For Raul Peña, samples at or above 6 are counted as wet and 0
    is treated as a valid dry reading, not missing data. Other stations remain
    explicit/configurable until their sensor semantics are validated.
    """
    config = dict(station.leaf_sensor_config or {})
    if station.external_guid and station.external_guid.upper() == RAUL_PENA_GUID:
        if config.get("mode") in {None, "", "unknown"}:
            return {
                "mode": "threshold",
                "wet_threshold": 6,
                "scale_min": 0,
                "scale_max": 15,
                "interpretation": "samples_at_or_above_threshold_count_as_wet",
                "source": "project_rule_from_supplied_spreadsheets",
            }
    return config


def _leaf_wetness_hours(values: pd.Series, config: dict, expected_interval_minutes: int) -> float | None:
    mode = config.get("mode", "unknown")
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty or mode == "unknown":
        return None
    if mode == "binary_wet":
        wet_value = float(config.get("wet_value", 1))
        wet_samples = np.isclose(clean.to_numpy(dtype=float), wet_value).sum()
        return round(float(wet_samples * expected_interval_minutes / 60), 2)
    if mode == "threshold":
        threshold = float(config.get("wet_threshold"))
        wet_samples = (clean >= threshold).sum()
        return round(float(wet_samples * expected_interval_minutes / 60), 2)
    if mode == "hours":
        return float(clean.mean())
    return None


async def aggregate_station_day(db: AsyncSession, station: Station, summary_date: date) -> WeatherDailySummary:
    tz = ZoneInfo(station.timezone or "America/Asuncion")
    local_start = datetime.combine(summary_date, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start, utc_end = local_start.astimezone(UTC), local_end.astimezone(UTC)
    rows = (await db.scalars(select(WeatherReading).where(WeatherReading.station_id == station.id, WeatherReading.sampled_at >= utc_start, WeatherReading.sampled_at < utc_end).order_by(WeatherReading.sampled_at))).all()
    interval_minutes = max(1, station.expected_interval_minutes)
    now_local = datetime.now(tz)
    if summary_date == now_local.date():
        elapsed_minutes = max(0.0, min(1440.0, (now_local - local_start).total_seconds() / 60))
        expected = max(1, int(elapsed_minutes // interval_minutes) + 1)
    else:
        expected = max(1, round(24 * 60 / interval_minutes))
    if rows:
        frame = pd.DataFrame([{c: getattr(row, c) for c in ["temperature_c", "humidity_pct", "rain_mm", "soil_temperature_c", "soil_moisture_pct", "leaf_sensor_value", "solar_wm2", "evapotranspiration_mm"]} for row in rows])
        values = {
            "temperature_avg_c": frame.temperature_c.mean(skipna=True),
            "humidity_avg_pct": frame.humidity_pct.mean(skipna=True),
            "rain_sum_mm": frame.rain_mm.sum(min_count=1),
            "soil_temperature_avg_c": frame.soil_temperature_c.mean(skipna=True),
            "soil_moisture_avg_pct": frame.soil_moisture_pct.mean(skipna=True),
            "leaf_wetness_hours": _leaf_wetness_hours(frame.leaf_sensor_value, effective_leaf_sensor_config(station), station.expected_interval_minutes),
            "solar_sum_wm2_samples": frame.solar_wm2.sum(min_count=1),
            "evapotranspiration_sum_mm": frame.evapotranspiration_mm.sum(min_count=1),
        }
        values = {key: None if pd.isna(value) else round(float(value), 3) for key, value in values.items()}
        outliers = sum(len(row.quality_flags or []) for row in rows)
        sources = sorted({row.source for row in rows})
    else:
        values = {key: None for key in ["temperature_avg_c", "humidity_avg_pct", "rain_sum_mm", "soil_temperature_avg_c", "soil_moisture_avg_pct", "leaf_wetness_hours", "solar_sum_wm2_samples", "evapotranspiration_sum_mm"]}
        outliers = 0
        sources = []
    payload = {
        "station_id": station.id,
        "summary_date": summary_date,
        "timezone": station.timezone,
        **values,
        "records_count": len(rows),
        "expected_records": expected,
        "completeness_pct": round(min(100, len(rows) * 100 / expected), 2),
        "outlier_count": outliers,
        "sources": sources,
    }
    statement = insert(WeatherDailySummary).values(**payload).on_conflict_do_update(index_elements=[WeatherDailySummary.station_id, WeatherDailySummary.summary_date], set_={key: value for key, value in payload.items() if key not in {"station_id", "summary_date"}}).returning(WeatherDailySummary.id)
    summary_id = (await db.execute(statement)).scalar_one()
    await db.commit()
    return await db.get(WeatherDailySummary, summary_id)


async def aggregate_range(db: AsyncSession, station_id, date_from: date, date_to: date) -> int:
    station = await db.get(Station, station_id)
    if not station:
        raise ValueError("Estación no encontrada")
    count = 0
    current = date_from
    while current <= date_to:
        await aggregate_station_day(db, station, current)
        count += 1
        current += timedelta(days=1)
    return count


async def _phenology_for_date(
    db: AsyncSession,
    crop_id,
    planting_date: date | None,
    on_date: date,
) -> dict:
    if not planting_date:
        return {
            "planting_date": None,
            "analysis_date": on_date.isoformat(),
            "days_after_planting": None,
            "stage_code": None,
            "stage_name": "Fecha de siembra requerida",
            "description": "Seleccione la fecha de siembra para estimar la etapa fenológica.",
            "timeline": [],
        }
    from app.db.models import PhenologyStage

    stages = list(
        (
            await db.scalars(
                select(PhenologyStage)
                .where(
                    PhenologyStage.crop_id == crop_id,
                    PhenologyStage.active.is_(True),
                )
                .order_by(PhenologyStage.order_index)
            )
        ).all()
    )
    return calculate_stage(
        planting_date,
        on_date,
        [
            {
                "code": stage.code,
                "name": stage.name,
                "description": stage.description,
                "order_index": stage.order_index,
                "start_day": stage.start_day,
                "end_day": stage.end_day,
            }
            for stage in stages
        ],
    )


async def _persist_insufficient_without_rule(
    db: AsyncSession,
    station_id,
    crop_id,
    summary_date: date,
    planting_date: date | None,
    phenology: dict,
) -> dict:
    result = {
        "level": "DATOS_INSUFICIENTES",
        "simple_explanation": "El cultivo no tiene una versión activa de reglas determinísticas.",
        "conditions": [],
        "conditions_met": 0,
        "conditions_total": 0,
        "completion_percentage": 0,
        "confidence": 0,
        "diseases": [],
        "values_used": {},
        "stage_code": phenology.get("stage_code"),
        "phenology": phenology,
        "rule_version": None,
    }
    existing = await db.scalar(
        select(AlertEvaluation).where(
            AlertEvaluation.station_id == station_id,
            AlertEvaluation.crop_id == crop_id,
            AlertEvaluation.planting_date == planting_date,
            AlertEvaluation.evaluation_date == summary_date,
        )
    )
    if not existing:
        existing = AlertEvaluation(
            station_id=station_id,
            crop_id=crop_id,
            crop_cycle_id=None,
            planting_date=planting_date,
            evaluation_date=summary_date,
        )
        db.add(existing)
    existing.rule_version_id = None
    existing.stage_code = phenology.get("stage_code")
    existing.level = result["level"]
    existing.conditions_met = 0
    existing.conditions_total = 0
    existing.completion_pct = 0
    existing.confidence = 0
    existing.values_used = {}
    existing.diseases = []
    existing.data_sources = []
    existing.explanation = result["simple_explanation"]
    existing.evaluated_at = datetime.now(UTC)
    await db.flush()
    await db.execute(
        delete(AlertConditionResult).where(
            AlertConditionResult.alert_evaluation_id == existing.id
        )
    )
    await db.commit()
    result["alert_id"] = str(existing.id)
    return result


async def evaluate_alert_for_day(
    db: AsyncSession,
    station_id,
    crop_id,
    summary_date: date,
    planting_date: date | None = None,
) -> dict:
    phenology = await _phenology_for_date(db, crop_id, planting_date, summary_date)
    rule = await db.scalar(
        select(CropRuleVersion)
        .where(
            CropRuleVersion.crop_id == crop_id,
            CropRuleVersion.status == "PUBLISHED",
            (
                CropRuleVersion.effective_from.is_(None)
                | (CropRuleVersion.effective_from <= summary_date)
            ),
            (
                CropRuleVersion.effective_to.is_(None)
                | (CropRuleVersion.effective_to >= summary_date)
            ),
        )
        .order_by(
            CropRuleVersion.effective_from.desc().nullslast(),
            CropRuleVersion.created_at.desc(),
        )
    )
    if not rule:
        return await _persist_insufficient_without_rule(
            db,
            station_id,
            crop_id,
            summary_date,
            planting_date,
            phenology,
        )

    summary = await db.scalar(
        select(WeatherDailySummary).where(
            WeatherDailySummary.station_id == station_id,
            WeatherDailySummary.summary_date == summary_date,
        )
    )
    if not summary:
        station = await db.get(Station, station_id)
        if not station:
            raise ValueError("Estación no encontrada")
        summary = await aggregate_station_day(db, station, summary_date)

    conditions = list(
        (
            await db.scalars(
                select(BaseCondition)
                .where(
                    BaseCondition.rule_version_id == rule.id,
                    BaseCondition.active.is_(True),
                )
                .order_by(BaseCondition.order_index)
            )
        ).all()
    )
    disease_rows = list(
        (
            await db.scalars(
                select(DiseaseRule).where(
                    DiseaseRule.rule_version_id == rule.id,
                    DiseaseRule.active.is_(True),
                )
            )
        ).all()
    )
    specs = [
        ConditionSpec(
            key=condition.key,
            label=condition.label,
            variable=condition.variable,
            operator=condition.operator,
            threshold_min=condition.threshold_min,
            threshold_max=condition.threshold_max,
            threshold_value=condition.threshold_value,
            unit=condition.unit,
            required=condition.required,
        )
        for condition in conditions
    ]
    values = {
        "temperature_avg_c": summary.temperature_avg_c,
        "humidity_avg_pct": summary.humidity_avg_pct,
        "rain_sum_mm": summary.rain_sum_mm,
        "soil_temperature_avg_c": summary.soil_temperature_avg_c,
        "soil_moisture_avg_pct": summary.soil_moisture_avg_pct,
        "leaf_wetness_hours": summary.leaf_wetness_hours,
    }
    stage = phenology.get("stage_code")
    disease_rules = [
        {
            "name": disease.name,
            "conditions": disease.conditions,
            "valid_stages": disease.valid_stages,
            "validation_status": disease.validation_status,
            "enabled_for_evaluation": disease.enabled_for_evaluation,
            "risk": "moderado",
            "source_reference": disease.source_reference,
        }
        for disease in disease_rows
    ]
    result = evaluate_daily_alert(
        values,
        specs,
        stage_code=stage,
        disease_rules=disease_rules,
        data_completeness=summary.completeness_pct / 100,
        alert_configuration=rule.configuration,
    )
    existing = await db.scalar(
        select(AlertEvaluation).where(
            AlertEvaluation.station_id == station_id,
            AlertEvaluation.crop_id == crop_id,
            AlertEvaluation.planting_date == planting_date,
            AlertEvaluation.evaluation_date == summary_date,
        )
    )
    if not existing:
        existing = AlertEvaluation(
            station_id=station_id,
            crop_id=crop_id,
            crop_cycle_id=None,
            planting_date=planting_date,
            evaluation_date=summary_date,
        )
        db.add(existing)

    existing.rule_version_id = rule.id
    existing.stage_code = stage
    existing.level = result["level"]
    existing.conditions_met = result["conditions_met"]
    existing.conditions_total = result["conditions_total"]
    existing.completion_pct = result["completion_percentage"]
    existing.confidence = result["confidence"]
    existing.values_used = result["values_used"]
    existing.diseases = result["diseases"]
    existing.data_sources = summary.sources or []
    existing.explanation = result["simple_explanation"]
    existing.evaluated_at = datetime.now(UTC)

    await db.flush()
    await db.execute(
        delete(AlertConditionResult).where(
            AlertConditionResult.alert_evaluation_id == existing.id
        )
    )
    by_key = {condition.key: condition for condition in conditions}
    for item in result["conditions"]:
        matched = by_key.get(item["key"])
        db.add(
            AlertConditionResult(
                alert_evaluation_id=existing.id,
                condition_id=matched.id if matched else None,
                key=item["key"],
                label=item["label"],
                status=item["status"],
                observed_value=item["value"],
                unit=item["unit"],
                explanation=item["explanation"],
            )
        )
    await db.commit()
    result["alert_id"] = str(existing.id)
    result["rule_version"] = rule.version
    result["stage_code"] = stage
    result["phenology"] = phenology
    return result
