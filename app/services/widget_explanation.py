"""Short, on-demand, evidence-bound explanations for public dashboard widgets."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import AIAgentRun, AlertEvaluation, Station, WeatherDailySummary, WeatherReading
from app.services.agent_runtime import select_agent_version
from app.services.ai import generate_markdown_response, provider_fallback_reason
from app.services.analysis_context import build_complete_analysis_context
from app.services.year_comparison import build_year_comparison


METRIC_FIELDS = {
    "temperature": ("temperature_c", "Temperatura", "°C"),
    "feels_like": ("feels_like_c", "Sensación térmica", "°C"),
    "heat_index": ("heat_index_c", "Índice de calor", "°C"),
    "humidity": ("humidity_pct", "Humedad del aire", "%"),
    "dew_point": ("dew_point_c", "Punto de rocío", "°C"),
    "wind": ("wind_speed_ms", "Viento", "m/s"),
    "wind_gust": ("wind_gust_ms", "Ráfaga", "m/s"),
    "wind_direction": ("wind_direction_deg", "Dirección del viento", "°"),
    "rain": ("rain_mm", "Lluvia", "mm"),
    "total_rain": ("total_rain_mm", "Lluvia acumulada", "mm"),
    "pressure": ("pressure_hpa", "Presión", "hPa"),
    "solar": ("solar_wm2", "Luz solar", "W/m²"),
    "et": ("evapotranspiration_mm", "Pérdida de agua", "mm"),
    "uv": ("uv_index", "Índice UV", ""),
    "soil_temperature": ("soil_temperature_c", "Temperatura del suelo", "°C"),
    "soil_moisture": ("soil_moisture_pct", "Humedad del suelo", ""),
    "leaf_sensor": ("leaf_sensor_value", "Sensor de hoja", ""),
}

WIDGETS = {
    "alert": "Nivel de atención",
    "phenology": "Etapa del cultivo",
    "quality": "Calidad de los datos",
    "metric": "Lectura meteorológica",
    "disease": "Condiciones para enfermedades",
    "forecast": "Pronóstico",
    "forecast_risk": "Riesgo según el pronóstico",
    "forecast_disease": "Enfermedad y pronóstico",
    "chart": "Cambios en el tiempo",
    "year_comparison": "Comparación con el año pasado",
    "satellite": "Imagen satelital",
}


def _widget_evidence(context: dict[str, Any], widget: str, item_key: str | None) -> dict[str, Any]:
    """Only use server-owned evidence; never accept observations from the browser."""
    if widget not in WIDGETS:
        raise ValueError("Widget no disponible")
    query = context["query"]
    alert = context["deterministic_result"]
    evidence: dict[str, Any] = {
        "consulta": query,
        "ultima_lectura": context["data_freshness"],
        "nivel_de_atencion": alert.get("level"),
        "widget": WIDGETS[widget],
    }
    if widget == "metric":
        if item_key not in METRIC_FIELDS:
            raise ValueError("Lectura no disponible")
        field, label, unit = METRIC_FIELDS[item_key]
        if item_key == "soil_moisture":
            unit = context.get("sensor_units", {}).get(field, "unidad no confirmada")
            label = "Tensión del agua en el suelo" if unit == "cb" else "Sensor de suelo" if unit != "%" else label
        latest = context.get("latest_metric_reading") or {}
        evidence["lectura"] = {"nombre": label, "valor": latest.get(field), "unidad": unit, "fecha": latest.get("sampled_at")}
    elif widget == "alert":
        evidence["alerta"] = {key: alert.get(key) for key in ("simple_explanation", "conditions", "diseases")}
    elif widget == "phenology":
        evidence["etapa_calculada"] = context.get("phenology") or {}
    elif widget == "quality":
        evidence["calidad"] = context.get("quality_evidence") or {}
        evidence["lecturas_en_el_periodo"] = (context.get("observed_series_summary") or {}).get("samples", 0)
        evidence["condiciones_sin_datos"] = [
            item.get("label") for item in alert.get("conditions", []) if item.get("status") == "missing"
        ]
        evidence["frecuencia_esperada_minutos"] = context["data_freshness"].get("expected_interval_minutes")
    elif widget in {"disease", "forecast_disease"}:
        rows = alert.get("diseases", []) if widget == "disease" else (context.get("forecast_risk") or {}).get("diseases", [])
        row = next((item for item in rows if item.get("name") == item_key), None)
        if not row:
            raise ValueError("Enfermedad no encontrada en esta consulta")
        evidence["condiciones_para_vigilar"] = row
    elif widget == "forecast":
        row = next((item for item in context.get("forecast", []) if item.get("date") == item_key), None)
        if not row:
            raise ValueError("Día no encontrado en el pronóstico")
        evidence["pronostico_del_dia"] = row
    elif widget == "forecast_risk":
        row = next((item for item in (context.get("forecast_risk") or {}).get("days", []) if item.get("date") == item_key), None)
        if not row:
            raise ValueError("Día no encontrado en el riesgo pronosticado")
        evidence["riesgo_previsto"] = row
    elif widget == "chart":
        if item_key not in {"weather", "rain_soil"}:
            raise ValueError("Gráfico no disponible")
        summary = context.get("observed_series_summary") or {}
        variables = summary.get("variables") or {}
        selected = ("temperature_c", "humidity_pct") if item_key == "weather" else ("rain_mm", "soil_moisture_pct")
        evidence["resumen_de_lecturas"] = {
            "muestras": summary.get("samples", 0),
            "desde": summary.get("from"), "hasta": summary.get("to"),
            "variables": {key: variables.get(key) for key in selected},
            "unidades_sensor": context.get("sensor_units", {}),
        }
    elif widget == "year_comparison":
        comparison = context.get("year_comparison") or {}
        evidence["comparacion"] = {
            "cobertura_actual": (comparison.get("current") or {}).get("coverage_pct"),
            "cobertura_pasada": (comparison.get("previous_year") or {}).get("coverage_pct"),
            "comparabilidad": comparison.get("comparability"),
            "diferencias": comparison.get("deltas"),
        }
    elif widget == "satellite":
        satellite = context.get("satellite") or {}
        evidence["satelite"] = {key: satellite.get(key) for key in ("captured_at", "data_age_days", "data_warning", "analysis", "ndvi", "ndmi", "ndwi", "indices", "index_legends")}
    return evidence


def _data_based_explanation(evidence: dict[str, Any], reason: str) -> str:
    label = evidence["widget"]
    latest = evidence["ultima_lectura"]
    if "lectura" in evidence:
        row = evidence["lectura"]
        value = row["valor"]
        detail = f"La última lectura registró **{value} {row['unidad']}**." if value is not None else "Todavía no hay una lectura para este dato."
    elif "condiciones_para_vigilar" in evidence:
        row = evidence["condiciones_para_vigilar"]
        detail = str(row.get("reason") or "Las condiciones invitan a revisar el cultivo en el lote.")
    elif "pronostico_del_dia" in evidence:
        row = evidence["pronostico_del_dia"]
        detail = f"Para el **{row['date']}** se estiman {row.get('precipitation_mm') if row.get('precipitation_mm') is not None else '—'} mm de lluvia. Es un pronóstico, no una medición."
    elif "riesgo_previsto" in evidence:
        row = evidence["riesgo_previsto"]
        detail = f"Para el **{row['date']}**, el nivel potencial es **{row.get('potential_label') or row.get('potential_level') or 'sin datos'}**. Aún debe comprobarse en campo."
    elif "alerta" in evidence:
        detail = evidence["alerta"].get("simple_explanation") or "Revise el lote y confirme su estado en campo."
    elif "etapa_calculada" in evidence:
        detail = f"Según la fecha de siembra, la etapa estimada es **{evidence['etapa_calculada'].get('name') or evidence['etapa_calculada'].get('stage_name') or 'sin calendario'}**. Confírmela observando el cultivo."
    elif "calidad" in label.lower():
        quality = evidence.get("calidad") or {}
        coverage = quality.get("porcentaje_de_lecturas")
        value = f"Se recibió el **{coverage}% de las lecturas esperadas** para la estación" if coverage is not None else f"Hay **{evidence['lecturas_en_el_periodo']} lecturas** en este periodo"
        detail = f"{value}. Si faltan mediciones, no conviene sacar conclusiones firmes."
    elif "comparacion" in evidence:
        comparison = evidence["comparacion"]
        detail = "Solo conviene comparar con el año pasado cuando hay suficientes lecturas en ambos periodos." if (comparison.get("comparabilidad") or {}).get("status") != "comparable" else "Se pueden comparar ambos periodos; las diferencias orientan, pero no prueban la causa de un cambio."
    elif "satelite" in evidence:
        detail = (evidence["satelite"].get("analysis") or {}).get("summary") or "Todavía no hay imagen o índices suficientes para explicar esta zona."
    elif "resumen_de_lecturas" in evidence:
        detail = f"Hay **{evidence['resumen_de_lecturas']['muestras']} lecturas** en el periodo elegido. Compare la tendencia con lo que observa en el lote; si faltan datos, no complete los huecos por su cuenta."
    else:
        detail = "Revise la tendencia de las lecturas y contrástela con lo que observa en su campo."
    stale = " La última lectura de la estación está atrasada." if latest.get("stale") else ""
    return f"**{label}:** {detail}{stale}\n\n_Explicación basada en datos; la IA no estuvo disponible para esta consulta._"


async def explain_widget(db: AsyncSession, alert_id: UUID, period: str, widget: str, item_key: str | None) -> dict[str, Any]:
    alert = await db.get(AlertEvaluation, alert_id)
    if not alert:
        raise ValueError("Análisis no encontrado")
    context = await build_complete_analysis_context(db, alert_id, period=period)
    if widget == "metric":
        latest = await db.scalar(
            select(WeatherReading).where(WeatherReading.station_id == alert.station_id).order_by(WeatherReading.sampled_at.desc()).limit(1)
        )
        context["latest_metric_reading"] = {
            **{field: getattr(latest, field) if latest else None for field, _, _ in METRIC_FIELDS.values()},
            "sampled_at": latest.sampled_at.isoformat() if latest else None,
        }
    if widget == "year_comparison":
        station = await db.get(Station, alert.station_id)
        context["year_comparison"] = await build_year_comparison(db, station, period=period, refresh_historical_if_missing=False)
    if widget == "quality":
        summary = await db.scalar(
            select(WeatherDailySummary).where(WeatherDailySummary.station_id == alert.station_id).order_by(WeatherDailySummary.summary_date.desc()).limit(1)
        )
        context["quality_evidence"] = {
            "porcentaje_de_lecturas": round(summary.completeness_pct, 1) if summary else None,
            "lecturas_fuera_de_rango": summary.outlier_count if summary else None,
        }
    evidence = _widget_evidence(context, widget, item_key)
    agent, version = await select_agent_version(db, alert.crop_id, "CROP_ANALYSIS")
    started = datetime.now(UTC)
    run = AIAgentRun(
        agent_id=agent.id if agent else None,
        agent_version_id=version.id if version else None,
        alert_evaluation_id=alert_id,
        status="RUNNING" if version else "FALLBACK",
        model=(version.model or settings.ai_default_model) if version else None,
        input_payload={"widget": widget, "item_key": item_key, "evidence": evidence},
        started_at=started,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    reason: str | None = None
    markdown = ""
    try:
        if not version:
            reason = "agent_not_configured"
        else:
            result = await generate_markdown_response(
                version.system_prompt + "\nResponde con calidez, sin jerga, solo a partir de la evidencia suministrada. No cambies el nivel de alerta; distingue pronóstico de medición, no diagnostiques ni inventes datos.",
                {"widget_evidence": evidence, "agent_purpose": "WIDGET_EXPLANATION"},
                "Explícame este dato como si acompañaras al productor en el lote. En 2 a 4 frases (máximo 90 palabras), di qué pasa, qué puede revisar y una limitación si aplica. Sin introducciones ni tablas.",
                version.model,
                temperature=version.temperature,
                max_tokens=550,
                provider=getattr(version, "provider", "OPENAI"),
                response_policy={"max_words": 110, "max_sections": 2},
            )
            reason = "provider_not_configured" if result.get("fallback") else None
            if not reason:
                markdown = result["markdown"]
    except Exception as exc:
        reason = provider_fallback_reason(exc)
        run.error_message = f"{reason} · HTTP {exc.status_code}" if hasattr(exc, "status_code") else reason
    if reason:
        markdown = _data_based_explanation(evidence, reason)
    run.status = "FALLBACK" if reason else "COMPLETED"
    run.output_payload = {"markdown": markdown, "fallback_reason": reason}
    run.finished_at = datetime.now(UTC)
    run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
    await db.commit()
    return {"markdown": markdown, "_agent_status": run.status.lower(), "_fallback_reason": reason, "_run_id": str(run.id)}
