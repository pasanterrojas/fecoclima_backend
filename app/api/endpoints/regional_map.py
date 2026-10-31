"""Open-data regional map. Measurements and spatial estimates stay distinguishable."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from shapely.geometry import box, mapping, shape
from sqlalchemy import select

from app.api.deps import DbSession
from app.db.models import BackgroundJob
from app.services.jobs import enqueue_job
from app.services.map_terrain import atomic_write, cache_dir, cached_download, terrain_tile
from app.services.parcel_browser import BrowserParcelActor, browser_actor
from app.services.regional_map import VARIABLES, climate_tile, map_variables

router = APIRouter(prefix="/public/map", tags=["Mapa regional sin WebGL"])
Actor = Annotated[BrowserParcelActor, Depends(browser_actor)]


def _rate(request, limit):
    from app.api.endpoints.agent_public import _check_rate_limit

    _check_rate_limit(request, limit=limit)


def _validate_tile(z, x, y):
    if not (0 <= z <= 14 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(404, "Tesela fuera de rango")


@router.post(
    "/analyses",
    description="Actualiza todas las estaciones activas y analiza exactamente las últimas 72 horas. Requiere la sesión firmada del navegador.",
)
async def analyze(request: Request, db: DbSession, actor: Actor):
    _rate(request, 3)
    # Serialize repeated clicks for this browser, without locking another visitor.
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
        {"owner": "regional-map:" + str(actor.browser_id)},
    )
    existing = await db.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.job_type == "REGIONAL_MAP_ANALYSIS",
            BackgroundJob.status.in_(["PENDING", "RUNNING"]),
            BackgroundJob.payload["browser_id"].as_string() == str(actor.browser_id),
        )
        .order_by(BackgroundJob.created_at.desc())
        .limit(1)
    )
    if existing:
        return {"job_id": str(existing.id), "status": existing.status}
    job = await enqueue_job(
        db,
        "REGIONAL_MAP_ANALYSIS",
        {"browser_id": str(actor.browser_id), "window_hours": 72},
        priority=60,
        max_attempts=1,
    )
    return {"job_id": str(job.id), "status": job.status}


async def _job(db, job_id, actor=None):
    job = await db.get(BackgroundJob, job_id)
    if (
        not job
        or job.job_type != "REGIONAL_MAP_ANALYSIS"
        or (actor and job.payload.get("browser_id") != str(actor.browser_id))
    ):
        raise HTTPException(404, "Análisis de mapa no encontrado")
    return job


def _snapshot(job_id):
    path = cache_dir() / "snapshots" / f"{job_id}.json"
    if not path.is_file():
        raise HTTPException(410, "La captura ya no está disponible; vuelva a analizar")
    return json.loads(path.read_text())


@router.get("/analyses/{job_id}")
async def analysis(job_id: UUID, db: DbSession, actor: Actor, response: Response):
    job = await _job(db, job_id, actor)
    response.headers["Cache-Control"] = "no-store"
    result = {"job_id": str(job.id), "status": job.status, "snapshot": None}
    if job.status == "COMPLETED":
        result["snapshot"] = _snapshot(job_id)
        result["snapshot"]["variables"] = map_variables(result["snapshot"]["stations"])
    elif job.status == "FAILED":
        result["message"] = "No se pudo preparar el mapa. Puede volver a analizar."
    return result


@router.get(
    "/analyses/{job_id}/tiles/{variable}/{z}/{x}/{y}.png",
    description="PNG público con tendencia regional estimada o interpolación limitada a las influencias, según domain. La escala relative resalta diferencias observadas. Solo contiene observaciones públicas de estaciones, sin parcelas ni identidad del navegador.",
)
async def climate(
    job_id: UUID,
    variable: str,
    z: int,
    x: int,
    y: int,
    db: DbSession,
    request: Request,
    altitude: bool = False,
    domain: Literal["regional", "influence"] = "regional",
    scale: Literal["relative", "absolute"] = "relative",
):
    _validate_tile(z, x, y)
    if variable not in VARIABLES:
        raise HTTPException(404, "Variable no disponible")
    job = await _job(db, job_id)
    if job.status != "COMPLETED":
        raise HTTPException(409, "El análisis todavía no está listo")
    snapshot = _snapshot(job_id)
    adjusted = altitude and variable == "temperature"
    path = cache_dir() / f"climate-v7/{job_id}/{variable}/{domain}/{scale}/{int(adjusted)}/{z}/{x}/{y}.png"
    if path.is_file():
        return Response(
            path.read_bytes(), media_type="image/png", headers={"Cache-Control": "public,max-age=86400"}
        )
    _rate(request, 240)
    try:
        data = await climate_tile(
            snapshot, variable, z, x, y, adjusted, domain == "regional", scale == "relative"
        )
    except Exception:
        raise HTTPException(503, "No se pudo preparar la capa; pruebe sin corrección por altitud") from None
    atomic_write(path, data)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public,max-age=86400"})


@router.get("/terrain/{kind}/{z}/{x}/{y}.png")
async def terrain(
    kind: Literal["hillshade", "altitude", "slope", "aspect"], z: int, x: int, y: int, request: Request
):
    _validate_tile(z, x, y)
    _rate(request, 240)
    try:
        data = await terrain_tile(kind, z, x, y)
    except Exception:
        raise HTTPException(503, "La fuente abierta de relieve no está disponible temporalmente") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "public,max-age=604800"})


@router.get(
    "/rivers",
    description="Ríos principales de Natural Earth, dominio público, escala 1:10 millones. No representa cursos menores ni límites de parcela.",
)
async def rivers(
    request: Request,
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-85, le=85),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-85, le=85),
):
    if west >= east or south >= north or east - west > 15 or north - south > 15:
        raise HTTPException(422, "Acérquese al área para consultar ríos (máximo 15 grados)")
    _rate(request, 12)
    try:
        raw = await cached_download(
            "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_rivers_lake_centerlines.geojson",
            "rivers/ne_10m_rivers.geojson",
            max_bytes=35_000_000,
        )
        data = json.loads(raw)
    except Exception:
        raise HTTPException(503, "No se pudo consultar la fuente abierta de ríos") from None
    boundary = box(west, south, east, north)
    features = []
    for feature in data["features"]:
        geometry = shape(feature["geometry"])
        if geometry.intersects(boundary):
            clipped = geometry.intersection(boundary)
            features.append(
                {
                    "type": "Feature",
                    "geometry": mapping(clipped),
                    "properties": {
                        "name": feature["properties"].get("name") or "Río",
                        "source": "Natural Earth",
                    },
                }
            )
    return {
        "type": "FeatureCollection",
        "features": features,
        "source": "Natural Earth · dominio público · escala 1:10 millones",
    }

