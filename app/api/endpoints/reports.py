from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.db.models import Crop, CropRuleVersion, Report, ReportFile, Station
from app.schemas.resources import ReportCreate
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict

router = APIRouter(prefix="/reports", tags=["Reportes"])


async def _serialize(db: DbSession, report: Report) -> dict:
    station, crop = await db.get(Station, report.station_id), await db.get(Crop, report.crop_id)
    file = await db.scalar(select(ReportFile).where(ReportFile.report_id == report.id).order_by(ReportFile.created_at.desc()).limit(1))
    return {**model_dict(report), "station_name": station.name if station else None, "crop_name": crop.name if crop else None, "file_available": bool(file)}


@router.get("/public")
async def public_reports(db: DbSession, limit: int = 30) -> list[dict]:
    rows = (await db.scalars(select(Report).order_by(Report.created_at.desc()).limit(min(limit, 100)))).all()
    return [await _serialize(db, row) for row in rows]


@router.get("")
async def admin_reports(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(Report).order_by(Report.created_at.desc()).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _serialize(db, row) for row in rows]}


@router.post("")
async def create_report(payload: ReportCreate, request: Request, db: DbSession) -> dict:
    # Basic database-backed rate guard for anonymous public generation.
    recent = await db.scalar(select(func.count(Report.id)).where(Report.created_at >= datetime.now(UTC) - timedelta(minutes=10)))
    if (recent or 0) >= 30:
        raise HTTPException(status_code=429, detail="Hay demasiados reportes en cola. Intente nuevamente en unos minutos")
    crop = await db.get(Crop, payload.crop_id) if payload.crop_id else None
    if not crop and payload.crop_code:
        crop = await db.scalar(select(Crop).where(Crop.code == payload.crop_code.upper(), Crop.active.is_(True)))
    station = await db.get(Station, payload.station_id)
    if not station or not crop:
        raise HTTPException(status_code=400, detail="Estación o cultivo inválido")
    rule = await db.scalar(select(CropRuleVersion).where(CropRuleVersion.crop_id == crop.id, CropRuleVersion.status == "PUBLISHED").order_by(CropRuleVersion.created_at.desc()).limit(1))
    report = Report(
        title=f"Reporte {crop.name} · {station.name}",
        station_id=station.id,
        crop_id=crop.id,
        crop_cycle_id=None,
        planting_date=payload.planting_date,
        date_from=payload.date_from,
        date_to=payload.date_to,
        status="PENDING",
        rule_version=rule.version if rule else None,
    )
    db.add(report); await db.flush()
    job = await enqueue_job(db, "REPORT_GENERATE", {"report_id": str(report.id)}, priority=50)
    await write_audit(db, action="REPORT_REQUEST", request=request, entity_type="report", entity_id=str(report.id), after={"station": station.name, "crop": crop.name, "planting_date": payload.planting_date.isoformat(), "date_from": payload.date_from.isoformat(), "date_to": payload.date_to.isoformat(), "job_id": str(job.id)})
    await db.commit()
    return await _serialize(db, report)


@router.get("/{report_id}/public")
async def public_report(report_id: uuid.UUID, db: DbSession) -> dict:
    report = await db.get(Report, report_id)
    if not report: raise HTTPException(status_code=404, detail="Reporte no encontrado")
    return await _serialize(db, report)


@router.get("/{report_id}/download")
async def download_report(report_id: uuid.UUID, db: DbSession):
    report = await db.get(Report, report_id)
    file = await db.scalar(select(ReportFile).where(ReportFile.report_id == report_id).order_by(ReportFile.created_at.desc()).limit(1)) if report else None
    if not report or report.status != "COMPLETED" or not file:
        raise HTTPException(status_code=404, detail="El archivo todavía no está disponible")
    path = Path(file.storage_path)
    if not path.is_file(): raise HTTPException(status_code=410, detail="El archivo ya no se encuentra en almacenamiento")
    return FileResponse(path, media_type=file.mime_type, filename=file.filename)


@router.delete("/{report_id}")
async def delete_report(report_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    report = await db.get(Report, report_id)
    if not report: raise HTTPException(status_code=404, detail="Reporte no encontrado")
    files = (await db.scalars(select(ReportFile).where(ReportFile.report_id == report.id))).all()
    for file in files:
        Path(file.storage_path).unlink(missing_ok=True)
        await db.delete(file)
    await db.delete(report)
    await write_audit(db, action="REPORT_DELETE", user=actor, request=request, entity_type="report", entity_id=str(report_id))
    await db.commit(); return {"ok": True}
