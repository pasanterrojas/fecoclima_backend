from __future__ import annotations

import base64
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.api.deps import DbSession
from app.schemas.actions import AgentAnalysisPayload, PhenologyAgentPayload
from app.services.agent_runtime import chat_with_agent_for_alert, run_agent_for_alert
from app.services.phenology_agent import analyze_phenology_environment

router = APIRouter(prefix="/public/agent", tags=["Agente público por cultivo"])

_MAX_IMAGE_BYTES = 8 * 1024 * 1024
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
    for item in raw_history[-8:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": content[:4000]})

    image_data_url = None
    if image is not None:
        content_type = (image.content_type or "").lower()
        if content_type not in _ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=415, detail="Solo se admiten imágenes JPG, PNG o WebP")
        content = await image.read(_MAX_IMAGE_BYTES + 1)
        if len(content) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="La imagen supera el límite de 8 MB")
        if not _valid_image_signature(content_type, content):
            raise HTTPException(status_code=422, detail="El archivo no coincide con un formato de imagen admitido")
        image_data_url = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"

    try:
        return await chat_with_agent_for_alert(
            db,
            alert_id,
            question=question,
            period=period,
            history=history,
            image_data_url=image_data_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc
