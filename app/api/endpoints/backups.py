from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession
from app.db.models import DatabaseBackup
from app.schemas.actions import BackupCreatePayload
from app.services.audit import write_audit
from app.services.backups import create_database_backup, verify_backup_file
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict

router = APIRouter(prefix="/backups", tags=["Respaldos"])


@router.get("")
async def list_backups(db: DbSession, _: AdminUser, limit: int = 100) -> dict:
    rows = list((await db.scalars(select(DatabaseBackup).order_by(DatabaseBackup.created_at.desc()).limit(min(limit, 500)))).all())
    return {"items": [model_dict(row) for row in rows]}


@router.post("")
async def create_backup(payload: BackupCreatePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if payload.run_in_background:
        job = await enqueue_job(db, "DATABASE_BACKUP", {"label": payload.label}, priority=120, created_by=actor.id)
        await write_audit(db, action="BACKUP_REQUEST", user=actor, request=request, entity_type="background_job", entity_id=str(job.id), after={"label": payload.label})
        await db.commit()
        return {"status": job.status, "job_id": str(job.id)}
    row = await create_database_backup(db, actor.id, payload.label)
    return model_dict(row)


@router.post("/{backup_id}/verify")
async def verify_backup(backup_id: UUID, db: DbSession, _: AdminUser) -> dict:
    try:
        row = await verify_backup_file(db, backup_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return model_dict(row)


@router.get("/{backup_id}/download")
async def download_backup(backup_id: UUID, db: DbSession, _: AdminUser):
    row = await db.get(DatabaseBackup, backup_id)
    if not row or not row.storage_location:
        raise HTTPException(status_code=404, detail="Respaldo no encontrado")
    path = Path(row.storage_location)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="El archivo de respaldo no existe")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.delete("/{backup_id}")
async def delete_backup(backup_id: UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await db.get(DatabaseBackup, backup_id)
    if not row:
        raise HTTPException(status_code=404, detail="Respaldo no encontrado")
    if row.storage_location:
        Path(row.storage_location).unlink(missing_ok=True)
    await db.delete(row)
    await write_audit(db, action="BACKUP_DELETE", user=actor, request=request, entity_type="database_backup", entity_id=str(backup_id))
    await db.commit()
    return {"ok": True}
