"""Strict WGS84 parcel geometry and metric station coverage. No AI involved."""

from __future__ import annotations
from html import unescape

import json
import math
from typing import Any

from pyproj import CRS, Geod, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plot, PlotStation, Station

GEOD = Geod(ellps="WGS84")


def validate_polygon(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("El polígono debe ser un GeoJSON válido") from exc
    if not isinstance(value, dict):
        raise ValueError("Dibuje un polígono GeoJSON")
    geometry = value.get("geometry") if value.get("type") == "Feature" else value
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Use Polygon o MultiPolygon")
    count = 0

    def check(item: Any) -> None:
        nonlocal count
        if not isinstance(item, list | tuple) or not item:
            raise ValueError("Coordenadas vacías o inválidas")
        if isinstance(item[0], int | float):
            count += 1
            if len(item) != 2 or any(
                isinstance(n, bool) or not isinstance(n, int | float) or not math.isfinite(n) for n in item
            ):
                raise ValueError("Use pares [longitud, latitud] finitos")
            if not -180 <= item[0] <= 180 or not -90 < item[1] < 90:
                raise ValueError("Coordenadas fuera de WGS84")
        else:
            for child in item:
                check(child)

    check(geometry.get("coordinates"))
    if count > 5000:
        raise ValueError("Máximo 5000 vértices por parcela")
    try:
        polygon = shape(geometry)
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("Estructura de polígono inválida") from exc
    if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
        raise ValueError(f"Polígono inválido: {explain_validity(polygon)}")
    if polygon.bounds[2] - polygon.bounds[0] > 10 or polygon.bounds[3] - polygon.bounds[1] > 10:
        raise ValueError("El polígono excede la extensión admitida para una parcela (10 grados)")
    return dict(mapping(polygon))


def metric_projection(geometry: dict):
    center = shape(geometry).centroid
    local = CRS.from_proj4(f"+proj=laea +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m")
    return Transformer.from_crs("EPSG:4326", local, always_xy=True).transform


def parcel_metrics(geometry: dict) -> dict[str, float]:
    polygon = shape(geometry)
    metric = transform(metric_projection(geometry), polygon)
    # representative_point is guaranteed to lie inside even a concave polygon.
    point = polygon.representative_point()
    return {"area_ha": round(metric.area / 10000, 3), "latitude": point.y, "longitude": point.x}


def station_footprint(station: Station) -> dict | None:
    if station.influence_geojson:
        return validate_polygon(station.influence_geojson)
    if station.latitude is None or station.longitude is None or not station.influence_radius_m:
        return None
    ring = []
    for bearing in range(0, 360, 3):
        lon, lat, _ = GEOD.fwd(station.longitude, station.latitude, bearing, station.influence_radius_m)
        ring.append([lon, lat])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def calculate_influences(geometry: dict, stations: list[Station]) -> list[dict[str, Any]]:
    polygon = shape(validate_polygon(geometry))
    project = metric_projection(geometry)
    metric_plot = transform(project, polygon)
    results = []
    for station in stations:
        if not station.active or station.deleted_at:
            continue
        try:
            footprint = station_footprint(station)
        except ValueError:
            continue
        if footprint is None:
            continue
        metric_station = transform(project, shape(footprint))
        overlap = metric_plot.intersection(metric_station)
        if overlap.area <= 0:
            continue
        center = shape(footprint).centroid
        _, _, distance = GEOD.inv(polygon.centroid.x, polygon.centroid.y, center.x, center.y)
        results.append(
            {
                "station_id": str(station.id),
                "name": unescape(station.name),
                "coverage_pct": round(min(100, overlap.area * 100 / metric_plot.area), 4),
                "distance_m": round(distance, 1),
                "influence_geojson": footprint,
                "intersection_geojson": dict(mapping(polygon.intersection(shape(footprint)))),
            }
        )
    return sorted(results, key=lambda row: (-row["coverage_pct"], row["distance_m"], row["station_id"]))


def coverage_percentage(geometry: dict, influences: list[dict]) -> float:
    if not influences:
        return 0.0
    project = metric_projection(geometry)
    polygon = transform(project, shape(geometry))
    union = unary_union([transform(project, shape(item["influence_geojson"])) for item in influences])
    return round(min(100, polygon.intersection(union).area * 100 / polygon.area), 3)


async def sync_plot_influences(db: AsyncSession, plot: Plot) -> tuple[list[Station], list[dict]]:
    if not plot.geojson:
        return [], []
    stations = list(
        (
            await db.scalars(select(Station).where(Station.active.is_(True), Station.deleted_at.is_(None)))
        ).all()
    )
    influences = calculate_influences(plot.geojson, stations)
    await db.execute(delete(PlotStation).where(PlotStation.plot_id == plot.id))
    from uuid import UUID

    for item in influences:
        db.add(
            PlotStation(
                plot_id=plot.id,
                station_id=UUID(item["station_id"]),
                coverage_pct=item["coverage_pct"],
                distance_m=item["distance_m"],
            )
        )
    plot.station_id = UUID(influences[0]["station_id"]) if influences else None
    await db.flush()
    await db.refresh(plot)
    ids = {item["station_id"] for item in influences}
    return [station for station in stations if str(station.id) in ids], influences
