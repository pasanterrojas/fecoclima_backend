from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DatabaseBackup


def _sync_database_url() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _safe_label(value: str | None) -> str:
    if not value:
        return "manual"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:60] or "manual"


async def create_database_backup(db: AsyncSession, responsible_id: UUID | None, label: str | None = None) -> DatabaseBackup:
    row = DatabaseBackup(status="PROCESSING", responsible_id=responsible_id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    directory = settings.storage_path / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"fecoclima_{timestamp}_{_safe_label(label)}_{str(row.id)[:8]}.dump"
    try:
        env = os.environ.copy()
        process = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(path),
            _sync_database_url(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[-3000:] or "pg_dump terminó con error")
        row.status = "COMPLETED"
        row.size_bytes = path.stat().st_size
        row.storage_location = str(path)
        row.error_message = None
    except Exception as exc:
        path.unlink(missing_ok=True)
        row.status = "FAILED"
        row.error_message = str(exc)[:3000]
    await db.commit()
    await db.refresh(row)
    return row


async def verify_backup_file(db: AsyncSession, backup_id: UUID) -> DatabaseBackup:
    row = await db.get(DatabaseBackup, backup_id)
    if not row:
        raise ValueError("Respaldo no encontrado")
    if not row.storage_location or not Path(row.storage_location).is_file():
        row.restore_test_status = "FILE_MISSING"
        row.restore_tested_at = datetime.now(UTC)
        await db.commit()
        return row
    process = await asyncio.create_subprocess_exec(
        "pg_restore",
        "--list",
        row.storage_location,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    row.restore_tested_at = datetime.now(UTC)
    if process.returncode == 0 and stdout:
        row.restore_test_status = "VALID_ARCHIVE"
        row.error_message = None
    else:
        row.restore_test_status = "INVALID_ARCHIVE"
        row.error_message = stderr.decode("utf-8", errors="replace")[-3000:]
    await db.commit()
    return row
