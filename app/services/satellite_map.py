from __future__ import annotations

import base64
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import math
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AIAgentRun,
    Crop,
    Plot,
    SatelliteObservation,
    Station,
    WeatherDailySummary,
    WeatherForecast,
    WeatherReading,
)
from app.services.agent_runtime import select_agent_version
from app.services.ai import generate_markdown_response, provider_fallback_reason
from app.services.rag import retrieve_chunks
from app.services.satellite import (
    CatalogTemporaryError,
    INDEX_NAMES,
    _feature_cloud,
    _feature_datetime,
    _feature_preview,
    _integration,
    _polygon_geometry,
    catalog_scenes_for_station,
    ensure_station_plot,
    local_satellite_image_path,
    satellite_observation_is_complete,
    satellite_observation_payload,
    satellite_runtime_config,
    station_geometry,
)


def _satellite_response_policy(output_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Read optional CMS response controls without depending on a specific agent-runtime revision."""
    configured = (output_schema or {}).get("response_policy") if isinstance(output_schema, dict) else {}
    configured = configured if isinstance(configured, dict) else {}
    try:
        max_words = int(configured.get("max_words") or 280)
    except (TypeError, ValueError):
        max_words = 280
    try:
        max_sections = int(configured.get("max_sections") or 4)
    except (TypeError, ValueError):
        max_sections = 4
    return {
        "max_words": min(max(max_words, 120), 360),
        "max_sections": min(max(max_sections, 2), 4),
        "tone": str(configured.get("tone") or "DIRECT_FRIENDLY").upper(),
        "show_technical_values": bool(configured.get("show_technical_values", False)),
        "include_forecast": bool(configured.get("include_forecast", True)),
        "include_sources": bool(configured.get("include_sources", False)),
        "use_emojis": bool(configured.get("use_emojis", True)),
        "custom_instructions": str(configured.get("custom_instructions") or "").strip()[:2000],
    }


def _satellite_agent_prompt(system_prompt: str, policy: dict[str, Any]) -> str:
    tone = {
        "DIRECT_FRIENDLY": "directo, cercano y fácil de entender para un productor",
        "SIMPLE_TECHNICAL": "técnico, pero explicado con palabras sencillas",
        "INSTITUTIONAL": "institucional, sobrio y claro",
    }.get(policy.get("tone"), "directo y fácil de entender")
    technical = (
        "Menciona únicamente cifras redondeadas que cambien una decisión."
        if policy.get("show_technical_values")
        else "Evita listas de cifras y decimales; traduce los índices a condiciones prácticas."
    )
    emoji = "Usa como máximo dos emojis." if policy.get("use_emojis") else "No uses emojis."
    custom = policy.get("custom_instructions")
    rules = f"""
PROTOCOLO OBLIGATORIO PARA EL MAPA SATELITAL:
- Usa únicamente el contexto suministrado: estación, polígono, escena seleccionada, índices, comparación, clima, pronóstico y documentos recuperados.
- No inventes síntomas, causas, valores ni diagnósticos. Un índice o una imagen no confirma una enfermedad.
- Empieza por lo que el productor debe revisar y luego explica por qué.
- Tono {tone}. Máximo {policy['max_words']} palabras y {policy['max_sections']} secciones.
- Integra todo en un solo criterio: no crees apartados sobre el funcionamiento del sistema, API, modelo, RAG o proceso interno.
- Resume la comparación anterior y el clima del día; no enumeres series completas.
- {technical}
- {emoji}
""".strip()
    if custom:
        rules += f"\n\nINSTRUCCIONES DEL ADMINISTRADOR:\n{custom}"
    return "\n\n".join(part for part in [str(system_prompt or "").strip(), rules] if part)


def _limit_satellite_markdown(markdown: str, policy: dict[str, Any]) -> str:
    """Apply a final deterministic length/section cap even with older AI service implementations."""
    lines = str(markdown or "").strip().splitlines()
    max_sections = int(policy["max_sections"])
    kept: list[str] = []
    headings = 0
    for line in lines:
        if line.lstrip().startswith("#"):
            headings += 1
            if headings > max_sections:
                break
        kept.append(line)
    words = "\n".join(kept).split()
    max_words = int(policy["max_words"])
    if len(words) > max_words:
        words = words[:max_words]
        return " ".join(words).rstrip(".,;:") + "…"
    return "\n".join(kept).strip()


def _flatten_coordinates(value: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            points.append((float(value[0]), float(value[1])))
        else:
            for item in value:
                points.extend(_flatten_coordinates(item))
    return points


def _geometry_bounds(geometry: dict[str, Any]) -> dict[str, float]:
    points = _flatten_coordinates(geometry.get("coordinates"))
    if not points:
        raise ValueError("La geozona no contiene coordenadas utilizables")
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return {
        "west": min(longitudes),
        "south": min(latitudes),
        "east": max(longitudes),
        "north": max(latitudes),
    }


def _polygon_area_ha(geometry: dict[str, Any]) -> float | None:
    """Approximate WGS84 polygon area for a small agricultural geozone."""
    if geometry.get("type") == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
    else:
        polygons = [geometry.get("coordinates") or []]
    total_m2 = 0.0
    for polygon in polygons:
        if not polygon or not polygon[0]:
            continue
        ring = polygon[0]
        valid = [(float(item[0]), float(item[1])) for item in ring if isinstance(item, list) and len(item) >= 2]
        if len(valid) < 4:
            continue
        mean_lat = sum(point[1] for point in valid) / len(valid)
        scale_x = 111_320 * max(0.2, math.cos(math.radians(mean_lat)))
        scale_y = 111_320
        area = 0.0
        for index, current in enumerate(valid):
            following = valid[(index + 1) % len(valid)]
            x1, y1 = current[0] * scale_x, current[1] * scale_y
            x2, y2 = following[0] * scale_x, following[1] * scale_y
            area += x1 * y2 - x2 * y1
        total_m2 += abs(area) / 2
    return round(total_m2 / 10_000, 2) if total_m2 else None


def _safe_timezone(station: Station) -> ZoneInfo:
    try:
        return ZoneInfo(station.timezone or "America/Asuncion")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _index_value(observation: SatelliteObservation, key: str) -> float | None:
    raw = observation.raw_payload or {}
    value = (raw.get("indices") or {}).get(key)
    if value is None and key in {"ndvi", "ndmi", "ndwi"}:
        value = getattr(observation, key)
    try:
        return None if value is None else round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _observation_summary(observation: SatelliteObservation) -> dict[str, Any]:
    raw = observation.raw_payload or {}
    indices = {name: _index_value(observation, name) for name in INDEX_NAMES}
    return {
        "observation_id": str(observation.id),
        "scene_id": raw.get("scene_id"),
        "captured_at": observation.captured_at.isoformat(),
        "cloud_percentage": observation.cloud_percentage,
        "processing_status": raw.get("processing_status") or "UNKNOWN",
        "processing_error": raw.get("processing_error"),
        "image_available": bool(local_satellite_image_path(observation)),
        "indices_available": sum(value is not None for value in indices.values()),
        "indices": indices,
        "quality": raw.get("quality") or {},
        "data_source_mode": raw.get("data_source_mode") or "LATEST_VALID_SCENE",
        "fallback_used": bool(raw.get("fallback_used")),
        "data_warning": raw.get("data_warning"),
        "latest_catalog_scene_at": raw.get("latest_catalog_scene_at"),
        "catalog_checked_at": raw.get("catalog_checked_at"),
        "selected_scene_rank": raw.get("selected_scene_rank"),
    }


def _weather_payload(summary: WeatherDailySummary | None) -> dict[str, Any] | None:
    if not summary:
        return None
    return {
        "date": summary.summary_date.isoformat(),
        "temperature_avg_c": summary.temperature_avg_c,
        "humidity_avg_pct": summary.humidity_avg_pct,
        "rain_sum_mm": summary.rain_sum_mm,
        "soil_temperature_avg_c": summary.soil_temperature_avg_c,
        "soil_moisture_avg_pct": summary.soil_moisture_avg_pct,
        "leaf_wetness_hours": summary.leaf_wetness_hours,
        "evapotranspiration_sum_mm": summary.evapotranspiration_sum_mm,
        "completeness_pct": summary.completeness_pct,
        "records_count": summary.records_count,
    }


def _latest_reading_payload(reading: WeatherReading | None) -> dict[str, Any] | None:
    if not reading:
        return None
    return {
        "sampled_at": reading.sampled_at.isoformat(),
        "temperature_c": reading.temperature_c,
        "humidity_pct": reading.humidity_pct,
        "rain_mm": reading.rain_mm,
        "soil_temperature_c": reading.soil_temperature_c,
        "soil_moisture_pct": reading.soil_moisture_pct,
        "leaf_sensor_value": reading.leaf_sensor_value,
    }


def _trend_label(value: float | None, threshold: float = 0.035) -> str:
    if value is None:
        return "sin comparación"
    if value <= -threshold:
        return "descenso"
    if value >= threshold:
        return "aumento"
    return "estable"


def _producer_interpretation(
    observation: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    weather: dict[str, Any] | None,
) -> dict[str, Any]:
    if not observation:
        return {
            "level": "NO_DATA",
            "headline": "Todavía no hay una escena procesada para esta zona",
            "summary": "Seleccione una fecha disponible y procese la escena para obtener la imagen y los índices de todo el polígono.",
            "actions": [
                "Compruebe que la estación tenga un polígono guardado.",
                "Seleccione una escena con poca nubosidad.",
                "Procese la fecha para calcular los índices del área completa.",
            ],
            "what_to_watch": [],
            "limitations": ["Sin una escena procesada no es posible interpretar cambios del cultivo."],
        }

    indices = observation.get("indices") or {}
    analysis = observation.get("analysis") or {}
    ndvi = indices.get("ndvi")
    ndmi = indices.get("ndmi")
    ndwi = indices.get("ndwi")
    previous_indices = (previous or {}).get("indices") or {}
    ndvi_delta = None if ndvi is None or previous_indices.get("ndvi") is None else round(float(ndvi) - float(previous_indices["ndvi"]), 4)
    ndmi_delta = None if ndmi is None or previous_indices.get("ndmi") is None else round(float(ndmi) - float(previous_indices["ndmi"]), 4)

    reliability = str(analysis.get("reliability") or "baja")
    level = "ATTENTION" if (ndvi_delta is not None and ndvi_delta <= -0.04) or (ndmi is not None and float(ndmi) < -0.1) else "WATCH"
    if reliability in {"baja", "limitada"} or not any(value is not None for value in (ndvi, ndmi, ndwi)):
        level = "LIMITED"

    if level == "ATTENTION":
        headline = "Hay un cambio en el polígono que conviene revisar en campo"
    elif level == "LIMITED":
        headline = "La escena sirve como referencia, pero la evidencia todavía es limitada"
    else:
        headline = "El polígono no muestra un cambio fuerte en la última comparación"

    parts: list[str] = []
    if ndvi is not None:
        if float(ndvi) >= 0.6:
            parts.append("La cobertura vegetal se observa activa y relativamente densa")
        elif float(ndvi) >= 0.35:
            parts.append("La cobertura vegetal es intermedia y puede variar dentro del lote")
        else:
            parts.append("La señal de cobertura verde es baja o existe bastante suelo visible")
    if ndvi_delta is not None:
        parts.append(f"frente a la escena anterior el vigor aparece {_trend_label(ndvi_delta)}")
    if ndmi is not None:
        if float(ndmi) < -0.1:
            parts.append("la señal de humedad de la vegetación es baja")
        elif float(ndmi) > 0.2:
            parts.append("la vegetación conserva una señal de humedad alta")
    if ndwi is not None and float(ndwi) > 0.15:
        parts.append("hay una señal compatible con agua superficial o sectores muy húmedos")
    if weather:
        humidity = weather.get("humidity_avg_pct")
        rain = weather.get("rain_sum_mm")
        if humidity is not None and float(humidity) >= 85:
            parts.append("la estación registró humedad alta ese día")
        if rain is not None and float(rain) >= 5:
            parts.append(f"la lluvia registrada fue de aproximadamente {float(rain):.1f} mm")

    summary = ". ".join(part[:1].upper() + part[1:] for part in parts[:4])
    if summary:
        summary += "."
    else:
        summary = "La escena está disponible, pero faltan índices suficientes para describir el comportamiento del polígono."

    actions = [
        "Recorra primero los sectores que se vean diferentes al resto del polígono.",
        "Compare esos sectores con humedad del suelo, drenaje, malezas, fallas de establecimiento y síntomas visibles.",
        "Tome fotografías y registre el punto antes de decidir riego o manejo fitosanitario.",
    ]
    what_to_watch: list[str] = []
    if ndvi_delta is not None:
        what_to_watch.append(f"NDVI: {_trend_label(ndvi_delta)} ({ndvi_delta:+.3f} frente a la escena anterior).")
    if ndmi_delta is not None:
        what_to_watch.append(f"NDMI: {_trend_label(ndmi_delta)} ({ndmi_delta:+.3f}).")
    if observation.get("cloud_percentage") is not None:
        what_to_watch.append(f"Nubosidad de la escena: {float(observation['cloud_percentage']):.1f}%.")

    limitations = list((analysis.get("limitations") or [])[:3])
    if not limitations:
        limitations = ["La imagen y los índices orientan dónde revisar; no confirman por sí solos una enfermedad o una causa."]
    return {
        "level": level,
        "headline": headline,
        "summary": summary,
        "actions": actions,
        "what_to_watch": what_to_watch,
        "limitations": limitations,
        "deltas": {"ndvi": ndvi_delta, "ndmi": ndmi_delta},
    }


async def build_satellite_map_context(
    db: AsyncSession,
    station_id: UUID,
    crop_code: str,
    *,
    observation_id: UUID | None = None,
    history_days: int | None = None,
) -> dict[str, Any]:
    station = await db.get(Station, station_id)
    if not station or not station.active or station.deleted_at is not None:
        raise ValueError("Estación no encontrada")
    crop = await db.scalar(
        select(Crop).where(Crop.code == crop_code.upper(), Crop.active.is_(True), Crop.deleted_at.is_(None))
    )
    if not crop:
        raise ValueError("Cultivo no encontrado")

    geometry = station_geometry(station)
    plot = await ensure_station_plot(db, station, crop.id)
    integration = await _integration(db)
    runtime = satellite_runtime_config(integration)
    period_days = max(7, min(int(history_days or runtime["history_days"]), 366))

    observations = list(
        (
            await db.scalars(
                select(SatelliteObservation)
                .where(
                    SatelliteObservation.plot_id == plot.id,
                    SatelliteObservation.captured_at >= datetime.now(UTC) - timedelta(days=period_days + 5),
                )
                .order_by(SatelliteObservation.captured_at.desc())
                .limit(runtime["history_limit"])
            )
        ).all()
    )
    selected = None
    if observation_id:
        selected = next((item for item in observations if item.id == observation_id), None)
        if selected is None:
            selected = await db.get(SatelliteObservation, observation_id)
            if not selected or selected.plot_id != plot.id:
                raise ValueError("La observación seleccionada no pertenece a esta estación")
    elif observations:
        # The public widget must not replace a usable NDVI with a newer cloudy
        # partial row. Prefer the newest complete observation and retain the
        # newer catalogue acquisition only as freshness metadata.
        selected = next((item for item in observations if satellite_observation_is_complete(item)), observations[0])

    selected_payload = await satellite_observation_payload(db, selected, plot) if selected else None
    previous_row = None
    if selected:
        previous_row = next(
            (
                item for item in observations
                if item.captured_at < selected.captured_at and satellite_observation_is_complete(item)
            ),
            None,
        )
        if previous_row is None:
            older_rows = list(
                (
                    await db.scalars(
                        select(SatelliteObservation)
                        .where(
                            SatelliteObservation.plot_id == plot.id,
                            SatelliteObservation.captured_at < selected.captured_at,
                        )
                        .order_by(SatelliteObservation.captured_at.desc())
                        .limit(40)
                    )
                ).all()
            )
            previous_row = next((item for item in older_rows if satellite_observation_is_complete(item)), None)
    previous_payload = await satellite_observation_payload(db, previous_row, plot) if previous_row else None

    local_date: date | None = None
    weather = None
    if selected:
        local_date = selected.captured_at.astimezone(_safe_timezone(station)).date()
        weather_row = await db.scalar(
            select(WeatherDailySummary).where(
                WeatherDailySummary.station_id == station.id,
                WeatherDailySummary.summary_date == local_date,
            )
        )
        weather = _weather_payload(weather_row)
    latest_reading = await db.scalar(
        select(WeatherReading)
        .where(WeatherReading.station_id == station.id)
        .order_by(WeatherReading.sampled_at.desc())
        .limit(1)
    )
    forecasts = list(
        (
            await db.scalars(
                select(WeatherForecast)
                .where(WeatherForecast.station_id == station.id, WeatherForecast.forecast_date >= datetime.now(UTC).date())
                .order_by(WeatherForecast.forecast_date)
                .limit(3)
            )
        ).all()
    )

    catalog_error = None
    catalog_warning = None
    catalog_status = "CURRENT"
    catalog_features: list[dict[str, Any]] = []
    try:
        catalog = await catalog_scenes_for_station(db, station, days=period_days, max_items=runtime["history_limit"])
        catalog_features = catalog["features"]
        catalog_status = str(catalog.get("catalog_status") or "CURRENT")
        catalog_warning = catalog.get("catalog_warning")
    except CatalogTemporaryError as exc:
        # A CDSE 5xx is an upstream catalogue incident, not an invalid polygon.
        # Keep the processed observations usable and expose a recoverable state.
        catalog_status = "DEGRADED"
        catalog_warning = (
            "Copernicus no respondió temporalmente después de varios reintentos. "
            "Puede seguir consultando las escenas ya procesadas; el historial se intentará actualizar nuevamente."
        )
        if not observations:
            catalog_error = str(exc)[:1000]
    except Exception as exc:
        catalog_status = "FAILED"
        catalog_error = str(exc)[:1000]

    stored_by_scene = {
        str((item.raw_payload or {}).get("scene_id")): item
        for item in observations
        if (item.raw_payload or {}).get("scene_id")
    }
    catalog_scenes: list[dict[str, Any]] = []
    for feature in catalog_features:
        scene_id = str(feature.get("id") or "")
        stored = stored_by_scene.get(scene_id)
        catalog_scenes.append({
            "scene_id": scene_id,
            "captured_at": _feature_datetime(feature).isoformat(),
            "cloud_percentage": _feature_cloud(feature),
            "preview_url": _feature_preview(feature),
            "processed": bool(stored),
            "observation_id": str(stored.id) if stored else None,
            "processing_status": (stored.raw_payload or {}).get("processing_status") if stored else "AVAILABLE",
            "image_available": bool(stored and local_satellite_image_path(stored)),
            "indices_available": sum(_index_value(stored, key) is not None for key in INDEX_NAMES) if stored else 0,
        })

    # Preserve processed observations even if STAC no longer returns them in the current window.
    known_scene_ids = {item["scene_id"] for item in catalog_scenes}
    for stored in observations:
        raw = stored.raw_payload or {}
        scene_id = str(raw.get("scene_id") or stored.id)
        if scene_id in known_scene_ids:
            continue
        summary = _observation_summary(stored)
        catalog_scenes.append({
            "scene_id": scene_id,
            "captured_at": stored.captured_at.isoformat(),
            "cloud_percentage": stored.cloud_percentage,
            "preview_url": raw.get("preview_url"),
            "processed": True,
            "observation_id": str(stored.id),
            "processing_status": summary["processing_status"],
            "image_available": summary["image_available"],
            "indices_available": summary["indices_available"],
        })
    catalog_scenes.sort(key=lambda item: item["captured_at"], reverse=True)

    interpretation = _producer_interpretation(selected_payload, previous_payload, weather)
    return {
        "station": {
            "id": str(station.id),
            "name": station.name,
            "code": station.code,
            "locality": station.locality,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "timezone": station.timezone,
        },
        "crop": {"id": str(crop.id), "code": crop.code, "name": crop.name},
        "geometry": {
            "type": "Feature",
            "properties": {"station_id": str(station.id), "station_name": station.name},
            "geometry": geometry,
        },
        "geometry_source": "POLYGON" if _polygon_geometry(station.influence_geojson) else "POINT_RADIUS",
        "geometry_bounds": _geometry_bounds(geometry),
        "area_ha": _polygon_area_ha(geometry),
        "observation": selected_payload,
        "previous_observation": previous_payload,
        "processed_observations": [_observation_summary(item) for item in observations],
        "catalog_scenes": catalog_scenes[: runtime["history_limit"]],
        "catalog_status": catalog_status,
        "catalog_warning": catalog_warning,
        "catalog_error": catalog_error,
        "weather_on_capture": weather,
        "latest_station_reading": _latest_reading_payload(latest_reading),
        "forecast": [
            {
                "date": item.forecast_date.isoformat(),
                "temperature_min_c": item.temperature_min_c,
                "temperature_max_c": item.temperature_max_c,
                "precipitation_mm": item.precipitation_mm,
                "humidity_pct": item.humidity_pct,
            }
            for item in forecasts
        ],
        "interpretation": interpretation,
        "runtime_config": runtime,
        "context_date": local_date.isoformat() if local_date else None,
        "generated_at": datetime.now(UTC).isoformat(),
    }


async def generate_satellite_map_agent(
    db: AsyncSession,
    station_id: UUID,
    crop_code: str,
    observation_id: UUID,
) -> dict[str, Any]:
    context = await build_satellite_map_context(
        db,
        station_id,
        crop_code,
        observation_id=observation_id,
    )
    crop_id = UUID(context["crop"]["id"])
    agent, version = await select_agent_version(db, crop_id, "SATELLITE_ANALYSIS", fallback_purpose="CROP_ANALYSIS")
    policy = _satellite_response_policy(version.output_schema if version else {})

    query = (
        f"{context['crop']['name']} análisis satelital NDVI NDMI NDWI humedad vigor estrés "
        f"{context['station']['name']}"
    )
    chunks: list[dict[str, Any]] = []
    if agent:
        try:
            chunks = await retrieve_chunks(db, query, crop_id, limit=5, agent_id=agent.id)
        except Exception:
            chunks = []
    sources = [
        {"documento": item["document"], "pagina_o_seccion": item["page_or_section"]}
        for item in chunks
    ]
    agent_context = {
        "agent_purpose": "SATELLITE_ANALYSIS",
        "query": {
            "station": context["station"]["name"],
            "crop": context["crop"]["name"],
            "scene_date": context.get("context_date"),
        },
        "satellite": context.get("observation"),
        "previous_satellite": context.get("previous_observation"),
        "weather_on_capture": context.get("weather_on_capture"),
        "latest_station_reading": context.get("latest_station_reading"),
        "forecast": context.get("forecast") if policy.get("include_forecast") else [],
        "producer_interpretation": context.get("interpretation"),
        "sources": sources if policy.get("include_sources") else [],
        "retrieved_fragments": chunks,
        "response_policy": policy,
    }

    fallback = context["interpretation"]
    fallback_markdown = (
        f"## {fallback['headline']}\n\n{fallback['summary']}\n\n"
        "### Qué hacer ahora\n"
        + "\n".join(f"- {item}" for item in fallback["actions"][:3])
        + ("\n\n### Qué vigilar\n" + "\n".join(f"- {item}" for item in fallback["what_to_watch"][:3]) if fallback["what_to_watch"] else "")
        + f"\n\n> {fallback['limitations'][0]}"
    )
    if not agent or not version:
        return {
            "markdown": _limit_satellite_markdown(fallback_markdown, policy),
            "sources": sources if policy.get("include_sources") else [],
            "model": None,
            "fallback": True,
            "reason": "agent_not_configured",
        }

    started = datetime.now(UTC)
    try:
        image_data_url = None
        observation = await db.get(SatelliteObservation, observation_id)
        if observation:
            path = local_satellite_image_path(observation)
            if path:
                content = path.read_bytes()
                if len(content) <= 8 * 1024 * 1024:
                    image_data_url = f"data:image/jpeg;base64,{base64.b64encode(content).decode('ascii')}"
        prompt = _satellite_agent_prompt(version.system_prompt, policy)
        result = await generate_markdown_response(
            prompt,
            agent_context,
            (
                "Explique al productor qué está pasando en todo el polígono para la escena seleccionada. "
                "Empiece por la acción, integre la comparación anterior y el clima de la estación del mismo día, "
                "y no mencione componentes internos ni presente índices como diagnóstico."
            ),
            version.model,
            image_data_url=image_data_url,
            temperature=version.temperature,
            max_tokens=min(version.max_tokens, 1800),
            provider=getattr(version, "provider", "OPENAI"),
        )
        payload = {
            "markdown": _limit_satellite_markdown(
                fallback_markdown if result.get("fallback") else result["markdown"],
                policy,
            ),
            "sources": sources if policy.get("include_sources") else [],
            "model": result.get("model"),
            "fallback": bool(result.get("fallback")),
            "reason": "provider_not_configured" if result.get("fallback") else None,
        }
        status = "FALLBACK" if payload["fallback"] else "COMPLETED"
        error_message = None
    except Exception as exc:
        reason = provider_fallback_reason(exc)
        payload = {
            "markdown": _limit_satellite_markdown(fallback_markdown, policy),
            "sources": sources if policy.get("include_sources") else [],
            "model": None,
            "fallback": True,
            "reason": reason,
        }
        status = "FALLBACK"
        error_message = f"{reason} · HTTP {exc.status_code}" if hasattr(exc, "status_code") else reason

    finished = datetime.now(UTC)
    run = AIAgentRun(
        agent_id=agent.id,
        agent_version_id=version.id,
        status=status,
        model=payload.get("model"),
        input_payload={
            **agent_context,
            "context_hash": hashlib.sha256(json.dumps(agent_context, sort_keys=True, default=str).encode()).hexdigest(),
        },
        output_payload=payload,
        sources=sources,
        started_at=started,
        finished_at=finished,
        duration_ms=int((finished - started).total_seconds() * 1000),
        error_message=error_message,
    )
    db.add(run)
    await db.commit()
    return {**payload, "run_id": str(run.id)}
