from __future__ import annotations

import base64
import json
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile

from app.api.deps import DbSession
from app.schemas.actions import AgentAnalysisPayload, PhenologyAgentPayload
from app.services.agent_runtime import chat_with_agent_for_alert, run_agent_for_alert
from app.services.chat_report import generate_chat_report_pdf
from app.services.phenology_agent import analyze_phenology_environment

router = APIRouter(prefix="/public/agent", tags=["Agente público por cultivo"])

_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_CHAT_IMAGES = 4
_MAX_REPORT_IMAGES = 8
_MAX_CHAT_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_REPORT_IMAGE_BYTES = 32 * 1024 * 1024
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_rate_windows: dict[str, deque[datetime]] = defaultdict(deque)


def _check_rate_limit(request: Request, *, limit: int = 12, window_seconds: int = 60) -> None:
    key = request.client.host if request.client else "unknown"
    now = datetime.now(UTC)
    window = _rate_windows[key]
    cutoff = now - timedelta(seconds=window_seconds)
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(status_code=429, detail="Demasiadas consultas al agente. Espere un momento.")
    window.append(now)


def _valid_image_signature(content_type: str, content: bytes) -> bool:
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


async def _read_image_uploads(
    uploads: list[UploadFile],
    *,
    max_count: int,
    max_total_bytes: int,
) -> list[dict[str, Any]]:
    if len(uploads) > max_count:
        raise HTTPException(
            status_code=413,
            detail=f"Se admiten como máximo {max_count} imágenes",
        )
    total = 0
    result: list[dict[str, Any]] = []
    for index, upload in enumerate(uploads, start=1):
        content_type = (upload.content_type or "").lower()
        if content_type not in _ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=415, detail="Solo se admiten imágenes JPG, PNG o WebP")
        content = await upload.read(_MAX_IMAGE_BYTES + 1)
        await upload.close()
        if len(content) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Cada imagen puede tener como máximo 8 MB")
        total += len(content)
        if total > max_total_bytes:
            raise HTTPException(status_code=413, detail="El conjunto de imágenes supera el límite permitido")
        if not _valid_image_signature(content_type, content):
            raise HTTPException(status_code=422, detail="Un archivo no coincide con un formato de imagen admitido")
        raw_name = str(upload.filename or f"imagen-{index}").replace("\r", " ").replace("\n", " ")
        result.append(
            {
                "filename": raw_name[-160:],
                "content_type": content_type,
                "content": content,
            }
        )
    return result


@router.post("/analyze")
async def analyze_with_crop_agent(
    payload: AgentAnalysisPayload,
    request: Request,
    db: DbSession,
) -> dict[str, Any]:
    _check_rate_limit(request, limit=8, window_seconds=60)
    result = await run_agent_for_alert(
        db,
        payload.alert_id,
        period=payload.period,
        force=payload.force,
    )
    return result


@router.post("/satellite")
async def analyze_with_satellite_agent(
    payload: AgentAnalysisPayload,
    request: Request,
    db: DbSession,
) -> dict[str, Any]:
    _check_rate_limit(request, limit=8, window_seconds=60)
    result = await run_agent_for_alert(
        db,
        payload.alert_id,
        period=payload.period,
        purpose="SATELLITE_ANALYSIS",
        force=payload.force,
    )
    return result


@router.post("/year-comparison")
async def analyze_year_comparison(
    payload: AgentAnalysisPayload,
    request: Request,
    db: DbSession,
) -> dict[str, Any]:
    _check_rate_limit(request, limit=8, window_seconds=60)
    return await run_agent_for_alert(
        db,
        payload.alert_id,
        period=payload.period,
        purpose="YEAR_COMPARISON",
        force=payload.force,
    )


@router.post("/phenology")
async def analyze_phenology_with_agent(
    payload: PhenologyAgentPayload,
    request: Request,
    db: DbSession,
) -> dict[str, Any]:
    _check_rate_limit(request, limit=6, window_seconds=60)
    try:
        return await analyze_phenology_environment(
            db,
            payload.station_id,
            payload.crop_code,
            payload.planting_date,
            force=payload.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/chat")
async def chat_with_crop_agent(
    request: Request,
    db: DbSession,
    alert_id: UUID = Form(...),
    period: str = Form("72h"),
    message: str = Form(...),
    history_json: str = Form("[]"),
    image: UploadFile | None = File(default=None),
    images: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    _check_rate_limit(request, limit=12, window_seconds=60)
    if period not in {"24h", "72h", "7d", "30d"}:
        raise HTTPException(status_code=422, detail="Periodo de contexto inválido")
    question = message.strip()
    if not question or len(question) > 2500:
        raise HTTPException(status_code=422, detail="La consulta debe tener entre 1 y 2500 caracteres")

    try:
        raw_history = json.loads(history_json or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="El historial de conversación no es JSON válido") from exc
    if not isinstance(raw_history, list):
        raise HTTPException(status_code=422, detail="El historial debe ser una lista")
    history: list[dict[str, str]] = []
    for item in raw_history[-12:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": content[:4000]})

    uploads = ([image] if image is not None else []) + list(images or [])
    validated_images = await _read_image_uploads(
        uploads,
        max_count=_MAX_CHAT_IMAGES,
        max_total_bytes=_MAX_CHAT_IMAGE_BYTES,
    )
    image_data_urls = [
        f"data:{item['content_type']};base64,{base64.b64encode(item['content']).decode('ascii')}"
        for item in validated_images
    ]

    try:
        return await chat_with_agent_for_alert(
            db,
            alert_id,
            question=question,
            period=period,
            history=history,
            image_data_urls=image_data_urls,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc


@router.post("/chat/report")
async def create_contextual_chat_report(
    request: Request,
    db: DbSession,
    alert_id: UUID = Form(...),
    period: str = Form("72h"),
    conversation_json: str = Form(...),
    images: list[UploadFile] | None = File(default=None),
) -> Response:
    _check_rate_limit(request, limit=4, window_seconds=60)
    if period not in {"24h", "72h", "7d", "30d"}:
        raise HTTPException(status_code=422, detail="Periodo de contexto inválido")
    try:
        raw_conversation = json.loads(conversation_json or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="La conversación no es JSON válido") from exc
    if not isinstance(raw_conversation, list) or not raw_conversation:
        raise HTTPException(status_code=422, detail="La conversación está vacía")

    conversation: list[dict[str, Any]] = []
    total_characters = 0
    for item in raw_conversation[-40:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        content = content[:8000]
        total_characters += len(content)
        if total_characters > 120_000:
            raise HTTPException(status_code=413, detail="La conversación es demasiado extensa para un reporte")
        image_names = [str(value)[:160] for value in (item.get("image_names") or [])[:4]]
        conversation.append({"role": role, "content": content, "image_names": image_names})
    if not conversation:
        raise HTTPException(status_code=422, detail="La conversación no contiene mensajes utilizables")

    validated_images = await _read_image_uploads(
        list(images or []),
        max_count=_MAX_REPORT_IMAGES,
        max_total_bytes=_MAX_REPORT_IMAGE_BYTES,
    )
    try:
        pdf = await generate_chat_report_pdf(
            db,
            alert_id,
            period=period,
            conversation=conversation,
            uploaded_images=validated_images,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo generar el reporte: {str(exc)[:1000]}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="reporte-consulta-fecoclima.pdf"',
            "X-Content-Type-Options": "nosniff",
        },
    )
