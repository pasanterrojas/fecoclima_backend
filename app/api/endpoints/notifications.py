from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession
from app.core.crypto import encrypt_secret, mask_secret, decrypt_secret
from app.db.models import Crop, IntegrationLog, Station
from app.schemas.actions import ProducerNotificationSettingsPayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_unique
from app.services.producer_notifications import (
    CONFIG_NAME,
    default_config,
    generate_daily_digest,
    get_notification_integration,
    telegram_request,
)
from app.services.serialization import model_dict

router = APIRouter(prefix="/notifications", tags=["Avisos a productores"])


def _settings(row) -> dict:
    config = {**default_config(), **(row.non_sensitive_config or {})}
    return {
        **config,
        "id": str(row.id),
        "has_telegram_token": bool(row.encrypted_secret),
        "telegram_token_masked": mask_secret(decrypt_secret(row.encrypted_secret)),
        "last_run_at": row.last_run_at,
        "last_result": row.last_result,
    }


@router.get("/settings")
async def get_settings(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db, create=True)
    await db.commit()
    stations = list((await db.scalars(select(Station).where(Station.active.is_(True), Station.deleted_at.is_(None)).order_by(Station.name))).all())
    crops = list((await db.scalars(select(Crop).where(Crop.active.is_(True), Crop.deleted_at.is_(None)).order_by(Crop.name))).all())
    return {
        "settings": _settings(row),
        "stations": [{"id": str(item.id), "name": item.name} for item in stations],
        "crops": [{"id": str(item.id), "code": item.code, "name": item.name} for item in crops],
    }


@router.put("/settings")
async def update_settings(payload: ProducerNotificationSettingsPayload, request: Request, db: DbSession, actor: AdminUser) -> dict:
    row = await get_notification_integration(db, create=True)
    before = _settings(row)
    data = payload.model_dump(exclude={"telegram_bot_token"}, mode="json")
    previous = {**default_config(), **(row.non_sensitive_config or {})}
    data["last_delivery_date"] = previous.get("last_delivery_date")
    row.non_sensitive_config = data
    row.active = payload.active
    row.sync_frequency_minutes = 1440
    if payload.telegram_bot_token:
        row.encrypted_secret = encrypt_secret(payload.telegram_bot_token)
        row.secret_hint = payload.telegram_bot_token[-4:]
    await write_audit(db, action="PRODUCER_NOTIFICATIONS_UPDATE", user=actor, request=request, entity_type="integration", entity_id=str(row.id), before=before, after=_settings(row))
    await db.commit()
    return _settings(row)


@router.post("/run-now")
async def run_now(db: DbSession, _: AdminUser) -> dict:
    job = await enqueue_unique(db, "DAILY_PRODUCER_DIGEST", {"force": True, "requested_at": datetime.now(UTC).replace(second=0, microsecond=0).isoformat()}, priority=15)
    return {"queued": bool(job), "job_id": str(job.id) if job else None}


@router.post("/test-email")
async def test_email(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    if not row:
        raise HTTPException(status_code=409, detail="Configure primero los avisos")
    config = {**default_config(), **(row.non_sensitive_config or {})}
    if not config.get("email_enabled"):
        raise HTTPException(status_code=409, detail="Active el canal de correo")
    # La misma canalización genera un análisis real y evita una prueba artificial.
    try:
        result = await generate_daily_digest(
            db,
            force=True,
            only_channels={"email"},
            mark_delivered=False,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc
    return result


@router.post("/test-telegram")
async def test_telegram(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    if not row:
        raise HTTPException(status_code=409, detail="Configure primero Telegram")
    token = decrypt_secret(row.encrypted_secret)
    config = {**default_config(), **(row.non_sensitive_config or {})}
    if not token or not config.get("telegram_chat_id"):
        raise HTTPException(status_code=409, detail="Falta token o chat_id de Telegram")
    try:
        bot = await telegram_request(token, "getMe")
        await telegram_request(token, "sendMessage", {
            "chat_id": str(config["telegram_chat_id"]),
            "text": "✅ FECOCLIMA IA: integración de Telegram funcionando.",
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc
    return {"ok": True, "bot": bot.get("result", {}).get("username")}


@router.post("/telegram/chats")
async def detect_telegram_chats(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    token = decrypt_secret(row.encrypted_secret) if row else None
    if not token:
        raise HTTPException(status_code=409, detail="Guarde primero el token del bot")
    try:
        result = await telegram_request(token, "getUpdates", {"limit": 100, "timeout": 0})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc
    chats = {}
    for update in result.get("result", []):
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
            or update.get("my_chat_member")
            or {}
        )
        chat = message.get("chat") if isinstance(message, dict) else None
        if not chat:
            continue
        chats[str(chat.get("id"))] = {
            "id": str(chat.get("id")),
            "title": chat.get("title") or chat.get("username") or chat.get("first_name") or "Chat",
            "type": chat.get("type"),
        }
    return {"items": list(chats.values())}


@router.get("/logs")
async def notification_logs(db: DbSession, _: AdminUser, limit: int = 50) -> dict:
    row = await get_notification_integration(db)
    if not row:
        return {"items": []}
    rows = list((await db.scalars(select(IntegrationLog).where(IntegrationLog.integration_id == row.id).order_by(IntegrationLog.created_at.desc()).limit(min(limit, 100)))).all())
    return {"items": [model_dict(item) for item in rows]}
