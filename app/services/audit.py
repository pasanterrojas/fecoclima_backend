from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, User


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
            before_data=before,
            after_data=after,
            extra_data=extra or {},
        )
    )
