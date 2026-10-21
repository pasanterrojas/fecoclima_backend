"""Sandboxed HTML templates rendered to PDF with external resource access disabled."""

from __future__ import annotations

from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

DEFAULT_TEMPLATE = """<!doctype html><html lang="es"><meta charset="utf-8">
<style>@page {size:A4;margin:18mm} body {font:11pt sans-serif;color:#15334a}
h1,h2 {color:#004481} table {width:100%;border-collapse:collapse} td,th {padding:7px;border:1px solid #ccd9e4}
pre {white-space:pre-wrap;font:10pt sans-serif} footer {margin-top:20px;font-size:9pt}</style>
<h1>FECOCLIMA IA · {{ context.plot.name }}</h1>
<p>{{ context.crop.name }} · Siembra: {{ context.plot.planting_date }} · {{ context.plot.area_ha }} ha</p>
<p>Referencia del análisis: {{ context.reference_at }}</p>
<h2>Etapa estimada</h2><p>{{ context.phenology.stage_name }}</p>
<h2>Alertas calculadas</h2><table><tr><th>Ventana</th><th>Estado</th></tr>
{% for hours, window in context.windows.items() %}
<tr><td>{{ hours }} h</td><td>{{ window.level }}</td></tr>{% endfor %}</table>
<h2>Interpretación</h2>{{ narrative_html }}
<h2>Últimas lecturas</h2>
{% for station in context.raw_readings %}<p><b>{{ station.name }}</b>
{% if station.reading %} {{ station.reading.sampled_at }} · {{ station.reading.temperature_c }} °C
· {{ station.reading.humidity_pct }}% HR{% else %}Sin lectura disponible{% endif %}</p>{% endfor %}
<h2>Próximos tres días · Open-Meteo</h2>
{% for day in context.forecast.days %}<p>{{ day.date }} · {{ day.temperature_min_c }} a
{{ day.temperature_max_c }} °C · {{ day.precipitation_mm }} mm</p>{% endfor %}
<h2>Evidencia satelital</h2>
{% if context.satellite %}<p>{{ context.satellite.captured_at }} · NDVI {{ context.satellite.ndvi }}
· NDMI {{ context.satellite.ndmi }} · NDWI {{ context.satellite.ndwi }}</p>
{% else %}<p>Sin escena óptica procesada para esta parcela.</p>{% endif %}
{% if context.radar %}<p>Sentinel-1 · {{ context.radar.captured_at }} · VV {{ context.radar.vv_mean }}
· VH {{ context.radar.vh_mean }} (gamma0 lineal)</p>{% endif %}
<h2>Fuentes agronómicas</h2>
{% for source in context.sources %}<p>{{ source.document }} · {{ source.page_or_section }}</p>{% endfor %}
<h2>Estaciones influyentes</h2>{% for station in context.station_influences %}
<p>{{ station.name }} · cobertura {{ station.coverage_pct }}%</p>{% endfor %}
<h2>Punto de revisión</h2><p>{{ context.inspection_point.latitude }}, {{ context.inspection_point.longitude }}
 · {{ context.inspection_point.method }}</p>
<h2>Limitaciones</h2>{% for item in context.limitations %}<p>{{ item }}</p>{% endfor %}
<footer>Datos y cálculos determinísticos; la IA solo interpreta.
Confirmar hipótesis agronómicas en campo.</footer></html>"""


def render_plot_html(context: dict[str, Any], narrative: str, template: str | None = None) -> str:
    env = ImmutableSandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
    # Templates may arrange already-calculated values, never calculate them.
    env.globals.clear()
    env.filters = {
        key: value
        for key, value in env.filters.items()
        if key in {"escape", "e", "default", "d", "replace", "title", "capitalize", "lower", "upper", "join"}
    }
    source = template or DEFAULT_TEMPLATE
    tree = env.parse(source)
    from jinja2 import nodes

    forbidden = (nodes.Add, nodes.Sub, nodes.Mul, nodes.Div, nodes.FloorDiv, nodes.Mod, nodes.Pow)
    if any(isinstance(node, forbidden) for node in tree.find_all(nodes.Expr)):
        raise ValueError("La plantilla solo puede presentar resultados; calcule en Python")
    from markupsafe import Markup

    from app.services.producer_notifications import _markdown_to_html

    return env.from_string(source).render(
        context=context, narrative=narrative, narrative_html=Markup(_markdown_to_html(narrative))
    )


def blocked_resource(url: str, *args, **kwargs):
    raise ValueError("Los PDF no permiten cargar recursos externos o archivos locales")


def plot_pdf_bytes(context: dict, narrative: str, template: str | None = None) -> bytes:
    from weasyprint import HTML

    return HTML(
        string=render_plot_html(context, narrative, template), url_fetcher=blocked_resource
    ).write_pdf()


async def generate_plot_report(db, report_id) -> dict:
    import asyncio
    from uuid import UUID

    from app.core.config import settings
    from app.db.models import PlotAnalysis, PlotReport
    from app.services.agent_runtime import select_agent_version
    from app.services.plot_agents import interpret_plot

    report = await db.get(PlotReport, report_id)
    if not report:
        raise ValueError("Informe no encontrado")
    analysis = await db.get(PlotAnalysis, report.analysis_id)
    report.status = "RUNNING"
    await db.commit()
    try:
        result = await interpret_plot(
            db, analysis, "PDF_REPORT", window_hours=analysis.context.get("selected_window_hours", 72)
        )
        _, version = await select_agent_version(db, UUID(analysis.context["crop"]["id"]), "PDF_REPORT")
        template = version.html_template if version else None
        pdf = await asyncio.to_thread(
            plot_pdf_bytes, {**analysis.context, "sources": result["sources"]}, result["markdown"], template
        )
        folder = settings.storage_path / "reports" / "plots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{report.id}.pdf"
        path.write_bytes(pdf)
        report.storage_path, report.status, report.error = str(path), "COMPLETED", None
        await db.commit()
        return {"report_id": str(report.id), "size_bytes": len(pdf)}
    except Exception:
        await db.rollback()
        report = await db.get(PlotReport, report_id)
        report.status, report.error = (
            "FAILED",
            "No se pudo generar el PDF; revise la plantilla y las dependencias",
        )
        await db.commit()
        raise
