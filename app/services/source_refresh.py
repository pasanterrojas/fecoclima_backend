from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import IntegrationLog, Station, WeatherReading
from app.services.weather import sync_fecoclima_station


async def _latest_reading(db: AsyncSession, station_id) -> WeatherReading | None:
    return await db.scalar(
        select(WeatherReading)
        .where(WeatherReading.station_id == station_id)
        .order_by(WeatherReading.sampled_at.desc())
        .limit(1)
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def refresh_station_source_if_needed(
    db: AsyncSession,
    station: Station,
    *,
    requested: bool,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh the station source only when the user explicitly requests analysis.

    The dashboard must remain usable when the upstream FECOCLIMA service is slow
    or unavailable, so failures are returned as status metadata instead of
    aborting the deterministic analysis over cached readings.
    """

    latest_before = await _latest_reading(db, station.id)
    source_type = str(station.source_type or "").upper()
    supported = source_type in {"FECOCLIMA", "FECOCLIMA_API"}
    result: dict[str, Any] = {
        "provider": "FECOCLIMA" if supported else source_type or "MANUAL",
        "supported": supported,
        "requested": requested,
        "attempted": False,
        "status": "NOT_REQUESTED" if not requested else "SKIPPED",
        "reason": None,
        "error": None,
        "last_synced_at": _iso(station.last_synced_at),
        "latest_before": _iso(latest_before.sampled_at if latest_before else None),
        "latest_after": _iso(latest_before.sampled_at if latest_before else None),
        "received": 0,
        "created": 0,
        "updated": 0,
        "sync_from": None,
        "sync_to": None,
        "sync_timezone": station.timezone or "America/Asuncion",
        "received_definition": "unique_samples_returned_for_sync_window",
    }

    if not requested:
        return result
    if not supported:
        result["reason"] = "station_source_is_not_fecoclima"
        return result
    if not station.external_guid:
        result.update(status="MISCONFIGURED", reason="missing_external_guid")
        return result
    if not settings.fecoclima_api_key:
        result.update(status="MISCONFIGURED", reason="missing_fecoclima_api_key")
        return result

    now = datetime.now(UTC)
    cooldown = timedelta(minutes=max(1, settings.public_analysis_sync_cooldown_minutes))
    if not force and station.last_synced_at and station.last_synced_at >= now - cooldown:
        result.update(status="CURRENT", reason="sync_cooldown", last_synced_at=_iso(station.last_synced_at))
        return result

    running = await db.scalar(
        select(IntegrationLog.id)
        .where(
            IntegrationLog.station_id == station.id,
            IntegrationLog.operation == "FECOCLIMA_SYNC",
            IntegrationLog.status == "RUNNING",
            IntegrationLog.started_at >= now - timedelta(minutes=max(5, settings.job_lock_timeout_minutes)),
        )
        .limit(1)
    )
    if running:
        result.update(status="IN_PROGRESS", reason="another_sync_is_running")
        return result

    timezone = ZoneInfo(station.timezone or "America/Asuncion")
    date_to = datetime.now(timezone).date()
    date_from = date_to - timedelta(days=max(1, settings.public_analysis_sync_lookback_days))
    # FECOCLIMA accepts year/month blocks rather than timestamps. The adapter
    # narrows that response to these local calendar dates and deduplicates by
    # sampled_at. This window intentionally covers the exact rolling analysis
    # window even when it starts part-way through the first calendar date.
    result.update(
        sync_from=date_from.isoformat(),
        sync_to=date_to.isoformat(),
        sync_timezone=str(timezone),
    )
    result["attempted"] = True
    try:
        sync_result = await sync_fecoclima_station(db, station.id, date_from, date_to)
        await db.refresh(station)
        latest_after = await _latest_reading(db, station.id)
        result.update(
            status="COMPLETED",
            reason="source_refreshed",
            last_synced_at=_iso(station.last_synced_at),
            latest_after=_iso(latest_after.sampled_at if latest_after else None),
            received=int(sync_result.get("received", 0)),
            created=int(sync_result.get("created", 0)),
            updated=int(sync_result.get("updated", 0)),
        )
    except Exception as exc:  # The cached dashboard must still be returned.
        latest_after = await _latest_reading(db, station.id)
        result.update(
            status="FAILED",
            reason="upstream_request_failed",
            error=str(exc)[:1500],
            latest_after=_iso(latest_after.sampled_at if latest_after else None),
            last_synced_at=_iso(station.last_synced_at),
        )
    return result
