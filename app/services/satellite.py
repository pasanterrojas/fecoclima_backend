from __future__ import annotations

import asyncio
import json
import math
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import structlog
from sqlalchemy import case, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.db.models import IntegrationConfig, Plot, SatelliteObservation, Station
from app.services.serialization import model_dict
from app.services.spectral_palettes import INDEX_CLASSES, VISUAL_VERSION, index_legend

logger = structlog.get_logger(__name__)
_token_cache: dict[str, dict[str, Any]] = {}
_catalog_scene_cache: dict[str, dict[str, Any]] = {}

INDEX_NAMES = ("ndvi", "ndmi", "ndwi", "evi", "savi", "ndre", "gndvi", "nbr", "msi", "recl")


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coordinate(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    if isinstance(value[0], bool) or isinstance(value[1], bool):
        return None
    try:
        longitude = float(value[0])
        latitude = float(value[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return None
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        return None
    return [longitude, latitude]


def _normalize_ring(value: Any) -> list[list[float]] | None:
    if not isinstance(value, list):
        return None
    ring = [point for item in value if (point := _coordinate(item)) is not None]
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    if len(ring) < 4:
        return None
    # Reject degenerate rings: they are accepted by shallow JSON validation but
    # fail or return empty products in STAC/Process API calls.
    area = 0.0
    for current, following in zip(ring, ring[1:]):
        area += current[0] * following[1] - following[0] * current[1]
    if abs(area) < 1e-12:
        return None
    return ring


def _normalize_polygon(value: Any) -> list[list[list[float]]] | None:
    if not isinstance(value, list) or not value:
        return None
    exterior = _normalize_ring(value[0])
    if not exterior:
        return None
    polygon = [exterior]
    for candidate in value[1:]:
        hole = _normalize_ring(candidate)
        if hole:
            polygon.append(hole)
    return polygon


def normalize_polygon_geometry(value: Any) -> dict[str, Any]:
    """Validate and canonicalize a GeoJSON Polygon/MultiPolygon.

    Feature wrappers are accepted. Rings are closed automatically, invalid
    points/ranges and zero-area polygons are rejected, and only geometry is
    returned so the same canonical object is used by Catalog, Process and
    Statistical APIs.
    """
    if not isinstance(value, dict):
        raise ValueError("El GeoJSON debe ser un objeto JSON")
    geometry = value.get("geometry") if value.get("type") == "Feature" else value
    if not isinstance(geometry, dict):
        raise ValueError("El GeoJSON no contiene una geometría")
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        polygon = _normalize_polygon(coordinates)
        if not polygon:
            raise ValueError("El Polygon no contiene un anillo exterior válido y con área")
        return {"type": "Polygon", "coordinates": polygon}
    if geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise ValueError("El MultiPolygon no contiene polígonos")
        polygons = [polygon for candidate in coordinates if (polygon := _normalize_polygon(candidate))]
        if not polygons:
            raise ValueError("El MultiPolygon no contiene polígonos válidos y con área")
        return {"type": "MultiPolygon", "coordinates": polygons}
    raise ValueError("La geometría debe ser Polygon o MultiPolygon")


def _polygon_geometry(value: Any) -> dict[str, Any] | None:
    try:
        return normalize_polygon_geometry(value)
    except ValueError:
        return None


def _buffer_geometry(latitude: float, longitude: float, radius_m: int | float | None) -> dict[str, Any]:
    radius = float(radius_m or settings.satellite_auto_radius_m)
    delta_lat = radius / 111_320
    delta_lon = radius / (111_320 * max(0.2, abs(math.cos(math.radians(latitude)))))
    return {
        "type": "Polygon",
        "coordinates": [[
            [longitude - delta_lon, latitude - delta_lat],
            [longitude + delta_lon, latitude - delta_lat],
            [longitude + delta_lon, latitude + delta_lat],
            [longitude - delta_lon, latitude + delta_lat],
            [longitude - delta_lon, latitude - delta_lat],
        ]],
    }


def station_geometry(station: Station) -> dict[str, Any]:
    """Return the station geozone. The station CMS is the source of truth."""
    geometry = _polygon_geometry(station.influence_geojson)
    if geometry:
        return geometry
    if station.influence_geojson:
        logger.warning(
            "invalid_station_geojson_falling_back_to_coordinates",
            station_id=str(getattr(station, "id", "")),
            station_name=str(getattr(station, "name", "")),
            geojson_type=str(
                station.influence_geojson.get("type")
                if isinstance(station.influence_geojson, dict)
                else type(station.influence_geojson).__name__
            ),
        )
    if station.latitude is None or station.longitude is None:
        if station.influence_geojson:
            raise ValueError(
                "La estación tiene un GeoJSON inválido y tampoco cuenta con latitud y longitud de respaldo. "
                "Vuelva a guardar el polígono desde /fecoclima-ia/admin/estaciones."
            )
        raise ValueError(
            "La estación no tiene zona de influencia. Dibuje un Polygon/MultiPolygon o configure latitud y longitud."
        )
    return _buffer_geometry(station.latitude, station.longitude, station.influence_radius_m)


def plot_geometry(plot: Plot) -> dict[str, Any]:
    geometry = _polygon_geometry(plot.geojson)
    if geometry:
        return geometry
    if plot.geojson:
        logger.warning(
            "invalid_plot_geojson_falling_back_to_coordinates",
            plot_id=str(getattr(plot, "id", "")),
            plot_name=str(getattr(plot, "name", "")),
            geojson_type=str(plot.geojson.get("type") if isinstance(plot.geojson, dict) else type(plot.geojson).__name__),
        )
    if plot.latitude is None or plot.longitude is None:
        if plot.geojson:
            raise ValueError(
                "La parcela tiene un GeoJSON inválido y tampoco cuenta con coordenadas de respaldo. "
                "Configure un Polygon/MultiPolygon o latitud y longitud."
            )
        raise ValueError("La parcela no tiene polígono ni coordenadas")
    return _buffer_geometry(plot.latitude, plot.longitude, plot.influence_radius_m)


def _managed_station_plot_condition():
    return or_(
        Plot.producer_name == "Polígono configurado en la estación",
        Plot.name.startswith("Zona de influencia ·"),
    )


async def select_station_plot(
    db: AsyncSession,
    station_id: UUID,
    crop_id: UUID | None = None,
) -> Plot | None:
    """Select the managed station geozone and skip malformed legacy plots.

    Sentinel processing is station-centric: the polygon saved in
    /fecoclima-ia/admin/estaciones is authoritative. Crop plots are only a legacy fallback
    until a managed station geozone is created.
    """
    base = [
        Plot.station_id == station_id,
        Plot.active.is_(True),
        Plot.deleted_at.is_(None),
    ]
    managed = await db.scalar(
        select(Plot)
        .where(*base, Plot.crop_id.is_(None), _managed_station_plot_condition())
        .order_by(Plot.updated_at.desc(), Plot.created_at.desc())
        .limit(1)
    )
    if managed:
        return managed

    legacy_query = select(Plot).where(*base)
    if crop_id:
        legacy_query = legacy_query.order_by(
            case((Plot.crop_id == crop_id, 0), (Plot.crop_id.is_(None), 1), else_=2),
            Plot.updated_at.desc(),
        )
    else:
        legacy_query = legacy_query.order_by(
            case((Plot.crop_id.is_(None), 0), else_=1),
            Plot.updated_at.desc(),
        )
    candidates = list((await db.scalars(legacy_query.limit(25))).all())
    for candidate in candidates:
        try:
            plot_geometry(candidate)
            return candidate
        except ValueError:
            logger.warning(
                "invalid_legacy_plot_skipped",
                plot_id=str(candidate.id),
                plot_name=candidate.name,
                station_id=str(station_id),
            )
    return None


async def ensure_station_plot(
    db: AsyncSession,
    station: Station,
    crop_id: UUID | None = None,
) -> Plot:
    """Create/update the internal plot that mirrors the station CMS geozone."""
    geometry = station_geometry(station)
    plot = await db.scalar(
        select(Plot)
        .where(
            Plot.station_id == station.id,
            Plot.active.is_(True),
            Plot.deleted_at.is_(None),
            Plot.crop_id.is_(None),
            _managed_station_plot_condition(),
        )
        .order_by(Plot.updated_at.desc(), Plot.created_at.desc())
        .limit(1)
    )
    station_polygon = station.influence_geojson if _polygon_geometry(station.influence_geojson) else None
    if not plot:
        plot = Plot(
            name=f"Zona de influencia · {station.name}",
            producer_name="Polígono configurado en la estación",
            station_id=station.id,
            crop_id=None,
            latitude=station.latitude,
            longitude=station.longitude,
            geojson=station_polygon,
            influence_radius_m=station.influence_radius_m or settings.satellite_auto_radius_m,
            active=True,
        )
        db.add(plot)
    else:
        plot.name = f"Zona de influencia · {station.name}"
        plot.producer_name = "Polígono configurado en la estación"
        plot.latitude = station.latitude
        plot.longitude = station.longitude
        plot.geojson = station_polygon
        plot.influence_radius_m = station.influence_radius_m or settings.satellite_auto_radius_m
        plot.active = True
    await db.flush()
    await db.refresh(plot)
    # Validate the persisted representation too. A point/radius station keeps
    # geojson=None and uses coordinates; a drawn zone keeps Polygon/MultiPolygon.
    plot_geometry(plot)
    return plot


async def _integration(db: AsyncSession) -> IntegrationConfig | None:
    return await db.scalar(
        select(IntegrationConfig)
        .where(
            IntegrationConfig.provider == "COPERNICUS_DATA_SPACE",
            IntegrationConfig.active.is_(True),
            IntegrationConfig.deleted_at.is_(None),
        )
        .order_by(IntegrationConfig.updated_at.desc(), IntegrationConfig.created_at.desc())
        .limit(1)
    )


def _integration_config(integration: IntegrationConfig | None) -> dict[str, Any]:
    return dict((integration.non_sensitive_config or {}) if integration else {})


def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def satellite_runtime_config(integration: IntegrationConfig | None) -> dict[str, Any]:
    """Return validated Copernicus behaviour configured from Admin → Integraciones.

    The defaults are deliberately conservative for a public application: a
    catalogue refresh is cached, only a bounded number of scenes are screened,
    and a scene is accepted only when the parcel contains enough valid pixels.
    """
    config = _integration_config(integration)
    minimum_coverage = _bounded_number(
        config.get("minimum_valid_coverage_percentage"), 20, 0, 100
    )
    preferred_coverage = _bounded_number(
        config.get("preferred_valid_coverage_percentage"), 60, minimum_coverage, 100
    )
    return {
        "max_cloud_percentage": _bounded_number(
            config.get("max_cloud_percentage"), settings.satellite_max_cloud_percentage, 0, 100
        ),
        # eo:cloud_cover/maxCloudCoverage describe the complete Sentinel tile,
        # not only the parcel. Keep this provider pre-filter broad and decide
        # acceptance with SCL/dataMask coverage inside the GeoJSON.
        "provider_tile_cloud_ceiling_percentage": _bounded_number(
            config.get("provider_tile_cloud_ceiling_percentage"), 90, 0, 100
        ),
        "lookback_days": int(_bounded_number(
            config.get("lookback_days"), settings.satellite_lookback_days, 1, 366
        )),
        "history_days": int(_bounded_number(config.get("history_days"), 60, 7, 366)),
        "history_limit": int(_bounded_number(config.get("history_limit"), 18, 3, 60)),
        "refresh_hours": int(_bounded_number(
            config.get("refresh_hours"), settings.satellite_refresh_hours, 1, 168
        )),
        "catalog_limit": int(_bounded_number(config.get("catalog_limit"), 30, 5, 100)),
        "scene_quality_candidates": int(_bounded_number(
            config.get("scene_quality_candidates"), 12, 1, 40
        )),
        "statistics_resolution_m": _bounded_number(
            config.get("statistics_resolution_m"), 10, 5, 60
        ),
        "minimum_valid_pixels": int(_bounded_number(config.get("minimum_valid_pixels"), 25, 1, 100000)),
        "minimum_valid_coverage_percentage": minimum_coverage,
        "preferred_valid_coverage_percentage": preferred_coverage,
        "max_stale_days": int(_bounded_number(config.get("max_stale_days"), 45, 1, 366)),
        "default_map_opacity": _bounded_number(config.get("default_map_opacity"), 0.78, 0.1, 1),
    }


async def get_satellite_runtime_config(db: AsyncSession) -> dict[str, Any]:
    return satellite_runtime_config(await _integration(db))


def _catalog_url(integration: IntegrationConfig | None) -> str:
    config = _integration_config(integration)
    return str(config.get("stac_search_url") or (integration.base_url if integration else None) or settings.copernicus_stac_search_url)


def _collection(integration: IntegrationConfig | None) -> str:
    config = _integration_config(integration)
    return str(config.get("collection") or "sentinel-2-l2a")


class CatalogTemporaryError(RuntimeError):
    """Transient catalogue failure after retrying official CDSE endpoints."""


def _geometry_bbox(geometry: dict[str, Any]) -> list[float]:
    points: list[tuple[float, float]] = []

    def collect(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
                points.append((float(value[0]), float(value[1])))
                return
            for item in value:
                collect(item)

    collect(geometry.get("coordinates"))
    if not points:
        raise ValueError("La geozona no contiene coordenadas utilizables para consultar el catálogo")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _geometry_points(geometry: dict[str, Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []

    def collect(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
                points.append((float(value[0]), float(value[1])))
                return
            for item in value:
                collect(item)

    collect(geometry.get("coordinates"))
    return points


def _geometry_reference_latitude(geometry: dict[str, Any]) -> float:
    points = _geometry_points(geometry)
    if not points:
        raise ValueError("La geozona no contiene coordenadas utilizables")
    return sum(latitude for _, latitude in points) / len(points)


def _statistics_resolution_degrees(
    geometry: dict[str, Any],
    resolution_m: float,
) -> tuple[float, float]:
    """Convert a metric target resolution to WGS84 degree units.

    Statistical API interprets ``resx`` and ``resy`` in the units of the
    bounds CRS. FECOCLIMA sends GeoJSON in EPSG:4326, therefore passing the
    number ``10`` meant ten *degrees* and collapsed every parcel to one sample.
    This conversion preserves an approximately 10 metre sampling grid at the
    parcel latitude without adding a projection dependency.
    """
    latitude = _geometry_reference_latitude(geometry)
    metres_per_degree_latitude = 111_320.0
    metres_per_degree_longitude = metres_per_degree_latitude * max(
        0.05, abs(math.cos(math.radians(latitude)))
    )
    return (
        float(resolution_m) / metres_per_degree_longitude,
        float(resolution_m) / metres_per_degree_latitude,
    )


def _bounded_statistics_resolution_m(
    geometry: dict[str, Any], requested_resolution_m: float, *, max_side_pixels: int = 1200
) -> float:
    """Keep broad station geozones within the Statistical API's 2500px/side limit.

    A 100 km-radius station covers ~20,000 samples per side at 10 metres. The
    service rejects that request before any fresh image can be accepted. Use a
    coarser *declared* resolution for broad regions, without changing the AOI
    or claiming the resulting pixels represent parcel-level detail.
    """
    west, south, east, north = _geometry_bbox(geometry)
    latitude = _geometry_reference_latitude(geometry)
    width_m = abs(east - west) * 111_320.0 * max(0.05, abs(math.cos(math.radians(latitude))))
    height_m = abs(north - south) * 111_320.0
    return max(float(requested_resolution_m), float(math.ceil(max(width_m, height_m) / (max_side_pixels - 2))))


def _ring_area_m2(ring: list[list[float]], reference_latitude: float) -> float:
    if len(ring) < 4:
        return 0.0
    metres_per_degree_latitude = 111_320.0
    metres_per_degree_longitude = metres_per_degree_latitude * max(
        0.05, abs(math.cos(math.radians(reference_latitude)))
    )
    origin_longitude = float(ring[0][0])
    origin_latitude = float(ring[0][1])
    area = 0.0
    for current, following in zip(ring, ring[1:]):
        x1 = (float(current[0]) - origin_longitude) * metres_per_degree_longitude
        y1 = (float(current[1]) - origin_latitude) * metres_per_degree_latitude
        x2 = (float(following[0]) - origin_longitude) * metres_per_degree_longitude
        y2 = (float(following[1]) - origin_latitude) * metres_per_degree_latitude
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _geometry_area_m2(geometry: dict[str, Any]) -> float:
    reference_latitude = _geometry_reference_latitude(geometry)

    def polygon_area(polygon: list[list[list[float]]]) -> float:
        if not polygon:
            return 0.0
        exterior = _ring_area_m2(polygon[0], reference_latitude)
        holes = sum(_ring_area_m2(ring, reference_latitude) for ring in polygon[1:])
        return max(0.0, exterior - holes)

    if geometry.get("type") == "Polygon":
        return polygon_area(geometry.get("coordinates") or [])
    if geometry.get("type") == "MultiPolygon":
        return sum(polygon_area(polygon) for polygon in geometry.get("coordinates") or [])
    return 0.0


def _catalog_endpoint_candidates(integration: IntegrationConfig | None) -> list[tuple[str, bool]]:
    """Prefer the authenticated Catalog API and retain public STAC as fallback."""
    config = _integration_config(integration)
    primary = _catalog_url(integration).rstrip("/")
    public_stac = str(config.get("public_stac_search_url") or "https://stac.dataspace.copernicus.eu/v1/search").rstrip("/")
    sentinel_hub = str(
        config.get("sentinelhub_catalog_url")
        or "https://sh.dataspace.copernicus.eu/catalog/v1/search"
    ).rstrip("/")
    client_id, client_secret = _oauth_credentials(integration)
    ordered = (sentinel_hub, primary, public_stac) if client_id and client_secret else (primary, public_stac, sentinel_hub)
    values: list[tuple[str, bool]] = []
    for url in ordered:
        if not url or any(existing[0] == url for existing in values):
            continue
        values.append((url, "sh.dataspace.copernicus.eu" in url))
    return values


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.2, min(float(retry_after), 8.0))
            except (TypeError, ValueError):
                pass
    return min(0.5 * (2**attempt) + random.uniform(0.05, 0.25), 4.0)


async def _search_copernicus_catalog_for_geometry(
    integration: IntegrationConfig | None,
    geometry: dict[str, Any],
    date_from: date,
    date_to: date,
    max_cloud_percentage: float = 40,
    max_items: int = 20,
    *,
    collection_override: str | None = None,
) -> dict[str, Any]:
    """Search CDSE robustly, with retries and an authenticated official fallback.

    When OAuth is available, the authenticated Sentinel Hub Catalog endpoint is
    tried first. Public STAC remains a retry/fallback path; transient provider
    errors are never reported as invalid station geometry.
    """
    collection = collection_override or _collection(integration)
    limit = max(1, min(int(max_items), 100))
    datetime_range = f"{date_from.isoformat()}T00:00:00Z/{date_to.isoformat()}T23:59:59Z"
    bbox = _geometry_bbox(geometry)
    full_payload = {
        "collections": [collection],
        "intersects": geometry,
        "datetime": datetime_range,
        "limit": limit,
        "query": {"eo:cloud_cover": {"lte": max_cloud_percentage}},
        "sortby": [{"field": "datetime", "direction": "desc"}],
    }
    simple_intersects = {
        "collections": [collection],
        "intersects": geometry,
        "datetime": datetime_range,
        "limit": limit,
    }
    if collection.startswith("sentinel-1"):
        full_payload.pop("query", None)
    simple_bbox = {
        "collections": [collection],
        "bbox": bbox,
        "datetime": datetime_range,
        "limit": limit,
    }
    get_params = {
        "collections": collection,
        "bbox": ",".join(str(value) for value in bbox),
        "datetime": datetime_range,
        "limit": str(limit),
    }

    timeout = httpx.Timeout(connect=15.0, read=max(45.0, float(settings.external_http_timeout_seconds)), write=30.0, pool=15.0)
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    errors: list[str] = []
    parsed_result: dict[str, Any] | None = None
    used_url: str | None = None
    used_mode: str | None = None

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=2),
        headers={"Accept": "application/geo+json, application/json", "User-Agent": "FecoClimaIA/1.0"},
    ) as client:
        for endpoint, requires_oauth in _catalog_endpoint_candidates(integration):
            headers: dict[str, str] = {}
            if requires_oauth:
                try:
                    headers["Authorization"] = f"Bearer {await _access_token(integration)}"
                except Exception as exc:
                    errors.append(f"{endpoint}: OAuth no disponible ({str(exc)[:240]})")
                    continue

            request_variants: list[tuple[str, str, dict[str, Any]]] = []
            if not requires_oauth:
                request_variants.extend([
                    ("POST", "intersects_filtered", {"json": full_payload}),
                    ("POST", "intersects_simple", {"json": simple_intersects}),
                ])
            request_variants.extend([
                ("POST", "bbox_post", {"json": simple_bbox}),
                ("GET", "bbox_get", {"params": get_params}),
            ])

            for method, mode, kwargs in request_variants:
                last_response: httpx.Response | None = None
                for attempt in range(3):
                    try:
                        response = await client.request(method, endpoint, headers=headers, **kwargs)
                        last_response = response
                    except httpx.RequestError as exc:
                        errors.append(f"{endpoint} {mode} intento {attempt + 1}: {type(exc).__name__}: {str(exc)[:240]}")
                        if attempt < 2:
                            await asyncio.sleep(_retry_delay(None, attempt))
                            continue
                        break

                    if response.status_code < 400:
                        try:
                            candidate = response.json()
                        except (ValueError, json.JSONDecodeError) as exc:
                            errors.append(f"{endpoint} {mode}: respuesta no JSON ({str(exc)[:180]})")
                            candidate = None
                        if isinstance(candidate, dict) and isinstance(candidate.get("features", []), list):
                            parsed_result = candidate
                            used_url = endpoint
                            used_mode = mode
                            break
                        errors.append(f"{endpoint} {mode}: respuesta sin colección 'features'")
                        break

                    body = response.text[:500].replace("\n", " ")
                    errors.append(f"{endpoint} {mode} HTTP {response.status_code}: {body}")
                    if response.status_code in retryable_statuses and attempt < 2:
                        logger.warning(
                            "copernicus_catalog_retry",
                            endpoint=endpoint,
                            mode=mode,
                            status=response.status_code,
                            attempt=attempt + 1,
                        )
                        await asyncio.sleep(_retry_delay(response, attempt))
                        continue
                    break

                if parsed_result is not None:
                    break
                if last_response is not None and last_response.status_code not in retryable_statuses | {400, 404, 405, 422}:
                    # Authentication/permission and other terminal errors should
                    # still allow the next official endpoint, but not repeat this shape.
                    continue
            if parsed_result is not None:
                break

    if parsed_result is None:
        detail = " | ".join(errors[-8:]) or "sin respuesta del catálogo"
        raise CatalogTemporaryError(
            "Copernicus no pudo responder después de varios reintentos y del catálogo alternativo oficial. "
            f"Detalle: {detail}"
        )

    features = parsed_result.get("features", [])
    features = [
        item
        for item in features
        if isinstance(item, dict) and (_feature_cloud(item) is None or _feature_cloud(item) <= max_cloud_percentage)
    ]
    features.sort(key=_feature_datetime, reverse=True)
    return {
        "provider": "COPERNICUS_DATA_SPACE",
        "features": features,
        "count": len(features),
        "catalog_url": used_url or _catalog_url(integration),
        "catalog_mode": used_mode,
        "fallback_used": bool(used_url and used_url.rstrip("/") != _catalog_url(integration).rstrip("/")),
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
    integration = await _integration(db)
    geometry = plot_geometry(plot)
    result = await _search_copernicus_catalog_for_geometry(
        integration,
        geometry,
        date_from,
        date_to,
        max_cloud_percentage,
        20,
    )
    return {
        **result,
        "plot_id": str(plot_id),
        "geometry_mode": "polygon" if _polygon_geometry(plot.geojson) else "point_buffer_approximation",
    }


def _feature_datetime(feature: dict[str, Any]) -> datetime:
    value = (feature.get("properties") or {}).get("datetime") or (feature.get("properties") or {}).get("start_datetime")
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _feature_cloud(feature: dict[str, Any]) -> float | None:
    value = (feature.get("properties") or {}).get("eo:cloud_cover")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _feature_preview(feature: dict[str, Any]) -> str | None:
    assets = feature.get("assets") or {}
    for key in ("thumbnail", "preview", "visual", "rendered_preview", "overview"):
        href = (assets.get(key) or {}).get("href")
        if href:
            return str(href)
    for asset in assets.values():
        href = asset.get("href") if isinstance(asset, dict) else None
        media_type = str(asset.get("type") or "") if isinstance(asset, dict) else ""
        roles = asset.get("roles") or [] if isinstance(asset, dict) else []
        if href and (media_type.startswith("image/") or "thumbnail" in roles or "overview" in roles):
            return str(href)
    return None


def _oauth_credentials(integration: IntegrationConfig | None) -> tuple[str | None, str | None]:
    config = _integration_config(integration)
    client_id = str(config.get("client_id") or settings.copernicus_client_id or "").strip() or None
    client_secret = settings.copernicus_client_secret
    decrypted = decrypt_secret(integration.encrypted_secret) if integration else None
    if decrypted:
        try:
            parsed = json.loads(decrypted)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            client_id = str(parsed.get("client_id") or client_id or "").strip() or None
            client_secret = str(parsed.get("client_secret") or parsed.get("secret") or "").strip() or None
        else:
            client_secret = decrypted.strip() or None
    return client_id, client_secret


def _endpoint(integration: IntegrationConfig | None, key: str, default: str) -> str:
    return str(_integration_config(integration).get(key) or default)


async def copernicus_credentials_configured(db: AsyncSession) -> bool:
    integration = await _integration(db)
    client_id, client_secret = _oauth_credentials(integration)
    return bool(client_id and client_secret)


async def _access_token(integration: IntegrationConfig | None) -> str:
    client_id, client_secret = _oauth_credentials(integration)
    if not client_id or not client_secret:
        raise RuntimeError(
            "Faltan credenciales OAuth de Copernicus. Configure client_id en Parámetros JSON y el client_secret en el campo Secreto."
        )
    token_url = _endpoint(integration, "token_url", settings.copernicus_token_url)
    cache_key = f"{token_url}|{client_id}"
    now = datetime.now(UTC)
    cached = _token_cache.get(cache_key) or {}
    if cached.get("token") and cached.get("expires_at") and cached["expires_at"] > now + timedelta(seconds=60):
        return str(cached["token"])
    async with httpx.AsyncClient(timeout=settings.external_http_timeout_seconds) as client:
        response = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OAuth Copernicus respondió HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("OAuth Copernicus no devolvió access_token")
    _token_cache[cache_key] = {
        "token": token,
        "expires_at": now + timedelta(seconds=int(payload.get("expires_in", 600))),
    }
    return str(token)


TRUE_COLOR_EVALSCRIPT = """//VERSION=3
function setup() { return { input: ["B02","B03","B04","dataMask"], output: { bands: 3, sampleType: "AUTO" } }; }
function evaluatePixel(s) { return [2.5*s.B04*s.dataMask, 2.5*s.B03*s.dataMask, 2.5*s.B02*s.dataMask]; }
"""

INDEX_EXPRESSIONS = {
    "ndvi": "ratio(s.B08,s.B04)", "ndmi": "ratio(s.B08,s.B11)",
    "ndwi": "ratio(s.B03,s.B08)",
    "evi": "2.5*divide(s.B08-s.B04,s.B08+6*s.B04-7.5*s.B02+1)",
    "savi": "1.5*divide(s.B08-s.B04,s.B08+s.B04+0.5)",
    "ndre": "ratio(s.B8A,s.B05)", "gndvi": "ratio(s.B08,s.B03)",
    "nbr": "ratio(s.B08,s.B12)", "msi": "divide(s.B11,s.B08)",
    "recl": "divide(s.B07,s.B05)-1",
}
INDEX_VISUAL_CONFIG = {
    name: {**config, "expression": INDEX_EXPRESSIONS[name]} for name, config in INDEX_CLASSES.items()
}


def _index_visual_evalscript(index_name: str) -> str:
    config = INDEX_VISUAL_CONFIG.get(index_name)
    if not config:
        raise ValueError(f"Índice visual no soportado: {index_name}")
    palette = [[int(color[i:i+2], 16) / 255 for i in (1, 3, 5)] for color in config["colors"]]
    return f"""//VERSION=3
// {VISUAL_VERSION}: fixed classes; identical for every acquisition.
function setup() {{
  return {{ input: [{{bands:["B02","B03","B04","B05","B07","B08","B8A","B11","B12","SCL","dataMask"]}}], output: {{bands:4, sampleType:"AUTO"}} }};
}}
function ratio(a,b) {{ return Math.abs(a+b) < 0.000001 ? NaN : (a-b)/(a+b); }}
function divide(a,b) {{ return Math.abs(b) < 0.000001 ? NaN : a/b; }}
function palette(value) {{
  const breaks = {json.dumps(config['breaks'])};
  const colors = {json.dumps(palette, separators=(',', ':'))};
  for (let i=0; i<breaks.length; i++) if (value <= breaks[i]) return colors[i];
  return colors[colors.length-1];
}}
function evaluatePixel(s) {{
  const invalid = s.dataMask !== 1 || [0,1,3,8,9,10,11].includes(s.SCL);
  if (invalid) return [0,0,0,0];
  const value = {config['expression']};
  if (!Number.isFinite(value)) return [0,0,0,0];
  const color = palette(value);
  return [color[0],color[1],color[2],1];
}}
"""


def _current_visual_paths(observation: SatelliteObservation | None) -> dict[str, str]:
    if not observation:
        return {}
    versions = (observation.raw_payload or {}).get("index_visual_versions") or {}
    return {name: path for name, path in _local_index_paths(observation).items()
            if versions.get(name) == VISUAL_VERSION}


def versioned_index_url(root: str, name: str, versions: dict[str, str]) -> str:
    suffix = f"?v={VISUAL_VERSION}" if versions.get(name) == VISUAL_VERSION else ""
    return f"{root}/{name}.png{suffix}"


def _visual_metadata(observation: SatelliteObservation) -> dict[str, Any]:
    raw = observation.raw_payload or {}
    versions = raw.get("index_visual_versions") or {}
    paths = _local_index_paths(observation)
    current = {name for name in paths if versions.get(name) == VISUAL_VERSION}
    return {
        "index_visual_versions": {name: versions.get(name, "legacy-continuous") for name in paths},
        "index_legends": {name: index_legend(name) for name in current},
        "visual_grid": raw.get("visual_grid"),
        "classification_warning": (
            "Hay capas con la escala anterior. Actualice las fuentes para aplicar los rangos del cliente."
            if paths.keys() - current else None
        ),
    }


def _visual_grid(geometry: dict[str, Any]) -> dict[str, Any]:
    # WGS84 output, metric size at the AOI latitude; bounded for the provider.
    resolution = _bounded_statistics_resolution_m(geometry, 5, max_side_pixels=2500)
    resx, resy = _statistics_resolution_degrees(geometry, resolution)
    west, south, east, north = _geometry_bbox(geometry)
    return {"width": max(1, math.ceil((east-west)/resx)),
            "height": max(1, math.ceil((north-south)/resy)),
            "target_resolution_m": 5, "output_resolution_m": resolution,
            "native_resolution_m": [10, 20], "resampling": "NEAREST",
            "note": "Malla aproximada a la latitud de la parcela; remuestreo sin detalle nativo adicional."}


def _scene_time_range(captured_at: datetime) -> tuple[datetime, datetime]:
    """Return a narrow acquisition window for RGB and visual index rasters."""
    value = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value - timedelta(minutes=5), value + timedelta(minutes=45)


def _statistics_time_range(captured_at: datetime) -> tuple[datetime, datetime]:
    """Return the full UTC day required by a P1D Statistical API interval.

    The previous implementation sent a roughly 50-minute range together with
    ``P1D``. Copernicus normally skips an incomplete final interval, which can
    produce an HTTP 200 response with no statistical rows. Using the complete
    UTC day keeps the statistics tied to the same acquisition date while
    satisfying the aggregation contract.
    """
    value = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    start = datetime(value.year, value.month, value.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


INDEX_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{bands:["B02","B03","B04","B05","B07","B08","B8A","B11","B12","SCL","dataMask"]}],
    output: [
      {id:"ndvi", bands:1, sampleType:"FLOAT32"},
      {id:"ndmi", bands:1, sampleType:"FLOAT32"},
      {id:"ndwi", bands:1, sampleType:"FLOAT32"},
      {id:"evi", bands:1, sampleType:"FLOAT32"},
      {id:"savi", bands:1, sampleType:"FLOAT32"},
      {id:"ndre", bands:1, sampleType:"FLOAT32"},
      {id:"gndvi", bands:1, sampleType:"FLOAT32"},
      {id:"nbr", bands:1, sampleType:"FLOAT32"},
      {id:"msi", bands:1, sampleType:"FLOAT32"},
      {id:"recl", bands:1, sampleType:"FLOAT32"},
      {id:"dataMask", bands:1}
    ]
  };
}
function ratio(a,b) { return Math.abs(a+b) < 0.000001 ? 0 : (a-b)/(a+b); }
function divide(a,b) { return Math.abs(b) < 0.000001 ? 0 : a/b; }
function evaluatePixel(s) {
  const invalid = [0,1,3,8,9,10,11].includes(s.SCL);
  const valid = s.dataMask === 1 && !invalid ? 1 : 0;
  return {
    ndvi:[ratio(s.B08,s.B04)],
    ndmi:[ratio(s.B08,s.B11)],
    ndwi:[ratio(s.B03,s.B08)],
    evi:[2.5*divide(s.B08-s.B04,s.B08+6*s.B04-7.5*s.B02+1)],
    savi:[1.5*divide(s.B08-s.B04,s.B08+s.B04+0.5)],
    ndre:[ratio(s.B8A,s.B05)],
    gndvi:[ratio(s.B08,s.B03)],
    nbr:[ratio(s.B08,s.B12)],
    msi:[divide(s.B11,s.B08)],
    recl:[Math.abs(s.B05) < 0.000001 ? NaN : s.B07/s.B05-1],
    dataMask:[valid]
  };
}
"""



def _bounds(geometry: dict[str, Any]) -> dict[str, Any]:
    return {"geometry": geometry, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}}


def _process_payload(
    geometry: dict[str, Any],
    time_from: datetime,
    time_to: datetime,
    max_cloud: float,
    *,
    evalscript: str,
    media_type: str,
) -> dict[str, Any]:
    grid = _visual_grid(geometry)
    # SCL is categorical: nearest-neighbour preserves cloud classes as integers.
    return {
        "input": {
            "bounds": _bounds(geometry),
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": time_from.isoformat().replace("+00:00", "Z"), "to": time_to.isoformat().replace("+00:00", "Z")},
                    "mosaickingOrder": "mostRecent",
                    "maxCloudCoverage": max_cloud,
                },
                "processing": {"upsampling": "NEAREST", "downsampling": "NEAREST"},
            }],
        },
        "output": {"width": grid["width"], "height": grid["height"], "responses": [{"identifier": "default", "format": {"type": media_type}}]},
        "evalscript": evalscript,
    }


async def _process_raster(
    geometry: dict[str, Any],
    time_from: datetime,
    time_to: datetime,
    max_cloud: float,
    integration: IntegrationConfig | None,
    *,
    evalscript: str,
    media_type: str,
    token: str | None = None,
) -> bytes:
    access_token = token or await _access_token(integration)
    payload = _process_payload(
        geometry, time_from, time_to, max_cloud, evalscript=evalscript, media_type=media_type
    )
    async with httpx.AsyncClient(timeout=max(90, settings.external_http_timeout_seconds)) as client:
        response = await client.post(
            _endpoint(integration, "process_url", settings.copernicus_process_url),
            headers={"Authorization": f"Bearer {access_token}", "Accept": media_type},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Process API HTTP {response.status_code}: {response.text[:1000]}")
        if not response.content:
            raise RuntimeError("Process API respondió sin contenido de imagen")
        if media_type == "image/png" and not response.content.startswith(b"\x89PNG"):
            raise RuntimeError("Process API no devolvió un PNG válido para el índice")
        if media_type == "image/jpeg" and not response.content.startswith(b"\xff\xd8"):
            raise RuntimeError("Process API no devolvió un JPEG válido")
        return response.content


async def _process_image(geometry: dict[str, Any], time_from: datetime, time_to: datetime, max_cloud: float, integration: IntegrationConfig | None) -> bytes:
    return await _process_raster(
        geometry, time_from, time_to, max_cloud, integration, evalscript=TRUE_COLOR_EVALSCRIPT, media_type="image/jpeg"
    )


async def _process_index_images(
    geometry: dict[str, Any],
    time_from: datetime,
    time_to: datetime,
    max_cloud: float,
    integration: IntegrationConfig | None,
    *,
    only: set[str] | None = None,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Render all index maps from the same scene window, with bounded concurrency."""
    token = await _access_token(integration)
    semaphore = asyncio.Semaphore(3)
    names = [name for name in INDEX_NAMES if only is None or name in only]

    async def render(name: str) -> tuple[str, bytes | None, str | None]:
        async with semaphore:
            try:
                content = await _process_raster(
                    geometry, time_from, time_to, max_cloud, integration,
                    evalscript=_index_visual_evalscript(name), media_type="image/png", token=token,
                )
                return name, content, None
            except Exception as exc:
                return name, None, str(exc)[:1000]

    rendered = await asyncio.gather(*(render(name) for name in names))
    images = {name: content for name, content, error in rendered if content is not None}
    errors = {name: error for name, content, error in rendered if error}
    return images, errors


def _stat_value(stats: dict[str, Any], key: str) -> float | int | None:
    value = stats.get(key)
    if value is None:
        return None
    try:
        if not math.isfinite(float(value)):
            return None
        return int(value) if key in {"sampleCount", "noDataCount"} else round(float(value), 4)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_output_statistics(result: dict[str, Any], output_name: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for interval in result.get("data") or []:
        outputs = interval.get("outputs") or {}
        bands = (outputs.get(output_name) or {}).get("bands") or {}
        stats = (bands.get("B0") or {}).get("stats") or {}
        if stats:
            rows.append(stats)
    if not rows:
        return {}

    sample_count = sum(int(item.get("sampleCount") or 0) for item in rows)
    no_data_count = sum(int(item.get("noDataCount") or 0) for item in rows)
    valid_pixel_count = max(0, sample_count - no_data_count)

    weighted_rows: list[tuple[float, int]] = []
    for item in rows:
        mean = item.get("mean")
        if mean is None or not math.isfinite(float(mean)):
            continue
        row_samples = int(item.get("sampleCount") or 0)
        row_no_data = int(item.get("noDataCount") or 0)
        row_valid = max(0, row_samples - row_no_data)
        if row_valid:
            weighted_rows.append((float(mean), row_valid))
    weighted_total = sum(weight for _, weight in weighted_rows)
    weighted_mean = (
        sum(value * weight for value, weight in weighted_rows) / weighted_total
        if weighted_total
        else None
    )
    percentiles = rows[-1].get("percentiles") or {}
    coverage = round(valid_pixel_count * 100 / sample_count, 2) if sample_count else 0.0
    minima = [float(item["min"]) for item in rows if item.get("min") is not None and math.isfinite(float(item["min"]))]
    maxima = [float(item["max"]) for item in rows if item.get("max") is not None and math.isfinite(float(item["max"]))]
    return {
        "min": min(minima) if minima else None,
        "max": max(maxima) if maxima else None,
        "mean": None if weighted_mean is None else round(weighted_mean, 4),
        "stdev": _stat_value(rows[-1], "stDev"),
        "sample_count": sample_count,
        "no_data_count": no_data_count,
        "valid_pixel_count": valid_pixel_count,
        "valid_coverage_percentage": coverage,
        "percentiles": {str(key): round(float(value), 4) for key, value in percentiles.items() if value is not None and math.isfinite(float(value))},
    }


def _statistics_quality(statistics: dict[str, Any], *, interval_count: int = 0) -> dict[str, Any]:
    reference = statistics.get("ndvi") or next(
        (value for value in statistics.values() if isinstance(value, dict) and value),
        {},
    )
    total_pixels = int(reference.get("sample_count") or 0)
    no_data_pixels = int(reference.get("no_data_count") or 0)
    valid_pixels = int(reference.get("valid_pixel_count") or max(0, total_pixels - no_data_pixels))
    valid_coverage = (
        float(reference.get("valid_coverage_percentage"))
        if reference.get("valid_coverage_percentage") is not None
        else (valid_pixels * 100 / total_pixels if total_pixels else 0.0)
    )
    status = "COMPLETED" if valid_pixels > 0 else "EMPTY_INTERVAL" if interval_count == 0 else "NO_VALID_PIXELS"
    return {
        "status": status,
        "interval_count": int(interval_count),
        "total_pixels": total_pixels,
        "valid_pixels": valid_pixels,
        "no_data_pixels": no_data_pixels,
        "valid_coverage_percentage": round(valid_coverage, 2),
    }


async def _index_statistics(
    geometry: dict[str, Any],
    captured_at: datetime,
    max_cloud: float,
    integration: IntegrationConfig | None,
    *,
    resolution_m: float = 10.0,
) -> dict[str, Any]:
    token = await _access_token(integration)
    time_from, time_to = _statistics_time_range(captured_at)
    resolution_m = _bounded_statistics_resolution_m(geometry, resolution_m)
    resx_degrees, resy_degrees = _statistics_resolution_degrees(geometry, resolution_m)
    area_m2 = _geometry_area_m2(geometry)
    estimated_pixels = max(1, int(round(area_m2 / max(1.0, resolution_m**2))))
    calculations = {
        name: {
            "statistics": {
                "default": {"percentiles": {"k": [10, 25, 50, 75, 90]}}
            }
        }
        for name in INDEX_NAMES
    }
    payload = {
        "input": {
            "bounds": _bounds(geometry),
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": time_from.isoformat().replace("+00:00", "Z"), "to": time_to.isoformat().replace("+00:00", "Z")},
                    "mosaickingOrder": "mostRecent",
                    "maxCloudCoverage": max_cloud,
                },
            }],
        },
        "aggregation": {
            "timeRange": {"from": time_from.isoformat().replace("+00:00", "Z"), "to": time_to.isoformat().replace("+00:00", "Z")},
            "aggregationInterval": {"of": "P1D"},
            "lastIntervalBehavior": "SHORTEN",
            "evalscript": INDEX_EVALSCRIPT,
            # Bounds are EPSG:4326. Statistical API reads resx/resy in CRS
            # units, therefore these values must be degrees, not metres.
            "resx": resx_degrees,
            "resy": resy_degrees,
        },
        "calculations": calculations,
    }
    async with httpx.AsyncClient(timeout=max(90, settings.external_http_timeout_seconds)) as client:
        response = await client.post(
            _endpoint(integration, "statistics_url", settings.copernicus_statistics_url),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Statistical API HTTP {response.status_code}: {response.text[:1000]}")
        result = response.json()
    if result.get("status") not in {None, "OK"}:
        raise RuntimeError(f"Statistical API devolvió estado {result.get('status')}: {json.dumps(result, ensure_ascii=False)[:1000]}")
    interval_count = len(result.get("data") or [])
    statistics = {name: _parse_output_statistics(result, name) for name in INDEX_NAMES}
    indices = {name: (statistics[name].get("mean") if statistics[name] else None) for name in INDEX_NAMES}
    quality = _statistics_quality(statistics, interval_count=interval_count)
    quality.update({
        "estimated_aoi_pixels": estimated_pixels,
        "geometry_area_m2": round(area_m2, 2),
        "statistics_resolution_m": float(resolution_m),
        "statistics_resx_degrees": resx_degrees,
        "statistics_resy_degrees": resy_degrees,
    })
    return {
        **indices,
        "indices": indices,
        "statistics": statistics,
        "quality": quality,
        "statistics_grid": {
            "crs": "EPSG:4326",
            "resolution_m": float(resolution_m),
            "resx_degrees": resx_degrees,
            "resy_degrees": resy_degrees,
            "geometry_area_m2": round(area_m2, 2),
            "estimated_aoi_pixels": estimated_pixels,
        },
        "statistics_time_range": {
            "from": time_from.isoformat(),
            "to": time_to.isoformat(),
        },
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _observation_indices(observation: SatelliteObservation | None) -> dict[str, Any]:
    if not observation:
        return {}
    raw = observation.raw_payload or {}
    values = dict(raw.get("indices") or {})
    values.setdefault("ndvi", observation.ndvi)
    values.setdefault("ndmi", observation.ndmi)
    values.setdefault("ndwi", observation.ndwi)
    return values


def satellite_observation_is_complete(observation: SatelliteObservation | None) -> bool:
    if not observation:
        return False
    raw = observation.raw_payload or {}
    has_indices = any(value is not None for value in _observation_indices(observation).values())
    return bool(
        has_indices
        and local_satellite_image_path(observation)
        and set(INDEX_NAMES) - {"recl"} <= _local_index_paths(observation).keys()
        and str(raw.get("processing_status") or "") == "PROCESSED"
    )


def _observation_quality(observation: SatelliteObservation | None) -> dict[str, Any]:
    if not observation:
        return {}
    raw = observation.raw_payload or {}
    quality = raw.get("quality")
    if isinstance(quality, dict) and quality:
        return dict(quality)
    statistics = raw.get("statistics") or {}
    return _statistics_quality(statistics, interval_count=1 if statistics else 0)


def _effective_minimum_valid_pixels(quality: dict[str, Any], runtime: dict[str, Any]) -> int:
    total_pixels = int(quality.get("total_pixels") or 0)
    configured_minimum = int(runtime["minimum_valid_pixels"])
    if total_pixels <= 0:
        return configured_minimum
    coverage_minimum = max(
        1,
        math.ceil(total_pixels * float(runtime["minimum_valid_coverage_percentage"]) / 100),
    )
    return min(configured_minimum, coverage_minimum)


def _quality_acceptance(quality: dict[str, Any], runtime: dict[str, Any]) -> tuple[bool, str]:
    valid_pixels = int(quality.get("valid_pixels") or 0)
    total_pixels = int(quality.get("total_pixels") or 0)
    estimated_pixels = int(quality.get("estimated_aoi_pixels") or 0)
    coverage = float(quality.get("valid_coverage_percentage") or 0)
    effective_minimum = _effective_minimum_valid_pixels(quality, runtime)
    quality["effective_minimum_valid_pixels"] = effective_minimum

    if quality.get("status") == "EMPTY_INTERVAL":
        return False, "Statistical API no devolvió intervalos para la fecha"
    if total_pixels <= 1 and estimated_pixels >= 4:
        return False, (
            "La cuadrícula estadística produjo un solo píxel para una geozona mayor; "
            "revise la resolución y el CRS"
        )
    if valid_pixels < effective_minimum:
        return False, f"Solo {valid_pixels} píxeles válidos; mínimo efectivo {effective_minimum}"
    if coverage < float(runtime["minimum_valid_coverage_percentage"]):
        return False, (
            f"Cobertura válida {coverage:.1f}%; mínimo "
            f"{float(runtime['minimum_valid_coverage_percentage']):.1f}%"
        )
    return True, "Escena aceptada"


def _quality_band(quality: dict[str, Any], runtime: dict[str, Any]) -> str:
    accepted, _ = _quality_acceptance(quality, runtime)
    if not accepted:
        return "REJECTED"
    coverage = float(quality.get("valid_coverage_percentage") or 0)
    if coverage >= float(runtime["preferred_valid_coverage_percentage"]):
        return "PREFERRED"
    return "ACCEPTABLE"


def _observation_freshness(
    observation: SatelliteObservation,
    runtime: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(UTC)
    captured_at = observation.captured_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    else:
        captured_at = captured_at.astimezone(UTC)
    age_days = max(0.0, (reference - captured_at).total_seconds() / 86_400)
    max_stale_days = int(runtime["max_stale_days"])
    return {
        "data_age_days": round(age_days, 1),
        "data_is_stale": age_days > max_stale_days,
        "max_stale_days": max_stale_days,
    }


def _warning_with_staleness(
    warning: str | None,
    freshness: dict[str, Any],
) -> str | None:
    if not freshness.get("data_is_stale"):
        return warning
    stale_message = (
        f"El último producto válido tiene {float(freshness['data_age_days']):.1f} días de antigüedad; "
        f"supera el máximo configurado de {int(freshness['max_stale_days'])} días."
    )
    if warning and stale_message not in warning:
        return f"{warning} {stale_message}"
    return warning or stale_message


async def _latest_complete_observation(
    db: AsyncSession,
    plot_id: UUID,
    *,
    before: datetime | None = None,
) -> SatelliteObservation | None:
    query = select(SatelliteObservation).where(SatelliteObservation.plot_id == plot_id)
    if before is not None:
        query = query.where(SatelliteObservation.captured_at < before)
    rows = list((await db.scalars(query.order_by(SatelliteObservation.captured_at.desc()).limit(80))).all())
    return next((row for row in rows if satellite_observation_is_complete(row)), None)


def _catalog_checked_at(observation: SatelliteObservation | None) -> datetime | None:
    if not observation:
        return None
    raw = observation.raw_payload or {}
    value = _parse_datetime(raw.get("catalog_checked_at")) or observation.updated_at or observation.created_at
    return _parse_datetime(value)


def satellite_catalog_cache_is_stale(
    observation: SatelliteObservation | None,
    runtime: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    checked_at = _catalog_checked_at(observation)
    if checked_at is None:
        return True
    reference = now or datetime.now(UTC)
    return checked_at < reference - timedelta(hours=max(1, int(runtime["refresh_hours"])))


def _cached_observation_result(
    observation: SatelliteObservation,
    plot: Plot,
    *,
    credentials_ready: bool,
    runtime: dict[str, Any],
    status: str | None = None,
    warning: str | None = None,
) -> dict[str, Any]:
    raw = observation.raw_payload or {}
    index_paths = _local_index_paths(observation)
    quality = _observation_quality(observation)
    quality.setdefault("quality_band", _quality_band(quality, runtime))
    freshness = _observation_freshness(observation, runtime)
    resolved_warning = _warning_with_staleness(warning or raw.get("data_warning"), freshness)
    return {
        "status": status or str(raw.get("processing_status") or "PROCESSED"),
        "observation_id": str(observation.id),
        "captured_at": observation.captured_at.isoformat(),
        "cloud_percentage": observation.cloud_percentage,
        "ndvi": observation.ndvi,
        "ndmi": observation.ndmi,
        "ndwi": observation.ndwi,
        "indices": _observation_indices(observation),
        "statistics": raw.get("statistics") or {},
        "quality": quality,
        "processing_error": raw.get("processing_error"),
        "image_available": bool(local_satellite_image_path(observation)),
        "index_images": {name: versioned_index_url(f"/api/v1/public/satellite/{observation.id}/indices", name, raw.get("index_visual_versions") or {}) for name in index_paths},
        "visual_layers_available": len(index_paths),
        **_visual_metadata(observation),
        "processing_credentials_configured": credentials_ready,
        "plot_id": str(plot.id),
        "data_source_mode": raw.get("data_source_mode") or "LATEST_VALID_SCENE",
        "fallback_used": bool(raw.get("fallback_used")),
        "data_warning": resolved_warning,
        **freshness,
        "latest_catalog_scene_at": raw.get("latest_catalog_scene_at"),
        "catalog_checked_at": raw.get("catalog_checked_at"),
        "selected_scene_rank": raw.get("selected_scene_rank"),
        "runtime_config": runtime,
    }


async def _acquire_station_refresh_lock(db: AsyncSession, station_id: UUID | str, crop_id: UUID | None) -> None:
    """Serialize one Copernicus refresh per station/crop across API workers.

    PostgreSQL transaction advisory locks prevent simultaneous public requests
    from launching duplicate Statistical/Process API calls. The lock is released
    automatically when this session commits or rolls back. Tests using another
    SQL dialect keep the no-op behaviour.
    """
    try:
        bind = db.get_bind()
        if bind.dialect.name != "postgresql":
            return
        key = f"fecoclima:satellite:{station_id}:{crop_id or 'all'}"
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
    except Exception as exc:
        logger.warning("satellite_refresh_lock_unavailable", error=str(exc)[:500])


async def _evaluate_catalog_candidates(
    db: AsyncSession,
    plot: Plot,
    features: list[dict[str, Any]],
    geometry: dict[str, Any],
    integration: IntegrationConfig | None,
    runtime: dict[str, Any],
    *,
    explicit_scene_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = features
    if explicit_scene_id:
        candidates = [item for item in features if str(item.get("id")) == str(explicit_scene_id)]
    else:
        # Statistical API mosaics one output per UTC day. Evaluate only the
        # newest catalogue item for each date so metadata and statistics stay
        # coherent and duplicate same-day calls are avoided.
        unique_by_day: list[dict[str, Any]] = []
        seen_days: set[date] = set()
        for item in candidates:
            acquisition_day = _feature_datetime(item).date()
            if acquisition_day in seen_days:
                continue
            seen_days.add(acquisition_day)
            unique_by_day.append(item)
        candidates = unique_by_day
    candidates = candidates[: int(runtime["scene_quality_candidates"])]

    existing_rows = list(
        (
            await db.scalars(
                select(SatelliteObservation)
                .where(SatelliteObservation.plot_id == plot.id)
                .order_by(SatelliteObservation.captured_at.desc())
                .limit(100)
            )
        ).all()
    )
    existing_by_scene = {
        str((row.raw_payload or {}).get("scene_id")): row
        for row in existing_rows
        if (row.raw_payload or {}).get("scene_id") and (
            not getattr(plot, "planting_date", None) or (row.raw_payload or {}).get("processing_geometry") == geometry
        )
    }
    attempts: list[dict[str, Any]] = []
    statistics_by_day: dict[date, dict[str, Any]] = {}

    for rank, feature in enumerate(candidates, start=1):
        captured_at = _feature_datetime(feature)
        scene_key = str(feature.get("id") or "")
        existing = existing_by_scene.get(scene_key)
        stats_result: dict[str, Any] | None = None
        source = "COPERNICUS_STATISTICS"
        try:
            if existing and any(value is not None for value in _observation_indices(existing).values()):
                raw = existing.raw_payload or {}
                cached_statistics = raw.get("statistics") or {}
                cached_quality = _observation_quality(existing)
                if (cached_statistics and cached_quality.get("valid_pixels")
                        and all(name in cached_statistics for name in INDEX_NAMES)):
                    stats_result = {
                        **_observation_indices(existing),
                        "indices": _observation_indices(existing),
                        "statistics": cached_statistics,
                        "quality": cached_quality,
                        "statistics_grid": raw.get("statistics_grid") or {},
                        "statistics_time_range": raw.get("statistics_time_range") or {},
                    }
                    source = "DATABASE_CACHE"
            acquisition_day = captured_at.date()
            if stats_result is None and acquisition_day in statistics_by_day:
                stats_result = statistics_by_day[acquisition_day]
                source = "SAME_DAY_CACHE"
            if stats_result is None:
                stats_result = await _index_statistics(
                    geometry,
                    captured_at,
                    runtime["provider_tile_cloud_ceiling_percentage"],
                    integration,
                    resolution_m=float(runtime["statistics_resolution_m"]),
                )
                statistics_by_day[acquisition_day] = stats_result
            quality = dict(stats_result.get("quality") or {})
            quality["quality_band"] = _quality_band(quality, runtime)
            stats_result["quality"] = quality
            accepted, reason = _quality_acceptance(quality, runtime)
            attempts.append({
                "scene_id": scene_key,
                "captured_at": captured_at.isoformat(),
                "cloud_percentage": _feature_cloud(feature),
                "rank": rank,
                "accepted": accepted,
                "reason": reason,
                "source": source,
                **quality,
            })
            if accepted or explicit_scene_id:
                return feature, stats_result, attempts
        except Exception as exc:
            attempts.append({
                "scene_id": scene_key,
                "captured_at": captured_at.isoformat(),
                "cloud_percentage": _feature_cloud(feature),
                "rank": rank,
                "accepted": False,
                "reason": f"No fue posible evaluar la escena: {str(exc)[:500]}",
                "source": source,
                "status": "FAILED",
            })
            logger.warning(
                "satellite_scene_quality_failed",
                scene_id=scene_key,
                captured_at=captured_at.isoformat(),
                error=str(exc)[:1000],
            )
    return None, None, attempts

def build_satellite_analysis(
    observation: SatelliteObservation,
    plot: Plot,
    *,
    previous: SatelliteObservation | None = None,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = dict(config or {})
    now = now or datetime.now(UTC)
    max_age_days = max(1.0, _number(config.get("analysis_max_age_days"), 20.0))
    reliable_cloud = min(100.0, max(0.0, _number(config.get("analysis_max_reliable_cloud_percentage"), 30.0)))
    ndvi_low = _number(config.get("ndvi_low_threshold"), 0.25)
    ndvi_active = _number(config.get("ndvi_active_threshold"), 0.55)
    ndmi_dry = _number(config.get("ndmi_dry_threshold"), 0.0)
    ndmi_moist = _number(config.get("ndmi_moist_threshold"), 0.2)
    ndwi_water = _number(config.get("ndwi_water_threshold"), 0.1)
    ndvi_decline = _number(config.get("ndvi_decline_delta"), -0.08)
    ndmi_decline = _number(config.get("ndmi_decline_delta"), -0.05)
    minimum_valid_pixels = max(1, int(_number(config.get("minimum_valid_pixels"), 25)))

    age_days = max(0.0, (now - observation.captured_at).total_seconds() / 86400)
    cloud = observation.cloud_percentage
    raw = observation.raw_payload or {}
    statistics = raw.get("statistics") or {}
    comparison = dict(observation.previous_comparison or {})
    if previous:
        if observation.ndvi is not None and previous.ndvi is not None:
            comparison.setdefault("ndvi_delta", round(observation.ndvi - previous.ndvi, 4))
        if observation.ndmi is not None and previous.ndmi is not None:
            comparison.setdefault("ndmi_delta", round(observation.ndmi - previous.ndmi, 4))
        if observation.ndwi is not None and previous.ndwi is not None:
            comparison.setdefault("ndwi_delta", round(observation.ndwi - previous.ndwi, 4))

    indices = dict(raw.get("indices") or {})
    indices.update({"ndvi": observation.ndvi, "ndmi": observation.ndmi, "ndwi": observation.ndwi})
    index_count = sum(value is not None for value in indices.values())
    freshness_score = max(0.0, 100.0 - (age_days / max_age_days) * 55.0)
    cloud_score = 70.0 if cloud is None else max(0.0, 100.0 - float(cloud) * 2.0)
    geometry_mode = raw.get("geometry_mode") or ("polygon" if plot.geojson else "point_buffer_approximation")
    geometry_score = 100.0 if geometry_mode == "polygon" else 65.0
    index_score = min(100.0, index_count / max(3, len(INDEX_NAMES)) * 100.0)
    valid_pixel_counts = [
        int(value.get("sample_count") or 0)
        for value in statistics.values()
        if isinstance(value, dict) and value.get("sample_count") is not None
    ]
    valid_pixels = max(valid_pixel_counts) if valid_pixel_counts else 0
    pixel_score = 100.0 if valid_pixels >= minimum_valid_pixels else (valid_pixels / minimum_valid_pixels * 100.0 if valid_pixels else 35.0)
    quality_score = round(freshness_score * 0.30 + cloud_score * 0.22 + geometry_score * 0.18 + index_score * 0.18 + pixel_score * 0.12)
    reliability = "alta" if quality_score >= 80 else "media" if quality_score >= 55 else "limitada"

    signals: list[dict[str, Any]] = []
    evidence: list[str] = []
    limitations: list[str] = [
        "Los índices espectrales son evidencia complementaria y no constituyen diagnóstico de enfermedad.",
        "La interpretación debe contrastarse con etapa fenológica, clima y observación de campo.",
    ]

    if observation.ndvi is not None:
        if observation.ndvi < ndvi_low:
            label, level = "Señal vegetal baja", "attention"
            message = "El NDVI medio es bajo para una superficie con vegetación activa; puede reflejar cobertura escasa, suelo expuesto, senescencia o mezcla de coberturas."
        elif observation.ndvi < ndvi_active:
            label, level = "Señal vegetal intermedia", "info"
            message = "El NDVI medio muestra actividad vegetal intermedia. Debe interpretarse según la edad y etapa del cultivo."
        else:
            label, level = "Señal vegetal activa", "positive"
            message = "El NDVI medio muestra respuesta vegetal activa en la geozona analizada."
        signals.append({"code": "ndvi_state", "label": label, "level": level, "value": observation.ndvi, "message": message})
        evidence.append(f"NDVI medio {observation.ndvi:.3f} ({label.lower()}).")

    if observation.ndmi is not None:
        if observation.ndmi < ndmi_dry:
            label, level = "Menor señal de humedad", "attention"
            message = "El NDMI presenta una señal espectral baja de humedad del dosel o de la superficie. Conviene contrastar con humedad de suelo y lluvia."
        elif observation.ndmi < ndmi_moist:
            label, level = "Humedad espectral intermedia", "info"
            message = "El NDMI muestra una señal intermedia de humedad en la cobertura."
        else:
            label, level = "Humedad espectral alta", "positive"
            message = "El NDMI muestra una señal alta de humedad en la cobertura o superficie observada."
        signals.append({"code": "ndmi_state", "label": label, "level": level, "value": observation.ndmi, "message": message})
        evidence.append(f"NDMI medio {observation.ndmi:.3f} ({label.lower()}).")

    if observation.ndwi is not None:
        level = "attention" if observation.ndwi >= ndwi_water else "info"
        label = "Señal superficial húmeda/agua" if observation.ndwi >= ndwi_water else "Sin señal superficial alta de agua"
        message = (
            "El NDWI sugiere presencia relativa de agua, suelo muy húmedo o píxeles mixtos; no implica por sí solo anegamiento."
            if observation.ndwi >= ndwi_water
            else "El NDWI no muestra una señal superficial alta de agua en el promedio de la geozona."
        )
        signals.append({"code": "ndwi_state", "label": label, "level": level, "value": observation.ndwi, "message": message})
        evidence.append(f"NDWI medio {observation.ndwi:.3f}.")

    ndvi_delta = comparison.get("ndvi_delta")
    if ndvi_delta is not None:
        level = "attention" if float(ndvi_delta) <= ndvi_decline else "positive" if float(ndvi_delta) >= abs(ndvi_decline) else "info"
        direction = "descenso" if float(ndvi_delta) < 0 else "aumento" if float(ndvi_delta) > 0 else "estabilidad"
        signals.append({
            "code": "ndvi_trend",
            "label": f"Tendencia NDVI: {direction}",
            "level": level,
            "value": round(float(ndvi_delta), 4),
            "message": "Cambio frente a la observación satelital anterior de la misma geozona.",
        })
        evidence.append(f"Cambio NDVI frente a la escena anterior: {float(ndvi_delta):+.3f}.")

    ndmi_delta = comparison.get("ndmi_delta")
    if ndmi_delta is not None:
        level = "attention" if float(ndmi_delta) <= ndmi_decline else "positive" if float(ndmi_delta) >= abs(ndmi_decline) else "info"
        direction = "descenso" if float(ndmi_delta) < 0 else "aumento" if float(ndmi_delta) > 0 else "estabilidad"
        signals.append({
            "code": "ndmi_trend",
            "label": f"Tendencia NDMI: {direction}",
            "level": level,
            "value": round(float(ndmi_delta), 4),
            "message": "Cambio relativo de la señal de humedad frente a la escena anterior.",
        })
        evidence.append(f"Cambio NDMI frente a la escena anterior: {float(ndmi_delta):+.3f}.")

    for code, label in [
        ("evi", "EVI"), ("savi", "SAVI"), ("ndre", "NDRE"),
        ("gndvi", "GNDVI"), ("nbr", "NBR"), ("msi", "MSI"), ("recl", "ReCl"),
    ]:
        value = indices.get(code)
        if value is not None:
            evidence.append(f"{label} medio {float(value):.3f}.")

    if cloud is not None:
        evidence.append(f"Nubosidad de la escena: {float(cloud):.1f}%.")
        if float(cloud) > reliable_cloud:
            limitations.append("La nubosidad supera el umbral recomendado para una lectura espectral robusta.")
    if age_days > max_age_days:
        limitations.append("La escena es antigua para describir el estado actual del cultivo.")
    if geometry_mode != "polygon":
        limitations.append("La geozona es una aproximación circular; puede mezclar caminos, bosque, viviendas u otros cultivos.")
    if valid_pixels and valid_pixels < minimum_valid_pixels:
        limitations.append(
            f"Solo se obtuvieron {valid_pixels} píxeles válidos; la configuración recomienda al menos {minimum_valid_pixels}."
        )
    if index_count < 2:
        limitations.append("Faltan índices procesados; el agente debe usar Copernicus solo como referencia visual o de catálogo.")

    summary_parts = [f"Escena de hace {age_days:.0f} días", f"confiabilidad {reliability}"]


    return {
        "summary": " · ".join(summary_parts),
        "quality_score": quality_score,
        "reliability": reliability,
        "age_days": round(age_days, 1),
        "geometry_mode": geometry_mode,
        "geometry_label": "Polígono de parcela" if geometry_mode == "polygon" else "Área aproximada por punto y radio",
        "indices_available": index_count,
        "signals": signals,
        "evidence_for_agent": evidence,
        "limitations": limitations,
        "statistics": statistics,
        "thresholds": {
            "ndvi_low": ndvi_low,
            "ndvi_active": ndvi_active,
            "ndmi_dry": ndmi_dry,
            "ndmi_moist": ndmi_moist,
            "ndwi_water": ndwi_water,
            "ndvi_decline_delta": ndvi_decline,
            "ndmi_decline_delta": ndmi_decline,
            "max_age_days": max_age_days,
            "max_reliable_cloud_percentage": reliable_cloud,
            "minimum_valid_pixels": minimum_valid_pixels,
            "valid_pixels": valid_pixels,
        },
        "agent_policy": {
            "use_as": "complementary_evidence",
            "must_combine_with": ["fenología", "datos meteorológicos", "reglas publicadas", "observación de campo"],
            "must_not_claim": ["diagnóstico fitosanitario", "causalidad por un índice aislado", "estado uniforme de toda la parcela"],
        },
    }


async def latest_satellite_observation(
    db: AsyncSession,
    station_id: UUID,
    crop_id: UUID | None = None,
) -> tuple[Plot | None, SatelliteObservation | None]:
    plot = await select_station_plot(db, station_id, crop_id)
    if not plot:
        return None, None
    valid = await _latest_complete_observation(db, plot.id)
    if valid:
        return plot, valid
    observation = await db.scalar(
        select(SatelliteObservation)
        .where(SatelliteObservation.plot_id == plot.id)
        .order_by(SatelliteObservation.captured_at.desc())
        .limit(1)
    )
    return plot, observation


async def satellite_observation_payload(
    db: AsyncSession,
    observation: SatelliteObservation,
    plot: Plot,
) -> dict[str, Any]:
    previous = await db.scalar(
        select(SatelliteObservation)
        .where(
            SatelliteObservation.plot_id == plot.id,
            SatelliteObservation.captured_at < observation.captured_at,
        )
        .order_by(SatelliteObservation.captured_at.desc())
        .limit(1)
    )
    integration = await _integration(db)
    runtime = satellite_runtime_config(integration)
    raw = observation.raw_payload or {}
    quality = raw.get("quality") or _observation_quality(observation)
    quality = dict(quality)
    quality.setdefault("quality_band", _quality_band(quality, runtime))
    freshness = _observation_freshness(observation, runtime)
    data_warning = _warning_with_staleness(raw.get("data_warning"), freshness)
    analysis = build_satellite_analysis(
        observation,
        plot,
        previous=previous,
        config={**_integration_config(integration), **runtime},
    )
    return {
        **model_dict(observation, exclude={"raw_payload"}),
        "plot_name": plot.name,
        "plot_id": str(plot.id),
        "plot_crop_id": str(plot.crop_id) if plot.crop_id else None,
        "plot_area_ha": float(plot.area_ha) if plot.area_ha is not None else None,
        "processing_status": raw.get("processing_status"),
        "scene_id": raw.get("scene_id"),
        "geometry_mode": raw.get("geometry_mode") or analysis["geometry_mode"],
        "processing_error": raw.get("processing_error"),
        "image_available": bool(local_satellite_image_path(observation)),
        # Always expose the clipped Process API image when it exists. STAC
        # thumbnails are useful as fallback, but they are not guaranteed to use
        # the exact polygon or rendering window of the calculated indices.
        "source_url": (
            f"/api/v1/public/satellite/{observation.id}/image.jpg"
            if local_satellite_image_path(observation)
            else observation.source_url
        ),
        "index_images": {
            name: (versioned_index_url(f"/api/v1/public/satellite/{observation.id}/indices", name, raw.get("index_visual_versions") or {}) if local_satellite_index_image_path(observation, name) else None)
            for name in INDEX_NAMES
        },
        "visual_layers_available": sum(
            1 for name in INDEX_NAMES if local_satellite_index_image_path(observation, name)
        ),
        **_visual_metadata(observation),
        "indices": raw.get("indices") or {
            "ndvi": observation.ndvi,
            "ndmi": observation.ndmi,
            "ndwi": observation.ndwi,
        },
        "statistics": raw.get("statistics") or {},
        "quality": quality,
        "statistics_time_range": raw.get("statistics_time_range") or {},
        "data_source_mode": raw.get("data_source_mode") or "LATEST_VALID_SCENE",
        "fallback_used": bool(raw.get("fallback_used")),
        "data_warning": data_warning,
        **freshness,
        "latest_catalog_scene_at": raw.get("latest_catalog_scene_at"),
        "catalog_checked_at": raw.get("catalog_checked_at"),
        "selected_scene_rank": raw.get("selected_scene_rank"),
        "candidate_checks": raw.get("candidate_checks") or [],
        "scene_metadata": raw.get("scene_metadata") or {},
        "analysis": analysis,
    }


def local_satellite_image_path(observation: SatelliteObservation) -> Path | None:
    """Return a validated local satellite image path for an observation.

    The path is accepted only when it exists inside the configured satellite
    storage directory. This prevents exposing arbitrary filesystem paths.
    """
    raw = observation.raw_payload or {}
    value = raw.get("local_image_path")
    if not value:
        return None

    path = Path(str(value)).resolve()
    root = (settings.storage_path / "satellite").resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def local_satellite_index_image_path(observation: SatelliteObservation, index_name: str) -> Path | None:
    """Return a validated local PNG path for one supported spectral index."""
    if index_name not in INDEX_NAMES:
        return None
    raw = observation.raw_payload or {}
    value = (raw.get("local_index_image_paths") or {}).get(index_name)
    if not value:
        return None
    path = Path(str(value)).resolve()
    root = (settings.storage_path / "satellite").resolve()
    if root not in path.parents or path.suffix.lower() != ".png" or not path.is_file():
        return None
    return path


def _local_index_paths(observation: SatelliteObservation | None) -> dict[str, str]:
    if not observation:
        return {}
    return {
        name: str(path)
        for name in INDEX_NAMES
        if (path := local_satellite_index_image_path(observation, name)) is not None
    }


async def sync_satellite_for_station(
    db: AsyncSession,
    station_id: UUID | str | None,
    *,
    crop_id: UUID | None = None,
    force: bool = False,
    target_date: date | None = None,
    scene_id: str | None = None,
    plot_id: UUID | None = None,
) -> dict[str, Any]:
    station = await db.get(Station, station_id) if station_id else None
    if station_id and (not station or station.deleted_at):
        raise ValueError("Estación no encontrada")
    if not plot_id and not station:
        raise ValueError("Indique una parcela o estación")
    plot = await db.get(Plot, plot_id) if plot_id else await ensure_station_plot(db, station, crop_id)
    if not plot or not plot.active or plot.deleted_at:
        raise ValueError("Parcela no disponible")
    integration = await _integration(db)
    runtime = satellite_runtime_config(integration)
    client_id, client_secret = _oauth_credentials(integration)
    credentials_ready = bool(client_id and client_secret)

    # Public users can trigger this endpoint at the same time. Serialize the
    # expensive provider work and re-check the cache after acquiring the lock.
    await _acquire_station_refresh_lock(db, plot.id if plot_id else station.id, crop_id)
    latest_valid = await _latest_complete_observation(db, plot.id)
    if plot_id and latest_valid and (latest_valid.raw_payload or {}).get("processing_geometry") != plot.geojson:
        latest_valid = None
    explicit_request = target_date is not None or scene_id is not None
    if (
        latest_valid
        and not force
        and not explicit_request
        and len(_current_visual_paths(latest_valid)) == len(INDEX_NAMES)
        and "recl" in ((latest_valid.raw_payload or {}).get("indices") or {})
        and not satellite_catalog_cache_is_stale(latest_valid, runtime)
    ):
        return _cached_observation_result(
            latest_valid,
            plot,
            credentials_ready=credentials_ready,
            runtime=runtime,
        )

    date_to = target_date or datetime.now(UTC).date()
    date_from = target_date or (date_to - timedelta(days=runtime["lookback_days"]))
    geometry = plot_geometry(plot)
    catalog_result = await _search_copernicus_catalog_for_geometry(
        integration,
        geometry,
        date_from,
        date_to,
        runtime["provider_tile_cloud_ceiling_percentage"],
        runtime["catalog_limit"],
    )
    features = catalog_result["features"]
    now = datetime.now(UTC)
    if not features:
        warning = "Copernicus no encontró escenas en el período configurado."
        if latest_valid and not explicit_request:
            raw = dict(latest_valid.raw_payload or {})
            raw.update({
                "catalog_checked_at": now.isoformat(),
                "data_source_mode": "LAST_VALID_CACHE",
                "fallback_used": True,
                "data_warning": warning + " Se conserva el último NDVI válido almacenado.",
                "candidate_checks": [],
            })
            latest_valid.raw_payload = raw
            await db.commit()
            await db.refresh(latest_valid)
            return _cached_observation_result(
                latest_valid,
                plot,
                credentials_ready=credentials_ready,
                runtime=runtime,
                warning=raw["data_warning"],
            )
        return {
            "status": "NO_SCENE",
            "plot_id": str(plot.id),
            "count": 0,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "processing_credentials_configured": credentials_ready,
            "data_warning": warning,
            "runtime_config": runtime,
        }

    if scene_id and not any(str(item.get("id")) == str(scene_id) for item in features):
        raise ValueError("La escena seleccionada ya no está disponible en el catálogo para la geozona y fecha indicadas")

    selected_feature: dict[str, Any] | None
    stats_result: dict[str, Any] | None
    attempts: list[dict[str, Any]]
    if credentials_ready:
        selected_feature, stats_result, attempts = await _evaluate_catalog_candidates(
            db,
            plot,
            features,
            geometry,
            integration,
            runtime,
            explicit_scene_id=scene_id,
        )
    else:
        selected_feature = next((item for item in features if str(item.get("id")) == str(scene_id)), None) if scene_id else features[0]
        stats_result = None
        attempts = []

    latest_catalog_feature = features[0]
    latest_catalog_at = _feature_datetime(latest_catalog_feature)

    if selected_feature is None:
        warning = (
            "Las escenas recientes no alcanzaron el mínimo de píxeles válidos dentro del polígono. "
            "Se conserva el último NDVI válido almacenado."
        )
        if latest_valid and not explicit_request:
            raw = dict(latest_valid.raw_payload or {})
            raw.update({
                "catalog_checked_at": now.isoformat(),
                "latest_catalog_scene_at": latest_catalog_at.isoformat(),
                "latest_catalog_scene_id": latest_catalog_feature.get("id"),
                "data_source_mode": "LAST_VALID_CACHE",
                "fallback_used": True,
                "data_warning": warning,
                "candidate_checks": attempts,
            })
            latest_valid.raw_payload = raw
            await db.commit()
            await db.refresh(latest_valid)
            return _cached_observation_result(
                latest_valid,
                plot,
                credentials_ready=credentials_ready,
                runtime=runtime,
                warning=warning,
            )
        return {
            "status": "NO_VALID_SCENE",
            "plot_id": str(plot.id),
            "processing_credentials_configured": credentials_ready,
            "data_warning": warning,
            "latest_catalog_scene_at": latest_catalog_at.isoformat(),
            "candidate_checks": attempts,
            "runtime_config": runtime,
        }

    captured_at = _feature_datetime(selected_feature)
    selected_scene_id = str(selected_feature.get("id") or "")
    selected_rank = next(
        (index for index, feature in enumerate(features, start=1) if str(feature.get("id")) == selected_scene_id),
        1,
    )
    quality = dict((stats_result or {}).get("quality") or {})
    accepted, acceptance_reason = _quality_acceptance(quality, runtime) if stats_result else (False, "OAuth no configurado")
    fallback_used = selected_rank > 1
    data_source_mode = "RECENT_VALID_FALLBACK" if fallback_used else "LATEST_VALID_SCENE"
    data_warning = None
    if fallback_used:
        data_warning = (
            f"La adquisición más reciente ({latest_catalog_at.date().isoformat()}) no tuvo suficiente superficie válida. "
            f"Se muestra la escena válida más reciente del {captured_at.date().isoformat()}."
        )
    elif explicit_request and not accepted and credentials_ready:
        data_warning = f"La escena seleccionada no cumple el umbral de calidad: {acceptance_reason}."

    existing = await db.scalar(
        select(SatelliteObservation).where(
            SatelliteObservation.plot_id == plot.id,
            SatelliteObservation.captured_at == captured_at,
            SatelliteObservation.provider == "COPERNICUS_DATA_SPACE_SENTINEL_2_L2A",
        )
    )
    existing_raw = dict(existing.raw_payload or {}) if existing else {}
    if plot_id and existing and existing_raw.get("processing_geometry") != plot.geojson:
        existing = None
        existing_raw = {}
    if (existing and satellite_observation_is_complete(existing) and accepted
            and len(_current_visual_paths(existing)) == len(INDEX_NAMES)
            and "recl" in (existing_raw.get("indices") or {})):
        existing_raw.update({
            "catalog_checked_at": now.isoformat(),
            "latest_catalog_scene_at": latest_catalog_at.isoformat(),
            "latest_catalog_scene_id": latest_catalog_feature.get("id"),
            "selected_scene_rank": selected_rank,
            "data_source_mode": data_source_mode,
            "fallback_used": fallback_used,
            "data_warning": data_warning,
            "candidate_checks": attempts,
            "quality": quality or _observation_quality(existing),
        })
        existing.raw_payload = existing_raw
        await db.commit()
        await db.refresh(existing)
        return _cached_observation_result(
            existing,
            plot,
            credentials_ready=credentials_ready,
            runtime=runtime,
        )

    previous = await _latest_complete_observation(db, plot.id, before=captured_at)
    preview_url = _feature_preview(selected_feature)
    ndvi = existing.ndvi if existing else None
    ndmi = existing.ndmi if existing else None
    ndwi = existing.ndwi if existing else None
    statistics: dict[str, Any] = dict(existing_raw.get("statistics") or {})
    indices: dict[str, Any] = dict(existing_raw.get("indices") or {})
    processing_status = "CATALOG_ONLY"
    existing_local_image = local_satellite_image_path(existing) if existing else None
    local_image_path: str | None = str(existing_local_image) if existing_local_image else None
    local_index_image_paths: dict[str, str] = _local_index_paths(existing)
    current_visual_paths = _current_visual_paths(existing)
    visual_versions = dict(existing_raw.get("index_visual_versions") or {})
    processing_error: str | None = None
    observation_id = existing.id if existing else uuid4()
    source_url = existing.source_url if existing and existing.source_url else (preview_url[:1000] if preview_url else None)

    if credentials_ready:
        start, end = _scene_time_range(captured_at)
        phase_errors: list[str] = []
        image_ready = bool(local_image_path and Path(str(local_image_path)).is_file())
        visual_layers_ready = len(current_visual_paths) == len(INDEX_NAMES)
        statistics_ready = False

        if stats_result:
            indices = dict(stats_result.get("indices") or {})
            statistics = dict(stats_result.get("statistics") or {})
            quality = dict(stats_result.get("quality") or {})
            statistics_ready = accepted
            if any(value is not None for value in indices.values()):
                ndvi, ndmi, ndwi = indices.get("ndvi"), indices.get("ndmi"), indices.get("ndwi")
            if not accepted:
                phase_errors.append(f"Estadísticas: {acceptance_reason}")
        else:
            phase_errors.append("Estadísticas: no fue posible evaluar la escena")

        if not image_ready:
            try:
                image = await _process_image(
                    geometry,
                    start,
                    end,
                    runtime["provider_tile_cloud_ceiling_percentage"],
                    integration,
                )
                local = settings.storage_path / "satellite" / f"{observation_id}.jpg"
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(image)
                local_image_path = str(local)
                source_url = f"/api/v1/public/satellite/{observation_id}/image.jpg"
                image_ready = True
            except Exception as exc:
                message = str(exc)[:1000]
                logger.warning("satellite_process_image_failed", error=message)
                phase_errors.append(f"Imagen RGB: {message}")

        if not visual_layers_ready:
            missing = set(INDEX_NAMES) - set(current_visual_paths)
            images, visual_errors = await _process_index_images(
                geometry, start, end, runtime["provider_tile_cloud_ceiling_percentage"], integration, only=missing
            )
            layer_dir = settings.storage_path / "satellite" / str(observation_id)
            layer_dir.mkdir(parents=True, exist_ok=True)
            for name, content in images.items():
                layer_path = layer_dir / f"{name}-{VISUAL_VERSION}.png"
                layer_path.write_bytes(content)
                local_index_image_paths[name] = str(layer_path)
                current_visual_paths[name] = str(layer_path)
                visual_versions[name] = VISUAL_VERSION
            for name, message in visual_errors.items():
                logger.warning("satellite_index_image_failed", index=name, error=message)
            if visual_errors:
                phase_errors.append(
                    "Capas visuales: " + ", ".join(f"{name} ({message})" for name, message in visual_errors.items())
                )
            visual_layers_ready = len(current_visual_paths) == len(INDEX_NAMES)

        processing_error = " | ".join(phase_errors) or None
        if image_ready and statistics_ready and visual_layers_ready:
            processing_status = "PROCESSED"
        elif image_ready or any(value is not None for value in indices.values()) or local_index_image_paths:
            processing_status = "PARTIAL"
        else:
            processing_status = "FAILED"

    comparison: dict[str, Any] = {}
    if previous:
        if ndvi is not None and previous.ndvi is not None:
            comparison["ndvi_delta"] = round(ndvi - previous.ndvi, 4)
        if ndmi is not None and previous.ndmi is not None:
            comparison["ndmi_delta"] = round(ndmi - previous.ndmi, 4)
        if ndwi is not None and previous.ndwi is not None:
            comparison["ndwi_delta"] = round(ndwi - previous.ndwi, 4)

    row = existing or SatelliteObservation(id=observation_id, plot_id=plot.id, captured_at=captured_at)
    row.provider = "COPERNICUS_DATA_SPACE_SENTINEL_2_L2A"
    row.cloud_percentage = _feature_cloud(selected_feature)
    row.ndvi = ndvi
    row.ndmi = ndmi
    row.ndwi = ndwi
    row.previous_comparison = comparison
    row.anomalies = []
    row.source_url = source_url
    row.resolution_m = 10
    row.raw_payload = {
        **existing_raw,
        "scene_id": selected_feature.get("id"),
        "processing_geometry": geometry,
        "feature": selected_feature,
        "geometry_mode": "polygon" if _polygon_geometry(plot.geojson) else "point_buffer_approximation",
        "processing_status": processing_status,
        "processing_error": processing_error,
        "local_image_path": local_image_path,
        "local_index_image_paths": local_index_image_paths,
        "index_visual_versions": visual_versions,
        "visual_grid": _visual_grid(geometry) if current_visual_paths else existing_raw.get("visual_grid"),
        "preview_url": preview_url,
        "statistics": statistics,
        "indices": indices,
        "quality": quality,
        "statistics_grid": (stats_result or {}).get("statistics_grid") or {},
        "statistics_time_range": (stats_result or {}).get("statistics_time_range") or {},
        "catalog_checked_at": now.isoformat(),
        "latest_catalog_scene_at": latest_catalog_at.isoformat(),
        "latest_catalog_scene_id": latest_catalog_feature.get("id"),
        "selected_scene_rank": selected_rank,
        "data_source_mode": data_source_mode,
        "fallback_used": fallback_used,
        "data_warning": data_warning,
        "candidate_checks": attempts,
        "scene_metadata": {
            "id": selected_feature.get("id"),
            "collection": selected_feature.get("collection"),
            "datetime": (selected_feature.get("properties") or {}).get("datetime"),
            "platform": (selected_feature.get("properties") or {}).get("platform"),
            "constellation": (selected_feature.get("properties") or {}).get("constellation"),
            "instrument": (selected_feature.get("properties") or {}).get("instruments"),
            "cloud_cover": (selected_feature.get("properties") or {}).get("eo:cloud_cover"),
            "processing_level": (selected_feature.get("properties") or {}).get("processing:level"),
        },
    }
    if not existing:
        db.add(row)
    await db.commit()
    await db.refresh(row)

    analysis_integration = await _integration(db)
    analysis = build_satellite_analysis(
        row,
        plot,
        previous=previous,
        config={**_integration_config(analysis_integration), **satellite_runtime_config(analysis_integration)},
    )
    result = _cached_observation_result(
        row,
        plot,
        credentials_ready=credentials_ready,
        runtime=runtime,
    )
    result["analysis"] = analysis
    return result


async def catalog_scenes_for_station(
    db: AsyncSession,
    station: Station,
    *,
    days: int | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """List recent Sentinel-2 scenes for the station polygon without processing them."""
    integration = await _integration(db)
    runtime = satellite_runtime_config(integration)
    period_days = max(1, min(int(days or runtime["history_days"]), 366))
    item_limit = max(1, min(int(max_items or runtime["history_limit"]), 60))
    date_to = datetime.now(UTC).date()
    geometry = station_geometry(station)
    date_from = date_to - timedelta(days=period_days)
    cache_key = json.dumps(
        {
            "station_id": str(station.id),
            "geometry": geometry,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "max_cloud": runtime["provider_tile_cloud_ceiling_percentage"],
            "limit": max(item_limit, runtime["catalog_limit"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    cached = _catalog_scene_cache.get(cache_key) or {}
    catalog_warning: str | None = None
    stale_cache = False
    if cached.get("expires_at") and cached["expires_at"] > datetime.now(UTC):
        result = cached["result"]
    else:
        try:
            result = await _search_copernicus_catalog_for_geometry(
                integration,
                geometry,
                date_from,
                date_to,
                runtime["provider_tile_cloud_ceiling_percentage"],
                max(item_limit, runtime["catalog_limit"]),
            )
            _catalog_scene_cache[cache_key] = {
                "expires_at": datetime.now(UTC) + timedelta(minutes=30),
                "result": result,
            }
        except CatalogTemporaryError as exc:
            # A temporary CDSE outage must not empty the map when a previous
            # successful catalogue response is still available in memory.
            if not cached.get("result"):
                raise
            result = cached["result"]
            catalog_warning = str(exc)[:1000]
            stale_cache = True
            logger.warning(
                "copernicus_catalog_stale_cache_used",
                station_id=str(station.id),
                warning=catalog_warning,
            )
    features = result["features"][:item_limit]
    return {
        "features": features,
        "count": len(features),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "runtime_config": runtime,
        "catalog_status": "STALE_CACHE" if stale_cache else "CURRENT",
        "catalog_warning": catalog_warning,
        "catalog_url": result.get("catalog_url"),
        "catalog_mode": result.get("catalog_mode"),
        "fallback_used": bool(result.get("fallback_used")),
    }


async def test_copernicus_integration(
    db: AsyncSession,
    station_id: UUID | None = None,
) -> dict[str, Any]:
    """Execute a real, phase-by-phase Copernicus test with valid-scene fallback."""
    station_query = select(Station).where(
        Station.active.is_(True),
        Station.deleted_at.is_(None),
    )
    if station_id:
        station_query = station_query.where(Station.id == station_id)
    else:
        station_query = station_query.where(
            or_(
                Station.influence_geojson.is_not(None),
                (Station.latitude.is_not(None) & Station.longitude.is_not(None)),
            )
        ).order_by(Station.name, Station.id).limit(1)
    station = await db.scalar(station_query)
    if not station:
        return {
            "station_id": str(station_id) if station_id else None,
            "status": "FAILED",
            "processing_error": "No existe la estación seleccionada o no está activa",
            "diagnostics": {"geometry": "NOT_RUN", "catalog": "NOT_RUN", "oauth": "NOT_RUN", "process": "NOT_RUN", "statistics": "NOT_RUN"},
        }

    integration = await _integration(db)
    runtime = satellite_runtime_config(integration)
    client_id, client_secret = _oauth_credentials(integration)
    diagnostics: dict[str, Any] = {
        "geometry": "PENDING",
        "catalog": "PENDING",
        "oauth": "NOT_CONFIGURED" if not (client_id and client_secret) else "PENDING",
        "process": "NOT_RUN",
        "statistics": "NOT_RUN",
        "endpoints": {
            "stac": _catalog_url(integration),
            "token": _endpoint(integration, "token_url", settings.copernicus_token_url),
            "process": _endpoint(integration, "process_url", settings.copernicus_process_url),
            "statistics": _endpoint(integration, "statistics_url", settings.copernicus_statistics_url),
        },
    }

    try:
        geometry = station_geometry(station)
        geometry_source = "station_polygon" if _polygon_geometry(station.influence_geojson) else "station_point_radius"
        diagnostics["geometry"] = "COMPLETED"
        diagnostics["geometry_source"] = geometry_source
        diagnostics["geometry_type"] = geometry.get("type")
        diagnostics["station_coordinates"] = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "radius_m": station.influence_radius_m or settings.satellite_auto_radius_m,
        }
    except Exception as exc:
        message = str(exc)[:1000]
        diagnostics["geometry"] = "FAILED"
        diagnostics["geometry_error"] = message
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "FAILED",
            "processing_error": f"Falló la validación de la geozona de la estación: {message}",
            "image_available": False,
            "processing_credentials_configured": bool(client_id and client_secret),
            "diagnostics": diagnostics,
        }

    try:
        today = datetime.now(UTC).date()
        catalog = await _search_copernicus_catalog_for_geometry(
            integration,
            geometry,
            today - timedelta(days=runtime["lookback_days"]),
            today,
            runtime["provider_tile_cloud_ceiling_percentage"],
            runtime["catalog_limit"],
        )
        diagnostics["catalog"] = "COMPLETED"
        diagnostics["catalog_scenes"] = catalog["count"]
    except CatalogTemporaryError as exc:
        message = str(exc)[:1000]
        diagnostics["catalog"] = "DEGRADED"
        diagnostics["catalog_error"] = message
        diagnostics["catalog_retryable"] = True
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "PARTIAL",
            "processing_error": (
                "El catálogo de Copernicus está temporalmente inestable. "
                "La geometría de la estación es válida y la prueba puede repetirse sin cambiar el polígono. "
                f"Detalle técnico: {message}"
            ),
            "image_available": False,
            "processing_credentials_configured": bool(client_id and client_secret),
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        message = str(exc)[:1000]
        diagnostics["catalog"] = "FAILED"
        diagnostics["catalog_error"] = message
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "FAILED",
            "processing_error": f"Falló la fase STAC/catalog: {message}",
            "image_available": False,
            "processing_credentials_configured": bool(client_id and client_secret),
            "diagnostics": diagnostics,
        }

    if not catalog["features"]:
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "PARTIAL",
            "processing_error": "El catálogo respondió, pero no encontró escenas dentro del período y umbral de nubosidad configurados.",
            "image_available": False,
            "processing_credentials_configured": bool(client_id and client_secret),
            "diagnostics": {**diagnostics, "catalog": "COMPLETED_NO_SCENE"},
        }

    latest_feature = catalog["features"][0]
    if not (client_id and client_secret):
        captured_at = _feature_datetime(latest_feature)
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "CATALOG_ONLY",
            "processing_error": None,
            "image_available": False,
            "processing_credentials_configured": False,
            "scene_id": latest_feature.get("id"),
            "captured_at": captured_at.isoformat(),
            "diagnostics": diagnostics,
        }

    try:
        await _access_token(integration)
        diagnostics["oauth"] = "COMPLETED"
    except Exception as exc:
        message = str(exc)[:1000]
        diagnostics["oauth"] = "FAILED"
        diagnostics["oauth_error"] = message
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "FAILED",
            "processing_error": message,
            "image_available": False,
            "processing_credentials_configured": True,
            "scene_id": latest_feature.get("id"),
            "captured_at": _feature_datetime(latest_feature).isoformat(),
            "diagnostics": diagnostics,
        }

    plot = await ensure_station_plot(db, station)
    feature, stats_result, attempts = await _evaluate_catalog_candidates(
        db,
        plot,
        catalog["features"],
        geometry,
        integration,
        runtime,
    )
    diagnostics["candidate_checks"] = attempts
    if feature is None or stats_result is None:
        diagnostics["statistics"] = "NO_VALID_SCENE"
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "PARTIAL",
            "processing_error": "Ninguna escena reciente alcanzó el mínimo de píxeles y cobertura válidos dentro del polígono.",
            "image_available": False,
            "visual_layers_available": 0,
            "processing_credentials_configured": True,
            "scene_id": latest_feature.get("id"),
            "captured_at": _feature_datetime(latest_feature).isoformat(),
            "indices": {},
            "diagnostics": diagnostics,
        }

    captured_at = _feature_datetime(feature)
    selected_rank = next(
        (index for index, item in enumerate(catalog["features"], start=1) if str(item.get("id")) == str(feature.get("id"))),
        1,
    )
    diagnostics["statistics"] = str((stats_result.get("quality") or {}).get("status") or "COMPLETED")
    diagnostics["quality"] = stats_result.get("quality") or {}
    diagnostics["selected_scene_rank"] = selected_rank
    diagnostics["latest_catalog_scene_at"] = _feature_datetime(latest_feature).isoformat()
    diagnostics["fallback_used"] = selected_rank > 1

    start, end = _scene_time_range(captured_at)
    image_available = False
    process_error: str | None = None
    try:
        image = await _process_image(geometry, start, end, runtime["provider_tile_cloud_ceiling_percentage"], integration)
        image_available = bool(image)
        diagnostics["process"] = "COMPLETED" if image_available else "NO_IMAGE"
    except Exception as exc:
        process_error = str(exc)[:1000]
        diagnostics["process"] = "FAILED"
        diagnostics["process_error"] = process_error

    visual_layers_available = 0
    visual_error: str | None = None
    try:
        visual_layers, visual_errors = await _process_index_images(
            geometry, start, end, runtime["provider_tile_cloud_ceiling_percentage"], integration
        )
        visual_layers_available = len(visual_layers)
        diagnostics["visual_layers"] = "COMPLETED" if visual_layers_available == len(INDEX_NAMES) else "PARTIAL"
        diagnostics["visual_layers_available"] = visual_layers_available
        diagnostics["visual_layer_errors"] = visual_errors
        if visual_errors:
            visual_error = ", ".join(f"{name}: {message}" for name, message in visual_errors.items())[:1000]
    except Exception as exc:
        visual_error = str(exc)[:1000]
        diagnostics["visual_layers"] = "FAILED"
        diagnostics["visual_layers_error"] = visual_error

    phase_errors = [item for item in (process_error, visual_error) if item]
    completed = image_available and visual_layers_available == len(INDEX_NAMES) and diagnostics["statistics"] == "COMPLETED"
    status = "PROCESSED" if completed else "PARTIAL"
    return {
        "station_id": str(station.id),
        "station": station.name,
        "status": status,
        "processing_error": " | ".join(phase_errors) or None,
        "image_available": image_available,
        "visual_layers_available": visual_layers_available,
        "processing_credentials_configured": True,
        "scene_id": feature.get("id"),
        "captured_at": captured_at.isoformat(),
        "indices": stats_result.get("indices") or {},
        "quality": stats_result.get("quality") or {},
        "fallback_used": selected_rank > 1,
        "diagnostics": diagnostics,
    }
