"""Local storage and redaction checks; no database or external AI needed."""
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services.plot_chat import (
    attachment_text,
    public_messages,
    store_attachments,
)


def test_private_text_attachment_and_public_url(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    analysis, actor, browser = uuid4(), uuid4(), uuid4()
    results = store_attachments([
        {"filename": "mis-notas.txt", "content_type": "text/plain", "content": b"Hola desde mi cultivo"}
    ], analysis, actor, browser)
    assert len(results) == 1
    assert str(browser) in results[0]["storage_path"]
    assert "Hola desde mi cultivo" in attachment_text(results)
    exposed = public_messages([{"role": "user", "content": "", "attachments": results}], str(analysis))
    file = exposed[0]["attachments"][0]
    assert "storage_path" not in file
    assert file["url"].startswith(f"/parcels/analyses/{analysis}/chat/media/")
    assert file["url"].endswith(".txt")


def test_pdf_magic_required(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    with pytest.raises(ValueError, match="PDF"):
        store_attachments([{"filename": "falso.pdf", "content_type": "application/pdf", "content": b"Not a PDF"}], uuid4(), uuid4(), uuid4())


def test_audio_is_preserved_without_false_transcription(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    files = store_attachments([{"filename": "nota.webm", "content_type": "audio/webm", "content": b"test audio"}], uuid4(), uuid4(), uuid4())
    assert files[0]["content_type"] == "audio/webm"
    assert not attachment_text(files)
