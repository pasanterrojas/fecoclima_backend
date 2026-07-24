from __future__ import annotations

from typing import Any

from app.services.alert_engine import ConditionSpec, evaluate_conditions


def _condition_spec(item: dict[str, Any]) -> ConditionSpec:
    return ConditionSpec(
        key=item.get("key") or item["variable"],
        label=item.get("label") or item["variable"],
        variable=item["variable"],
        operator=item["operator"],
        threshold_min=item.get("min", item.get("threshold_min")),
        threshold_max=item.get("max", item.get("threshold_max")),
        threshold_value=item.get("value", item.get("threshold_value")),
        unit=item.get("unit"),
        required=bool(item.get("required", True)),
    )


def _stage_allowed(stage_code: str | None, valid_stages: list[str]) -> bool:
    if not valid_stages:
        return True
    if not stage_code:
        return False
    normalized = stage_code.upper().strip()
    return normalized in {str(item).upper().strip() for item in valid_stages}


def evaluate_disease_projection(
    values: dict[str, float | None],
    stage_code: str | None,
    diseases: list[dict[str, Any]],
    potential_level: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate only complete published disease rules.

    A disease is projected only when every variable required by its rule is
    available and every condition is met. Incomplete rules are returned as
    limitations; partial evidence is never presented as a prediction.
    """

    matched: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for disease in diseases:
        if not disease.get("enabled_for_evaluation") or disease.get("validation_status") != "PUBLISHED":
            continue
        valid_stages = disease.get("valid_stages") or []
        if not _stage_allowed(stage_code, valid_stages):
            continue
        raw_conditions = disease.get("conditions", {}).get("all", [])
        if not raw_conditions:
            continue
        specs = [_condition_spec(item) for item in raw_conditions]
        results = evaluate_conditions(values, specs)
        missing_labels = [item.label for item in results if item.status == "missing"]
        if missing_labels:
            unresolved.append(
                {
                    "name": disease["name"],
                    "missing_variables": missing_labels,
                    "source": disease.get("source_reference"),
                }
            )
            continue
        if any(item.status != "met" for item in results):
            continue
        if potential_level == "GRAVE":
            risk = "alto"
        elif potential_level == "MODERADA":
            risk = "moderado"
        else:
            risk = "bajo"
        matched.append(
            {
                "name": disease["name"],
                "risk": risk,
                "condition_coverage_pct": 100.0,
                "reason": "; ".join(item.explanation for item in results),
                "missing_variables": [],
                "source": disease.get("source_reference"),
                "classification": "PROYECCION_NO_DIAGNOSTICA",
            }
        )
    return matched, unresolved
