"""Durable private chat attachments; only validated local images are reattached."""

from __future__ import annotations

import base64
from pathlib import Path
from io import BytesIO
import re
import httpx
from typing import Any
from uuid import UUID, uuid4

from app.core.config import settings


def store_images(uploads: list[dict[str, Any]], analysis_id: UUID, user_id: UUID, browser_id: UUID) -> list[dict[str, str]]:
    if not uploads:
        return []
    folder = settings.storage_path / "plot-chat" / str(analysis_id) / str(user_id) / str(browser_id)
    folder.mkdir(parents=True, exist_ok=True)
    stored = []
    for image in uploads:
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[image["content_type"]]
        path = folder / f"{uuid4()}{suffix}"
        path.write_bytes(image["content"])
        stored.append(
            {"filename": image["filename"], "content_type": image["content_type"], "storage_path": str(path)}
        )
    return stored


def recent_image_urls(messages: list[dict], attachments: list[dict]) -> list[str]:
    all_images = [image for message in messages for image in message.get("images", [])] + attachments
    root = (settings.storage_path / "plot-chat").resolve()
    urls, total = [], 0
    for image in reversed(all_images):
        path = Path(image.get("storage_path", "")).resolve()
        content_type = image.get("content_type")
        if (
            root not in path.parents
            or not path.is_file()
            or content_type not in {"image/jpeg", "image/png", "image/webp"}
        ):
            continue
        if path.stat().st_size > 8 * 1024 * 1024:
            continue
        content = path.read_bytes()
        if total + len(content) > 16 * 1024 * 1024:
            continue
        urls.append(f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}")
        total += len(content)
        if len(urls) == 4:
            break
    return list(reversed(urls))



ALLOWED_ATTACHMENTS = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def store_attachments(uploads: list[dict[str, Any]], analysis_id: UUID, user_id: UUID, browser_id: UUID) -> list[dict[str, Any]]:
    if not uploads:
        return []
    folder = settings.storage_path / "plot-chat" / str(analysis_id) / str(user_id) / str(browser_id)
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for upload in uploads:
        media_type = upload["content_type"].split(";", 1)[0].lower()
        suffix = ALLOWED_ATTACHMENTS[media_type]
        content = upload["content"]
        if media_type == "application/pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("El archivo no parece un PDF válido")
        if media_type.endswith("wordprocessingml.document") and not content.startswith(b"PK"):
            raise ValueError("El documento Word no es válido")
        if media_type == "text/plain":
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("El archivo de texto debe estar en UTF-8") from exc
        path = folder / f"{uuid4()}{suffix}"
        path.write_bytes(content)
        results.append({
            "filename": str(upload.get("filename") or "Adjunto")[:180],
            "content_type": media_type, "size": len(content), "storage_path": str(path),
        })
    return results


def attachment_text(files: list[dict[str, Any]]) -> str:
    """Limit parsed document content before sending it to an LLM."""
    parts = []
    for item in files:
        kind = item["content_type"]
        path = Path(item["storage_path"])
        try:
            if kind == "text/plain":
                content = path.read_text(encoding="utf-8")[:7000]
            elif kind == "application/pdf":
                from pypdf import PdfReader
                pdf = PdfReader(str(path), strict=False)
                content = "\n".join((page.extract_text() or "")[:1500] for page in pdf.pages[:5])[:7000]
            elif kind.endswith("wordprocessingml.document"):
                from docx import Document
                content = "\n".join(p.text for p in Document(str(path)).paragraphs)[:7000]
            else:
                continue
            if content.strip():
                parts.append(f"Documento adjunto: {item['filename']}\n{content}")
        except Exception:
            continue
    return "\n\n".join(parts)[:12000]


async def transcribe_audio(files: list[dict[str, Any]]) -> str | None:
    """Optional OpenAI-compatible transcription. Never pretend an audio was heard."""
    voice = next((x for x in files if x["content_type"].startswith("audio/")), None)
    if not voice or not settings.ai_api_key or not settings.ai_base_url:
        return None
    model = getattr(settings, "ai_transcription_model", "whisper-1")
    url = settings.ai_base_url.rstrip("/") + "/audio/transcriptions"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(voice["storage_path"], "rb") as source:
                response = await client.post(url, headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                                             files={"file": (Path(voice["storage_path"]).name, source, voice["content_type"])},
                                             data={"model": model, "language": "es"})
            response.raise_for_status()
            return str(response.json().get("text") or "").strip()[:5000] or None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def _safe_attachment(item: dict, analysis_id: str) -> dict:
    sanitized = {key: value for key, value in item.items() if key != "storage_path"}
    stored = item.get("storage_path")
    if stored:
        filename = Path(stored).name
        if re.fullmatch(r"[a-f0-9-]{36}\.(?:jpg|png|webp|pdf|txt|docx|webm|ogg|m4a|mp3|wav)", filename):
            sanitized["url"] = f"/parcels/analyses/{analysis_id}/chat/media/{filename}"
    return sanitized


def public_messages(messages: list[dict], analysis_id: str = "") -> list[dict]:
    return [
        {
            **message,
            "images": [
                _safe_attachment(image, analysis_id)
                for image in message.get("images", [])
            ],
            "attachments": [_safe_attachment(file, analysis_id) for file in message.get("attachments", [])],
        }
        for message in messages
    ]
