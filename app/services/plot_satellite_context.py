"""Read validated, geometrically scoped Sentinel observations for parcel intelligence.

A preview plot promoted to a private parcel keeps its observations and job.
Only observations explicitly processed for the CURRENT geometry may enter AI context.
"""
from __future__ import annotations
from datetime import datetime
import math
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Plot, SatelliteObservation
from app.services.satellite import satellite_observation_payload, versioned_index_url
from app.services.plot_satellite import inspection_point


async def plot_optical_history(db: AsyncSession, plot: Plot) -> list[dict]:
    """Previously processed scenes for the current geometry, with private media URLs."""
    rows = (await db.scalars(select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot.id,
        SatelliteObservation.provider == 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A',
    ).order_by(SatelliteObservation.captured_at.desc()).limit(90))).all()
    history = []
    for row in rows:
        if (row.raw_payload or {}).get('processing_geometry') != plot.geojson:
            continue
        item = await satellite_observation_payload(db, row, plot)
        values = list((item.get('indices') or {}).values()) + [item.get('ndvi'), item.get('ndmi'), item.get('ndwi')]
        if not any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values):
            continue
        root = f'/parcels/{plot.id}/satellite/{row.id}'
        item['source_url'] = f'{root}/image.jpg' if item.get('image_available') else None
        item['index_images'] = {key: versioned_index_url(root, key, item.get('index_visual_versions') or {}) if url else None for key, url in (item.get('index_images') or {}).items()}
        history.append(item)
    return history


async def plot_satellite_context(db: AsyncSession, plot: Plot, reference: datetime | None = None):
    optical_rows = list((await db.scalars(select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot.id,
        SatelliteObservation.provider == 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A',
        *((SatelliteObservation.captured_at <= reference,) if reference else ()),
    ).order_by(SatelliteObservation.captured_at.desc()).limit(30))).all())
    def has_values(row: SatelliteObservation) -> bool:
        values = list(((row.raw_payload or {}).get('indices') or {}).values()) + [row.ndvi, row.ndmi, row.ndwi]
        return any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values)
    optical = next((row for row in optical_rows if (row.raw_payload or {}).get('processing_geometry') == plot.geojson
                    and has_values(row)), None)
    satellite = await satellite_observation_payload(db, optical, plot) if optical else None
    if satellite:
        satellite['source_url'] = f'/parcels/{plot.id}/satellite/{optical.id}/image.jpg' if satellite.get('image_available') else None
        satellite['index_images'] = {
            name: versioned_index_url(f'/parcels/{plot.id}/satellite/{optical.id}', name, satellite.get('index_visual_versions') or {}) if url else None
            for name, url in (satellite.get('index_images') or {}).items()
        }
    radar_rows = list((await db.scalars(select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot.id,
        SatelliteObservation.provider == 'COPERNICUS_SENTINEL_1_GRD',
        *((SatelliteObservation.captured_at <= reference,) if reference else ()),
    ).order_by(SatelliteObservation.captured_at.desc()).limit(15))).all())
    radar = next((row for row in radar_rows if (row.raw_payload or {}).get('processing_geometry') == plot.geojson), None)
    radar_data = None
    if radar:
        radar_data = {'captured_at': radar.captured_at.isoformat(),
                      **{k:(radar.raw_payload or {}).get(k) for k in ('vv_mean','vh_mean','valid_pixels')}}
    return satellite, radar_data, inspection_point(plot.geojson, optical)
