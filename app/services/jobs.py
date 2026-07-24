from __future__ import annotations

import os
import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import BackgroundJob, Report, Station, WeatherForecast
from app.services.backups import create_database_backup
from app.services.forecast import refresh_station_forecast
from app.services.rag import index_document
from app.services.reporting import generate_report_pdf
from app.services.producer_notifications import generate_daily_digest
from app.services.satellite import search_copernicus_catalog, sync_satellite_for_station
from app.services.weather import aggregate_range, evaluate_alert_for_day, sync_fecoclima_station

logger = structlog.get_logger(__name__)


async def enqueue_job(
    db: AsyncSession,
    job_type: str,
    payload: dict[str, Any],
    *,
    priority: int = 100,
    scheduled_at: datetime | None = None,
    max_attempts: int = 3,
    created_by=None,
) -> BackgroundJob:
    job = BackgroundJob(
        job_type=job_type.upper(),
        payload=payload,
        priority=priority,
        scheduled_at=scheduled_at or datetime.now(UTC),
        max_attempts=max_attempts,
        created_by=created_by,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def enqueue_unique(
    db: AsyncSession,
    job_type: str,
    payload: dict[str, Any],
    *,
    priority: int = 100,
) -> BackgroundJob | None:
    pending = await db.scalar(
        select(BackgroundJob).where(
            BackgroundJob.job_type == job_type.upper(),
            BackgroundJob.status.in_(["PENDING", "RUNNING"]),
            BackgroundJob.payload == payload,
        )
    )
    if pending:
        return None
    return await enqueue_job(db, job_type, payload, priority=priority)


async def release_stale_jobs(db: AsyncSession) -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.job_lock_timeout_minutes)
    result = await db.execute(
        update(BackgroundJob)
        .where(BackgroundJob.status == "RUNNING", BackgroundJob.locked_at < cutoff)
        .values(
            status="PENDING",
            next_attempt_at=datetime.now(UTC),
            locked_by=None,
            locked_at=None,
            error_message="El bloqueo del worker expiró; trabajo devuelto a la cola.",
        )
    )
    await db.commit()
    return int(result.rowcount or 0)


async def claim_job(db: AsyncSession, worker_name: str) -> BackgroundJob | None:
    now = datetime.now(UTC)
    async with db.begin():
        job = await db.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.status == "PENDING",
                BackgroundJob.scheduled_at <= now,
                or_(BackgroundJob.next_attempt_at.is_(None), BackgroundJob.next_attempt_at <= now),
            )
            .order_by(BackgroundJob.priority.asc(), BackgroundJob.scheduled_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status = "RUNNING"
        job.started_at = now
        job.finished_at = None
        job.locked_by = worker_name
        job.locked_at = now
        job.attempts += 1
    await db.refresh(job)
    return job


async def _refresh_forecast(db: AsyncSession, station_id) -> dict[str, Any]:
    station = await db.get(Station, station_id)
    if not station:
        raise ValueError("Estación no encontrada")
    return await refresh_station_forecast(db, station)


async def _evaluate_range(
    db: AsyncSession,
    station_id,
    date_from: date,
    date_to: date,
    crop_id=None,
    planting_date: date | None = None,
) -> dict[str, Any]:
    # Las alertas específicas requieren cultivo y fecha de siembra. La sincronización
    # meteorológica por sí sola no debe inventar un contexto fenológico.
    if not crop_id or not planting_date:
        return {"evaluations": 0, "reason": "crop_and_planting_date_required"}
    count = 0
    current = date_from
    while current <= date_to:
        await evaluate_alert_for_day(
            db, station_id, crop_id, current, planting_date=planting_date
        )
        count += 1
        current += timedelta(days=1)
    return {"evaluations": count}


async def _cleanup_files() -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=settings.report_retention_days)
    removed: dict[str, int] = {}
    for folder_name in ["uploads", "exports", "error-reports"]:
        count = 0
        folder = settings.storage_path / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        for file in folder.glob("*"):
            if file.is_file() and datetime.fromtimestamp(file.stat().st_mtime, UTC) < cutoff:
                file.unlink(missing_ok=True)
                count += 1
        removed[folder_name] = count
    return {"removed": removed}


async def handle_job(db: AsyncSession, job: BackgroundJob) -> dict[str, Any]:
    payload = job.payload or {}
    if job.job_type == "STATION_SYNC":
        date_from = date.fromisoformat(payload["date_from"])
        date_to = date.fromisoformat(payload["date_to"])
        sync_result = await sync_fecoclima_station(db, payload["station_id"], date_from, date_to)
        aggregate_count = await aggregate_range(db, payload["station_id"], date_from, date_to)
        alert_result = await _evaluate_range(
            db,
            payload["station_id"],
            date_from,
            date_to,
            crop_id=payload.get("crop_id"),
            planting_date=(date.fromisoformat(payload["planting_date"]) if payload.get("planting_date") else None),
        )
        return {**sync_result, "aggregated_days": aggregate_count, **alert_result}
    if job.job_type == "AGGREGATE_ALERTS":
        date_from = date.fromisoformat(payload["date_from"])
        date_to = date.fromisoformat(payload["date_to"])
        aggregate_count = await aggregate_range(db, payload["station_id"], date_from, date_to)
        alerts = await _evaluate_range(
            db,
            payload["station_id"],
            date_from,
            date_to,
            crop_id=payload.get("crop_id"),
            planting_date=(date.fromisoformat(payload["planting_date"]) if payload.get("planting_date") else None),
        )
        return {"aggregated_days": aggregate_count, **alerts}
    if job.job_type == "REPORT_GENERATE":
        report = await db.get(Report, payload["report_id"])
        if not report:
            raise ValueError("Reporte no encontrado")
        await aggregate_range(db, report.station_id, report.date_from, report.date_to)
        await _evaluate_range(
            db,
            report.station_id,
            report.date_from,
            report.date_to,
            crop_id=report.crop_id,
            planting_date=report.planting_date,
        )
        file = await generate_report_pdf(db, report.id)
        return {"report_file_id": str(file.id), "size_bytes": file.size_bytes}
    if job.job_type == "RAG_INDEX":
        chunks = await index_document(db, payload["document_id"])
        return {"chunks": chunks}
    if job.job_type == "FORECAST_REFRESH":
        return await _refresh_forecast(db, payload["station_id"])
    if job.job_type == "SATELLITE_SYNC":
        if payload.get("station_id"):
            return await sync_satellite_for_station(db, payload["station_id"], force=bool(payload.get("force", False)))
        return await search_copernicus_catalog(
            db,
            payload["plot_id"],
            date.fromisoformat(payload["date_from"]),
            date.fromisoformat(payload["date_to"]),
            float(payload.get("max_cloud_percentage", 40)),
        )
    if job.job_type == "DAILY_PRODUCER_DIGEST":
        return await generate_daily_digest(db, force=bool(payload.get("force")))
    if job.job_type == "DATABASE_BACKUP":
        row = await create_database_backup(db, job.created_by, payload.get("label"))
        if row.status != "COMPLETED":
            raise RuntimeError(row.error_message or "No se pudo generar el respaldo")
        return {
            "backup_id": str(row.id),
            "size_bytes": row.size_bytes,
            "storage_location": row.storage_location,
        }
    if job.job_type == "CLEANUP_FILES":
        return await _cleanup_files()
    raise ValueError(f"Tipo de trabajo no soportado: {job.job_type}")


async def complete_job(db: AsyncSession, job: BackgroundJob, result: dict[str, Any]) -> None:
    job.status = "COMPLETED"
    job.result = result
    job.finished_at = datetime.now(UTC)
    job.error_message = None
    job.locked_by = None
    job.locked_at = None
    await db.commit()


async def fail_job(db: AsyncSession, job: BackgroundJob, exc: Exception) -> None:
    job.error_message = str(exc)[:4000]
    job.finished_at = datetime.now(UTC)
    job.locked_by = None
    job.locked_at = None
    if job.attempts < job.max_attempts:
        job.status = "PENDING"
        job.next_attempt_at = datetime.now(UTC) + timedelta(minutes=min(30, 2**job.attempts))
    else:
        job.status = "FAILED"
        if job.job_type == "REPORT_GENERATE" and job.payload.get("report_id"):
            report = await db.get(Report, job.payload["report_id"])
            if report:
                report.status = "FAILED"
                report.error_message = job.error_message
    await db.commit()


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"
