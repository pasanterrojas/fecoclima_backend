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
from app.db.models import AIAgent, AgentKnowledgeDocument, Crop, KnowledgeChunk, KnowledgeDocument
from app.schemas.resources import KnowledgePayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_job
from app.services.serialization import model_dict
from app.services.crop_knowledge import sync_crop_knowledge

router = APIRouter(prefix="/knowledge", tags=["Base de conocimiento"])
ALLOWED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".txt", ".md"}
ALLOWED_MIME = {"application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/plain", "text/markdown", "text/x-markdown", "application/octet-stream"}


def _safe_filename(name: str) -> str:
    base = Path(name).name
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not clean or Path(clean).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Nombre o extensión de archivo no admitido")
    return clean


async def _doc_dict(db: DbSession, doc: KnowledgeDocument) -> dict:
    crop = await db.get(Crop, doc.crop_id) if doc.crop_id else None
    count = len((await db.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id))).all())
    linked_agent_ids = [
        str(value)
        for value in (
            await db.scalars(
                select(AgentKnowledgeDocument.agent_id).where(
                    AgentKnowledgeDocument.document_id == doc.id
                )
            )
        ).all()
    ]
    return {
        **model_dict(doc, exclude={"source_text", "storage_path", "deleted_at"}),
        "crop_name": crop.name if crop else None,
        "chunks_count": count,
        "linked_agent_ids": linked_agent_ids,
    }


@router.get("/documents")
async def list_documents(
    db: DbSession,
    _: AnalystUser,
    limit: int = 100,
    offset: int = 0,
    agent_id: uuid.UUID | None = None,
    crop_id: uuid.UUID | None = None,
) -> dict:
    query = select(KnowledgeDocument).where(KnowledgeDocument.deleted_at.is_(None))
    if crop_id:
        query = query.where(KnowledgeDocument.crop_id == crop_id)
    if agent_id:
        query = query.join(
            AgentKnowledgeDocument,
            AgentKnowledgeDocument.document_id == KnowledgeDocument.id,
        ).where(AgentKnowledgeDocument.agent_id == agent_id)
    rows = (
        await db.scalars(
            query.order_by(KnowledgeDocument.created_at.desc())
            .offset(offset)
            .limit(min(limit, 250))
        )
    ).all()
    return {"items": [await _doc_dict(db, row) for row in rows]}


@router.post("/documents")
async def create_document(payload: KnowledgePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if not payload.source_text:
        raise HTTPException(status_code=422, detail="Ingrese texto de referencia o use /knowledge/upload para adjuntar un archivo")
    doc = KnowledgeDocument(**payload.model_dump(), index_status="PENDING", created_by=actor.id)
    db.add(doc); await db.flush()
    if doc.crop_id: await sync_crop_knowledge(db, doc.crop_id)
    job = await enqueue_job(db, "RAG_INDEX", {"document_id": str(doc.id)}, priority=60, created_by=actor.id)
    await write_audit(db, action="KNOWLEDGE_CREATE", user=actor, request=request, entity_type="knowledge_document", entity_id=str(doc.id), after={"title": doc.title, "job_id": str(job.id)})
    await db.commit()
    return await _doc_dict(db, doc)


@router.post("/upload")
async def upload_document(
    request: Request, db: DbSession, actor: AdminUser,
    file: UploadFile = File(...), title: str = Form(...), crop_id: str | None = Form(None),
    agent_id: str | None = Form(None), institution: str | None = Form(None),
    year: int | None = Form(None), version: str | None = Form(None),
) -> dict:
    filename = _safe_filename(file.filename or "")
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail="Tipo MIME no permitido")
    content = await file.read((settings.max_upload_mb * 1024 * 1024) + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"El archivo supera {settings.max_upload_mb} MB")
    checksum = hashlib.sha256(content).hexdigest()
    try:
        parsed_agent_id = uuid.UUID(agent_id) if agent_id else None
        parsed_crop_id_input = uuid.UUID(crop_id) if crop_id else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Identificador de agente o cultivo inválido") from exc
    agent = await db.get(AIAgent, parsed_agent_id) if parsed_agent_id else None
    if parsed_agent_id and (not agent or agent.deleted_at):
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    if agent and parsed_crop_id_input and parsed_crop_id_input != agent.crop_id:
        raise HTTPException(
            status_code=422,
            detail="El cultivo del documento debe coincidir con el cultivo del agente",
        )
    parsed_crop_id = agent.crop_id if agent else parsed_crop_id_input
    if parsed_crop_id:
        crop = await db.get(Crop, parsed_crop_id)
        if not crop or crop.deleted_at: raise HTTPException(status_code=422, detail="Cultivo no disponible")
    duplicate = await db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.checksum_sha256 == checksum,
            KnowledgeDocument.crop_id == parsed_crop_id,
            KnowledgeDocument.deleted_at.is_(None),
        )
    )
    if duplicate:
        if parsed_crop_id and duplicate.crop_id == parsed_crop_id:
            await sync_crop_knowledge(db, parsed_crop_id)
            await db.commit()
            return await _doc_dict(db, duplicate)
        if not agent:
            raise HTTPException(status_code=409, detail="Este archivo ya fue registrado")
        if duplicate.crop_id is not None and duplicate.crop_id != agent.crop_id:
            raise HTTPException(
                status_code=409,
                detail="El archivo ya existe, pero pertenece a otro cultivo y no puede vincularse a este agente",
            )
        link = await db.scalar(
            select(AgentKnowledgeDocument).where(
                AgentKnowledgeDocument.agent_id == agent.id,
                AgentKnowledgeDocument.document_id == duplicate.id,
            )
        )
        if not link:
            db.add(AgentKnowledgeDocument(agent_id=agent.id, document_id=duplicate.id, created_by=actor.id))
            await db.commit()
        return await _doc_dict(db, duplicate)
    stored = settings.storage_path / "uploads" / f"{uuid.uuid4()}_{filename}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(content)
    doc = KnowledgeDocument(title=title, crop_id=parsed_crop_id, institution=institution, year=year, version=version, filename=filename, mime_type=file.content_type, storage_path=str(stored), checksum_sha256=checksum, index_status="PENDING", active=True, created_by=actor.id)
    db.add(doc); await db.flush()
    if parsed_crop_id: await sync_crop_knowledge(db, parsed_crop_id)
    if agent and not parsed_crop_id:
        db.add(AgentKnowledgeDocument(agent_id=agent.id, document_id=doc.id, created_by=actor.id))
    job = await enqueue_job(db, "RAG_INDEX", {"document_id": str(doc.id)}, priority=60, created_by=actor.id)
    await write_audit(db, action="KNOWLEDGE_UPLOAD", user=actor, request=request, entity_type="knowledge_document", entity_id=str(doc.id), after={"title": title, "filename": filename, "job_id": str(job.id)})
    await db.commit()
    return await _doc_dict(db, doc)


@router.post("/agents/{agent_id}/documents/{document_id}")
async def link_document_to_agent(
    agent_id: uuid.UUID,
    document_id: uuid.UUID,
    db: DbSession,
    actor: AdminUser,
) -> dict:
    agent = await db.get(AIAgent, agent_id)
    doc = await db.get(KnowledgeDocument, document_id)
    if not agent or agent.deleted_at:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    if not doc or doc.deleted_at:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if doc.crop_id is not None and doc.crop_id != agent.crop_id:
        raise HTTPException(
            status_code=422,
            detail="El documento pertenece a otro cultivo y no puede vincularse a este agente",
        )
    existing = await db.scalar(
        select(AgentKnowledgeDocument).where(
            AgentKnowledgeDocument.agent_id == agent.id,
            AgentKnowledgeDocument.document_id == doc.id,
        )
    )
    if not existing:
        db.add(AgentKnowledgeDocument(agent_id=agent.id, document_id=doc.id, created_by=actor.id))
        await db.commit()
    return await _doc_dict(db, doc)


@router.delete("/agents/{agent_id}/documents/{document_id}")
async def unlink_document_from_agent(
    agent_id: uuid.UUID,
    document_id: uuid.UUID,
    db: DbSession,
    _: AdminUser,
) -> dict:
    await db.execute(
        delete(AgentKnowledgeDocument).where(
            AgentKnowledgeDocument.agent_id == agent_id,
            AgentKnowledgeDocument.document_id == document_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.put("/documents/{document_id}")
async def update_document(document_id: uuid.UUID, payload: KnowledgePayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    doc = await db.get(KnowledgeDocument, document_id)
    if not doc or doc.deleted_at:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    before = await _doc_dict(db, doc)
    for key, value in payload.model_dump().items(): setattr(doc, key, value)
    if doc.crop_id: await sync_crop_knowledge(db, doc.crop_id)
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
    await db.commit()
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


@router.post("/crops/{crop_id}/sync")
async def sync_crop_documents(crop_id: uuid.UUID, db: DbSession, _: AdminUser) -> dict:
    crop = await db.get(Crop, crop_id)
    if not crop or crop.deleted_at: raise HTTPException(status_code=404, detail="Cultivo no encontrado")
    count = await sync_crop_knowledge(db, crop_id)
    await db.commit()
    return {"documents_count": count, "status": "SYNCED"}
