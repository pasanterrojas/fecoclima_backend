from types import SimpleNamespace

from app.api.endpoints.public import _soil_moisture_presentation
from app.services.weather import RAUL_PENA_GUID, summarize_readings_for_conditions


def test_centibar_scale_contains_requested_interpretation_ranges() -> None:
    station = SimpleNamespace(integration_config={"soil_moisture_unit": "cb"})
    unit, scale = _soil_moisture_presentation(station)

    assert unit == "cb"
    assert scale is not None
    assert [(item["from"], item["to"]) for item in scale["ranges"]] == [
        (0, 10),
        (10, 30),
        (30, 60),
        (60, 100),
        (100, 200),
    ]
    assert scale["source_url"].startswith("https://")


def test_existing_raul_pena_station_gets_centibar_scale_without_reseeding() -> None:
    station = SimpleNamespace(integration_config={}, external_guid=RAUL_PENA_GUID.lower())
    unit, scale = _soil_moisture_presentation(station)
    assert unit == "cb"
    assert scale is not None


def test_condition_chart_uses_exact_period_aggregates() -> None:
    station = SimpleNamespace(
        leaf_sensor_config={"mode": "threshold", "wet_threshold": 6},
        expected_interval_minutes=10,
        external_guid=None,
    )
    readings = [
        SimpleNamespace(
            temperature_c=18,
            humidity_pct=70,
            rain_mm=0.1,
            soil_temperature_c=15,
            soil_moisture_pct=40,
            leaf_sensor_value=6,
            solar_wm2=100,
            evapotranspiration_mm=0.05,
        ),
        SimpleNamespace(
            temperature_c=22,
            humidity_pct=90,
            rain_mm=0.3,
            soil_temperature_c=19,
            soil_moisture_pct=60,
            leaf_sensor_value=0,
            solar_wm2=200,
            evapotranspiration_mm=0.1,
        ),
    ]

    values = summarize_readings_for_conditions(readings, station)
    assert values["temperature_avg_c"] == 20
    assert values["humidity_avg_pct"] == 80
    assert values["rain_sum_mm"] == 0.4
    assert values["soil_moisture_avg_pct"] == 50
    assert values["leaf_wetness_hours"] == 0.17
