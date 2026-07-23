from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import case, select

from app.api.deps import DbSession
from app.db.models import (
    AlertConditionResult,
    AlertEvaluation,
    Crop,
    CropCycle,
    PhenologyStage,
    Plot,
    SatelliteObservation,
    Station,
    WeatherDailySummary,
    WeatherForecast,
    WeatherReading,
)
from app.schemas.resources import PhenologyCalculate
from app.services.agent_runtime import run_agent_for_alert
from app.services.alert_engine import maximum_level
from app.services.phenology import calculate_stage
from app.services.serialization import model_dict

router = APIRouter(tags=["Portal público"])


@router.get("/public/stations")
async def public_stations(db: DbSession) -> list[dict]:
    demo_first = case((Station.code == "DEMO-ASUNCION", 0), else_=1)
    stations = (await db.scalars(select(Station).where(Station.active.is_(True), Station.deleted_at.is_(None)).order_by(demo_first, Station.name))).all()
    return [model_dict(item, exclude={"import_config", "integration_config", "leaf_sensor_config", "created_by", "deleted_at"}) for item in stations]


@router.get("/public/crops")
async def public_crops(db: DbSession) -> list[dict]:
    crops = (await db.scalars(select(Crop).where(Crop.active.is_(True), Crop.deleted_at.is_(None)).order_by(Crop.name))).all()
    return [model_dict(item, exclude={"created_by", "deleted_at"}) for item in crops]


@router.get("/public/crop-cycles")
async def public_cycles(db: DbSession, station_id: str | None = None, crop_code: str | None = None) -> list[dict]:
    query = select(CropCycle, Crop, Station).join(Crop, Crop.id == CropCycle.crop_id).join(Station, Station.id == CropCycle.station_id).where(
        CropCycle.status == "ACTIVE",
        CropCycle.deleted_at.is_(None),
        Crop.active.is_(True),
        Crop.deleted_at.is_(None),
        Station.active.is_(True),
        Station.deleted_at.is_(None),
    )
    if station_id:
        try:
            station_uuid = uuid.UUID(station_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="ID de estación inválido") from exc
        query = query.where(CropCycle.station_id == station_uuid)
    if crop_code:
        query = query.where(Crop.code == crop_code.upper())
    rows = (await db.execute(query.order_by(CropCycle.planting_date.desc()))).all()
    return [{**model_dict(cycle), "crop_name": crop.name, "crop_code": crop.code, "station_name": station.name} for cycle, crop, station in rows]


def _period_start(period: str) -> datetime:
    hours = {"24h": 24, "72h": 72, "7d": 168, "30d": 720}.get(period, 72)
    return datetime.now(UTC) - timedelta(hours=hours)


def _metric(key: str, label: str, value, unit: str, source: str | None) -> dict:
    origin_map = {"measured": "measured", "imported": "imported", "estimated": "estimated", "forecast": "forecast"}
    return {"key": key, "label": label, "value": None if value is None else round(float(value), 2), "unit": unit, "origin": origin_map.get(source or "", "unavailable") if value is not None else "unavailable"}


@router.get("/public/dashboard")
async def dashboard(
    db: DbSession,
    station_id: str,
    crop_code: str = "SOJA",
    cycle_id: str | None = None,
    period: str = Query(default="72h", pattern="^(24h|72h|7d|30d)$"),
) -> dict:
    try:
        station_uuid = uuid.UUID(station_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="ID de estación inválido") from exc
    station = await db.get(Station, station_uuid)
    crop = await db.scalar(select(Crop).where(Crop.code == crop_code.upper(), Crop.active.is_(True), Crop.deleted_at.is_(None)))
    if not station or not station.active:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    if not crop:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    try:
        cycle_uuid = uuid.UUID(cycle_id) if cycle_id else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="ID de ciclo inválido") from exc
    cycle = await db.get(CropCycle, cycle_uuid) if cycle_uuid else None
    if cycle_uuid and (not cycle or cycle.deleted_at is not None or cycle.status != "ACTIVE"):
        raise HTTPException(status_code=404, detail="Ciclo productivo no encontrado")
    if cycle and (cycle.station_id != station.id or cycle.crop_id != crop.id):
        raise HTTPException(status_code=400, detail="El ciclo productivo no corresponde a la estación y cultivo seleccionados")
    latest = await db.scalar(select(WeatherReading).where(WeatherReading.station_id == station.id).order_by(WeatherReading.sampled_at.desc()).limit(1))
    start = _period_start(period)
    readings = (await db.scalars(select(WeatherReading).where(WeatherReading.station_id == station.id, WeatherReading.sampled_at >= start).order_by(WeatherReading.sampled_at))).all()
    now = datetime.now(UTC)
    if not latest:
        connection = "no_data"
        age_minutes = None
    else:
        age_minutes = round((now - latest.sampled_at).total_seconds() / 60, 1)
        expected = max(1, station.expected_interval_minutes)
        connection = "online" if age_minutes <= expected * 3 else "delayed" if age_minutes <= expected * 12 else "offline"
    metrics = [] if not latest else [
        _metric("temperature", "Temperatura", latest.temperature_c, "°C", latest.source),
        _metric("feels_like", "Sensación térmica", latest.feels_like_c, "°C", latest.source),
        _metric("heat_index", "Índice de calor", latest.heat_index_c, "°C", latest.source),
        _metric("humidity", "Humedad relativa", latest.humidity_pct, "%", latest.source),
        _metric("dew_point", "Punto de rocío", latest.dew_point_c, "°C", latest.source),
        _metric("wind", "Viento", latest.wind_speed_ms, "m/s", latest.source),
        _metric("wind_gust", "Ráfaga", latest.wind_gust_ms, "m/s", latest.source),
        _metric("wind_direction", "Dirección del viento", latest.wind_direction_deg, "°", latest.source),
        _metric("rain", "Lluvia", latest.rain_mm, "mm", latest.source),
        _metric("total_rain", "Lluvia acumulada", latest.total_rain_mm, "mm", latest.source),
        _metric("pressure", "Presión", latest.pressure_hpa, "hPa", latest.source),
        _metric("solar", "Radiación solar", latest.solar_wm2, "W/m²", latest.source),
        _metric("et", "Evapotranspiración", latest.evapotranspiration_mm, "mm", latest.source),
        _metric("uv", "Índice UV", latest.uv_index, "", latest.source),
        _metric("soil_temperature", "Temperatura del suelo", latest.soil_temperature_c, "°C", latest.source),
        _metric("soil_moisture", "Humedad del suelo", latest.soil_moisture_pct, "%", latest.source),
        _metric("leaf_sensor", "Sensor de hoja", latest.leaf_sensor_value, "", latest.source),
    ]
    if not latest:
        metrics = [_metric(key, label, None, unit, None) for key, label, unit in [
            ("temperature", "Temperatura", "°C"), ("feels_like", "Sensación térmica", "°C"), ("humidity", "Humedad relativa", "%"), ("dew_point", "Punto de rocío", "°C"), ("wind", "Viento", "m/s"), ("rain", "Lluvia", "mm"), ("pressure", "Presión", "hPa"), ("solar", "Radiación solar", "W/m²"), ("et", "Evapotranspiración", "mm"), ("uv", "Índice UV", ""), ("soil_temperature", "Temperatura del suelo", "°C"), ("soil_moisture", "Humedad del suelo", "%"), ("leaf_sensor", "Sensor de hoja", "")]]
    local_today = datetime.now(ZoneInfo(station.timezone or "America/Asuncion")).date()
    alert_query = select(AlertEvaluation).where(AlertEvaluation.station_id == station.id, AlertEvaluation.crop_id == crop.id, AlertEvaluation.evaluation_date >= local_today - timedelta(days=2))
    if cycle:
        alert_query = alert_query.where(AlertEvaluation.crop_cycle_id == cycle.id)
    else:
        alert_query = alert_query.where(AlertEvaluation.crop_cycle_id.is_(None))
    evaluations = (await db.scalars(alert_query.order_by(AlertEvaluation.evaluation_date.desc()))).all()
    current_eval = next((item for item in evaluations if item.level != "DATOS_INSUFICIENTES"), evaluations[0] if evaluations else None)
    if current_eval:
        condition_rows = (await db.scalars(select(AlertConditionResult).where(AlertConditionResult.alert_evaluation_id == current_eval.id).order_by(AlertConditionResult.key))).all()
        conditions = [{"key": c.key, "label": c.label, "status": c.status, "value": c.observed_value, "unit": c.unit, "explanation": c.explanation} for c in condition_rows]
        current_alert = {"current_level": current_eval.level, "maximum_level_72h": maximum_level([{"level": e.level} for e in evaluations]), "conditions_met": current_eval.conditions_met, "completion_percentage": current_eval.completion_pct, "confidence": current_eval.confidence, "evaluated_at": current_eval.evaluated_at.isoformat(), "conditions": conditions, "diseases": current_eval.diseases, "simple_explanation": current_eval.explanation or "Resultado determinístico disponible."}
    else:
        explanation = "Configuración pendiente para este cultivo." if crop.rules_status != "active" else "Aún no hay evaluaciones calculadas para los filtros seleccionados."
        current_alert = {"current_level": "DATOS_INSUFICIENTES", "maximum_level_72h": "DATOS_INSUFICIENTES", "conditions_met": 0, "completion_percentage": 0, "confidence": 0, "evaluated_at": None, "conditions": [], "diseases": [], "simple_explanation": explanation}
    latest_summary = await db.scalar(select(WeatherDailySummary).where(WeatherDailySummary.station_id == station.id).order_by(WeatherDailySummary.summary_date.desc()).limit(1))
    required_missing = [item["label"] for item in current_alert["conditions"] if item["status"] == "missing"]
    quality = {"completion_percentage": round(latest_summary.completeness_pct, 1) if latest_summary else 0, "missing_variables": required_missing, "outliers": latest_summary.outlier_count if latest_summary else 0}
    forecasts = (await db.scalars(select(WeatherForecast).where(WeatherForecast.station_id == station.id, WeatherForecast.forecast_date >= local_today).order_by(WeatherForecast.forecast_date).limit(7))).all()
    forecast_data = [{"date": item.forecast_date.isoformat(), "temperature_min_c": item.temperature_min_c, "temperature_max_c": item.temperature_max_c, "precipitation_mm": item.precipitation_mm, "precipitation_probability_pct": item.precipitation_probability_pct, "humidity_pct": item.humidity_pct, "wind_speed_ms": item.wind_speed_ms, "evapotranspiration_mm": item.evapotranspiration_mm, "provider": item.provider} for item in forecasts]
    satellite = None
    if cycle and cycle.plot_id:
        observation = await db.scalar(select(SatelliteObservation).where(SatelliteObservation.plot_id == cycle.plot_id).order_by(SatelliteObservation.captured_at.desc()).limit(1))
        if observation:
            satellite = model_dict(observation)
    recommendation = None
    if crop.rules_status == "active" and current_eval:
        recommendation = await run_agent_for_alert(
            db,
            current_eval.id,
            forecast=forecast_data,
            satellite=satellite,
        )
    series = [{"sampled_at": row.sampled_at.isoformat(), "temperature_c": row.temperature_c, "humidity_pct": row.humidity_pct, "rain_mm": row.rain_mm, "soil_temperature_c": row.soil_temperature_c, "soil_moisture_pct": row.soil_moisture_pct, "leaf_sensor_value": row.leaf_sensor_value, "solar_wm2": row.solar_wm2, "evapotranspiration_mm": row.evapotranspiration_mm} for row in readings]
    return {"station": model_dict(station, exclude={"import_config", "integration_config", "leaf_sensor_config", "created_by", "deleted_at"}), "latest_reading_at": latest.sampled_at.isoformat() if latest else None, "reading_age_minutes": age_minutes, "connection_status": connection, "metrics": metrics, "alert": current_alert, "series": series, "forecast": forecast_data, "satellite": satellite, "recommendation": recommendation, "data_quality": quality}


@router.post("/phenology/calculate")
async def phenology_calculate(payload: PhenologyCalculate, db: DbSession) -> dict:
    crop = await db.scalar(select(Crop).where(Crop.code == payload.crop_code.upper(), Crop.active.is_(True)))
    if not crop:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    stages = (await db.scalars(select(PhenologyStage).where(PhenologyStage.crop_id == crop.id, PhenologyStage.active.is_(True), PhenologyStage.cycle_type == payload.cycle_type).order_by(PhenologyStage.order_index))).all()
    return calculate_stage(payload.planting_date, datetime.now(ZoneInfo("America/Asuncion")).date(), [{"code": s.code, "name": s.name, "description": s.description, "order_index": s.order_index, "start_day": s.start_day, "end_day": s.end_day} for s in stages])
