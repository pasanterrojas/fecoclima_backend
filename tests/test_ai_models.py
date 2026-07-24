from app.services.ai import _chat_payload, _is_reasoning_model


def test_reasoning_model_omits_temperature() -> None:
    payload = _chat_payload(
        model="gpt-5.6-luna",
        system_prompt="Devuelve JSON",
        user_content='{"prueba": true}',
        temperature=0.2,
        max_tokens=500,
    )
    assert "temperature" not in payload
    assert payload["max_completion_tokens"] == 500
    assert payload["response_format"] == {"type": "json_object"}


def test_non_reasoning_model_keeps_temperature() -> None:
    payload = _chat_payload(
        model="gpt-4.1-mini",
        system_prompt="Devuelve JSON",
        user_content='{"prueba": true}',
        temperature=0.3,
        max_tokens=500,
    )
    assert payload["temperature"] == 0.3
    assert not _is_reasoning_model("gpt-4.1-mini")


def test_markdown_image_payload_keeps_context_and_image_without_temperature_for_reasoning_model() -> None:
    from app.services.ai import _markdown_chat_payload

    payload = _markdown_chat_payload(
        model="gpt-5.6-luna",
        system_prompt="Asistente de trigo",
        context={"deterministic_result": {"level": "MODERADA"}},
        question="Analiza la imagen",
        history=[{"role": "user", "content": "Contexto anterior"}],
        image_data_url="data:image/jpeg;base64,/9j/",
        temperature=0.2,
        max_tokens=900,
    )
    assert "temperature" not in payload
    assert payload["max_completion_tokens"] == 900
    content = payload["messages"][-1]["content"]
    assert content[0]["type"] == "text"
    assert "MODERADA" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_legacy_json_agent_response_is_converted_to_markdown() -> None:
    from app.services.ai import _coerce_markdown

    result = _coerce_markdown(
        '{"titulo":"Qué revisar hoy","cultivo":"Trigo","resultado_deterministico":{"nivel_alerta":"MODERADA"},"revision_en_lote":[{"punto":"Humedad relativa","criterio":"Monitorear"}]}'
    )
    assert result.startswith("## Qué revisar hoy")
    assert "### Resultado deterministico" in result
    assert "**Nivel alerta:** MODERADA" in result
    assert '"titulo"' not in result


def test_markdown_agent_response_is_preserved() -> None:
    from app.services.ai import _coerce_markdown

    source = "## 🌾 Criterio\n\n- Revisar el lote."
    assert _coerce_markdown(source) == source


def test_truncated_json_agent_response_is_not_shown_raw() -> None:
    from app.services.ai import _coerce_markdown

    result = _coerce_markdown('{"titulo":"Qué revisar hoy","revision_en_lote":[')
    assert result.startswith("## ⚠️ El criterio del agente quedó incompleto")
    assert '"revision_en_lote"' not in result
