from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Iterable

import structlog
from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentKnowledgeDocument, KnowledgeChunk, KnowledgeDocument
from app.services.ai import create_embedding

logger = structlog.get_logger(__name__)


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_file_text(content: bytes, mime_type: str, filename: str) -> list[tuple[str, str]]:
    suffix = Path(filename).suffix.lower()
    if mime_type == "application/pdf" or suffix == ".pdf":
        reader = PdfReader(BytesIO(content))
        return [(page.extract_text() or "", f"página {index + 1}") for index, page in enumerate(reader.pages)]
    if mime_type in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"} or suffix == ".docx":
        doc = DocxDocument(BytesIO(content))
        return [("\n".join(paragraph.text for paragraph in doc.paragraphs), "documento")]
    if mime_type.startswith("text/") or suffix == ".txt":
        return [(content.decode("utf-8", errors="replace"), "texto")]
    raise ValueError("Tipo de archivo no admitido")


def chunk_sections(sections: Iterable[tuple[str, str]], chunk_size: int = 1200, overlap: int = 180) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    for raw_text, source in sections:
        text = clean_text(raw_text)
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            if end < len(text):
                boundary = text.rfind(" ", start, end)
                if boundary > start + chunk_size // 2:
                    end = boundary
            chunk = text[start:end].strip()
            if chunk:
                chunks.append((chunk, source))
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return chunks


async def _safe_embedding(text: str) -> list[float] | None:
    """Treat the embedding provider as optional; lexical RAG must remain usable."""
    try:
        return await create_embedding(text)
    except Exception as exc:
        logger.warning("rag_embedding_unavailable", error=str(exc)[:1000])
        return None


async def index_document(db: AsyncSession, document_id) -> int:
    document = await db.get(KnowledgeDocument, document_id)
    if not document:
        raise ValueError("Documento no encontrado")
    if document.source_text:
        sections = [(document.source_text, "contenido registrado")]
    elif document.storage_path:
        path = Path(document.storage_path)
        sections = extract_file_text(path.read_bytes(), document.mime_type or "application/octet-stream", document.filename or path.name)
    else:
        raise ValueError("El documento no contiene texto ni archivo")
    chunks = chunk_sections(sections)
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    for index, (content, source) in enumerate(chunks):
        embedding = await _safe_embedding(content)
        db.add(
            KnowledgeChunk(
                document_id=document.id,
                crop_id=document.crop_id,
                chunk_index=index,
                content=content,
                page_or_section=source,
                token_count=max(1, len(content) // 4),
                embedding=embedding,
            )
        )
    document.index_status = "INDEXED"
    document.index_error = None
    from datetime import UTC, datetime
    document.indexed_at = datetime.now(UTC)
    await db.commit()
    return len(chunks)


def _base_query(crop_id=None, agent_id=None):
    base = (
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(
            KnowledgeDocument.active.is_(True),
            KnowledgeDocument.deleted_at.is_(None),
            KnowledgeDocument.index_status == "INDEXED",
        )
    )
    return base


async def _scoped_query(db: AsyncSession, *, crop_id=None, agent_id=None):
    base = _base_query(crop_id, agent_id)
    if agent_id:
        # Agent knowledge is an explicit allow-list. Migration 0004 links all
        # pre-existing crop documents, so a newly created agent with no links
        # must not inherit documents assigned to another agent of the crop.
        linked_ids = select(AgentKnowledgeDocument.document_id).where(
            AgentKnowledgeDocument.agent_id == agent_id
        )
        base = base.where(KnowledgeDocument.id.in_(linked_ids))
    elif crop_id:
        base = base.where(or_(KnowledgeChunk.crop_id == crop_id, KnowledgeChunk.crop_id.is_(None)))
    return base


async def retrieve_chunks(
    db: AsyncSession,
    query: str,
    crop_id=None,
    limit: int = 6,
    *,
    agent_id=None,
) -> list[dict]:
    """Retrieve agent-scoped evidence with vector search and lexical fallback.

    Neither an embedding outage nor chunks indexed without vectors may prevent
    the crop agent from answering with weather/rules and available documents.
    """
    embedding = await _safe_embedding(query)
    rows = []
    if embedding:
        vector_query = await _scoped_query(db, crop_id=crop_id, agent_id=agent_id)
        vector_query = (
            vector_query.where(KnowledgeChunk.embedding.is_not(None))
            .order_by(KnowledgeChunk.embedding.cosine_distance(embedding))
            .limit(limit)
        )
        try:
            # Isolate a possible pgvector/dimension error in a savepoint so the
            # alert and context already loaded in the outer transaction remain usable.
            async with db.begin_nested():
                rows = (await db.execute(vector_query)).all()
        except Exception as exc:
            # A stale vector dimension or unavailable pgvector operation must
            # degrade to textual retrieval instead of breaking public analysis.
            logger.warning("rag_vector_query_failed", error=str(exc)[:1000])
            rows = []

    if not rows:
        lexical_query = await _scoped_query(db, crop_id=crop_id, agent_id=agent_id)
        terms = [term for term in re.findall(r"[\wáéíóúñ]{4,}", query.lower())[:10]]
        if terms:
            lexical_query = lexical_query.where(
                or_(*(KnowledgeChunk.content.ilike(f"%{term}%") for term in terms))
            )
        lexical_query = lexical_query.order_by(KnowledgeChunk.created_at.desc()).limit(limit)
        rows = (await db.execute(lexical_query)).all()

    return [
        {
            "content": chunk.content,
            "document": document.title,
            "document_id": str(document.id),
            "page_or_section": chunk.page_or_section or "sin sección",
            "institution": document.institution,
            "year": document.year,
        }
        for chunk, document in rows
    ]
