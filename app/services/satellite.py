from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import IntegrationConfig, Plot, SatelliteObservation


def plot_geometry(plot: Plot) -> dict[str, Any]:
    if plot.geojson:
        geometry = plot.geojson.get("geometry") if plot.geojson.get("type") == "Feature" else plot.geojson
        if not geometry or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError("El GeoJSON de la parcela debe ser Polygon o MultiPolygon")
        return geometry
    if plot.latitude is None or plot.longitude is None:
        raise ValueError("La parcela no tiene polígono ni coordenadas")
    radius = plot.influence_radius_m or 500
    # Aproximación rectangular explícita para búsqueda de catálogo; no se usa como cálculo agronómico.
    delta_lat = radius / 111_320
    delta_lon = radius / (111_320 * max(0.2, abs(__import__("math").cos(__import__("math").radians(plot.latitude)))))
    return {
        "type": "Polygon",
        "coordinates": [[
            [plot.longitude - delta_lon, plot.latitude - delta_lat],
            [plot.longitude + delta_lon, plot.latitude - delta_lat],
            [plot.longitude + delta_lon, plot.latitude + delta_lat],
            [plot.longitude - delta_lon, plot.latitude + delta_lat],
            [plot.longitude - delta_lon, plot.latitude - delta_lat],
        ]],
    }


async def search_copernicus_catalog(
    db: AsyncSession,
    plot_id: UUID,
    date_from: date,
    date_to: date,
    max_cloud_percentage: float = 40,
) -> dict[str, Any]:
    plot = await db.get(Plot, plot_id)
    if not plot or plot.deleted_at:
        raise ValueError("Parcela no encontrada")
    integration = await db.scalar(
        select(IntegrationConfig).where(
            IntegrationConfig.provider == "COPERNICUS_DATA_SPACE",
            IntegrationConfig.active.is_(True),
            IntegrationConfig.deleted_at.is_(None),
        )
    )
    catalog_url = (
        (integration.non_sensitive_config or {}).get("stac_search_url") if integration else None
    ) or settings.copernicus_stac_search_url
    if not catalog_url:
        raise RuntimeError("COPERNICUS_STAC_SEARCH_URL no está configurada")
    payload = {
        "collections": [(integration.non_sensitive_config or {}).get("collection", "sentinel-2-l2a") if integration else "sentinel-2-l2a"],
        "intersects": plot_geometry(plot),
        "datetime": f"{date_from.isoformat()}T00:00:00Z/{date_to.isoformat()}T23:59:59Z",
        "limit": 20,
        "query": {"eo:cloud_cover": {"lte": max_cloud_percentage}},
        "sortby": [{"field": "datetime", "direction": "desc"}],
    }
    async with httpx.AsyncClient(timeout=settings.external_http_timeout_seconds, follow_redirects=True) as client:
        response = await client.post(catalog_url, json=payload)
        response.raise_for_status()
        result = response.json()
    features = result.get("features", []) if isinstance(result, dict) else []
    return {
        "provider": "COPERNICUS_DATA_SPACE",
        "plot_id": str(plot_id),
        "geometry_mode": "polygon" if plot.geojson else "point_buffer_approximation",
        "features": features,
        "count": len(features),
        "note": "La búsqueda de catálogo no calcula NDVI/NDMI. El procesamiento de bandas requiere un procesador satelital configurado.",
    }
