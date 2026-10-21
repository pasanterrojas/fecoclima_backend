import httpx
from app.main import app
from app.core.config import settings


async def test_application_health_includes_request_id():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
        response = await client.get('/health')
    assert response.status_code == 200
    assert response.headers.get('X-Request-ID')


async def test_parcel_session_header_is_allowed_by_cors():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
        response = await client.options('/api/v1/parcels',headers={'Origin':settings.cors_origins[0], 'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'X-Parcel-Session,X-CSRF-Token'})
    assert response.status_code == 200
    assert 'x-parcel-session' in response.headers['access-control-allow-headers'].lower()


async def test_chat_browser_header_is_allowed_by_cors():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        response = await client.options('/api/v1/parcels/analyses/00000000-0000-4000-8000-000000000001/chat', headers={
            'Origin': settings.cors_origins[0], 'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'X-Chat-Browser',
        })
    assert response.status_code == 200
    assert 'x-chat-browser' in response.headers['access-control-allow-headers'].lower()
