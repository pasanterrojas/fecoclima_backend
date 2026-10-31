"""Stage demand compared with measured station water inputs; no invented sensors/yield loss."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import WeatherReading
from app.services.phenology import calculate_stage
from app.services.technical_references import REFERENCE_REVISION

# Explicit multipliers in 'Por Ciclo', G6:S22. Existing published calendar wins.
SOY_DEMAND = {
    "VE": (2, "G6:S6"),
    "V1": (2, "G7:S7"),
    "V2-V4": (3, "G8:S8"),
    "V5-V8": (3, "G9:S9"),
    "V9": (6, "G13:S13"),
    "R1": (6, "G14:S14"),
    "R3": (6, "G15:S15"),
    "R5": (6, "G18:S18"),
    "R7": (2, "G20:S20"),
    "R8": (1, "G22:S22"),
}
SOURCE = "Soja fenologia y Clima para agrobuch.xlsx"


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def stage_demand(crop_code: str, code: str | None) -> dict:
    code = (code or "").upper().replace(" ", "")
    if crop_code.upper() != "SOJA" or code not in SOY_DEMAND:
        return {"mm_per_day": None, "source": None}
    rate, cells = SOY_DEMAND[code]
    return {"mm_per_day": rate, "source": {"document": SOURCE, "page_or_section": "Por Ciclo!" + cells}}


def susceptibility(crop_code: str, code: str | None) -> dict:
    result = {
        "actual_loss_pct": None,
        "loss_probability_pct": None,
        "reference_pct": None,
        "kind": "documentary_susceptibility_not_observed_loss",
        "explanation": "No es posible cuantificar daño o pérdida de rendimiento solo con clima e índices; requiere evaluación de campo.",
    }
    if crop_code.upper() != "SOJA":
        return result
    code = (code or "").upper()
    if code in {"R5", "R7"}:
        pct, cells = 40, "O20:O23 (celda combinada); B29"
    elif code in {"R1", "R3"}:
        pct, cells = 20, "O16:O17; B28"
    elif code in {"VE", "V1"}:
        pct, cells = 10, "O9:O10 (celda combinada)"
    else:
        return result
    return {
        **result,
        "reference_pct": pct,
        "source": {"document": SOURCE, "page_or_section": "Control y seguimiento!" + cells},
        "explanation": f"La fuente menciona {pct}% como referencia de susceptibilidad bajo estrés en esta fase. No es daño observado, probabilidad ni estimación de pérdida de esta parcela.",
    }


def aggregate_water_day(readings: list, field: str, mode: str | None, *, expected: int) -> dict:
    values = [v for row in readings if (v := number(getattr(row, field, None))) is not None]
    coverage = min(100, len(values) * 100 / max(1, expected))
    # A repeated daily counter must never be summed as interval precipitation.
    if mode == "interval_sum":
        total = sum(values) if values else None
    elif mode == "daily_counter":
        total = max(values) if values else None
    else:
        total = None
    reliable = total is not None and coverage >= 80
    return {
        "observed_mm": round(total, 3) if total is not None else None,
        "complete_mm": round(total, 3) if reliable else None,
        "coverage_pct": round(coverage, 1),
        "aggregation": mode or "unconfirmed",
        "status": "AVAILABLE"
        if reliable
        else "PARTIAL"
        if total is not None
        else "AGGREGATION_UNCONFIRMED"
        if values
        else "NOT_REPORTED",
    }


def water_by_stage(days: list[dict], stages: list[dict], crop_code: str) -> list[dict]:
    """Compare the same completed days; incomplete stage rain stays a chart gap."""
    results = []
    for stage in stages:
        code = stage["code"]
        elapsed = [d for d in days if d["stage_code"] == code and d["closed_day"]]
        paired = [d for d in elapsed if d["rain_minus_requirement_mm"] is not None]
        required = (
            round(sum(d["required_mm"] for d in elapsed), 2)
            if elapsed and all(d["required_mm"] is not None for d in elapsed)
            else None
        )
        rain = round(sum(d["rain_mm"] for d in paired), 2) if paired else None
        compared = round(sum(d["required_mm"] for d in paired), 2) if paired else None
        results.append({
            "stage_code": code,
            "stage_name": stage.get("name", code),
            "required_stage_mm": (
                stage_demand(crop_code, code)["mm_per_day"] * (int(stage["end_day"]) - int(stage["start_day"]) + 1)
                if stage_demand(crop_code, code)["mm_per_day"] is not None else None
            ),
            "closed_days": len(elapsed),
            "compared_days": len(paired),
            "required_elapsed_mm": required,
            "required_compared_mm": compared,
            "rain_compared_mm": rain,
            "rain_complete_mm": rain if paired and len(paired) == len(elapsed) else None,
            "gap_compared_mm": round(rain - compared, 2) if paired else None,
            "reference_pct": susceptibility(crop_code, code)["reference_pct"],
        })
    return results


def calculate_water_balance(
    *,
    crop_code: str,
    planting_date: date,
    reference: datetime,
    stages: list[dict],
    stations: list[tuple[object, list]],
    timezone: str,
    current_phenology: dict,
) -> dict:
    tz = ZoneInfo(timezone)
    reference = reference.replace(tzinfo=UTC) if reference.tzinfo is None else reference.astimezone(UTC)
    today = reference.astimezone(tz).date()
    demand = stage_demand(crop_code, current_phenology.get("stage_code"))
    end = min(today, planting_date + timedelta(days=max((int(s["end_day"]) for s in stages), default=0)))
    days = []
    # One bounded calendar cycle. Never fabricate an unbounded planting history.
    for offset in range(max(0, min(731, (end - planting_date).days + 1))):
        day = planting_date + timedelta(days=offset)
        phase = calculate_stage(planting_date, day, stages)
        need = stage_demand(crop_code, phase.get("stage_code"))
        days.append(
            {
                "date": day.isoformat(),
                "stage_code": phase.get("stage_code"),
                "required_mm": need["mm_per_day"],
                "source": need["source"],
                "closed_day": day < today,
            }
        )
    results = []
    for station, readings in stations:
        station_tz = ZoneInfo(station.timezone or timezone)
        by_date = {}
        for row in {r.sampled_at: r for r in readings}.values():
            at = (
                row.sampled_at.replace(tzinfo=UTC)
                if row.sampled_at.tzinfo is None
                else row.sampled_at.astimezone(UTC)
            )
            if at <= reference:
                by_date.setdefault(at.astimezone(station_tz).date().isoformat(), []).append(row)
        modes = (station.integration_config or {}).get("water_aggregation") or {}
        daily = []
        for day in days:
            expected = max(1, round(1440 / max(1, station.expected_interval_minutes or 10)))
            rows = by_date.get(day["date"], [])
            rain = aggregate_water_day(rows, "rain_mm", modes.get("rain_mm"), expected=expected)
            et = aggregate_water_day(
                rows, "evapotranspiration_mm", modes.get("evapotranspiration_mm"), expected=expected
            )
            needed = day["required_mm"]
            # Current day remains incomplete: do not compare a full-day demand to partial-day rain.
            complete_rain = rain["complete_mm"] if day["closed_day"] else None
            complete_et = et["complete_mm"] if day["closed_day"] else None
            gap = (
                round(complete_rain - needed, 3) if complete_rain is not None and needed is not None else None
            )
            worksheet = round(gap - complete_et, 3) if gap is not None and complete_et is not None else None
            daily.append(
                {
                    **day,
                    "rain_mm": complete_rain,
                    "et_mm": complete_et,
                    "observed_rain_mm": rain["observed_mm"],
                    "observed_et_mm": et["observed_mm"],
                    "rain_status": rain["status"],
                    "et_status": et["status"],
                    "rain_coverage_pct": rain["coverage_pct"],
                    "et_coverage_pct": et["coverage_pct"],
                    "rain_minus_requirement_mm": gap,
                    "worksheet_index_mm": worksheet,
                }
            )
        paired = [d for d in daily if d["rain_minus_requirement_mm"] is not None]
        paired_need = sum(d["required_mm"] for d in paired)
        paired_rain = sum(d["rain_mm"] for d in paired)
        stage_code = current_phenology.get("stage_code")
        stage_days = [d for d in daily if d["stage_code"] == stage_code and d["closed_day"]]
        stage_paired = [d for d in stage_days if d["rain_minus_requirement_mm"] is not None]
        stage_gap = (
            round(sum(d["rain_minus_requirement_mm"] for d in stage_paired), 2) if stage_paired else None
        )
        results.append(
            {
                "station_id": str(station.id),
                "name": station.name,
                "days": daily,
                "compared_days": len(paired),
                "closed_days": sum(d["closed_day"] for d in daily),
                "required_compared_mm": round(paired_need, 2) if paired else None,
                "rain_compared_mm": round(paired_rain, 2) if paired else None,
                "rain_minus_requirement_mm": round(paired_rain - paired_need, 2) if paired else None,
                "supply_coverage_pct": round(paired_rain * 100 / paired_need, 1) if paired_need else None,
                "current_stage_gap_mm": stage_gap,
                "current_stage_compared_days": len(stage_paired),
                "current_stage_closed_days": len(stage_days),
                "water_aggregation": modes,
                "stage_totals": water_by_stage(daily, stages, crop_code),
                "status": "AVAILABLE" if paired else "REFERENCE_ONLY",
            }
        )
    return {
        "revision": REFERENCE_REVISION,
        "status": "AVAILABLE" if any(s["compared_days"] for s in results) else "REFERENCE_ONLY",
        "phenology_basis": "published_calendar_and_planting_date",
        "current_stage_code": current_phenology.get("stage_code"),
        "crop_code": crop_code.upper(),
        "required_mm_per_day": demand["mm_per_day"],
        "demand_source": demand["source"],
        "susceptibility": susceptibility(crop_code, current_phenology.get("stage_code")),
        "stations": results,
        "method": "stage_requirement_compared_with_measured_rain_on_same_complete_days",
        "limitations": [
            "Demanda orientativa de la etapa; no es una dosis de riego ni humedad del suelo.",
            "Se conserva el calendario publicado; las otras duraciones de las planillas no lo sustituyen.",
            "No se asume cero donde faltan lluvia o ET. Se comparan únicamente los mismos días completos con al menos 80% de muestras válidas.",
            "La agregación de lluvia y ET debe confirmarse por estación: suma por intervalo o contador diario.",
            "No incluye riego, escorrentía, drenaje, reserva inicial, capacidad del suelo ni lluvia efectiva.",
            "El índice de la planilla lluvia − requerimiento − ET es experimental; no mide reservas y puede duplicar consumo.",
            "No se determina pérdida real ni probabilidad de enfermedad con estas diferencias.",
        ],
    }


async def build_water_balance(
    db, *, crop_code, planting_date, reference, stages, stations, timezone, phenology
):
    end_day = max((int(s["end_day"]) for s in stages), default=0)
    since = datetime.combine(planting_date, time.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC) - timedelta(
        days=1
    )
    until = min(reference, since + timedelta(days=min(733, end_day + 3)))
    series = []
    for station in stations:
        rows = list(
            (
                await db.execute(
                    select(
                        WeatherReading.sampled_at,
                        WeatherReading.rain_mm,
                        WeatherReading.evapotranspiration_mm,
                    )
                    .where(
                        WeatherReading.station_id == station.id,
                        WeatherReading.sampled_at >= since,
                        WeatherReading.sampled_at <= until,
                    )
                    .order_by(WeatherReading.sampled_at)
                )
            ).all()
        )
        series.append((station, rows))
    return calculate_water_balance(
        crop_code=crop_code,
        planting_date=planting_date,
        reference=reference,
        stages=stages,
        stations=series,
        timezone=timezone,
        current_phenology=phenology,
    )


def water_summary(balance: dict) -> str:
    rate = balance.get("required_mm_per_day")
    lines = ["**Agua y etapa del cultivo**"]
    lines.append(
        f"La etapa estimada tiene una demanda orientativa de {rate:g} mm/día según la planilla del cliente."
        if rate is not None
        else "La fuente no define una demanda de agua para esta etapa o cultivo; se conserva el calendario publicado."
    )
    for station in balance.get("stations", []):
        gap = station.get("rain_minus_requirement_mm")
        if gap is not None:
            lines.append(
                f"{station['name']}: en {station['compared_days']} días completos comparables, "
                f"la lluvia registrada fue {station['rain_compared_mm']:g} mm frente a "
                f"{station['required_compared_mm']:g} mm de demanda orientativa. "
                f"Diferencia: {gap:g} mm. No equivale a reserva de suelo ni dosis de riego."
            )
        else:
            lines.append(
                f"{station['name']}: la demanda sirve como referencia, pero falta confirmar la agregación de lluvia o completar días medidos para contrastarla. Revise la humedad en campo."
            )
    lines.append(balance.get("susceptibility", {}).get("explanation", ""))
    lines.append(
        "**Qué revisar:** humedad en la zona de raíces, distribución del agua, riego aplicado y síntomas en plantas. Relacione estas observaciones con las señales de alerta y el pronóstico antes de decidir."
    )
    return "\n\n".join(filter(None, lines))


def stage_water_summary(balance: dict, *, context: dict | None = None) -> str:
    """Grounded explanation when the agronomic model is unavailable."""
    stage = balance.get("current_stage_code") or "actual"
    lines = [f"**Etapa actual: {stage}**", "La etapa es estimada y debe confirmarse en campo."]
    for station in balance.get("stations", []):
        current = next((s for s in station.get("stage_totals", []) if s["stage_code"] == stage), {})
        if current.get("gap_compared_mm") is not None:
            lines.append(
                f"En {current['compared_days']} días con registro de esta etapa, "
                f"la lluvia requerida es de {current['required_compared_mm']:g} mm y "
                f"la estación {station['name']} registró {current['rain_compared_mm']:g} mm. "
                + ("La lluvia quedó por debajo de lo requerido."
                   if current["gap_compared_mm"] < 0 else
                   "Esa lluvia cubre lo requerido en esos días, pero no garantiza humedad uniforme en las raíces.")
            )
        else:
            rate = balance.get("required_mm_per_day")
            lines.append(f"{station['name']}: aún no hay días de esta etapa con lluvia comparable. "
                         + (f"La lluvia requerida es de {rate:g} mm/día." if rate is not None else
                            "La fuente no define una cantidad de lluvia requerida para esta fase."))
    if context:
        window = (context.get("windows") or {}).get(str(context.get("selected_window_hours", 72))) or {}
        recent = {s.get("station_id") for s in window.get("stations", []) if s.get("stale") is False}
        dry_names = []
        for row in context.get("raw_readings") or []:
            tension = row.get("soil_tension") or {}
            measured = (row.get("reading") or {}).get("soil_moisture_pct")
            unit = (row.get("sensor_units") or {}).get("soil_moisture_pct")
            band = tension.get("band_index")
            if (row.get("station_id") in recent and unit == "cb" and measured is not None
                    and tension.get("value") == measured and isinstance(band, (int, float)) and band >= 3):
                dry_names.append(row.get("name") or "la estación")
        if dry_names:
            lines.append("La lectura reciente del suelo en " + ", ".join(dry_names)
                         + " indica sequedad; podría haber estrés localizado, aunque haya llovido. La lluvia de la estación no representa toda la parcela.")
        from app.services.parcel_guidance import satellite_plain_evidence
        lines.append(satellite_plain_evidence(context))
    reference = balance.get("susceptibility", {}).get("reference_pct")
    if reference is not None:
        lines.append(f"La documentación de esta fase menciona {reference}% de reducción potencial bajo estrés; no es una pérdida medida de esta parcela.")
    lines.append("**Acción:** revise humedad en las raíces, hojas nuevas y uniformidad del cultivo en bajos, lomas y sectores con rastrojo.")
    return "\n\n".join(lines)


def compact_water_context(balance: dict) -> dict:
    """Keep numerical conclusions and recent evidence without sending a whole season to every agent."""
    return {
        **balance,
        "stations": [
            {**{k: v for k, v in station.items() if k != "days"}, "recent_days": station.get("days", [])[-7:]}
            for station in balance.get("stations", [])
        ],
    }
