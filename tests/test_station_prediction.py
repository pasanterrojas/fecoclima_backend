from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.station_prediction import daily_history, parcel_prediction, predict_days

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def station(**kwargs):
    return SimpleNamespace(
        timezone="UTC",
        expected_interval_minutes=60,
        integration_config={"soil_moisture_unit": "cb", "water_aggregation": {"rain_mm": "daily_counter"}},
        leaf_sensor_config={"mode": "threshold"},
        **kwargs,
    )


def samples():
    return [
        SimpleNamespace(
            sampled_at=NOW.replace(hour=h) - timedelta(days=d),
            temperature_c=20 + d,
            humidity_pct=None,
            rain_mm=h,
            wind_speed_ms=2,
            soil_temperature_c=None,
            soil_moisture_pct=88,
            evapotranspiration_mm=None,
            leaf_sensor_value=12,
        )
        for d in range(1, 9)
        for h in range(24)
    ]


def history():
    return [
        {
            "date": (NOW.date() - timedelta(days=d)).isoformat(),
            "values": {
                "temperature_avg_c": 20 + d / 10,
                "precipitation_mm": 4,
                "soil_tension_cb": 80,
                "wind_speed_ms": 2,
            },
            "coverage": {},
        }
        for d in range(8, 0, -1)
    ]


def test_daily_counter_is_not_summed_and_null_humidity_is_not_zero():
    days = daily_history(station(), samples(), NOW)
    assert len(days) == 8
    assert days[0]["values"]["precipitation_mm"] == 23
    assert "humidity_pct" not in days[0]["values"]
    assert days[0]["values"]["soil_tension_cb"] == 88
    assert "soil_moisture_pct" not in days[0]["values"]
    assert "leaf_wetness_hours" not in days[0]["values"]


def test_partial_days_duplicate_times_future_and_counter_resets_are_excluded():
    rows = samples()
    rows += [r for r in rows]  # duplicates cannot improve coverage
    rows += [SimpleNamespace(sampled_at=NOW + timedelta(days=1))]
    for r in rows:
        if r.sampled_at.day == 29 and r.sampled_at.hour == 12:
            r.rain_mm = 0
    days = daily_history(station(), rows, NOW)
    assert "precipitation_mm" not in days[-1]["values"]
    assert all(day["date"] < NOW.date().isoformat() for day in days)
    sparse = daily_history(station(), samples()[:10], NOW)
    assert not sparse[0]["values"]


def test_three_day_combination_keeps_components_and_no_soil_unit_conversion():
    models = [
        {
            "date": (NOW.date() + timedelta(days=i)).isoformat(),
            "temperature_avg_c": 30,
            "humidity_pct": 70,
            "precipitation_mm": 10,
            "estimated_soil_moisture_pct": 40,
        }
        for i in range(1, 4)
    ]
    rows = predict_days(history(), models, NOW, "UTC")
    assert len(rows) == 3
    for row in rows:
        temp = row["variables"]["temperature_avg_c"]
        assert temp["method"] == "combined" and 20 < temp["value"] < 30
        assert temp["model_value"] == 30 and temp["history_days"] == 8
        assert row["variables"]["humidity_pct"]["method"] == "open_meteo"
        assert row["variables"]["soil_moisture_pct"]["value"] == 40
        assert row["variables"]["soil_tension_cb"]["value"] == 80
        assert row["variables"]["soil_tension_cb"]["model_value"] is None


def test_model_failure_retains_recent_station_trends_without_claiming_rain_probability():
    rows = predict_days(history(), [], NOW, "UTC")
    assert all(r["variables"]["temperature_avg_c"]["method"] == "station_trend" for r in rows)
    assert all("precipitation_probability_pct" not in r for r in rows)
    stale = [{**r, "date": "2026-08-01"} for r in history()]
    assert not predict_days(stale, [], NOW, "UTC")[0]["variables"]


def test_parcel_average_uses_station_coverage_and_preserves_nulls():
    a = predict_days(history(), [], NOW, "UTC")
    b = predict_days(history(), [], NOW, "UTC")
    for d in b:
        d["variables"]["temperature_avg_c"]["value"] = 30
    result = parcel_prediction(
        [{"station_id": "a", "days": a}, {"station_id": "b", "days": b}],
        [{"station_id": "a", "coverage_pct": 75}, {"station_id": "b", "coverage_pct": 25}],
        NOW,
    )
    assert result["status"] == "COMPLETED"
    assert result["days"][0]["temperature_avg_c"] == pytest.approx(
        round(a[0]["variables"]["temperature_avg_c"]["value"] * 0.75 + 7.5, 2)
    )
    assert "humidity_pct" not in result["days"][0]["variables"]
