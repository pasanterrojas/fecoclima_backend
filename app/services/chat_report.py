from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertEvaluation, Crop, Station
from app.services.analysis_context import build_complete_analysis_context
from app.services.satellite import (
    latest_satellite_observation,
    local_satellite_image_path,
    local_satellite_index_image_path,
)
from app.services.weather import effective_soil_moisture_unit

PRIMARY = colors.HexColor("#004481")
SECONDARY = colors.HexColor("#3B8DC2")
DARK_TEXT = colors.HexColor("#15334A")
PILImage.MAX_IMAGE_PIXELS = 30_000_000


def _page_frame(canvas, document) -> None:
    canvas.saveState()
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, A4[1] - 1.45 * cm, A4[0], 1.45 * cm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(1.5 * cm, A4[1] - 0.9 * cm, "FECOCLIMA IA · Consulta de campo")
    canvas.setFillColor(DARK_TEXT)
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(A4[0] - 1.5 * cm, 0.75 * cm, f"Página {document.page}")
    canvas.restoreState()


def _report_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ChatTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=23,
            leading=28,
            textColor=PRIMARY,
            alignment=TA_CENTER,
            spaceAfter=12,
        ),
        "subtitle": ParagraphStyle(
            "ChatSubtitle",
            parent=base["BodyText"],
            fontSize=10,
            leading=15,
            textColor=DARK_TEXT,
            alignment=TA_CENTER,
            spaceAfter=16,
        ),
        "h1": ParagraphStyle(
            "ChatH1",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=19,
            textColor=PRIMARY,
            spaceBefore=12,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "ChatH2",
            parent=base["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=15,
            textColor=PRIMARY,
            spaceBefore=7,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "ChatBody",
            parent=base["BodyText"],
            fontSize=9.2,
            leading=13.5,
            textColor=DARK_TEXT,
            spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "ChatSmall",
            parent=base["BodyText"],
            fontSize=7.8,
            leading=11,
            textColor=colors.HexColor("#567083"),
            spaceAfter=4,
        ),
        "user": ParagraphStyle(
            "ChatUser",
            parent=base["BodyText"],
            fontSize=9.2,
            leading=13.5,
            textColor=DARK_TEXT,
            backColor=colors.HexColor("#EAF3F8"),
            borderColor=SECONDARY,
            borderWidth=0.5,
            borderPadding=8,
            spaceAfter=8,
        ),
        "assistant": ParagraphStyle(
            "ChatAssistant",
            parent=base["BodyText"],
            fontSize=9.2,
            leading=13.5,
            textColor=DARK_TEXT,
            backColor=colors.white,
            borderColor=colors.HexColor("#C8D8E2"),
            borderWidth=0.5,
            borderPadding=8,
            spaceAfter=8,
        ),
    }


def _inline_markdown(value: str) -> str:
    text = escape(value.strip())
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+)`", r"<font name=\"Courier\">\1</font>", text)
    return text


def _markdown_flowables(markdown: str, styles: dict[str, ParagraphStyle]) -> list[Any]:
    output: list[Any] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            output.append(Paragraph(_inline_markdown(" ".join(paragraph)), styles["body"]))
            paragraph.clear()

    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            flush()
            output.append(Paragraph(_inline_markdown(heading.group(1)), styles["h2"]))
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            flush()
            output.append(Paragraph(f"• {_inline_markdown(bullet.group(1))}", styles["body"]))
            continue
        if line.startswith(">"):
            flush()
            output.append(Paragraph(_inline_markdown(line.lstrip("> ")), styles["small"]))
            continue
        paragraph.append(line)
    flush()
    return output


def _normalized_jpeg(content: bytes) -> tuple[bytes, int, int] | None:
    try:
        with PILImage.open(io.BytesIO(content)) as source:
            if source.width * source.height > 30_000_000:
                return None
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1800, 1800))
            if image.mode not in {"RGB", "L"}:
                background = PILImage.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=86, optimize=True)
            return buffer.getvalue(), image.width, image.height
    except (UnidentifiedImageError, OSError, ValueError, PILImage.DecompressionBombError):
        return None


def _image_block(
    content: bytes,
    caption: str,
    styles: dict[str, ParagraphStyle],
    *,
    max_height_cm: float = 9.5,
) -> KeepTogether | None:
    normalized = _normalized_jpeg(content)
    if not normalized:
        return None
    data, width, height = normalized
    max_width = 16.2 * cm
    max_height = max_height_cm * cm
    scale = min(max_width / width, max_height / height, 1.0)
    flowable = Image(io.BytesIO(data), width=width * scale, height=height * scale)
    return KeepTogether(
        [
            flowable,
            Spacer(1, 0.12 * cm),
            Paragraph(_inline_markdown(caption), styles["small"]),
            Spacer(1, 0.25 * cm),
        ]
    )


def _styled_table(rows: list[list[Any]], widths: list[float]) -> Table:
    cell_style = ParagraphStyle(
        "TableCell",
        fontName="Helvetica",
        fontSize=7.7,
        leading=10.2,
        textColor=DARK_TEXT,
    )
    label_style = ParagraphStyle(
        "TableLabel",
        parent=cell_style,
        fontName="Helvetica-Bold",
        textColor=PRIMARY,
    )
    wrapped_rows = [rows[0]]
    for row in rows[1:]:
        wrapped_rows.append(
            [
                Paragraph(escape(str(value)), label_style if index == 0 else cell_style)
                for index, value in enumerate(row)
            ]
        )
    table = Table(wrapped_rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 1), (0, -1), PRIMARY),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C8D8E2")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LEADING", (0, 0), (-1, -1), 11),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _stat_text(stats: dict[str, Any], name: str, unit: str) -> str:
    raw = stats.get(name)
    return "—" if raw is None else f"{raw} {unit}".strip()


async def generate_chat_report_pdf(
    db: AsyncSession,
    alert_id: UUID,
    *,
    period: str,
    conversation: list[dict[str, Any]],
    uploaded_images: list[dict[str, Any]],
) -> bytes:
    alert = await db.get(AlertEvaluation, alert_id)
    if not alert:
        raise ValueError("Evaluación de alerta no encontrada")
    station = await db.get(Station, alert.station_id)
    crop = await db.get(Crop, alert.crop_id)
    if not station or not crop:
        raise ValueError("El contexto de estación o cultivo ya no está disponible")
    context = await build_complete_analysis_context(db, alert.id, period=period)
    styles = _report_styles()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.45 * cm,
        leftMargin=1.45 * cm,
        topMargin=1.85 * cm,
        bottomMargin=1.25 * cm,
        title=f"Consulta de campo · {crop.name} · {station.name}",
        author="FECOCLIMA IA",
    )

    query = context.get("query") or {}
    phenology = context.get("phenology") or {}
    deterministic = context.get("deterministic_result") or {}
    freshness = context.get("data_freshness") or {}
    story: list[Any] = [
        Spacer(1, 0.3 * cm),
        Paragraph("Reporte de consulta contextual", styles["title"]),
        Paragraph(
            "Conversación, evidencia meteorológica, reglas publicadas, fenología, Copernicus e imágenes de campo vinculadas a una misma consulta.",
            styles["subtitle"],
        ),
    ]
    overview = [
        ["Consulta", "Valor"],
        ["Estación", station.name],
        ["Cultivo", crop.name],
        ["Fecha de siembra", query.get("planting_date") or "No disponible"],
        ["Fecha de evaluación", query.get("evaluation_date") or "No disponible"],
        ["Periodo", period],
        ["Etapa estimada", phenology.get("name") or phenology.get("stage_name") or "No disponible"],
        ["Nivel determinístico", deterministic.get("level") or "No disponible"],
        ["Última lectura", freshness.get("latest_reading_at") or "No disponible"],
        ["Generado", datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")],
    ]
    story.extend([_styled_table(overview, [4.5 * cm, 11.7 * cm]), Spacer(1, 0.45 * cm)])
    story.append(
        Paragraph(
            "Este documento apoya el monitoreo y conserva la diferencia entre hipótesis y diagnóstico. No autoriza aplicaciones fitosanitarias; cualquier decisión debe validarse con un profesional y la etiqueta vigente.",
            styles["small"],
        )
    )

    observed = (context.get("observed_series_summary") or {}).get("variables") or {}
    metric_rows: list[list[Any]] = [["Variable", "Promedio", "Mínimo", "Máximo", "Acumulado"]]
    soil_unit = effective_soil_moisture_unit(station)
    for key, label, unit in [
        ("temperature_c", "Temperatura ambiente", "°C"),
        ("humidity_pct", "Humedad relativa", "%"),
        ("rain_mm", "Lluvia", "mm"),
        ("soil_temperature_c", "Temperatura del suelo", "°C"),
        ("soil_moisture_pct", "Humedad del suelo", soil_unit),
        ("leaf_sensor_value", "Sensor de hoja", ""),
        ("evapotranspiration_mm", "Evapotranspiración", "mm"),
    ]:
        stats = observed.get(key) or {}
        if not stats:
            continue

        metric_rows.append(
            [
                label,
                _stat_text(stats, "mean", unit),
                _stat_text(stats, "min", unit),
                _stat_text(stats, "max", unit),
                _stat_text(stats, "sum", unit),
            ]
        )
    if len(metric_rows) > 1:
        story.extend(
            [
                Paragraph("Evidencia meteorológica del periodo", styles["h1"]),
                Paragraph(
                    f"Se utilizaron {(context.get('observed_series_summary') or {}).get('samples', 0)} lecturas dentro de la ventana horaria exacta.",
                    styles["body"],
                ),
                _styled_table(metric_rows, [4.3 * cm, 3 * cm, 3 * cm, 3 * cm, 2.9 * cm]),
            ]
        )

    condition_rows = [["Condición", "Estado", "Valor observado", "Explicación"]]
    for item in deterministic.get("conditions") or []:
        condition_rows.append(
            [
                item.get("label") or item.get("key") or "Condición",
                item.get("status") or "—",
                f"{item.get('value')} {item.get('unit') or ''}" if item.get("value") is not None else "Sin dato",
                item.get("explanation") or "—",
            ]
        )
    if len(condition_rows) > 1:
        story.extend(
            [
                Paragraph("Resultado y reglas evaluadas", styles["h1"]),
                Paragraph(escape(str(deterministic.get("simple_explanation") or "")), styles["body"]),
                _styled_table(condition_rows, [4 * cm, 2.4 * cm, 3.1 * cm, 6.7 * cm]),
            ]
        )

    try:
        _, observation = await latest_satellite_observation(db, station.id, crop.id)
    except Exception:
        observation = None
    satellite_blocks: list[Any] = []
    if observation:
        paths = [
            (local_satellite_image_path(observation), "Imagen RGB Copernicus de la geozona"),
            (local_satellite_index_image_path(observation, "ndvi"), "Capa NDVI de la misma escena"),
        ]
        for path, caption in paths:
            if path:
                block = _image_block(path.read_bytes(), caption, styles, max_height_cm=8)
                if block:
                    satellite_blocks.append(block)
    if satellite_blocks:
        story.extend([Paragraph("Imágenes satelitales de apoyo", styles["h1"]), *satellite_blocks])

    if uploaded_images:
        field_image_blocks: list[KeepTogether] = []
        for index, item in enumerate(uploaded_images, start=1):
            block = _image_block(
                item.get("content") or b"",
                f"Imagen de campo {index}: {item.get('filename') or 'sin nombre'}",
                styles,
            )
            if block:
                field_image_blocks.append(block)
        if field_image_blocks:
            story.append(
                KeepTogether(
                    [
                        Paragraph("Imágenes de campo adjuntas", styles["h1"]),
                        field_image_blocks[0],
                    ]
                )
            )
            story.extend(field_image_blocks[1:])
            story.append(
                Paragraph(
                    "Las fotografías son evidencia complementaria. Su apariencia puede variar por enfoque, luz, escala y parte de la planta; no constituyen diagnóstico por sí solas.",
                    styles["small"],
                )
            )

    story.extend([PageBreak(), Paragraph("Conversación con el agente", styles["h1"]),])
    for index, item in enumerate(conversation, start=1):
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        label = "Productor o técnico" if role == "user" else "Agente FECOCLIMA IA"
        story.append(Paragraph(f"{index}. {label}", styles["h2"]))
        if role == "user":
            story.append(Paragraph(_inline_markdown(content), styles["user"]))
        else:
            answer = _markdown_flowables(content, styles)
            story.extend(answer or [Paragraph("Sin respuesta registrada.", styles["assistant"])])

    story.extend(
        [
            Paragraph("Trazabilidad y límites", styles["h1"]),
            Paragraph(
                "La conversación usa la evaluación indicada en la portada. Los datos de estación y el nivel de alerta se conservan como fueron calculados; la interpretación del agente no los reemplaza. Verifique síntomas, distribución y evolución en el lote antes de tomar una decisión.",
                styles["body"],
            ),
        ]
    )
    document.build(story, onFirstPage=_page_frame, onLaterPages=_page_frame)
    return buffer.getvalue()
