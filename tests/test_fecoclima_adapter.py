from datetime import date
from app.services.fecoclima_adapter import normalize_fecoclima_item, split_month_blocks


def test_ranges_are_split_by_year_and_six_months():
    blocks = split_month_blocks(date(2025, 10, 1), date(2026, 8, 31))
    assert [(b.year, b.month_from, b.month_to) for b in blocks] == [(2025, 10, 12), (2026, 1, 6), (2026, 7, 8)]


def test_raoul_pena_payload_mapping():
    row = normalize_fecoclima_item({"fechaMuestreo": "2023-07-10T08:40:00", "temp": 22.8, "humid": 96, "windDir": "0", "temperaturaSuelo": None})
    assert row["temperature_c"] == 22.8
    assert row["humidity_pct"] == 96
    assert row["wind_direction_deg"] == 0
    assert row["soil_temperature_c"] is None
    assert row["sampled_at"].tzinfo is not None


def test_october_provider_example_preserves_soil_leaf_zero_and_null_values():
    row = normalize_fecoclima_item({
        "nombreEstacion":"Raul Pena", "fechaMuestreo":"2026-10-01T00:20:00",
        "temp":19.6,"chill":20.5,"hIndex":None,"humid":83,"dewpt":None,
        "wind":0,"hiWind":0,"windDir":"131","rain":0,"totalRain":None,
        "barom":971.5,"solar":0,"et":0,"uv":0,
        "minTemperatura":19.6,"maxTemperatura":19.6,
        "fechaMinTemp":None,"fechaMaxTemp":None,
        "temperaturaSuelo":20.5,"humedadSuelo":100,"sensorHoja":4,
    })
    assert row['soil_moisture_pct'] == 100  # Legacy storage name; station declares the actual unit.
    assert row['leaf_sensor_value'] == 4  # Raw signal, never silently four hours of wetness.
    assert row['humidity_pct'] == 83 and row['soil_temperature_c'] == 20.5
    assert row['rain_mm'] == 0 and row['wind_speed_ms'] == 0
    assert row['heat_index_c'] is None
