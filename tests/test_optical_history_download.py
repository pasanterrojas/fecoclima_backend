"""A refresh fetches three distinct acquisitions, not three copies of the latest."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import plot_satellite, satellite


def scene(day, *, value=0.5):
    return {'observation_id': str(uuid4()), 'captured_at': f'2026-10-{day:02d}T12:00:00+00:00',
            'status': 'PROCESSED', 'image_available': True, 'indices': {'ndvi': value}}


@pytest.mark.asyncio
async def test_refresh_fetches_latest_and_two_prior_dates(monkeypatch):
    scenes = [scene(19, value=0.6), scene(16, value=0.4), scene(12, value=0.2)]
    provider = AsyncMock(side_effect=scenes)
    monkeypatch.setattr(plot_satellite, 'sync_satellite_for_station', provider)
    plot = SimpleNamespace(id=uuid4(), station_id=None, crop_id=None)
    result = await plot_satellite.sync_plot_optical_history(SimpleNamespace(), plot)
    assert result['history_count'] == 3 and result['history_status'] == 'COMPLETE'
    assert result['observation_id'] == scenes[0]['observation_id']
    assert [item['captured_at'] for item in result['history']] == [item['captured_at'] for item in scenes]
    calls = provider.await_args_list
    assert calls[0].kwargs['force'] is True and 'before' not in calls[0].kwargs
    assert calls[1].kwargs['before'] == datetime(2026, 10, 19, 12, tzinfo=UTC)
    assert calls[2].kwargs['before'] == datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert all(call.kwargs['plot_id'] == plot.id for call in calls)


@pytest.mark.asyncio
async def test_history_cannot_count_latest_again(monkeypatch):
    latest = scene(19)
    provider = AsyncMock(side_effect=[latest, latest])
    monkeypatch.setattr(plot_satellite, 'sync_satellite_for_station', provider)
    result = await plot_satellite.sync_plot_optical_history(SimpleNamespace(), SimpleNamespace(id=uuid4(), station_id=None, crop_id=None))
    assert result['history_count'] == 1 and result['history_status'] == 'PARTIAL'
    assert provider.await_count == 2
    assert 'fecha anterior distinta' in result['history_message']


@pytest.mark.asyncio
async def test_historical_failure_preserves_downloaded_latest(monkeypatch):
    latest = scene(19)
    monkeypatch.setattr(plot_satellite, 'sync_satellite_for_station', AsyncMock(side_effect=[latest, TimeoutError()]))
    db = SimpleNamespace(rollback=AsyncMock())
    result = await plot_satellite.sync_plot_optical_history(db, SimpleNamespace(id=uuid4(), station_id=None, crop_id=None))
    assert result['observation_id'] == latest['observation_id'] and result['status'] == 'PROCESSED'
    assert result['history_count'] == 1 and result['history_status'] == 'PARTIAL'
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['indices', 'image'])
async def test_catalog_metadata_without_image_and_indicators_is_not_counted(monkeypatch, missing):
    incomplete = scene(16)
    if missing == 'indices':
        incomplete['indices'] = {}
    else:
        incomplete['image_available'] = False
    monkeypatch.setattr(plot_satellite, 'sync_satellite_for_station', AsyncMock(side_effect=[scene(19), incomplete]))
    result = await plot_satellite.sync_plot_optical_history(SimpleNamespace(), SimpleNamespace(id=uuid4(), station_id=None, crop_id=None))
    assert result['history_count'] == 1 and result['history_status'] == 'PARTIAL'


@pytest.mark.asyncio
@pytest.mark.parametrize('same_day_result', [False, True])
async def test_historical_query_excludes_latest_day_and_never_uses_latest_cache(monkeypatch, same_day_result):
    plot = SimpleNamespace(id=uuid4(), active=True, deleted_at=None, geojson={'type': 'Polygon'}, crop_id=None)
    latest = SimpleNamespace(raw_payload={'processing_geometry': plot.geojson, 'indices': {'recl': 1}})
    monkeypatch.setattr(satellite, '_integration', AsyncMock(return_value=None))
    monkeypatch.setattr(satellite, '_oauth_credentials', lambda _: ('id', 'secret'))
    monkeypatch.setattr(satellite, '_acquire_station_refresh_lock', AsyncMock())
    monkeypatch.setattr(satellite, '_latest_complete_observation', AsyncMock(return_value=latest))
    monkeypatch.setattr(satellite, '_current_visual_paths', lambda _: {name: 'cached' for name in satellite.INDEX_NAMES})
    monkeypatch.setattr(satellite, 'satellite_catalog_cache_is_stale', lambda *_: False)
    monkeypatch.setattr(satellite, 'plot_geometry', lambda _: plot.geojson)
    features = [{'properties': {'datetime': '2026-10-19T10:00:00Z'}}] if same_day_result else []
    search = AsyncMock(return_value={'features': features})
    monkeypatch.setattr(satellite, '_search_copernicus_catalog_for_geometry', search)
    result = await satellite.sync_satellite_for_station(
        SimpleNamespace(get=AsyncMock(return_value=plot)), None, plot_id=plot.id,
        before=datetime(2026, 10, 19, 12, tzinfo=UTC),
    )
    assert result['status'] == 'NO_SCENE' and not result.get('observation_id')
    assert search.await_args.args[3].isoformat() == '2026-10-18'


@pytest.mark.asyncio
async def test_optional_inspection_raster_does_not_erase_three_downloaded_scenes(monkeypatch):
    latest = scene(19)
    completed = {**latest, 'history_count': 3, 'history_status': 'COMPLETE'}
    monkeypatch.setattr(plot_satellite, 'sync_plot_influences', AsyncMock(return_value=([], [])))
    monkeypatch.setattr(plot_satellite, 'sync_plot_optical_history', AsyncMock(return_value=completed))
    monkeypatch.setattr(plot_satellite, '_integration', AsyncMock(return_value=None))
    monkeypatch.setattr(plot_satellite, '_raster', AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(plot_satellite, '_search_copernicus_catalog_for_geometry', AsyncMock(return_value={'features': []}))
    observation = SimpleNamespace(raw_payload={}, captured_at=datetime.now(UTC))
    db = SimpleNamespace(get=AsyncMock(return_value=observation), commit=AsyncMock(), rollback=AsyncMock())
    result = await plot_satellite.sync_plot_satellite(db, SimpleNamespace(id=uuid4(), geojson={}))
    assert result['sentinel_2'] == completed
    assert result['inspection_point']['status'] == 'UNAVAILABLE'
