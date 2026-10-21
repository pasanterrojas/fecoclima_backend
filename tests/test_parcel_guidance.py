"""Sanity checks for contextual explanations without a working AI provider."""
from app.services.plot_agents import deterministic_summary
from app.services.widget_text import compact_widget_result


CONTEXT = {
    "windows": {
        "72": {
            "level": "DATOS_INSUFICIENTES",
            "stations": [{
                "station_id": "station-a",
                "alert": {"conditions": [
                    {"label": "Humedad ambiental alta", "status": "met"},
                    {"label": "Humedad del suelo", "status": "missing"},
                ]},
                "data_issues": [{"message": "La unidad cb no coincide con %", "action": "Corregir regla o calibración"}],
            }],
        }
    },
    "phenology": {"stage_name": "Desarrollo vegetativo V2 a V4"},
    "raw_readings": [{
        "station_id": "station-a", "name": "Estación Norte", "sensor_units": {"soil_moisture_pct": "cb"},
        "reading": {"sampled_at": "2026-10-19T10:00:00Z", "temperature_c": 25, "humidity_pct": 82, "rain_mm": 3, "soil_moisture_pct": 25},
        "soil_tension": {"value": 25, "unit": "cb", "label": "Intermedia", "description": "Medición puntual de tensión."},
    }],
    "forecast": {"days": [{"date": "2026-10-21", "precipitation_mm": 14}]},
    "satellite": {"captured_at": "2026-10-19", "ndvi": 0.4291, "ndmi": 0.038, "ndwi": -0.5162},
    "radar": {"vv_mean": 0.138928, "vh_mean": 0.03076},
    "inspection_point": {"method": "lowest_valid_ndvi_pixel"},
}


def test_producer_orchestrator_still_helps_when_missing_one_condition():
    summary = deterministic_summary(CONTEXT)
    assert "Riesgo aún sin clasificar" in summary
    assert "Humedad ambiental alta" in summary
    assert "Humedad del suelo" in summary
    assert "Estación Norte" in summary
    assert "14 mm" in summary


def test_chat_focuses_on_question_instead_of_repeating_generic_summary():
    soil = deterministic_summary(CONTEXT, "CROP_CHAT", question="¿Cómo está el suelo?")
    forecast = deterministic_summary(CONTEXT, "CROP_CHAT", question="¿Lloverá?")
    assert "25 cb" in soil
    assert "calibrada" in soil
    assert "2026-10-21" in forecast
    assert "Riesgo aún sin clasificar" not in forecast


def test_satellite_plain_for_producer_and_numerical_for_technician():
    producer = deterministic_summary(CONTEXT, "SATELLITE_ANALYSIS", view_mode="producer")
    technical = deterministic_summary(CONTEXT, "SATELLITE_ANALYSIS", view_mode="technical")
    assert "Sentinel-2" in producer
    assert "NDVI 0.4291" not in producer
    assert "NDVI 0.4291" in technical
    assert "Sentinel-1" in technical
    assert "diagnosticar" in producer


def test_preserve_format_in_parcel_interpretation():
    raw = "**Situación**\n\n- Lluvia 5 mm\n- Suelo: sin dato"
    result = compact_widget_result({"markdown": raw}, "ORCHESTRATOR", limit=1500, preserve_format=True)
    assert result["markdown"] == raw
    assert len(result["markdown"]) <= result["max_characters"]
