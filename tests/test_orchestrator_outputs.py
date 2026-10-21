import pytest

from app.services.orchestrator_outputs import permitted_widgets, parse_widget_outputs, render_widgets, fallback_widgets


def test_orchestrator_defaults_and_explicit_disable_are_different():
    assert "diagnosis" in permitted_widgets({})
    assert permitted_widgets({"allowed_widgets": []}) == []
    assert permitted_widgets({"allowed_widgets": "diagnosis"}) == []


def test_only_configured_destinations_can_reach_the_rendered_result():
    allowed = permitted_widgets({"allowed_widgets": ["diagnosis", "diagnosis", "unsupported"]})
    sections = parse_widget_outputs('{"widgets":{"diagnosis":"Revise el lote.","forecast":"No permitido."}}', allowed)
    assert sections == {"diagnosis": "Revise el lote."}
    assert "No permitido" not in render_widgets(sections)
    with pytest.raises(ValueError):
        parse_widget_outputs('Texto libre que no cumple el contrato', allowed)


def test_restricted_fallback_does_not_render_other_widgets():
    sections = fallback_widgets({"phenology":{"stage_name":"V1"}}, ["phenology"])
    assert set(sections) == {"phenology"} and "V1" in sections["phenology"]
