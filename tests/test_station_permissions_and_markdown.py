"""Station deletion permissions and Markdown ingestion at their API boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.deps import get_current_user
from app.api.endpoints.knowledge import ALLOWED_MIME, _safe_filename
from app.api.endpoints.stations import router
from app.core.config import settings
from app.db.models import Role
from app.db.session import get_db
from app.services.rag import extract_file_text


@pytest.mark.parametrize("role_code", ["administrador", "analista", "productor", "superadmin"])
async def test_only_superadmin_can_delete_station(role_code, monkeypatch):
    station = SimpleNamespace(id=uuid4(), active=True, deleted_at=None)
    actor = SimpleNamespace(id=uuid4(), role_id=uuid4())

    async def get_row(model, row_id):
        return SimpleNamespace(code=role_code) if model is Role else station

    db = SimpleNamespace(get=AsyncMock(side_effect=get_row), commit=AsyncMock())

    async def override_db():
        yield db

    async def override_user():
        return actor

    audit = AsyncMock()
    monkeypatch.setattr("app.api.endpoints.stations.write_audit", audit)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test.local",
        cookies={settings.csrf_cookie_name: "test-csrf"},
    ) as client:
        response = await client.delete(
            f"/stations/{station.id}", headers={"X-CSRF-Token": "test-csrf"}
        )

    if role_code == "superadmin":
        assert response.status_code == 200
        assert station.active is False
        assert station.deleted_at is not None
        db.commit.assert_awaited_once()
        audit.assert_awaited_once()
    else:
        assert response.status_code == 403
        assert station.active is True
        assert station.deleted_at is None
        db.commit.assert_not_awaited()
        audit.assert_not_awaited()


@pytest.mark.parametrize("mime_type", ["text/markdown", "text/x-markdown", "application/octet-stream"])
def test_markdown_upload_is_accepted_and_extracted_as_utf8(mime_type):
    assert mime_type in ALLOWED_MIME
    filename = _safe_filename("manual_trigo.MD")
    text = "# Trigo\n\nInspección de hojas y humedad del cultivo."
    assert extract_file_text(text.encode("utf-8"), mime_type, filename) == [(text, "texto")]


def test_markdown_support_does_not_accept_executable_extensions():
    with pytest.raises(HTTPException) as error:
        _safe_filename("manual.md.exe")
    assert error.value.status_code == 400
