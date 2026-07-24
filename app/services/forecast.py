from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import numpy as np
import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Station, WeatherForecast


HOURLY_VARIABLES = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "soil_temperature_6cm",
    "soil_moisture_3_to_9cm",
]
DAILY_VARIABLES = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "precipitation_probability_max",
    "relative_humidity_2m_mean",
    "wind_speed_10m_max",
    "et0_fao_evapotranspiration",
]


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(parsed) else round(parsed, 3)


def _daily_value(daily: dict[str, Any], key: str, index: int) -> float | None:
    values = daily.get(key) or []
    return _float_or_none(values[index]) if index < len(values) else None


def aggregate_open_meteo_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert hourly model output to agronomic daily variables with pandas/NumPy.

    Leaf wetness is an explicitly labelled proxy: an hour is considered potentially
    wet when precipitation is greater than zero or relative humidity is at least 90%.
    It is not mixed with observed FECOCLIMA leaf-sensor data.
    """

    daily = payload.get("daily") or {}
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    frame = pd.DataFrame({"time": pd.to_datetime(times, errors="coerce")})
    for variable in HOURLY_VARIABLES:
        values = hourly.get(variable) or []
        frame[variable] = pd.to_numeric(pd.Series(values), errors="coerce")
    if not frame.empty:
        frame = frame.dropna(subset=["time"])
        frame["date"] = frame["time"].dt.date
        frame["wet_proxy"] = (
            frame["precipitation"].fillna(0).gt(0)
            | frame["relative_humidity_2m"].fillna(-1).ge(90)
        )

    generated_at = datetime.now(UTC).isoformat()
    result: list[dict[str, Any]] = []
    for index, day_text in enumerate(daily.get("time") or []):
        day = date.fromisoformat(day_text)
        subset = frame.loc[frame["date"] == day] if not frame.empty else pd.DataFrame()
        soil_moisture = None
        if not subset.empty and "soil_moisture_3_to_9cm" in subset:
            raw_soil = pd.to_numeric(subset["soil_moisture_3_to_9cm"], errors="coerce").mean(skipna=True)
            if not pd.isna(raw_soil):
                # Open-Meteo returns volumetric water content in m³/m³.
                soil_moisture = float(raw_soil * 100 if abs(raw_soil) <= 1.5 else raw_soil)
        temperature_avg = None
        soil_temperature = None
        leaf_wetness_hours = None
        if not subset.empty:
            temperature_avg = _float_or_none(subset["temperature_2m"].mean(skipna=True))
            soil_temperature = _float_or_none(subset["soil_temperature_6cm"].mean(skipna=True))
            leaf_wetness_hours = _float_or_none(subset["wet_proxy"].sum())
        result.append(
            {
                "date": day_text,
                "temperature_min_c": _daily_value(daily, "temperature_2m_min", index),
                "temperature_max_c": _daily_value(daily, "temperature_2m_max", index),
                "temperature_avg_c": temperature_avg,
                "precipitation_mm": _daily_value(daily, "precipitation_sum", index),
                "precipitation_probability_pct": _daily_value(daily, "precipitation_probability_max", index),
                "humidity_pct": _daily_value(daily, "relative_humidity_2m_mean", index),
                "wind_speed_kmh": _daily_value(daily, "wind_speed_10m_max", index),
                "evapotranspiration_mm": _daily_value(daily, "et0_fao_evapotranspiration", index),
                "soil_temperature_avg_c": soil_temperature,
                "estimated_soil_moisture_pct": _float_or_none(soil_moisture),
                "leaf_wetness_hours": leaf_wetness_hours,
                "leaf_wetness_method": "estimated_rh90_or_precipitation",
                "provider": "Open-Meteo",
                "generated_at": generated_at,
                "source_kind": "forecast",
                "limitations": [
                    "La humedad del suelo y la temperatura del suelo son pronósticos de modelo, no mediciones de la estación.",
                    "Las horas de mojado foliar son una aproximación basada en HR ≥ 90% o precipitación; deben validarse con el sensor de hoja.",
                ],
            }
        )
    return result


async def fetch_open_meteo(latitude: float, longitude: float, days: int = 7) -> list[dict[str, Any]]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "America/Asuncion",
        "forecast_days": min(max(days, 1), 16),
        "wind_speed_unit": "kmh",
        "hourly": ",".join(HOURLY_VARIABLES),
        "daily": ",".join(DAILY_VARIABLES),
    }
    async with httpx.AsyncClient(timeout=settings.external_http_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(f"{settings.open_meteo_base_url.rstrip('/')}/forecast", params=params)
        response.raise_for_status()
        payload = response.json()
    return aggregate_open_meteo_payload(payload)


async def refresh_station_forecast(db: AsyncSession, station: Station, days: int = 7) -> dict[str, Any]:
    if station.latitude is None or station.longitude is None:
        raise ValueError("La estación no tiene coordenadas para pronóstico")
    rows = await fetch_open_meteo(station.latitude, station.longitude, days=days)
    generated_at = datetime.now(UTC)
    for row in rows:
        values = {
            "station_id": station.id,
            "forecast_date": date.fromisoformat(row["date"]),
            "generated_at": generated_at,
            "provider": row["provider"],
            "temperature_min_c": row.get("temperature_min_c"),
            "temperature_max_c": row.get("temperature_max_c"),
            "precipitation_mm": row.get("precipitation_mm"),
            "precipitation_probability_pct": row.get("precipitation_probability_pct"),
            "humidity_pct": row.get("humidity_pct"),
            "wind_speed_ms": (row.get("wind_speed_kmh") / 3.6) if row.get("wind_speed_kmh") is not None else None,
            "evapotranspiration_mm": row.get("evapotranspiration_mm"),
            "estimated_soil_moisture_pct": row.get("estimated_soil_moisture_pct"),
            "raw_payload": row,
        }
        statement = insert(WeatherForecast).values(**values).on_conflict_do_update(
            index_elements=[
                WeatherForecast.station_id,
                WeatherForecast.forecast_date,
                WeatherForecast.provider,
            ],
            set_={key: value for key, value in values.items() if key not in {"station_id", "forecast_date", "provider"}},
        )
        await db.execute(statement)
    await db.commit()
    return {
        "days": len(rows),
        "provider": "Open-Meteo",
        "coordinates": {"latitude": station.latitude, "longitude": station.longitude},
        "soil_forecast": True,
        "leaf_wetness_proxy": True,
    }
