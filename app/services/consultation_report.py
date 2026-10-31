"""Presentation data for the client's consultation model, from one saved analysis."""

from __future__ import annotations

import base64
import re
from html import escape
from io import BytesIO
from math import isfinite
from pathlib import Path

from app.services.technical_references import reference_manifest, stage_monitoring
from app.services.water_balance import water_summary

METRICS = [
    ("temperature_avg_c", "Temperatura media", "°C"),
    ("humidity_avg_pct", "Humedad relativa media", "%"),
    ("soil_temperature_avg_c", "Temperatura media del suelo", "°C"),
    ("soil_moisture_avg_pct", "Sensor de suelo (media)", ""),
    ("leaf_wetness_hours", "Mojado foliar según sensor configurado", "h"),
    ("rain_sum_mm", "Lluvia (suma de muestras del período)", "mm"),
    ("evapotranspiration_sum_mm", "ET (suma de muestras del período)", "mm"),
]
STATUS = {"met": "Se cumple", "not_met": "No se cumple", "missing": "Sin medición compatible"}


def report_markdown_html(markdown: str) -> str:
    from app.services.producer_notifications import _markdown_to_html

    # The existing converter escapes user/model HTML before inline emphasis.
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", _markdown_to_html(markdown))


def water_chart_svg(balance: dict) -> str:
    stations = balance.get("stations") or []
    if not stations:
        return ""
    # Each station has its own chart; values are never pooled or gaps filled.
    charts = []
    for station in stations:
        days = station.get("days") or []
        if not days:
            continue
        maximum = (
            max(
                (
                    float(d[k])
                    for d in days
                    for k in ("required_mm", "rain_mm", "et_mm")
                    if d.get(k) is not None and isfinite(float(d[k]))
                ),
                default=1,
            )
            or 1
        )
        parts = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="200" viewBox="0 0 640 200">',
            '<rect width="640" height="200" fill="#f5f9fc"/>',
            '<text x="25" y="20" font-size="12">' + escape(station["name"]) + " · mm/día</text>",
            '<path d="M30 35V160H620" stroke="#9aafbd" fill="none"/>',
        ]
        for field, color in [("required_mm", "#b67700"), ("rain_mm", "#1674ae"), ("et_mm", "#a05ba1")]:
            segment = []
            for i, day in enumerate(days):
                value = day.get(field)
                if value is None:
                    if len(segment) > 1:
                        parts.append(
                            '<polyline fill="none" stroke="'
                            + color
                            + '" stroke-width="2" points="'
                            + " ".join(segment)
                            + '"/>'
                        )
                    segment = []
                    continue
                x = 30 + i * 580 / max(1, len(days) - 1)
                y = 160 - float(value) * 120 / maximum
                segment.append(f"{x:.2f},{y:.2f}")
                parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.5" fill="{color}"/>')
            if len(segment) > 1:
                parts.append(
                    '<polyline fill="none" stroke="'
                    + color
                    + '" stroke-width="2" points="'
                    + " ".join(segment)
                    + '"/>'
                )
        parts.extend(
            [
                '<text x="30" y="178" font-size="10">' + escape(days[0]["date"]) + "</text>",
                '<text x="520" y="178" font-size="10">' + escape(days[-1]["date"]) + "</text>",
                '<text x="30" y="194" font-size="10">Demanda: ocre · Lluvia: azul · ET: violeta · Huecos: sin dato comparable</text></svg>',
            ]
        )
        charts.append("".join(parts))
    return "".join(charts)


def report_view(context: dict) -> dict:
    from markupsafe import Markup

    window = (context.get("windows") or {}).get(str(context.get("selected_window_hours", 72))) or {}
    names = {s["station_id"]: s["name"] for s in context.get("station_influences", [])}
    stations = []
    for station in window.get("stations", []):
        metrics = []
        for key, label, unit in METRICS:
            value = (station.get("values") or {}).get(key)
            raw_field = {
                "temperature_avg_c": "temperature_c",
                "humidity_avg_pct": "humidity_pct",
                "soil_temperature_avg_c": "soil_temperature_c",
                "soil_moisture_avg_pct": "soil_moisture_pct",
                "rain_sum_mm": "rain_mm",
                "evapotranspiration_sum_mm": "evapotranspiration_mm",
            }.get(key)
            stats = (station.get("statistics") or {}).get(raw_field, {})
            if key == "soil_moisture_avg_pct":
                unit = (station.get("units") or {}).get(key, "unidad no confirmada")
            metrics.append(
                {
                    "label": label,
                    "value": value if value is not None else "No reportado",
                    "unit": unit,
                    "minimum": stats.get("minimum"),
                    "maximum": stats.get("maximum"),
                }
            )
        alert = station.get("alert") or {}
        stations.append(
            {
                "name": names.get(station.get("station_id"), "Estación"),
                "from": station.get("from", ""),
                "to": station.get("to", ""),
                "samples": station.get("samples", 0),
                "status": alert.get("level_label")
                or {
                    "EVALUACION_PARCIAL": "Evaluación parcial",
                    "GRAVE": "Grave",
                    "MODERADA": "Moderada",
                    "NINGUNA": "Sin alerta",
                }.get(alert.get("level"), "Sin clasificación"),
                "metrics": metrics,
                "explanation": alert.get("simple_explanation", ""),
                "conditions": [
                    {**c, "status": STATUS.get(c.get("status"), c.get("status"))}
                    for c in alert.get("conditions", [])
                ],
            }
        )
    water = context.get("water_balance") or {}
    code = (context.get("crop") or {}).get("code", "")
    sources = list(context.get("sources") or [])
    sources += [
        {"document": d["document"], "page_or_section": "Referencia revisada · SHA-256 " + d["sha256"]}
        for d in reference_manifest(code)
    ]
    # Surface only documented symptoms/inspection, not a treatment table.
    guidance = (
        context.get("documentary_monitoring")
        or stage_monitoring(code, context.get("phenology", {}).get("stage_code"))
    )[:3]
    guidance_html = []
    for guide in guidance:
        symptoms = "".join("<li>" + escape(text) + "</li>" for text in guide.get("symptoms", []))
        inspection = "".join("<li>" + escape(text) + "</li>" for text in guide.get("inspection", []))
        guidance_html.append(
            "<h3>"
            + escape(guide["name"])
            + " · referencia para verificar</h3><h4>Síntomas descritos</h4><ul>"
            + symptoms
            + "</ul><h4>Inspección en campo</h4><ul>"
            + inspection
            + "</ul><small>"
            + escape(guide["document"] + " · " + guide["page_or_section"])
            + "</small>"
        )
    return {
        "report_stations": stations,
        "water_text": Markup(report_markdown_html(water_summary(water))) if water else "",
        "water_chart": Markup(water_chart_svg(water)),
        "report_sources": sources,
        "field_guidance": Markup("".join(guidance_html)),
    }


async def consultation_assets(db, analysis) -> tuple[list[dict], dict[str, bytes], list[dict]]:
    from PIL import Image, ImageOps
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.models import PlotConversation, SatelliteObservation
    from app.services.satellite import local_satellite_image_path

    images, resources, messages = [], {}, []
    paths = []
    scene = analysis.context.get("satellite") or {}
    if scene.get("id"):
        from uuid import UUID

        observation = await db.get(SatelliteObservation, UUID(scene["id"]))
        if observation:
            path = local_satellite_image_path(observation)
            if path:
                paths.append(
                    (path, "Imagen de la escena seleccionada · " + str(scene.get("captured_at", "")))
                )
    conversations = (
        await db.scalars(
            select(PlotConversation)
            .where(PlotConversation.analysis_id == analysis.id)
            .order_by(PlotConversation.created_at)
        )
    ).all()
    root = (settings.storage_path / "plot-chat" / str(analysis.id)).resolve()
    for conversation in conversations:
        for message in conversation.messages or []:
            messages.append(
                {
                    "role": "Productor" if message.get("role") == "user" else "Asistente",
                    "content": str(message.get("content") or ""),
                }
            )
            for image in message.get("images") or []:
                candidate = Path(image.get("storage_path") or "").resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    paths.append((candidate, "Fotografía adjunta a esta consulta; no confirma diagnóstico."))
    for path, caption in paths[:5]:
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            with Image.open(path) as source:
                if source.width * source.height > 30_000_000:
                    continue
                prepared = ImageOps.exif_transpose(source).convert("RGB")
                prepared.thumbnail((1100, 1100))
                output = BytesIO()
                prepared.save(output, format="JPEG", quality=82)
            content = output.getvalue()
            url = "data:image/jpeg;base64," + base64.b64encode(content).decode("ascii")
            resources[url] = content
            images.append({"url": url, "caption": caption})
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
    return images, resources, messages
