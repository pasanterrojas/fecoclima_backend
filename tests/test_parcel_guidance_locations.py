"""Visit links require both spatial evidence and a useful, explicit recommendation."""
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services import plot_agents
from app.services.parcel_guidance import field_review_candidate, parse_guidance_response, satellite_plain_evidence


def context():
    captured_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    return {
        'crop': {'id': str(uuid4()), 'name': 'Soja'},
        'plot': {'geojson': {'type': 'Polygon', 'coordinates': [
            [[-55.01, -25.01], [-55, -25.01], [-55, -25], [-55.01, -25], [-55.01, -25.01]]]}},
        'phenology': {'stage_name': 'Vegetativa estimada'},
        'selected_window_hours': 72, 'windows': {'72': {'hours': 72, 'stations': []}},
        'satellite': {
            'captured_at': captured_at, 'data_is_stale': False,
            'cloud_percentage': 5, 'ndvi': 0.4,
            'analysis': {'reliability': 'alta', 'geometry_mode': 'polygon', 'age_days': 1,
                         'signals': [{'code': 'ndvi_trend', 'level': 'attention'}],
                         'thresholds': {'max_age_days': 20, 'max_reliable_cloud_percentage': 30}},
        },
        'inspection_point': {
            'method': 'lowest_valid_ndvi_pixel', 'latitude': -25.005, 'longitude': -55.005, 'ndvi': 0.2,
            'captured_at': captured_at, 'maps_url': 'https://untrusted.invalid/location',
        },
    }


def response(recommended=True, reason='La vegetación cambió y conviene contrastarla con otra zona.', check='Compare cobertura de plantas y humedad a la profundidad de raíces.'):
    return json.dumps({'markdown': '### Qué está pasando\nLa vegetación requiere atención.',
                       'field_review': {'recommended': recommended, 'reason': reason, 'check': check,
                                        'maps_url': 'https://untrusted.invalid/other-place'}}, ensure_ascii=False)


def test_valid_pixel_is_only_a_candidate_and_uses_server_owned_url():
    data = context()
    candidate = field_review_candidate(data)
    assert candidate['maps_url'] == 'https://www.google.com/maps/search/?api=1&query=-25.0050000,-55.0050000'
    data['field_review_candidate'] = candidate
    assert 'field_review' not in parse_guidance_response(response(False), data)
    result = parse_guidance_response(response(), data)
    assert result['field_review']['maps_url'] == candidate['maps_url']
    assert 'Compare cobertura' in result['field_review']['check']
    assert 'untrusted' not in str(result)


@pytest.mark.parametrize('change', ['reference', 'outside', 'stale', 'cloudy', 'old', 'limited', 'missing_age', 'no_attention', 'other_scene', 'invalid_coords'])
def test_reference_missing_or_unreliable_evidence_cannot_create_a_visit_link(change):
    data = context()
    if change == 'reference': data['inspection_point']['method'] = 'interior_reference_only'
    elif change == 'outside': data['inspection_point']['longitude'] = -56
    elif change == 'stale': data['satellite']['data_is_stale'] = True
    elif change == 'cloudy': data['satellite']['cloud_percentage'] = 80
    elif change == 'old': data['satellite']['analysis']['age_days'] = 30
    elif change == 'limited': data['satellite']['analysis']['reliability'] = 'limitada'
    elif change == 'missing_age': data['satellite'].pop('data_is_stale')
    elif change == 'no_attention': data['satellite']['analysis']['signals'] = [{'code': 'ndvi_state', 'level': 'positive'}]
    elif change == 'other_scene': data['inspection_point']['captured_at'] = '2026-10-01T12:00:00+00:00'
    elif change == 'invalid_coords': data['inspection_point']['latitude'] = float('nan')
    data['field_review_candidate'] = field_review_candidate(data)
    assert data['field_review_candidate'] is None
    assert 'field_review' not in parse_guidance_response(response(), data)


@pytest.mark.parametrize('recommended,reason,check', [(False, 'Motivo', 'Revisión'), ('true', 'Motivo', 'Revisión'), (True, '', 'Revisión'), (True, 'Motivo', '')])
def test_recommendation_requires_a_true_boolean_reason_and_specific_check(recommended, reason, check):
    data = context()
    data['field_review_candidate'] = field_review_candidate(data)
    assert 'field_review' not in parse_guidance_response(response(recommended, reason, check), data)


def test_legacy_markdown_and_model_links_cannot_bypass_location_contract():
    data = context()
    data['field_review_candidate'] = field_review_candidate(data)
    result = parse_guidance_response('Revise el suelo. [Ir aquí](https://maps.google.com/arbitrary)', data)
    assert 'field_review' not in result and 'https://' not in result['markdown']


def test_restricted_destinations_still_apply_to_explanation_and_locations():
    data = context()
    data['field_review_candidate'] = field_review_candidate(data)
    parsed = json.loads(response())
    parsed['widgets'] = {'phenology': 'Etapa estimada.', 'diagnosis': 'No permitido.'}
    result = parse_guidance_response(json.dumps(parsed), data, ['phenology'])
    assert result['widgets'] == {'phenology': 'Etapa estimada.'}
    assert 'No permitido' not in result['markdown'] and 'field_review' not in result


def test_satellite_fallback_uses_calculated_signals_without_jargon_or_diagnosis():
    data = context()
    text = satellite_plain_evidence(data)
    assert 'disminuyó respecto de la imagen anterior' in text
    assert data['satellite']['captured_at'][:10] in text and 'NDVI' not in text and 'https://' not in text
    assert 'No hay una imagen válida' in satellite_plain_evidence({})
    data['satellite']['data_is_stale'] = True
    assert 'no como prueba del estado actual' in satellite_plain_evidence(data)


def test_old_snapshot_cannot_reuse_a_once_fresh_location():
    data = context()
    captured = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    data['satellite']['captured_at'] = data['inspection_point']['captured_at'] = captured
    assert data['satellite']['data_is_stale'] is False
    assert field_review_candidate(data) is None


async def test_widget_four_keeps_full_explanation_and_optional_visit_metadata(monkeypatch):
    data = context()
    original = deepcopy(data)
    analysis = SimpleNamespace(id=uuid4(), context=data, reference_at=datetime.now(UTC), agent_results={
        'SATELLITE_ANALYSIS': {'window_hours': 72, 'view_mode': 'producer', 'status': 'completed',
                              'images_processed': True, 'markdown': 'La cobertura disminuyó.'}})
    version = SimpleNamespace(id=uuid4(), updated_at=datetime.now(UTC), provider='OPENAI', model='fixture',
                              system_prompt='Interprete la parcela', temperature=0.2, max_tokens=4000, output_schema={})
    db = SimpleNamespace(refresh=AsyncMock(), flush=AsyncMock(), add=Mock())
    monkeypatch.setattr(plot_agents, 'select_agent_version', AsyncMock(return_value=(SimpleNamespace(id=uuid4()), version)))
    monkeypatch.setattr(plot_agents, '_knowledge_revision', AsyncMock(return_value='revision'))
    monkeypatch.setattr(plot_agents, 'retrieve_chunks', AsyncMock(return_value=[]))
    generated = AsyncMock(return_value={'markdown': response(), 'model': 'fixture', 'usage': {}})
    monkeypatch.setattr(plot_agents, 'generate_markdown_response', generated)
    result = await plot_agents.interpret_plot(db, analysis, 'ORCHESTRATOR', window_hours=72)
    assert result['status'] == 'completed' and result['field_review']['maps_url'].startswith('https://www.google.com/maps/')
    prompt, sent = generated.call_args.args[:2]
    assert '180 a 320 palabras' in prompt and 'CONTRATO DE SALIDA PRIORITARIO' in prompt
    assert sent['satellite_evidence_basis']['specialist_viewed_image'] is True
    assert 'No se puede clasificar' in sent['parcel_fact_brief']['alert']
    assert 'disminuyó' in sent['parcel_fact_brief']['satellite']
    assert 'inspection_point' not in sent
    assert sent['field_review_candidate']['latitude'] == -25.005
    assert generated.call_args.kwargs['preserve_structured_output'] is True
    assert generated.call_args.kwargs['response_policy'] is None
    assert analysis.context == original
