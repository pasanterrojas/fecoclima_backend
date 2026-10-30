"""Prevent display limits from cutting the final forecast or recommendation."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services import ai, plot_agents
from app.services.widget_recovery import recover_analysis_widget_text, recover_widget_text
from app.services.widget_text import compact_widget_result


COMPLETE = '\n\n'.join(f'## Apartado {i}\n' + 'La explicación debe conservar su contenido completo. ' * 30
                       for i in range(5)) + '\n\nPrepare el acceso al lote antes de la lluvia.'


@pytest.mark.parametrize('purpose', ['WEATHER_ANALYSIS', 'FORECAST_ANALYSIS', 'RAG_CROP', 'SATELLITE_ANALYSIS', 'ORCHESTRATOR', 'CROP_CHAT'])
def test_full_markdown_keeps_every_section_and_final_action(purpose):
    result = compact_widget_result({'markdown': COMPLETE, 'max_characters': 1500}, purpose,
                                   limit=1500, preserve_format=True)
    assert result['markdown'] == COMPLETE
    assert 'max_characters' not in result


def saved_result():
    return {'purpose': 'WEATHER_ANALYSIS', 'window_hours': 72, 'view_mode': 'producer',
            'version_id': 'version-a', 'version_updated_at': 'date-a', 'language_revision': 'old',
            'status': 'completed', 'markdown': COMPLETE[:1490].rsplit(' ', 1)[0] + '…', 'max_characters': 1500}


def test_old_character_cut_is_restored_from_its_own_saved_full_text():
    saved = saved_result()
    output = {**saved, 'full_markdown': COMPLETE, 'usage': {'private': 'not returned'}}
    restored = recover_widget_text(saved, output)
    assert restored['markdown'] == COMPLETE and restored['text_complete'] is True
    assert 'max_characters' not in restored and 'usage' not in restored
    assert saved['markdown'].endswith('…')  # Recovery does not mutate the snapshot.


@pytest.mark.parametrize('key,value', [('window_hours', 96), ('view_mode', 'technical'), ('version_id', 'other'),
                                       ('language_revision', 'other'), ('markdown', 'Otra respuesta…')])
def test_recovery_never_substitutes_a_different_run_or_scope(key, value):
    saved = saved_result()
    output = {**saved, 'full_markdown': COMPLETE, key: value}
    assert recover_widget_text(saved, output) is None


@pytest.mark.parametrize('ending', ['Parte de una explicación…', '## Diferencia con la tendencia'])
def test_unrecoverable_word_limit_or_empty_heading_is_marked_incomplete(ending):
    saved = saved_result()
    full = COMPLETE + '\n\n' + ending
    restored = recover_widget_text(saved, {**saved, 'full_markdown': full})
    assert restored['markdown'] == full and restored['text_complete'] is False


async def test_recovery_query_is_limited_to_authorized_analysis_and_does_not_call_ai():
    saved = saved_result()
    analysis = SimpleNamespace(id=uuid4(), agent_results={'WEATHER_ANALYSIS': saved})
    original = deepcopy(analysis.agent_results)
    db = SimpleNamespace(execute=AsyncMock(return_value=[('WEATHER_ANALYSIS', {**saved, 'full_markdown': COMPLETE})]))
    results = await recover_analysis_widget_text(db, analysis)
    assert results['WEATHER_ANALYSIS']['markdown'] == COMPLETE
    statement = db.execute.call_args.args[0].compile()
    assert str(analysis.id) in statement.params.values()
    assert analysis.agent_results == original


async def test_missing_archive_is_honest_and_complete_results_need_no_query():
    db = SimpleNamespace(execute=AsyncMock(return_value=[]))
    analysis = SimpleNamespace(id=uuid4(), agent_results={'WEATHER_ANALYSIS': saved_result()})
    restored = await recover_analysis_widget_text(db, analysis)
    assert restored['WEATHER_ANALYSIS']['text_complete'] is False
    db.execute.reset_mock()
    analysis.agent_results = {'WEATHER_ANALYSIS': {'markdown': COMPLETE, 'text_complete': True}}
    assert await recover_analysis_widget_text(db, analysis) == analysis.agent_results
    db.execute.assert_not_awaited()


@pytest.mark.parametrize('partial', ['', '## Diferencia con la tendencia\nDatos a medio', '{"markdown":"a medio'])
async def test_provider_token_limit_never_becomes_a_completed_answer(monkeypatch, partial):
    monkeypatch.setattr(settings, 'ai_api_key', 'test-key')
    monkeypatch.setattr(settings, 'ai_base_url', 'https://test.invalid/v1')
    monkeypatch.setattr(ai, '_post_chat', AsyncMock(return_value={
        'choices': [{'finish_reason': 'length', 'message': {'content': partial}}], 'usage': {'total_tokens': 1500}}))
    with pytest.raises(ai.AIResponseError) as error:
        await ai.generate_markdown_response('Explique', {}, 'Pronóstico', model='fixture')
    assert error.value.reason == 'provider_output_limit'
    assert error.value.usage['total_tokens'] == 1500


async def test_forecast_request_does_not_apply_word_or_section_cuts(monkeypatch):
    analysis = SimpleNamespace(id=uuid4(), reference_at=datetime.now(UTC), agent_results={}, context={
        'crop': {'id': str(uuid4()), 'name': 'Soja'}, 'phenology': {}, 'windows': {'72': {}},
    })
    version = SimpleNamespace(id=uuid4(), updated_at=datetime.now(UTC), provider='OPENAI', model='fixture',
                              system_prompt='Interprete el clima', temperature=0.2, max_tokens=4000,
                              output_schema={'response_policy': {'max_words': 100, 'max_sections': 2}})
    db = SimpleNamespace(refresh=AsyncMock(), flush=AsyncMock(), add=Mock())
    monkeypatch.setattr(plot_agents, 'select_agent_version', AsyncMock(return_value=(SimpleNamespace(id=uuid4()), version)))
    monkeypatch.setattr(plot_agents, '_knowledge_revision', AsyncMock(return_value='revision'))
    monkeypatch.setattr(plot_agents, 'retrieve_chunks', AsyncMock(return_value=[]))
    generated = AsyncMock(return_value={'markdown': COMPLETE, 'model': 'fixture', 'usage': {}})
    monkeypatch.setattr(plot_agents, 'generate_markdown_response', generated)
    result = await plot_agents.interpret_plot(db, analysis, 'WEATHER_ANALYSIS', window_hours=72)
    assert result['markdown'] == COMPLETE and result['text_complete'] is True
    assert generated.call_args.kwargs['response_policy'] is None
    assert 'No repitas todo el análisis observado' in generated.call_args.args[0]
    assert 'max_characters' not in result
