from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator




def _validate_strong_password(value: str) -> str:
    groups = [any(c.islower() for c in value), any(c.isupper() for c in value), any(c.isdigit() for c in value)]
    if sum(groups) < 3:
        raise ValueError("La contraseña debe incluir mayúsculas, minúsculas y números")
    return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=20)
    password: str = Field(min_length=12, max_length=200)

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        groups = [any(c.islower() for c in value), any(c.isupper() for c in value), any(c.isdigit() for c in value)]
        if sum(groups) < 3:
            raise ValueError("La contraseña debe incluir mayúsculas, minúsculas y números")
        return value


class UserCreate(BaseModel):
    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        return _validate_strong_password(value)

    name: str = Field(min_length=2, max_length=180)
    email: EmailStr
    role: str
    password: str = Field(min_length=12, max_length=200)
    active: bool = True


class UserUpdate(BaseModel):
    @field_validator("password", mode="before")
    @classmethod
    def normalize_password(cls, value):
        return None if value in (None, "") else value

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str | None):
        return _validate_strong_password(value) if value else value

    name: str | None = Field(default=None, min_length=2, max_length=180)
    email: EmailStr | None = None
    role: str | None = None
    password: str | None = Field(default=None, min_length=12, max_length=200)
    active: bool | None = None
