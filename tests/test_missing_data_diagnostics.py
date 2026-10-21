"""Regression checks for the causes shown to producers as 'datos faltantes'."""
from types import SimpleNamespace

from app.services.alert_engine import ConditionSpec
from app.services.fecoclima_adapter import normalize_fecoclima_item
from app.services.plot_pipeline import diagnose_window


def _window(**updates):
    value = {
        'samples': 69, 'expected_samples': 72, 'hours': 72,
        'completeness_pct': 95.83, 'stale': False,
        'units': {'soil_moisture_avg_pct': 'cb'},
        'variable_completeness_pct': {
            'temperature_avg_c': 95.83,
            'soil_moisture_avg_pct': 95.83,
            'leaf_wetness_hours': 0,
        },
    }
    value.update(updates)
    return value


def _station(mode='threshold'):
    return SimpleNamespace(
        leaf_sensor_config={'mode': mode, 'wet_threshold': 6},
        external_guid='example-other-station', integration_config={'soil_moisture_unit': 'cb'},
    )


def _spec(label, variable, unit):
    return ConditionSpec(key=variable, label=label, variable=variable, operator='gt', threshold_value=1, unit=unit)


def test_zero_or_missing_sensor_has_a_specific_explanation():
    spec = _spec('Humedad foliar', 'leaf_wetness_hours', 'h')
    issues = diagnose_window(_window(), [spec], _station())
    assert any(issue['code'] == 'VARIABLE_NO_READINGS' and issue['variable'] == spec.variable for issue in issues)


def test_incompatible_soil_unit_is_not_falsely_interpreted_as_percentage():
    issues = diagnose_window(_window(), [_spec('Humedad del suelo', 'soil_moisture_avg_pct', '%')], _station())
    assert any(issue['code'] == 'SOIL_UNIT_MISMATCH' and 'cb' in issue['message'] for issue in issues)


def test_unpublished_rule_and_no_readings_produce_distinct_causes():
    issues = diagnose_window(
        _window(samples=0, expected_samples=72, completeness_pct=0, stale=True),
        [], _station(), has_rule=False, has_phenology=False,
    )
    assert {'RULE_INCOMPLETE', 'NO_READINGS', 'PHENOLOGY_UNAVAILABLE'}.issubset({issue['code'] for issue in issues})


def test_healthy_station_does_not_report_false_gaps():
    specs = [ConditionSpec(
        key=f'condition_{i}', label='Temperatura', variable='temperature_avg_c',
        operator='gt', threshold_value=i, unit='°C',
    ) for i in range(6)]
    issues = diagnose_window(_window(), specs, _station())
    assert issues == []


def test_invalid_optional_timestamp_and_nan_do_not_destroy_valid_reading():
    row = normalize_fecoclima_item({
        'fechaMuestreo': '2026-10-19T08:00:00',
        'fechaMinTemp': 'NO DISPONIBLE', 'temp': 0, 'humid': 'NaN',
    })
    assert row['temperature_c'] == 0
    assert row['humidity_pct'] is None
    assert row['min_temperature_at'] is None
