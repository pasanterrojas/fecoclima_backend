from app.services.chat_context import widget_context, conversation_observations
from app.services.parcel_language import parcel_language_instruction


def test_chat_receives_synthesis_and_other_widgets_only_from_selected_period():
    result = widget_context({
        'ORCHESTRATOR': {'markdown':'Síntesis de parcela', 'window_hours':72},
        'SATELLITE_ANALYSIS': {'markdown':'Imagen de parcela', 'window_hours':72},
        'WEATHER_ANALYSIS': {'markdown':'Periodo anterior', 'window_hours':120},
        'CROP_CHAT': {'markdown':'Respuesta anterior', 'window_hours':72},
    }, 72)
    assert result == {'ORCHESTRATOR':'Síntesis de parcela', 'SATELLITE_ANALYSIS':'Imagen de parcela'}


def test_user_correction_survives_more_than_twelve_messages_without_becoming_verified_fact():
    history = [{'role':'user','content':'Esta foto es de café','images':[{'storage_path':'private'}]}]
    history += [{'role':'assistant','content':'Soja en V2'}] * 15
    notes = conversation_observations(history)
    assert len(notes) == 1
    assert notes[0]['statement'] == 'Esta foto es de café'
    assert notes[0]['source'] == 'user_statement_not_verified'
    assert notes[0]['photo_count'] == 1
    assert 'private' not in str(notes)
    assert 'Soja' not in str(notes)


def test_photo_species_is_independent_of_registered_crop_in_both_views():
    for mode in ('producer', 'technical'):
        prompt = parcel_language_instruction(mode, 'CROP_CHAT')
        assert 'NO identifica la especie' in prompt
        assert 'no presupongas que pertenece a la parcela' in prompt
        assert 'ni apliques fenología, reglas o documentos de soja al café' in prompt
        assert 'rectifícalas ante evidencia nueva' in prompt
