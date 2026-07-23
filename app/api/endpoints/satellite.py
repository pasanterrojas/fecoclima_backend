from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import Plot, SatelliteObservation
from app.schemas.actions import SatelliteObservationPayload, SatelliteSyncPayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.satellite import search_copernicus_catalog
from app.services.serialization import model_dict

router = APIRouter(prefix="/satellite", tags=["Información satelital"])


@router.get("")
async def list_observations(db: DbSession, _: AnalystUser, plot_id: UUID | None = None, limit: int = 100) -> dict:
    query = select(SatelliteObservation)
    if plot_id:
        query = query.where(SatelliteObservation.plot_id == plot_id)
    rows = list((await db.scalars(query.order_by(SatelliteObservation.captured_at.desc()).limit(min(limit, 500)))).all())
    return {"items": [model_dict(row, exclude={"raw_payload"}) for row in rows]}


@router.post("")
async def create_observation(payload: SatelliteObservationPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    plot = await db.get(Plot, payload.plot_id)
    if not plot or plot.deleted_at:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")
    row = SatelliteObservation(**payload.model_dump())
    db.add(row)
    await db.flush()
    await write_audit(db, action="SATELLITE_OBSERVATION_CREATE", user=actor, request=request, entity_type="satellite_observation", entity_id=str(row.id), after=model_dict(row, exclude={"raw_payload"}))
    await db.commit()
    return model_dict(row, exclude={"raw_payload"})


@router.post("/search")
async def search_catalog(payload: SatelliteSyncPayload, db: DbSession, actor: AnalystUser) -> dict:
    if payload.run_in_background:
        job = await enqueue_job(
            db,
            "SATELLITE_SYNC",
            {
                "plot_id": str(payload.plot_id),
                "date_from": payload.date_from.isoformat(),
                "date_to": payload.date_to.isoformat(),
                "provider": payload.provider,
                "max_cloud_percentage": payload.max_cloud_percentage,
            },
            priority=90,
            created_by=actor.id,
        )
        return {"status": job.status, "job_id": str(job.id)}
    try:
        return await search_copernicus_catalog(db, payload.plot_id, payload.date_from, payload.date_to, payload.max_cloud_percentage)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{observation_id}")
async def delete_observation(observation_id: UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(SatelliteObservation, observation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Observación no encontrada")
    await db.delete(row)
    await write_audit(db, action="SATELLITE_OBSERVATION_DELETE", user=actor, request=request, entity_type="satellite_observation", entity_id=str(observation_id))
    await db.commit()
    return {"ok": True}
