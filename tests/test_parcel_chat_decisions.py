"""Regression coverage for the irrigation example and the conversational request."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services import ai, plot_agents
from app.services.ai import _markdown_chat_payload
from app.services.soil_tension import soil_tension


def context():
    return {
        'crop': {'id': str(uuid4()), 'name': 'Soja'},
        'plot': {'name': 'Parcela elegida', 'producer_document': 'private'},
        'reference_at': '2026-10-26T17:00:00+00:00',
        'selected_window_hours': 72,
        'phenology': {'stage_name': 'Vegetativa'},
        'windows': {'72': {'hours': 72, 'stations': [{
            'station_id': 'a', 'stale': False,
            'values': {'rain_sum_mm': 12},
            'alert': {'conditions': [{'label': 'Humedad relativa mayor a 80%', 'status': 'met'}]},
        }]}},
        'raw_readings': [{
            'station_id': 'a', 'name': 'Raul Peña',
            'sensor_units': {'soil_moisture_pct': 'cb'},
            'reading': {'sampled_at': '2026-10-26T16:45:00+00:00', 'soil_moisture_pct': 99, 'rain_mm': 0},
            'soil_tension': soil_tension(99, 'cb'),
        }],
        'forecast': {'days': [{'date': '2026-10-27', 'precipitation_mm': 4}]},
    }


def test_irrigation_answers_the_decision_without_disease_rules_or_invented_amounts():
    text = plot_agents.deterministic_summary(context(), 'CROP_CHAT', question='¿Aplico riego o espero lluvia?')
    assert '99 cb' in text and 'suelo seco' in text
    assert 'preparar el riego si se confirma' in text
    assert '12 mm' in text and '4 mm para 2026-10-27' in text
    assert 'profundidad' in text
    assert 'Condiciones cumplidas' not in text
    assert 'Humedad relativa mayor' not in text
    assert 'Administración' not in text
    assert 'no llovió' not in text


@pytest.mark.parametrize('value, expected', [(5, 'no apoya añadir más agua'), (25, 'no indica necesidad inmediata'), (45, 'umbral de riego calibrado')])
def test_wetter_soil_does_not_receive_dry_soil_recommendation(value, expected):
    data = context()
    data['raw_readings'][0]['reading']['soil_moisture_pct'] = value
    data['raw_readings'][0]['soil_tension'] = soil_tension(value, 'cb')
    text = plot_agents.deterministic_summary(data, 'CROP_CHAT', question='¿Debo regar?')
    assert expected in text
    assert 'conviene regar según' not in text


def test_stale_dry_reading_does_not_recommend_current_irrigation():
    data = context()
    data['windows']['72']['stations'][0]['stale'] = True
    text = plot_agents.deterministic_summary(data, 'CROP_CHAT', question='¿Aplico riego?')
    assert 'atrasada' in text and 'actualizarla' in text
    assert 'conviene regar según' not in text


def test_percentage_reading_cannot_reuse_centibar_classification():
    data = context()
    data['raw_readings'][0]['sensor_units']['soil_moisture_pct'] = '%'
    text = plot_agents.deterministic_summary(data, 'CROP_CHAT', question='¿Aplico riego?')
    assert '99 %' in text and 'referencia calibrada' in text
    assert 'suelo seco' not in text and 'conviene regar según' not in text


def test_multiple_stations_are_named_without_merging_their_rain():
    data = context()
    second = deepcopy(data['raw_readings'][0])
    second.update(station_id='b', name='Estación Sur')
    data['raw_readings'].append(second)
    data['windows']['72']['stations'].append({'station_id': 'b', 'stale': True, 'values': {'rain_sum_mm': 30}})
    text = plot_agents.deterministic_summary(data, 'CROP_CHAT', question='¿Aplico riego?')
    assert 'Raul Peña' in text and 'Estación Sur' in text
    assert '12 mm' in text and '30 mm' in text and '42 mm' not in text


def test_history_preserves_older_dialogue_voice_and_fallback_provenance():
    history = [{'role': 'user', 'content': 'Mi suelo es arcilloso.'}]
    history += [{'role': 'assistant', 'content': 'Mensaje intermedio'}] * 15
    history += [{'role': 'user', 'content': 'Mensaje de voz', 'transcription': 'Sensor a 30 cm.'},
                {'role': 'assistant', 'content': 'No pude responder', 'status': 'fallback'}]
    payload = _markdown_chat_payload(model='test', system_prompt='Chat', context={'agent_purpose': 'CROP_CHAT'},
                                     question='¿Y ahora?', history=history, image_data_url=None,
                                     temperature=0.2, max_tokens=3000)
    messages = payload['messages']
    assert messages[1]['content'] == 'Mi suelo es arcilloso.'
    assert 'Sensor a 30 cm.' in messages[-3]['content']
    assert 'sin IA' in messages[-2]['content']
    assert '¿Y ahora?' in messages[-1]['content']


def test_long_history_has_bounded_size_and_keeps_the_latest_turns():
    payload = _markdown_chat_payload(model='test', system_prompt='Chat', context={'agent_purpose': 'CROP_CHAT'},
                                     question='Última pregunta', history=[{'role': 'user', 'content': str(i) * 4000} for i in range(100)],
                                     image_data_url=None, temperature=0.2, max_tokens=3000)
    assert sum(len(item['content']) for item in payload['messages'][1:-1]) <= 32000
    assert payload['messages'][-2]['content'].startswith('99')


async def test_live_chat_sends_only_selected_analysis_and_all_relevant_widgets(monkeypatch):
    data = context()
    data['windows']['96'] = {'private_other_period': True}
    data['possible_diseases'] = [{'hours': 72, 'diseases': []}, {'hours': 96, 'diseases': []}]
    analysis = SimpleNamespace(id=uuid4(), context=data, reference_at=datetime.now(UTC), agent_results={
        'ORCHESTRATOR': {'window_hours': 72, 'markdown': 'Síntesis seleccionada'},
        'WEATHER_ANALYSIS': {'window_hours': 96, 'markdown': 'Interpretación de otro período'},
    })
    version = SimpleNamespace(id=uuid4(), updated_at=datetime.now(UTC), provider='OPENAI', model='test-model',
                              system_prompt='Asistente agronómico', temperature=0.2, max_tokens=6000, output_schema={})
    agent = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(refresh=AsyncMock(), flush=AsyncMock(), add=Mock())
    monkeypatch.setattr(plot_agents, 'select_agent_version', AsyncMock(return_value=(agent, version)))
    monkeypatch.setattr(plot_agents, '_knowledge_revision', AsyncMock(return_value='revision'))
    monkeypatch.setattr(plot_agents, 'retrieve_chunks', AsyncMock(return_value=[]))
    monkeypatch.setattr(settings, 'ai_api_key', 'test-key')
    monkeypatch.setattr(settings, 'ai_base_url', 'https://test.invalid/v1')
    post = AsyncMock(return_value={'choices': [{'message': {'content': 'La lectura de suelo orienta a revisar el riego.'}}]})
    monkeypatch.setattr(ai, '_post_chat', post)
    result = await plot_agents.interpret_plot(db, analysis, 'CROP_CHAT', question='¿Aplico riego?',
                                             history=[{'role': 'user', 'content': 'Sensor a 30 cm.'}], window_hours=72)
    assert result['status'] == 'completed' and result['model'] == 'test-model'
    payload = post.call_args.args[0]
    prompt = payload['messages'][0]['content']
    assert 'NO son umbrales de riego' in prompt
    assert 'reference_at y chat_requested_at' in prompt
    sent = payload['messages'][-1]['content']
    assert 'Síntesis seleccionada' in sent and 'chat_requested_at' in sent
    assert 'private_other_period' not in sent and 'Interpretación de otro período' not in sent
    assert 'producer_document' not in sent
    assert 'Sensor a 30 cm.' in payload['messages'][1]['content']
    assert '96' in analysis.context['windows']  # The stored snapshot is preserved.


async def test_provider_model_failure_keeps_honest_irrigation_fallback(monkeypatch):
    data = context()
    analysis = SimpleNamespace(id=uuid4(), context=data, reference_at=datetime.now(UTC), agent_results={})
    version = SimpleNamespace(id=uuid4(), updated_at=datetime.now(UTC), provider='OPENAI', model='missing-model',
                              system_prompt='Asistente', temperature=0.2, max_tokens=3000, output_schema={})
    db = SimpleNamespace(refresh=AsyncMock(), flush=AsyncMock(), add=Mock())
    monkeypatch.setattr(plot_agents, 'select_agent_version', AsyncMock(return_value=(SimpleNamespace(id=uuid4()), version)))
    monkeypatch.setattr(plot_agents, '_knowledge_revision', AsyncMock(return_value='revision'))
    monkeypatch.setattr(plot_agents, 'retrieve_chunks', AsyncMock(return_value=[]))
    monkeypatch.setattr(plot_agents, 'generate_markdown_response', AsyncMock(side_effect=ai.AIProviderError(404, 'Unavailable', 'model_not_found')))
    result = await plot_agents.interpret_plot(db, analysis, 'CROP_CHAT', question='¿Aplico riego?', window_hours=72)
    assert result['status'] == 'fallback' and result['fallback_reason'] == 'provider_model'
    assert '99 cb' in result['markdown'] and 'Condiciones cumplidas' not in result['markdown']
