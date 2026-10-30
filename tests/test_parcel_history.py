"""Rolling producer imagery and permanent administrator access use separate scopes."""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.api.endpoints import admin
from app.core.config import settings
from app.db.models import Plot, PlotAnalysis, Role, SatelliteObservation
from test_parcel_imagery import imagery, integration_setup  # noqa: F401
from test_plot_integration import DATABASE, POLYGON, payload

pytestmark = pytest.mark.skipif(not DATABASE, reason='Requires disposable FECOCLIMA_TEST_DB_URL')
PROVIDER = 'COPERNICUS_DATA_SPACE_SENTINEL_2_L2A'


async def observations(db, plot_id, count=4):
    rows = []
    for index in range(count):
        row = SatelliteObservation(
            plot_id=plot_id, captured_at=datetime.now(UTC) - timedelta(days=count-index),
            provider=PROVIDER, ndvi=0.2 + index / 10,
            raw_payload={'processing_geometry': POLYGON, 'indices': {'ndvi': 0.2 + index / 10}},
        )
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows


async def test_preview_and_saved_parcel_roll_to_latest_three_without_deleting_observations(imagery):
    db, user, crop, station, client, preview = imagery
    polygon = (await preview.post('/public/parcel-imagery', json={'geojson': POLYGON})).json()
    plot_id = UUID(polygon['id'])
    rows = await observations(db, plot_id)
    # A newer failed scene must not push an available image out of the selector.
    db.add(SatelliteObservation(plot_id=plot_id, captured_at=datetime.now(UTC), provider=PROVIDER,
                               raw_payload={'processing_geometry': POLYGON}))
    await db.commit()
    state = (await preview.get(f'/public/parcel-imagery/{plot_id}')).json()
    assert [item['id'] for item in state['history']] == [str(row.id) for row in reversed(rows[1:])]
    assert state['satellite']['id'] == str(rows[-1].id)
    assert (await client.post('/parcels', json=payload(crop.id))).json()['id'] == str(plot_id)
    response = await client.get(f'/parcels/{plot_id}/satellite/history')
    assert response.json()['limit'] == 3
    assert [item['id'] for item in response.json()['items']] == [str(row.id) for row in reversed(rows[1:])]
    newest = (await observations(db, plot_id, count=1))[0]
    newest.captured_at = datetime.now(UTC) + timedelta(seconds=1)
    await db.commit()
    window = (await client.get(f'/parcels/{plot_id}/satellite/history')).json()['items']
    assert [item['id'] for item in window] == [str(newest.id), str(rows[3].id), str(rows[2].id)]
    assert await db.scalar(select(func.count()).select_from(SatelliteObservation).where(SatelliteObservation.plot_id == plot_id)) == 6


async def test_deleted_parcel_keeps_complete_images_geometry_and_analysis_references_for_admin(imagery):
    db, user, crop, station, client, preview = imagery
    client._transport.app.include_router(admin.router)
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    plot_id = UUID(created['id'])
    rows = await observations(db, plot_id)
    folder = settings.storage_path / 'satellite' / str(rows[0].id)
    folder.mkdir(parents=True)
    rgb, ndvi = folder / 'image.jpg', folder / 'ndvi.png'
    rgb.write_bytes(b'original RGB'); ndvi.write_bytes(b'original NDVI')
    old_geometry = {'type': 'Polygon', 'coordinates': [[[-55.02, -25.02], [-55, -25.02], [-55, -25], [-55.02, -25.02]]]}
    rows[0].raw_payload = {**rows[0].raw_payload, 'processing_geometry': old_geometry,
                           'local_image_path': str(rgb), 'local_index_image_paths': {'ndvi': str(ndvi)}}
    analysis = PlotAnalysis(plot_id=plot_id, created_by=user.id, reference_at=datetime.now(UTC),
                            context={'satellite': {'id': str(rows[0].id)}}, agent_results={})
    db.add(analysis)
    await db.commit()
    assert (await client.delete(f'/parcels/{plot_id}')).status_code == 200
    root = f'/admin/parcel-history/{plot_id}'
    assert (await client.get(root)).status_code == 403
    role = await db.get(Role, user.role_id)
    role.code = 'administrador'
    await db.commit()
    record = (await client.get(root)).json()
    assert record['deleted_at'] and record['active'] is False
    page = (await client.get(f'{root}/images', params={'limit': 2})).json()
    assert page['total'] == 4 and len(page['items']) == 2
    older = (await client.get(f'{root}/images', params={'offset': 2})).json()['items'][-1]
    assert older['processing_geometry'] == old_geometry
    assert older['used_in_analyses'] == [str(analysis.id)]
    assert (await client.get(older['source_url'])).content == b'original RGB'
    downloaded = await client.get(older['index_images']['ndvi'], params={'download': True})
    assert downloaded.content == b'original NDVI'
    assert 'attachment' in downloaded.headers['content-disposition']
    assert downloaded.headers['cache-control'] == 'private, no-store'
    assert (await client.get(f'{root}/images/{uuid4()}/image.jpg')).status_code == 404
    assert (await client.get(f'{root}/images/{rows[0].id}/ndvi')).status_code == 404
    assert (await client.get(f'/parcels/{plot_id}/satellite/history')).status_code == 404
    await db.refresh(await db.get(Plot, plot_id))
    assert (await db.get(Plot, plot_id)).geojson == POLYGON
    listing = (await client.get('/admin/parcel-history', params={'state': 'deleted', 'search': created['name']})).json()
    assert listing['total'] == 1 and listing['items'][0]['scene_count'] == 4


async def test_admin_registry_includes_exploratory_polygons_and_is_private(imagery):
    db, user, crop, station, client, preview = imagery
    client._transport.app.include_router(admin.router)
    polygon = (await preview.post('/public/parcel-imagery', json={'geojson': POLYGON})).json()
    assert (await client.get('/admin/parcel-history')).status_code == 403
    role = await db.get(Role, user.role_id)
    role.code = 'administrador'
    await db.commit()
    result = (await client.get('/admin/parcel-history', params={'state': 'preview'})).json()
    assert result['total'] == 1 and result['items'][0]['id'] == polygon['id']
    assert (await client.get('/admin/parcel-history', params={'limit': 101})).status_code == 422
    role.code = 'productor'
    await db.commit()
    assert (await client.get(f"/admin/parcel-history/{polygon['id']}/images")).status_code == 403
