from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, User


def _serialize_datetime(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    """Convierte el contenido de auditoría a valores compatibles con JSONB.

    Las capturas de auditoría pueden contener datetime, date, UUID, Enum,
    Decimal y modelos Pydantic. El serializador JSON estándar de SQLAlchemy
    no puede guardar directamente esos objetos de Python.
    """
    if value is None:
        return None
    return jsonable_encoder(value, custom_encoder={datetime: _serialize_datetime})


async def write_audit(
    db: AsyncSession,
    *,
    action: str,
    user: User | None = None,
    request: Request | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    extra: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=user.id if user else None,
            actor_email=user.email if user else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent")[:500] if request else None,
            before_data=_json_safe(before),
            after_data=_json_safe(after),
            extra_data=_json_safe(extra or {}),
        )
    )
