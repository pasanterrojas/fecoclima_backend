"""Configuration fingerprints detect functional drift."""
from app.scripts.release_state import digest


def test_configuration_fingerprint_detects_prompt_and_sensor_changes():
    config = {"system_prompt": "Explique con palabras sencillas", "rain_mm": "daily_counter"}
    assert digest(config) == digest(dict(reversed(list(config.items()))))
    assert digest(config) != digest({**config, "system_prompt": "Otro tono"})
    assert digest(config) != digest({**config, "rain_mm": "interval_sum"})


