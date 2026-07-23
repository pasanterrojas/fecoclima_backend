from __future__ import annotations

import asyncio

import structlog

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import AsyncSessionLocal
from app.services.jobs import (
    claim_job,
    complete_job,
    fail_job,
    handle_job,
    release_stale_jobs,
    worker_identity,
)

configure_logging()
logger = structlog.get_logger(__name__)


async def run() -> None:
    name = worker_identity()
    logger.info("worker_started", worker=name)
    loops = 0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                loops += 1
                if loops == 1 or loops % 150 == 0:
                    released = await release_stale_jobs(db)
                    if released:
                        logger.warning("stale_jobs_released", count=released)
                job = await claim_job(db, name)
                if not job:
                    await asyncio.sleep(settings.worker_poll_seconds)
                    continue
                logger.info("job_started", job_id=str(job.id), job_type=job.job_type, attempt=job.attempts)
                try:
                    result = await handle_job(db, job)
                    await complete_job(db, job, result)
                    logger.info("job_completed", job_id=str(job.id), result=result)
                except Exception as exc:
                    logger.exception("job_failed", job_id=str(job.id), error=str(exc))
                    await fail_job(db, job, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("worker_loop_error", error=str(exc))
            await asyncio.sleep(max(5, settings.worker_poll_seconds))


if __name__ == "__main__":
    asyncio.run(run())
