"""Administrative reports reuse immutable parcel analyses and the existing PDF worker."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import String, func, select

from app.api.deps import AnalystUser, DbSession
from app.db.models import BackgroundJob, Crop, Plot, PlotAnalysis, PlotReport
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.technical_references import REFERENCE_REVISION, library

router = APIRouter(prefix="/admin/consultation-reports", tags=["Informes de consulta técnica"])


@router.get("/sources")
async def report_sources(_: AnalystUser, db: DbSession, crop_id: UUID | None = None) -> dict:
    crop = await db.get(Crop, crop_id) if crop_id else None
    if crop_id and (not crop or crop.deleted_at):
        raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    return {
        "revision": REFERENCE_REVISION,
        "items": [
            {
                "document": d["document"],
                "sha256": d["sha256"],
                "crop": d["crop"],
                "sections": len(d["sections"]),
            }
            for d in library()["documents"]
            if not crop or d["crop"] == crop.code.upper()
        ],
    }


@router.get("")
async def consultations(
    db: DbSession,
    _: AnalystUser,
    search: str = Query("", max_length=180),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    query = select(PlotAnalysis, Plot).join(Plot, Plot.id == PlotAnalysis.plot_id)
    if search.strip():
        match = "%" + search.strip() + "%"
        query = query.where(Plot.name.ilike(match) | Plot.producer_name.ilike(match))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.execute(
            query.order_by(PlotAnalysis.reference_at.desc(), PlotAnalysis.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    reports = (
        list(
            (
                await db.scalars(
                    select(PlotReport)
                    .where(
                        select(BackgroundJob.id)
                        .where(
                            BackgroundJob.job_type == "PLOT_REPORT",
                            BackgroundJob.payload["report_id"].as_string() == func.cast(PlotReport.id, String),
                            BackgroundJob.payload["client_model"].as_boolean().is_(True),
                        )
                        .exists()
                    )
                    .where(PlotReport.analysis_id.in_([a.id for a, p in rows]))
                    .order_by(PlotReport.created_at.desc())
                )
            ).all()
        )
        if rows
        else []
    )
    latest = {}
    for report in reports:
        latest.setdefault(report.analysis_id, report)
    return {
        "total": total or 0,
        "items": [
            {
                "analysis_id": str(a.id),
                "plot_id": str(p.id),
                "name": a.context.get("plot", {}).get("name") or p.name,
                "producer_name": p.producer_name,
                "reference_at": a.reference_at.isoformat(),
                "crop_name": a.context.get("crop", {}).get("name"),
                "window_hours": a.context.get("selected_window_hours", 72),
                "stage_name": a.context.get("phenology", {}).get("stage_name"),
                "water_available": bool(a.context.get("water_balance")),
                "report": _serialize(latest[a.id]) if a.id in latest else None,
            }
            for a, p in rows
        ],
    }


def _serialize(row):
    return {
        "id": str(row.id),
        "analysis_id": str(row.analysis_id),
        "status": row.status,
        "error": row.error,
        "file_available": row.status == "COMPLETED" and bool(row.storage_path),
    }


@router.post(
    "/{analysis_id}",
    description="Genera el modelo de consulta desde una captura existente; no recalcula ni mezcla períodos. Requiere sesión administrativa y CSRF.",
)
async def generate(analysis_id: UUID, request: Request, db: DbSession, actor: AnalystUser) -> dict:
    analysis = await db.get(PlotAnalysis, analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    # Serialize repeated requests from this screen; avoid duplicate jobs on double click.
    await db.scalar(select(PlotAnalysis.id).where(PlotAnalysis.id == analysis_id).with_for_update())
    pending = list(
        (
            await db.scalars(
                select(PlotReport)
                .where(PlotReport.analysis_id == analysis_id, PlotReport.status.in_(["PENDING", "RUNNING"]))
                .order_by(PlotReport.created_at.desc())
            )
        ).all()
    )
    for existing in pending:
        client_job = await db.scalar(
            select(BackgroundJob.id)
            .where(
                BackgroundJob.job_type == "PLOT_REPORT",
                BackgroundJob.payload["report_id"].as_string() == str(existing.id),
                BackgroundJob.payload["client_model"].as_boolean().is_(True),
            )
            .limit(1)
        )
        if client_job:
            return _serialize(existing)
    row = PlotReport(analysis_id=analysis_id, created_by=actor.id, status="PENDING")
    db.add(row)
    await db.flush()
    await enqueue_job(
        db, "PLOT_REPORT", {"report_id": str(row.id), "client_model": True}, priority=50, created_by=actor.id
    )
    await write_audit(
        db,
        action="CONSULTATION_REPORT_REQUEST",
        user=actor,
        request=request,
        entity_type="plot_report",
        entity_id=str(row.id),
        after={"analysis_id": str(analysis_id), "reference_revision": REFERENCE_REVISION},
    )
    await db.commit()
    return _serialize(row)


@router.get("/files/{report_id}")
async def report_file(report_id: UUID, db: DbSession, _: AnalystUser):
    row = await db.get(PlotReport, report_id)
    if not row or row.status != "COMPLETED" or not row.storage_path:
        raise HTTPException(status_code=404, detail="El informe todavía no está disponible")
    path = Path(row.storage_path)
    if not path.is_file():
        raise HTTPException(
            status_code=410, detail="El archivo ya no está en almacenamiento; genere otro informe"
        )
    return FileResponse(path, media_type="application/pdf", filename=f"consulta-tecnica-{row.id}.pdf")
