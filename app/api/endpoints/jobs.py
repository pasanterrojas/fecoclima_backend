from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import BackgroundJob
from app.schemas.actions import BackgroundJobPayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict

router = APIRouter(prefix="/jobs", tags=["Trabajos en segundo plano"])


@router.get("")
async def list_jobs(db: DbSession, _: AnalystUser, status: str | None = None, job_type: str | None = None, limit: int = 100) -> dict:
    query = select(BackgroundJob)
    if status:
        query = query.where(BackgroundJob.status == status.upper())
    if job_type:
        query = query.where(BackgroundJob.job_type == job_type.upper())
    rows = list((await db.scalars(query.order_by(BackgroundJob.created_at.desc()).limit(min(limit, 500)))).all())
    return {"items": [model_dict(row) for row in rows]}


@router.get("/{job_id}")
async def job_detail(job_id: UUID, db: DbSession, _: AnalystUser) -> dict:
    row = await db.get(BackgroundJob, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    return model_dict(row)


@router.post("")
async def create_job(payload: BackgroundJobPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await enqueue_job(db, payload.job_type, payload.payload, priority=payload.priority, max_attempts=payload.max_attempts, created_by=actor.id)
    await write_audit(db, action="JOB_CREATE", user=actor, request=request, entity_type="background_job", entity_id=str(row.id), after={"job_type": row.job_type, "payload": row.payload})
    await db.commit()
    return model_dict(row)


@router.post("/{job_id}/retry")
async def retry_job(job_id: UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(BackgroundJob, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    if row.status not in {"FAILED", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="Solo se pueden reintentar trabajos fallidos o cancelados")
    row.status = "PENDING"
    row.next_attempt_at = datetime.now(UTC)
    row.finished_at = None
    row.error_message = None
    row.locked_by = None
    row.locked_at = None
    await write_audit(db, action="JOB_RETRY", user=actor, request=request, entity_type="background_job", entity_id=str(row.id))
    await db.commit()
    return model_dict(row)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(BackgroundJob, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    if row.status == "RUNNING":
        raise HTTPException(status_code=409, detail="No se cancela un trabajo ya tomado por un worker")
    if row.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="El trabajo ya terminó")
    row.status = "CANCELLED"
    row.finished_at = datetime.now(UTC)
    await write_audit(db, action="JOB_CANCEL", user=actor, request=request, entity_type="background_job", entity_id=str(row.id))
    await db.commit()
    return model_dict(row)
