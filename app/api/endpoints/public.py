from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import case, select

from app.api.deps import DbSession
from app.core.config import settings
from app.db.models import (
    AlertConditionResult,
    AlertEvaluation,
    BaseCondition,
    Crop,
    CropRuleVersion,
    PhenologyStage,
    SatelliteObservation,
    Station,
    WeatherDailySummary,
    WeatherForecast,
    WeatherReading,
)
from app.schemas.actions import (
    PublicSatelliteMapAgentPayload,
    PublicSatelliteMapProcessPayload,
    PublicSatelliteRefreshPayload,
)
from app.schemas.resources import PhenologyCalculate
from app.services.alert_engine import classify_alert_level, maximum_level
from app.services.disease_forecast import build_forecast_risk
from app.services.forecast import refresh_station_forecast
from app.services.phenology import calculate_stage
from app.services.satellite import (
    INDEX_NAMES,
    _integration,
    copernicus_credentials_configured,
    latest_satellite_observation,
    local_satellite_image_path,
    local_satellite_index_image_path,
    satellite_catalog_cache_is_stale,
    satellite_observation_payload,
    satellite_runtime_config,
    sync_satellite_for_station,
)
from app.services.satellite_map import build_satellite_map_context, generate_satellite_map_agent
from app.services.serialization import model_dict
from app.services.source_refresh import refresh_station_source_if_needed
from app.services.weather import (
    aggregate_range,
    effective_soil_moisture_unit,
    evaluate_alert_for_day,
    summarize_readings_for_conditions,
)
from app.services.year_comparison import build_year_comparison

router = APIRouter(tags=["Portal público"])


def _dashboard_evaluation_for_window(
    evaluations: list[AlertEvaluation],
) -> tuple[AlertEvaluation | None, str]:
    """Select the evaluation represented by the 72-hour alert card.

    The dashboard headline is the worst level in the window. Its condition
    details and the agent evaluation id must therefore come from that same
    daily evaluation; mixing the latest day with the worst headline produced
    combinations such as “Alerta grave” with a moderate explanation.
    Evaluations arrive newest first, so ties keep the most recent occurrence.
    """
    maximum_code = maximum_level(
        [{"level": evaluation.level} for evaluation in evaluations]
    )
    selected = next(
        (evaluation for evaluation in evaluations if evaluation.level == maximum_code),
        evaluations[0] if evaluations else None,
    )
    return selected, maximum_code


@router.get("/public/stations")
async def public_stations(db: DbSession) -> list[dict]:
    demo_first = case((Station.code == "DEMO-ASUNCION", 0), else_=1)
    stations = (
        await db.scalars(
            select(Station)
            .where(Station.active.is_(True), Station.deleted_at.is_(None))
            .order_by(demo_first, Station.name)
        )
    ).all()
    return [
        model_dict(
            item,
            exclude={
                "import_config",
                "integration_config",
                "leaf_sensor_config",
                "created_by",
                "deleted_at",
            },
        )
        for item in stations
    ]


@router.get("/public/crops")
async def public_crops(db: DbSession) -> list[dict]:
    wheat_first = case((Crop.code == "TRIGO", 0), else_=1)
    crops = (
        await db.scalars(
            select(Crop)
            .where(Crop.active.is_(True), Crop.deleted_at.is_(None))
            .order_by(wheat_first, Crop.name)
        )
    ).all()
    return [model_dict(item, exclude={"created_by", "deleted_at"}) for item in crops]


@router.get("/public/satellite-map")
async def public_satellite_map(
    station_id: uuid.UUID,
    crop_code: str,
    db: DbSession,
    observation_id: uuid.UUID | None = None,
    history_days: int | None = Query(default=None, ge=7, le=366),
) -> dict:
    try:
        return await build_satellite_map_context(
            db,
            station_id,
            crop_code,
            observation_id=observation_id,
            history_days=history_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/public/satellite-map/process")
async def public_satellite_map_process(
    payload: PublicSatelliteMapProcessPayload,
    db: DbSession,
) -> dict:
    crop = await db.scalar(
        select(Crop).where(
            Crop.code == payload.crop_code.upper(),
            Crop.active.is_(True),
            Crop.deleted_at.is_(None),
        )
    )
    if not crop:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    try:
        result = await sync_satellite_for_station(
            db,
            payload.station_id,
            crop_id=crop.id,
            # Un usuario público no puede forzar llamadas repetidas al proveedor.
            # Las solicitudes manuales de escena/fecha siguen procesándose porque
            # sync_satellite_for_station las identifica como explícitas.
            force=False,
            target_date=payload.captured_date,
            scene_id=payload.scene_id,
        )
        observation_id = result.get("observation_id")
        context = await build_satellite_map_context(
            db,
            payload.station_id,
            payload.crop_code,
            observation_id=uuid.UUID(observation_id) if observation_id else None,
            history_days=payload.history_days,
        )
        return {"result": result, "context": context}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/public/satellite-map/agent")
async def public_satellite_map_agent(
    payload: PublicSatelliteMapAgentPayload,
    db: DbSession,
) -> dict:
    try:
        return await generate_satellite_map_agent(
            db,
            payload.station_id,
            payload.crop_code,
            payload.observation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/public/crop-cycles", deprecated=True)
async def public_cycles_legacy() -> list[dict]:
    """Legacy endpoint kept temporarily for older frontends.

    The operational workflow now receives the planting date directly from the
    user and no longer requires crop-cycle maintenance.
    """

    return []


def _period_start(period: str) -> datetime:
    hours = {"24h": 24, "72h": 72, "7d": 168, "30d": 720}.get(period, 72)
    return datetime.now(UTC) - timedelta(hours=hours)


def _metric(
    key: str,
    label: str,
    value,
    unit: str,
    source: str | None,
    *,
    interpretation: dict | None = None,
) -> dict:
    origin_map = {
        "measured": "measured",
        "imported": "imported",
        "estimated": "estimated",
        "forecast": "forecast",
    }
    payload = {
        "key": key,
        "label": label,
        "value": None if value is None else round(float(value), 2),
        "unit": unit,
        "origin": (
            origin_map.get(source or "", "unavailable")
            if value is not None
            else "unavailable"
        ),
    }
    if interpretation:
        payload["interpretation"] = interpretation
    return payload


def _soil_moisture_presentation(station: Station) -> tuple[str, dict | None]:
    unit = effective_soil_moisture_unit(station)
    if unit == "cb":
        return "cb", {
            "title": "Interpretación de tensión de humedad",
            "minimum": 0,
            "maximum": 200,
            "unit": "cb",
            "ranges": [
                {"from": 0, "to": 10, "label": "Suelo saturado", "description": "Habitual justo después de lluvia abundante o riego copioso.", "color": "#4aa8d8"},
                {"from": 10, "to": 30, "label": "Capacidad de campo", "description": "Agua disponible en un rango favorable sin saturar las raíces.", "color": "#4fbd72"},
                {"from": 30, "to": 60, "label": "Rango normal de riego", "description": "Momento habitual para valorar el inicio del riego según cultivo y suelo.", "color": "#f1c84b"},
                {"from": 60, "to": 100, "label": "Suelo seco", "description": "Puede indicar estrés hídrico, salvo cultivos tolerantes o suelos muy arcillosos.", "color": "#ef9842"},
                {"from": 100, "to": 200, "label": "Severamente seco", "description": "La extracción de agua es difícil y aumenta el riesgo de marchitez.", "color": "#d85a52"},
            ],
            "caution": "",
            "source_url": "https://csrlaboratorio.es/laboratorio/aguas/aguas-de-riego/riego-vii-cuanto-y-cuando-tengo-que-regar/",
        }
    return "%", None


async def _phenology(db: DbSession, crop_id, planting_date: date, on_date: date) -> dict:
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


@router.get("/public/satellite/{observation_id}/image.jpg")
async def public_satellite_image(observation_id: uuid.UUID, db: DbSession):
    observation = await db.get(SatelliteObservation, observation_id)
    if not observation:
        raise HTTPException(status_code=404, detail="Imagen satelital no encontrada")
    path = local_satellite_image_path(observation)
    if not path:
        raise HTTPException(status_code=404, detail="La observación no tiene una imagen local procesada")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/public/satellite/{observation_id}/indices/{index_name}.png")
async def public_satellite_index_image(observation_id: uuid.UUID, index_name: str, db: DbSession):
    normalized = index_name.strip().lower()
    if normalized not in INDEX_NAMES:
        raise HTTPException(status_code=404, detail="Índice satelital no soportado")
    observation = await db.get(SatelliteObservation, observation_id)
    if not observation:
        raise HTTPException(status_code=404, detail="Observación satelital no encontrada")
    path = local_satellite_index_image_path(observation, normalized)
    if not path:
        raise HTTPException(status_code=404, detail=f"La observación no tiene la capa {normalized.upper()} procesada")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/public/dashboard")
async def dashboard(
    db: DbSession,
    station_id: str,
    planting_date: date,
    crop_code: str = "SOJA",
    period: str = Query(default="72h", pattern="^(24h|72h|7d|30d)$"),
    refresh_source: bool = Query(
        default=False,
        description="Actualiza FECOCLIMA antes de analizar. Solo debe activarse desde el botón explícito de análisis.",
    ),
) -> dict:
    try:
        station_uuid = uuid.UUID(station_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="ID de estación inválido") from exc

    station = await db.get(Station, station_uuid)
    crop = await db.scalar(
        select(Crop).where(
            Crop.code == crop_code.upper(),
            Crop.active.is_(True),
            Crop.deleted_at.is_(None),
        )
    )
    if not station or not station.active or station.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    if not crop:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")

    timezone = ZoneInfo(station.timezone or "America/Asuncion")
    local_today = datetime.now(timezone).date()
    if planting_date > local_today:
        raise HTTPException(
            status_code=422,
            detail="La fecha de siembra no puede ser posterior a la fecha actual",
        )

    # The explicit Analyze button may request a bounded FECOCLIMA refresh.
    # Upstream failures never erase or block the cached analysis: the refresh
    # service returns diagnostic metadata and the dashboard continues with the
    # latest readings already stored in PostgreSQL.
    source_sync = await refresh_station_source_if_needed(
        db,
        station,
        requested=bool(refresh_source and settings.public_analysis_sync_enabled),
        # El botón “Analizar con IA” es una acción explícita del usuario. Cada
        # consulta comprueba el origen FECOCLIMA antes de usar la caché local.
        force=bool(refresh_source and settings.public_analysis_sync_enabled),
    )

    latest = await db.scalar(
        select(WeatherReading)
        .where(WeatherReading.station_id == station.id)
        .order_by(WeatherReading.sampled_at.desc())
        .limit(1)
    )
    # Anchor the deterministic observation to the newest real station sample.
    # Otherwise, an upstream delay that crosses midnight creates an empty
    # "today" evaluation and hides the actual conditions from the agent.
    observation_date = (
        latest.sampled_at.astimezone(timezone).date()
        if latest
        else local_today
    )
    evaluation_end = min(local_today, observation_date)

    # The public dashboard is self-sufficient: it creates/refreshes daily
    # aggregates and deterministic evaluations for the selected planting date.
    evaluation_start = max(planting_date, evaluation_end - timedelta(days=2))
    await aggregate_range(db, station.id, evaluation_start, evaluation_end)
    current = evaluation_start
    while current <= evaluation_end:
        await evaluate_alert_for_day(
            db,
            station.id,
            crop.id,
            current,
            planting_date=planting_date,
        )
        current += timedelta(days=1)

    hours = {"24h": 24, "72h": 72, "7d": 168, "30d": 720}.get(period, 72)
    period_end = latest.sampled_at if latest else datetime.now(UTC)
    start = period_end - timedelta(hours=hours)
    readings = (
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
    # Keep synchronization counts separate from the exact series used by the
    # selected dashboard period. The upstream API is synchronized by local
    # calendar dates, while the analysis itself is a rolling timestamp window.
    source_sync.update(
        analysis_period=period,
        analysis_from=start.isoformat(),
        analysis_to=period_end.isoformat(),
        analysis_readings=len(readings),
    )
    period_condition_values = summarize_readings_for_conditions(list(readings), station)

    now = datetime.now(UTC)
    if not latest:
        connection = "no_data"
        age_minutes = None
    else:
        age_minutes = round((now - latest.sampled_at).total_seconds() / 60, 1)
        expected = max(1, station.expected_interval_minutes)
        connection = (
            "online"
            if age_minutes <= expected * 3
            else "delayed"
            if age_minutes <= expected * 12
            else "offline"
        )

    soil_unit, soil_interpretation = _soil_moisture_presentation(station)
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
        _metric("leaf_sensor", "Sensor de hoja", latest.leaf_sensor_value, "", latest.source),
        _metric(
            "soil_moisture",
            "Humedad del suelo",
            latest.soil_moisture_pct,
            soil_unit,
            latest.source,
            interpretation=soil_interpretation,
        ),
        
    ]
    if not latest:
        metrics = [
            _metric(key, label, None, unit, None)
            for key, label, unit in [
                ("temperature", "Temperatura", "°C"),
                ("feels_like", "Sensación térmica", "°C"),
                ("humidity", "Humedad relativa", "%"),
                ("dew_point", "Punto de rocío", "°C"),
                ("wind", "Viento", "m/s"),
                ("rain", "Lluvia", "mm"),
                ("pressure", "Presión", "hPa"),
                ("solar", "Radiación solar", "W/m²"),
                ("et", "Evapotranspiración", "mm"),
                ("uv", "Índice UV", ""),
                ("soil_temperature", "Temperatura del suelo", "°C"),
                ("soil_moisture", "Humedad del suelo", soil_unit),
                ("leaf_sensor", "Sensor de hoja", ""),
            ]
        ]

    evaluations = list(
        (
            await db.scalars(
                select(AlertEvaluation)
                .where(
                    AlertEvaluation.station_id == station.id,
                    AlertEvaluation.crop_id == crop.id,
                    AlertEvaluation.planting_date == planting_date,
                    AlertEvaluation.evaluation_date >= evaluation_start,
                    AlertEvaluation.evaluation_date <= evaluation_end,
                )
                .order_by(AlertEvaluation.evaluation_date.desc())
            )
        ).all()
    )
    latest_eval = evaluations[0] if evaluations else None
    display_eval, maximum_code = _dashboard_evaluation_for_window(evaluations)
    if display_eval:
        active_rule = await db.get(CropRuleVersion, display_eval.rule_version_id) if display_eval.rule_version_id else None
        base_conditions = list(
            (
                await db.scalars(
                    select(BaseCondition)
                    .where(
                        BaseCondition.rule_version_id == display_eval.rule_version_id,
                        BaseCondition.active.is_(True),
                    )
                    .order_by(BaseCondition.order_index)
                )
            ).all()
        ) if display_eval.rule_version_id else []
        specs_by_id = {item.id: item for item in base_conditions}
        specs_by_key = {item.key: item for item in base_conditions}
        condition_rows = list(
            (
                await db.scalars(
                    select(AlertConditionResult)
                    .where(AlertConditionResult.alert_evaluation_id == display_eval.id)
                    .order_by(AlertConditionResult.key)
                )
            ).all()
        )
        conditions = []
        for condition in condition_rows:
            spec = specs_by_id.get(condition.condition_id) or specs_by_key.get(condition.key)
            conditions.append({
                "key": condition.key,
                "label": condition.label,
                "status": condition.status,
                "value": condition.observed_value,
                "unit": condition.unit,
                "explanation": condition.explanation,
                "variable": spec.variable if spec else None,
                "operator": spec.operator if spec else None,
                "threshold_min": spec.threshold_min if spec else None,
                "threshold_max": spec.threshold_max if spec else None,
                "threshold_value": spec.threshold_value if spec else None,
                "period_value": period_condition_values.get(spec.variable) if spec else None,
                "period_aggregation": (
                    "sum" if spec and spec.variable in {
                        "rain_sum_mm",
                        "leaf_wetness_hours",
                        "solar_sum_wm2_samples",
                        "evapotranspiration_sum_mm",
                    } else "mean"
                ),
            })
        classification = classify_alert_level(
            display_eval.conditions_met,
            display_eval.conditions_total,
            display_eval.completion_pct,
            active_rule.configuration if active_rule else None,
            missing_required=any(item["status"] == "missing" for item in conditions),
        )
        maximum_settings = classification["configuration"]["levels"].get(
            maximum_code,
            classification["configuration"]["levels"]["DATOS_INSUFICIENTES"],
        )
        current_alert = {
            # El agente recibe exactamente la evaluación que representa el
            # encabezado “Peor nivel · últimas 72 horas”.
            "evaluation_id": str(display_eval.id),
            "rule_version_id": str(display_eval.rule_version_id) if display_eval.rule_version_id else None,
            "rule_version": active_rule.version if active_rule else None,
            "current_evaluation_id": str(latest_eval.id) if latest_eval else None,
            "current_level": latest_eval.level if latest_eval else "DATOS_INSUFICIENTES",
            "level_label": classification["label"],
            "level_color": classification["color"],
            "maximum_level_72h": maximum_code,
            "maximum_level_label": maximum_settings.get("label", maximum_code.title()),
            "maximum_level_color": maximum_settings.get("color", "gray"),
            "conditions_met": display_eval.conditions_met,
            "conditions_total": display_eval.conditions_total,
            "completion_percentage": display_eval.completion_pct,
            "confidence": display_eval.confidence,
            "evaluation_date": display_eval.evaluation_date.isoformat(),
            "evaluated_at": display_eval.evaluated_at.isoformat(),
            "conditions": conditions,
            "diseases": display_eval.diseases,
            "simple_explanation": display_eval.explanation
            or "Resultado determinístico disponible.",
        }
    else:
        current_alert = {
            "evaluation_id": None,
            "rule_version_id": None,
            "rule_version": None,
            "current_evaluation_id": None,
            "current_level": "DATOS_INSUFICIENTES",
            "level_label": "Datos insuficientes",
            "level_color": "gray",
            "maximum_level_72h": "DATOS_INSUFICIENTES",
            "maximum_level_label": "Datos insuficientes",
            "maximum_level_color": "gray",
            "conditions_met": 0,
            "conditions_total": 0,
            "completion_percentage": 0,
            "confidence": 0,
            "evaluation_date": None,
            "evaluated_at": None,
            "conditions": [],
            "diseases": [],
            "simple_explanation": "No fue posible crear una evaluación para los filtros seleccionados.",
        }

    latest_summary = await db.scalar(
        select(WeatherDailySummary)
        .where(WeatherDailySummary.station_id == station.id)
        .order_by(WeatherDailySummary.summary_date.desc())
        .limit(1)
    )
    required_missing = [
        item["label"]
        for item in current_alert["conditions"]
        if item["status"] == "missing"
    ]
    leaf_config = station.leaf_sensor_config or {}
    quality_notes = [
        "La completitud de registros compara lecturas recibidas contra la frecuencia esperada hasta la hora actual.",
        "La completitud de variables indica cuántas de las seis condiciones pudieron evaluarse.",
    ]
    if soil_unit == "cb":
        quality_notes.append(
            "humedadSuelo se presenta como tensión en centibares (cb) según la configuración de la estación; los umbrales de reglas deben conservar esa misma unidad."
        )
    if leaf_config.get("mode") == "threshold" and leaf_config.get("wet_threshold") is not None:
        quality_notes.append(
            f"sensorHoja=0 es hoja seca válida; una muestra se cuenta como mojada desde {leaf_config['wet_threshold']} y se convierte a horas usando la frecuencia de la estación."
        )
    quality = {
        "completion_percentage": round(latest_summary.completeness_pct, 1) if latest_summary else 0,
        "record_completeness_percentage": round(latest_summary.completeness_pct, 1) if latest_summary else 0,
        "variable_completeness_percentage": current_alert["completion_percentage"],
        "missing_variables": required_missing,
        "outliers": latest_summary.outlier_count if latest_summary else 0,
        "notes": quality_notes,
    }

    forecast_status = {
        "configured": station.latitude is not None and station.longitude is not None,
        "provider": "Open-Meteo",
        "refreshed": False,
        "error": None,
        "coordinate_source": (station.integration_config or {}).get("coordinate_source", "station_coordinates"),
    }
    forecasts = list(
        (
            await db.scalars(
                select(WeatherForecast)
                .where(
                    WeatherForecast.station_id == station.id,
                    WeatherForecast.forecast_date >= local_today,
                )
                .order_by(WeatherForecast.forecast_date)
                .limit(7)
            )
        ).all()
    )
    stale_forecast = not forecasts or max(item.generated_at for item in forecasts) < datetime.now(UTC) - timedelta(hours=6)
    if forecast_status["configured"] and stale_forecast:
        try:
            await refresh_station_forecast(db, station, days=7)
            forecast_status["refreshed"] = True
            forecasts = list(
                (
                    await db.scalars(
                        select(WeatherForecast)
                        .where(
                            WeatherForecast.station_id == station.id,
                            WeatherForecast.forecast_date >= local_today,
                        )
                        .order_by(WeatherForecast.forecast_date)
                        .limit(7)
                    )
                ).all()
            )
        except Exception as exc:
            forecast_status["error"] = str(exc)
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
            "leaf_wetness_method": (item.raw_payload or {}).get("leaf_wetness_method"),
            "limitations": (item.raw_payload or {}).get("limitations", []),
            "provider": item.provider,
        }
        for item in forecasts
    ]
    forecast_risk = await build_forecast_risk(
        db,
        crop.id,
        planting_date,
        forecasts,
    )

    phenology = await _phenology(db, crop.id, planting_date, evaluation_end)

    satellite = None
    plot, observation = await latest_satellite_observation(db, station.id, crop.id)
    plot_has_geometry = bool(
        plot
        and (
            plot.geojson
            or (plot.latitude is not None and plot.longitude is not None)
        )
    )
    satellite_status = {
        "configured": bool(
            station.influence_geojson
            or plot_has_geometry
            or (station.latitude is not None and station.longitude is not None)
        ),
        "catalog": "COPERNICUS_DATA_SPACE",
        "processing_credentials": await copernicus_credentials_configured(db),
        "refresh_result": None,
        "error": None,
    }
    satellite_runtime = satellite_runtime_config(await _integration(db))
    stale_satellite = satellite_catalog_cache_is_stale(observation, satellite_runtime)
    if satellite_status["configured"] and stale_satellite:
        try:
            satellite_status["refresh_result"] = await sync_satellite_for_station(
                db,
                station.id,
                crop_id=crop.id,
            )
            plot, observation = await latest_satellite_observation(db, station.id, crop.id)
        except Exception as exc:
            satellite_status["error"] = str(exc)
    if observation and plot:
        satellite_status["processing_credentials"] = await copernicus_credentials_configured(db)
        satellite = await satellite_observation_payload(db, observation, plot)

    recommendation = None
    year_comparison = await build_year_comparison(
        db,
        station,
        period=period,
        latest_reading=latest,
        refresh_historical_if_missing=bool(refresh_source and settings.public_analysis_sync_enabled),
    )

    series = [
        {
            "sampled_at": row.sampled_at.isoformat(),
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
    return {
        "station": model_dict(
            station,
            exclude={
                "import_config",
                "integration_config",
                "leaf_sensor_config",
                "created_by",
                "deleted_at",
            },
        ),
        "crop": model_dict(crop, exclude={"created_by", "deleted_at"}),
        "planting_date": planting_date.isoformat(),
        "observation_date": evaluation_end.isoformat(),
        "phenology": phenology,
        "latest_reading_at": latest.sampled_at.isoformat() if latest else None,
        "reading_age_minutes": age_minutes,
        "connection_status": connection,
        "source_sync": source_sync,
        "metrics": metrics,
        "alert": current_alert,
        "series": series,
        "year_comparison": year_comparison,
        "forecast": forecast_data,
        "forecast_status": forecast_status,
        "forecast_risk": forecast_risk,
        "satellite": satellite,
        "satellite_status": satellite_status,
        "recommendation": recommendation,
        "data_quality": quality,
        "analysis_engine": {
            "aggregation": "pandas",
            "numerical_processing": "numpy",
            "alert_method": "deterministic_rule_engine_configured_in_admin",
            "agent_role": "manual_explanation_chat_and_image_analysis",
            "agent_trigger": "explicit_button",
        },
        "analysis_generated_at": datetime.now(UTC).isoformat(),
    }


@router.post("/public/satellite/refresh")
async def public_satellite_refresh(
    payload: PublicSatelliteRefreshPayload,
    db: DbSession,
) -> dict:
    station = await db.get(Station, payload.station_id)
    crop = await db.scalar(
        select(Crop).where(
            Crop.code == payload.crop_code.upper(),
            Crop.active.is_(True),
            Crop.deleted_at.is_(None),
        )
    )
    if not station or not station.active or station.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    if not crop:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    plot, _ = await latest_satellite_observation(db, station.id, crop.id)
    plot_has_geometry = bool(
        plot
        and (
            plot.geojson
            or (plot.latitude is not None and plot.longitude is not None)
        )
    )
    if not station.influence_geojson and not plot_has_geometry and (station.latitude is None or station.longitude is None):
        raise HTTPException(
            status_code=422,
            detail="La estación necesita coordenadas o una parcela geográfica para consultar Copernicus",
        )

    try:
        refresh_result = await sync_satellite_for_station(
            db,
            station.id,
            crop_id=crop.id,
            # Protección de cuota y concurrencia: el público reutiliza la última
            # verificación compartida dentro de refresh_hours.
            force=False,
        )
        plot, observation = await latest_satellite_observation(db, station.id, crop.id)
        satellite = (
            await satellite_observation_payload(db, observation, plot)
            if observation and plot
            else None
        )
        return {
            "refresh_result": refresh_result,
            "satellite": satellite,
            "satellite_status": {
                "configured": True,
                "catalog": "COPERNICUS_DATA_SPACE",
                "processing_credentials": await copernicus_credentials_configured(db),
                "refresh_result": refresh_result,
                "error": None,
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Copernicus no pudo actualizar la geozona: {exc}") from exc


@router.post("/phenology/calculate")
async def phenology_calculate(payload: PhenologyCalculate, db: DbSession) -> dict:
    crop = await db.scalar(
        select(Crop).where(
            Crop.code == payload.crop_code.upper(),
            Crop.active.is_(True),
            Crop.deleted_at.is_(None),
        )
    )
    if not crop:
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    on_date = payload.on_date or datetime.now(ZoneInfo("America/Asuncion")).date()
    return await _phenology(db, crop.id, payload.planting_date, on_date)
