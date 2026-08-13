from __future__ import annotations

import io
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from app.db.models import AlertEvaluation, Crop, Station
from app.services import chat_report


@pytest.mark.asyncio
async def test_chat_report_contains_context_conversation_and_images(monkeypatch) -> None:
    alert_id = uuid4()
    station_id = uuid4()
    crop_id = uuid4()
    alert = SimpleNamespace(id=alert_id, station_id=station_id, crop_id=crop_id)
    station = SimpleNamespace(id=station_id, name="Raul Peña", integration_config={"soil_moisture_unit": "cb"})
    crop = SimpleNamespace(id=crop_id, name="Trigo")

    class FakeDb:
        async def get(self, model, identifier):
            if model is AlertEvaluation and identifier == alert_id:
                return alert
            if model is Station and identifier == station_id:
                return station
            if model is Crop and identifier == crop_id:
                return crop
            return None

    async def fake_context(db, identifier, *, period):
        assert identifier == alert_id
        return {
            "query": {
                "station": "Raul Peña",
                "crop": "Trigo",
                "planting_date": "2026-05-21",
                "evaluation_date": "2026-08-13",
                "period": period,
            },
            "phenology": {"name": "Floración"},
            "deterministic_result": {
                "level": "MODERADA",
                "simple_explanation": "Tres condiciones requieren vigilancia.",
                "conditions": [
                    {
                        "label": "Humedad relativa",
                        "status": "met",
                        "value": 82,
                        "unit": "%",
                        "explanation": "Superó el umbral publicado.",
                    }
                ],
            },
            "observed_series_summary": {
                "samples": 215,
                "variables": {
                    "temperature_c": {"mean": 17.2, "min": 12, "max": 24},
                    "rain_mm": {"sum": 32.6},
                },
            },
            "data_freshness": {"latest_reading_at": "2026-08-13T12:00:00+00:00"},
        }

    async def no_satellite(db, station_identifier, crop_identifier):
        return None, None

    monkeypatch.setattr(chat_report, "build_complete_analysis_context", fake_context)
    monkeypatch.setattr(chat_report, "latest_satellite_observation", no_satellite)

    image_buffer = io.BytesIO()
    Image.new("RGB", (320, 200), "#7da94b").save(image_buffer, format="PNG")
    result = await chat_report.generate_chat_report_pdf(
        FakeDb(),
        alert_id,
        period="72h",
        conversation=[
            {"role": "user", "content": "Aparecieron manchas en hojas superiores."},
            {"role": "assistant", "content": "## Hipótesis\n\n- Verifique forma y distribución."},
        ],
        uploaded_images=[{"filename": "hoja.png", "content": image_buffer.getvalue()}],
    )
    assert result.startswith(b"%PDF-")
    assert len(result) > 5_000
