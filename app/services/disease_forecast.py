from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BaseCondition, CropRuleVersion, DiseaseRule, PhenologyStage, WeatherForecast
from app.services.alert_engine import ConditionSpec, classify_alert_level, evaluate_conditions
from app.services.disease_projection import evaluate_disease_projection
from app.services.phenology import calculate_stage

_RISK_ORDER = {"bajo": 0, "moderado": 1, "alto": 2}


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(parsed) else parsed



def _frame_from_forecasts(forecasts: list[WeatherForecast]) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "date": item.forecast_date,
                "temperature_min_c": item.temperature_min_c,
                "temperature_max_c": item.temperature_max_c,
                "humidity_avg_pct": item.humidity_pct,
                "rain_sum_mm": item.precipitation_mm,
                "soil_moisture_avg_pct": item.estimated_soil_moisture_pct,
                "soil_temperature_avg_c": (item.raw_payload or {}).get("soil_temperature_avg_c"),
                "leaf_wetness_hours": (item.raw_payload or {}).get("leaf_wetness_hours"),
                "leaf_wetness_method": (item.raw_payload or {}).get("leaf_wetness_method"),
                "provider": item.provider,
                "generated_at": item.generated_at,
            }
            for item in forecasts
        ]
    )
    if frame.empty:
        return frame
    if "temperature_avg_c" not in frame or frame["temperature_avg_c"].isna().all():
        frame["temperature_avg_c"] = frame[["temperature_min_c", "temperature_max_c"]].mean(axis=1, skipna=True)
    return frame


async def build_forecast_risk(
    db: AsyncSession,
    crop_id,
    planting_date: date,
    forecasts: list[WeatherForecast],
) -> dict[str, Any]:
    """Project disease-favourable weather without changing official alerts.

    Open-Meteo supplies forecast air and soil variables. Leaf wetness is an
    explicit proxy estimated from hourly precipitation or relative humidity.
    The response exposes a *potential* level and coverage, never an official
    observed alert.
    """

    rule = await db.scalar(
        select(CropRuleVersion)
        .where(CropRuleVersion.crop_id == crop_id, CropRuleVersion.status == "PUBLISHED")
        .order_by(CropRuleVersion.effective_from.desc().nullslast(), CropRuleVersion.created_at.desc())
        .limit(1)
    )
    if not rule or not forecasts:
        return {
            "days": [],
            "diseases": [],
            "unresolved_diseases": [],
            "methodology": "Sin pronóstico o sin reglas publicadas para el cultivo.",
        }

    condition_rows = list(
        (
            await db.scalars(
                select(BaseCondition)
                .where(BaseCondition.rule_version_id == rule.id, BaseCondition.active.is_(True))
                .order_by(BaseCondition.order_index)
            )
        ).all()
    )
    disease_rows = list(
        (
            await db.scalars(
                select(DiseaseRule).where(
                    DiseaseRule.rule_version_id == rule.id,
                    DiseaseRule.active.is_(True),
                )
            )
        ).all()
    )
    stages = list(
        (
            await db.scalars(
                select(PhenologyStage)
                .where(PhenologyStage.crop_id == crop_id, PhenologyStage.active.is_(True))
                .order_by(PhenologyStage.order_index)
            )
        ).all()
    )
    stage_payload = [
        {
            "code": item.code,
            "name": item.name,
            "description": item.description,
            "order_index": item.order_index,
            "start_day": item.start_day,
            "end_day": item.end_day,
        }
        for item in stages
    ]
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
        )
        for item in condition_rows
    ]
    diseases = [
        {
            "name": item.name,
            "conditions": item.conditions,
            "valid_stages": item.valid_stages,
            "validation_status": item.validation_status,
            "enabled_for_evaluation": item.enabled_for_evaluation,
            "source_reference": item.source_reference,
            "risk": (item.conditions or {}).get("risk", "moderado"),
        }
        for item in disease_rows
    ]

    frame = _frame_from_forecasts(forecasts)
    daily: list[dict[str, Any]] = []
    aggregate: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"risk": "bajo", "dates": [], "reasons": [], "source": None}
    )
    unresolved_summary: dict[str, dict[str, Any]] = {}

    for row in frame.to_dict(orient="records"):
        forecast_date = row["date"]
        values = {
            "temperature_avg_c": _number(row.get("temperature_avg_c")),
            "humidity_avg_pct": _number(row.get("humidity_avg_pct")),
            "rain_sum_mm": _number(row.get("rain_sum_mm")),
            "soil_temperature_avg_c": _number(row.get("soil_temperature_avg_c")),
            "soil_moisture_avg_pct": _number(row.get("soil_moisture_avg_pct")),
            "leaf_wetness_hours": _number(row.get("leaf_wetness_hours")),
        }
        condition_results = evaluate_conditions(values, specs)
        met_count = sum(item.status == "met" for item in condition_results)
        available_count = sum(item.status != "missing" for item in condition_results)
        missing_count = len(condition_results) - available_count
        coverage = round(100 * available_count / max(1, len(condition_results)), 1)
        potential_classification = classify_alert_level(
            met_count,
            len(condition_results),
            coverage,
            rule.configuration,
            missing_required=False,
            enforce_completion=False,
        )
        potential_level = potential_classification["level"]
        phenology = calculate_stage(planting_date, forecast_date, stage_payload)
        projected_diseases, unresolved_diseases = evaluate_disease_projection(
            values,
            phenology.get("stage_code"),
            diseases,
            potential_level,
        )
        for unresolved in unresolved_diseases:
            current = unresolved_summary.setdefault(
                unresolved["name"],
                {
                    "name": unresolved["name"],
                    "missing_variables": set(),
                    "source": unresolved.get("source"),
                },
            )
            current["missing_variables"].update(unresolved["missing_variables"])
        for disease in projected_diseases:
            item = aggregate[disease["name"]]
            if _RISK_ORDER[disease["risk"]] > _RISK_ORDER[item["risk"]]:
                item["risk"] = disease["risk"]
            item["dates"].append(forecast_date.isoformat())
            if disease["reason"] and disease["reason"] not in item["reasons"]:
                item["reasons"].append(disease["reason"])
            item["source"] = disease.get("source")
        daily.append(
            {
                "date": forecast_date.isoformat(),
                "stage_code": phenology.get("stage_code"),
                "stage_name": phenology.get("stage_name"),
                "potential_level": potential_level,
                "potential_color": potential_classification["color"],
                "potential_label": potential_classification["label"],
                "official_alert": False,
                "conditions_met": met_count,
                "conditions_total": len(condition_results),
                "available_conditions": available_count,
                "missing_conditions": missing_count,
                "coverage_pct": coverage,
                "confidence": round((coverage / 100) * 0.75, 3),
                "conditions": [item.__dict__ for item in condition_results],
                "diseases": projected_diseases,
                "unresolved_diseases": unresolved_diseases,
                "provider": row.get("provider"),
                "values": values,
                "estimated_variables": ["soil_temperature_avg_c", "soil_moisture_avg_pct", "leaf_wetness_hours"],
                "leaf_wetness_method": row.get("leaf_wetness_method"),
                "explanation": (
                    f"Proyección potencial {potential_level.lower()} con {met_count} condiciones favorables. "
                    f"Cobertura de variables: {coverage:.0f}%."
                    + (
                        " No constituye una alerta oficial porque faltan variables pronosticadas."
                        if missing_count
                        else " Las seis variables estuvieron disponibles para esta proyección."
                    )
                ),
            }
        )

    disease_summary = [
        {
            "name": name,
            "risk": data["risk"],
            "dates": sorted(set(data["dates"])),
            "reason": " | ".join(data["reasons"][:3]),
            "source": data["source"],
            "classification": "PROYECCION_NO_DIAGNOSTICA",
        }
        for name, data in aggregate.items()
    ]
    disease_summary.sort(key=lambda item: (-_RISK_ORDER[item["risk"]], item["name"]))
    unresolved = [
        {
            "name": data["name"],
            "missing_variables": sorted(data["missing_variables"]),
            "source": data["source"],
        }
        for data in unresolved_summary.values()
    ]
    unresolved.sort(key=lambda item: item["name"])
    return {
        "days": daily,
        "diseases": disease_summary,
        "unresolved_diseases": unresolved,
        "rule_version": rule.version,
        "methodology": (
            "Pandas consolida el pronóstico diario y NumPy calcula promedios y valores faltantes. "
            "La clasificación potencial usa las mismas seis condiciones publicadas. Una enfermedad "
            "solo se proyecta cuando todas las variables de su regla medible están disponibles y se cumplen; "
            "las reglas incompletas se informan como limitaciones y nunca como diagnóstico."
        ),
    }
