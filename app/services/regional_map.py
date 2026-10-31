"""Immutable, 72-hour station snapshots and bounded spatial estimates, never invented readings."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

import numpy as np
from shapely import contains_xy
from shapely.geometry import MultiPoint, shape
from sqlalchemy import select

from app.db.models import Station, WeatherReading
from app.services.geospatial import station_footprint
from app.services.map_terrain import atomic_write, cache_dir, colorize, elevation, png, tile_coordinates

VARIABLES = {
    "temperature": {
        "label": "Temperatura del aire",
        "field": "temperature_c",
        "unit": "°C",
        "min": -15,
        "max": 55,
        "range": [10, 40],
    },
    "humidity": {
        "label": "Humedad del aire",
        "field": "humidity_pct",
        "unit": "%",
        "min": 0,
        "max": 100,
        "range": [0, 100],
    },
    "rain": {
        "label": "Lluvia registrada (72 h)",
        "field": "rain_mm",
        "unit": "mm",
        "min": 0,
        "max": 2000,
        "range": [0, 100],
    },
    "soil_temperature": {
        "label": "Temperatura del suelo",
        "field": "soil_temperature_c",
        "unit": "°C",
        "min": -10,
        "max": 65,
        "range": [10, 40],
    },
    "soil_moisture": {
        "label": "Humedad del suelo",
        "field": "soil_moisture_pct",
        "unit": "%",
        "min": 0,
        "max": 100,
        "range": [0, 100],
    },
    "soil_tension": {
        "label": "Tensión del suelo",
        "field": "soil_moisture_pct",
        "unit": "cb",
        "min": 0,
        "max": 200,
        "range": [0, 200],
    },
    "wind": {
        "label": "Velocidad del viento",
        "field": "wind_speed_ms",
        "unit": "m/s",
        "min": 0,
        "max": 80,
        "range": [0, 12],
    },
    "et": {
        "label": "Evapotranspiración acumulada (72 h)",
        "field": "evapotranspiration_mm",
        "unit": "mm",
        "min": 0,
        "max": 30,
        "range": [0, 20],
    },
}


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def valid(value, minimum, maximum):
    return (
        value is not None
        and not isinstance(value, bool)
        and isinstance(value, (float, int))
        and isfinite(value)
        and minimum <= value <= maximum
    )


def cumulative_window(readings, station, field, start, end):
    """Daily counters: subtract the opening-day baseline; never sum repeated counters."""
    mode = ((station.integration_config or {}).get("water_aggregation") or {}).get(field)
    if mode not in {"daily_counter", "interval_sum"}:
        return None, "AGGREGATION_UNCONFIRMED"
    zone = ZoneInfo(station.timezone or "America/Asuncion")
    interval = max(1, station.expected_interval_minutes or 10)
    total = 0.0
    partial_boundary = False
    # A counter reports an observation at a time, not rainfall up to the
    # server clock. Stop at its last valid sample, including near midnight.
    # This leaves the unmeasured tail outside the observed accumulation.
    maximum = 2000 if field == "rain_mm" else 30
    available = [
        r for r in readings if start < utc(r.sampled_at) <= end and valid(getattr(r, field), 0, maximum)
    ]
    if not available:
        return None, "NOT_REPORTED"
    last = utc(available[-1].sampled_at)
    if end - last > timedelta(minutes=max(60, interval * 3)):
        return None, "STALE"
    partial_boundary = end - last > timedelta(minutes=interval)
    end = last
    day = start.astimezone(zone).date()
    final_day = end.astimezone(zone).date()
    while day <= final_day:
        begin = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
        finish = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
        lower, upper = max(start, begin), min(end, finish)
        if upper <= lower:
            day += timedelta(days=1)
            continue
        rows = [
            r
            for r in readings
            if (
                lower < utc(r.sampled_at) <= upper
                or (mode == "daily_counter" and lower == begin and utc(r.sampled_at) == begin)
            )
            and (mode == "interval_sum" or utc(r.sampled_at).astimezone(zone).date() == day)
        ]
        maximum = 2000 if field == "rain_mm" else 30
        measured = [r for r in rows if valid(getattr(r, field), 0, maximum)]
        # A rolling window may open less than one sample interval before a
        # daily reset. Keep the observed total, explicitly marked as partial;
        # do not invent rainfall for that unmeasured fraction or discard 72 h.
        if (
            not measured
            and mode == "daily_counter"
            and lower > begin
            and (upper - lower).total_seconds() < interval * 60
        ):
            before = [
                r
                for r in readings
                if lower - timedelta(minutes=interval * 3) <= utc(r.sampled_at) <= lower
                and utc(r.sampled_at).astimezone(zone).date() == day
                and valid(getattr(r, field), 0, maximum)
            ]
            after = [
                r
                for r in readings
                if upper <= utc(r.sampled_at) <= upper + timedelta(minutes=interval * 3)
                and valid(getattr(r, field), 0, maximum)
            ]
            if before and after:
                partial_boundary = True
                day += timedelta(days=1)
                continue
        expected = max(1, (upper - lower).total_seconds() / 60 / interval)
        if len(measured) / expected < 0.8:
            return None, "INCOMPLETE_HISTORY"
        values = [float(getattr(r, field)) for r in measured]
        if not values:
            return None, "NOT_REPORTED"
        if mode == "interval_sum":
            total += sum(values)
        else:
            if any(b < a for a, b in zip(values, values[1:])):
                return None, "COUNTER_INCONSISTENT"
            baseline = 0.0
            if lower > begin:
                previous = [
                    r
                    for r in readings
                    if utc(r.sampled_at) <= lower
                    and utc(r.sampled_at) >= lower - timedelta(minutes=interval * 3)
                    and utc(r.sampled_at).astimezone(zone).date() == day
                    and valid(getattr(r, field), 0, maximum)
                ]
                if not previous:
                    # Start at an actual counter measurement rather than
                    # guessing the missing opening reading. Return only the
                    # observed increase and label the shorter span explicitly.
                    if utc(measured[0].sampled_at) - lower > timedelta(minutes=interval * 3):
                        return None, "MISSING_COUNTER_BASELINE"
                    baseline = values[0]
                    partial_boundary = True
                else:
                    baseline = float(getattr(previous[-1], field))
            if max(values) < baseline:
                return None, "COUNTER_INCONSISTENT"
            total += max(values) - baseline
        day += timedelta(days=1)
    return round(total, 3), "PARTIAL_BOUNDARY" if partial_boundary else "AVAILABLE"


def station_summary(station, readings, reference):
    reference = utc(reference)
    start = reference - timedelta(hours=72)
    readings = sorted(
        {utc(r.sampled_at): r for r in readings if utc(r.sampled_at) <= reference}.values(),
        key=lambda r: utc(r.sampled_at),
    )
    window = [r for r in readings if start < utc(r.sampled_at) <= reference]
    interval = max(1, station.expected_interval_minutes or 10)
    expected = max(1, 4320 / interval)
    latest = utc(window[-1].sampled_at) if window else None
    stale = latest is None or (reference - latest).total_seconds() / 60 > max(60, interval * 3)
    from app.services.weather import effective_soil_moisture_unit

    soil_unit = effective_soil_moisture_unit(station)
    values = {}
    statuses = {}
    coverage = {}
    for name, spec in VARIABLES.items():
        numbers = [
            float(getattr(r, spec["field"]))
            for r in window
            if valid(getattr(r, spec["field"]), spec["min"], spec["max"])
        ]
        coverage[name] = round(min(100, len(numbers) * 100 / expected), 1)
        if (name == "soil_moisture" and soil_unit != "%") or (name == "soil_tension" and soil_unit != "cb"):
            value, status = None, "INCOMPATIBLE_UNIT"
        elif stale:
            value, status = None, "STALE"
        elif len(numbers) / expected < 0.8:
            value, status = None, "NOT_REPORTED" if not numbers else "INCOMPLETE_HISTORY"
        elif name in {"rain", "et"}:
            value, status = cumulative_window(readings, station, spec["field"], start, reference)
        else:
            value, status = round(sum(numbers) / len(numbers), 3), "AVAILABLE"
        values[name] = value
        statuses[name] = status
    return {
        "id": str(station.id),
        "name": station.name,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "altitude_m": station.altitude_m,
        "influence_geojson": station_footprint(station),
        "influence_radius_m": station.influence_radius_m,
        "map_color": (station.integration_config or {}).get("map_color", "#277da1"),
        "latest_at": latest.isoformat() if latest else None,
        "stale": stale,
        "samples": len(window),
        "values": values,
        "statuses": statuses,
        "coverage_pct": coverage,
    }


# Small spreads remain small in color, rather than exaggerating sensor noise.
MINIMUM_COLOR_SPAN = {
    "temperature": 1.0,
    "humidity": 5.0,
    "rain": 5.0,
    "soil_temperature": 1.0,
    "soil_moisture": 5.0,
    "soil_tension": 10.0,
    "wind": 1.0,
    "et": 1.0,
}


# Variable-specific sequential/diverging scales, shared by raster, legend and markers.
VARIABLE_PALETTES = {
    "temperature": ["#1764c0", "#f5c400", "#c51b32"],
    "humidity": ["#9ecb18", "#00a882", "#1251a0"],
    "rain": ["#00c4d8", "#2171d1", "#54208c"],
    "soil_temperature": ["#e6bf00", "#ef6600", "#942513"],
    "soil_moisture": ["#a65b10", "#9dbb16", "#008477"],
    "soil_tension": ["#159947", "#e6bf00", "#cf283e"],
    "wind": ["#54208c", "#cd477e", "#e6c700"],
    "et": ["#e6c700", "#ee5415", "#ad1768"],
}


def map_variables(stations):
    result = []
    for key, spec in VARIABLES.items():
        observed = [
            station["values"].get(key) for station in stations if station["values"].get(key) is not None
        ]
        absolute_low, absolute_high = spec["range"]
        low, high = (min(observed), max(observed)) if observed else (absolute_low, absolute_high)
        spread = high - low if observed else None
        if observed and high - low < MINIMUM_COLOR_SPAN[key]:
            center = (low + high) / 2
            low = max(spec["min"], center - MINIMUM_COLOR_SPAN[key] / 2)
            high = min(spec["max"], low + MINIMUM_COLOR_SPAN[key])
            low = max(spec["min"], high - MINIMUM_COLOR_SPAN[key])
        result.append(
            {
                "id": key,
                "label": spec["label"],
                "unit": spec["unit"],
                "minimum": low,
                "maximum": high,
                "absolute_minimum": min(absolute_low, min(observed)) if observed else absolute_low,
                "absolute_maximum": max(absolute_high, max(observed)) if observed else absolute_high,
                "observed_minimum": min(observed) if observed else None,
                "observed_maximum": max(observed) if observed else None,
                "spread": spread,
                "small_difference": spread is not None and spread < MINIMUM_COLOR_SPAN[key],
                "available_stations": len(observed),
                "palette": VARIABLE_PALETTES[key],
            }
        )
    return result


async def build_snapshot(db, reference):
    stations = list(
        (
            await db.scalars(select(Station).where(Station.active.is_(True), Station.deleted_at.is_(None)))
        ).all()
    )
    result = []
    for station in stations:
        rows = list(
            (
                await db.scalars(
                    select(WeatherReading)
                    .where(
                        WeatherReading.station_id == station.id,
                        WeatherReading.sampled_at
                        >= reference
                        - timedelta(hours=72, minutes=max(60, 3 * (station.expected_interval_minutes or 10))),
                        WeatherReading.sampled_at <= reference,
                    )
                    .order_by(WeatherReading.sampled_at)
                )
            ).all()
        )
        result.append(station_summary(station, rows, reference))
    variables = map_variables(result)
    return {
        "reference_at": reference.isoformat(),
        "from": (reference - timedelta(hours=72)).isoformat(),
        "window_hours": 72,
        "stations": result,
        "variables": variables,
        "method": "IDW continuo de estaciones válidas. La envolvente redondeada se desvanece en los bordes y pierde intensidad a mayor distancia de las lecturas; se permite limitarla a los polígonos configurados. No es una medición local ni un modelo calibrado.",
        "limitations": [
            "Sin color fuera de influencia o sin sensor válido.",
            "Las estaciones conservan sus unidades; cb y % son capas distintas.",
            "La lluvia y ET usan la agregación configurada por estación; no se suman contadores diarios.",
            "Los acumulados son registros a la frecuencia del sensor. Una fracción inicial menor al intervalo de lectura se marca como parcial si no fue medida.",
            "Las medias no son normales climatológicas. Son observaciones de las últimas 72 horas.",
            "La corrección por altitud, si se activa, es experimental: −0,65 °C por cada 100 m y solo para temperatura del aire.",
        ],
    }


async def run_regional_analysis(db, job_id):
    from app.db.session import AsyncSessionLocal
    from app.services.source_refresh import refresh_station_source_if_needed

    semaphore = asyncio.Semaphore(3)
    stations = list(
        (
            await db.scalars(select(Station.id).where(Station.active.is_(True), Station.deleted_at.is_(None)))
        ).all()
    )

    async def refresh(identifier):
        async with semaphore, AsyncSessionLocal() as session:
            station = await session.get(Station, identifier)
            try:
                value = await refresh_station_source_if_needed(session, station, requested=True)
                await session.commit()
                # Errors may contain provider internals; expose only state to the public map.
                return {"station_id": str(identifier), "status": value["status"]}
            except Exception:
                await session.rollback()
                return {"station_id": str(identifier), "status": "UNAVAILABLE"}

    refreshed = await asyncio.gather(*(refresh(identifier) for identifier in stations))
    snapshot = await build_snapshot(db, datetime.now(UTC))
    snapshot["source_refresh"] = refreshed
    folder = cache_dir() / "snapshots"
    folder.mkdir(exist_ok=True)
    path = folder / f"{job_id}.json"
    atomic_write(path, json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode())
    return {"snapshot_id": str(job_id), "station_count": len(stations)}


def interpolate(
    snapshot, variable, longitudes, latitudes, heights=None, adjust_altitude=False, regional=False
):
    """Measured influence areas, or a separate regional estimate inside the station hull."""
    if regional:
        return interpolate_region(snapshot, variable, longitudes, latitudes, heights, adjust_altitude)
    numerator = np.zeros(longitudes.shape)
    denominator = np.zeros(longitudes.shape)
    for station in snapshot["stations"]:
        value = station["values"].get(variable)
        footprint = station.get("influence_geojson")
        if value is None or not footprint or station["latitude"] is None or station["longitude"] is None:
            continue
        inside = contains_xy(shape(footprint), longitudes, latitudes)
        lat = np.radians(latitudes)
        slat = np.radians(station["latitude"])
        delta = np.radians(longitudes - station["longitude"])
        angle = np.sin((lat - slat) / 2) ** 2 + np.cos(lat) * np.cos(slat) * np.sin(delta / 2) ** 2
        distance = 6371008.8 * 2 * np.arcsin(np.sqrt(np.clip(angle, 0, 1)))
        weights = np.where(inside, 1 / np.maximum(distance, 50) ** 2, 0)
        corrected = value
        if (
            adjust_altitude
            and variable == "temperature"
            and heights is not None
            and station.get("altitude_m") is not None
        ):
            corrected = value - 0.0065 * (heights - station["altitude_m"])
        numerator += weights * corrected
        denominator += weights
    values = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 0)
    return values, denominator > 0


def interpolate_region(
    snapshot, variable, longitudes, latitudes, heights=None, adjust_altitude=False, *, return_opacity=False
):
    """Continuous IDW with a rounded, feathered envelope and distance confidence.

    All stations contribute continuously; no nearest-neighbour switches, tile-local
    normalization or hard 100 km cuts. Transparency expresses distance, not rain.
    The envelope is computed from the complete snapshot, so adjoining PNGs agree.
    """
    eligible = [
        s
        for s in snapshot["stations"]
        if s["values"].get(variable) is not None
        and s.get("latitude") is not None
        and s.get("longitude") is not None
    ]
    if len(eligible) < 3:
        values, mask = interpolate(snapshot, variable, longitudes, latitudes, heights, adjust_altitude)
        return (values, mask.astype(float)) if return_opacity else (values, mask)
    center_lat = np.mean([s["latitude"] for s in eligible])
    scale_x = 111_195 * np.cos(np.radians(center_lat))
    scale_y = 111_195
    hull = MultiPoint([(s["longitude"] * scale_x, s["latitude"] * scale_y) for s in eligible]).convex_hull
    # Rounded 40 km outer envelope, including collinear station networks.
    envelope = hull.buffer(40_000)
    points_x, points_y = longitudes * scale_x, latitudes * scale_y
    from shapely import distance, points

    point_grid = points(points_x, points_y)
    edge = distance(point_grid, envelope.boundary)
    inside = contains_xy(envelope, points_x, points_y)
    taper = np.clip(edge / 40_000, 0, 1)
    taper = taper * taper * (3 - 2 * taper)
    numerator = np.zeros(longitudes.shape)
    denominator = np.zeros(longitudes.shape)
    nearest = np.full(longitudes.shape, np.inf)
    for station in eligible:
        lat, slat = np.radians(latitudes), np.radians(station["latitude"])
        delta = np.radians(longitudes - station["longitude"])
        angle = np.sin((lat - slat) / 2) ** 2 + np.cos(lat) * np.cos(slat) * np.sin(delta / 2) ** 2
        meters = 6371008.8 * 2 * np.arcsin(np.sqrt(np.clip(angle, 0, 1)))
        nearest = np.minimum(nearest, meters)
        weights = 1 / (meters**2 + 3_000**2)
        value = station["values"][variable]
        if (
            adjust_altitude
            and variable == "temperature"
            and heights is not None
            and station.get("altitude_m") is not None
        ):
            value = value - 0.0065 * (heights - station["altitude_m"])
        numerator += weights * value
        denominator += weights
    values = numerator / denominator
    # Long gaps remain connected but almost transparent; no artificial coverage
    # of the whole country beyond the observed network.
    confidence = np.exp(-0.5 * (nearest / 85_000) ** 2)
    opacity = np.where(inside, taper * confidence, 0)
    return (values, opacity) if return_opacity else (values, opacity > 1 / 255)


async def climate_tile(snapshot, variable, z, x, y, adjust_altitude=False, regional=False, relative=True):
    longitude, latitude = tile_coordinates(z, x, y, 256)
    heights = None
    if adjust_altitude and variable == "temperature":
        terrain = await elevation(z, x, y)
        heights = terrain
    if regional:
        values, alpha = interpolate_region(
            snapshot, variable, longitude, latitude, heights, adjust_altitude, return_opacity=True
        )
        mask = alpha > 0
    else:
        values, mask = interpolate(snapshot, variable, longitude, latitude, heights, adjust_altitude)
        alpha = mask.astype(float)
    spec = next(s for s in map_variables(snapshot["stations"]) if s["id"] == variable)
    low, high = (
        (spec["minimum"], spec["maximum"])
        if relative
        else (spec["absolute_minimum"], spec["absolute_maximum"])
    )
    rgba = colorize(values, low, high, mask=mask, palette=spec["palette"])
    rgba[:, :, 3] = np.rint(rgba[:, :, 3] * alpha).astype(np.uint8)
    return png(rgba)
