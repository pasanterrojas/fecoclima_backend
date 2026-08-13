from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import case, delete, select, update

from app.api.deps import AdminUser, DbSession
from app.core.crypto import encrypt_secret, mask_secret
from app.db.models import Crop, IntegrationConfig, IntegrationLog, Station, TelegramSubscriber
from app.schemas.actions import ProducerNotificationSettingsPayload
from app.services.audit import write_audit
from app.services.jobs import enqueue_unique
from app.services.producer_notifications import (
    default_config,
    generate_daily_digest,
    get_notification_integration,
    send_telegram_to_subscribers,
    sync_telegram_subscriptions,
    telegram_bot_token,
    telegram_request,
    telegram_subscriber_payload,
)
from app.services.serialization import model_dict

router = APIRouter(prefix="/notifications", tags=["Avisos a productores"])


def _settings(row: IntegrationConfig) -> dict:
    config = {**default_config(), **(row.non_sensitive_config or {})}
    token = telegram_bot_token(row)
    username = str(config.get("telegram_bot_username") or "").lstrip("@")
    return {
        **config,
        "id": str(row.id),
        "has_telegram_token": bool(token),
        "telegram_token_masked": mask_secret(token),
        "telegram_subscribe_url": f"https://t.me/{username}?start=fecoclima" if username else None,
        "telegram_mode": "polling",
        "last_run_at": row.last_run_at,
        "last_result": row.last_result,
    }


async def _subscribers(db: DbSession, integration_id: UUID) -> list[TelegramSubscriber]:
    return list(
        (
            await db.scalars(
                select(TelegramSubscriber)
                .where(TelegramSubscriber.integration_id == integration_id)
                .order_by(TelegramSubscriber.active.desc(), TelegramSubscriber.subscribed_at.desc())
            )
        ).all()
    )


async def _verify_bot(token: str) -> dict:
    try:
        bot = (await telegram_request(token, "getMe")).get("result") or {}
        webhook = (await telegram_request(token, "getWebhookInfo")).get("result") or {}
        if webhook.get("url"):
            await telegram_request(token, "deleteWebhook", {"drop_pending_updates": False})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc
    if not bot.get("username"):
        raise HTTPException(status_code=502, detail="Telegram validó el token, pero el bot no tiene username público")
    return bot


@router.get("/settings")
async def get_settings(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db, create=True)
    await db.commit()
    stations = list(
        (
            await db.scalars(
                select(Station)
                .where(Station.active.is_(True), Station.deleted_at.is_(None))
                .order_by(Station.name)
            )
        ).all()
    )
    crops = list(
        (
            await db.scalars(
                select(Crop)
                .where(Crop.active.is_(True), Crop.deleted_at.is_(None))
                .order_by(case((Crop.code == "TRIGO", 0), else_=1), Crop.name)
            )
        ).all()
    )
    subscribers = await _subscribers(db, row.id)
    return {
        "settings": _settings(row),
        "stations": [{"id": str(item.id), "name": item.name} for item in stations],
        "crops": [{"id": str(item.id), "code": item.code, "name": item.name} for item in crops],
        "telegram_subscribers": [telegram_subscriber_payload(item) for item in subscribers],
    }


@router.put("/settings")
async def update_settings(
    payload: ProducerNotificationSettingsPayload,
    request: Request,
    db: DbSession,
    actor: AdminUser,
) -> dict:
    row = await get_notification_integration(db, create=True)
    before = _settings(row)
    previous = {**default_config(), **(row.non_sensitive_config or {})}
    old_token = telegram_bot_token(row)
    new_token = payload.telegram_bot_token or old_token

    if payload.telegram_enabled and not new_token:
        raise HTTPException(status_code=409, detail="Guarde primero el token del bot de Telegram")

    data = payload.model_dump(exclude={"telegram_bot_token"}, mode="json")
    data["last_delivery_date"] = previous.get("last_delivery_date")
    data["telegram_update_offset"] = previous.get("telegram_update_offset", 0)
    data["telegram_bot_username"] = previous.get("telegram_bot_username")
    data["telegram_bot_name"] = previous.get("telegram_bot_name")
    data["telegram_last_sync_at"] = previous.get("telegram_last_sync_at")
    data["telegram_polling_initialized"] = previous.get("telegram_polling_initialized", False)

    if payload.telegram_bot_token:
        bot = await _verify_bot(payload.telegram_bot_token)
        token_changed = payload.telegram_bot_token != old_token
        row.encrypted_secret = encrypt_secret(payload.telegram_bot_token)
        row.secret_hint = payload.telegram_bot_token[-4:]
        data["telegram_bot_username"] = bot.get("username")
        data["telegram_bot_name"] = bot.get("first_name") or bot.get("username")
        data["telegram_polling_initialized"] = True
        if token_changed:
            data["telegram_update_offset"] = 0
            data["telegram_last_sync_at"] = None
            await db.execute(
                delete(TelegramSubscriber).where(TelegramSubscriber.integration_id == row.id)
            )

    row.non_sensitive_config = data
    row.active = payload.active
    row.sync_frequency_minutes = 1440
    await db.execute(
        update(TelegramSubscriber)
        .where(TelegramSubscriber.integration_id == row.id)
        .values(minimum_level=data["telegram_minimum_level"])
    )
    await write_audit(
        db,
        action="PRODUCER_NOTIFICATIONS_UPDATE",
        user=actor,
        request=request,
        entity_type="integration",
        entity_id=str(row.id),
        before=before,
        after=_settings(row),
    )
    await db.commit()
    return _settings(row)


@router.post("/run-now")
async def run_now(db: DbSession, _: AdminUser) -> dict:
    job = await enqueue_unique(
        db,
        "DAILY_PRODUCER_DIGEST",
        {"force": True, "requested_at": datetime.now(UTC).replace(second=0, microsecond=0).isoformat()},
        priority=15,
    )
    return {"queued": bool(job), "job_id": str(job.id) if job else None}


@router.post("/test-email")
async def test_email(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    if not row:
        raise HTTPException(status_code=409, detail="Configure primero los avisos")
    config = {**default_config(), **(row.non_sensitive_config or {})}
    if not config.get("email_enabled"):
        raise HTTPException(status_code=409, detail="Active el canal de correo")
    try:
        return await generate_daily_digest(db, force=True, only_channels={"email"}, mark_delivered=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc


@router.post("/telegram/verify")
async def verify_telegram(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    token = telegram_bot_token(row)
    if not row or not token:
        raise HTTPException(status_code=409, detail="Guarde primero el token del bot")
    bot = await _verify_bot(token)
    config = {**default_config(), **(row.non_sensitive_config or {})}
    config["telegram_bot_username"] = bot.get("username")
    config["telegram_bot_name"] = bot.get("first_name") or bot.get("username")
    config["telegram_polling_initialized"] = True
    row.non_sensitive_config = config
    await db.commit()
    username = str(bot.get("username") or "").lstrip("@")
    return {
        "ok": True,
        "bot": bot,
        "subscribe_url": f"https://t.me/{username}?start=fecoclima",
        "mode": "polling",
    }


@router.post("/telegram/sync")
async def sync_telegram(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    if not row or not telegram_bot_token(row):
        raise HTTPException(status_code=409, detail="Guarde primero el token del bot")
    try:
        result = await sync_telegram_subscriptions(db, row)
        subscribers = await _subscribers(db, row.id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc
    return {
        **result,
        "items": [telegram_subscriber_payload(item) for item in subscribers],
    }


@router.post("/test-telegram")
async def test_telegram(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    token = telegram_bot_token(row)
    if not row or not token:
        raise HTTPException(status_code=409, detail="Guarde primero el token del bot de Telegram")
    try:
        await sync_telegram_subscriptions(db, row)
        result = await send_telegram_to_subscribers(
            db,
            row,
            token,
            "✅ FECOCLIMA IA: conexión correcta. Este chat quedó suscrito a las alertas automáticas.",
        )
        bot = (await telegram_request(token, "getMe")).get("result") or {}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:3000]) from exc
    return {"ok": True, "bot": bot.get("username"), "delivery": result}


@router.get("/telegram/subscribers")
async def list_telegram_subscribers(db: DbSession, _: AdminUser) -> dict:
    row = await get_notification_integration(db)
    if not row:
        return {"items": []}
    return {"items": [telegram_subscriber_payload(item) for item in await _subscribers(db, row.id)]}


@router.delete("/telegram/subscribers/{subscriber_id}")
async def deactivate_telegram_subscriber(subscriber_id: UUID, db: DbSession, _: AdminUser) -> dict:
    row = await db.get(TelegramSubscriber, subscriber_id)
    if not row:
        raise HTTPException(status_code=404, detail="Suscriptor no encontrado")
    row.active = False
    row.unsubscribed_at = datetime.now(UTC)
    await db.commit()
    return {"ok": True}


@router.post("/telegram/reset")
async def reset_telegram(
    request: Request,
    db: DbSession,
    actor: AdminUser,
) -> dict:
    row = await get_notification_integration(db, create=True)
    before = _settings(row)
    token = telegram_bot_token(row)
    if token:
        try:
            await telegram_request(token, "deleteWebhook", {"drop_pending_updates": True})
        except Exception:
            # The local reset must still work when the old token was revoked.
            pass
    await db.execute(delete(TelegramSubscriber).where(TelegramSubscriber.integration_id == row.id))
    config = {**default_config(), **(row.non_sensitive_config or {})}
    config.update(
        {
            "telegram_enabled": False,
            "telegram_update_offset": 0,
            "telegram_bot_username": None,
            "telegram_bot_name": None,
            "telegram_last_sync_at": None,
            "telegram_polling_initialized": False,
        }
    )
    config.pop("telegram_chat_id", None)
    config.pop("telegram_webhook_url", None)
    row.non_sensitive_config = config
    row.encrypted_secret = None
    row.secret_hint = None
    await write_audit(
        db,
        action="TELEGRAM_INTEGRATION_RESET",
        user=actor,
        request=request,
        entity_type="integration",
        entity_id=str(row.id),
        before=before,
        after=_settings(row),
    )
    await db.commit()
    return {"ok": True, "message": "Telegram quedó reiniciado. Configure un bot y suscriba los chats nuevamente."}


@router.get("/logs")
async def notification_logs(db: DbSession, _: AdminUser, limit: int = 50) -> dict:
    row = await get_notification_integration(db)
    if not row:
        return {"items": []}
    rows = list(
        (
            await db.scalars(
                select(IntegrationLog)
                .where(IntegrationLog.integration_id == row.id)
                .order_by(IntegrationLog.created_at.desc())
                .limit(min(limit, 100))
            )
        ).all()
    )
    return {"items": [model_dict(item) for item in rows]}
