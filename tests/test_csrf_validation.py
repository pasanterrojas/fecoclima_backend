import asyncio
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.api.deps import require_csrf
from app.core.config import settings


def check(cookie: str, header: str | None, method: str = 'POST'):
    request = Request({'type':'http', 'method':method, 'path':'/api/v1/stations', 'headers':[(b'cookie', cookie.encode())]})
    return asyncio.run(require_csrf(request, header))


def test_csrf_accepts_matching_configured_cookie():
    check(f'{settings.csrf_cookie_name}=test-token', 'test-token')


@pytest.mark.parametrize('cookie,header', [('',None), ('csrf_token=token',None), ('', 'token'), ('csrf_token=token','other')])
def test_csrf_rejects_missing_or_mismatched_tokens(cookie, header):
    with pytest.raises(HTTPException) as result:
        check(cookie, header)
    assert result.value.status_code == 403


def test_csrf_ignores_read_only_requests():
    check('', None, 'GET')
