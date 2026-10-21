from __future__ import annotations

from app.services.stage_codes import stages_overlap

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from math import isfinite
from typing import Any

from app.services.soil_measurements import SOIL_VARIABLE_UNITS, normalize_soil_unit

LEVEL_ORDER = {"DATOS_INSUFICIENTES": -1, "NINGUNA": 0, "MODERADA": 1, "GRAVE": 2}

DEFAULT_ALERT_CONFIGURATION: dict[str, Any] = {
    "levels": {
        "NINGUNA": {
            "label": "Sin alerta",
            "color": "green",
            "min_conditions": 0,
            "max_conditions": 1,
        },
        "MODERADA": {
            "label": "Alerta moderada",
            "color": "amber",
            "min_conditions": 2,
            "max_conditions": 4,
        },
        "GRAVE": {
            "label": "Alerta grave",
            "color": "red",
            "min_conditions": 5,
            "max_conditions": 6,
        },
        "DATOS_INSUFICIENTES": {
            "label": "Datos insuficientes",
            "color": "gray",
        },
    },
    "missing_required_policy": "DATOS_INSUFICIENTES",
    "minimum_completion_pct": 100,
}


@dataclass
class ConditionSpec:
    key: str
    label: str
    variable: str
    operator: str
    threshold_min: float | None = None
    threshold_max: float | None = None
    threshold_value: float | None = None
    unit: str | None = None
    required: bool = True
    soil_alternative: dict[str, Any] | None = None


@dataclass
class ConditionResult:
    key: str
    label: str
    status: str
    value: float | None
    unit: str | None
    explanation: str
    missing_reason: str | None = None
    observed_unit: str | None = None


def normalize_alert_configuration(configuration: dict[str, Any] | None, total_conditions: int) -> dict[str, Any]:
    """Merge versioned rule settings with safe defaults.

    The current traffic light uses 0-1 / 2-4 / 5-6. Every threshold, label and
    color can be changed in the crop's published rule version in the CMS.
    """

    normalized = deepcopy(DEFAULT_ALERT_CONFIGURATION)
    source = configuration or {}
    if isinstance(source.get("alert"), dict):
        source = source["alert"]

    if isinstance(source.get("levels"), dict):
        for code, values in source["levels"].items():
            if code in normalized["levels"] and isinstance(values, dict):
                normalized["levels"][code].update(values)

    # Compatibility with a flatter configuration used by early patches.
    thresholds = source.get("alert_thresholds") or {}
    if isinstance(thresholds, dict):
        mapping = {
            "none_min": ("NINGUNA", "min_conditions"),
            "none_max": ("NINGUNA", "max_conditions"),
            "moderate_min": ("MODERADA", "min_conditions"),
            "moderate_max": ("MODERADA", "max_conditions"),
            "severe_min": ("GRAVE", "min_conditions"),
            "severe_max": ("GRAVE", "max_conditions"),
        }
        for key, (level, target) in mapping.items():
            if thresholds.get(key) is not None:
                normalized["levels"][level][target] = int(thresholds[key])

    for key in ("missing_required_policy", "minimum_completion_pct"):
        if source.get(key) is not None:
            normalized[key] = source[key]

    # Clamp only values that would make the rule impossible to evaluate.
    for code in ("NINGUNA", "MODERADA", "GRAVE"):
        level = normalized["levels"][code]
        level["min_conditions"] = max(0, int(level.get("min_conditions", 0)))
        level["max_conditions"] = min(
            max(level["min_conditions"], int(level.get("max_conditions", total_conditions))),
            max(0, total_conditions),
        )
    normalized["minimum_completion_pct"] = max(
        0.0, min(100.0, float(normalized.get("minimum_completion_pct", 100)))
    )
    return normalized


def classify_alert_level(
    met_count: int,
    total_conditions: int,
    completion_pct: float,
    configuration: dict[str, Any] | None = None,
    *,
    missing_required: bool = False,
    enforce_completion: bool = True,
) -> dict[str, Any]:
    config = normalize_alert_configuration(configuration, total_conditions)
    insufficient = config["levels"]["DATOS_INSUFICIENTES"]
    policy = str(config.get("missing_required_policy") or "DATOS_INSUFICIENTES").upper()
    minimum_completion = float(config.get("minimum_completion_pct", 100))

    if enforce_completion and (
        (missing_required and policy == "DATOS_INSUFICIENTES")
        or completion_pct < minimum_completion
    ):
        return {
            "level": "DATOS_INSUFICIENTES",
            "label": insufficient.get("label", "Datos insuficientes"),
            "color": insufficient.get("color", "gray"),
            "configuration": config,
        }

    for code in ("GRAVE", "MODERADA", "NINGUNA"):
        item = config["levels"][code]
        if int(item["min_conditions"]) <= met_count <= int(item["max_conditions"]):
            return {
                "level": code,
                "label": item.get("label", code.title()),
                "color": item.get("color", "gray"),
                "configuration": config,
            }

    # A gap in administrator thresholds must never invent a classification.
    return {
        "level": "DATOS_INSUFICIENTES",
        "label": insufficient.get("label", "Datos insuficientes"),
        "color": insufficient.get("color", "gray"),
        "configuration": config,
    }


def _matches(value: float, spec: ConditionSpec) -> bool:
    if spec.operator == "between":
        return spec.threshold_min is not None and spec.threshold_max is not None and spec.threshold_min <= value <= spec.threshold_max
    if spec.operator == "gt":
        return spec.threshold_value is not None and value > spec.threshold_value
    if spec.operator == "gte":
        return spec.threshold_value is not None and value >= spec.threshold_value
    if spec.operator == "lt":
        return spec.threshold_value is not None and value < spec.threshold_value
    if spec.operator == "lte":
        return spec.threshold_value is not None and value <= spec.threshold_value
    if spec.operator == "eq":
        return spec.threshold_value is not None and value == spec.threshold_value
    raise ValueError(f"Operador no soportado: {spec.operator}")


def _expected_text(spec: ConditionSpec) -> str:
    unit = f" {spec.unit}" if spec.unit else ""
    if spec.operator == "between":
        return f"entre {spec.threshold_min:g} y {spec.threshold_max:g}{unit}"
    symbols = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "="}
    return f"{symbols.get(spec.operator, spec.operator)} {spec.threshold_value:g}{unit}"


def select_soil_criterion(spec: ConditionSpec, measured_unit: str | None) -> ConditionSpec:
    """Choose an explicitly configured criterion; never convert a measurement."""
    alternative = spec.soil_alternative
    unit = normalize_soil_unit(measured_unit)
    if (spec.variable not in SOIL_VARIABLE_UNITS or not isinstance(alternative, dict)
            or unit not in {"%", "cb"} or unit == SOIL_VARIABLE_UNITS[spec.variable]
            or alternative.get("unit") != unit or not str(alternative.get("source_reference") or "").strip()):
        return spec
    variable = "soil_tension_avg_cb" if unit == "cb" else "soil_moisture_avg_pct"
    return replace(spec, variable=variable, unit=unit, operator=alternative["operator"],
                   label="Tensión del agua en el suelo" if unit == "cb" else "Humedad del suelo",
                   threshold_min=alternative.get("threshold_min"), threshold_max=alternative.get("threshold_max"),
                   threshold_value=alternative.get("threshold_value"), soil_alternative=None)


def evaluate_conditions(
    values: dict[str, float | None],
    specs: list[ConditionSpec],
    *,
    observed_units: dict[str, str] | None = None,
) -> list[ConditionResult]:
    results: list[ConditionResult] = []
    for spec in sorted(specs, key=lambda item: item.key):
        spec = select_soil_criterion(spec, (observed_units or {}).get(spec.variable))
        value = values.get(spec.variable)
        expected_unit = SOIL_VARIABLE_UNITS.get(spec.variable)
        actual_unit = (observed_units or {}).get(spec.variable)
        if expected_unit and (
            (spec.unit and normalize_soil_unit(spec.unit) != expected_unit)
            or (actual_unit is not None and normalize_soil_unit(actual_unit) != expected_unit)
        ):
            observed = normalize_soil_unit(actual_unit) if actual_unit is not None else expected_unit
            results.append(ConditionResult(
                spec.key, spec.label, "missing", None, spec.unit or expected_unit,
                f"La estación reporta el suelo en {observed or 'una unidad no confirmada'}; "
                f"esta condición exige {spec.unit or expected_unit}. "
                "Tensión y porcentaje no son equivalentes; configure una regla en la unidad del sensor.",
                missing_reason="incompatible_unit", observed_unit=observed,
            ))
            continue
        if value is None or isinstance(value, bool) or not isfinite(float(value)):
            results.append(ConditionResult(
                spec.key, spec.label, "missing", None, spec.unit,
                "La variable requerida no tiene una medición válida disponible.",
                missing_reason="no_valid_readings",
            ))
            continue
        met = _matches(float(value), spec)
        results.append(
            ConditionResult(
                spec.key,
                spec.label,
                "met" if met else "not_met",
                float(value),
                spec.unit,
                f"Valor observado {value:g} {spec.unit or ''}; condición esperada {_expected_text(spec)}.",
            )
        )
    return results


def evaluate_disease_rules(
    values: dict[str, float | None],
    stage_code: str | None,
    rules: list[dict[str, Any]],
    *,
    observed_units: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    diseases: list[dict[str, str]] = []
    for rule in rules:
        if not rule.get("enabled_for_evaluation") or rule.get("validation_status") != "PUBLISHED":
            continue
        valid_stages = rule.get("valid_stages") or []
        if valid_stages and stage_code and not stages_overlap(stage_code, valid_stages):
            continue
        if valid_stages and not stage_code:
            continue
        checks: list[bool] = []
        reasons: list[str] = []
        missing = False
        for condition in rule.get("conditions", {}).get("all", []):
            variable = condition["variable"]
            observed = values.get(variable)
            if observed is None:
                missing = True
                break
            spec = ConditionSpec(
                key=variable,
                label=condition.get("label") or variable,
                variable=variable,
                operator=condition["operator"],
                threshold_min=condition.get("min", condition.get("threshold_min")),
                threshold_max=condition.get("max", condition.get("threshold_max")),
                threshold_value=condition.get("value", condition.get("threshold_value")),
                unit=condition.get("unit"),
                required=True,
            )
            checked = evaluate_conditions(values, [spec], observed_units=observed_units)[0]
            if checked.status == "missing":
                missing = True
                break
            checks.append(checked.status == "met")
            reasons.append(f"{variable}={observed:g}")
        if missing or not checks or not all(checks):
            continue
        diseases.append({
            "name": rule["name"],
            "risk": rule.get("risk") or (rule.get("conditions") or {}).get("risk", "moderado"),
            "reason": ", ".join(reasons) + (f"; etapa {stage_code}" if stage_code else ""),
            "source": rule.get("source_reference"),
        })
    return diseases


def evaluate_daily_alert(
    values: dict[str, float | None],
    specs: list[ConditionSpec],
    *,
    stage_code: str | None = None,
    disease_rules: list[dict[str, Any]] | None = None,
    data_completeness: float = 1.0,
    alert_configuration: dict[str, Any] | None = None,
    observed_units: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not specs:
        classification = classify_alert_level(0, 0, 0, alert_configuration, missing_required=True)
        return {
            "level": classification["level"], "level_label": classification["label"], "level_color": classification["color"],
            "conditions_met": 0, "conditions_total": 0,
            "completion_percentage": 0.0, "confidence": 0.0, "conditions": [], "diseases": [],
            "simple_explanation": "No existe una versión publicada con condiciones base configuradas.",
            "values_used": values,
        }
    conditions = evaluate_conditions(values, specs, observed_units=observed_units)
    required_keys = {spec.key for spec in specs if spec.required}
    missing_required = [item for item in conditions if item.status == "missing" and item.key in required_keys]
    met_count = sum(item.status == "met" for item in conditions)
    completion = round(100 * sum(item.status != "missing" for item in conditions) / max(1, len(conditions)), 1)
    confidence = round(max(0.0, min(1.0, (completion / 100) * data_completeness)), 3)
    classification = classify_alert_level(
        met_count,
        len(specs),
        completion,
        alert_configuration,
        missing_required=bool(missing_required),
    )
    level = classification["level"]
    if level == "DATOS_INSUFICIENTES":
        explanation = (
            f"No se calcula una alerta válida: faltan {len(missing_required)} variables obligatorias "
            f"o no se alcanza la completitud mínima configurada."
        )
        unit_issues = [item.explanation for item in missing_required if item.missing_reason == "incompatible_unit"]
        if unit_issues:
            explanation = unit_issues[0]
    elif level == "GRAVE":
        explanation = f"Se cumplieron {met_count} de {len(specs)} condiciones; la versión publicada clasifica el resultado como grave."
    elif level == "MODERADA":
        explanation = f"Se cumplieron {met_count} de {len(specs)} condiciones; la versión publicada clasifica el resultado como moderado."
    else:
        explanation = f"Se cumplieron {met_count} de {len(specs)} condiciones; la versión publicada no identifica una alerta relevante."
    diseases = [] if level == "DATOS_INSUFICIENTES" else evaluate_disease_rules(
        values, stage_code, disease_rules or [], observed_units=observed_units,
    )
    return {
        "level": level,
        "level_label": classification["label"],
        "level_color": classification["color"],
        "conditions_met": met_count,
        "conditions_total": len(specs),
        "completion_percentage": completion,
        "confidence": confidence,
        "conditions": [asdict(item) for item in conditions],
        "missing_reasons": [item.explanation for item in conditions if item.status == "missing"],
        "diseases": diseases,
        "simple_explanation": explanation,
        "values_used": values,
        "alert_configuration": classification["configuration"],
    }


def maximum_level(evaluations: list[dict[str, Any]]) -> str:
    if not evaluations:
        return "DATOS_INSUFICIENTES"
    valid = [item["level"] for item in evaluations if item["level"] != "DATOS_INSUFICIENTES"]
    if not valid:
        return "DATOS_INSUFICIENTES"
    return max(valid, key=lambda level: LEVEL_ORDER[level])
