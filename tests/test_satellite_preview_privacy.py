from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.endpoints.public import public_satellite_image, public_satellite_index_image
from app.db.models import Plot, SatelliteObservation


@pytest.mark.parametrize('private_field,private_value', [
    ('browser_session_id', uuid4()),
    ('created_by', uuid4()),
    ('planting_date', date(2026, 10, 1)),
    ('deleted_at', datetime(2026, 10, 1, tzinfo=UTC)),
])
@pytest.mark.parametrize('layer', ['rgb', 'ndvi'])
async def test_legacy_public_media_does_not_bypass_preview_grants(private_field, private_value, layer):
    plot_id, observation_id = uuid4(), uuid4()
    plot = SimpleNamespace(id=plot_id, planting_date=None, created_by=None, browser_session_id=None, deleted_at=None)
    setattr(plot, private_field, private_value)
    observation = SimpleNamespace(id=observation_id, plot_id=plot_id)

    class Database:
        async def get(self, model, record_id):
            if model is SatelliteObservation:
                assert record_id == observation_id
                return observation
            assert model is Plot and record_id == plot_id
            return plot

    with pytest.raises(HTTPException) as error:
        if layer == 'rgb':
            await public_satellite_image(observation_id, Database())
        else:
            await public_satellite_index_image(observation_id, layer, Database())
    assert error.value.status_code == 404


async def test_legacy_media_remains_available_for_public_station_area(monkeypatch, tmp_path):
    plot_id, observation_id = uuid4(), uuid4()
    plot = SimpleNamespace(id=plot_id, planting_date=None, created_by=None, browser_session_id=None, deleted_at=None)
    observation = SimpleNamespace(id=observation_id, plot_id=plot_id)
    path = tmp_path / 'station.jpg'
    path.write_bytes(b'fixture-station-image')

    class Database:
        async def get(self, model, _record_id):
            return observation if model is SatelliteObservation else plot

    monkeypatch.setattr('app.api.endpoints.public.local_satellite_image_path', lambda _row: path)
    monkeypatch.setattr('app.api.endpoints.public.local_satellite_index_image_path', lambda _row, _index: path)
    assert (await public_satellite_image(observation_id, Database())).status_code == 200
    assert (await public_satellite_index_image(observation_id, 'ndvi', Database())).status_code == 200
