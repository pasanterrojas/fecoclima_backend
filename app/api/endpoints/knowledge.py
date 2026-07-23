from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import delete, select

from app.api.deps import AdminUser, AnalystUser, DbSession
from app.core.config import settings
from app.db.models import Crop, KnowledgeChunk, KnowledgeDocument
from app.schemas.resources import KnowledgePayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict

router = APIRouter(prefix="/knowledge", tags=["Base de conocimiento"])
ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt"}
ALLOWED_MIME = {"application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain", "application/octet-stream"}


def _safe_filename(name: str) -> str:
    base = Path(name).name
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not clean or Path(clean).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Nombre o extensión de archivo no admitido")
    return clean


async def _doc_dict(db: DbSession, doc: KnowledgeDocument) -> dict:
    crop = await db.get(Crop, doc.crop_id) if doc.crop_id else None
    count = len((await db.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id))).all())
    return {**model_dict(doc, exclude={"source_text", "storage_path", "deleted_at"}), "crop_name": crop.name if crop else None, "chunks_count": count}


@router.get("/documents")
async def list_documents(db: DbSession, _: AnalystUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.deleted_at.is_(None)).order_by(KnowledgeDocument.created_at.desc()).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _doc_dict(db, row) for row in rows]}


@router.post("/documents")
async def create_document(payload: KnowledgePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if not payload.source_text:
        raise HTTPException(status_code=422, detail="Ingrese texto de referencia o use /knowledge/upload para adjuntar un archivo")
    doc = KnowledgeDocument(**payload.model_dump(), index_status="PENDING", created_by=actor.id)
    db.add(doc); await db.flush()
    job = await enqueue_job(db, "RAG_INDEX", {"document_id": str(doc.id)}, priority=60, created_by=actor.id)
    await write_audit(db, action="KNOWLEDGE_CREATE", user=actor, request=request, entity_type="knowledge_document", entity_id=str(doc.id), after={"title": doc.title, "job_id": str(job.id)})
    await db.commit()
    return await _doc_dict(db, doc)


@router.post("/upload")
async def upload_document(
    request: Request, db: DbSession, actor: AdminUser,
    file: UploadFile = File(...), title: str = Form(...), crop_id: str | None = Form(None),
    institution: str | None = Form(None), year: int | None = Form(None), version: str | None = Form(None),
) -> dict:
    filename = _safe_filename(file.filename or "")
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail="Tipo MIME no permitido")
    content = await file.read((settings.max_upload_mb * 1024 * 1024) + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"El archivo supera {settings.max_upload_mb} MB")
    checksum = hashlib.sha256(content).hexdigest()
    duplicate = await db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.checksum_sha256 == checksum, KnowledgeDocument.deleted_at.is_(None)))
    if duplicate:
        raise HTTPException(status_code=409, detail="Este archivo ya fue registrado")
    stored = settings.storage_path / "uploads" / f"{uuid.uuid4()}_{filename}"
    stored.write_bytes(content)
    parsed_crop_id = uuid.UUID(crop_id) if crop_id else None
    doc = KnowledgeDocument(title=title, crop_id=parsed_crop_id, institution=institution, year=year, version=version, filename=filename, mime_type=file.content_type, storage_path=str(stored), checksum_sha256=checksum, index_status="PENDING", active=True, created_by=actor.id)
    db.add(doc); await db.flush()
    job = await enqueue_job(db, "RAG_INDEX", {"document_id": str(doc.id)}, priority=60, created_by=actor.id)
    await write_audit(db, action="KNOWLEDGE_UPLOAD", user=actor, request=request, entity_type="knowledge_document", entity_id=str(doc.id), after={"title": title, "filename": filename, "job_id": str(job.id)})
    await db.commit()
    return await _doc_dict(db, doc)


@router.put("/documents/{document_id}")
async def update_document(document_id: uuid.UUID, payload: KnowledgePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    doc = await db.get(KnowledgeDocument, document_id)
    if not doc or doc.deleted_at:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    before = await _doc_dict(db, doc)
    for key, value in payload.model_dump().items(): setattr(doc, key, value)
    doc.index_status = "PENDING"
    await db.flush(); job = await enqueue_job(db, "RAG_INDEX", {"document_id": str(doc.id)}, priority=60, created_by=actor.id)
    after = await _doc_dict(db, doc)
    await write_audit(db, action="KNOWLEDGE_UPDATE", user=actor, request=request, entity_type="knowledge_document", entity_id=str(doc.id), before=before, after={**after, "job_id": str(job.id)})
    await db.commit(); return after


@router.post("/documents/{document_id}/reindex")
async def reindex_document(document_id: uuid.UUID, db: DbSession, actor: AdminUser) -> dict:
    doc = await db.get(KnowledgeDocument, document_id)
    if not doc or doc.deleted_at: raise HTTPException(status_code=404, detail="Documento no encontrado")
    doc.index_status = "PENDING"; doc.index_error = None
    job = await enqueue_job(db, "RAG_INDEX", {"document_id": str(doc.id)}, priority=40, created_by=actor.id)
    return {"job_id": str(job.id), "status": job.status}


@router.get("/documents/{document_id}/chunks")
async def document_chunks(document_id: uuid.UUID, db: DbSession, _: AnalystUser, limit: int = 50) -> dict:
    rows = (await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id).order_by(KnowledgeChunk.chunk_index).limit(min(limit, 200)))).all()
    return {"items": [model_dict(row, exclude={"embedding"}) for row in rows]}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    doc = await db.get(KnowledgeDocument, document_id)
    if not doc or doc.deleted_at: raise HTTPException(status_code=404, detail="Documento no encontrado")
    doc.active = False; doc.deleted_at = datetime.now(UTC)
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
    await write_audit(db, action="KNOWLEDGE_DELETE", user=actor, request=request, entity_type="knowledge_document", entity_id=str(doc.id))
    await db.commit(); return {"ok": True}
