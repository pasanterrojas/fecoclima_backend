"""Evaluate available evidence without inventing values or rescaling published thresholds."""

from __future__ import annotations

from app.services.alert_engine import classify_alert_level, evaluate_disease_rules

PARTIAL_LEVEL = "EVALUACION_PARCIAL"


def with_sensor_contingency(
    result: dict,
    *,
    values: dict,
    stage_code: str | None,
    diseases: list,
    configuration: dict | None,
    observed_units: dict,
) -> dict:
    conditions = result.get("conditions") or []
    missing = [c for c in conditions if c["status"] == "missing"]
    evaluated = [c for c in conditions if c["status"] != "missing"]
    if not missing or not evaluated:
        return result
    met = result.get("conditions_met", 0)
    # Bound the published classification over EVERY possible value of the unknowns.
    # A single lower-bound label must never masquerade as the full risk level.
    possible = list(
        dict.fromkeys(
            classify_alert_level(count, len(conditions), 100, configuration, enforce_completion=False)[
                "level"
            ]
            for count in range(met, met + len(missing) + 1)
        )
    )
    decisive = len(possible) == 1 and possible[0] != "DATOS_INSUFICIENTES"
    level = possible[0] if decisive else PARTIAL_LEVEL
    minimum = classify_alert_level(met, len(conditions), 100, configuration, enforce_completion=False)
    label = minimum["label"] if decisive else "Señales disponibles · evaluación parcial"
    explanation = (
        f"Se evaluaron {len(evaluated)} de {len(conditions)} condiciones: {met} se cumplen. "
        f"{len(missing)} no tienen una medición compatible en este período. "
        + (
            "La clasificación no cambia incluso al considerar las condiciones sin medir."
            if decisive
            else "Las señales observadas permiten orientar el monitoreo; las condiciones sin medir podrían cambiar el nivel de riesgo."
        )
        + " Las mediciones ausentes no se tomaron como cero."
    )
    # Each disease still requires ALL its own measured predicates, independently of the general traffic light.
    disease_evidence = evaluate_disease_rules(values, stage_code, diseases, observed_units=observed_units)
    return {
        **result,
        "level": level,
        "level_label": label,
        "level_color": minimum["color"] if decisive else "amber",
        "assessment_status": "PARTIAL",
        "conditions_evaluated": len(evaluated),
        "conditions_unavailable": len(missing),
        "possible_levels": possible,
        "observed_risk_floor": minimum["level"],
        "classification_basis": "bounded_missing_sensor_evidence",
        "diseases": disease_evidence,
        "simple_explanation": explanation,
        "contingency_actions": [
            "Use las mediciones disponibles junto con etapa, demanda de agua y pronóstico.",
            "Revise en campo las condiciones que la estación no reporta; no equivalen a ausencia de riesgo.",
        ],
    }
