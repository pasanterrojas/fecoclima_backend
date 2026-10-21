from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _timeline(planting_date: date, stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stage in sorted(stages, key=lambda item: item["order_index"]):
        start_day = int(stage["start_day"])
        end_day = int(stage["end_day"])
        rows.append(
            {
                **stage,
                "estimated_start_date": (planting_date + timedelta(days=start_day)).isoformat(),
                "estimated_end_date": (planting_date + timedelta(days=end_day)).isoformat(),
            }
        )
    return rows


def calculate_stage(
    planting_date: date,
    on_date: date,
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Estimate phenology exclusively from crop calendar + planting date.

    The result is intentionally an estimate. The crop calendar comes from the
    technical source configured for the crop; no sowing window or crop cycle
    record is required.
    """

    days = (on_date - planting_date).days
    timeline = _timeline(planting_date, stages)

    if not timeline:
        return {
            "planting_date": planting_date.isoformat(),
            "analysis_date": on_date.isoformat(),
            "days_after_planting": days,
            "stage_code": None,
            "stage_name": "Configuración pendiente",
            "description": "Este cultivo todavía no tiene un calendario fenológico publicado.",
            "stage_start_day": None,
            "stage_end_day": None,
            "stage_progress_pct": None,
            "cycle_finished": False,
            "timeline": [],
        }

    if days < 0:
        return {
            "planting_date": planting_date.isoformat(),
            "analysis_date": on_date.isoformat(),
            "days_after_planting": days,
            "stage_code": "NO_INICIADO",
            "stage_name": "Siembra aún no iniciada",
            "description": "La fecha de análisis es anterior a la fecha de siembra indicada.",
            "stage_start_day": None,
            "stage_end_day": None,
            "stage_progress_pct": 0.0,
            "cycle_finished": False,
            "timeline": timeline,
        }

    matches = [stage for stage in timeline if int(stage["start_day"]) <= days <= int(stage["end_day"])]
    last = max(timeline, key=lambda stage: int(stage["end_day"]))
    if len(matches) > 1 or (not matches and days <= int(last["end_day"])):
        return {
            "planting_date": planting_date.isoformat(),
            "analysis_date": on_date.isoformat(),
            "days_after_planting": days,
            "stage_code": None,
            "stage_name": "Calendario por revisar",
            "description": (
                "Hay etapas superpuestas para este día; revise el calendario del cultivo."
                if len(matches) > 1 else
                f"No hay una etapa configurada para el día {days} desde la siembra."
            ),
            "stage_start_day": None,
            "stage_end_day": None,
            "stage_progress_pct": None,
            "cycle_finished": False,
            "timeline": timeline,
        }
    selected = matches[0] if matches else None
    cycle_finished = False
    if selected is None:
        selected = last
        cycle_finished = days > int(selected["end_day"])

    start_day = int(selected["start_day"])
    end_day = int(selected["end_day"])
    duration = max(1, end_day - start_day + 1)
    progress = max(0.0, min(100.0, ((days - start_day + 1) / duration) * 100))

    return {
        "planting_date": planting_date.isoformat(),
        "analysis_date": on_date.isoformat(),
        "days_after_planting": days,
        "stage_code": selected["code"],
        "stage_name": selected["name"],
        "description": selected.get("description"),
        "stage_start_day": start_day,
        "stage_end_day": end_day,
        "stage_progress_pct": round(progress, 1),
        "cycle_finished": cycle_finished,
        "timeline": timeline,
    }
