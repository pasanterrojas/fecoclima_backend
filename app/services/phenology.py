from __future__ import annotations

from datetime import date
from typing import Any


def calculate_stage(planting_date: date, on_date: date, stages: list[dict[str, Any]]) -> dict[str, Any]:
    days = max(0, (on_date - planting_date).days)
    ordered = sorted(stages, key=lambda item: item["order_index"])
    if not ordered:
        return {"days_after_planting": days, "stage_code": None, "stage_name": "Configuración pendiente", "description": "No hay un calendario fenológico publicado.", "timeline": []}
    current = ordered[-1]
    for stage in ordered:
        if stage["start_day"] <= days <= stage["end_day"]:
            current = stage
            break
    return {
        "days_after_planting": days,
        "stage_code": current["code"],
        "stage_name": current["name"],
        "description": current.get("description") or f"Etapa estimada por días transcurridos desde la siembra.",
        "timeline": [
            {"code": stage["code"], "name": stage["name"], "start_day": stage["start_day"], "end_day": stage["end_day"]}
            for stage in ordered
        ],
    }
