from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid
from email.message import EmailMessage

import aiosmtplib
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require_csrf
from app.core.config import settings
from app.core.security import (
    access_token,
    csrf_token,
    decode_token,
    hash_password,
    random_token,
    refresh_token,
    token_digest,
    verify_password,
)
from app.db.models import PasswordResetToken, Role, User
from app.schemas.auth import LoginRequest, PasswordResetConfirm, PasswordResetRequest
from app.services.audit import write_audit

router = APIRouter(prefix="/auth", tags=["Autenticación"])


def _set_auth_cookies(response: Response, user: User, role_code: str) -> None:
    common = {"secure": settings.cookie_secure, "samesite": "lax", "domain": settings.cookie_domain}
    response.set_cookie("access_token", access_token(str(user.id), role_code, user.token_version), httponly=True, max_age=settings.access_token_minutes * 60, path="/", **common)
    response.set_cookie("refresh_token", refresh_token(str(user.id), user.token_version), httponly=True, max_age=settings.refresh_token_days * 86400, path=f"{settings.api_v1_prefix}/auth", **common)
    response.set_cookie("csrf_token", csrf_token(), httponly=False, max_age=settings.refresh_token_days * 86400, path="/", **common)


async def _user_response(db: DbSession, user: User) -> dict:
    role = await db.get(Role, user.role_id)
    return {"id": str(user.id), "email": user.email, "name": user.name, "role": role.code if role else "consulta"}


@router.post("/login")
async def login(payload: LoginRequest, response: Response, request: Request, db: DbSession) -> dict:
    now = datetime.now(UTC)
    user = await db.scalar(select(User).where(User.email == payload.email.lower(), User.deleted_at.is_(None)))
    if not user:
        await write_audit(db, action="AUTH_LOGIN_FAILED", request=request, extra={"email": payload.email.lower(), "reason": "unknown_user"})
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña incorrectos")
    if not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="La cuenta está desactivada")
    if user.locked_until and user.locked_until > now:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Cuenta temporalmente bloqueada. Intente más tarde")
    if not verify_password(payload.password, user.password_hash):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= 5:
            user.locked_until = now + timedelta(minutes=15)
            user.failed_login_attempts = 0
        await write_audit(db, action="AUTH_LOGIN_FAILED", user=user, request=request, entity_type="user", entity_id=str(user.id), extra={"reason": "invalid_password"})
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña incorrectos")
    role = await db.get(Role, user.role_id)
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    await write_audit(db, action="AUTH_LOGIN_SUCCESS", user=user, request=request, entity_type="user", entity_id=str(user.id))
    await db.commit()
    _set_auth_cookies(response, user, role.code)
    return await _user_response(db, user)


@router.get("/me")
async def me(user: CurrentUser, db: DbSession) -> dict:
    return await _user_response(db, user)


@router.post("/refresh")
async def refresh(response: Response, request: Request, db: DbSession) -> dict:
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="Refresh token no disponible")
    try:
        payload = decode_token(token, "refresh")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = await db.get(User, uuid.UUID(payload.get("sub")))
    if not user or not user.active or user.deleted_at or payload.get("tv") != user.token_version:
        raise HTTPException(status_code=401, detail="Usuario o sesión no disponible")
    role = await db.get(Role, user.role_id)
    _set_auth_cookies(response, user, role.code)
    return await _user_response(db, user)


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(response: Response, request: Request, db: DbSession, user: CurrentUser) -> dict:
    await write_audit(db, action="AUTH_LOGOUT", user=user, request=request, entity_type="user", entity_id=str(user.id))
    await db.commit()
    response.delete_cookie("access_token", path="/", domain=settings.cookie_domain)
    response.delete_cookie("refresh_token", path=f"{settings.api_v1_prefix}/auth", domain=settings.cookie_domain)
    response.delete_cookie("csrf_token", path="/", domain=settings.cookie_domain)
    return {"ok": True}


async def _send_reset_email(email: str, token: str) -> None:
    if not settings.smtp_host:
        return
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = email
    message["Subject"] = "Restablecer contraseña · FECOCLIMA IA"
    url = f"{settings.frontend_base_url.rstrip('/')}/admin/restablecer-contrasena?token={token}"
    message.set_content(f"Use el siguiente enlace una sola vez para restablecer su contraseña. Expira en 30 minutos:\n\n{url}")
    await aiosmtplib.send(message, hostname=settings.smtp_host, port=settings.smtp_port, username=settings.smtp_username, password=settings.smtp_password, start_tls=settings.smtp_starttls)


@router.post("/password-reset/request")
async def request_password_reset(payload: PasswordResetRequest, db: DbSession) -> dict:
    user = await db.scalar(select(User).where(User.email == payload.email.lower(), User.active.is_(True), User.deleted_at.is_(None)))
    if user:
        raw = random_token()
        db.add(PasswordResetToken(user_id=user.id, token_hash=token_digest(raw), expires_at=datetime.now(UTC) + timedelta(minutes=30)))
        await db.commit()
        await _send_reset_email(user.email, raw)
        if settings.environment == "development" and not settings.smtp_host:
            return {"ok": True, "development_token": raw}
    return {"ok": True}


@router.post("/password-reset/confirm")
async def confirm_password_reset(payload: PasswordResetConfirm, db: DbSession) -> dict:
    record = await db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_digest(payload.token), PasswordResetToken.used_at.is_(None), PasswordResetToken.expires_at > datetime.now(UTC)))
    if not record:
        raise HTTPException(status_code=400, detail="El token es inválido, ya fue usado o expiró")
    user = await db.get(User, record.user_id)
    if not user:
        raise HTTPException(status_code=400, detail="Usuario no disponible")
    user.password_hash = hash_password(payload.password)
    user.token_version += 1
    record.used_at = datetime.now(UTC)
    await db.commit()
    return {"ok": True}
