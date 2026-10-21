from __future__ import annotations

import asyncio
from getpass import getpass

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import Role, User
from app.db.session import AsyncSessionLocal, engine
from app.core.config import settings


def read_new_password() -> str:
    password = getpass("Nueva contraseña (no se mostrará): ")
    confirmation = getpass("Repítala: ")
    if password != confirmation:
        raise SystemExit("Las contraseñas no coinciden.")
    if len(password) < 16:
        raise SystemExit("Use al menos 16 caracteres.")
    if not (
        any(char.islower() for char in password)
        and any(char.isupper() for char in password)
        and any(char.isdigit() for char in password)
    ):
        raise SystemExit("Incluya mayúsculas, minúsculas y números.")
    return password


async def main() -> None:
    password = read_new_password()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User, Role.code)
            .join(Role, Role.id == User.role_id)
            .where(
                User.email == settings.superadmin_email.lower(),
                User.deleted_at.is_(None),
            )
            .with_for_update()
        )
        row = result.first()
        if row is None:
            raise SystemExit(
                "No existe una cuenta activa con SUPERADMIN_EMAIL. "
                "Revise el correo configurado; no se creó ningún usuario."
            )
        user, role_code = row
        if role_code != "superadmin":
            raise SystemExit(
                "SUPERADMIN_EMAIL corresponde a una cuenta que no tiene el rol "
                "superadmin. No se modificó la cuenta."
            )
        if not user.active:
            raise SystemExit(
                "La cuenta superadmin está desactivada. Actívela desde la base "
                "de datos antes de iniciar sesión."
            )

        user.password_hash = hash_password(password)
        user.failed_login_attempts = 0
        user.locked_until = None
        user.token_version += 1
        await db.commit()
        print(f"Acceso restablecido para {user.email}. Las sesiones anteriores fueron revocadas.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
