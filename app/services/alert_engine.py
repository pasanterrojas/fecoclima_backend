from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

LEVEL_ORDER = {"DATOS_INSUFICIENTES": -1, "NINGUNA": 0, "MODERADA": 1, "GRAVE": 2}


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


@dataclass
class ConditionResult:
    key: str
    label: str
    status: str
    value: float | None
    unit: str | None
    explanation: str


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


def evaluate_conditions(values: dict[str, float | None], specs: list[ConditionSpec]) -> list[ConditionResult]:
    results: list[ConditionResult] = []
    for spec in sorted(specs, key=lambda item: item.key):
        value = values.get(spec.variable)
        if value is None:
            results.append(ConditionResult(spec.key, spec.label, "missing", None, spec.unit, "La variable obligatoria no está disponible."))
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


def evaluate_disease_rules(values: dict[str, float | None], stage_code: str | None, rules: list[dict[str, Any]]) -> list[dict[str, str]]:
    diseases: list[dict[str, str]] = []
    for rule in rules:
        if not rule.get("enabled_for_evaluation") or rule.get("validation_status") != "PUBLISHED":
            continue
        valid_stages = rule.get("valid_stages") or []
        if valid_stages and stage_code and stage_code not in valid_stages:
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
                label=variable,
                variable=variable,
                operator=condition["operator"],
                threshold_min=condition.get("min"),
                threshold_max=condition.get("max"),
                threshold_value=condition.get("value"),
                unit=condition.get("unit"),
                required=True,
            )
            checks.append(_matches(float(observed), spec))
            reasons.append(f"{variable}={observed:g}")
        if missing or not checks or not all(checks):
            continue
        diseases.append({"name": rule["name"], "risk": rule.get("risk", "moderado"), "reason": ", ".join(reasons) + (f"; etapa {stage_code}" if stage_code else "")})
    return diseases


def evaluate_daily_alert(
    values: dict[str, float | None],
    specs: list[ConditionSpec],
    *,
    stage_code: str | None = None,
    disease_rules: list[dict[str, Any]] | None = None,
    data_completeness: float = 1.0,
) -> dict[str, Any]:
    if not specs:
        return {
            "level": "DATOS_INSUFICIENTES", "conditions_met": 0, "conditions_total": 0,
            "completion_percentage": 0.0, "confidence": 0.0, "conditions": [], "diseases": [],
            "simple_explanation": "No existe una versión publicada con condiciones base configuradas.",
            "values_used": values,
        }
    conditions = evaluate_conditions(values, specs)
    required_keys = {spec.key for spec in specs if spec.required}
    missing_required = [item for item in conditions if item.status == "missing" and item.key in required_keys]
    met_count = sum(item.status == "met" for item in conditions)
    completion = round(100 * sum(item.status != "missing" for item in conditions) / max(1, len(conditions)), 1)
    confidence = round(max(0.0, min(1.0, (completion / 100) * data_completeness)), 3)
    if missing_required:
        level = "DATOS_INSUFICIENTES"
        explanation = f"No se calcula una alerta válida porque faltan {len(missing_required)} variables obligatorias."
    elif met_count == len(specs):
        level = "GRAVE"
        explanation = "Las seis condiciones base se cumplieron durante el día evaluado."
    elif met_count >= 3:
        level = "MODERADA"
        explanation = f"Se cumplieron {met_count} de {len(specs)} condiciones base; conviene reforzar el monitoreo en campo."
    else:
        level = "NINGUNA"
        explanation = f"Se cumplieron {met_count} de {len(specs)} condiciones base. No se identifica una alerta relevante con esta regla."
    diseases = [] if level == "DATOS_INSUFICIENTES" else evaluate_disease_rules(values, stage_code, disease_rules or [])
    return {
        "level": level,
        "conditions_met": met_count,
        "conditions_total": len(specs),
        "completion_percentage": completion,
        "confidence": confidence,
        "conditions": [asdict(item) for item in conditions],
        "diseases": diseases,
        "simple_explanation": explanation,
        "values_used": values,
    }


def maximum_level(evaluations: list[dict[str, Any]]) -> str:
    if not evaluations:
        return "DATOS_INSUFICIENTES"
    valid = [item["level"] for item in evaluations if item["level"] != "DATOS_INSUFICIENTES"]
    if not valid:
        return "DATOS_INSUFICIENTES"
    return max(valid, key=lambda level: LEVEL_ORDER[level])
