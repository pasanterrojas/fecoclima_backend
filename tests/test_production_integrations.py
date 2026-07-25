from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import producer_notifications as notifications
from app.services.satellite import (
    INDEX_NAMES,
    _index_visual_evalscript,
    _process_payload,
    _scene_time_range,
    normalize_polygon_geometry,
)


def test_geojson_feature_is_canonicalized_and_ring_is_closed():
    geometry = normalize_polygon_geometry(
        {
            "type": "Feature",
            "properties": {"name": "Lote de prueba"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-55.2800, -26.1500],
                    [-55.2700, -26.1500],
                    [-55.2700, -26.1600],
                    [-55.2800, -26.1600],
                ]],
            },
        }
    )
    assert geometry["type"] == "Polygon"
    assert geometry["coordinates"][0][0] == geometry["coordinates"][0][-1]
    assert len(geometry["coordinates"][0]) == 5


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Polygon", "coordinates": [[[-181, 0], [-180, 1], [-179, 0], [-181, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [2, 2], [0, 0]]]},
        {"type": "Point", "coordinates": [-55.2, -26.1]},
    ],
)
def test_invalid_or_degenerate_geojson_is_rejected(geometry):
    with pytest.raises(ValueError):
        normalize_polygon_geometry(geometry)


@pytest.mark.parametrize("index_name", INDEX_NAMES)
def test_every_supported_index_builds_a_cloud_masked_png_evalscript(index_name):
    script = _index_visual_evalscript(index_name)
    assert "bands:4" in script
    assert "dataMask" in script
    assert "SCL" in script
    assert "return [0,0,0,0]" in script


def test_rgb_and_index_process_payload_use_same_geometry_and_latest_scene_order():
    captured_at = datetime(2026, 7, 23, 14, 10, tzinfo=UTC)
    time_from, time_to = _scene_time_range(captured_at)
    geometry = {
        "type": "Polygon",
        "coordinates": [[[-55.28, -26.15], [-55.27, -26.15], [-55.27, -26.16], [-55.28, -26.16], [-55.28, -26.15]]],
    }
    payload = _process_payload(
        geometry,
        time_from,
        time_to,
        35,
        evalscript=_index_visual_evalscript("ndvi"),
        media_type="image/png",
    )
    assert payload["input"]["bounds"]["geometry"] == geometry
    assert payload["input"]["data"][0]["dataFilter"]["mosaickingOrder"] == "mostRecent"
    assert payload["output"]["responses"][0]["format"]["type"] == "image/png"
    assert (time_to - time_from).total_seconds() == 50 * 60


def test_telegram_bot_token_supports_raw_and_previous_json_format(monkeypatch):
    integration = SimpleNamespace(encrypted_secret=b"encrypted")

    monkeypatch.setattr(notifications, "decrypt_secret", lambda _value: "123456:abcdefghijklmnopqrstuvwxyz")
    assert notifications.telegram_bot_token(integration) == "123456:abcdefghijklmnopqrstuvwxyz"

    monkeypatch.setattr(
        notifications,
        "decrypt_secret",
        lambda _value: '{"token":"654321:zyxwvutsrqponmlkjihgfedcba","webhook_secret":"old"}',
    )
    assert notifications.telegram_bot_token(integration) == "654321:zyxwvutsrqponmlkjihgfedcba"


def test_telegram_default_config_uses_polling_without_manual_chat_id():
    config = notifications.default_config()
    assert config["telegram_update_offset"] == 0
    assert "telegram_chat_id" not in config
    assert "telegram_webhook_url" not in config


def test_telegram_update_extracts_private_and_group_commands():
    private_update = {
        "update_id": 10,
        "message": {
            "chat": {"id": 123, "type": "private", "first_name": "Ana"},
            "text": "/start",
        },
    }
    group_update = {
        "update_id": 11,
        "message": {
            "chat": {"id": -456, "type": "supergroup", "title": "Productores"},
            "text": "/start@FecoClimaBot",
        },
    }
    assert notifications.telegram_chat_from_update(private_update)["chat_id"] == "123"
    assert notifications.telegram_chat_from_update(group_update)["chat_id"] == "-456"
    assert notifications._telegram_command(private_update) == "/start"
    assert notifications._telegram_command(group_update) == "/start"


def test_telegram_chunks_never_exceed_safe_message_size():
    chunks = notifications._telegram_chunks("línea de prueba\n" * 1000)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 3900 for chunk in chunks)


@pytest.mark.asyncio
async def test_telegram_delivery_continues_when_one_subscriber_is_blocked(monkeypatch):
    active = SimpleNamespace(
        chat_id="100",
        active=True,
        last_delivery_at=None,
        last_error=None,
        unsubscribed_at=None,
    )
    blocked = SimpleNamespace(
        chat_id="200",
        active=True,
        last_delivery_at=None,
        last_error=None,
        unsubscribed_at=None,
    )

    async def fake_subscribers(_db, _integration_id):
        return [active, blocked]

    async def fake_send(_token, chat_id, _markdown):
        if chat_id == "200":
            raise notifications.TelegramAPIError("Telegram: Forbidden: bot was blocked by the user", status_code=403)

    class FakeDb:
        commits = 0

        async def commit(self):
            self.commits += 1

    monkeypatch.setattr(notifications, "active_telegram_subscribers", fake_subscribers)
    monkeypatch.setattr(notifications, "_send_telegram", fake_send)
    db = FakeDb()
    result = await notifications.send_telegram_to_subscribers(
        db,
        SimpleNamespace(id=uuid4()),
        "token",
        "boletín",
    )
    assert result["sent"] == 1
    assert len(result["failed"]) == 1
    assert active.last_delivery_at is not None
    assert blocked.active is False
    assert blocked.unsubscribed_at is not None
    assert db.commits == 2

@pytest.mark.asyncio
async def test_telegram_polling_advances_offset_and_processes_start(monkeypatch):
    integration = SimpleNamespace(
        id=uuid4(),
        encrypted_secret=b"encrypted",
        non_sensitive_config={
            **notifications.default_config(),
            "telegram_polling_initialized": True,
            "telegram_bot_username": "FecoClimaBot",
            "telegram_bot_name": "FECOCLIMA",
            "telegram_update_offset": 10,
        },
    )
    calls = []

    async def fake_request(_token, method, payload=None, **_kwargs):
        calls.append((method, payload or {}))
        if method == "getUpdates":
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 11,
                        "message": {
                            "chat": {"id": 123, "type": "private", "first_name": "Ana"},
                            "text": "/start fecoclima",
                        },
                    }
                ],
            }
        raise AssertionError(f"Unexpected method: {method}")

    async def fake_process(_db, _integration, _token, update):
        return {"action": "SUBSCRIBED", "chat_id": str(update["message"]["chat"]["id"])}

    class FakeDb:
        commits = 0

        async def commit(self):
            self.commits += 1

    monkeypatch.setattr(notifications, "telegram_bot_token", lambda _integration: "token")
    monkeypatch.setattr(notifications, "telegram_request", fake_request)
    monkeypatch.setattr(notifications, "process_telegram_update", fake_process)

    db = FakeDb()
    result = await notifications.sync_telegram_subscriptions(db, integration)

    assert result["processed"] == 1
    assert calls[0][0] == "getUpdates"
    assert calls[0][1]["offset"] == 10
    assert integration.non_sensitive_config["telegram_update_offset"] == 12
    assert db.commits == 1
