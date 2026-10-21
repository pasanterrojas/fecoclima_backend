import pytest
from app.schemas.resources import StationPayload
from app.services.station_registration import generated_station_code, normalize_station_endpoint
from app.services.station_location import coordinates_from_text, address_fields

GUID = 'C2694D32-5A4B-48C0-A3B6-8345D9E4935B'
BASE = 'https://fecoclima.fecoprod.com.py/clima/api/estacion/lectura-guid-por-periodo'


def test_concrete_endpoint_uses_dynamic_period_and_infers_guid():
    payload = StationPayload(name='Raúl Peña', source_type='FECOCLIMA_API', endpoint_url=f'{BASE}/{GUID}/2026/1/6')
    assert payload.code is None
    assert payload.external_guid == GUID
    assert payload.endpoint_url == f'{BASE}/{{guid}}/{{year}}/{{month_from}}/{{month_to}}'
    assert 'external_guid' in payload.model_fields_set


def test_pdf_placeholders_and_optional_codes():
    url, guid = normalize_station_endpoint(f'{BASE}/{{id-estacion}}/{{año}}/{{mesDesde}}/{{mesHasta}}', GUID)
    assert guid == GUID
    assert url.endswith('/{guid}/{year}/{month_from}/{month_to}')
    assert StationPayload(name='Estación', code='  abc  ').code == 'ABC'
    assert StationPayload(name='Estación', code='').code is None
    assert len({generated_station_code() for _ in range(100)}) == 100


@pytest.mark.parametrize('suffix', [f'{GUID}/2026/0/6', f'{GUID}/2026/1/13', 'invalid/2026/1/6'])
def test_invalid_endpoint(suffix):
    with pytest.raises(ValueError):
        normalize_station_endpoint(f'{BASE}/{suffix}')


def test_maps_marker_overrides_camera_and_ignores_camera_height():
    assert coordinates_from_text('https://www.google.com/maps/place/test/@-25,-57,450m/data=!3d-26.1585!4d-55.2722') == (-26.1585, -55.2722)
    assert coordinates_from_text('https://www.google.com/maps/search/?api=1&query=-26.1%2C-55.2') == (-26.1, -55.2)
    assert coordinates_from_text('-26.1, -55.2') == (-26.1, -55.2)


@pytest.mark.parametrize('value', ['91,20', 'https://evil.example/@-25,-57', 'https://google.com.evil.example/@-25,-57'])
def test_maps_invalid_input(value):
    with pytest.raises(ValueError):
        coordinates_from_text(value)


def test_paraguay_address_fields():
    assert address_fields({'state':'Alto Paraná', 'county':'Raúl Peña', 'village':'Centro', 'country':'Paraguay'}) == {'department':'Alto Paraná','district':'Raúl Peña','locality':'Centro','country':'Paraguay'}


def test_location_provider_cache_and_partial_failures(monkeypatch, tmp_path):
    import httpx
    from app.services import station_location
    monkeypatch.setattr(station_location.settings, 'storage_path', tmp_path)
    requests = []
    def response(request):
        requests.append(request)
        if request.url.path.endswith('/reverse'):
            return httpx.Response(200, json={'address': {'state': 'Alto Paraná', 'county': 'Raúl Peña', 'village': 'Centro'}})
        return httpx.Response(200, json={'elevation': [321.5]})
    original_client = httpx.Client
    monkeypatch.setattr(station_location.httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs))
    result = station_location.lookup_coordinates(-26.1, -55.2)
    assert result['altitude_m'] == 321.5
    assert result['district'] == 'Raúl Peña'
    assert len(requests) == 2
    assert station_location.lookup_coordinates(-26.1, -55.2) == result
    assert len(requests) == 2


def test_location_provider_unavailable_preserves_coordinates(monkeypatch, tmp_path):
    import httpx
    from app.services import station_location
    monkeypatch.setattr(station_location.settings, 'storage_path', tmp_path)
    original_client = httpx.Client
    monkeypatch.setattr(station_location.httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    result = station_location.lookup_coordinates(-26.1, -55.2)
    assert result['latitude'] == -26.1
    assert result['longitude'] == -55.2
    assert result['altitude_m'] is None
    assert len(result['warnings']) == 2


def test_station_url_without_period_and_base_with_explicit_guid():
    endpoint, guid = normalize_station_endpoint(f'{BASE}/{GUID}')
    assert guid == GUID
    assert endpoint.endswith('/{guid}/{year}/{month_from}/{month_to}')
    endpoint, guid = normalize_station_endpoint('https://fecoclima.fecoprod.com.py/clima/api',GUID)
    assert endpoint == f'{BASE}/{{guid}}/{{year}}/{{month_from}}/{{month_to}}'
    assert guid == GUID
