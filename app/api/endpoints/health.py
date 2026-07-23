from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from app.api.deps import DbSession

router = APIRouter(tags=["Salud"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "fecoclima-ia-api"}


@router.get("/ready")
async def ready(db: DbSession) -> dict:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Base de datos no disponible") from exc
    return {"status": "ready", "database": "ok"}
