from __future__ import annotations

from collections import Counter
from app.services.stage_codes import expand_stage_codes
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BaseCondition, CropRuleVersion, DiseaseRule, PhenologyStage, Station, WeatherDailySummary
from app.services.alert_engine import ConditionSpec, normalize_alert_configuration, evaluate_daily_alert
from app.services.phenology import calculate_stage
from app.services.soil_measurements import SOIL_VARIABLE_UNITS, normalize_soil_unit, soil_rule_units, soil_rule_values

SUPPORTED_VARIABLES = {
    "temperature_avg_c",
    "humidity_avg_pct",
    "rain_sum_mm",
    "soil_temperature_avg_c",
    "soil_moisture_avg_pct",
    "soil_tension_avg_cb",
    "leaf_wetness_hours",
    "solar_sum_wm2_samples",
    "evapotranspiration_sum_mm",
}
SUPPORTED_OPERATORS = {"between", "gt", "gte", "lt", "lte", "eq"}


def validate_condition_dict(condition: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if condition.get("variable") not in SUPPORTED_VARIABLES:
        errors.append(f"Variable no soportada: {condition.get('variable')}")
    soil_unit = SOIL_VARIABLE_UNITS.get(condition.get("variable"))
    if soil_unit and condition.get("unit") and normalize_soil_unit(condition["unit"]) != soil_unit:
        errors.append(f"La variable {condition['variable']} requiere unidad {soil_unit}; tensión y porcentaje no son equivalentes")
    alternative = condition.get("soil_alternative")
    if alternative is not None:
        from app.schemas.actions import SoilAlternativePayload
        from pydantic import ValidationError
        try:
            parsed = SoilAlternativePayload.model_validate(alternative)
            if not soil_unit or parsed.unit == soil_unit:
                errors.append("El criterio alternativo requiere la otra unidad de suelo (% o cb)")
        except ValidationError:
            errors.append("El criterio alternativo del suelo no tiene umbrales y referencia válidos")
    operator = condition.get("operator")
    if operator not in SUPPORTED_OPERATORS:
        errors.append(f"Operador no soportado: {operator}")
    if operator == "between":
        if condition.get("threshold_min") is None or condition.get("threshold_max") is None:
            errors.append("El operador between requiere mínimo y máximo")
        elif float(condition["threshold_min"]) > float(condition["threshold_max"]):
            errors.append("El mínimo no puede superar el máximo")
    elif operator in SUPPORTED_OPERATORS and condition.get("threshold_value") is None:
        errors.append(f"El operador {operator} requiere threshold_value")
    return errors


async def validate_rule_version(db: AsyncSession, rule_id: UUID) -> dict[str, Any]:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule:
        raise ValueError("Versión de regla no encontrada")
    conditions = list(
        (
            await db.scalars(
                select(BaseCondition)
                .where(BaseCondition.rule_version_id == rule.id, BaseCondition.active.is_(True))
                .order_by(BaseCondition.order_index)
            )
        ).all()
    )
    diseases = list(
        (
            await db.scalars(
                select(DiseaseRule)
                .where(DiseaseRule.rule_version_id == rule.id, DiseaseRule.active.is_(True))
                .order_by(DiseaseRule.name)
            )
        ).all()
    )
    stage_codes = set(
        (
            await db.scalars(
                select(PhenologyStage.code).where(
                    PhenologyStage.crop_id == rule.crop_id,
                    PhenologyStage.active.is_(True),
                    PhenologyStage.deleted_at.is_(None),
                )
            )
        ).all()
    )
    errors: list[str] = []
    warnings: list[str] = []
    if len(conditions) != 6:
        errors.append(f"Se requieren exactamente 6 condiciones base; actualmente hay {len(conditions)}")
    keys = [item.key for item in conditions]
    duplicate_keys = [key for key, count in Counter(keys).items() if count > 1]
    if duplicate_keys:
        errors.append("Hay claves de condición duplicadas: " + ", ".join(duplicate_keys))
    order_indexes = [item.order_index for item in conditions]
    if len(set(order_indexes)) != len(order_indexes):
        errors.append("Hay posiciones de condición duplicadas")
    for condition in conditions:
        errors.extend(
            f"{condition.key}: {message}"
            for message in validate_condition_dict(
                {
                    "variable": condition.variable,
                    "operator": condition.operator,
                    "threshold_min": condition.threshold_min,
                    "threshold_max": condition.threshold_max,
                    "threshold_value": condition.threshold_value,
                    "unit": condition.unit,
                    "soil_alternative": condition.soil_alternative,
                }
            )
        )
    for disease in diseases:
        if disease.enabled_for_evaluation and disease.validation_status != "PUBLISHED":
            errors.append(f"{disease.name}: está habilitada sin validación PUBLISHED")
        condition_group = disease.conditions if isinstance(disease.conditions, dict) else {}
        measurable = condition_group.get("all") if isinstance(condition_group.get("all"), list) else []
        if disease.validation_status == "PUBLISHED" and not measurable:
            errors.append(f"{disease.name}: publicada sin condiciones medibles")
        risk = str(condition_group.get("risk") or "moderado").lower()
        if risk not in {"bajo", "moderado", "alto"}:
            errors.append(f"{disease.name}: nivel de riesgo no soportado")
        for index, condition in enumerate(measurable, start=1):
            if not isinstance(condition, dict):
                errors.append(f"{disease.name}: parámetro {index} no es un objeto válido")
                continue
            normalized_condition = {
                "variable": condition.get("variable"),
                "operator": condition.get("operator"),
                "threshold_min": condition.get("min", condition.get("threshold_min")),
                "threshold_max": condition.get("max", condition.get("threshold_max")),
                "threshold_value": condition.get("value", condition.get("threshold_value")),
                "unit": condition.get("unit"),
            }
            errors.extend(
                f"{disease.name}, parámetro {index}: {message}"
                for message in validate_condition_dict(normalized_condition)
            )
        unknown_stages = sorted(expand_stage_codes(disease.valid_stages or []) - expand_stage_codes(stage_codes))
        if unknown_stages:
            errors.append(
                f"{disease.name}: etapas fenológicas no configuradas para el cultivo: "
                + ", ".join(unknown_stages)
            )
        if disease.validation_status == "PENDING_TECHNICAL":
            warnings.append(f"{disease.name}: pendiente de validación técnica y excluida del cálculo")
    if rule.effective_from and rule.effective_to and rule.effective_to < rule.effective_from:
        errors.append("effective_to no puede ser anterior a effective_from")

    alert_config = normalize_alert_configuration(rule.configuration, len(conditions))
    allowed_colors = {"green", "yellow", "amber", "orange", "red", "gray", "blue"}
    if str(alert_config.get("missing_required_policy") or "").upper() not in {"DATOS_INSUFICIENTES", "CONTINUE"}:
        errors.append("La política de datos faltantes debe ser DATOS_INSUFICIENTES o CONTINUE")
    for code, item in alert_config["levels"].items():
        if not str(item.get("label") or "").strip():
            errors.append(f"{code}: requiere una etiqueta visible")
        if str(item.get("color") or "").lower() not in allowed_colors:
            errors.append(f"{code}: color no soportado")
    ranges: list[tuple[int, int, str]] = []
    for code in ("NINGUNA", "MODERADA", "GRAVE"):
        item = alert_config["levels"][code]
        start = int(item["min_conditions"])
        end = int(item["max_conditions"])
        if end < start:
            errors.append(f"{code}: máximo menor que mínimo")
        ranges.append((start, end, code))
    covered: dict[int, str] = {}
    for start, end, code in ranges:
        for value in range(start, end + 1):
            if value in covered:
                errors.append(f"Los niveles {covered[value]} y {code} se superponen en {value} condiciones")
            covered[value] = code
    missing_counts = [str(value) for value in range(0, len(conditions) + 1) if value not in covered]
    if missing_counts:
        errors.append("Los niveles de alerta no cubren estas cantidades de condiciones: " + ", ".join(missing_counts))
    minimum_completion = float(alert_config.get("minimum_completion_pct", 100))
    if minimum_completion < 0 or minimum_completion > 100:
        errors.append("La completitud mínima debe estar entre 0 y 100")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "conditions_count": len(conditions),
        "diseases_count": len(diseases),
        "published_diseases_count": sum(
            item.validation_status == "PUBLISHED" and item.enabled_for_evaluation for item in diseases
        ),
    }


async def _stage_for_day(
    db: AsyncSession, crop_id: UUID, planting_date: date | None, day: date
) -> str | None:
    if not planting_date:
        return None
    stages = list(
        (
            await db.scalars(
                select(PhenologyStage)
                .where(
                    PhenologyStage.crop_id == crop_id,
                    PhenologyStage.active.is_(True),
                    PhenologyStage.deleted_at.is_(None),
                    PhenologyStage.cycle_type == "normal",
                )
                .order_by(PhenologyStage.order_index)
            )
        ).all()
    )
    result = calculate_stage(
        planting_date, day,
        [
            {
                "code": item.code, "name": item.name, "description": item.description,
                "order_index": item.order_index, "start_day": item.start_day,
                "end_day": item.end_day,
            }
            for item in stages
        ],
    )
    return result.get("stage_code")


async def simulate_rule(
    db: AsyncSession,
    rule_id: UUID,
    station_id: UUID,
    date_from: date,
    date_to: date,
    planting_date: date | None = None,
) -> dict[str, Any]:
    rule = await db.get(CropRuleVersion, rule_id)
    if not rule:
        raise ValueError("Versión de regla no encontrada")
    station = await db.get(Station, station_id)
    if not station or station.deleted_at:
        raise ValueError("Estación no encontrada")
    from app.services.weather import effective_soil_moisture_unit
    measured_soil_unit = effective_soil_moisture_unit(station)
    validation = await validate_rule_version(db, rule_id)
    if validation["errors"]:
        raise ValueError("La versión no es simulable: " + "; ".join(validation["errors"]))
    conditions = list(
        (
            await db.scalars(
                select(BaseCondition)
                .where(BaseCondition.rule_version_id == rule.id, BaseCondition.active.is_(True))
                .order_by(BaseCondition.order_index)
            )
        ).all()
    )
    diseases = list(
        (
            await db.scalars(
                select(DiseaseRule).where(
                    DiseaseRule.rule_version_id == rule.id,
                    DiseaseRule.active.is_(True),
                )
            )
        ).all()
    )
    specs = [
        ConditionSpec(
            key=item.key,
            label=item.label,
            variable=item.variable,
            operator=item.operator,
            threshold_min=item.threshold_min,
            threshold_max=item.threshold_max,
            threshold_value=item.threshold_value,
            unit=item.unit,
            required=item.required,
            soil_alternative=item.soil_alternative,
        )
        for item in conditions
    ]
    disease_specs = [
        {
            "name": item.name,
            "conditions": item.conditions,
            "valid_stages": item.valid_stages,
            "validation_status": item.validation_status,
            "enabled_for_evaluation": item.enabled_for_evaluation,
            "risk": "moderado",
        }
        for item in diseases
    ]
    summaries = {
        item.summary_date: item
        for item in (
            await db.scalars(
                select(WeatherDailySummary).where(
                    WeatherDailySummary.station_id == station_id,
                    WeatherDailySummary.summary_date >= date_from,
                    WeatherDailySummary.summary_date <= date_to,
                )
            )
        ).all()
    }
    days: list[dict[str, Any]] = []
    current = date_from
    while current <= date_to:
        summary = summaries.get(current)
        values = {
            "temperature_avg_c": getattr(summary, "temperature_avg_c", None),
            "humidity_avg_pct": getattr(summary, "humidity_avg_pct", None),
            "rain_sum_mm": getattr(summary, "rain_sum_mm", None),
            "soil_temperature_avg_c": getattr(summary, "soil_temperature_avg_c", None),
            "soil_moisture_avg_pct": getattr(summary, "soil_moisture_avg_pct", None),
            "leaf_wetness_hours": getattr(summary, "leaf_wetness_hours", None),
            "solar_sum_wm2_samples": getattr(summary, "solar_sum_wm2_samples", None),
            "evapotranspiration_sum_mm": getattr(summary, "evapotranspiration_sum_mm", None),
        }
        stage = await _stage_for_day(db, rule.crop_id, planting_date, current)
        result = evaluate_daily_alert(
            soil_rule_values(values, measured_soil_unit),
            specs,
            stage_code=stage,
            disease_rules=disease_specs,
            data_completeness=(summary.completeness_pct / 100) if summary else 0,
            alert_configuration=rule.configuration,
            observed_units=soil_rule_units(measured_soil_unit),
        )
        days.append({"date": current.isoformat(), "stage_code": stage, **result})
        current += timedelta(days=1)
    level_counts = Counter(item["level"] for item in days)
    return {
        "rule_id": str(rule.id),
        "rule_version": rule.version,
        "station_id": str(station_id),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": dict(level_counts),
        "days": days,
    }


async def compare_rule_versions(
    db: AsyncSession,
    first_rule_id: UUID,
    second_rule_id: UUID,
    station_id: UUID,
    date_from: date,
    date_to: date,
    planting_date: date | None = None,
) -> dict[str, Any]:
    first = await simulate_rule(db, first_rule_id, station_id, date_from, date_to, planting_date)
    second = await simulate_rule(db, second_rule_id, station_id, date_from, date_to, planting_date)
    changes: list[dict[str, Any]] = []
    for a, b in zip(first["days"], second["days"], strict=True):
        if a["level"] != b["level"] or a["conditions_met"] != b["conditions_met"]:
            changes.append(
                {
                    "date": a["date"],
                    "first_level": a["level"],
                    "second_level": b["level"],
                    "first_conditions_met": a["conditions_met"],
                    "second_conditions_met": b["conditions_met"],
                }
            )
    return {
        "first": {"rule_id": first["rule_id"], "version": first["rule_version"], "summary": first["summary"]},
        "second": {"rule_id": second["rule_id"], "version": second["rule_version"], "summary": second["summary"]},
        "changed_days": changes,
        "changed_days_count": len(changes),
    }
