"""Sandboxed HTML templates rendered to PDF with external resource access disabled."""

from __future__ import annotations

from typing import Any
from pathlib import Path

from jinja2 import StrictUndefined, Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

DEFAULT_TEMPLATE = (Path(__file__).parents[1] / 'templates' / 'consultation_report.html').read_text()


def render_plot_html(context: dict[str, Any], narrative: str, template: str | None = None,
                     *, images: list | None = None, messages: list | None = None) -> str:
    env = ImmutableSandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
    # Templates may arrange already-calculated values, never calculate them.
    env.globals.clear()
    env.filters = {
        key: value
        for key, value in env.filters.items()
        if key in {"escape", "e", "default", "d", "replace", "title", "capitalize", "lower", "upper", "join"}
    }
    env.filters['observed'] = lambda value: 'No disponible' if value is None or isinstance(value, Undefined) else value
    source = template or DEFAULT_TEMPLATE
    tree = env.parse(source)
    from jinja2 import nodes

    forbidden = (nodes.Add, nodes.Sub, nodes.Mul, nodes.Div, nodes.FloorDiv, nodes.Mod, nodes.Pow)
    if any(isinstance(node, forbidden) for node in tree.find_all(nodes.Expr)):
        raise ValueError("La plantilla solo puede presentar resultados; calcule en Python")
    from markupsafe import Markup

    from app.services.consultation_report import report_view, report_markdown_html
    return env.from_string(source).render(
        context=context, narrative=narrative, narrative_html=Markup(report_markdown_html(narrative)),
        **report_view(context), report_images=images or [], consultation_messages=messages or [],
    )


def blocked_resource(url: str, *args, **kwargs):
    raise ValueError("Los PDF no permiten cargar recursos externos o archivos locales")


def plot_pdf_bytes(context: dict, narrative: str, template: str | None = None,
                   *, images: list | None = None, resources: dict | None = None, messages: list | None = None) -> bytes:
    from weasyprint import HTML

    def fetch_resource(url, *args, **kwargs):
        if url in (resources or {}):
            return {'string': resources[url], 'mime_type': 'image/jpeg'}
        return blocked_resource(url, *args, **kwargs)
    return HTML(
        string=render_plot_html(context, narrative, template, images=images, messages=messages), url_fetcher=fetch_resource
    ).write_pdf()


async def generate_plot_report(db, report_id, *, client_model: bool = False) -> dict:
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
        template = version.html_template if version and not client_model else None
        from app.services.consultation_report import consultation_assets
        images, resources, messages = await consultation_assets(db, analysis)
        pdf = await asyncio.to_thread(
            plot_pdf_bytes, {**analysis.context, "sources": result["sources"]}, result["markdown"], template,
            images=images, resources=resources, messages=messages,
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
