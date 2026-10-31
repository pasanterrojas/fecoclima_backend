"""Three local days: bounded station trends + Open-Meteo, with pre-day verification.

Weights are an initial heuristic, not a calibrated or validated yield/weather model.
Only complete past days train the trend; predictions are never overwritten once
that target day starts. Null sensors are never zeros or converted between units.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import StationForecastVerification, WeatherReading
from app.services.audit import write_audit
from app.services.forecast import fetch_open_meteo
from app.services.weather import effective_soil_moisture_unit

REVISION = "station-blend-v1"
# field, unit, physical range, maximum daily slope, matching model daily mean/total
VARIABLES = {
    "temperature_avg_c": ("temperature_c", "°C", -40, 60, 3, "temperature_avg_c"),
    "temperature_min_c": ("temperature_c", "°C", -40, 60, 3, "temperature_min_c"),
    "temperature_max_c": ("temperature_c", "°C", -40, 60, 3, "temperature_max_c"),
    "humidity_pct": ("humidity_pct", "%", 0, 100, 10, "humidity_pct"),
    "precipitation_mm": ("rain_mm", "mm", 0, 2000, 15, "precipitation_mm"),
    "wind_speed_ms": ("wind_speed_ms", "m/s", 0, 80, 3, "wind_speed_avg_ms"),
    "evapotranspiration_mm": ("evapotranspiration_mm", "mm", 0, 30, 2, "evapotranspiration_mm"),
    "soil_temperature_avg_c": ("soil_temperature_c", "°C", -10, 65, 2, "soil_temperature_avg_c"),
    "soil_moisture_pct": ("soil_moisture_pct", "%", 0, 100, 5, "estimated_soil_moisture_pct"),
    "soil_tension_cb": ("soil_moisture_pct", "cb", 0, 200, 10, None),
    "leaf_wetness_hours": ("leaf_sensor_value", "h", 0, 24, 4, "leaf_wetness_hours"),
}


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def daily_history(station, readings, reference):
    zone = ZoneInfo(station.timezone or "America/Asuncion")
    today = utc(reference).astimezone(zone).date()
    unique = {utc(r.sampled_at): r for r in readings if utc(r.sampled_at) < utc(reference)}
    grouped = {}
    for at, row in sorted(unique.items()):
        day = at.astimezone(zone).date()
        if today - timedelta(days=30) <= day < today:
            grouped.setdefault(day, []).append(row)
    unit = effective_soil_moisture_unit(station)
    interval = max(1, station.expected_interval_minutes or 10)
    config = station.integration_config or {}
    leaf_mode = (station.leaf_sensor_config or {}).get("mode")
    result = []
    for day, rows in sorted(grouped.items()):
        begin = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
        expected = (end - begin).total_seconds() / 60 / interval
        values, coverage = {}, {}
        for key, (field, _, low, high, _, _) in VARIABLES.items():
            if key == "soil_moisture_pct" and unit != "%" or key == "soil_tension_cb" and unit != "cb":
                continue
            # Raw leaf scales (e.g. 0..15) do not measure hours.
            if key == "leaf_wetness_hours" and leaf_mode != "hours":
                continue
            valid = [
                (utc(r.sampled_at), getattr(r, field, None))
                for r in rows
                if finite(getattr(r, field, None)) and low <= getattr(r, field) <= high
            ]
            coverage[key] = round(min(1, len(valid) / expected), 3)
            if coverage[key] < 0.8:
                continue
            numbers = [v for _, v in valid]
            if key in {"precipitation_mm", "evapotranspiration_mm"}:
                mode = (config.get("water_aggregation") or {}).get(field)
                if mode not in {"daily_counter", "interval_sum"}:
                    continue
                if valid[0][0] - begin > timedelta(minutes=3 * interval) or end - valid[-1][0] > timedelta(
                    minutes=3 * interval
                ):
                    continue
                if mode == "daily_counter" and any(b < a for a, b in zip(numbers, numbers[1:])):
                    continue
                value = max(numbers) if mode == "daily_counter" else sum(numbers)
            elif key == "leaf_wetness_hours":
                # Preserve the existing hours-valued sensor contract (daily mean).
                value = float(np.mean(numbers))
            elif key == "temperature_min_c":
                value = min(numbers)
            elif key == "temperature_max_c":
                value = max(numbers)
            else:
                value = float(np.mean(numbers))
            values[key] = round(value, 3)
        result.append({"date": day.isoformat(), "values": values, "coverage": coverage})
    return result


def predict_days(history, model_days, reference, timezone):
    today = utc(reference).astimezone(ZoneInfo(timezone or "America/Asuncion")).date()
    models = {r["date"]: r for r in model_days}
    forecasts = []
    for lead in range(1, 4):
        target = today + timedelta(days=lead)
        model = models.get(target.isoformat(), {})
        variables = {}
        for key, (_, unit, low, high, slope_limit, model_key) in VARIABLES.items():
            samples = [r for r in history if finite(r["values"].get(key))]
            # No stale or sparse trend bridging a long gap in the station stream.
            recent = [r for r in samples if (today - date.fromisoformat(r["date"])).days <= 14]
            trend, rmse, slope = None, None, None
            if len(recent) >= 3 and (today - date.fromisoformat(recent[-1]["date"])).days <= 2:
                x = np.array([(date.fromisoformat(r["date"]) - today).days for r in recent], dtype=float)
                y = np.array([r["values"][key] for r in recent], dtype=float)
                raw_slope, intercept = np.polyfit(x, y, 1)
                slope = float(np.clip(raw_slope, -slope_limit, slope_limit))
                # Anchor to the recent mean and damp extrapolation as lead grows.
                trend = float(np.clip(np.mean(y[-3:]) + slope * (lead + 1) / (1 + 0.3 * lead), low, high))
                rmse = round(float(np.sqrt(np.mean((y - (raw_slope * x + intercept)) ** 2))), 3)
            baseline = model.get(model_key) if model_key else None
            baseline = float(baseline) if finite(baseline) and low <= baseline <= high else None
            # Model soil depth and leaf proxy differ from station installations.
            compatible = key not in {"soil_temperature_avg_c", "soil_moisture_pct", "leaf_wetness_hours"}
            weight = min(0.25, len(recent) / 56) if trend is not None and compatible else 0
            if key == "precipitation_mm":
                weight = min(weight, 0.1)
            if baseline is not None:
                value = baseline * (1 - weight) + (trend or 0) * weight
                method = "combined" if weight else "open_meteo"
            elif trend is not None:
                value, method, weight = trend, "station_trend", 1
            else:
                continue
            variables[key] = {
                "value": round(float(np.clip(value, low, high)), 2),
                "unit": unit,
                "method": method,
                "model_value": baseline,
                "station_trend": round(trend, 3) if trend is not None else None,
                "station_weight": round(weight, 3),
                "history_days": len(recent),
                "fit_rmse": rmse,
                "slope_per_day": round(slope, 3) if slope is not None else None,
            }
        forecasts.append(
            {
                **model,
                "date": target.isoformat(),
                "variables": variables,
                **{key: item["value"] for key, item in variables.items()},
                "provider": "FECOCLIMA_BLEND",
                "source_kind": "forecast",
                "method_revision": REVISION,
            }
        )
    return forecasts


async def save_predictions(db, station, days, reference):
    zone = ZoneInfo(station.timezone or "America/Asuncion")
    for row in days:
        row = {**row, "coordinates": {"latitude": station.latitude, "longitude": station.longitude}}
        target = date.fromisoformat(row["date"])
        deadline = datetime.combine(target, time.min, tzinfo=zone).astimezone(UTC)
        if reference >= deadline or not row["variables"]:
            continue
        values = dict(
            station_id=station.id,
            target_date=target,
            issued_at=reference,
            target_starts_at=deadline,
            predicted=row,
            method_revision=REVISION,
            timezone=station.timezone or "America/Asuncion",
        )
        stmt = insert(StationForecastVerification).values(**values)
        changed = await db.scalar(
            stmt.on_conflict_do_update(
                index_elements=[
                    StationForecastVerification.station_id,
                    StationForecastVerification.target_date,
                ],
                set_={
                    key: value for key, value in values.items() if key not in {"station_id", "target_date"}
                },
                where=(StationForecastVerification.issued_at < reference)
                & (StationForecastVerification.target_starts_at > reference),
            ).returning(StationForecastVerification.id)
        )
        if changed:
            await write_audit(
                db,
                action="STATION_PREDICTION_GENERATED",
                entity_type="station_prediction",
                entity_id=str(changed),
                after={
                    "station_id": str(station.id),
                    "date": row["date"],
                    "issued_at": reference.isoformat(),
                    "prediction": row,
                },
            )


async def verify_observations(db, station, history, reference):
    today = utc(reference).astimezone(ZoneInfo(station.timezone or "America/Asuncion")).date()
    rows = list(
        (
            await db.scalars(
                select(StationForecastVerification).where(
                    StationForecastVerification.station_id == station.id,
                    StationForecastVerification.target_date < today,
                    StationForecastVerification.target_date >= today - timedelta(days=30),
                )
            )
        ).all()
    )
    by_date = {r["date"]: r for r in history}
    for row in rows:
        observed = by_date.get(row.target_date.isoformat(), {})
        data = {key: observed.get("values", {}).get(key) for key in row.predicted.get("variables", {})}
        coverage = observed.get("coverage", {})
        if row.observed == data and row.observation_coverage == coverage:
            continue
        row.observed, row.observation_coverage, row.verified_at = data, coverage, reference
        await write_audit(
            db,
            action="STATION_PREDICTION_VERIFIED",
            entity_type="station_prediction",
            entity_id=str(row.id),
            after={
                "station_id": str(station.id),
                "date": row.target_date.isoformat(),
                "predicted": row.predicted,
                "observed": data,
                "coverage": coverage,
            },
        )


async def station_prediction(db, station, reference=None, *, fetcher=None, persist=True):
    reference = utc(reference or datetime.now(UTC))
    rows = list(
        (
            await db.scalars(
                select(WeatherReading)
                .where(
                    WeatherReading.station_id == station.id,
                    WeatherReading.sampled_at >= reference - timedelta(days=31),
                    WeatherReading.sampled_at < reference,
                )
                .order_by(WeatherReading.sampled_at)
            )
        ).all()
    )
    history = daily_history(station, rows, reference)
    await verify_observations(db, station, history, reference)
    today = reference.astimezone(ZoneInfo(station.timezone or "America/Asuncion")).date()
    cached = list(
        (
            await db.scalars(
                select(StationForecastVerification)
                .where(
                    StationForecastVerification.station_id == station.id,
                    StationForecastVerification.target_date > today,
                    StationForecastVerification.target_date <= today + timedelta(days=3),
                    StationForecastVerification.issued_at > reference - timedelta(hours=6),
                    StationForecastVerification.issued_at <= reference,
                    StationForecastVerification.method_revision == REVISION,
                )
                .order_by(StationForecastVerification.target_date)
            )
        ).all()
    )
    coordinates = {"latitude": station.latitude, "longitude": station.longitude}
    if len(cached) == 3 and all(r.predicted.get("coordinates") == coordinates for r in cached):
        days = [r.predicted for r in cached]
        return {
            "station_id": str(station.id),
            "name": station.name,
            "days": days,
            "model_available": any(
                v.get("model_value") is not None for d in days for v in d["variables"].values()
            ),
            "history_days": len(history),
            "coordinates": coordinates,
            "status": "COMPLETED",
        }
    model_days = []
    if station.latitude is not None and station.longitude is not None:
        try:
            model_days = await (fetcher or fetch_open_meteo)(station.latitude, station.longitude, days=4)
        except Exception:
            pass
    days = predict_days(history, model_days, reference, station.timezone)
    if persist:
        # Issue after the external response/calculation, never backdate a late
        # response across the target day's midnight to create a training sample.
        issued_at = max(reference, datetime.now(UTC))
        days = [{**row, "issued_at": issued_at.isoformat()} for row in days]
        await save_predictions(db, station, days, issued_at)
    return {
        "station_id": str(station.id),
        "name": station.name,
        "days": days,
        "model_available": bool(model_days),
        "history_days": len(history),
        "coordinates": {"latitude": station.latitude, "longitude": station.longitude},
        "status": "COMPLETED" if any(d["variables"] for d in days) else "UNAVAILABLE",
    }


def parcel_prediction(station_results, influences, reference):
    """Area-weight station predictions; keep per-station provenance for auditing."""
    weights = {r["station_id"]: r.get("coverage_pct", 0) for r in influences}
    days = []
    dates = sorted({d["date"] for r in station_results for d in r["days"]})
    for day in dates:
        sources = [
            (r, next(d for d in r["days"] if d["date"] == day))
            for r in station_results
            if any(d["date"] == day for d in r["days"])
        ]
        variables = {}
        for key in VARIABLES:
            candidates = [
                (row["variables"][key], weights.get(st["station_id"], 0))
                for st, row in sources
                if key in row["variables"] and weights.get(st["station_id"], 0) > 0
            ]
            total = sum(weight for _, weight in candidates)
            if total:
                variables[key] = {
                    "value": round(sum(item["value"] * weight for item, weight in candidates) / total, 2),
                    "unit": VARIABLES[key][1],
                    "station_count": len(candidates),
                }
        result = {"date": day, "variables": variables, **{k: v["value"] for k, v in variables.items()}}
        for key in ("temperature_min_c", "temperature_max_c", "precipitation_probability_pct"):
            numbers = [
                (row[key], weights.get(st["station_id"], 0)) for st, row in sources if finite(row.get(key))
            ]
            total = sum(w for _, w in numbers)
            if key not in variables:
                result[key] = round(sum(v * w for v, w in numbers) / total, 2) if total else None
        days.append(result)
    return {
        "status": "COMPLETED" if any(d["variables"] for d in days) else "UNAVAILABLE",
        "generated_at": reference.isoformat(),
        "provider": "FECOCLIMA_BLEND",
        "method_revision": REVISION,
        "days": days,
        "stations": station_results,
        "limitations": [
            "Aproximación inicial; aún no está calibrada con los errores observados.",
            "Los sensores ausentes conservan la referencia del modelo cuando existe; no son mediciones.",
            "La humedad de suelo del modelo y la tensión de una estación no son equivalentes.",
        ],
    }
