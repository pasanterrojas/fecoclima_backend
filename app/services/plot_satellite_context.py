"""Read validated, geometrically scoped Sentinel observations for parcel intelligence.

A preview plot promoted to a private parcel keeps its observations and job.
Only observations explicitly processed for the CURRENT geometry may enter AI context.
"""
from __future__ import annotations
from datetime import datetime
from uuid import UUID
import math
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Plot, SatelliteObservation
from app.services.satellite import satellite_observation_payload, versioned_index_url, local_satellite_image_path, local_satellite_index_image_path, INDEX_NAMES
from app.services.plot_satellite import inspection_point, OPTICAL_HISTORY_LIMIT

VISIBLE_SCENE_LIMIT = OPTICAL_HISTORY_LIMIT


async def plot_optical_history(db: AsyncSession, plot: Plot) -> list[dict]:
    """Previously processed scenes for the current geometry, with private media URLs."""
    rows = await db.stream_scalars(select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot.id,
        SatelliteObservation.provider == 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A',
        SatelliteObservation.raw_payload['processing_geometry'] == plot.geojson,
    ).order_by(SatelliteObservation.captured_at.desc(), SatelliteObservation.created_at.desc(), SatelliteObservation.id.desc()).execution_options(yield_per=30))
    history = []
    async for row in rows:
        if (row.raw_payload or {}).get('processing_geometry') != plot.geojson:
            continue
        item = await satellite_observation_payload(db, row, plot)
        values = list((item.get('indices') or {}).values()) + [item.get('ndvi'), item.get('ndmi'), item.get('ndwi')]
        if not item.get('image_available') and not any((item.get('index_images') or {}).values()) and not any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values):
            continue
        root = f'/parcels/{plot.id}/satellite/{row.id}'
        item['source_url'] = f'{root}/image.jpg' if item.get('image_available') else None
        item['index_images'] = {key: versioned_index_url(root, key, item.get('index_visual_versions') or {}) if url else None for key, url in (item.get('index_images') or {}).items()}
        history.append(item)
        if len(history) == VISIBLE_SCENE_LIMIT:
            break
    await rows.close()
    return history


async def prepare_selected_observation(db: AsyncSession, plot: Plot, observation_id: UUID, browser_session_id: UUID | None) -> UUID:
    """Retain an authorized preview scene when an existing parcel's polygon changes.

    Copy its record instead of moving it, preserving both historical registries.
    The downloaded files are immutable per observation and stay in satellite storage.
    """
    row = await db.get(SatelliteObservation, observation_id)
    if not row or row.provider != 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A' or (row.raw_payload or {}).get('processing_geometry') != plot.geojson:
        raise ValueError('La imagen seleccionada no corresponde al polígono actual. Actualice las imágenes y seleccione otra.')
    if row.plot_id == plot.id:
        return row.id
    source = await db.get(Plot, row.plot_id)
    if (not browser_session_id or not source or source.browser_session_id != browser_session_id
            or source.created_by or source.crop_id or source.deleted_at or not source.active
            or source.geojson != plot.geojson):
        raise ValueError('La imagen seleccionada no está disponible para esta parcela.')
    existing = await db.scalar(select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot.id,
        SatelliteObservation.raw_payload['source_observation_id'].as_string() == str(row.id),
    ).limit(1))
    if existing:
        return existing.id
    copied = SatelliteObservation(**{column.key: getattr(row, column.key) for column in SatelliteObservation.__table__.columns
                                     if column.key not in {'id', 'plot_id', 'created_at', 'updated_at'}})
    copied.plot_id = plot.id
    copied.raw_payload = {**(row.raw_payload or {}), 'source_observation_id': str(row.id), 'source_preview_plot_id': str(source.id)}
    db.add(copied)
    await db.flush()
    return copied.id


async def plot_satellite_context(db: AsyncSession, plot: Plot, reference: datetime | None = None, *, observation_id: UUID | None = None):
    optical_rows = list((await db.scalars(select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot.id,
        SatelliteObservation.provider == 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A',
        *((SatelliteObservation.id == observation_id,) if observation_id else ()),
        *((SatelliteObservation.captured_at <= reference,) if reference else ()),
    ).order_by(SatelliteObservation.captured_at.desc()).limit(30))).all())
    def has_values(row: SatelliteObservation) -> bool:
        values = list(((row.raw_payload or {}).get('indices') or {}).values()) + [row.ndvi, row.ndmi, row.ndwi]
        return any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values)
    optical = next((row for row in optical_rows if (row.raw_payload or {}).get('processing_geometry') == plot.geojson
                    and (has_values(row) or (observation_id and (local_satellite_image_path(row) or any(local_satellite_index_image_path(row, name) for name in INDEX_NAMES))))), None)
    if observation_id and not optical:
        raise ValueError('La imagen seleccionada ya no está disponible para este polígono. Actualice las imágenes y seleccione otra.')
    satellite = await satellite_observation_payload(db, optical, plot) if optical else None
    if satellite:
        satellite['source_url'] = f'/parcels/{plot.id}/satellite/{optical.id}/image.jpg' if satellite.get('image_available') else None
        satellite['index_images'] = {
            name: versioned_index_url(f'/parcels/{plot.id}/satellite/{optical.id}', name, satellite.get('index_visual_versions') or {}) if url else None
            for name, url in (satellite.get('index_images') or {}).items()
        }
    radar_reference = optical.captured_at if observation_id and optical else reference
    radar_rows = list((await db.scalars(select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot.id,
        SatelliteObservation.provider == 'COPERNICUS_SENTINEL_1_GRD',
        *((SatelliteObservation.captured_at <= radar_reference,) if radar_reference else ()),
    ).order_by(SatelliteObservation.captured_at.desc()).limit(15))).all())
    radar = next((row for row in radar_rows if (row.raw_payload or {}).get('processing_geometry') == plot.geojson), None)
    radar_data = None
    if radar:
        radar_data = {'captured_at': radar.captured_at.isoformat(),
                      **{k:(radar.raw_payload or {}).get(k) for k in ('vv_mean','vh_mean','valid_pixels')}}
    return satellite, radar_data, inspection_point(plot.geojson, optical)
