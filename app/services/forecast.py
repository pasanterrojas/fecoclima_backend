from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings


async def fetch_open_meteo(latitude: float, longitude: float, days: int = 7) -> list[dict[str, Any]]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "America/Asuncion",
        "forecast_days": days,
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "relative_humidity_2m_mean",
            "wind_speed_10m_max",
            "et0_fao_evapotranspiration",
        ]),
    }
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(f"{settings.open_meteo_base_url.rstrip('/')}/forecast", params=params)
        response.raise_for_status()
        payload = response.json()
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    result: list[dict[str, Any]] = []
    for i, day in enumerate(times):
        def at(key):
            values = daily.get(key) or []
            return values[i] if i < len(values) else None
        result.append({
            "date": day,
            "temperature_max_c": at("temperature_2m_max"),
            "temperature_min_c": at("temperature_2m_min"),
            "precipitation_mm": at("precipitation_sum"),
            "precipitation_probability_pct": at("precipitation_probability_max"),
            "humidity_pct": at("relative_humidity_2m_mean"),
            "wind_speed_kmh": at("wind_speed_10m_max"),
            "evapotranspiration_mm": at("et0_fao_evapotranspiration"),
            "provider": "Open-Meteo",
            "generated_at": datetime.now(UTC).isoformat(),
        })
    return result
