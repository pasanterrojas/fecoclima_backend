from app.services.forecast import aggregate_open_meteo_payload


def test_open_meteo_hourly_variables_make_six_condition_projection_possible():
    payload = {
        "daily": {
            "time": ["2026-07-24"],
            "temperature_2m_max": [28.0],
            "temperature_2m_min": [20.0],
            "precipitation_sum": [2.5],
            "precipitation_probability_max": [80],
            "relative_humidity_2m_mean": [88],
            "wind_speed_10m_max": [18],
            "et0_fao_evapotranspiration": [2.1],
        },
        "hourly": {
            "time": [f"2026-07-24T{hour:02d}:00" for hour in range(24)],
            "temperature_2m": [24.0] * 24,
            "relative_humidity_2m": [92.0] * 8 + [75.0] * 16,
            "precipitation": [0.0] * 6 + [0.4] * 2 + [0.0] * 16,
            "soil_temperature_6cm": [21.0] * 24,
            "soil_moisture_3_to_9cm": [0.58] * 24,
        },
    }
    rows = aggregate_open_meteo_payload(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row["temperature_avg_c"] == 24.0
    assert row["soil_temperature_avg_c"] == 21.0
    assert row["estimated_soil_moisture_pct"] == 58.0
    assert row["leaf_wetness_hours"] == 8.0
    assert row["leaf_wetness_method"] == "estimated_rh90_or_precipitation"


def test_missing_model_hourly_data_does_not_invent_zero_leaf_wetness():
    rows = aggregate_open_meteo_payload({"daily":{"time":["2026-10-31"]},
        "hourly":{"time":[f"2026-10-31T{h:02d}:00" for h in range(24)]}})
    assert rows[0]["leaf_wetness_hours"] is None
    assert rows[0]["wind_speed_avg_ms"] is None
