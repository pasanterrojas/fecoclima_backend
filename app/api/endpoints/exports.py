from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.api.deps import AnalystUser, DbSession
from app.services.exports import export_readings

router = APIRouter(prefix="/exports", tags=["Exportación"])


@router.get("/stations/{station_id}/readings")
async def export_station_readings(
    station_id: UUID,
    date_from: date,
    date_to: date,
    db: DbSession,
    _: AnalystUser,
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
):
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="date_to no puede ser anterior a date_from")
    if (date_to - date_from).days > 3660:
        raise HTTPException(status_code=422, detail="El rango no puede superar 10 años")
    try:
        result = await export_readings(db, station_id, date_from, date_to, format)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = Path(result["path"])
    return FileResponse(path, media_type=result["mime_type"], filename=result["filename"], headers={"X-Checksum-SHA256": result["checksum_sha256"]})
