"""Validate the orchestrator's configured output destinations deterministically."""
from __future__ import annotations

import json

WIDGET_LABELS = {
    "raw_data": "Condiciones actuales", "alerts": "Tipo de alerta",
    "phenology": "Estado fenológico", "diagnosis": "Análisis de la parcela",
    "satellite": "Imagen e indicadores", "diseases": "Posibles enfermedades",
    "forecast": "Predicción", "chat": "Orientación para la conversación",
}


def permitted_widgets(schema: dict | None) -> list[str]:
    configured = (schema or {}).get("allowed_widgets", list(WIDGET_LABELS))
    if not isinstance(configured, list):
        return []
    return [key for key in WIDGET_LABELS if key in configured]


def parse_widget_outputs(content: str, allowed: list[str]) -> dict[str, str]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    parsed = json.loads(text)
    sections = parsed.get("widgets") if isinstance(parsed, dict) else None
    if not isinstance(sections, dict):
        raise ValueError("El orquestador no devolvió secciones verificables")
    result = {key: sections[key].strip() for key in allowed
              if isinstance(sections.get(key), str) and sections[key].strip()}
    if not result:
        raise ValueError("El orquestador no devolvió widgets permitidos")
    return result


def render_widgets(sections: dict[str, str]) -> str:
    return "\n\n".join(f"**{WIDGET_LABELS[key]}**\n\n{text}" for key, text in sections.items())


def fallback_widgets(context: dict, allowed: list[str]) -> dict[str, str]:
    window = (context.get("windows") or {}).get(str(context.get("selected_window_hours", 72))) or {}
    stage = context.get("phenology") or {}
    satellite = context.get("satellite") or {}
    content = {
        "raw_data": "Consulte la última lectura de las estaciones que cubren su parcela.",
        "alerts": {"NINGUNA": "No se alcanzó el umbral de alerta configurado.",
                   "MODERADA": "Se activó una alerta moderada; revise el cultivo.",
                   "GRAVE": "Se activó una alerta grave; priorice la revisión en campo."}.get(
                       window.get("level"), "La clasificación está pendiente: revise las condiciones sin medición compatible."),
        "phenology": f"Etapa estimada: {stage.get('stage_name') or 'sin calendario disponible'}. Confirme en campo.",
        "diagnosis": "Revise las condiciones observadas y los sectores señalados. Falta la interpretación del modelo para completar la recomendación.",
        "satellite": "Hay una escena del polígono disponible; consulte su fecha y capas." if satellite else "Todavía no hay una escena válida procesada para el polígono.",
        "diseases": "Consulte las reglas y evidencias disponibles. Una señal de riesgo requiere confirmación en campo.",
        "forecast": "Consulte el pronóstico disponible para las coordenadas de la parcela.",
        "chat": "Pregunte sobre las mediciones y limitaciones de esta consulta.",
    }
    return {key: content[key] for key in allowed}
