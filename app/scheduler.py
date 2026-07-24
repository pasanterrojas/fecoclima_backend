from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.models import Station
from app.db.session import AsyncSessionLocal
from app.services.jobs import enqueue_unique

configure_logging()
logger = structlog.get_logger(__name__)


async def schedule_station_jobs() -> None:
    today = datetime.now(UTC).date()
    date_from = today - timedelta(days=3)
    scheduled = 0
    async with AsyncSessionLocal() as db:
        stations = list(
            (
                await db.scalars(
                    select(Station).where(Station.active.is_(True), Station.deleted_at.is_(None))
                )
            ).all()
        )
        for station in stations:
            if station.source_type == "FECOCLIMA_API" and station.external_guid:
                job = await enqueue_unique(
                    db,
                    "STATION_SYNC",
                    {
                        "station_id": str(station.id),
                        "date_from": date_from.isoformat(),
                        "date_to": today.isoformat(),
                    },
                    priority=20,
                )
                scheduled += int(job is not None)
            if station.latitude is not None and station.longitude is not None:
                job = await enqueue_unique(
                    db,
                    "FORECAST_REFRESH",
                    {"station_id": str(station.id)},
                    priority=80,
                )
                scheduled += int(job is not None)
                satellite_job = await enqueue_unique(
                    db,
                    "SATELLITE_SYNC",
                    {"station_id": str(station.id)},
                    priority=120,
                )
                scheduled += int(satellite_job is not None)
    logger.info("scheduled_station_jobs", stations=len(stations), jobs=scheduled)


async def schedule_cleanup() -> None:
    async with AsyncSessionLocal() as db:
        await enqueue_unique(db, "CLEANUP_FILES", {}, priority=200)


async def schedule_backup() -> None:
    if not settings.scheduler_backup_enabled:
        return
    async with AsyncSessionLocal() as db:
        await enqueue_unique(
            db,
            "DATABASE_BACKUP",
            {"label": "automatico"},
            priority=150,
        )


async def schedule_daily_digest() -> None:
    """La configuración del CMS decide hora local y evita duplicados diarios."""
    async with AsyncSessionLocal() as db:
        hour_key = datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
        await enqueue_unique(
            db,
            "DAILY_PRODUCER_DIGEST",
            {"force": False, "hour_key": hour_key},
            priority=40,
        )


async def main() -> None:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        schedule_station_jobs,
        "interval",
        minutes=settings.scheduler_sync_minutes,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=10),
    )
    scheduler.add_job(schedule_cleanup, "cron", hour=3, minute=20, max_instances=1, coalesce=True)
    scheduler.add_job(
        schedule_daily_digest,
        "interval",
        minutes=30,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=45),
    )
    scheduler.add_job(
        schedule_backup,
        "cron",
        hour=settings.scheduler_backup_hour_utc,
        minute=0,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "scheduler_started",
        sync_minutes=settings.scheduler_sync_minutes,
        backup_enabled=settings.scheduler_backup_enabled,
    )
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
