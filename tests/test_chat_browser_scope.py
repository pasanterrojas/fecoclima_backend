from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.endpoints.parcels import _chat_browser_id
from app.core.config import settings
from app.services.plot_chat import public_messages, store_images


@pytest.mark.parametrize('value', [None, '', 'invalid', '00000000-0000-0000-0000-000000000000'])
def test_chat_does_not_fall_back_to_unscoped_history(value):
    headers = [(b'x-chat-browser', value.encode())] if value is not None else []
    request = Request({'type': 'http', 'headers': headers})
    with pytest.raises(HTTPException) as error:
        _chat_browser_id(request)
    assert error.value.status_code == 422


def test_chat_browser_scope_accepts_uuid():
    browser_id = uuid4()
    request = Request({'type': 'http', 'headers': [(b'x-chat-browser', str(browser_id).encode())]})
    assert _chat_browser_id(request) == browser_id


def test_private_chat_images_are_separated_by_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'storage_path', tmp_path)
    analysis_id, user_id, first_browser, second_browser = (uuid4() for _ in range(4))
    image = {'filename': 'campo.png', 'content_type': 'image/png', 'content': b'fixture'}
    first = store_images([image], analysis_id, user_id, first_browser)[0]
    second = store_images([image], analysis_id, user_id, second_browser)[0]
    assert str(first_browser) in first['storage_path']
    assert str(second_browser) in second['storage_path']
    assert first['storage_path'] != second['storage_path']
    rendered = public_messages([{'role': 'user', 'content': 'Foto', 'images': [first]}])
    assert 'storage_path' not in rendered[0]['images'][0]
