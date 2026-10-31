"""Measured tension is a separate rule input, never a fabricated percentage."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.db.models import Station, WeatherReading
from app.schemas.actions import BaseConditionPayload
from app.schemas.resources import StationPayload
from app.services.alert_engine import ConditionSpec, evaluate_conditions, evaluate_daily_alert, evaluate_disease_rules
from app.services.plot_pipeline import adapt_soil_condition_for_station, diagnose_window, evaluate_window, rolling_windows
from app.services.rule_validation import validate_condition_dict
from app.services.soil_measurements import soil_rule_units, soil_rule_values
from app.services.weather import effective_soil_moisture_unit


def station(*, soil_unit="cb", enabled=True, threshold=30, validated=True, reference="Informe de validación local de prueba"):
    return SimpleNamespace(
        id="test-station",
        external_guid="C2694D32-5A4B-48C0-A3B6-8345D9E4935B",
        integration_config={
            "soil_moisture_unit": soil_unit,
            "soil_tension_proxy_enabled": enabled,
            "soil_tension_wet_max_cb": threshold,
            "soil_tension_rule_validated": validated,
            "soil_tension_rule_reference": reference,
        },
    )


def standard_soil():
    return ConditionSpec(
        key="soil_moisture", label="Humedad del suelo igual o superior a 50%",
        variable="soil_moisture_avg_pct", operator="gte", threshold_value=50, unit="%",
    )


@pytest.mark.parametrize('unit,value,status', [('cb',39,'met'),('cb',40,'met'),('cb',41,'not_met'),('cb',80,'not_met'),('%',50,'met'),('%',49,'not_met')])
def test_configured_soil_alert_is_at_most_40_cb_without_changing_percent_rule(unit, value, status):
    spec = standard_soil()
    spec.soil_alternative = {'unit':'cb','operator':'lte','threshold_value':40,
                             'source_reference':'Criterio de prueba solicitado por administración'}
    result = evaluate_conditions(soil_rule_values({'soil_moisture_avg_pct':value},unit),[spec],observed_units=soil_rule_units(unit))
    assert len(result) == 1
    assert (result[0].unit,result[0].value,result[0].status) == (unit,value,status)


@pytest.mark.parametrize('color', ['red','#12345','<svg/>',None])
def test_station_rejects_invalid_influence_color(color):
    with pytest.raises(ValidationError):
        StationPayload(name='Estación',integration_config={'map_color':color})


def test_station_persists_normalized_color_and_independent_soil_unit():
    value=StationPayload(name='Estación',integration_config={'map_color':'#Aa00Ff','soil_moisture_unit':'cb'})
    assert value.integration_config == {'map_color':'#aa00ff','soil_moisture_unit':'cb'}


@pytest.mark.parametrize('unit,value,status', [('%', 70, 'met'), ('%', 40, 'not_met'), ('cb', 20, 'met'), ('cb', 100, 'not_met')])
def test_crop_criterion_selects_its_sensor_unit_without_conversion(unit, value, status):
    spec = standard_soil()
    spec.soil_alternative = {'unit': 'cb', 'operator': 'lte', 'threshold_value': 35,
                             'source_reference': 'Criterio sintético exclusivamente para pruebas'}
    results = evaluate_conditions(soil_rule_values({'soil_moisture_avg_pct': value}, unit), [spec],
                                  observed_units=soil_rule_units(unit))
    assert len(results) == 1  # One condition, not two votes in the traffic light.
    assert (results[0].status, results[0].value, results[0].unit) == (status, value, unit)


@pytest.mark.parametrize('changes', [{'unit':'%'}, {'source_reference':''}, {'threshold_value':float('nan')}])
def test_alternative_criterion_requires_other_unit_and_explicit_valid_parameters(changes):
    alternative = {'unit':'cb','operator':'lte','threshold_value':35,'source_reference':'Prueba sintética'} | changes
    with pytest.raises(ValidationError):
        BaseConditionPayload(key='soil',label='Agua en el suelo',variable='soil_moisture_avg_pct',
                             operator='gte',threshold_value=50,unit='%',order_index=4,soil_alternative=alternative)


def test_tensiometer_reading_is_evaluated_in_centibars_not_fake_percentages():
    specs, adaptations = adapt_soil_condition_for_station([standard_soil()], station())
    assert len(adaptations) == 1
    assert adaptations[0]["method"] == "soil_tension_explicit_local_alternative"
    assert (specs[0].unit, specs[0].operator, specs[0].threshold_value) == ("cb", "lte", 30)
    assert specs[0].variable == "soil_tension_avg_cb"
    wet = evaluate_conditions(soil_rule_values({"soil_moisture_avg_pct": 20}, "cb"), specs)
    dry = evaluate_conditions(soil_rule_values({"soil_moisture_avg_pct": 65}, "cb"), specs)
    assert wet[0].status == "met" and dry[0].status == "not_met"
    assert wet[0].value == 20 and wet[0].unit == "cb"


def test_station_threshold_is_adjustable():
    specs, _ = adapt_soil_condition_for_station([standard_soil()], station(threshold=40))
    assert specs[0].threshold_value == 40


def test_percent_station_does_not_change_rule():
    original = standard_soil()
    specs, adaptations = adapt_soil_condition_for_station([original], station(soil_unit="%"))
    assert specs == [original] and not adaptations


def test_disabled_proxy_returns_original_mismatch_not_a_fabricated_result():
    original = standard_soil()
    specs, adaptations = adapt_soil_condition_for_station([original], station(enabled=False))
    assert specs == [original] and not adaptations


def test_custom_percent_rule_must_not_be_silently_overridden():
    original = standard_soil()
    original.threshold_value = 60
    specs, adaptations = adapt_soil_condition_for_station([original], station())
    assert specs == [original] and not adaptations


def test_invalid_threshold_disables_proxy():
    original = standard_soil()
    specs, adaptations = adapt_soil_condition_for_station([original], station(threshold="invalid"))
    assert specs == [original] and not adaptations


@pytest.mark.parametrize("changes", [
    {"validated": False}, {"reference": ""}, {"threshold": None},
    {"threshold": True}, {"threshold": float("nan")}, {"threshold": float("inf")},
])
def test_no_unvalidated_or_missing_threshold_is_used(changes):
    original = standard_soil()
    specs, adaptations = adapt_soil_condition_for_station([original], station(**changes))
    assert specs == [original] and not adaptations


def test_old_ui_defaults_do_not_count_as_technical_validation():
    legacy = station()
    legacy.integration_config = {
        "soil_moisture_unit": "cb", "soil_tension_proxy_enabled": True,
        "soil_tension_wet_max_cb": 30,
    }
    original = standard_soil()
    assert adapt_soil_condition_for_station([original], legacy) == ([original], [])


def test_cb_and_percentage_channels_are_physically_separate():
    values = {"soil_moisture_avg_pct": 70, "temperature_avg_c": 25}
    cb = soil_rule_values(values, "cb")
    pct = soil_rule_values(values, "%")
    assert cb == {"soil_moisture_avg_pct": None, "soil_tension_avg_cb": 70, "temperature_avg_c": 25}
    assert pct == {"soil_moisture_avg_pct": 70, "soil_tension_avg_cb": None, "temperature_avg_c": 25}
    assert values["soil_moisture_avg_pct"] == 70


def test_unknown_unit_is_not_silently_treated_as_percentage():
    assert effective_soil_moisture_unit(station(soil_unit="cp")) == "cp"
    values = soil_rule_values({"soil_moisture_avg_pct": 70}, "cp")
    assert values == {"soil_moisture_avg_pct": None, "soil_tension_avg_cb": None}
    with pytest.raises(ValidationError, match="unidad"):
        StationPayload(name="Prueba", integration_config={"soil_moisture_unit": "cp"})


def test_daily_engine_rejects_mismatched_units_with_an_actionable_reason():
    result = evaluate_daily_alert(
        {"soil_moisture_avg_pct": 70}, [standard_soil()],
        observed_units=soil_rule_units("cb"),
    )
    condition = result["conditions"][0]
    assert result["level"] == "DATOS_INSUFICIENTES"
    assert result["completion_percentage"] == 0
    assert condition["missing_reason"] == "incompatible_unit"
    assert condition["observed_unit"] == "cb"
    assert "no son equivalentes" in condition["explanation"]


def test_explicit_crop_tension_rule_is_evaluated_without_percent_proxy():
    spec = ConditionSpec(
        "soil_tension", "Criterio local de tensión", "soil_tension_avg_cb",
        "lte", threshold_value=25, unit="cb",
    )
    result = evaluate_conditions(
        soil_rule_values({"soil_moisture_avg_pct": 20}, "cb"), [spec],
        observed_units=soil_rule_units("cb"),
    )[0]
    assert (result.status, result.value, result.unit) == ("met", 20, "cb")
    incompatible = evaluate_conditions(
        soil_rule_values({"soil_moisture_avg_pct": 20}, "%"), [spec],
        observed_units=soil_rule_units("%"),
    )[0]
    assert incompatible.status == "missing" and incompatible.missing_reason == "incompatible_unit"


@pytest.mark.parametrize("variable,unit", [("soil_moisture_avg_pct", "cb"), ("soil_tension_avg_cb", "%")])
def test_rules_require_the_unit_of_their_physical_variable(variable, unit):
    data = {
        "key": "soil", "label": "Estado del suelo", "variable": variable,
        "operator": "lte", "threshold_value": 25, "unit": unit, "order_index": 1,
    }
    with pytest.raises(ValidationError, match="requiere unidad"):
        BaseConditionPayload(**data)
    assert validate_condition_dict(data)


def test_tension_rule_payload_sets_canonical_unit():
    payload = BaseConditionPayload(
        key="soil", label="Tensión del suelo", variable="soil_tension_avg_cb",
        operator="lte", threshold_value=25, order_index=1,
    )
    assert payload.unit == "cb"


def test_disease_rule_cannot_reuse_a_cb_measurement_as_percent():
    disease = {
        "name": "Riesgo de prueba", "enabled_for_evaluation": True, "validation_status": "PUBLISHED",
        "conditions": {"all": [{"variable": "soil_moisture_avg_pct", "operator": "gte", "value": 50, "unit": "%"}]},
    }
    assert evaluate_disease_rules(
        {"soil_moisture_avg_pct": 70}, None, [disease], observed_units=soil_rule_units("cb"),
    ) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_non_finite_or_boolean_measurements_do_not_become_false_results(value):
    result = evaluate_conditions({"soil_moisture_avg_pct": value}, [standard_soil()])[0]
    assert result.status == "missing"


def test_window_uses_explicit_tension_rule_and_reports_missing_percent_correctly():
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    source = Station(
        external_guid="OTHER", expected_interval_minutes=60,
        integration_config={"soil_moisture_unit": "cb"}, leaf_sensor_config={"mode": "hours"},
    )
    rows = [WeatherReading(
        sampled_at=now-timedelta(hours=i), temperature_c=24, humidity_pct=85,
        rain_mm=0, soil_temperature_c=22, soil_moisture_pct=20,
        leaf_sensor_value=8, solar_wm2=None, evapotranspiration_mm=None,
    ) for i in range(72)]
    window = rolling_windows(rows, source, now)["72"]
    other = [ConditionSpec(f"condition_{i}", "Temperatura", "temperature_avg_c", "gte", threshold_value=20, unit="°C") for i in range(5)]
    tension = ConditionSpec("soil", "Tensión local", "soil_tension_avg_cb", "lte", threshold_value=25, unit="cb")
    rule = SimpleNamespace(configuration={})
    complete = evaluate_window(window, [tension, *other], [], {"stage_code": "V2"}, rule)
    assert complete["level"] == "GRAVE" and complete["conditions_met"] == 6
    assert diagnose_window(window, [tension, *other], source) == []
    incomplete = evaluate_window(window, [standard_soil(), *other], [], {"stage_code": "V2"}, rule)
    assert incomplete['level'] == 'GRAVE'
    assert incomplete['assessment_status'] == 'PARTIAL'
    assert incomplete["conditions_met"] == 5
    soil = next(item for item in incomplete["conditions"] if item["key"] == "soil_moisture")
    assert soil["missing_reason"] == "incompatible_unit"
    assert "no son equivalentes" in incomplete["missing_reasons"][0]
