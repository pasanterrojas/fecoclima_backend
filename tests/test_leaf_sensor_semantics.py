import pandas as pd

from app.services.weather import _leaf_wetness_hours


def test_zero_leaf_sensor_is_valid_dry_value():
    values = pd.Series([0] * 144)
    result = _leaf_wetness_hours(values, {"mode": "threshold", "wet_threshold": 6}, 10)
    assert result == 0.0


def test_threshold_samples_are_converted_to_hours():
    values = pd.Series([7] * 36 + [0] * 108)
    result = _leaf_wetness_hours(values, {"mode": "threshold", "wet_threshold": 6}, 10)
    assert result == 6.0
