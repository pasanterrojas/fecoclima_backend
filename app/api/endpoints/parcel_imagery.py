"""Session-scoped Sentinel previews, independent of crop and station coverage."""
from datetime import UTC, datetime, timedelta
import math
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.core.security import create_token, decode_token
from app.db.models import BackgroundJob, Plot, SatelliteObservation
from app.schemas.plots import ParcelCoveragePayload
from app.services.geospatial import validate_polygon, parcel_metrics
from app.services.jobs import enqueue_unique
from app.services.parcel_identity import geometry_key
from app.services.satellite import satellite_observation_payload, local_satellite_image_path, local_satellite_index_image_path, versioned_index_url, INDEX_NAMES
from app.services.spectral_palettes import VISUAL_VERSION

router=APIRouter(prefix="/public/parcel-imagery",tags=["Vista satelital por polígono"])


def browser_id(request:Request)->UUID:
    try:return UUID(request.headers.get("X-Parcel-Session", ""))
    except ValueError:raise HTTPException(422,"Inicie una sesión de navegador válida") from None


async def preview(db,plot_id:UUID,request:Request):
    row=await db.get(Plot,plot_id)
    if not row or not row.active or row.deleted_at or row.created_by or row.crop_id or row.browser_session_id!=browser_id(request):
        raise HTTPException(404,"Vista satelital no disponible para este navegador")
    return row


async def snapshot(db: AsyncSession, plot: Plot) -> dict:
    rows=(await db.scalars(select(SatelliteObservation).where(SatelliteObservation.plot_id==plot.id).order_by(SatelliteObservation.captured_at.desc()).limit(90))).all()
    token=create_token(str(plot.id),"parcel_imagery",timedelta(hours=8))
    history=[];radar=None
    for row in rows:
        raw=row.raw_payload or {}
        if raw.get("processing_geometry")!=plot.geojson:continue
        if row.provider=="COPERNICUS_SENTINEL_1_GRD":
            if radar is None:radar={"captured_at":row.captured_at.isoformat(),"vv_mean":raw.get("vv_mean"),"vh_mean":raw.get("vh_mean"),"unit":"gamma0 lineal"}
            continue
        if row.provider!="COPERNICUS_DATA_SPACE_SENTINEL_2_L2A":continue
        item=await satellite_observation_payload(db,row,plot)
        if not item.get("image_available") and not any(item.get("index_images",{}).values()) and not any(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in (item.get('indices') or {}).values() if value is not None
        ):continue
        root=f"/public/parcel-imagery/{plot.id}/images/{row.id}"
        item["source_url"]=f"{root}/image.jpg?token={token}"
        image_paths=item.get("index_images",{})
        item["index_images"]={}
        for key,path in image_paths.items():
            url=versioned_index_url(root,key,item.get("index_visual_versions") or {})
            item["index_images"][key]=f"{url}{'&' if '?' in url else '?'}token={token}" if path else None
        history.append(item)
    job=await db.scalar(select(BackgroundJob).where(BackgroundJob.job_type=="PLOT_SATELLITE_SYNC",BackgroundJob.payload=={"plot_id":str(plot.id)}).order_by(BackgroundJob.created_at.desc()).limit(1))
    pending=job and job.status in {"PENDING","RUNNING"}
    return {"id":str(plot.id),"status":"PROCESSING" if pending else "AVAILABLE" if history else "NO_VALID_SCENE", "satellite":history[0] if history else None,"history":history,"radar":radar,"job_status":job.status if job else None,"area_ha":float(plot.area_ha or 0),"message":"Buscando y procesando escenas para su polígono." if pending else "Las imágenes corresponden al polígono; la cobertura meteorológica se consulta por separado." if history else "Aún no hay una escena válida procesada. Puede solicitar una nueva búsqueda; la disponibilidad depende de Copernicus."}


@router.post("")
async def request_preview(payload:ParcelCoveragePayload,request:Request,db:DbSession)->dict:
    from app.api.endpoints.agent_public import _check_rate_limit
    _check_rate_limit(request,limit=12,window_seconds=60)
    session=browser_id(request)
    try:geometry=validate_polygon(payload.geojson)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    metrics = parcel_metrics(geometry)
    if metrics["area_ha"] > 25000:
        raise HTTPException(422, "Para la vista previa, delimite un área de hasta 25.000 hectáreas.")
    key=geometry_key(geometry)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),{"key":f"imagery:{session}"})
    plot=await db.scalar(select(Plot).where(Plot.browser_session_id==session,Plot.geometry_key==key,Plot.created_by.is_(None),Plot.crop_id.is_(None),Plot.deleted_at.is_(None)).limit(1))
    if not plot:
        count=await db.scalar(select(func.count(Plot.id)).where(Plot.browser_session_id==session,Plot.created_by.is_(None),Plot.crop_id.is_(None),Plot.created_at>=datetime.now(UTC)-timedelta(days=1)))
        if count>=10:raise HTTPException(429,"Ya consultó diez polígonos en esta sesión. Reutilice una de sus parcelas.")
        plot=Plot(name="Vista satelital de polígono",geojson=geometry,geometry_key=key,browser_session_id=session,active=True,**metrics)
        db.add(plot);await db.flush();await db.commit()
    # Reuse recent work rather than scheduling a provider request on every edit.
    recent=await db.scalar(select(BackgroundJob).where(BackgroundJob.job_type=="PLOT_SATELLITE_SYNC",BackgroundJob.payload=={"plot_id":str(plot.id)},BackgroundJob.created_at>=datetime.now(UTC)-timedelta(minutes=30)).order_by(BackgroundJob.created_at.desc()).limit(1))
    failed = recent and (recent.status == "FAILED" or (recent.result or {}).get("sentinel_2", {}).get("status") == "UNAVAILABLE")
    visual_result = ((recent.result or {}).get("sentinel_2") or {}) if recent else {}
    versions = visual_result.get("index_visual_versions") or {}
    outdated = (recent and recent.status == "COMPLETED" and visual_result.get("visual_layers_available", 0)
                and any(versions.get(name) != VISUAL_VERSION for name in INDEX_NAMES))
    if not recent or failed or outdated:
        await enqueue_unique(db, "PLOT_SATELLITE_SYNC", {"plot_id": str(plot.id)}, priority=85)
    return await snapshot(db,plot)


@router.get("/{plot_id}")
async def get_preview(plot_id:UUID,request:Request,db:DbSession)->dict:
    return await snapshot(db,await preview(db,plot_id,request))


@router.get("/{plot_id}/images/{observation_id}/{layer}")
async def preview_image(plot_id:UUID,observation_id:UUID,layer:str,token:str,db:DbSession):
    try:
        claims=decode_token(token,"parcel_imagery")
        if claims["sub"]!=str(plot_id):raise ValueError()
    except (ValueError,KeyError):raise HTTPException(403,"Enlace satelital inválido o vencido") from None
    plot = await db.get(Plot, plot_id)
    row = await db.get(SatelliteObservation, observation_id)
    if (not plot or not plot.active or plot.deleted_at or plot.crop_id or plot.created_by
            or not row or row.plot_id != plot_id
            or (row.raw_payload or {}).get("processing_geometry") != plot.geojson):
        raise HTTPException(404, "Imagen no disponible")
    if layer not in {"image.jpg", *(f"{name}.png" for name in INDEX_NAMES)}:
        raise HTTPException(404, "Capa no disponible")
    path=local_satellite_image_path(row) if layer=="image.jpg" else local_satellite_index_image_path(row,layer.removesuffix(".png"))
    if not path:raise HTTPException(404,"Capa no disponible")
    return FileResponse(path,media_type="image/jpeg" if layer=="image.jpg" else "image/png",headers={"Cache-Control":"private, no-store","Referrer-Policy":"no-referrer"})
