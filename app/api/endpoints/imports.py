from __future__ import annotations

import json
import re
import uuid
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import AnalystUser, DbSession
from app.core.config import settings
from app.db.models import DataImport, DataImportError, Station
from app.services.imports import ALLOWED_MIME, import_weather_file, preview_tabular
from app.services.serialization import model_dict

router = APIRouter(prefix="/imports", tags=["Importación de datos"])


def _validate_file(file: UploadFile, content: bytes) -> str:
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", (file.filename or "datos").split("/")[-1])
    if not filename.lower().endswith((".xlsx", ".csv")):
        raise HTTPException(status_code=400, detail="Solo se admiten archivos XLSX o CSV")
    if file.content_type not in ALLOWED_MIME and file.content_type != "application/octet-stream":
        raise HTTPException(status_code=415, detail="Tipo MIME no permitido")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"El archivo supera {settings.max_upload_mb} MB")
    return filename


@router.post("/preview")
async def preview(_: AnalystUser, file: UploadFile = File(...)) -> dict:
    content = await file.read((settings.max_upload_mb * 1024 * 1024) + 1)
    filename = _validate_file(file, content)
    try: return preview_tabular(content, filename)
    except Exception as exc: raise HTTPException(status_code=422, detail=f"No se pudo leer el archivo: {exc}") from exc


@router.post("/weather")
async def import_weather(db: DbSession, actor: AnalystUser, station_id: uuid.UUID = Form(...), mapping: str = Form(...), file: UploadFile = File(...)) -> dict:
    station = await db.get(Station, station_id)
    if not station: raise HTTPException(status_code=404, detail="Estación no encontrada")
    content = await file.read((settings.max_upload_mb * 1024 * 1024) + 1)
    filename = _validate_file(file, content)
    try: parsed_mapping = json.loads(mapping)
    except json.JSONDecodeError as exc: raise HTTPException(status_code=422, detail="El mapeo debe ser JSON válido") from exc
    try: return await import_weather_file(db, station, content, filename, parsed_mapping, actor.id)
    except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
async def list_imports(db: DbSession, _: AnalystUser, limit: int = 100) -> dict:
    rows = (await db.scalars(select(DataImport).order_by(DataImport.created_at.desc()).limit(min(limit, 250)))).all()
    return {"items": [model_dict(row) for row in rows]}


@router.get("/{import_id}")
async def import_detail(import_id: uuid.UUID, db: DbSession, _: AnalystUser) -> dict:
    row = await db.get(DataImport, import_id)
    if not row:
        raise HTTPException(status_code=404, detail="Importación no encontrada")
    errors = (await db.scalars(select(DataImportError).where(DataImportError.import_id == row.id).order_by(DataImportError.row_number).limit(500))).all()
    return {**model_dict(row), "errors": [model_dict(item) for item in errors]}


@router.get("/{import_id}/errors/download")
async def download_import_errors(import_id: uuid.UUID, db: DbSession, _: AnalystUser):
    from pathlib import Path
    row = await db.get(DataImport, import_id)
    if not row or not row.error_report_path:
        raise HTTPException(status_code=404, detail="Reporte de errores no disponible")
    path = Path(row.error_report_path)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="El archivo de errores ya no existe")
    return FileResponse(path, media_type="text/csv", filename=path.name)
