from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import aiosmtplib
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.db.models import Crop, EmailSubscription, IntegrationConfig, IntegrationLog, Station, TelegramSubscriber
from app.services.agent_runtime import run_agent_for_alert
from app.services.source_refresh import refresh_station_source_if_needed
from app.services.weather import aggregate_range, evaluate_alert_for_day

CONFIG_NAME = "Avisos diarios a productores"
PROVIDER = "EMAIL_TELEGRAM_DIGEST"
ALERT_ORDER = {"DATOS_INSUFICIENTES": -1, "NINGUNA": 0, "MODERADA": 1, "GRAVE": 2}


class TelegramAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, error_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def default_config() -> dict[str, Any]:
    return {
        "active": False,
        "timezone": "America/Asuncion",
        "send_hour": 6,
        "email_enabled": False,
        "recipients": [],
        "telegram_enabled": False,
        "telegram_minimum_level": "MODERADA",
        "telegram_update_offset": 0,
        "telegram_bot_username": None,
        "telegram_bot_name": None,
        "telegram_last_sync_at": None,
        "telegram_polling_initialized": False,
        "contexts": [],
        "last_delivery_date": None,
    }


async def get_notification_integration(db: AsyncSession, *, create: bool = False) -> IntegrationConfig | None:
    row = await db.scalar(
        select(IntegrationConfig).where(
            IntegrationConfig.name == CONFIG_NAME,
            IntegrationConfig.deleted_at.is_(None),
        )
    )
    if not row and create:
        row = IntegrationConfig(
            name=CONFIG_NAME,
            integration_type="NOTIFICATION",
            provider=PROVIDER,
            active=False,
            non_sensitive_config=default_config(),
            timeout_seconds=60,
            retry_count=3,
            sync_frequency_minutes=1440,
        )
        db.add(row)
        await db.flush()
    return row


def telegram_bot_token(integration: IntegrationConfig | None) -> str:
    """Return the BotFather token, including compatibility with the old JSON bundle."""
    decrypted = decrypt_secret(integration.encrypted_secret) if integration else None
    if not decrypted:
        return ""
    try:
        payload = json.loads(decrypted)
    except json.JSONDecodeError:
        return decrypted.strip()
    if isinstance(payload, dict):
        return str(payload.get("token") or payload.get("bot_token") or "").strip()
    return decrypted.strip()


def _markdown_to_plain(markdown: str) -> str:
    text = markdown.replace("**", "").replace("__", "").replace("`", "")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        while stripped.startswith("#"):
            stripped = stripped[1:].strip()
        if stripped.startswith(">"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    return "\n".join(lines).strip()


def _markdown_to_html(markdown: str) -> str:
    parts: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                parts.append("</ul>")
                in_list = False
            continue
        if line.startswith("### "):
            if in_list:
                parts.append("</ul>"); in_list = False
            parts.append(f"<h3>{escape(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_list:
                parts.append("</ul>"); in_list = False
            parts.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("# "):
            if in_list:
                parts.append("</ul>"); in_list = False
            parts.append(f"<h1>{escape(line[2:])}</h1>")
        elif line.startswith(("- ", "* ")):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{escape(line[2:])}</li>")
        else:
            if in_list:
                parts.append("</ul>"); in_list = False
            parts.append(f"<p>{escape(line)}</p>")
    if in_list:
        parts.append("</ul>")
    return "".join(parts)


def _email_message(recipient: str, subject: str, markdown: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(_markdown_to_plain(markdown))
    message.add_alternative(
        "<html><body style='font-family:Arial,sans-serif;line-height:1.55;color:#18364a'>"
        "<div style='max-width:760px;margin:auto;padding:24px'>"
        f"{_markdown_to_html(markdown)}"
        "<hr><p style='font-size:12px;color:#667'>FECOCLIMA IA · Recurso informativo para productores. "
        "Valide decisiones fitosanitarias con un profesional.</p></div></body></html>",
        subtype="html",
    )
    return message


async def _send_email(recipients: list[str], subject: str, markdown: str) -> None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST no está configurado")
    for recipient in recipients:
        await aiosmtplib.send(
            _email_message(recipient, subject, markdown),
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            start_tls=settings.smtp_starttls,
            timeout=60,
        )


async def telegram_request(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    if not token:
        raise TelegramAPIError("Telegram: falta el token del bot")
    url = f"https://api.telegram.org/bot{token}/{method}"
    retryable = {408, 425, 429, 500, 502, 503, 504}
    last_error: TelegramAPIError | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15)) as client:
        for attempt in range(max(1, attempts)):
            try:
                response = await client.post(url, json=payload or {})
            except httpx.RequestError as exc:
                last_error = TelegramAPIError(f"Telegram: {type(exc).__name__}: {str(exc).replace(token, "[REDACTED]")[:500]}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(0.5 * (2**attempt), 4))
                    continue
                raise last_error from exc
            try:
                data = response.json()
            except Exception as exc:
                raise TelegramAPIError(
                    f"Telegram respondió HTTP {response.status_code} sin JSON válido",
                    status_code=response.status_code,
                ) from exc
            if response.status_code < 400 and data.get("ok"):
                return data
            description = str(data.get("description") or response.text[:1000])
            error = TelegramAPIError(
                f"Telegram: {description}",
                status_code=response.status_code,
                error_code=data.get("error_code"),
            )
            last_error = error
            if response.status_code in retryable and attempt + 1 < attempts:
                retry_after = ((data.get("parameters") or {}).get("retry_after"))
                try:
                    delay = max(0.5, min(float(retry_after), 10)) if retry_after is not None else min(0.5 * (2**attempt), 4)
                except (TypeError, ValueError):
                    delay = min(0.5 * (2**attempt), 4)
                await asyncio.sleep(delay)
                continue
            raise error
    raise last_error or TelegramAPIError("Telegram no respondió")


def _telegram_chunks(markdown: str) -> list[str]:
    plain = _markdown_to_plain(markdown)
    chunks: list[str] = []
    remaining = plain
    while remaining:
        if len(remaining) <= 3900:
            chunks.append(remaining)
            break
        split = remaining.rfind("\n", 0, 3900)
        if split < 1000:
            split = 3900
        chunks.append(remaining[:split].strip())
        remaining = remaining[split:].strip()
    return chunks


async def _send_telegram(token: str, chat_id: str, markdown: str) -> None:
    for chunk in _telegram_chunks(markdown):
        await telegram_request(
            token,
            "sendMessage",
            {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
        )


def telegram_subscriber_payload(row: TelegramSubscriber) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "chat_id": row.chat_id,
        "chat_type": row.chat_type,
        "title": row.title or row.username or row.first_name or row.chat_id,
        "username": row.username,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "active": row.active,
        "minimum_level": row.minimum_level,
        "subscribed_at": row.subscribed_at,
        "unsubscribed_at": row.unsubscribed_at,
        "last_delivery_at": row.last_delivery_at,
        "last_error": row.last_error,
    }


async def active_telegram_subscribers(db: AsyncSession, integration_id: UUID) -> list[TelegramSubscriber]:
    return list(
        (
            await db.scalars(
                select(TelegramSubscriber)
                .where(
                    TelegramSubscriber.integration_id == integration_id,
                    TelegramSubscriber.active.is_(True),
                )
                .order_by(TelegramSubscriber.subscribed_at, TelegramSubscriber.created_at)
            )
        ).all()
    )


def _telegram_message(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        value = update.get(key)
        if isinstance(value, dict):
            return value
    membership = update.get("my_chat_member")
    if isinstance(membership, dict):
        return membership
    return None


def telegram_chat_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    message = _telegram_message(update)
    chat = message.get("chat") if isinstance(message, dict) else None
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None
    return {
        "chat_id": str(chat["id"]),
        "chat_type": str(chat.get("type") or "unknown"),
        "title": chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat["id"]),
        "username": chat.get("username"),
        "first_name": chat.get("first_name"),
        "last_name": chat.get("last_name"),
    }


def _telegram_command(update: dict[str, Any]) -> str | None:
    message = _telegram_message(update)
    text = str(message.get("text") or "").strip() if isinstance(message, dict) else ""
    if not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0].split("@", 1)[0].lower()


async def process_telegram_update(
    db: AsyncSession,
    integration: IntegrationConfig,
    token: str,
    update: dict[str, Any],
) -> dict[str, Any]:
    chat = telegram_chat_from_update(update)
    if not chat:
        return {"action": "IGNORED", "reason": "no_chat"}
    subscriber = await db.scalar(
        select(TelegramSubscriber).where(
            TelegramSubscriber.integration_id == integration.id,
            TelegramSubscriber.chat_id == chat["chat_id"],
        )
    )
    command = _telegram_command(update)
    message_data = _telegram_message(update)
    message_text = str(message_data.get("text") or "").strip() if isinstance(message_data, dict) else ""
    # Solo un enlace recibido después de confirmar correo permite vincular
    # Telegram con el cultivo y estación de la libreta de direcciones.
    linked_email_subscription = None
    if command == "/start" and " " in message_text:
        from app.services.notification_events import digest
        from hmac import compare_digest
        import re
        match = re.fullmatch(r"fc_([a-f0-9]{32})_([a-f0-9]{24})", message_text.split(" ", 1)[1].strip())
        if match and chat["chat_type"] == "private":
            candidate = await db.get(EmailSubscription, UUID(hex=match.group(1)))
            if candidate and candidate.status == "ACTIVE" and candidate.confirmed_at and candidate.telegram_enabled and compare_digest(
                match.group(2), digest(f"tg:{candidate.id}:{candidate.manage_secret}")[:24]
            ):
                candidate.telegram_chat_id = chat["chat_id"]
                candidate.telegram_active = True
                candidate.telegram_username = chat.get("username") or candidate.telegram_username
                linked_email_subscription = candidate
    membership = update.get("my_chat_member") if isinstance(update.get("my_chat_member"), dict) else None
    new_status = str(((membership or {}).get("new_chat_member") or {}).get("status") or "")
    now = datetime.now(UTC)
    response_text: str | None = None
    action = "IGNORED"

    if command in {"/start", "/subscribe", "/suscribir"}:
        if not linked_email_subscription:
            linked_email_subscription = await db.scalar(select(EmailSubscription).where(
                EmailSubscription.telegram_chat_id == chat['chat_id'],
                EmailSubscription.status == 'ACTIVE', EmailSubscription.confirmed_at.is_not(None),
                EmailSubscription.telegram_enabled.is_(True)).limit(1))
            if linked_email_subscription:
                linked_email_subscription.telegram_active = True
        if not subscriber:
            subscriber = TelegramSubscriber(integration_id=integration.id, chat_id=chat["chat_id"])
            db.add(subscriber)
        subscriber.chat_type = chat["chat_type"]
        subscriber.title = chat["title"]
        subscriber.username = chat["username"]
        subscriber.first_name = chat["first_name"]
        subscriber.last_name = chat["last_name"]
        subscriber.active = bool(linked_email_subscription)
        subscriber.minimum_level = str((integration.non_sensitive_config or {}).get("telegram_minimum_level") or "MODERADA")
        subscriber.subscribed_at = now
        subscriber.unsubscribed_at = None
        subscriber.last_error = None
        action = "SUBSCRIBED" if linked_email_subscription else "LINK_REQUIRED"
        from app.services.notification_events import public_url
        response_text = (
            "✅ Telegram vinculado a su suscripción confirmada. Recibirá los avisos y alertas que correspondan a las condiciones observadas. Use /status para consultar el estado o /stop para dejar de recibir Telegram."
            if linked_email_subscription else
            "Para recibir avisos, inscríbase en la libreta de direcciones, elija Telegram y confirme su correo. Después abra el enlace de vinculación del bot: " + public_url('')
        )
    elif command in {"/stop", "/unsubscribe", "/salir"}:
        linked = (await db.scalars(select(EmailSubscription).where(EmailSubscription.telegram_chat_id == chat["chat_id"]))).all()
        for email_subscription in linked:
            email_subscription.telegram_active = False
        if subscriber:
            subscriber.active = False
            subscriber.unsubscribed_at = now
        action = "UNSUBSCRIBED"
        response_text = "Suscripción desactivada. Puede volver a activarla con /start."
    elif command == "/status":
        linked = await db.scalar(select(EmailSubscription).where(
            EmailSubscription.telegram_chat_id == chat['chat_id'], EmailSubscription.status == 'ACTIVE',
            EmailSubscription.confirmed_at.is_not(None), EmailSubscription.telegram_enabled.is_(True),
            EmailSubscription.telegram_active.is_(True)).limit(1))
        action = "STATUS"
        response_text = "Estado: activo para avisos y alertas." if linked else "Estado: inactivo. Confirme su inscripción y use el enlace de vinculación de Telegram."
    elif new_status in {"kicked", "left"}:
        linked = (await db.scalars(select(EmailSubscription).where(EmailSubscription.telegram_chat_id == chat["chat_id"]))).all()
        for email_subscription in linked:
            email_subscription.telegram_active = False
        if subscriber:
            subscriber.active = False
            subscriber.unsubscribed_at = now
        action = "DEACTIVATED_BY_MEMBERSHIP"
    elif command:
        action = "HELP"
        response_text = "Comandos disponibles: /start, /status y /stop."

    if subscriber:
        try:
            subscriber.last_update_id = int(update.get("update_id")) if update.get("update_id") is not None else subscriber.last_update_id
        except (TypeError, ValueError):
            pass
    await db.commit()
    if response_text:
        await _send_telegram(token, chat["chat_id"], response_text)
    return {"action": action, "chat_id": chat["chat_id"]}


async def sync_telegram_subscriptions(
    db: AsyncSession,
    integration: IntegrationConfig | None = None,
) -> dict[str, Any]:
    """Read pending Bot API updates and persist /start, /status and /stop commands.

    This integration intentionally uses getUpdates instead of a webhook. It works
    in local development and production without exposing a public callback URL.
    Exactly one scheduler/worker stack should poll a given bot token.
    """
    integration = integration or await get_notification_integration(db)
    if not integration:
        return {"status": "SKIPPED", "reason": "not_configured", "processed": 0}
    token = telegram_bot_token(integration)
    if not token:
        return {"status": "SKIPPED", "reason": "token_missing", "processed": 0}

    config = {**default_config(), **(integration.non_sensitive_config or {})}
    bot = {
        "username": config.get("telegram_bot_username"),
        "first_name": config.get("telegram_bot_name"),
    }
    if not config.get("telegram_polling_initialized"):
        webhook = (await telegram_request(token, "getWebhookInfo")).get("result") or {}
        if webhook.get("url"):
            await telegram_request(token, "deleteWebhook", {"drop_pending_updates": False})
        bot = (await telegram_request(token, "getMe")).get("result") or bot
        config["telegram_polling_initialized"] = True

    offset = int(config.get("telegram_update_offset") or 0)
    response = await telegram_request(
        token,
        "getUpdates",
        {
            "offset": offset,
            "limit": 100,
            "timeout": 0,
            "allowed_updates": ["message", "edited_message", "channel_post", "my_chat_member"],
        },
    )
    updates = list(response.get("result") or [])
    processed: list[dict[str, Any]] = []
    for update in updates:
        processed.append(await process_telegram_update(db, integration, token, update))

    if updates:
        config["telegram_update_offset"] = max(int(item.get("update_id") or 0) for item in updates) + 1
    config["telegram_bot_username"] = bot.get("username")
    config["telegram_bot_name"] = bot.get("first_name") or bot.get("username")
    config["telegram_last_sync_at"] = datetime.now(UTC).isoformat()
    config.pop("telegram_chat_id", None)
    config.pop("telegram_webhook_url", None)
    integration.non_sensitive_config = config
    await db.commit()
    return {
        "status": "COMPLETED",
        "processed": len(processed),
        "actions": processed,
        "bot": {
            "id": bot.get("id"),
            "username": bot.get("username"),
            "name": bot.get("first_name"),
        },
        "polling": True,
    }


def _terminal_telegram_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(value in text for value in ("bot was blocked", "chat not found", "forbidden", "user is deactivated", "bot was kicked"))


async def send_telegram_to_subscribers(
    db: AsyncSession,
    integration: IntegrationConfig,
    token: str,
    markdown: str,
) -> dict[str, Any]:
    subscribers = await active_telegram_subscribers(db, integration.id)
    if not subscribers:
        raise RuntimeError(
            "No hay suscriptores de Telegram. Abra el enlace del bot, pulse Iniciar y luego sincronice las suscripciones."
        )

    sent: list[str] = []
    failed: list[dict[str, str]] = []
    for subscriber in subscribers:
        chat_id = subscriber.chat_id
        try:
            await _send_telegram(token, chat_id, markdown)
            sent.append(chat_id)
            subscriber.last_delivery_at = datetime.now(UTC)
            subscriber.last_error = None
        except Exception as exc:
            message = str(exc)[:1000]
            failed.append({"chat_id": chat_id, "error": message})
            subscriber.last_error = message
            if _terminal_telegram_error(exc):
                subscriber.active = False
                subscriber.unsubscribed_at = datetime.now(UTC)
        await db.commit()
    if not sent:
        raise RuntimeError("Telegram no pudo entregar el boletín a ningún suscriptor: " + " | ".join(item["error"] for item in failed[:3]))
    return {"sent": len(sent), "failed": failed}


async def _context_digest(
    db: AsyncSession,
    context: dict[str, Any],
    *,
    synced_station_ids: set[UUID],
    minimum_level: str,
    include_below_threshold: bool = False,
) -> dict[str, Any]:
    station = await db.get(Station, UUID(str(context["station_id"])))
    crop = await db.scalar(select(Crop).where(Crop.code == str(context["crop_code"]).upper(), Crop.deleted_at.is_(None)))
    if not station or station.deleted_at:
        raise ValueError("Estación configurada no disponible")
    if not crop:
        raise ValueError("Cultivo configurado no disponible")
    planting_date = date.fromisoformat(str(context["planting_date"]))
    timezone = ZoneInfo(station.timezone or "America/Asuncion")
    today = datetime.now(timezone).date()
    date_from = today - timedelta(days=2)
    if station.id not in synced_station_ids:
        await refresh_station_source_if_needed(db, station, requested=True, force=True)
        synced_station_ids.add(station.id)
    await aggregate_range(db, station.id, date_from, today)

    evaluations: list[dict[str, Any]] = []
    current = date_from
    while current <= today:
        result = await evaluate_alert_for_day(db, station.id, crop.id, current, planting_date=planting_date)
        evaluations.append({**result, "evaluation_date": current.isoformat()})
        current += timedelta(days=1)
    candidates = [item for item in evaluations if item.get("alert_id")]
    if not candidates:
        raise ValueError("No fue posible evaluar las últimas 72 horas")
    maximum = max(candidates, key=lambda item: (ALERT_ORDER.get(str(item.get("level") or "").upper(), -1), item["evaluation_date"]))
    level = str(maximum.get("level") or "DATOS_INSUFICIENTES").upper()
    eligible = ALERT_ORDER.get(level, -1) >= ALERT_ORDER.get(minimum_level, 1)
    markdown = ""
    if eligible or include_below_threshold:
        recommendation = await run_agent_for_alert(
            db,
            UUID(str(maximum["alert_id"])),
            period="72h",
            purpose="DAILY_DIGEST",
            force=True,
        )
        markdown = (recommendation or {}).get("markdown") or "No se pudo generar el criterio IA."
    return {
        "station_id": str(station.id),
        "crop_id": str(crop.id),
        "label": context.get("label") or f"{crop.name} · {station.name}",
        "station": station.name,
        "crop": crop.name,
        "level": level,
        "eligible": eligible,
        "evaluation_date": maximum["evaluation_date"],
        "markdown": markdown,
    }


async def generate_daily_digest(
    db: AsyncSession,
    *,
    force: bool = False,
    only_channels: set[str] | None = None,
    mark_delivered: bool = True,
) -> dict[str, Any]:
    integration = await get_notification_integration(db)
    if not integration:
        return {"status": "SKIPPED", "reason": "not_configured"}
    config = {**default_config(), **(integration.non_sensitive_config or {})}
    if (not integration.active or not config.get("active")) and not force:
        return {"status": "SKIPPED", "reason": "inactive"}
    timezone = ZoneInfo(config.get("timezone") or "America/Asuncion")
    now_local = datetime.now(timezone)
    today_key = now_local.date().isoformat()
    if not force:
        # Run at the first scheduler cycle at or after the configured local hour;
        # this avoids permanently missing a day when a worker is delayed.
        if now_local.hour < int(config.get("send_hour", 6)):
            return {"status": "SKIPPED", "reason": "before_schedule"}
        if config.get("last_delivery_date") == today_key:
            return {"status": "SKIPPED", "reason": "already_sent_today"}

    started = datetime.now(UTC)
    minimum_level = str(config.get("telegram_minimum_level") or "MODERADA").upper()
    if minimum_level not in {"MODERADA", "GRAVE"}:
        minimum_level = "MODERADA"
    log = IntegrationLog(
        integration_id=integration.id,
        operation="DAILY_PRODUCER_DIGEST",
        status="RUNNING",
        started_at=started,
        extra_data={
            "force": force,
            "date": today_key,
            "only_channels": sorted(only_channels or []),
            "minimum_level": minimum_level,
        },
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    try:
        all_sections: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        synced_station_ids: set[UUID] = set()
        for item in config.get("contexts") or []:
            try:
                all_sections.append(
                    await _context_digest(
                        db,
                        item,
                        synced_station_ids=synced_station_ids,
                        minimum_level=minimum_level,
                        include_below_threshold=force,
                    )
                )
            except Exception as exc:
                failures.append({"context": item.get("label") or item, "error": str(exc)[:500]})

        sections = [item for item in all_sections if item["eligible"] or force]
        if not sections and not force:
            integration.last_run_at = datetime.now(UTC)
            integration.last_result = "SKIPPED"
            log.status = "COMPLETED"
            log.finished_at = datetime.now(UTC)
            log.duration_ms = int((log.finished_at - started).total_seconds() * 1000)
            log.records_created = 0
            log.extra_data = {
                "reason": "no_moderate_or_severe_alert",
                "minimum_level": minimum_level,
                "evaluated_contexts": [
                    {"label": item["label"], "level": item["level"], "evaluation_date": item["evaluation_date"]}
                    for item in all_sections
                ],
                "failures": failures,
            }
            await db.commit()
            return {
                "status": "SKIPPED",
                "reason": "no_moderate_or_severe_alert",
                "minimum_level": minimum_level,
                "contexts": len(all_sections),
                "failures": failures,
            }
        if not sections:
            raise RuntimeError("Ningún contexto pudo generar el boletín")

        header = (
            f"# 🌦️ FECOCLIMA IA · Alerta de 72 horas\n\n"
            f"**Fecha:** {now_local.strftime('%d/%m/%Y')}\n\n"
        )
        body = [header]
        for section in sections:
            body.append(
                f"\n## 🌱 {section['label']} · {section['level']}\n\n{section['markdown']}"
            )
        if failures:
            body.append("\n## ⚠️ Contextos no procesados\n" + "\n".join(f"- {item['context']}: {item['error']}" for item in failures))
        markdown = "\n".join(body)
        sent_channels: list[str] = []
        delivery_details: dict[str, Any] = {}

        # El boletín antiguo ya no puede utilizar correos pegados en el CMS:
        # solamente usuarios confirmados y con la misma estación/cultivo.
        from app.services.notification_events import _message as _scoped_message, send_mail as _send_scoped_mail, manage_token, public_url
        enabled_scopes = {(str(item['station_id']),str(item['crop_id'])) for item in sections}
        address_book = list((await db.scalars(select(EmailSubscription).where(
            EmailSubscription.status == 'ACTIVE', EmailSubscription.confirmed_at.is_not(None)
        ))).all())
        targets = [r for r in address_book if (str(r.station_id),str(r.crop_id)) in enabled_scopes]
        if config.get("email_enabled") and (only_channels is None or "email" in only_channels):
            email_sent=0
            for recipient in targets:
                local_sections = [item for item in sections if (str(item['station_id']),str(item['crop_id'])) == (str(recipient.station_id),str(recipient.crop_id))]
                content = '\n\n'.join(f"{item['label']} · {item['level']}\n{_markdown_to_plain(item['markdown'])}" for item in local_sections)
                await _send_scoped_mail(_scoped_message(
                    recipient.email,
                    f"FECOCLIMA IA · Alerta {minimum_level.lower()} · {now_local.strftime('%d/%m/%Y')}",
                    'Boletín meteorológico para su cultivo', content,
                    unsubscribe_url=public_url('?baja='+manage_token(recipient)),
                ))
                email_sent+=1
            sent_channels.append("email")
            delivery_details["email"]={"sent":email_sent,"origin":"confirmed_address_book"}

        if config.get("telegram_enabled") and (only_channels is None or "telegram" in only_channels):
            token = telegram_bot_token(integration)
            if not token:
                raise RuntimeError("El token de Telegram no está configurado")
            telegram_sent=0
            for recipient in targets:
                if not recipient.telegram_active or not recipient.telegram_chat_id:
                    continue
                local_sections = [item for item in sections if (str(item['station_id']),str(item['crop_id'])) == (str(recipient.station_id),str(recipient.crop_id))]
                content = '\n\n'.join(f"{item['label']} · {item['level']}\n{_markdown_to_plain(item['markdown'])}" for item in local_sections)
                await _send_telegram(token, recipient.telegram_chat_id,content+'\n\nPara dejar de recibir Telegram envíe /stop.')
                telegram_sent+=1
            sent_channels.append("telegram")
            delivery_details["telegram"]={"sent":telegram_sent,"origin":"confirmed_address_book"}

        if not sent_channels:
            raise RuntimeError("No hay canales de envío activos")
        if mark_delivered:
            config["last_delivery_date"] = today_key
        integration.non_sensitive_config = config
        integration.last_run_at = datetime.now(UTC)
        integration.last_result = "COMPLETED"
        log.status = "COMPLETED"
        log.finished_at = datetime.now(UTC)
        log.duration_ms = int((log.finished_at - started).total_seconds() * 1000)
        log.records_created = len(sections)
        log.extra_data = {
            "channels": sent_channels,
            "delivery": delivery_details,
            "contexts": len(sections),
            "levels": {item["label"]: item["level"] for item in sections},
            "failures": failures,
        }
        await db.commit()
        return {
            "status": "COMPLETED",
            "channels": sent_channels,
            "delivery": delivery_details,
            "contexts": len(sections),
            "failures": failures,
        }
    except Exception as exc:
        integration.last_run_at = datetime.now(UTC)
        integration.last_result = "FAILED"
        log.status = "FAILED"
        log.finished_at = datetime.now(UTC)
        log.duration_ms = int((log.finished_at - started).total_seconds() * 1000)
        log.error_message = str(exc)[:3000]
        await db.commit()
        raise
