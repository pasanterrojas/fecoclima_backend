from app.services.parcel_language import parcel_language_instruction
from app.services.widget_text import compact_widget_result


def test_producer_synthesis_requests_short_complete_actionable_language():
    prompt = parcel_language_instruction('producer', 'ORCHESTRATOR')
    assert '100 palabras' in prompt
    assert '«Hoy»' in prompt and '«Qué hacer»' in prompt
    assert 'no activaron una alerta' in prompt
    assert 'No menciones determinista' in prompt
    assert 'Termina la última oración' in prompt


def test_technical_and_report_keep_their_own_language():
    assert parcel_language_instruction('technical', 'ORCHESTRATOR') == ''
    assert parcel_language_instruction('producer', 'PDF_REPORT') == ''


def test_synthesis_does_not_cut_final_recommendation_mid_sentence():
    text = 'Observación completa. ' * 100 + 'Revise las hojas de las zonas señaladas.'
    result = compact_widget_result({'markdown': text}, 'ORCHESTRATOR', limit=1500, preserve_format=True)
    assert result['markdown'] == text


def test_chat_prioritizes_latest_message_and_actual_photo_without_report_template():
    prompt = parcel_language_instruction('producer', 'CROP_CHAT')
    assert '2 a 5 oraciones' in prompt
    assert 'no un informe' in prompt
    assert 'lo que realmente se ve' in prompt
    assert 'una última lectura no es un máximo' in prompt
    assert 'tres bloques' not in prompt
    assert 'No menciones determinístico' in prompt
    assert 'detalle técnico' in parcel_language_instruction('technical', 'CROP_CHAT')


def test_chat_keeps_complete_answer():
    text = 'Antecedente. ' * 220 + 'Envíame una foto cercana de la hoja afectada.'
    assert compact_widget_result({'markdown': text}, 'CROP_CHAT', limit=2200, preserve_format=True)['markdown'] == text


def test_chat_payload_keeps_photo_history_and_question_without_forcing_report():
    from app.services.ai import _markdown_chat_payload
    payload = _markdown_chat_payload(
        model='test-model', system_prompt='Chat de parcela',
        context={'agent_purpose':'CROP_CHAT', 'selected_window_hours':72},
        question='Así está mi cultivo', history=[{'role':'assistant','content':'¿Puedes mostrarme las hojas?'}],
        image_data_url='data:image/jpeg;base64,example', temperature=0.2, max_tokens=1000,
    )
    messages = payload['messages']
    assert messages[1]['content'] == '¿Puedes mostrarme las hojas?'
    assert messages[-1]['content'][1]['image_url']['url'] == 'data:image/jpeg;base64,example'
    assert 'Así está mi cultivo' in messages[-1]['content'][0]['text']
    assert 'con subtítulos' not in messages[-1]['content'][0]['text']
