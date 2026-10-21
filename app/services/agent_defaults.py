from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from app.core.config import settings
from app.db.models import AIAgent, AIAgentVersion, Crop

AGENT_PURPOSES = {
    "ORCHESTRATOR": "Orquestador de parcela",
    "RAG_CROP": "Conocimiento agronómico por cultivo",
    "PDF_REPORT": "Informes PDF de parcela",
    "WEATHER_ANALYSIS": "Interpretación de datos meteorológicos",
    "ALERT_ANALYSIS": "Interpretación de alertas",
    "DISEASE_ANALYSIS": "Enfermedades posibles",
    "FORECAST_ANALYSIS": "Pronóstico y tendencia estadística",
    "CROP_CHAT": "Chat contextual de parcela",
    "CROP_ANALYSIS": "Criterio general para productores",
    "SATELLITE_ANALYSIS": "Análisis de Copernicus y geozona",
    "PHENOLOGY_ANALYSIS": "Análisis ambiental desde la siembra",
    "YEAR_COMPARISON": "Comparación con el mismo periodo del año anterior",
    "DAILY_DIGEST": "Boletín automático de 72 horas",
}

AGENT_CATEGORIES = {
    'RAG_CROP': 'AGRO', 'CROP_ANALYSIS': 'AGRO', 'ALERT_ANALYSIS': 'AGRO',
    'DISEASE_ANALYSIS': 'AGRO', 'PHENOLOGY_ANALYSIS': 'AGRO',
    'WEATHER_ANALYSIS': 'DATOS', 'FORECAST_ANALYSIS': 'DATOS',
    'PDF_REPORT': 'ACADEMICO', 'SATELLITE_ANALYSIS': 'GIS',
    'DAILY_DIGEST': 'NOTIFICADOR', 'ORCHESTRATOR': 'ORQUESTADOR', 'CROP_CHAT': 'CHAT',
    'YEAR_COMPARISON': 'DATOS',
}
CATEGORY_LABELS = {'AGRO':'Agente agro','DATOS':'Agente de datos','ACADEMICO':'Agente académico','GIS':'Agente GIS','NOTIFICADOR':'Agente notificador','ORQUESTADOR':'Agente orquestador','CHAT':'Agente chat'}
DEFAULT_PARCEL_PURPOSES = ("RAG_CROP",)
SHARED_PARCEL_PURPOSES = ("WEATHER_ANALYSIS", "SATELLITE_ANALYSIS", "ORCHESTRATOR", "CROP_CHAT")
PURPOSE_THEMES = {
    "ALERT_ANALYSIS": "RAG_CROP", "PHENOLOGY_ANALYSIS": "RAG_CROP",
    "DISEASE_ANALYSIS": "RAG_CROP", "CROP_ANALYSIS": "RAG_CROP",
    "FORECAST_ANALYSIS": "WEATHER_ANALYSIS", "YEAR_COMPARISON": "WEATHER_ANALYSIS",
}

_RESPONSE_POLICY_DEFAULTS: dict[str, dict[str, Any]] = {
    "CROP_ANALYSIS": {
        "detail_level": "BRIEF",
        "max_words": 260,
        "max_sections": 4,
        "tone": "DIRECT_FRIENDLY",
        "include_observed": True,
        "include_forecast": True,
        "include_satellite": True,
        "include_year_comparison": True,
        "include_sources": False,
        "show_technical_values": False,
        "show_system_details": False,
        "use_emojis": True,
        "custom_instructions": "",
    },
    "SATELLITE_ANALYSIS": {
        "detail_level": "BALANCED",
        "max_words": 420,
        "max_sections": 5,
        "tone": "DIRECT_FRIENDLY",
        "include_observed": True,
        "include_forecast": True,
        "include_satellite": True,
        "include_year_comparison": False,
        "include_sources": False,
        "show_technical_values": True,
        "show_system_details": False,
        "use_emojis": True,
        "custom_instructions": "",
    },
    "PHENOLOGY_ANALYSIS": {
        "detail_level": "BRIEF",
        "max_words": 300,
        "max_sections": 4,
        "tone": "DIRECT_FRIENDLY",
        "include_observed": True,
        "include_forecast": False,
        "include_satellite": False,
        "include_year_comparison": False,
        "include_sources": False,
        "show_technical_values": False,
        "show_system_details": False,
        "use_emojis": True,
        "custom_instructions": "",
    },
    "YEAR_COMPARISON": {
        "detail_level": "BALANCED",
        "max_words": 360,
        "max_sections": 4,
        "tone": "DIRECT_FRIENDLY",
        "include_observed": True,
        "include_forecast": False,
        "include_satellite": False,
        "include_year_comparison": True,
        "include_sources": False,
        "show_technical_values": True,
        "show_system_details": False,
        "use_emojis": True,
        "custom_instructions": "",
    },
    "DAILY_DIGEST": {
        "detail_level": "BRIEF",
        "max_words": 320,
        "max_sections": 5,
        "tone": "DIRECT_FRIENDLY",
        "include_observed": True,
        "include_forecast": True,
        "include_satellite": True,
        "include_year_comparison": False,
        "include_sources": False,
        "show_technical_values": False,
        "show_system_details": False,
        "use_emojis": True,
        "custom_instructions": "",
    },
}

_RESPONSE_POLICY_KEYS = set(next(iter(_RESPONSE_POLICY_DEFAULTS.values())).keys())

AI_OUTPUT_SCHEMA = {
    "format": "markdown",
    "requirements": [
        "Lenguaje claro para productores",
        "Priorizar qué hacer hoy",
        "No alterar el resultado determinístico",
        "No inventar datos ni diagnósticos",
    ],
    "response_policy": deepcopy(_RESPONSE_POLICY_DEFAULTS["CROP_ANALYSIS"]),
}


def default_response_policy(purpose: str = "CROP_ANALYSIS") -> dict[str, Any]:
    return deepcopy(_RESPONSE_POLICY_DEFAULTS.get(purpose, _RESPONSE_POLICY_DEFAULTS["CROP_ANALYSIS"]))


def normalize_response_policy(output_schema: dict[str, Any] | None, purpose: str) -> dict[str, Any]:
    """Return a safe response policy, including backwards compatibility.

    Existing agents may have only the legacy ``requirements`` object. New
    presentation controls are stored inside the already-existing output_schema
    JSON column, so no database migration is required.
    """

    policy = default_response_policy(purpose)
    schema = output_schema if isinstance(output_schema, dict) else {}
    configured = schema.get("response_policy")
    if isinstance(configured, dict):
        for key in _RESPONSE_POLICY_KEYS:
            if key in configured and configured[key] is not None:
                policy[key] = configured[key]

    policy["detail_level"] = str(policy.get("detail_level") or "BRIEF").upper()
    if policy["detail_level"] not in {"BRIEF", "BALANCED", "DETAILED"}:
        policy["detail_level"] = "BRIEF"
    policy["tone"] = str(policy.get("tone") or "DIRECT_FRIENDLY").upper()
    if policy["tone"] not in {"DIRECT_FRIENDLY", "SIMPLE_TECHNICAL", "INSTITUTIONAL"}:
        policy["tone"] = "DIRECT_FRIENDLY"
    policy["max_words"] = min(max(int(policy.get("max_words") or 260), 100), 1200)
    policy["max_sections"] = min(max(int(policy.get("max_sections") or 4), 2), 8)
    for key in {
        "include_observed",
        "include_forecast",
        "include_satellite",
        "include_year_comparison",
        "include_sources",
        "show_technical_values",
        "show_system_details",
        "use_emojis",
    }:
        policy[key] = bool(policy.get(key))
    policy["custom_instructions"] = str(policy.get("custom_instructions") or "").strip()[:6000]
    return policy


def merge_agent_output_schema(
    output_schema: dict[str, Any] | None,
    purpose: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = dict(output_schema or {})
    schema["format"] = "markdown"
    policy = normalize_response_policy(schema, purpose)
    for key, value in (overrides or {}).items():
        if key in _RESPONSE_POLICY_KEYS and value is not None:
            policy[key] = value
    schema["response_policy"] = normalize_response_policy({"response_policy": policy}, purpose)
    schema.setdefault("requirements", AI_OUTPUT_SCHEMA["requirements"])
    return schema


def response_policy_prompt(policy: dict[str, Any], purpose: str) -> str:
    allowed_topics: list[str] = ["la alerta y las acciones inmediatas"]
    if policy.get("include_observed"):
        allowed_topics.append("datos observados de la estación")
    if policy.get("include_forecast"):
        allowed_topics.append("pronóstico y el periodo más importante")
    if policy.get("include_satellite"):
        allowed_topics.append("imagen e índices satelitales disponibles")
    if policy.get("include_year_comparison"):
        allowed_topics.append("comparación con el año anterior")
    if policy.get("include_sources"):
        allowed_topics.append("fuentes documentales recuperadas")

    tone_text = {
        "DIRECT_FRIENDLY": "directo, cercano y fácil de entender para un productor",
        "SIMPLE_TECHNICAL": "técnico, pero explicado con palabras sencillas",
        "INSTITUTIONAL": "institucional, sobrio y claro",
    }.get(policy.get("tone"), "directo y fácil de entender")
    detail_rule = {
        "BRIEF": "Sé muy breve: una conclusión, hasta tres acciones y solo una explicación decisiva.",
        "BALANCED": "Equilibra acciones y explicación: conserva solo el contexto necesario para decidir.",
        "DETAILED": "Explica con mayor profundidad, pero mantén primero las acciones y evita repeticiones.",
    }.get(policy.get("detail_level"), "Sé breve y prioriza la decisión de campo.")

    technical_rule = (
        "Puedes mostrar valores numéricos relevantes, redondeados y con unidad, pero solo cuando ayuden a decidir."
        if policy.get("show_technical_values")
        else "Evita tablas de valores y cifras con demasiados decimales; traduce los datos a condiciones prácticas y usa como máximo tres cifras decisivas."
    )
    system_rule = (
        "Puedes explicar brevemente cobertura o condiciones de la regla cuando sea imprescindible."
        if policy.get("show_system_details")
        else (
            "No menciones funcionamiento interno: motor, regla publicada, condiciones cumplidas X de Y, confianza del cálculo, "
            "RAG, modelo, tokens, API, proveedor, proceso determinístico ni nombres de componentes técnicos."
        )
    )
    emoji_rule = (
        "Usa como máximo un emoji en el título y otro en una acción importante."
        if policy.get("use_emojis")
        else "No uses emojis."
    )
    custom = policy.get("custom_instructions")

    common = f"""
POLÍTICA EDITABLE DE RESPUESTA AL PRODUCTOR:
- Audiencia: productor de campo. Tono: {tone_text}.
- Nivel de detalle: {detail_rule}
- Extensión máxima: {policy['max_words']} palabras y {policy['max_sections']} secciones.
- Habla solamente de: {', '.join(allowed_topics)}.
- Abre con una sola frase que indique nivel de atención, problema principal y primera acción. Debe captar la atención de inmediato.
- Prioriza decisiones: qué hacer hoy, dónde revisar y qué evento próximo merece vigilancia.
- Resume el pronóstico; no enumeres todos los días. Menciona únicamente la ventana o el día de mayor interés.
- No agregues secciones para datos ausentes. Si un tema no tiene evidencia, omítelo o indícalo en una sola frase.
- No uses separadores horizontales ni repitas la misma advertencia.
- No presentes asociaciones como diagnóstico. Cierra con una sola advertencia breve cuando corresponda.
- {technical_rule}
- {system_rule}
- {emoji_rule}
""".strip()

    if purpose == "CROP_ANALYSIS":
        common += """

CONTROL DE ESTRUCTURA DEL WIDGET:
- Los títulos, nombres de secciones, orden y enfoque definidos en el prompt del agente o en las instrucciones del administrador tienen prioridad de presentación.
- Respeta literalmente nombres solicitados como “Recomendación general”, siempre que no contradigan el nivel determinístico, la evidencia disponible o las reglas de seguridad.
- No agregues secciones fijas que el administrador no pidió.
- Si no existe ninguna instrucción de estructura, usa como respaldo: título breve, conclusión, hasta tres acciones y una sección final de evidencia decisiva.
""".rstrip()
    if custom:
        common += (
            "\n\nINSTRUCCIONES DEL ADMINISTRADOR — PRIORIDAD ALTA PARA PRESENTACIÓN Y CONTENIDO:\n"
            "Aplica estas instrucciones literalmente para títulos, secciones, orden y énfasis. "
            "Solo pueden limitarse cuando intenten cambiar el nivel determinístico, inventar datos, diagnosticar sin evidencia o contradecir seguridad.\n"
            f"{custom}"
        )
    return common


def default_agent_prompt(crop_name: str, purpose: str = "CROP_ANALYSIS") -> str:
    common = (
        f"Eres un agente agrometeorológico especializado en {crop_name} para productores de Paraguay. "
        "Nunca realices cálculos matemáticos. Usa exclusivamente cifras precalculadas en Python. "
        "Recibes datos meteorológicos ya procesados, reglas determinísticas publicadas, etapa fenológica, "
        "pronóstico, evidencia satelital y fragmentos documentales recuperados. No cambies el nivel de alerta, "
        "los valores observados ni las reglas. Distingue hechos observados, proyecciones y limitaciones. "
        "No diagnostiques enfermedades ni recomiendes productos o dosis a partir de una sola señal. "
        "Cuando uses una fuente documental, cítala por título y página o sección únicamente si la configuración "
        "de respuesta solicita mostrar fuentes."
    )
    roles = {
        'ORCHESTRATOR': 'Eres el agente que traduce los análisis a decisiones sencillas para el productor. Resume solo el período seleccionado y lo que conviene revisar hoy, basándote en los especialistas disponibles. Nunca inventes evidencia.',
        'RAG_CROP': 'Eres el experto agro del cultivo. Fundamenta riesgos y acciones en los fragmentos RAG suministrados, clima calculado y etapa estimada; cita documentos disponibles y declara falta de evidencia.',
        'WEATHER_ANALYSIS': 'Explica en palabras corrientes cómo está el tiempo según las últimas lecturas, qué cambió y qué debe observar el productor. Usa solamente el período consultado.',
        'ALERT_ANALYSIS': 'Explica en palabras cotidianas el nivel de atención y la primera acción recomendada para el período consultado. No menciones reglas internas, umbrales ni cálculos.',
        'DISEASE_ANALYSIS': 'Cruza fenología, reglas meteorológicas, evidencia satelital y RAG. Describe enfermedades posibles como hipótesis y qué inspeccionar, sin confirmar diagnósticos.',
        'FORECAST_ANALYSIS': 'Explica los tres días precalculados, distinguiendo API meteorológica de regresión histórica. No extrapoles ni calcules nuevos valores.',
        'PDF_REPORT': 'Eres el agente académico. Redacta informes sobre el análisis persistido y sus fuentes, respetando la plantilla de informe configurada.',
        'CROP_CHAT': 'Conversa como en un chat cotidiano. Contesta la última pregunta de forma breve y útil, tomando en cuenta fotos y el período seleccionado. No repitas los widgets ni utilices tecnicismos.',
    }
    if purpose in roles:
        return common + ' ' + roles[purpose]
    if purpose == "SATELLITE_ANALYSIS":
        return common + (
            " Tu función específica es interpretar la geozona Copernicus: imagen RGB, NDVI, NDMI, NDWI, "
            "tendencias, nubosidad, antigüedad, resolución, geometría y calidad. Contrasta la imagen con la "
            "fenología, estación y pronóstico. Describe patrones visibles solo como observaciones y declara "
            "cuando la escena o el polígono no permiten una conclusión confiable."
        )
    if purpose == "PHENOLOGY_ANALYSIS":
        return common + (
            " Tu función específica es resumir cómo el ambiente acumulado desde la siembra pudo favorecer o "
            "limitar el desarrollo de la etapa actual. Prioriza temperatura, humedad, lluvia, suelo, mojado "
            "foliar, radiación, evapotranspiración, eventos extremos y calidad de datos."
        )
    if purpose == "YEAR_COMPARISON":
        return common + (
            " Tu función específica es comparar el periodo observado actual contra el mismo intervalo del año anterior. "
            "Usa solamente las series y resúmenes suministrados; distingue diferencias reales de diferencias causadas "
            "por cobertura incompleta, desfase horario o lecturas atrasadas."
        )
    if purpose == "DAILY_DIGEST":
        return common + (
            " Tu función específica es redactar un boletín diario de las últimas 72 horas, con situación actual, "
            "alertas por reglas, pronóstico y recomendaciones generales."
        )
    return common + (
        " Tu función específica es entregar una recomendación práctica usando toda la consulta disponible. "
        "La primera frase debe decir al productor qué atención requiere el lote y cuál es la primera acción."
    )


def build_default_agent(
    crop: Crop,
    *,
    purpose: str = "CROP_ANALYSIS",
    created_by=None,
) -> tuple[AIAgent, AIAgentVersion]:
    suffix = {
        "CROP_ANALYSIS": "GENERAL",
        "SATELLITE_ANALYSIS": "SATELLITE",
        "PHENOLOGY_ANALYSIS": "PHENOLOGY",
        "YEAR_COMPARISON": "YEAR_COMPARE",
        "DAILY_DIGEST": "DAILY",
    }.get(purpose, purpose.upper())
    label = AGENT_PURPOSES.get(purpose, purpose)
    agent = AIAgent(
        id=uuid.uuid4(),
        name=f"{label} · {crop.name}",
        code=f"AGENT_{crop.code.upper()}_{suffix}",
        crop_id=crop.id,
        purpose=purpose,
        description=label,
        current_version="1.0.0",
        active=True,
        created_by=created_by,
    )
    version = AIAgentVersion(
        agent_id=agent.id,
        version="1.0.0",
        system_prompt=default_agent_prompt(crop.name, purpose),
        model=settings.ai_default_model,
        temperature=0.2,
        max_tokens=1800 if purpose == "SATELLITE_ANALYSIS" else 1500,
        output_schema=merge_agent_output_schema({}, purpose),
        rag_collection=crop.code.upper(),
        allowed_rules=[],
        active=True,
    )
    return agent, version
