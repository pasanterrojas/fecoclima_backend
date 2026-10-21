from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.schemas.resources import StationPayload
from app.services.ai import AIProviderError, provider_fallback_reason
from app.services.fecoclima_adapter import normalize_fecoclima_item
from app.services.widget_explanation import _data_based_explanation, _widget_evidence
from app.services import widget_explanation


@pytest.fixture
def analysis_context():
    return {
        "query": {"station": "Estación Norte", "crop": "Trigo", "period": "72h"},
        "data_freshness": {"stale": True, "expected_interval_minutes": 10},
        "latest_metric_reading": {"sampled_at": "2026-10-08T12:00:00+00:00", "temperature_c": 21.5},
        "deterministic_result": {
            "level": "MODERADA", "simple_explanation": "Vigilar humedad",
            "conditions": [{"label": "Humedad", "status": "met"}],
            "diseases": [{"name": "Roya", "reason": "Hay humedad", "risk": "moderado"}],
        },
        "phenology": {"stage_name": "Macollaje"},
        "observed_series_summary": {"samples": 3, "from": "a", "to": "b", "variables": {"temperature_c": {"mean": 21}, "rain_mm": {"sum": 2}}},
        "forecast": [{"date": "2026-10-09", "precipitation_mm": 4}],
        "forecast_risk": {"days": [{"date": "2026-10-09", "potential_level": "MODERADA"}], "diseases": [{"name": "Roya", "risk": "moderado"}]},
    }


def test_widget_metric_comes_from_the_station_reading(analysis_context):
    evidence = _widget_evidence(analysis_context, "metric", "temperature")
    assert evidence["lectura"]["valor"] == 21.5
    assert "21.5" in _data_based_explanation(evidence, "provider_auth")
    assert "IA no estuvo disponible" in _data_based_explanation(evidence, "provider_auth")


def test_soil_widget_explains_the_actual_sensor_unit(analysis_context):
    analysis_context["sensor_units"] = {"soil_moisture_pct": "cb"}
    analysis_context["latest_metric_reading"]["soil_moisture_pct"] = 65
    evidence = _widget_evidence(analysis_context, "metric", "soil_moisture")
    assert evidence["lectura"]["valor"] == 65
    assert evidence["lectura"]["unidad"] == "cb"
    assert "Tensión" in evidence["lectura"]["nombre"]
    analysis_context["sensor_units"]["soil_moisture_pct"] = "cp"
    assert _widget_evidence(analysis_context, "metric", "soil_moisture")["lectura"]["unidad"] == "cp"


@pytest.mark.parametrize("widget,item_key", [
    ("metric", "arbitrary"), ("disease", "Enfermedad inventada"),
    ("forecast", "2099-01-01"), ("chart", "otro"), ("unknown", None),
])
def test_widget_rejects_evidence_not_in_the_analysis(analysis_context, widget, item_key):
    with pytest.raises(ValueError):
        _widget_evidence(analysis_context, widget, item_key)


def test_widget_forecast_remains_labelled_as_forecast(analysis_context):
    evidence = _widget_evidence(analysis_context, "forecast", "2026-10-09")
    assert "pronóstico, no una medición" in _data_based_explanation(evidence, "provider_not_configured")


def test_safe_provider_statuses():
    assert provider_fallback_reason(AIProviderError(401, "", "secret-key")) == "provider_auth"
    assert provider_fallback_reason(AIProviderError(429, "", "insufficient_quota")) == "provider_quota"
    assert provider_fallback_reason(AIProviderError(429, "", "rate_limit")) == "provider_limited"


def test_new_fecoclima_station_needs_unique_source_and_valid_timezone():
    base = {"name": "Estación Norte", "code": "NORTE", "source_type": "FECOCLIMA_API"}
    with pytest.raises(ValueError, match="GUID externo"):
        StationPayload(**base)
    assert StationPayload(**base, external_guid="otro-guid", timezone="America/Asuncion").external_guid == "otro-guid"
    with pytest.raises(ValueError, match="zona horaria"):
        StationPayload(**base, external_guid="otro-guid", timezone="No/Existe")


def test_station_timezone_is_used_for_naive_provider_dates():
    row = {"fechaMuestreo": "2026-10-08T08:00:00"}
    asuncion = normalize_fecoclima_item(row, timezone=ZoneInfo("America/Asuncion"))
    bogota = normalize_fecoclima_item(row, timezone=ZoneInfo("America/Bogota"))
    assert asuncion["sampled_at"] != bogota["sampled_at"]
    assert bogota["sampled_at"] == datetime(2026, 10, 8, 13, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_explanation_distinguishes_ai_from_fallback(monkeypatch, analysis_context, provider_fails):
    alert = SimpleNamespace(station_id=uuid4(), crop_id=uuid4())
    agent = SimpleNamespace(id=uuid4())
    version = SimpleNamespace(id=uuid4(), model="modelo-test", temperature=0.2, system_prompt="Ayuda al productor")

    class FakeDb:
        run = None

        async def get(self, _model, _id):
            return alert

        def add(self, run):
            self.run = run

        async def commit(self):
            pass

        async def refresh(self, run):
            run.id = uuid4()

    async def fake_context(*_args, **_kwargs):
        return analysis_context

    async def fake_version(*_args, **_kwargs):
        return agent, version

    async def fake_generate(*_args, **_kwargs):
        if provider_fails:
            raise AIProviderError(401, "rechazado", "secreto-privado")
        return {"markdown": "Revise hoy la humedad de las hojas.", "fallback": False}

    monkeypatch.setattr(widget_explanation, "build_complete_analysis_context", fake_context)
    monkeypatch.setattr(widget_explanation, "select_agent_version", fake_version)
    monkeypatch.setattr(widget_explanation, "generate_markdown_response", fake_generate)
    db = FakeDb()
    result = await widget_explanation.explain_widget(db, uuid4(), "72h", "alert", None)
    assert result["_agent_status"] == ("fallback" if provider_fails else "completed")
    assert db.run.status == result["_agent_status"].upper()
    if provider_fails:
        assert result["_fallback_reason"] == "provider_auth"
        assert "secreto-privado" not in str(db.run.error_message)
    else:
        assert result["_fallback_reason"] is None
