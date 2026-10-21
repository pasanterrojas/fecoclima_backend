"""Deterministic input and output boundaries for orchestrator widget permissions."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from app.services.widget_text import widget_summary

WIDGETS = ("raw_data", "alerts", "phenology", "diagnosis", "satellite", "diseases", "forecast", "chat")
PERMISSION_PROTOCOL = "orchestrator-widgets-v1"
WIDGET_LABELS = {
    "raw_data": "Condiciones actuales", "alerts": "Tipo de alerta", "phenology": "Estado fenológico",
    "diagnosis": "Análisis agronómico", "satellite": "Interpretación del polígono",
    "diseases": "Posibles enfermedades", "forecast": "Predicción", "chat": "Consulta contextual",
}


def allowed_widgets(schema: dict | None) -> list[str]:
    """An omitted permission list means all; an explicit empty list means none."""
    requested = schema.get("allowed_widgets", list(WIDGETS)) if isinstance(schema, dict) else list(WIDGETS)
    if not isinstance(requested, list):
        return []
    return [key for key in WIDGETS if key in requested]


def _without_diseases(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_diseases(item) for key, item in value.items()
                if key not in {"diseases", "possible_diseases", "disease_rules"}}
    if isinstance(value, list):
        return [_without_diseases(item) for item in value]
    return deepcopy(value)


def permitted_context(original: dict, permitted: list[str], previous: dict | None = None) -> dict:
    """Whitelist topics; a permission does not implicitly authorize adjacent ones.

    Combined specialist prose is reusable only when ALL of its topics are
    permitted. The persisted deterministic analysis is never modified here.
    """
    context = {key: deepcopy(original[key]) for key in ("protocol", "reference_at", "crop") if key in original}
    context["plot"] = {key: deepcopy(value) for key, value in (original.get("plot") or {}).items()
                       if key in {"id", "name", "crop_id", "crop_code"}}
    fields = {
        "raw_data": ("raw_readings", "station_influences"),
        "alerts": ("windows", "rule_version", "spatial_coverage_pct", "station_influences"),
        "phenology": ("phenology",),
        "satellite": ("satellite", "radar", "inspection_point"),
        "diseases": ("possible_diseases",),
        "forecast": ("forecast", "historical_regression"),
    }
    for widget in permitted:
        for key in fields.get(widget, ()):
            if key in original:
                context[key] = deepcopy(original[key])
    if "alerts" in permitted and "diseases" not in permitted:
        context["windows"] = _without_diseases(context.get("windows", {}))
    if "phenology" in permitted and "planting_date" in original.get("plot", {}):
        context["plot"]["planting_date"] = original["plot"]["planting_date"]
    if "satellite" in permitted:
        for key in ("geojson", "area_ha", "latitude", "longitude"):
            if key in original.get("plot", {}):
                context["plot"][key] = deepcopy(original["plot"][key])
    specialist_topics = {
        "RAG_CROP": {"raw_data", "alerts", "phenology", "diagnosis", "satellite", "diseases", "forecast"},
        "WEATHER_ANALYSIS": {"raw_data", "forecast"},
        "SATELLITE_ANALYSIS": {"satellite"},
    }
    context["previous_widget_interpretations"] = {
        key: deepcopy(value) for key, value in (previous or {}).items()
        if key in specialist_topics and specialist_topics[key].issubset(set(permitted))
    }
    context["allowed_widgets"] = list(permitted)
    return context


def output_contract(permitted: list[str]) -> str:
    names = ", ".join(permitted)
    return (
        "\nCONTRATO OBLIGATORIO DEL ORQUESTADOR: esta configuración autoriza exclusivamente los widgets "
        f"[{names}]. Devuelve SOLO un objeto JSON válido con esta estructura: "
        '{"widgets":{"clave_autorizada":"explicación breve en español"}}. '
        "No agregues claves fuera de la lista, ni texto fuera del JSON. Cada explicación admite hasta 300 caracteres. "
        "No generes contenido de los otros widgets. No inventes datos faltantes; explica la limitación del tema autorizado. "
        "Este contrato de formato y alcance tiene prioridad sobre las preferencias de Markdown del agente."
    )


def parse_widgets(value: str, permitted: list[str]) -> dict[str, str]:
    text = str(value or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    if len(text) > 50000:
        raise ValueError("orchestrator_invalid_output")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("orchestrator_invalid_output") from exc
    widgets = parsed.get("widgets") if isinstance(parsed, dict) else None
    if not isinstance(widgets, dict):
        raise ValueError("orchestrator_invalid_output")
    accepted = {key: widget_summary(widgets[key]) for key in permitted
                if isinstance(widgets.get(key), str) and widgets[key].strip()}
    if permitted and not accepted:
        raise ValueError("orchestrator_invalid_output")
    return accepted


def widgets_markdown(widgets: dict[str, str]) -> str:
    return "\n\n".join(f"### {WIDGET_LABELS[key]}\n\n{widgets[key]}" for key in WIDGETS if widgets.get(key))


def fallback_widgets(context: dict, permitted: list[str]) -> dict[str, str]:
    """Topic-local fallbacks, without inferred severity or unauthorized sections."""
    window = (context.get("windows") or {}).get("72") or {}
    stage = context.get("phenology") or {}
    satellite = context.get("satellite") or {}
    forecast = context.get("forecast") or {}
    raw = next((row for row in context.get("raw_readings", []) if row.get("reading")), {})
    diseases = list(dict.fromkeys(str(disease["name"]) for row in context.get("possible_diseases", [])
                                 for disease in row.get("diseases", []) if disease.get("name")))
    readings = raw.get("reading") or {}
    observed = []
    for key, label, unit in (("temperature_c", "temperatura", "°C"), ("humidity_pct", "humedad del aire", "%"), ("rain_mm", "lluvia", "mm")):
        if readings.get(key) is not None:
            observed.append(f"{label} {readings[key]} {unit}")
    labels = {"NINGUNA": "Normal", "MODERADA": "Moderada", "GRAVE": "Grave", "DATOS_INSUFICIENTES": "Sin clasificación: faltan datos compatibles"}
    defaults = {
        "raw_data": (f"Última lectura de {raw.get('name', 'la estación')}: {', '.join(observed)}." if observed else "No hay una lectura meteorológica disponible para interpretar."),
        "alerts": f"{labels.get(window.get('level'), 'Alerta sin evaluar')}. Revise las condiciones medidas y sus limitaciones antes de actuar.",
        "phenology": (f"Etapa estimada: {stage.get('stage_name')}. La estimación por calendario debe confirmarse en campo." if stage.get("stage_name") else "No hay una etapa fenológica calculada disponible."),
        "diagnosis": "Revise las observaciones autorizadas y confirme en campo cualquier señal del cultivo. No hay una síntesis de IA validada para ampliar la conclusión.",
        "satellite": (f"Escena satelital: {satellite.get('captured_at', 'fecha no disponible')}. Los índices son orientativos; compare las zonas señaladas en campo." if satellite else "No hay una imagen satelital válida procesada para interpretar este polígono."),
        "diseases": (f"Hipótesis por verificar: {', '.join(diseases)}. Estos riesgos no confirman una enfermedad." if diseases else "Los datos disponibles no aportan una hipótesis de enfermedad verificable; no se confirma ni descarta enfermedad."),
        "forecast": (f"Hay pronóstico meteorológico para {', '.join(str(day.get('date', '')) for day in (forecast.get('days') or [])[:3])}. Es una previsión, no una medición del terreno." if forecast.get("days") else "No hay una predicción meteorológica disponible para esta parcela."),
        "chat": "Puede consultar dudas sobre los resultados disponibles de esta parcela. Las respuestas deben respetar sus datos y limitaciones.",
    }
    return {key: widget_summary(defaults[key]) for key in permitted}
