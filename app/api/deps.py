from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decode_token
from app.db.models import Role, User
from app.db.session import get_db

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    db: DbSession,
    request: Request,
) -> User:
    access_token = request.cookies.get(settings.access_cookie_name)
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión no autenticada")
    try:
        payload = decode_token(access_token, "access")
        user_id = uuid.UUID(payload["sub"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión inválida") from None
    user = await db.scalar(select(User).where(User.id == user_id, User.active.is_(True), User.deleted_at.is_(None)))
    if not user or payload.get("tv") != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario o sesión no disponible")
    if user.locked_until and user.locked_until > datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Cuenta temporalmente bloqueada")
    return user

CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_csrf(
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    csrf_token = request.cookies.get(settings.csrf_cookie_name)
    if not csrf_token or not x_csrf_token or csrf_token != x_csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token CSRF inválido")


async def get_role(db: DbSession, user: CurrentUser) -> Role:
    role = await db.get(Role, user.role_id)
    if not role:
        raise HTTPException(status_code=403, detail="Rol no disponible")
    return role


def require_roles(*allowed: str):
    async def dependency(db: DbSession, user: CurrentUser, _: Annotated[None, Depends(require_csrf)]) -> User:
        role = await db.get(Role, user.role_id)
        if not role or role.code not in allowed:
            raise HTTPException(status_code=403, detail="No tiene permisos para esta operación")
        return user
    return dependency


AdminUser = Annotated[User, Depends(require_roles("superadmin", "administrador"))]
SuperAdminUser = Annotated[User, Depends(require_roles("superadmin"))]
AnalystUser = Annotated[User, Depends(require_roles("superadmin", "administrador", "analista"))]
