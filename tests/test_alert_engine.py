from app.services.alert_engine import ConditionSpec, evaluate_daily_alert, maximum_level


def specs():
    return [
        ConditionSpec("temperature", "Temperatura", "temperature_avg_c", "between", 20, 30, unit="°C"),
        ConditionSpec("humidity", "Humedad", "humidity_avg_pct", "gt", threshold_value=80, unit="%"),
        ConditionSpec("rain", "Lluvia", "rain_sum_mm", "gt", threshold_value=.1, unit="mm"),
        ConditionSpec("soil_temperature", "Temperatura suelo", "soil_temperature_avg_c", "between", 15, 25, unit="°C"),
        ConditionSpec("leaf", "Mojado", "leaf_wetness_hours", "gte", threshold_value=6, unit="h"),
        ConditionSpec("soil_moisture", "Humedad suelo", "soil_moisture_avg_pct", "gte", threshold_value=50, unit="%"),
    ]


def test_six_conditions_is_severe():
    values = {"temperature_avg_c": 25, "humidity_avg_pct": 90, "rain_sum_mm": 1, "soil_temperature_avg_c": 20, "leaf_wetness_hours": 8, "soil_moisture_avg_pct": 65}
    result = evaluate_daily_alert(values, specs())
    assert result["level"] == "GRAVE"
    assert result["conditions_met"] == 6


def test_three_conditions_is_moderate():
    values = {"temperature_avg_c": 25, "humidity_avg_pct": 90, "rain_sum_mm": 1, "soil_temperature_avg_c": 30, "leaf_wetness_hours": 2, "soil_moisture_avg_pct": 30}
    assert evaluate_daily_alert(values, specs())["level"] == "MODERADA"


def test_missing_required_never_creates_valid_alert():
    values = {"temperature_avg_c": 25, "humidity_avg_pct": 90, "rain_sum_mm": 1, "soil_temperature_avg_c": None, "leaf_wetness_hours": 8, "soil_moisture_avg_pct": 65}
    result = evaluate_daily_alert(values, specs())
    assert result["level"] == "DATOS_INSUFICIENTES"
    assert result["completion_percentage"] < 100


def test_no_rule_configuration_is_insufficient():
    result = evaluate_daily_alert({}, [])
    assert result["level"] == "DATOS_INSUFICIENTES"
    assert result["conditions_total"] == 0


def test_maximum_ignores_insufficient_when_valid_days_exist():
    assert maximum_level([{"level": "DATOS_INSUFICIENTES"}, {"level": "NINGUNA"}, {"level": "MODERADA"}]) == "MODERADA"
