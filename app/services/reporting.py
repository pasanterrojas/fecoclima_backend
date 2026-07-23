from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    AlertEvaluation,
    Crop,
    CropCycle,
    Report,
    ReportFile,
    SatelliteObservation,
    Station,
    WeatherDailySummary,
    WeatherForecast,
)
from app.services.agent_runtime import run_agent_for_alert
from app.services.alert_engine import LEVEL_ORDER

PRIMARY = colors.HexColor("#004481")
SECONDARY = colors.HexColor("#3B8DC2")
LIGHT = colors.HexColor("#EAF3F8")
DARK_TEXT = colors.HexColor("#15334A")


def _plot_daily(rows: list[WeatherDailySummary]) -> bytes | None:
    if not rows:
        return None
    dates = [row.summary_date for row in rows]
    temperatures = [row.temperature_avg_c for row in rows]
    humidity = [row.humidity_avg_pct for row in rows]
    rain = [row.rain_sum_mm for row in rows]
    fig = plt.figure(figsize=(9, 4.6))
    ax = fig.add_subplot(111)
    ax.plot(dates, temperatures, marker="o", linewidth=1.5, label="Temperatura °C")
    ax.set_ylabel("Temperatura °C")
    ax.tick_params(axis="x", rotation=35)
    ax2 = ax.twinx()
    ax2.plot(dates, humidity, marker=".", linewidth=1.2, label="Humedad %")
    ax2.bar(dates, [value or 0 for value in rain], alpha=0.18, label="Lluvia mm")
    ax2.set_ylabel("Humedad % / lluvia mm")
    lines, labels = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labels + labels2, loc="upper right")
    ax.grid(alpha=0.22)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150)
    plt.close(fig)
    return buffer.getvalue()


def _plot_alerts(rows: list[AlertEvaluation]) -> bytes | None:
    if not rows:
        return None
    values = {"DATOS_INSUFICIENTES": -1, "NINGUNA": 0, "MODERADA": 1, "GRAVE": 2}
    fig = plt.figure(figsize=(9, 3.2))
    ax = fig.add_subplot(111)
    ax.step([row.evaluation_date for row in rows], [values.get(row.level, -1) for row in rows], where="mid")
    ax.scatter([row.evaluation_date for row in rows], [values.get(row.level, -1) for row in rows])
    ax.set_yticks([-1, 0, 1, 2], ["Sin datos", "Ninguna", "Moderada", "Grave"])
    ax.tick_params(axis="x", rotation=35)
    ax.grid(alpha=0.22)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150)
    plt.close(fig)
    return buffer.getvalue()


def _page_header(canvas, document):
    canvas.saveState()
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, A4[1] - 1.45 * cm, A4[0], 1.45 * cm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(1.5 * cm, A4[1] - 0.9 * cm, "FECOCLIMA IA")
    canvas.setFillColor(DARK_TEXT)
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(A4[0] - 1.5 * cm, 0.8 * cm, f"Página {document.page}")
    canvas.restoreState()


def _paragraph_list(items: list[str], style) -> list[Paragraph]:
    return [Paragraph(f"• {escape(str(item))}", style) for item in items if item]


async def generate_report_pdf(db: AsyncSession, report_id) -> ReportFile:
    report = await db.get(Report, report_id)
    if not report:
        raise ValueError("Reporte no encontrado")
    report.status = "PROCESSING"
    report.error_message = None
    await db.commit()
    station = await db.get(Station, report.station_id)
    crop = await db.get(Crop, report.crop_id)
    cycle = await db.get(CropCycle, report.crop_cycle_id) if report.crop_cycle_id else None
    daily = list(
        (
            await db.scalars(
                select(WeatherDailySummary)
                .where(
                    WeatherDailySummary.station_id == report.station_id,
                    WeatherDailySummary.summary_date >= report.date_from,
                    WeatherDailySummary.summary_date <= report.date_to,
                )
                .order_by(WeatherDailySummary.summary_date)
            )
        ).all()
    )
    alerts = list(
        (
            await db.scalars(
                select(AlertEvaluation)
                .where(
                    AlertEvaluation.station_id == report.station_id,
                    AlertEvaluation.crop_id == report.crop_id,
                    AlertEvaluation.crop_cycle_id == report.crop_cycle_id
                    if report.crop_cycle_id
                    else AlertEvaluation.crop_cycle_id.is_(None),
                    AlertEvaluation.evaluation_date >= report.date_from,
                    AlertEvaluation.evaluation_date <= report.date_to,
                )
                .order_by(AlertEvaluation.evaluation_date)
            )
        ).all()
    )
    forecasts = list(
        (
            await db.scalars(
                select(WeatherForecast)
                .where(
                    WeatherForecast.station_id == report.station_id,
                    WeatherForecast.forecast_date >= report.date_to,
                )
                .order_by(WeatherForecast.forecast_date)
                .limit(7)
            )
        ).all()
    )
    satellite = None
    if cycle and cycle.plot_id:
        satellite = await db.scalar(
            select(SatelliteObservation)
            .where(SatelliteObservation.plot_id == cycle.plot_id)
            .order_by(SatelliteObservation.captured_at.desc())
            .limit(1)
        )
    alert_by_date = {item.evaluation_date: item for item in alerts}
    worst_alert = max(alerts, key=lambda item: LEVEL_ORDER.get(item.level, -1), default=None)
    recommendation = None
    if worst_alert:
        recommendation = await run_agent_for_alert(
            db,
            worst_alert.id,
            forecast=[
                {
                    "date": row.forecast_date.isoformat(),
                    "temperature_min_c": row.temperature_min_c,
                    "temperature_max_c": row.temperature_max_c,
                    "precipitation_mm": row.precipitation_mm,
                    "humidity_pct": row.humidity_pct,
                    "provider": row.provider,
                }
                for row in forecasts
            ],
            satellite=(
                {
                    "captured_at": satellite.captured_at.isoformat(),
                    "provider": satellite.provider,
                    "cloud_percentage": satellite.cloud_percentage,
                    "ndvi": satellite.ndvi,
                    "ndmi": satellite.ndmi,
                    "lai": satellite.lai,
                }
                if satellite
                else None
            ),
        )
    output = settings.storage_path / "reports" / f"fecoclima-{report.id}.pdf"
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="FCTitle", parent=styles["Title"], textColor=PRIMARY, fontSize=24, leading=28, alignment=TA_CENTER, spaceAfter=16))
    styles.add(ParagraphStyle(name="FCHeading", parent=styles["Heading2"], textColor=PRIMARY, fontSize=15, leading=19, spaceBefore=12, spaceAfter=10))
    styles.add(ParagraphStyle(name="FCBody", parent=styles["BodyText"], fontSize=9.5, leading=14, textColor=DARK_TEXT))
    styles.add(ParagraphStyle(name="FCSmall", parent=styles["BodyText"], fontSize=8, leading=11, textColor=DARK_TEXT))
    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=2 * cm,
        bottomMargin=1.5 * cm,
        title=report.title,
        author="FECOCLIMA IA",
    )
    story = [
        Spacer(1, 2.2 * cm),
        Paragraph("FECOCLIMA IA", styles["FCTitle"]),
        Paragraph("Reporte agrometeorológico, fenológico y de alertas", ParagraphStyle(name="Sub", parent=styles["Heading2"], alignment=TA_CENTER, textColor=SECONDARY)),
        Spacer(1, 0.7 * cm),
    ]
    overview = [
        ["Estación", station.name],
        ["Ubicación", ", ".join(filter(None, [station.locality, station.district, station.department])) or "No registrada"],
        ["Cultivo", crop.name],
        ["Ciclo", cycle.name if cycle else "No seleccionado"],
        ["Fecha de siembra", cycle.planting_date.isoformat() if cycle else "No disponible"],
        ["Periodo", f"{report.date_from.isoformat()} a {report.date_to.isoformat()}"],
        ["Generado", datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")],
        ["Versión de regla", report.rule_version or "No disponible"],
    ]
    table = Table(overview, colWidths=[4.3 * cm, 11 * cm])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), LIGHT), ("TEXTCOLOR", (0, 0), (0, -1), PRIMARY), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C8D8E2")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 8)]))
    story += [table, Spacer(1, 0.7 * cm), Paragraph("Advertencia", styles["FCHeading"]), Paragraph("Este documento apoya el monitoreo. Cualquier aplicación fitosanitaria debe validarse con un profesional y cumplir el registro y la etiqueta vigente en Paraguay.", styles["FCBody"]), PageBreak()]
    total_days = (report.date_to - report.date_from).days + 1
    days_with_data = len([row for row in daily if row.records_count > 0])
    severe = len([item for item in alerts if item.level == "GRAVE"])
    moderate = len([item for item in alerts if item.level == "MODERADA"])
    insufficient = len([item for item in alerts if item.level == "DATOS_INSUFICIENTES"])
    story += [Paragraph("Resumen ejecutivo", styles["FCHeading"]), Paragraph(f"Se analizaron {total_days} días. Hubo datos meteorológicos en {days_with_data} días, {severe} evaluaciones graves, {moderate} moderadas y {insufficient} con información insuficiente. El color y el número de condiciones provienen exclusivamente del motor determinístico.", styles["FCBody"])]
    if worst_alert:
        story += [Spacer(1, 0.25 * cm), Paragraph(f"El nivel máximo observado fue <b>{escape(worst_alert.level)}</b> el {worst_alert.evaluation_date.isoformat()}, con {worst_alert.conditions_met} de {worst_alert.conditions_total} condiciones.", styles["FCBody"])]
    story += [Paragraph("Calidad y completitud", styles["FCHeading"])]
    if daily:
        avg_completion = sum(row.completeness_pct for row in daily) / len(daily)
        missing_soil = sum(row.soil_moisture_avg_pct is None for row in daily)
        missing_leaf = sum(row.leaf_wetness_hours is None for row in daily)
        story += [Paragraph(f"Completitud temporal promedio: {avg_completion:.1f}%. Días sin humedad de suelo: {missing_soil}. Días sin mojado foliar interpretable: {missing_leaf}. Los datos faltantes se conservan como nulos y no se sustituyen automáticamente.", styles["FCBody"])]
    else:
        story += [Paragraph("No existen agregados diarios para el periodo seleccionado.", styles["FCBody"])]
    graph = _plot_daily(daily)
    if graph:
        story += [Spacer(1, 0.3 * cm), Image(io.BytesIO(graph), width=17 * cm, height=8.7 * cm)]
    alert_graph = _plot_alerts(alerts)
    if alert_graph:
        story += [Spacer(1, 0.4 * cm), Paragraph("Evolución del nivel de alerta", styles["FCHeading"]), Image(io.BytesIO(alert_graph), width=17 * cm, height=6 * cm)]
    story += [PageBreak(), Paragraph("Tabla diaria de alertas", styles["FCHeading"])]
    rows = [["Fecha", "Temp. °C", "HR %", "Lluvia mm", "Completitud", "Condiciones", "Nivel", "Etapa", "Enfermedades"]]
    for summary in daily:
        alert = alert_by_date.get(summary.summary_date)
        rows.append([
            summary.summary_date.isoformat(),
            "—" if summary.temperature_avg_c is None else f"{summary.temperature_avg_c:.1f}",
            "—" if summary.humidity_avg_pct is None else f"{summary.humidity_avg_pct:.1f}",
            "—" if summary.rain_sum_mm is None else f"{summary.rain_sum_mm:.1f}",
            f"{summary.completeness_pct:.0f}%",
            "—" if not alert else f"{alert.conditions_met}/{alert.conditions_total}",
            "Sin evaluar" if not alert else alert.level,
            "—" if not alert or not alert.stage_code else alert.stage_code,
            "—" if not alert or not alert.diseases else ", ".join(item.get("name", "") for item in alert.diseases),
        ])
    daily_table = Table(rows, repeatRows=1, colWidths=[1.65 * cm, 1.2 * cm, 1.1 * cm, 1.25 * cm, 1.4 * cm, 1.4 * cm, 1.6 * cm, 1.25 * cm, 4.6 * cm])
    daily_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), PRIMARY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 6.6), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C8D8E2")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT])]))
    story += [daily_table]
    story += [PageBreak(), Paragraph("Pronóstico disponible", styles["FCHeading"])]
    if forecasts:
        forecast_rows = [["Fecha", "Mín. °C", "Máx. °C", "Lluvia mm", "Prob. lluvia", "HR %", "Viento m/s", "Fuente"]]
        for item in forecasts:
            forecast_rows.append([item.forecast_date.isoformat(), item.temperature_min_c, item.temperature_max_c, item.precipitation_mm, item.precipitation_probability_pct, item.humidity_pct, item.wind_speed_ms, item.provider])
        forecast_table = Table(forecast_rows, repeatRows=1, colWidths=[2.1 * cm, 1.6 * cm, 1.6 * cm, 1.7 * cm, 1.9 * cm, 1.4 * cm, 1.8 * cm, 2.6 * cm])
        forecast_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), SECONDARY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C8D8E2"))]))
        story += [forecast_table, Paragraph("Los datos pronosticados se muestran separados de las lecturas observadas.", styles["FCSmall"])]
    else:
        story += [Paragraph("No había pronóstico almacenado para la estación al generar el reporte.", styles["FCBody"])]
    story += [Paragraph("Información satelital", styles["FCHeading"])]
    if satellite:
        story += [Paragraph(f"Observación más reciente: {satellite.captured_at.isoformat()} · proveedor {escape(satellite.provider)} · nubosidad {satellite.cloud_percentage if satellite.cloud_percentage is not None else 'sin dato'}% · NDVI {satellite.ndvi if satellite.ndvi is not None else 'sin dato'} · NDMI {satellite.ndmi if satellite.ndmi is not None else 'sin dato'}.", styles["FCBody"]), Paragraph("La observación satelital describe el estado de la superficie; no es una predicción meteorológica.", styles["FCSmall"])]
    else:
        story += [Paragraph("No existe una observación satelital asociada al ciclo o parcela.", styles["FCBody"])]
    story += [Paragraph("Recomendación del agente", styles["FCHeading"])]
    if recommendation:
        story += [Paragraph(escape(recommendation.get("resumen", "")), styles["FCBody"]), Paragraph(escape(recommendation.get("explicacion_alerta", "")), styles["FCBody"])]
        story += [Paragraph("Acciones recomendadas", styles["FCHeading"]), *_paragraph_list(recommendation.get("acciones_recomendadas", []), styles["FCBody"])]
        story += [Paragraph("Variables a vigilar", styles["FCHeading"]), *_paragraph_list(recommendation.get("variables_a_vigilar", []), styles["FCBody"])]
        story += [Paragraph("Limitaciones", styles["FCHeading"]), *_paragraph_list(recommendation.get("limitaciones", []), styles["FCBody"])]
        sources = recommendation.get("fuentes", [])
        story += [Paragraph("Fuentes documentales", styles["FCHeading"])]
        if sources:
            story += [Paragraph(f"• {escape(str(source.get('documento', 'Documento')))} — {escape(str(source.get('pagina_o_seccion', 'sin sección')))}", styles["FCBody"]) for source in sources]
        else:
            story += [Paragraph("El agente no devolvió fuentes documentales para esta ejecución.", styles["FCBody"])]
        status = recommendation.get("_agent_status")
        if status == "fallback":
            story += [Paragraph("La integración de IA no estaba configurada; se utilizó una explicación de respaldo basada únicamente en el resultado determinístico.", styles["FCSmall"])]
    else:
        story += [Paragraph("La explicación técnica del agente no estuvo disponible. El análisis determinístico anterior permanece válido.", styles["FCBody"])]
    story += [PageBreak(), Paragraph("Metodología y limitaciones", styles["FCHeading"]), Paragraph("Las lecturas se normalizan, deduplican y agregan por día en la zona America/Asuncion. Se evalúan seis condiciones base. Con 0 a 2 condiciones el nivel es NINGUNA; con 3 a 5 es MODERADA; con 6 es GRAVE. Si falta una variable obligatoria, el resultado es DATOS_INSUFICIENTES salvo que exista una sustitución publicada y auditada.", styles["FCBody"]), Spacer(1, 0.3 * cm), Paragraph("Las reglas ambiguas de las fuentes, como ‘humedad elevada’ o ‘lluvias persistentes’, permanecen pendientes de validación técnica y no se convierten en umbrales inventados.", styles["FCBody"])]
    doc.build(story, onFirstPage=_page_header, onLaterPages=_page_header)
    content = output.read_bytes()
    file = ReportFile(report_id=report.id, filename=output.name, storage_path=str(output), size_bytes=len(content), checksum_sha256=hashlib.sha256(content).hexdigest())
    db.add(file)
    report.status = "COMPLETED"
    report.completed_at = datetime.now(UTC)
    report.summary = {"days": total_days, "days_with_data": days_with_data, "severe": severe, "moderate": moderate, "insufficient": insufficient, "agent_available": bool(recommendation), "forecast_days": len(forecasts), "satellite_available": bool(satellite)}
    await db.commit()
    return file
