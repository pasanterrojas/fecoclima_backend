from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.core.logging import configure_logging
from app.db.models import Station
from app.db.session import AsyncSessionLocal
from app.services.producer_notifications import (
    active_telegram_subscribers,
    get_notification_integration,
    send_telegram_to_subscribers,
    telegram_bot_token,
    telegram_request,
)
from app.services.satellite import (
    INDEX_NAMES,
    sync_satellite_for_station,
    test_copernicus_integration,
)


@dataclass
class AcceptanceResult:
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, **details: Any) -> None:
        self.checks.append({"name": name, "ok": ok, **details})

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)

    def payload(self) -> dict[str, Any]:
        return {
            "status": "PASSED" if self.ok else "FAILED",
            "checks": self.checks,
        }


async def run(args: argparse.Namespace) -> AcceptanceResult:
    result = AcceptanceResult()
    station_id = UUID(args.station_id)

    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("SELECT 1"))
            result.add("database", True)
        except Exception as exc:
            result.add("database", False, error=str(exc)[:1000])
            return result

        station = await db.scalar(
            select(Station).where(
                Station.id == station_id,
                Station.active.is_(True),
                Station.deleted_at.is_(None),
            )
        )
        if not station:
            result.add("station", False, error="La estación no existe o no está activa")
            return result
        result.add("station", True, station=station.name, station_id=str(station.id))

        try:
            copernicus = await test_copernicus_integration(db, station.id)
            copernicus_ok = (
                copernicus.get("status") == "PROCESSED"
                and copernicus.get("image_available") is True
                and int(copernicus.get("visual_layers_available") or 0) == len(INDEX_NAMES)
                and all((copernicus.get("indices") or {}).get(name) is not None for name in INDEX_NAMES)
            )
            result.add(
                "copernicus_provider",
                copernicus_ok,
                status=copernicus.get("status"),
                scene_id=copernicus.get("scene_id"),
                captured_at=copernicus.get("captured_at"),
                image_available=copernicus.get("image_available"),
                visual_layers_available=copernicus.get("visual_layers_available"),
                indices_available=sum(
                    (copernicus.get("indices") or {}).get(name) is not None for name in INDEX_NAMES
                ),
                error=copernicus.get("processing_error"),
                diagnostics=copernicus.get("diagnostics"),
            )
        except Exception as exc:
            result.add("copernicus_provider", False, error=str(exc)[:2000])

        if args.full_copernicus:
            try:
                stored = await sync_satellite_for_station(db, station.id, force=True)
                stored_ok = (
                    stored.get("status") == "PROCESSED"
                    and stored.get("image_available") is True
                    and int(stored.get("visual_layers_available") or 0) == len(INDEX_NAMES)
                    and len(stored.get("index_images") or {}) == len(INDEX_NAMES)
                )
                result.add(
                    "copernicus_storage_widget",
                    stored_ok,
                    status=stored.get("status"),
                    observation_id=stored.get("observation_id"),
                    image_available=stored.get("image_available"),
                    visual_layers_available=stored.get("visual_layers_available"),
                    processing_error=stored.get("processing_error"),
                )
            except Exception as exc:
                result.add("copernicus_storage_widget", False, error=str(exc)[:2000])

        notifications = await get_notification_integration(db)
        token = telegram_bot_token(notifications)
        if not notifications or not token:
            result.add("telegram_configuration", False, error="No existe token de Telegram configurado")
        else:
            try:
                bot = (await telegram_request(token, "getMe")).get("result") or {}
                webhook = (await telegram_request(token, "getWebhookInfo")).get("result") or {}
                subscribers = await active_telegram_subscribers(db, notifications.id)
                webhook_ok = str(webhook.get("url") or "").startswith("https://")
                result.add(
                    "telegram_configuration",
                    bool(bot.get("id")) and webhook_ok and bool(subscribers),
                    bot_username=bot.get("username"),
                    webhook_url=webhook.get("url"),
                    webhook_pending_updates=webhook.get("pending_update_count"),
                    webhook_last_error=webhook.get("last_error_message"),
                    active_subscribers=len(subscribers),
                )
                if args.send_telegram_test:
                    delivery = await send_telegram_to_subscribers(
                        db,
                        notifications,
                        token,
                        "✅ FECOCLIMA IA · prueba de aceptación de producción completada.",
                    )
                    result.add("telegram_delivery", delivery.get("sent", 0) > 0, **delivery)
            except Exception as exc:
                result.add("telegram_configuration", False, error=str(exc)[:2000])

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prueba real de aceptación para PostgreSQL, Copernicus y Telegram."
    )
    parser.add_argument("--station-id", required=True, help="UUID de una estación activa con GeoJSON o coordenadas")
    parser.add_argument(
        "--full-copernicus",
        action="store_true",
        help="Además de probar el proveedor, persiste una escena y verifica las 9 capas del widget",
    )
    parser.add_argument(
        "--send-telegram-test",
        action="store_true",
        help="Envía un mensaje real a todos los suscriptores activos",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    result = await run(args)
    print(json.dumps(result.payload(), ensure_ascii=False, indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    configure_logging()
    raise SystemExit(asyncio.run(main()))
