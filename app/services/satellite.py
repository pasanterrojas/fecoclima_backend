from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import structlog
from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.db.models import IntegrationConfig, Plot, SatelliteObservation, Station
from app.services.serialization import model_dict

logger = structlog.get_logger(__name__)
_token_cache: dict[str, dict[str, Any]] = {}

INDEX_NAMES = ("ndvi", "ndmi", "ndwi", "evi", "savi", "ndre", "gndvi", "nbr", "msi")


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _polygon_geometry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    geometry = value.get("geometry") if value.get("type") == "Feature" else value
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        return None
    if geometry["type"] == "Polygon":
        ring = coordinates[0] if coordinates else None
    else:
        ring = coordinates[0][0] if coordinates and isinstance(coordinates[0], list) and coordinates[0] else None
    if not isinstance(ring, list) or len(ring) < 4:
        return None
    return geometry


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
                "Vuelva a guardar el polígono desde /admin/estaciones."
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
    /admin/estaciones is authoritative. Crop plots are only a legacy fallback
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


def _catalog_url(integration: IntegrationConfig | None) -> str:
    config = _integration_config(integration)
    return str(config.get("stac_search_url") or (integration.base_url if integration else None) or settings.copernicus_stac_search_url)


def _collection(integration: IntegrationConfig | None) -> str:
    config = _integration_config(integration)
    return str(config.get("collection") or "sentinel-2-l2a")


async def _search_copernicus_catalog_for_geometry(
    integration: IntegrationConfig | None,
    geometry: dict[str, Any],
    date_from: date,
    date_to: date,
    max_cloud_percentage: float = 40,
) -> dict[str, Any]:
    payload = {
        "collections": [_collection(integration)],
        "intersects": geometry,
        "datetime": f"{date_from.isoformat()}T00:00:00Z/{date_to.isoformat()}T23:59:59Z",
        "limit": 20,
        "query": {"eo:cloud_cover": {"lte": max_cloud_percentage}},
        "sortby": [{"field": "datetime", "direction": "desc"}],
    }
    async with httpx.AsyncClient(timeout=settings.external_http_timeout_seconds, follow_redirects=True) as client:
        attempts = [payload]
        without_query = dict(payload)
        without_query.pop("query", None)
        attempts.append(without_query)
        basic = dict(without_query)
        basic.pop("sortby", None)
        attempts.append(basic)
        response = None
        for candidate in attempts:
            response = await client.post(_catalog_url(integration), json=candidate)
            if response.status_code not in {400, 422}:
                break
        assert response is not None
        if response.status_code >= 400:
            raise RuntimeError(f"STAC/catalog HTTP {response.status_code}: {response.text[:1000]}")
        result = response.json()
    features = result.get("features", []) if isinstance(result, dict) else []
    features = [item for item in features if _feature_cloud(item) is None or _feature_cloud(item) <= max_cloud_percentage]
    features.sort(key=_feature_datetime, reverse=True)
    return {
        "provider": "COPERNICUS_DATA_SPACE",
        "features": features,
        "count": len(features),
        "catalog_url": _catalog_url(integration),
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

INDEX_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{bands:["B02","B03","B04","B05","B08","B8A","B11","B12","SCL","dataMask"]}],
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
      {id:"dataMask", bands:1}
    ]
  };
}
function ratio(a,b) { return Math.abs(a+b) < 0.000001 ? 0 : (a-b)/(a+b); }
function divide(a,b) { return Math.abs(b) < 0.000001 ? 0 : a/b; }
function evaluatePixel(s) {
  const cloudy = [3,8,9,10,11].includes(s.SCL);
  const valid = s.dataMask === 1 && !cloudy ? 1 : 0;
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
    dataMask:[valid]
  };
}
"""



def _bounds(geometry: dict[str, Any]) -> dict[str, Any]:
    return {"geometry": geometry, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}}


async def _process_image(geometry: dict[str, Any], time_from: datetime, time_to: datetime, max_cloud: float, integration: IntegrationConfig | None) -> bytes:
    token = await _access_token(integration)
    payload = {
        "input": {
            "bounds": _bounds(geometry),
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": time_from.isoformat().replace("+00:00", "Z"), "to": time_to.isoformat().replace("+00:00", "Z")},
                    "mosaickingOrder": "leastCC",
                    "maxCloudCoverage": max_cloud,
                },
            }],
        },
        "output": {"width": 640, "height": 640, "responses": [{"identifier": "default", "format": {"type": "image/jpeg"}}]},
        "evalscript": TRUE_COLOR_EVALSCRIPT,
    }
    async with httpx.AsyncClient(timeout=max(90, settings.external_http_timeout_seconds)) as client:
        response = await client.post(
            _endpoint(integration, "process_url", settings.copernicus_process_url),
            headers={"Authorization": f"Bearer {token}", "Accept": "image/jpeg"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Process API HTTP {response.status_code}: {response.text[:1000]}")
        if not response.content:
            raise RuntimeError("Process API respondió sin contenido de imagen")
        return response.content


def _stat_value(stats: dict[str, Any], key: str) -> float | int | None:
    value = stats.get(key)
    if value is None:
        return None
    try:
        return int(value) if key in {"sampleCount", "noDataCount"} else round(float(value), 4)
    except (TypeError, ValueError):
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
    # A single-day request normally yields one row. Weighted mean keeps the
    # parser safe if the provider returns more than one interval.
    total = sum(int(item.get("sampleCount") or 0) for item in rows)
    weighted_mean = None
    if total:
        weighted_mean = sum(float(item.get("mean") or 0) * int(item.get("sampleCount") or 0) for item in rows) / total
    percentiles = rows[-1].get("percentiles") or {}
    return {
        "min": min(float(item["min"]) for item in rows if item.get("min") is not None) if any(item.get("min") is not None for item in rows) else None,
        "max": max(float(item["max"]) for item in rows if item.get("max") is not None) if any(item.get("max") is not None for item in rows) else None,
        "mean": None if weighted_mean is None else round(weighted_mean, 4),
        "stdev": _stat_value(rows[-1], "stDev"),
        "sample_count": total or _stat_value(rows[-1], "sampleCount"),
        "no_data_count": sum(int(item.get("noDataCount") or 0) for item in rows),
        "percentiles": {str(key): round(float(value), 4) for key, value in percentiles.items()},
    }


async def _index_statistics(
    geometry: dict[str, Any],
    time_from: datetime,
    time_to: datetime,
    max_cloud: float,
    integration: IntegrationConfig | None,
) -> dict[str, Any]:
    token = await _access_token(integration)
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
                    "mosaickingOrder": "leastCC",
                    "maxCloudCoverage": max_cloud,
                },
            }],
        },
        "aggregation": {
            "timeRange": {"from": time_from.isoformat().replace("+00:00", "Z"), "to": time_to.isoformat().replace("+00:00", "Z")},
            "aggregationInterval": {"of": "P1D"},
            "evalscript": INDEX_EVALSCRIPT,
            "resx": 10,
            "resy": 10,
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
    statistics = {name: _parse_output_statistics(result, name) for name in INDEX_NAMES}
    indices = {name: (statistics[name].get("mean") if statistics[name] else None) for name in INDEX_NAMES}
    return {**indices, "indices": indices, "statistics": statistics}


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
    quality_score = round(freshness_score * 0.35 + cloud_score * 0.25 + geometry_score * 0.2 + index_score * 0.2)
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
        ("gndvi", "GNDVI"), ("nbr", "NBR"), ("msi", "MSI"),
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
    if index_count < 2:
        limitations.append("Faltan índices procesados; el agente debe usar Copernicus solo como referencia visual o de catálogo.")

    summary_parts = [f"Escena de hace {age_days:.0f} días", f"confiabilidad {reliability}"]
    if observation.ndvi is not None:
        summary_parts.append(f"NDVI {observation.ndvi:.3f}")
    if observation.ndmi is not None:
        summary_parts.append(f"NDMI {observation.ndmi:.3f}")
    if observation.ndwi is not None:
        summary_parts.append(f"NDWI {observation.ndwi:.3f}")

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
    raw = observation.raw_payload or {}
    analysis = build_satellite_analysis(
        observation,
        plot,
        previous=previous,
        config=_integration_config(integration),
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
        "indices": raw.get("indices") or {
            "ndvi": observation.ndvi,
            "ndmi": observation.ndmi,
            "ndwi": observation.ndwi,
        },
        "statistics": raw.get("statistics") or {},
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


async def sync_satellite_for_station(
    db: AsyncSession,
    station_id: UUID | str,
    *,
    crop_id: UUID | None = None,
    force: bool = False,
) -> dict[str, Any]:
    station = await db.get(Station, station_id)
    if not station or station.deleted_at:
        raise ValueError("Estación no encontrada")
    plot = await ensure_station_plot(db, station, crop_id)
    latest = await db.scalar(
        select(SatelliteObservation)
        .where(SatelliteObservation.plot_id == plot.id)
        .order_by(SatelliteObservation.captured_at.desc())
        .limit(1)
    )
    if latest and not force and latest.created_at >= datetime.now(UTC) - timedelta(hours=settings.satellite_refresh_hours):
        latest_raw = latest.raw_payload or {}
        latest_indices = latest_raw.get("indices") or {}
        latest_has_indices = any(value is not None for value in latest_indices.values()) or any(
            value is not None for value in (latest.ndvi, latest.ndmi, latest.ndwi)
        )
        # Las filas generadas por versiones anteriores podían marcarse como
        # PROCESSED solo por tener imagen. Si faltan índices, no se devuelve el
        # caché: se intenta completar Statistical API en la misma escena.
        if latest_has_indices:
            return {
                "status": str(latest_raw.get("processing_status") or "CURRENT"),
                "observation_id": str(latest.id),
                "captured_at": latest.captured_at.isoformat(),
                "cloud_percentage": latest.cloud_percentage,
                "ndvi": latest.ndvi,
                "ndmi": latest.ndmi,
                "ndwi": latest.ndwi,
                "indices": latest_indices,
                "processing_error": latest_raw.get("processing_error"),
                "image_available": bool(local_satellite_image_path(latest)),
                "processing_credentials_configured": await copernicus_credentials_configured(db),
                "plot_id": str(plot.id),
            }
    date_to = datetime.now(UTC).date()
    catalog = await search_copernicus_catalog(
        db,
        plot.id,
        date_to - timedelta(days=settings.satellite_lookback_days),
        date_to,
        settings.satellite_max_cloud_percentage,
    )
    if not catalog["features"]:
        return {"status": "NO_SCENE", "plot_id": str(plot.id), "count": 0}
    feature = catalog["features"][0]
    captured_at = _feature_datetime(feature)
    existing = await db.scalar(
        select(SatelliteObservation).where(
            SatelliteObservation.plot_id == plot.id,
            SatelliteObservation.captured_at == captured_at,
        )
    )
    existing_raw = (existing.raw_payload or {}) if existing else {}
    existing_status = str(existing_raw.get("processing_status") or "") if existing else ""
    existing_indices = existing_raw.get("indices") or {}
    existing_has_indices = any(value is not None for value in existing_indices.values()) or bool(
        existing and any(value is not None for value in (existing.ndvi, existing.ndmi, existing.ndwi))
    )
    existing_has_image = bool(existing and local_satellite_image_path(existing))
    existing_complete = existing_status == "PROCESSED" and existing_has_indices and existing_has_image
    integration = await _integration(db)
    client_id, client_secret = _oauth_credentials(integration)
    credentials_ready = bool(client_id and client_secret)
    # Una fila antigua marcada PROCESSED pero sin índices no está completa. Se
    # conserva la imagen y se vuelve a ejecutar la fase estadística.
    if existing and (existing_complete or not credentials_ready):
        return {
            "status": existing_status or "CURRENT",
            "observation_id": str(existing.id),
            "captured_at": existing.captured_at.isoformat(),
            "cloud_percentage": existing.cloud_percentage,
            "ndvi": existing.ndvi,
            "ndmi": existing.ndmi,
            "ndwi": existing.ndwi,
            "indices": existing_indices,
            "processing_error": existing_raw.get("processing_error"),
            "image_available": existing_has_image,
            "processing_credentials_configured": credentials_ready,
            "plot_id": str(plot.id),
        }

    previous = await db.scalar(
        select(SatelliteObservation)
        .where(
            SatelliteObservation.plot_id == plot.id,
            SatelliteObservation.captured_at < captured_at,
        )
        .order_by(SatelliteObservation.captured_at.desc())
        .limit(1)
    )

    geometry = plot_geometry(plot)
    preview_url = _feature_preview(feature)
    ndvi = existing.ndvi if existing else None
    ndmi = existing.ndmi if existing else None
    ndwi = existing.ndwi if existing else None
    statistics: dict[str, Any] = dict(existing_raw.get("statistics") or {})
    indices: dict[str, Any] = dict(existing_raw.get("indices") or {})
    processing_status = "CATALOG_ONLY"
    local_image_path: str | None = existing_raw.get("local_image_path")
    processing_error: str | None = None
    observation_id = existing.id if existing else uuid4()
    if credentials_ready:
        start = captured_at - timedelta(hours=12)
        end = captured_at + timedelta(hours=36)
        phase_errors: list[str] = []
        image_ready = bool(local_image_path and Path(str(local_image_path)).is_file())
        statistics_ready = any(value is not None for value in indices.values())

        source_url = existing.source_url if existing and existing.source_url else (preview_url[:1000] if preview_url else None)
        if not image_ready:
            try:
                image = await _process_image(
                    geometry,
                    start,
                    end,
                    settings.satellite_max_cloud_percentage,
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
                phase_errors.append(message)

        if not statistics_ready:
            try:
                stats = await _index_statistics(
                    geometry,
                    start,
                    end,
                    settings.satellite_max_cloud_percentage,
                    integration,
                )
                new_indices = stats["indices"]
                new_statistics_ready = any(value is not None for value in new_indices.values())
                if new_statistics_ready:
                    ndvi, ndmi, ndwi = stats["ndvi"], stats["ndmi"], stats["ndwi"]
                    indices = new_indices
                    statistics = stats["statistics"]
                    statistics_ready = True
                else:
                    phase_errors.append("Statistical API no devolvió píxeles válidos para los índices solicitados")
            except Exception as exc:
                message = str(exc)[:1000]
                logger.warning("satellite_statistics_failed", error=message)
                phase_errors.append(message)

        processing_error = " | ".join(phase_errors) or None
        if image_ready and statistics_ready:
            processing_status = "PROCESSED"
        elif image_ready or statistics_ready:
            processing_status = "PARTIAL"
        else:
            processing_status = "FAILED"
    else:
        source_url = preview_url[:1000] if preview_url else None

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
    row.cloud_percentage = _feature_cloud(feature)
    row.ndvi = ndvi
    row.ndmi = ndmi
    row.ndwi = ndwi
    row.previous_comparison = comparison
    row.anomalies = []
    row.source_url = source_url
    row.resolution_m = 10
    row.raw_payload = {
        "scene_id": feature.get("id"),
        "feature": feature,
        "geometry_mode": catalog["geometry_mode"],
        "processing_status": processing_status,
        "processing_error": processing_error,
        "local_image_path": local_image_path,
        "preview_url": preview_url,
        "statistics": statistics,
        "indices": indices,
        "scene_metadata": {
            "id": feature.get("id"),
            "collection": feature.get("collection"),
            "datetime": (feature.get("properties") or {}).get("datetime"),
            "platform": (feature.get("properties") or {}).get("platform"),
            "constellation": (feature.get("properties") or {}).get("constellation"),
            "instrument": (feature.get("properties") or {}).get("instruments"),
            "cloud_cover": (feature.get("properties") or {}).get("eo:cloud_cover"),
            "processing_level": (feature.get("properties") or {}).get("processing:level"),
        },
    }
    if not existing:
        db.add(row)
    await db.commit()
    await db.refresh(row)
    analysis = build_satellite_analysis(row, plot, previous=previous, config=_integration_config(await _integration(db)))
    return {
        "status": processing_status,
        "observation_id": str(row.id),
        "captured_at": row.captured_at.isoformat(),
        "cloud_percentage": row.cloud_percentage,
        "ndvi": row.ndvi,
        "ndmi": row.ndmi,
        "ndwi": row.ndwi,
        "indices": (row.raw_payload or {}).get("indices") or {},
        "processing_error": processing_error,
        "image_available": bool(local_image_path),
        "processing_credentials_configured": credentials_ready,
        "plot_id": str(plot.id),
        "analysis": analysis,
    }


async def test_copernicus_integration(
    db: AsyncSession,
    station_id: UUID | None = None,
) -> dict[str, Any]:
    """Execute a real phase-by-phase Copernicus check for one station.

    The test reads geometry directly from stations. It intentionally does not
    depend on legacy plot rows, because those can be stale or malformed.
    """
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
        ).order_by(case((Station.code == "RAUL-PENA", 0), else_=1), Station.updated_at.desc()).limit(1)
    station = await db.scalar(station_query)
    if not station:
        return {
            "station_id": str(station_id) if station_id else None,
            "status": "FAILED",
            "processing_error": "No existe la estación seleccionada o no está activa",
            "diagnostics": {"geometry": "NOT_RUN", "catalog": "NOT_RUN", "oauth": "NOT_RUN", "process": "NOT_RUN", "statistics": "NOT_RUN"},
        }

    integration = await _integration(db)
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
            today - timedelta(days=settings.satellite_lookback_days),
            today,
            settings.satellite_max_cloud_percentage,
        )
        diagnostics["catalog"] = "COMPLETED"
        diagnostics["catalog_scenes"] = catalog["count"]
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

    feature = catalog["features"][0]
    captured_at = _feature_datetime(feature)
    start = captured_at - timedelta(hours=12)
    end = captured_at + timedelta(hours=36)

    if not (client_id and client_secret):
        return {
            "station_id": str(station.id),
            "station": station.name,
            "status": "CATALOG_ONLY",
            "processing_error": None,
            "image_available": False,
            "processing_credentials_configured": False,
            "scene_id": feature.get("id"),
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
            "scene_id": feature.get("id"),
            "captured_at": captured_at.isoformat(),
            "diagnostics": diagnostics,
        }

    image_available = False
    process_error: str | None = None
    try:
        image = await _process_image(geometry, start, end, settings.satellite_max_cloud_percentage, integration)
        image_available = bool(image)
        diagnostics["process"] = "COMPLETED" if image_available else "NO_IMAGE"
    except Exception as exc:
        process_error = str(exc)[:1000]
        diagnostics["process"] = "FAILED"
        diagnostics["process_error"] = process_error

    stats_result: dict[str, Any] = {}
    statistics_error: str | None = None
    try:
        stats_result = await _index_statistics(geometry, start, end, settings.satellite_max_cloud_percentage, integration)
        has_indices = any(stats_result.get(name) is not None for name in INDEX_NAMES)
        diagnostics["statistics"] = "COMPLETED" if has_indices else "NO_VALID_PIXELS"
    except Exception as exc:
        statistics_error = str(exc)[:1000]
        diagnostics["statistics"] = "FAILED"
        diagnostics["statistics_error"] = statistics_error

    phase_errors = [item for item in (process_error, statistics_error) if item]
    completed = image_available and diagnostics["statistics"] == "COMPLETED"
    partial = image_available or diagnostics["statistics"] in {"COMPLETED", "NO_VALID_PIXELS"}
    status = "PROCESSED" if completed else "PARTIAL" if partial else "FAILED"
    return {
        "station_id": str(station.id),
        "station": station.name,
        "status": status,
        "processing_error": " | ".join(phase_errors) or None,
        "image_available": image_available,
        "processing_credentials_configured": True,
        "scene_id": feature.get("id"),
        "captured_at": captured_at.isoformat(),
        "indices": stats_result.get("indices") or {},
        "diagnostics": diagnostics,
    }

