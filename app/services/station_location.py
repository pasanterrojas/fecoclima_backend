"""Station-only coordinate import, cached reverse address and terrain elevation."""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import re
import sqlite3
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx

from app.core.config import settings

GOOGLE_HOSTS = {"maps.app.goo.gl", "goo.gl", "maps.google.com", "www.google.com", "google.com", "www.google.com.py", "maps.google.com.py"}
PAIR = r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"


def coordinates_from_text(value: str) -> tuple[float, float]:
    value = unquote(value.strip())
    parsed = urlsplit(value)
    candidates: list[str] = []
    if parsed.scheme:
        if parsed.scheme != "https" or parsed.hostname not in GOOGLE_HOSTS or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError("Pegue un enlace HTTPS de Google Maps o coordenadas latitud, longitud")
        # A place marker takes priority over @..., which can be the camera centre.
        marker = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", value)
        if marker:
            candidates.append(f"{marker[1]},{marker[2]}")
        params = parse_qs(parsed.query)
        for key in ("query", "q", "ll", "destination"):
            candidates.extend(params.get(key, []))
        camera = re.search(r"@" + PAIR, value)
        if camera:
            candidates.append(f"{camera[1]},{camera[2]}")
    else:
        candidates.append(value)
    for candidate in candidates:
        match = re.fullmatch(PAIR, candidate.strip())
        if not match:
            continue
        latitude, longitude = map(float, match.groups())
        if math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return latitude, longitude
    raise ValueError("No se encontraron coordenadas. Copie el enlace completo del punto o pegue latitud, longitud")


async def expand_maps_link(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.hostname not in {"maps.app.goo.gl", "goo.gl"}:
        return value
    current = value.strip()
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for _ in range(5):
            parsed = urlsplit(current)
            if parsed.scheme != "https" or parsed.hostname not in GOOGLE_HOSTS or parsed.username or parsed.password or parsed.port not in {None, 443}:
                raise ValueError("El enlace corto no redirige a una ubicación válida de Google Maps")
            response = await client.get(current)
            if response.is_redirect and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            response.raise_for_status()
            return current
    raise ValueError("El enlace corto tiene demasiadas redirecciones; copie la URL completa")


def address_fields(address: dict) -> dict:
    return {
        "country": address.get("country"),
        "department": address.get("state") or address.get("region"),
        "district": address.get("county") or address.get("state_district") or address.get("municipality"),
        "locality": next((address.get(key) for key in ("city", "town", "village", "hamlet", "suburb", "neighbourhood") if address.get(key)), None),
    }


def lookup_coordinates(latitude: float, longitude: float) -> dict:
    """One shared file lock/cache per deployment host, including API workers.

    Public Nominatim is used only for this manual administrative operation.
    A durable timestamp enforces <=1 request/second across local processes;
    cache entries are reused for 30 days. Configure a private instance for a
    deployment distributed across several hosts.
    """
    cache_key = f"{latitude:.6f},{longitude:.6f}"
    cache_path = settings.storage_path / "station-location-cache.sqlite3"
    with (settings.storage_path / "station-location-cache.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(cache_path) as cache:
            cache.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, payload TEXT)")
            cache.execute("CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, fetched REAL)")
            row = cache.execute("SELECT payload FROM cache WHERE key=? AND expires>?", (cache_key, time.time())).fetchone()
            if row:
                return json.loads(row[0])
            result = {"latitude": latitude, "longitude": longitude, "altitude_m": None, "country": None, "department": None, "district": None, "locality": None, "warnings": [], "sources": []}
            last = cache.execute("SELECT fetched FROM limits WHERE key='reverse'").fetchone()
            if last:
                time.sleep(max(0, 1.05 - (time.time() - last[0])))
            cache.execute("INSERT OR REPLACE INTO limits VALUES ('reverse', ?)", (time.time(),))
            cache.commit()
            with httpx.Client(timeout=12, headers={"User-Agent": "FECOCLIMA-IA/1.0 (station administration)"}) as client:
                try:
                    response = client.get(f"{settings.station_geocoding_base_url.rstrip('/')}/reverse", params={"format": "jsonv2", "lat": latitude, "lon": longitude, "zoom": 18, "addressdetails": 1, "accept-language": "es"})
                    response.raise_for_status()
                    result.update(address_fields(response.json().get("address") or {}))
                    result["sources"].append("OpenStreetMap / Nominatim")
                    if not result["department"] or not result["district"]:
                        result["warnings"].append("La dirección no está completa en la cartografía. Revise departamento, distrito y localidad.")
                except (httpx.HTTPError, ValueError, TypeError):
                    result["warnings"].append("No se pudo completar la dirección automáticamente; puede ingresarla manualmente.")
                try:
                    response = client.get(settings.station_elevation_url, params={"latitude": latitude, "longitude": longitude})
                    response.raise_for_status()
                    elevation = response.json().get("elevation", [None])[0]
                    if elevation is not None and math.isfinite(float(elevation)):
                        result["altitude_m"] = float(elevation)
                        result["sources"].append("Open-Meteo / Copernicus DEM GLO-90 (altitud estimada)")
                    else:
                        result["warnings"].append("No hay altitud disponible para este punto; puede ingresarla manualmente.")
                except (httpx.HTTPError, ValueError, TypeError, IndexError):
                    result["warnings"].append("No se pudo obtener la altitud estimada; puede ingresarla manualmente.")
            expires = time.time() + (300 if result["warnings"] else 30 * 86400)
            cache.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (cache_key, expires, json.dumps(result)))
            return result


async def resolve_station_location(value: str) -> dict:
    expanded = await expand_maps_link(value)
    latitude, longitude = coordinates_from_text(expanded)
    return await asyncio.to_thread(lookup_coordinates, latitude, longitude)
