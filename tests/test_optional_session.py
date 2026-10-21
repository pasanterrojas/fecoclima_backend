from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.endpoints import auth
from app.core.config import settings
from app.db.session import get_db


@pytest.mark.parametrize("cookies,refresh_available", [({}, False), ({"refresh": "expired"}, True)])
async def test_guest_session_is_successful_without_refresh_attempt(cookies, refresh_available):
    app = FastAPI()
    app.include_router(auth.router)

    async def database():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if cookies:
            client.cookies.set(settings.refresh_cookie_name, "expired")
        response = await client.get("/auth/session")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json() == {"user": None, "refresh_available": refresh_available}
        assert (await client.get("/auth/me")).status_code == 401


async def test_invalid_access_session_remains_guest(monkeypatch):
    app = FastAPI()
    app.include_router(auth.router)

    async def database():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(auth, "get_current_user", AsyncMock(side_effect=HTTPException(401)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(settings.access_cookie_name, "invalid")
        response = await client.get("/auth/session")
    assert response.status_code == 200
    assert response.json()["user"] is None
