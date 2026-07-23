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
