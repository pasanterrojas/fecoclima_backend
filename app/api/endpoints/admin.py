from __future__ import annotations

import uuid
from typing import Literal
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.db.models import Crop, Plot, PlotAnalysis, SatelliteObservation, AIAgent, AIAgentRun, AlertEvaluation, AuditLog, BackgroundJob, Report, Station, SystemSetting, User, WeatherReading
from app.schemas.resources import SettingPayload
from app.services.audit import write_audit
from app.services.serialization import model_dict

router = APIRouter(tags=["Administración"])


@router.get('/admin/parcel-history')
async def parcel_history(
    db: DbSession, _: AdminUser, search: str = Query('', max_length=180),
    state: Literal['all', 'active', 'deleted', 'preview'] = 'all',
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
) -> dict:
    """Persistent registry, including deleted parcels and exploratory polygons."""
    query = select(Plot)
    if search.strip():
        match = f'%{search.strip()}%'
        query = query.where(Plot.name.ilike(match) | Plot.producer_name.ilike(match) | Plot.producer_document.ilike(match))
    if state == 'deleted':
        query = query.where(Plot.deleted_at.is_not(None))
    elif state == 'preview':
        query = query.where(Plot.crop_id.is_(None), Plot.deleted_at.is_(None))
    elif state == 'active':
        query = query.where(Plot.active.is_(True), Plot.deleted_at.is_(None), Plot.crop_id.is_not(None))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(Plot.created_at.desc(), Plot.id.desc()).offset(offset).limit(limit))).all()
    counts = dict((await db.execute(select(SatelliteObservation.plot_id, func.count()).where(
        SatelliteObservation.plot_id.in_([row.id for row in rows]),
        SatelliteObservation.provider == 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A',
    ).group_by(SatelliteObservation.plot_id))).all())
    return {'items': [{**model_dict(row), 'scene_count': counts.get(row.id, 0)} for row in rows],
            'total': total or 0, 'limit': limit, 'offset': offset}


async def _archived_parcel(db: DbSession, plot_id: uuid.UUID) -> Plot:
    plot = await db.get(Plot, plot_id)
    if not plot:
        raise HTTPException(404, 'Parcela no encontrada en el histórico')
    return plot


@router.get('/admin/parcel-history/{plot_id}')
async def archived_parcel(plot_id: uuid.UUID, db: DbSession, _: AdminUser) -> dict:
    return model_dict(await _archived_parcel(db, plot_id))


@router.get('/admin/parcel-history/{plot_id}/images')
async def archived_parcel_images(
    plot_id: uuid.UUID, db: DbSession, _: AdminUser,
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
) -> dict:
    """All stored optical scenes, including earlier polygon geometries and failed scenes.

    Producer limits never prune these records or their downloaded files.
    Analysis references identify which images actually supplied an AI snapshot.
    """
    from app.services.satellite import satellite_observation_payload, versioned_index_url

    plot = await _archived_parcel(db, plot_id)
    query = select(SatelliteObservation).where(
        SatelliteObservation.plot_id == plot_id,
        SatelliteObservation.provider == 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A',
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(SatelliteObservation.captured_at.desc(), SatelliteObservation.id.desc()).offset(offset).limit(limit))).all()
    scene_ids = [str(row.id) for row in rows]
    analysis_refs = (await db.execute(select(
        PlotAnalysis.id, PlotAnalysis.context['satellite']['id'].as_string(),
    ).where(PlotAnalysis.plot_id == plot_id, PlotAnalysis.context['satellite']['id'].as_string().in_(scene_ids)))).all()
    items = []
    for row in rows:
        raw = row.raw_payload or {}
        # Compute scene metadata against the polygon used at capture, without editing the parcel.
        scene_plot = Plot(**{column.key: getattr(plot, column.key) for column in Plot.__table__.columns})
        scene_plot.geojson = raw.get('processing_geometry') or plot.geojson
        item = await satellite_observation_payload(db, row, scene_plot)
        root = f'/admin/parcel-history/{plot_id}/images/{row.id}'
        item['source_url'] = f'{root}/image.jpg' if item.get('image_available') else None
        item['index_images'] = {name: versioned_index_url(root, name, item.get('index_visual_versions') or {}) if path else None
                                for name, path in (item.get('index_images') or {}).items()}
        item['processing_geometry'] = raw.get('processing_geometry')
        item['used_in_analyses'] = [str(analysis_id) for analysis_id, scene_id in analysis_refs if scene_id == str(row.id)]
        items.append(item)
    return {'items': items, 'total': total or 0, 'limit': limit, 'offset': offset}


@router.get('/admin/parcel-history/{plot_id}/images/{observation_id}/{layer}')
async def archived_parcel_image(
    plot_id: uuid.UUID, observation_id: uuid.UUID, layer: str, db: DbSession, _: AdminUser,
    download: bool = False,
) -> FileResponse:
    from app.services.satellite import INDEX_NAMES, local_satellite_image_path, local_satellite_index_image_path

    await _archived_parcel(db, plot_id)
    row = await db.get(SatelliteObservation, observation_id)
    if not row or row.plot_id != plot_id or layer not in {'image.jpg', *(f'{name}.png' for name in INDEX_NAMES)}:
        raise HTTPException(404, 'Imagen no disponible')
    path = local_satellite_image_path(row) if layer == 'image.jpg' else local_satellite_index_image_path(row, layer.removesuffix('.png'))
    if not path:
        raise HTTPException(404, 'Capa no disponible')
    filename = f'{plot_id}-{row.captured_at.date()}-{row.id}-{layer}' if download else None
    return FileResponse(path, media_type='image/jpeg' if layer == 'image.jpg' else 'image/png', filename=filename,
                        headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer'})


@router.get("/admin/summary")
async def summary(db: DbSession, _: AdminUser) -> dict:
    async def count(query): return int((await db.scalar(query)) or 0)
    return {
        "stations_active": await count(select(func.count(Station.id)).where(Station.active.is_(True), Station.deleted_at.is_(None))),
        "users_active": await count(select(func.count(User.id)).where(User.active.is_(True), User.deleted_at.is_(None))),
        "readings_total": await count(select(func.count(WeatherReading.id))),
        "alerts_72h": await count(select(func.count(AlertEvaluation.id)).where(AlertEvaluation.evaluated_at >= datetime.now(UTC) - timedelta(hours=72))),
        "agents_active": await count(select(func.count(AIAgent.id)).where(AIAgent.active.is_(True), AIAgent.deleted_at.is_(None))),
        "plots_total": await count(select(func.count(Plot.id)).where(Plot.deleted_at.is_(None), Plot.crop_id.is_not(None))),
        "crops_active": await count(select(func.count(Crop.id)).where(Crop.active.is_(True), Crop.deleted_at.is_(None))),
        "reports_total": await count(select(func.count(Report.id))),
    }


@router.get("/admin/jobs")
async def jobs(db: DbSession, _: AdminUser, limit: int = 50) -> list[dict]:
    rows = (await db.scalars(select(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(min(limit, 250)))).all()
    return [model_dict(row) for row in rows]


@router.get("/admin/ai-runs")
async def recent_ai_runs(db: DbSession, _: AdminUser) -> dict:
    """Operational transparency without leaking prompts, keys or provider error bodies."""
    since = datetime.now(UTC) - timedelta(hours=24)
    completed = await db.scalar(select(func.count(AIAgentRun.id)).where(AIAgentRun.created_at >= since, AIAgentRun.status == "COMPLETED"))
    fallback = await db.scalar(select(func.count(AIAgentRun.id)).where(AIAgentRun.created_at >= since, AIAgentRun.status == "FALLBACK"))
    rows = (await db.scalars(select(AIAgentRun).order_by(AIAgentRun.created_at.desc()).limit(12))).all()
    items = []
    for row in rows:
        payload = row.input_payload or {}
        output = row.output_payload or {}
        reason = output.get("_fallback_reason") or output.get("fallback_reason")
        if row.status == "FALLBACK" and not reason:
            reason = "provider_auth" if "HTTP 401" in (row.error_message or "") else "provider_error"
        items.append({
            "created_at": row.created_at.isoformat(),
            "status": row.status,
            "reason": reason,
            "widget": payload.get("widget"),
            "model": row.model if row.status == "COMPLETED" else None,
            "duration_ms": row.duration_ms,
        })
    return {"completed_24h": completed or 0, "fallback_24h": fallback or 0, "items": items}


@router.get("/audit")
async def audit(db: DbSession, _: AdminUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).offset(offset).limit(min(limit, 500)))).all()
    return {"items": [model_dict(row, exclude={"before_data", "after_data", "extra_data"}) for row in rows]}


@router.get("/settings")
async def settings_list(db: DbSession, _: AdminUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(SystemSetting).order_by(SystemSetting.key).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [model_dict(row) for row in rows]}


@router.post("/settings")
async def setting_create(payload: SettingPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if await db.scalar(select(SystemSetting).where(SystemSetting.key == payload.key)):
        raise HTTPException(status_code=409, detail="La clave ya existe")
    row = SystemSetting(**payload.model_dump(), updated_by=actor.id); db.add(row); await db.flush()
    await write_audit(db, action="SETTING_CREATE", user=actor, request=request, entity_type="system_setting", entity_id=str(row.id), after=model_dict(row))
    await db.commit(); return model_dict(row)


@router.put("/settings/{setting_id}")
async def setting_update(setting_id: uuid.UUID, payload: SettingPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(SystemSetting, setting_id)
    if not row: raise HTTPException(status_code=404, detail="Configuración no encontrada")
    before = model_dict(row)
    for key, value in payload.model_dump().items(): setattr(row, key, value)
    row.updated_by = actor.id; await db.flush()
    await write_audit(db, action="SETTING_UPDATE", user=actor, request=request, entity_type="system_setting", entity_id=str(row.id), before=before, after=model_dict(row))
    await db.commit(); return model_dict(row)


@router.delete("/settings/{setting_id}")
async def setting_delete(setting_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(SystemSetting, setting_id)
    if not row: raise HTTPException(status_code=404, detail="Configuración no encontrada")
    await db.delete(row)
    await write_audit(db, action="SETTING_DELETE", user=actor, request=request, entity_type="system_setting", entity_id=str(setting_id))
    await db.commit(); return {"ok": True}


@router.get("/audit/{audit_id}")
async def get_audit(audit_id: uuid.UUID, db: DbSession, _: AdminUser) -> dict:
    row = await db.get(AuditLog, audit_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evento de auditoría no encontrado")
    return model_dict(row)
