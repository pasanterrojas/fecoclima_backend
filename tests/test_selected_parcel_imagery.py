"""The selected optical scene supplies AI evidence, not whichever scene arrives last."""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.db.models import BackgroundJob, Plot, SatelliteObservation
from app.api.endpoints import public_parcels
from app.core.security import create_token
from app.db.session import get_db
from test_parcel_history import observations
from test_parcel_imagery import imagery, integration_setup  # noqa: F401
from test_plot_integration import DATABASE, POLYGON, payload

pytestmark = pytest.mark.skipif(not DATABASE, reason='Requires disposable FECOCLIMA_TEST_DB_URL')


async def test_public_browser_can_fetch_images_and_analyze_its_selected_scene(imagery):
    db, user, crop, station, _, preview = imagery
    browser_id = UUID(preview.headers['X-Parcel-Session'])
    app = FastAPI()
    app.include_router(public_parcels.router)
    async def database():
        yield db
    app.dependency_overrides[get_db] = database
    headers = {'X-Parcel-Session': str(browser_id),
               'X-Parcel-Access': create_token(str(browser_id), 'parcel_browser', timedelta(hours=1))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test.local', headers=headers) as client:
        created = await client.post('/public/parcels', json=payload(crop.id))
        assert created.status_code == 200, created.text
        plot_id = UUID(created.json()['id'])
        rows = await observations(db, plot_id, 3)
        assert (await client.post(f'/public/parcels/{plot_id}/satellite/refresh')).status_code == 200
        assert len((await client.get(f'/public/parcels/{plot_id}/satellite/history')).json()['items']) == 3
        result = await client.post(f'/public/parcels/{plot_id}/analyses', json={'satellite_observation_id': str(rows[0].id)})
        assert result.status_code == 200, result.text
        assert result.json()['context']['satellite']['id'] == str(rows[0].id)
        assert result.json()['context']['satellite_selection']['mode'] == 'selected'
        job = await db.scalar(select(BackgroundJob).where(
            BackgroundJob.job_type == 'PLOT_SATELLITE_SYNC', BackgroundJob.payload == {'plot_id': str(plot_id)},
        ).order_by(BackgroundJob.created_at.desc()).limit(1))
        job.status = 'COMPLETED'
        job.result = {'sentinel_2': {'status': 'UNAVAILABLE'}}
        await db.commit()
        status = (await client.get(f'/public/parcels/{plot_id}/satellite-status')).json()
        assert status['status'] == 'FAILED' and status['satellite'] is not None
        assert 'Se conservan' in status['message']


async def test_selected_historical_image_is_the_ai_snapshot_and_cannot_be_replaced(imagery):
    db, user, crop, station, client, preview = imagery
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    plot_id = UUID(created['id'])
    rows = await observations(db, plot_id, 3)
    selected = rows[0]
    selected.raw_payload = {**selected.raw_payload, 'indices': {'ndvi': selected.ndvi, 'recl': 3.25}}
    await db.commit()
    response = await client.post(f'/parcels/{plot_id}/analyses', json={
        'window_hours': 96, 'satellite_observation_id': str(selected.id),
    })
    assert response.status_code == 200, response.text
    analysis = response.json()
    assert analysis['context']['satellite']['id'] == str(selected.id)
    assert analysis['context']['satellite']['ndvi'] == selected.ndvi
    assert analysis['context']['satellite']['indices']['recl'] == 3.25
    assert analysis['context']['satellite_selection'] == {'mode': 'selected', 'observation_id': str(selected.id)}
    assert analysis['context']['selected_window_hours'] == 96
    assert analysis['context']['satellite']['captured_at'].startswith(selected.captured_at.date().isoformat())
    newer = (await observations(db, plot_id, 1))[0]
    newer.captured_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    synced = (await client.post(f"/parcels/analyses/{analysis['id']}/satellite/sync")).json()
    assert synced['updated'] is False
    assert synced['analysis']['context']['satellite']['id'] == str(selected.id)


async def test_selection_survives_preview_promotion_and_public_update_route_exists(imagery):
    db, user, crop, station, client, preview = imagery
    polygon = (await preview.post('/public/parcel-imagery', json={'geojson': POLYGON})).json()
    plot_id = UUID(polygon['id'])
    rows = await observations(db, plot_id, 3)
    assert (await client.post('/parcels', json=payload(crop.id))).json()['id'] == str(plot_id)
    result = await client.post(f'/parcels/{plot_id}/analyses', json={'satellite_observation_id': str(rows[1].id)})
    assert result.status_code == 200, result.text
    assert result.json()['context']['satellite']['id'] == str(rows[1].id)
    refreshed = await client.post(f'/parcels/{plot_id}/satellite/refresh')
    assert refreshed.status_code == 200 and refreshed.json()['status'] == 'PROCESSING'


async def test_foreign_wrong_geometry_and_empty_scenes_cannot_supply_ai(imagery):
    db, user, crop, station, client, preview = imagery
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    plot_id = UUID(created['id'])
    foreign = Plot(name='Otro navegador', geojson=POLYGON, browser_session_id=uuid4(), active=True)
    db.add(foreign)
    await db.flush()
    foreign_scene = (await observations(db, foreign.id, 1))[0]
    invalid_geometry = SatelliteObservation(plot_id=plot_id, captured_at=datetime.now(UTC),
        provider='COPERNICUS_DATA_SPACE_SENTINEL_2_L2A', ndvi=0.7, raw_payload={'processing_geometry': {}})
    empty = SatelliteObservation(plot_id=plot_id, captured_at=datetime.now(UTC),
        provider='COPERNICUS_DATA_SPACE_SENTINEL_2_L2A', raw_payload={'processing_geometry': POLYGON})
    db.add_all([invalid_geometry, empty]); await db.commit()
    for identifier in (foreign_scene.id, invalid_geometry.id, empty.id, uuid4()):
        response = await client.post(f'/parcels/{plot_id}/analyses', json={'satellite_observation_id': str(identifier)})
        assert response.status_code == 422, response.text


async def test_edited_parcel_adopts_own_preview_scene_and_keeps_both_archives(imagery):
    db, user, crop, station, client, preview = imagery
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    plot_id = UUID(created['id'])
    source = Plot(name='Polígono explorado', geojson=POLYGON, active=True,
                  browser_session_id=UUID(client.headers['X-Parcel-Session']))
    db.add(source); await db.flush()
    original = (await observations(db, source.id, 1))[0]
    response = await client.post(f'/parcels/{plot_id}/analyses', json={'satellite_observation_id': str(original.id)})
    assert response.status_code == 200, response.text
    copied_id = UUID(response.json()['context']['satellite']['id'])
    copied = await db.get(SatelliteObservation, copied_id)
    assert copied_id != original.id and copied.plot_id == plot_id
    assert copied.raw_payload['source_observation_id'] == str(original.id)
    assert original.plot_id == source.id
    assert response.json()['context']['satellite']['ndvi'] == original.ndvi
    repeated = await client.post(f'/parcels/{plot_id}/analyses', json={'satellite_observation_id': str(original.id)})
    assert repeated.json()['context']['satellite']['id'] == str(copied_id)
    assert len((await db.scalars(select(SatelliteObservation).where(SatelliteObservation.plot_id == plot_id))).all()) == 1
