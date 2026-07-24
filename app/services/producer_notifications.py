from __future__ import annotations

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
from app.db.models import Crop, IntegrationConfig, IntegrationLog, Station
from app.services.agent_runtime import run_agent_for_alert
from app.services.source_refresh import refresh_station_source_if_needed
from app.services.weather import aggregate_range, evaluate_alert_for_day

CONFIG_NAME = "Avisos diarios a productores"
PROVIDER = "EMAIL_TELEGRAM_DIGEST"


def default_config() -> dict[str, Any]:
    return {
        "active": False,
        "timezone": "America/Asuncion",
        "send_hour": 6,
        "email_enabled": False,
        "recipients": [],
        "telegram_enabled": False,
        "telegram_chat_id": "",
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
    """Build one message per producer so recipient addresses stay private."""
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    plain = _markdown_to_plain(markdown)
    message.set_content(plain)
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
    # Se envía un mensaje individual por destinatario. Así ningún productor
    # recibe ni puede ver las direcciones de los demás miembros de la lista.
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


async def telegram_request(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(url, json=payload or {})
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"Telegram respondió HTTP {response.status_code} sin JSON válido") from exc
    if response.status_code >= 400 or not data.get("ok"):
        description = data.get("description") or response.text[:1000]
        raise RuntimeError(f"Telegram: {description}")
    return data


async def _send_telegram(token: str, chat_id: str, markdown: str) -> None:
    plain = _markdown_to_plain(markdown)
    # Telegram limita sendMessage; dividimos sin cortar palabras en bloques manejables.
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
    for chunk in chunks:
        await telegram_request(token, "sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        })


async def _context_digest(
    db: AsyncSession,
    context: dict[str, Any],
    *,
    synced_station_ids: set[UUID],
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
    # Una estación puede aparecer en varios cultivos. Sincronizamos FECOCLIMA
    # una sola vez por boletín y reutilizamos las lecturas recién almacenadas.
    if station.id not in synced_station_ids:
        await refresh_station_source_if_needed(db, station, requested=True, force=True)
        synced_station_ids.add(station.id)
    await aggregate_range(db, station.id, date_from, today)
    alert_id = None
    current = date_from
    while current <= today:
        result = await evaluate_alert_for_day(
            db,
            station.id,
            crop.id,
            current,
            planting_date=planting_date,
        )
        alert_id = result.get("alert_id") or alert_id
        current += timedelta(days=1)
    if not alert_id:
        raise ValueError("No fue posible evaluar las últimas 72 horas")
    recommendation = await run_agent_for_alert(
        db,
        UUID(str(alert_id)),
        period="72h",
        purpose="DAILY_DIGEST",
        force=True,
    )
    return {
        "label": context.get("label") or f"{crop.name} · {station.name}",
        "station": station.name,
        "crop": crop.name,
        "markdown": (recommendation or {}).get("markdown") or "No se pudo generar el criterio IA.",
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
        if int(config.get("send_hour", 6)) != now_local.hour:
            return {"status": "SKIPPED", "reason": "outside_schedule"}
        if config.get("last_delivery_date") == today_key:
            return {"status": "SKIPPED", "reason": "already_sent_today"}

    started = datetime.now(UTC)
    log = IntegrationLog(
        integration_id=integration.id,
        operation="DAILY_PRODUCER_DIGEST",
        status="RUNNING",
        started_at=started,
        extra_data={"force": force, "date": today_key, "only_channels": sorted(only_channels or [])},
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    try:
        sections = []
        failures = []
        synced_station_ids: set[UUID] = set()
        for item in config.get("contexts") or []:
            try:
                sections.append(
                    await _context_digest(
                        db,
                        item,
                        synced_station_ids=synced_station_ids,
                    )
                )
            except Exception as exc:
                failures.append({"context": item.get("label") or item, "error": str(exc)[:500]})
        if not sections:
            raise RuntimeError("Ningún contexto pudo generar el boletín")
        header = (
            f"# 🌦️ FECOCLIMA IA · Boletín de 72 horas\n\n"
            f"**Fecha:** {now_local.strftime('%d/%m/%Y')}\n\n"
            "Este boletín combina estación, reglas publicadas, fenología, pronóstico, Copernicus y fuentes documentales."
        )
        body = [header]
        for section in sections:
            body.append(f"\n## 🌱 {section['label']}\n\n{section['markdown']}")
        if failures:
            body.append("\n## ⚠️ Contextos no procesados\n" + "\n".join(f"- {item['context']}: {item['error']}" for item in failures))
        markdown = "\n".join(body)
        sent_channels: list[str] = []
        if config.get("email_enabled") and (only_channels is None or "email" in only_channels):
            recipients = list(config.get("recipients") or [])
            if not recipients:
                raise RuntimeError("No hay destinatarios de correo configurados")
            await _send_email(recipients, f"FECOCLIMA IA · Análisis de 72 horas · {now_local.strftime('%d/%m/%Y')}", markdown)
            sent_channels.append("email")
        if config.get("telegram_enabled") and (only_channels is None or "telegram" in only_channels):
            token = decrypt_secret(integration.encrypted_secret)
            if not token:
                raise RuntimeError("El token de Telegram no está configurado")
            await _send_telegram(token, str(config.get("telegram_chat_id")), markdown)
            sent_channels.append("telegram")
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
        log.extra_data = {"channels": sent_channels, "contexts": len(sections), "failures": failures}
        await db.commit()
        return {"status": "COMPLETED", "channels": sent_channels, "contexts": len(sections), "failures": failures}
    except Exception as exc:
        integration.last_run_at = datetime.now(UTC)
        integration.last_result = "FAILED"
        log.status = "FAILED"
        log.finished_at = datetime.now(UTC)
        log.duration_ms = int((log.finished_at - started).total_seconds() * 1000)
        log.error_message = str(exc)[:3000]
        await db.commit()
        raise
