from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Station, WeatherReading
from app.services.weather import sync_fecoclima_station

_PERIOD_HOURS = {"24h": 24, "72h": 72, "7d": 168, "30d": 720}
_COLUMNS = [
    "temperature_c",
    "humidity_pct",
    "rain_mm",
    "soil_temperature_c",
    "soil_moisture_pct",
    "leaf_sensor_value",
    "solar_wm2",
    "evapotranspiration_mm",
]
_SUM_COLUMNS = {"rain_mm", "evapotranspiration_mm"}


def _same_moment_previous_year(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # 29 February is aligned to 28 February in a non-leap year.
        return value.replace(year=value.year - 1, day=28)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(parsed):
        return None
    return round(parsed, 3)


def _summary(frame: pd.DataFrame) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for column in _COLUMNS:
        if column not in frame:
            continue
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if not numeric.notna().any():
            continue
        item = {
            "min": _number(numeric.min()),
            "max": _number(numeric.max()),
            "mean": _number(numeric.mean()),
            "last": _number(numeric.dropna().iloc[-1]),
            "available_samples": int(numeric.notna().sum()),
        }
        if column in _SUM_COLUMNS:
            item["sum"] = _number(numeric.sum(min_count=1))
        summary[column] = item
    return summary


def _resample_rule(hours: int) -> str:
    if hours <= 24:
        return "30min"
    if hours <= 72:
        return "1h"
    if hours <= 168:
        return "3h"
    return "12h"


def _frame(readings: list[WeatherReading], hours: int) -> pd.DataFrame:
    rows = [
        {
            "sampled_at": row.sampled_at,
            **{column: getattr(row, column) for column in _COLUMNS},
        }
        for row in readings
    ]
    if not rows:
        return pd.DataFrame(columns=["sampled_at", *_COLUMNS])
    frame = pd.DataFrame(rows).sort_values("sampled_at")
    frame["sampled_at"] = pd.to_datetime(frame["sampled_at"], utc=True)
    frame = frame.set_index("sampled_at")
    aggregations = {
        column: ((lambda values: values.sum(min_count=1)) if column in _SUM_COLUMNS else "mean")
        for column in _COLUMNS
    }
    # min_count=1 prevents an empty time bin from becoming a false zero for
    # rain or evapotranspiration.
    frame = frame.resample(_resample_rule(hours)).agg(aggregations).dropna(how="all")
    return frame.reset_index()


def _period_payload(
    readings: list[WeatherReading],
    *,
    date_from: datetime,
    date_to: datetime,
    expected_interval_minutes: int,
    hours: int,
) -> dict[str, Any]:
    raw_frame = pd.DataFrame(
        [
            {
                "sampled_at": row.sampled_at,
                **{column: getattr(row, column) for column in _COLUMNS},
            }
            for row in readings
        ]
    )
    expected = max(1, round((date_to - date_from).total_seconds() / 60 / max(1, expected_interval_minutes)))
    series_frame = _frame(readings, hours)
    series = []
    for record in series_frame.to_dict(orient="records"):
        sampled_at = record.pop("sampled_at")
        series.append(
            {
                "sampled_at": sampled_at.isoformat(),
                **{key: _number(value) for key, value in record.items()},
            }
        )
    return {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "samples": len(readings),
        "expected_samples": expected,
        "coverage_pct": round(min(100.0, len(readings) / expected * 100), 1),
        "summary": _summary(raw_frame),
        "series": series,
    }


def _delta(current: dict[str, Any], previous: dict[str, Any], column: str, statistic: str) -> float | None:
    current_value = ((current.get("summary") or {}).get(column) or {}).get(statistic)
    previous_value = ((previous.get("summary") or {}).get(column) or {}).get(statistic)
    if current_value is None or previous_value is None:
        return None
    return round(float(current_value) - float(previous_value), 3)


async def _readings_between(
    db: AsyncSession,
    station_id: UUID,
    date_from: datetime,
    date_to: datetime,
) -> list[WeatherReading]:
    return list(
        (
            await db.scalars(
                select(WeatherReading)
                .where(
                    WeatherReading.station_id == station_id,
                    WeatherReading.sampled_at >= date_from,
                    WeatherReading.sampled_at <= date_to,
                )
                .order_by(WeatherReading.sampled_at)
            )
        ).all()
    )


async def build_year_comparison(
    db: AsyncSession,
    station: Station,
    *,
    period: str,
    latest_reading: WeatherReading | None = None,
    refresh_historical_if_missing: bool = False,
) -> dict[str, Any]:
    hours = _PERIOD_HOURS.get(period, 72)
    if latest_reading is None:
        latest_reading = await db.scalar(
            select(WeatherReading)
            .where(WeatherReading.station_id == station.id)
            .order_by(WeatherReading.sampled_at.desc())
            .limit(1)
        )
    current_to = latest_reading.sampled_at if latest_reading else datetime.now(UTC)
    current_from = current_to - timedelta(hours=hours)
    previous_to = _same_moment_previous_year(current_to)
    previous_from = previous_to - timedelta(hours=hours)

    current_rows = await _readings_between(db, station.id, current_from, current_to)
    previous_rows = await _readings_between(db, station.id, previous_from, previous_to)
    historical_sync: dict[str, Any] | None = None
    if (
        refresh_historical_if_missing
        and len(previous_rows) < 3
        and station.source_type.upper() in {"FECOCLIMA", "FECOCLIMA_API"}
        and station.external_guid
    ):
        try:
            historical_sync = {
                "status": "COMPLETED",
                **await sync_fecoclima_station(
                    db,
                    station.id,
                    previous_from.date(),
                    previous_to.date(),
                ),
            }
            previous_rows = await _readings_between(db, station.id, previous_from, previous_to)
        except Exception as exc:
            historical_sync = {"status": "FAILED", "error": str(exc)[:1000]}

    current = _period_payload(
        current_rows,
        date_from=current_from,
        date_to=current_to,
        expected_interval_minutes=station.expected_interval_minutes,
        hours=hours,
    )
    previous = _period_payload(
        previous_rows,
        date_from=previous_from,
        date_to=previous_to,
        expected_interval_minutes=station.expected_interval_minutes,
        hours=hours,
    )
    minimum_coverage = 20.0
    comparable = current["coverage_pct"] >= minimum_coverage and previous["coverage_pct"] >= minimum_coverage
    notes: list[str] = []
    if current["coverage_pct"] < minimum_coverage:
        notes.append("El periodo actual tiene poca cobertura de lecturas.")
    if previous["coverage_pct"] < minimum_coverage:
        notes.append("El periodo equivalente del año anterior tiene poca cobertura de lecturas.")
    if latest_reading and datetime.now(UTC) - latest_reading.sampled_at > timedelta(
        minutes=max(60, station.expected_interval_minutes * 12)
    ):
        notes.append("La comparación se ancló a la última lectura disponible porque la estación está atrasada.")

    return {
        "period": period,
        "anchor": "latest_station_reading" if latest_reading else "current_time_without_station_data",
        "current": current,
        "previous_year": previous,
        "deltas": {
            "temperature_mean_c": _delta(current, previous, "temperature_c", "mean"),
            "humidity_mean_pct": _delta(current, previous, "humidity_pct", "mean"),
            "rain_sum_mm": _delta(current, previous, "rain_mm", "sum"),
            "soil_moisture_mean_pct": _delta(current, previous, "soil_moisture_pct", "mean"),
            "evapotranspiration_sum_mm": _delta(current, previous, "evapotranspiration_mm", "sum"),
        },
        "comparability": {
            "status": "comparable" if comparable else "insufficient",
            "minimum_coverage_pct": minimum_coverage,
            "notes": notes,
        },
        "historical_sync": historical_sync,
        "generated_at": datetime.now(UTC).isoformat(),
    }
