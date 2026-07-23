from __future__ import annotations

import uuid
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession
from app.core.security import hash_password
from app.db.models import Role, User
from app.schemas.auth import UserCreate, UserUpdate
from app.services.audit import write_audit
from app.services.serialization import model_dict

router = APIRouter(prefix="/users", tags=["Usuarios"])


async def _actor_role(db: DbSession, actor: User) -> str:
    role = await db.get(Role, actor.role_id)
    return role.code if role else "consulta"


async def _serialize(db: DbSession, user: User) -> dict:
    role = await db.get(Role, user.role_id)
    return {**model_dict(user, exclude={"password_hash", "token_version", "deleted_at"}), "role": role.code if role else None}


@router.get("")
async def list_users(db: DbSession, _: AdminUser, limit: int = 100, offset: int = 0) -> dict:
    rows = (await db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc()).offset(offset).limit(min(limit, 250)))).all()
    return {"items": [await _serialize(db, row) for row in rows], "limit": limit, "offset": offset}


@router.post("")
async def create_user(payload: UserCreate, request: Request, db: DbSession, actor: AdminUser) -> dict:
    if await db.scalar(select(User).where(User.email == payload.email.lower(), User.deleted_at.is_(None))):
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo")
    if payload.role == "superadmin" and await _actor_role(db, actor) != "superadmin":
        raise HTTPException(status_code=403, detail="Solo un superadministrador puede asignar ese rol")
    role = await db.scalar(select(Role).where(Role.code == payload.role))
    if not role:
        raise HTTPException(status_code=400, detail="Rol inválido")
    user = User(name=payload.name, email=payload.email.lower(), password_hash=hash_password(payload.password), role_id=role.id, active=payload.active)
    db.add(user)
    await db.flush()
    await write_audit(db, action="USER_CREATE", user=actor, request=request, entity_type="user", entity_id=str(user.id), after={"email": user.email, "role": role.code, "active": user.active})
    await db.commit()
    return await _serialize(db, user)


@router.put("/{user_id}")
async def update_user(user_id: uuid.UUID, payload: UserUpdate, request: Request, db: DbSession, actor: AdminUser) -> dict:
    user = await db.get(User, user_id)
    if not user or user.deleted_at:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    before = await _serialize(db, user)
    actor_role = await _actor_role(db, actor)
    target_role = await _actor_role(db, user)
    if target_role == "superadmin" and actor_role != "superadmin":
        raise HTTPException(status_code=403, detail="Solo un superadministrador puede modificar esta cuenta")
    data = payload.model_dump(exclude_unset=True)
    if data.get("role") == "superadmin" and actor_role != "superadmin":
        raise HTTPException(status_code=403, detail="Solo un superadministrador puede asignar ese rol")
    if "email" in data:
        duplicate = await db.scalar(select(User).where(User.email == str(data["email"]).lower(), User.id != user.id, User.deleted_at.is_(None)))
        if duplicate:
            raise HTTPException(status_code=409, detail="El correo ya está en uso")
        user.email = str(data.pop("email")).lower()
    if "role" in data:
        role = await db.scalar(select(Role).where(Role.code == data.pop("role")))
        if not role:
            raise HTTPException(status_code=400, detail="Rol inválido")
        user.role_id = role.id
    if data.get("password"):
        user.password_hash = hash_password(data.pop("password"))
        user.token_version += 1
    else:
        data.pop("password", None)
    for key, value in data.items():
        setattr(user, key, value)
    await db.flush()
    after = await _serialize(db, user)
    await write_audit(db, action="USER_UPDATE", user=actor, request=request, entity_type="user", entity_id=str(user.id), before=before, after=after)
    await db.commit()
    return after


@router.delete("/{user_id}")
async def delete_user(user_id: uuid.UUID, request: Request, db: DbSession, actor: AdminUser) -> dict:
    user = await db.get(User, user_id)
    if not user or user.deleted_at:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="No puede eliminar su propio usuario")
    if await _actor_role(db, user) == "superadmin" and await _actor_role(db, actor) != "superadmin":
        raise HTTPException(status_code=403, detail="Solo un superadministrador puede eliminar esta cuenta")
    user.active = False
    from datetime import UTC, datetime
    user.deleted_at = datetime.now(UTC)
    await write_audit(db, action="USER_DELETE", user=actor, request=request, entity_type="user", entity_id=str(user.id), before={"email": user.email})
    await db.commit()
    return {"ok": True}
