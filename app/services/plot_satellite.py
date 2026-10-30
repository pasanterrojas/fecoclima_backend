"""Sentinel optical/radar products for the actual parcel, with pixel geolocation."""

from __future__ import annotations

import io
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import numpy as np
import tifffile
from shapely.geometry import Point, shape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Plot, SatelliteObservation
from app.services.geospatial import sync_plot_influences
from app.services.satellite import (
    _access_token,
    _bounds,
    _endpoint,
    _feature_datetime,
    _geometry_bbox,
    _integration,
    _search_copernicus_catalog_for_geometry,
    sync_satellite_for_station,
)

OPTICAL_HISTORY_LIMIT = 3


async def sync_plot_optical_history(db: AsyncSession, plot: Plot) -> dict:
    """Download the newest usable scene and two earlier, distinct UTC dates.

    Each retrieval performs the usual polygon quality checks. A historical
    request cannot fall back to the newest cached scene and count it twice.
    Committed observations become visible while the rest is still processing.
    """
    latest = await sync_satellite_for_station(db, plot.station_id, plot_id=plot.id, crop_id=plot.crop_id, force=True)
    scenes: list[dict] = []
    candidate = latest
    cursor = None
    reason = None
    for index in range(OPTICAL_HISTORY_LIMIT):
        if index:
            try:
                candidate = await sync_satellite_for_station(
                    db, plot.station_id, plot_id=plot.id, crop_id=plot.crop_id, before=cursor,
                )
            except Exception as exc:
                await db.rollback()
                reason = f'No se pudo completar la búsqueda histórica ({type(exc).__name__}).'
                break
        values = list((candidate.get('indices') or {}).values()) + [candidate.get('ndvi'), candidate.get('ndmi'), candidate.get('ndwi')]
        has_values = any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values)
        has_image = candidate.get('image_available') or any((candidate.get('index_images') or {}).values())
        if not candidate.get('observation_id') or not candidate.get('captured_at') or not has_values or not has_image:
            reason = 'No se encontraron más escenas con indicadores válidos en el período configurado.'
            break
        captured = datetime.fromisoformat(candidate['captured_at'].replace('Z', '+00:00'))
        if cursor and captured.date() >= cursor.date():
            reason = 'Copernicus no devolvió una fecha anterior distinta.'
            break
        scenes.append({'observation_id': candidate['observation_id'], 'captured_at': candidate['captured_at']})
        cursor = captured
    return {**latest, 'history': scenes, 'history_count': len(scenes),
            'history_status': 'COMPLETE' if len(scenes) == OPTICAL_HISTORY_LIMIT else 'PARTIAL',
            'history_message': f'{len(scenes)} de {OPTICAL_HISTORY_LIMIT} escenas con indicadores disponibles.' + (f' {reason}' if reason else '')}

NDVI_RASTER = """//VERSION=3
function setup(){return {input:["B04","B08","SCL","dataMask"],output:{bands:1,sampleType:"FLOAT32"}};}
function evaluatePixel(s){const d=s.B08+s.B04;
return [s.dataMask && ![0,1,3,8,9,10,11].includes(s.SCL) && d>0 ? (s.B08-s.B04)/d : NaN];}"""
RADAR_RASTER = """//VERSION=3
function setup(){return {input:["VV","VH","dataMask"],output:{bands:3,sampleType:"FLOAT32"}};}
function evaluatePixel(s){return [s.VV,s.VH,s.dataMask];}"""


def pixel_inspection_point(values: np.ndarray, geometry: dict) -> dict | None:
    """Lowest valid optical NDVI pixel inside the polygon; only a field-check lead."""
    if values.ndim != 2:
        return None
    polygon = shape(geometry)
    west, south, east, north = polygon.bounds
    height, width = values.shape
    candidates = np.argwhere(np.isfinite(values) & (values >= -1) & (values <= 1))
    if not len(candidates):
        return None
    ordered = sorted(candidates, key=lambda rc: float(values[tuple(rc)]))
    for row, col in ordered:
        longitude = west + (int(col) + 0.5) * (east - west) / width
        latitude = north - (int(row) + 0.5) * (north - south) / height
        if polygon.covers(Point(longitude, latitude)):
            return {
                "latitude": round(latitude, 7),
                "longitude": round(longitude, 7),
                "method": "lowest_valid_ndvi_pixel",
                "ndvi": round(float(values[row, col]), 4),
                "requires_field_confirmation": True,
                "maps_url": f"https://www.google.com/maps/search/?api=1&query={latitude:.7f},{longitude:.7f}",
            }
    return None


def inspection_point(geometry: dict, observation: SatelliteObservation | None) -> dict:
    stored = (observation.raw_payload or {}).get("inspection_point") if observation else None
    if stored and shape(geometry).covers(Point(stored["longitude"], stored["latitude"])):
        return {**stored, "captured_at": observation.captured_at.isoformat()}
    point = shape(geometry).representative_point()
    return {
        "latitude": round(point.y, 7),
        "longitude": round(point.x, 7),
        "method": "interior_reference_only",
        "requires_field_confirmation": True,
        "reason": "Sin píxel anómalo verificable; punto de referencia dentro de la parcela.",
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={point.y:.7f},{point.x:.7f}",
    }


async def _raster(geometry: dict, captured_at: datetime, *, radar: bool, integration) -> bytes:
    start = captured_at.replace(hour=0, minute=0, second=0, microsecond=0)
    data_filter: dict[str, Any] = {
        "timeRange": {
            "from": start.isoformat().replace("+00:00", "Z"),
            "to": (start + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        },
        "mosaickingOrder": "mostRecent",
    }
    if radar:
        data_filter.update({"polarization": "DV", "acquisitionMode": "IW"})
    payload = {
        "input": {
            "bounds": _bounds(geometry),
            "data": [
                {
                    "type": "sentinel-1-grd" if radar else "sentinel-2-l2a",
                    "dataFilter": data_filter,
                    **(
                        {"processing": {"backCoeff": "GAMMA0_ELLIPSOID", "orthorectify": True}}
                        if radar
                        else {}
                    ),
                }
            ],
        },
        "output": {
            "width": 128,
            "height": 128,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": RADAR_RASTER if radar else NDVI_RASTER,
    }
    token = await _access_token(integration)
    async with httpx.AsyncClient(timeout=max(90, settings.external_http_timeout_seconds)) as client:
        response = await client.post(
            _endpoint(integration, "process_url", settings.copernicus_process_url),
            headers={"Authorization": f"Bearer {token}", "Accept": "image/tiff"},
            json=payload,
        )
        response.raise_for_status()
    return response.content


async def sync_plot_satellite(db: AsyncSession, plot: Plot) -> dict:
    _, influences = await sync_plot_influences(db, plot)
    await db.commit()
    results: dict[str, Any] = {}
    try:
        results["sentinel_2"] = await sync_plot_optical_history(db, plot)
    except Exception as exc:
        await db.rollback()
        results["sentinel_2"] = {"status": "UNAVAILABLE", "reason": type(exc).__name__, "detail": "Revise los registros del servicio satelital"}
    try:
        identifier = results['sentinel_2'].get('observation_id')
        observation = await db.get(SatelliteObservation, UUID(identifier)) if identifier else None
        if observation and not (observation.raw_payload or {}).get("inspection_point"):
            content = await _raster(
                plot.geojson, observation.captured_at, radar=False, integration=await _integration(db)
            )
            pixels = tifffile.imread(io.BytesIO(content))
            point = pixel_inspection_point(pixels, plot.geojson)
            folder = settings.storage_path / "satellite" / str(observation.id)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "ndvi-float32.tiff"
            path.write_bytes(content)
            observation.raw_payload = {
                **observation.raw_payload,
                "inspection_point": point,
                "ndvi_raster_path": str(path),
                "processing_geometry": plot.geojson,
                "influencing_station_ids": [row["station_id"] for row in influences],
            }
            await db.commit()
    except Exception as exc:
        await db.rollback()
        results["inspection_point"] = {"status": "UNAVAILABLE", "reason": type(exc).__name__}
    # Radar uses its own collection and bands: it is never presented as NDVI.
    try:
        integration = await _integration(db)
        now = datetime.now(UTC)
        catalog = await _search_copernicus_catalog_for_geometry(
            integration,
            plot.geojson,
            (now - timedelta(days=30)).date(),
            now.date(),
            collection_override="sentinel-1-grd",
        )
        feature = next(
            (
                f
                for f in catalog["features"]
                if {"VV", "VH"}.issubset(set(f.get("properties", {}).get("sar:polarizations") or []))
            ),
            None,
        )
        if not feature:
            results["sentinel_1"] = {"status": "NO_SCENE"}
            return results
        captured = _feature_datetime(feature)
        existing = await db.scalar(
            select(SatelliteObservation)
            .where(
                SatelliteObservation.plot_id == plot.id,
                SatelliteObservation.captured_at == captured,
                SatelliteObservation.provider == "COPERNICUS_SENTINEL_1_GRD",
            )
            .limit(1)
        )
        if existing and existing.raw_payload.get("processing_geometry") == plot.geojson:
            results["sentinel_1"] = {"status": "CACHED", "id": str(existing.id)}
            return results
        content = await _raster(plot.geojson, captured, radar=True, integration=integration)
        pixels = tifffile.imread(io.BytesIO(content))
        if pixels.ndim != 3 or pixels.shape[-1] != 3:
            raise ValueError("Raster radar inválido")
        valid = (pixels[..., 2] > 0) & np.isfinite(pixels[..., 0]) & np.isfinite(pixels[..., 1])
        if not valid.any():
            raise ValueError("Sin píxeles radar válidos")
        row = existing or SatelliteObservation(
            plot_id=plot.id, captured_at=captured, provider="COPERNICUS_SENTINEL_1_GRD", resolution_m=10
        )
        db.add(row)
        await db.flush()
        folder = settings.storage_path / "satellite" / str(row.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "radar-vv-vh.tiff"
        path.write_bytes(content)
        row.raw_payload = {
            "scene_id": feature.get("id"),
            "processing_status": "PROCESSED",
            "processing_geometry": plot.geojson,
            "bbox": _geometry_bbox(plot.geojson),
            "backscatter_unit": "linear_gamma0",
            "valid_pixels": int(valid.sum()),
            "vv_mean": round(float(pixels[..., 0][valid].mean()), 6),
            "vh_mean": round(float(pixels[..., 1][valid].mean()), 6),
            "raster_path": str(path),
            "influencing_station_ids": [item["station_id"] for item in influences],
        }
        await db.commit()
        results["sentinel_1"] = {"status": "COMPLETED", "id": str(row.id)}
    except Exception as exc:
        await db.rollback()
        results["sentinel_1"] = {"status": "UNAVAILABLE", "reason": type(exc).__name__, "detail": "Revise los registros del servicio satelital"}
    return results
