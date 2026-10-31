"""Interpretation only. Input is a persisted, completed Python analysis."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIAgentRun, PlotAnalysis
from app.services.agent_defaults import default_agent_prompt
from app.services.agent_runtime import _knowledge_revision, select_agent_version
from app.services.ai import generate_markdown_response, provider_fallback_reason
from app.services.rag import retrieve_chunks
from app.services.technical_references import REFERENCE_POLICY, REFERENCE_REVISION, reference_fragments
from app.services.water_balance import water_summary, compact_water_context, stage_water_summary
from app.services.orchestrator_outputs import WIDGET_LABELS, permitted_widgets, render_widgets, fallback_widgets
from app.services.chat_context import widget_context, conversation_observations, irrigation_fallback
from app.services.widget_text import compact_widget_result
from app.services.parcel_language import PARCEL_LANGUAGE_REVISION, parcel_language_instruction, producer_voice_instruction
from app.services.parcel_guidance import (
    field_review_candidate, guidance_response_contract, parse_guidance_response, satellite_plain_evidence,
)

PARCEL_PURPOSES = {
    "ORCHESTRATOR",
    "CROP_ANALYSIS",
    "RAG_CROP",
    "PDF_REPORT",
    "WEATHER_ANALYSIS",
    "ALERT_ANALYSIS",
    "PHENOLOGY_ANALYSIS",
    "SATELLITE_ANALYSIS",
    "DISEASE_ANALYSIS",
    "FORECAST_ANALYSIS",
    "CROP_CHAT",
}
QUESTIONS = {
    "ORCHESTRATOR": "Explica qué está pasando en esta parcela, qué aporta la imagen seleccionada y qué hacer ahora. Relaciona las evidencias y prioriza decisiones concretas; sugiere visitar una ubicación solo si hay un motivo verificable.",
    "CROP_ANALYSIS": "Explica la situación de esta parcela y las acciones sugeridas para hoy.",
    "RAG_CROP": "Cruza clima, alertas calculadas, fenología, satélite y documentos del cultivo. Explica riesgos y posibles enfermedades como hipótesis y prioriza qué revisar en campo.",
    "PDF_REPORT": "Redacta el informe de esta parcela usando los resultados y las fuentes disponibles.",
    "WEATHER_ANALYSIS": "Explica qué espera al productor mañana, pasado mañana y el tercer día usando forecast combinado ya calculado. Habla de calor, lluvia, viento y humedad en lenguaje cotidiano. Menciona solo cifras útiles y una labor que convenga anticipar o posponer. No digas OLS, cb, RMSE ni regresión. Las previsiones no son certezas; no calcules cifras nuevas.",
    "ALERT_ANALYSIS": "Explica únicamente las señales de alerta del período elegido por el productor.",
    "PHENOLOGY_ANALYSIS": "Explica la etapa actual conectando lluvia requerida y registrada, suelo e imagen seleccionada con las evidencias disponibles. Indica reducción potencial bajo estrés solo si hay un porcentaje documentado para esta etapa y termina con una acción de campo concreta.",
    "SATELLITE_ANALYSIS": (
        "Interpreta Sentinel-1 y Sentinel-2 y el punto de revisión precalculado; no inventes coordenadas."
    ),
    "DISEASE_ANALYSIS": (
        "Cruza riesgos precalculados, fenología, satélite y RAG. "
        "Sugiere posibles enfermedades como hipótesis, cita evidencia y aclara datos faltantes."
    ),
    "FORECAST_ANALYSIS": (
        "Explica los próximos tres días con forecast combinado ya calculado, en palabras sencillas y una recomendación práctica; no calcules cifras ni presentes estimaciones como mediciones."
    ),
}


def _format_number(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "no disponible"


def deterministic_summary(
    context: dict, purpose: str = "ORCHESTRATOR", *, question: str | None = None,
    view_mode: str = "producer",
) -> str:
    """Honest, contextual fallback. Never infers an unmeasured condition or a disease.

    A failed/missing LLM must not cause the chat to repeat the same three lines
    regardless of the question. Use only facts already persisted in the analysis.
    """
    technical = view_mode == "technical"
    window = (context.get("windows") or {}).get(str(context.get("selected_window_hours", 72))) or {}
    stations = window.get("stations") or []
    level = window.get("level")
    raw = context.get("raw_readings") or []
    forecast = (context.get("forecast") or {}).get("days") or []
    phenology = context.get("phenology") or {}
    satellite = context.get("satellite") or {}
    radar = context.get("radar") or {}
    question_lower = (question or "").lower()
    if purpose == "PHENOLOGY_ANALYSIS" and context.get('water_balance'):
        return stage_water_summary(context['water_balance'], context=context)
    if context.get('water_balance') and any(term in question_lower for term in (
        'agua', 'requerimiento', 'déficit', 'deficit', 'pérdida', 'perdida', 'daño', 'riego',
    )):
        return water_summary(context['water_balance'])
    if purpose == "CROP_CHAT" and any(term in question_lower for term in (
        "suelo", "riego", "regar", "regando", "seco", "seca", "tensión", "tension",
    )):
        return irrigation_fallback(context)
    conditions = [
        {**condition, "station": station.get("station_id")}
        for station in stations
        for condition in (station.get("alert") or {}).get("conditions") or []
    ]
    met = list(dict.fromkeys(str(c.get("label")) for c in conditions if c.get("status") == "met"))
    missing = list(dict.fromkeys(str(c.get("label")) for c in conditions if c.get("status") == "missing"))
    issues = [item for station in stations for item in (station.get("data_issues") or [])]
    stage = phenology.get("stage_name") or "no determinada"
    reading = next((r for r in raw if r.get("reading")), None)

    level_message = {
        "NINGUNA": "Las condiciones evaluadas no activaron una alerta. Esto no descarta otros problemas en el cultivo.",
        "MODERADA": "**Alerta moderada** según las condiciones evaluadas.",
        "GRAVE": "**Alerta grave**: priorice una revisión en campo.",
        "EVALUACION_PARCIAL": "**Señales disponibles:** la estación permite evaluar parte de las condiciones. Use esas señales para orientar el monitoreo; las condiciones sin medir podrían cambiar el nivel de riesgo.",
        "DATOS_INSUFICIENTES": "**Riesgo aún sin clasificar**: faltan mediciones para determinar si es normal, moderado o grave.",
    }.get(level, "**Riesgo sin evaluar**: no hay una clasificación disponible.")

    def measurement() -> str:
        if not reading:
            return "No hay una lectura de estación disponible."
        data = reading["reading"]
        fields = [("temperature_c", "temperatura", "°C"), ("humidity_pct", "humedad del aire", "%"),
                  ("rain_mm", "lluvia", "mm"), ("soil_moisture_pct", "sensor de suelo", "")]
        observations = []
        for key, name, unit in fields:
            value = data.get(key)
            if value is not None:
                if key == "soil_moisture_pct":
                    unit = (reading.get("sensor_units") or {}).get(key) or "%"
                    name = "tensión del suelo" if unit.lower() in {"cb", "kpa"} else "humedad del suelo"
                observations.append(f"{name} {_format_number(value)} {unit}".strip())
        source = reading.get("name", "estación")
        return f"Última lectura de {source}: " + (", ".join(observations) if observations else "sin mediciones meteorológicas compatibles") + "."

    def forecast_message() -> str:
        days = [d for d in forecast if d.get("precipitation_mm") is not None]
        if not days:
            return "No hay pronóstico de lluvia disponible para esta parcela."
        parts = [f"{d['date']}: {_format_number(d['precipitation_mm'])} mm" for d in days[:3]]
        return "Lluvia prevista (no medida): " + "; ".join(parts) + "."

    def alert_evidence() -> list[str]:
        parts = []
        if met:
            parts.append("**Condiciones cumplidas:** " + ", ".join(met[:6]) + ".")
        else:
            parts.append("No se observaron condiciones de alerta cumplidas con los datos evaluables.")
        if missing:
            parts.append("**Sin evaluar por falta de dato:** " + ", ".join(missing[:6]) + ".")
        adaptations = [item for st in stations for item in st.get("condition_adaptations", [])]
        if adaptations:
            parts.append("**Suelo medido en cb:** se evaluó una condición tensiométrica orientativa; no equivale a convertir cb en % y no valida reglas de enfermedades definidas en porcentajes. El umbral puede revisarse en Administración > Estaciones.")
        if level == "DATOS_INSUFICIENTES" and not missing:
            parts.append("Hay cobertura o muestras insuficientes para clasificar el riesgo.")
        if technical and issues:
            part = issues[0]
            parts.append("**Calidad de datos:** " + str(part.get("message") or "Revise la sincronización") +
                         (" Acción: " + str(part["action"]) if part.get("action") else ""))
        return parts

    def satellite_evidence() -> list[str]:
        if not satellite and not radar:
            return ["Todavía no hay una imagen satelital procesada y válida para este polígono. Sin imagen no es posible localizar diferencias de vegetación."]
        lines = []
        if satellite:
            ndvi = satellite.get("ndvi")
            if ndvi is None:
                lines.append("La escena Sentinel-2 no tiene un NDVI válido para esta parcela.")
            else:
                value = float(ndvi)
                if value < 0.2:
                    signal = "baja"
                elif value < 0.6:
                    signal = "intermedia"
                else:
                    signal = "alta"
                lines.append(f"La escena Sentinel-2 muestra una señal de vegetación {signal} en el índice NDVI. "
                             "Esto sirve para localizar diferencias dentro del terreno; no permite diagnosticar una enfermedad por sí solo. "
                             "El resultado debe compararse con la etapa del cultivo, la cobertura de suelo y otras fechas.")
            point = context.get("inspection_point") or {}
            if point.get("method") == "lowest_valid_ndvi_pixel":
                lines.append("**Dónde revisar:** el punto marcado corresponde a uno de los valores de vigor más bajos de la imagen; compruébelo en campo.")
            else:
                lines.append("**Dónde revisar:** el punto marcado es una referencia dentro del terreno, no una anomalía confirmada.")
            if technical:
                indices = ", ".join(f"{key.upper()} {_format_number(satellite[key], 4)}" for key in ("ndvi", "ndmi", "ndwi") if satellite.get(key) is not None)
                if indices:
                    lines.append("**Valores Sentinel-2:** " + indices + ".")
                if satellite.get("captured_at"):
                    lines.append("Captura Sentinel-2: " + str(satellite["captured_at"]) + ".")
        if radar and technical:
            lines.append("Sentinel-1 radar: VV " + _format_number(radar.get("vv_mean"), 5) +
                         "; VH " + _format_number(radar.get("vh_mean"), 5) + ". "
                         "Estos coeficientes son retrodispersión; no indican directamente la salud del cultivo.")
        return lines

    if purpose == "SATELLITE_ANALYSIS" or any(term in question_lower for term in ("satélite", "satelite", "imagen", "ndvi", "ndmi", "radar", "vigor")):
        output = ["**Qué muestra la imagen de su parcela**", *satellite_evidence(),
                  "**Qué hacer:** compare las zonas de distinto aspecto en la imagen con lo que observa sobre el terreno."]
        if technical:
            output.append("Compare fechas, píxeles válidos, nubosidad y etapa fenológica antes de atribuir causas al índice.")
        return "\n\n".join(output)

    if purpose in {"WEATHER_ANALYSIS", "FORECAST_ANALYSIS"} or any(term in question_lower for term in ("lluvia", "clima", "temperatura", "pronóstico", "pronostico", "llover", "lloverá", "llovera", "humedad del aire")):
        return "\n\n".join(["**Qué se espera**", forecast_message(),
            "**Cómo prepararse**", "Revise el pronóstico antes de organizar las labores al aire libre y compruebe cómo está la tierra en su parcela. La cantidad de lluvia puede cambiar."])

    if any(term in question_lower for term in ("suelo", "riego", "seco", "tensión", "tension")):
        tension = next((r.get("soil_tension") for r in raw if r.get("soil_tension")), None)
        text = (f"El suelo se encuentra en una condición {str(tension.get('label') or 'que necesita revisión').lower()}. "
                + "Compruebe la humedad directamente en su terreno. "
                + (f"El sensor registró {_format_number(tension.get('value'))} {tension.get('unit', '')}. " if technical else "")
                if tension else "Aún no hay una medición confiable de la humedad del suelo.")
        return "\n\n".join(["**Agua en el suelo**", text, measurement(), "Compare la lectura con una referencia calibrada para el suelo y el cultivo.", *alert_evidence(),
                             "**Qué hacer:** compruebe la humedad del suelo en campo y revise que la regla use la misma unidad que el sensor antes de decidir el riego."])

    output = ["**Cómo está su parcela**", level_message, *alert_evidence()]
    if context.get('water_balance'):
        output.append(water_summary(context['water_balance']))
    if purpose != "RAG_CROP":
        output.extend([f"Etapa del cultivo estimada por fecha de siembra: **{stage}** (confirmar en campo).", measurement(), forecast_message()])
    if level == "DATOS_INSUFICIENTES":
        output.append("**Próximo paso:** revise primero las mediciones necesarias en Calidad de datos; mientras tanto puede examinar el cultivo y sus puntos de inspección, sin dar por normal el riesgo.")
    elif level == "GRAVE":
        output.append("**Próximo paso:** inspeccione hoy la parcela y consulte con el técnico antes de elegir un tratamiento.")
    elif level == "MODERADA":
        output.append("**Próximo paso:** revise el cultivo, observe señales visibles y continúe vigilando las condiciones.")
    else:
        output.append("**Próximo paso:** continúe el monitoreo y revise de nuevo ante cambios del clima o el cultivo.")
    if question:
        output.insert(0, "**Respuesta según los datos disponibles**")
    if purpose == "ORCHESTRATOR":
        output.extend(["**Qué aporta la imagen**", satellite_plain_evidence(context)])
    return "\n\n".join(output)


async def interpret_plot(
    db: AsyncSession,
    analysis: PlotAnalysis,
    purpose: str = "ORCHESTRATOR",
    *,
    question: str | None = None,
    history: list[dict] | None = None,
    images: list[str] | None = None,
    current_image_count: int = 0,
    force: bool = False,
    view_mode: str = "producer",
    window_hours: int = 72,
) -> dict[str, Any]:
    if view_mode not in {"producer", "technical"}:
        raise ValueError("Modo de interpretación no soportado")
    if purpose not in PARCEL_PURPOSES:
        raise ValueError("Propósito de agente no soportado para parcelas")
    if window_hours not in (60, 72, 96, 120):
        raise ValueError("Período de análisis no soportado")
    # Serialize updates to agent_results so simultaneous widget calls cannot
    # overwrite one another. The snapshot's deterministic context never changes.
    await db.refresh(analysis, with_for_update=True)
    if str(window_hours) not in (analysis.context.get("windows") or {}):
        raise ValueError("Ese período no fue calculado. Selecciónelo y vuelva a analizar la parcela.")
    crop_id = UUID(analysis.context["crop"]["id"])
    agent, version = await select_agent_version(db, crop_id, purpose)
    allowed = permitted_widgets(version.output_schema if version else None)
    restricted = purpose == "ORCHESTRATOR" and set(allowed) != set(WIDGET_LABELS)
    if restricted and not allowed:
        result = {"purpose": purpose, "markdown": "La síntesis del orquestador está desactivada en su configuración.",
                  "status": "disabled", "allowed_widgets": [], "view_mode": view_mode,
                  "window_hours": window_hours, "sources": []}
        analysis.agent_results = {**(analysis.agent_results or {}), purpose: result}
        await db.flush()
        return result
    rag_agent, _ = await select_agent_version(db, crop_id, "RAG_CROP")
    revision = await _knowledge_revision(
        db, crop_id, rag_agent.id if rag_agent else (agent.id if agent else None)
    )
    existing = (analysis.agent_results or {}).get(purpose)
    if not force and not question and existing and existing.get("status") == "completed" and version:
        if (
            existing.get("version_id") == str(version.id)
            and existing.get("version_updated_at") == str(version.updated_at)
            and existing.get("knowledge_revision") == str(revision)
            and existing.get("view_mode", "producer") == view_mode
            and existing.get("window_hours") == window_hours
            and existing.get("language_revision") == PARCEL_LANGUAGE_REVISION
            and existing.get("reference_revision") == REFERENCE_REVISION
            and (purpose != "PHENOLOGY_ANALYSIS" or existing.get("water_criterion_revision") == "stage-water-v5")
        ):
            return compact_widget_result(existing, purpose, limit=2200 if purpose == "CROP_CHAT" else 1500, preserve_format=True)
    context = dict(analysis.context)
    if context.get('water_balance'):
        context['water_balance'] = compact_water_context(context['water_balance'])
    # La IA jamás recibe ventanas diferentes a la elegida: evita comparaciones
    # involuntarias, incluso cuando un prompt persistido en CMS las solicite.
    context["windows"] = {str(window_hours): (context.get("windows") or {}).get(str(window_hours), {})}
    context["selected_window_hours"] = window_hours
    if purpose == "CROP_CHAT":
        context["chat_requested_at"] = datetime.now(UTC).isoformat()
    context["temporal_scope"] = {
        "water_balance": "Historia desde siembra hasta reference_at; independiente de la ventana de alertas. No se presentan sus totales como lluvia de la ventana seleccionada.",
        "observations": {"hours": window_hours, "ends_at": analysis.reference_at.isoformat()},
        "forecast": {"horizon_days": 3, "dates": [day["date"] for day in (context.get("forecast") or {}).get("days", [])]},
        "regression": "Tendencia precalculada con días históricos completos, independiente del período de alertas; forecast contiene su combinación prudente con Open-Meteo por estación. Explica forecast, sin recalcular ni volver a mezclar cifras.",
    }
    context["possible_diseases"] = [
        item for item in (context.get("possible_diseases") or [])
        if item.get("hours") == window_hours
    ]
    context["plot"] = {key: value for key, value in context.get("plot", {}).items() if key not in {"producer_document", "browser_session_id", "geometry_key"}}
    try:
        chunks = await retrieve_chunks(
            db,
            " ".join(
                [
                    context["crop"]["name"],
                    context["phenology"].get("stage_name", ""),
                    question or QUESTIONS.get(purpose, purpose),
                ]
            ),
            crop_id,
            allow_embedding=not (version and version.provider == "EDGE"),
            agent_id=rag_agent.id if rag_agent else (agent.id if agent else None),
        )
        context["rag_status"] = "AVAILABLE" if chunks else "NO_EVIDENCE"
    except Exception:
        chunks = []
        context["rag_status"] = "UNAVAILABLE"
    chunks = reference_fragments(context['crop'].get('code', ''),
        ' '.join([context['phenology'].get('stage_code') or '', question or QUESTIONS.get(purpose, purpose)]), limit=6) + chunks
    context['reference_policy'] = REFERENCE_POLICY
    context['reference_revision'] = REFERENCE_REVISION
    if chunks:
        context['rag_status'] = 'AVAILABLE'
    context["retrieved_fragments"] = chunks
    context["sources"] = [
        {"document": row["document"], "page_or_section": row.get("page_or_section")} for row in chunks
    ]
    context["agent_purpose"] = purpose
    context["view_mode"] = view_mode
    context["allowed_widgets"] = allowed
    context["previous_widget_interpretations"] = widget_context(analysis.agent_results or {}, window_hours)
    context["previous_widget_interpretations"].pop(purpose, None)
    if purpose == "ORCHESTRATOR":
        selected_window = context["windows"][str(window_hours)]
        context["parcel_fact_brief"] = {
            "alert": {
                "NINGUNA": "Las condiciones evaluadas no activaron una alerta; esto no descarta otros problemas.",
                "MODERADA": "Se activó una alerta moderada según las condiciones evaluadas.",
                "GRAVE": "Se activó una alerta grave según las condiciones evaluadas.",
                "EVALUACION_PARCIAL": "Hay señales observadas útiles; las condiciones que la estación no reporta podrían cambiar la clasificación.",
            }.get(selected_window.get("level"), "No se puede clasificar el riesgo con los datos disponibles; no equivale a ausencia de alerta."),
            "satellite": satellite_plain_evidence(context),
        }
        context["field_review_candidate"] = field_review_candidate(context)
        context.pop("inspection_point", None)
    if purpose in {"ORCHESTRATOR", "PHENOLOGY_ANALYSIS"}:
        context["satellite_fact_brief"] = satellite_plain_evidence(context)
        satellite_result = (analysis.agent_results or {}).get("SATELLITE_ANALYSIS") or {}
        context["satellite_evidence_basis"] = {
            "specialist_viewed_image": satellite_result.get("status") == "completed"
            and satellite_result.get("window_hours") == window_hours
            and satellite_result.get("view_mode", "producer") == view_mode
            and bool(satellite_result.get("images_processed")),
            "policy": "Los indicadores y promedios no describen lesiones ni localizan daños por sí solos.",
        }
    if purpose in {"WEATHER_ANALYSIS", "FORECAST_ANALYSIS"}:
        context.pop("historical_regression", None)
        context["prediction_contract"] = "Usa forecast combinado ya calculado. No combines cifras de nuevo ni infieras unidades de sensores. La ausencia de sensor no equivale a cero."
    if purpose == "PHENOLOGY_ANALYSIS":
        context.pop("forecast", None)
        context.pop("historical_regression", None)
        context["temporal_scope"].pop("forecast", None)
        context["temporal_scope"].pop("regression", None)
        context["previous_widget_interpretations"] = {
            key: value for key, value in context["previous_widget_interpretations"].items()
            if key in {"SATELLITE_ANALYSIS", "RAG_CROP"}
        }
    if purpose == "CROP_CHAT":
        context["conversation_observations"] = conversation_observations(history)
        context["photo_context"] = {
            "attached_to_current_message": current_image_count,
            "images_sent_to_model": len(images or []),
            "order": "historical photos first, current-message photos last",
            "scope": "User photographs; species and location are not established by the registered crop.",
        }
    sources = [{"document": row["document"], "page_or_section": row.get("page_or_section")} for row in chunks]
    started = datetime.now(UTC)
    result = {
        "markdown": deterministic_summary(context, purpose, question=question, view_mode=view_mode),
        "status": "fallback",
        "sources": sources,
        "purpose": purpose,
        "view_mode": view_mode,
        "window_hours": window_hours,
        "fallback_reason": "agent_not_configured",
        "version_id": str(version.id) if version else None,
        "version_updated_at": str(version.updated_at) if version else None,
        "images_processed": False,
        "text_complete": True,
        "knowledge_revision": str(revision),
        "language_revision": PARCEL_LANGUAGE_REVISION,
        "reference_revision": REFERENCE_REVISION,
        **({"water_criterion_revision": "stage-water-v5"} if purpose == "PHENOLOGY_ANALYSIS" else {}),
        "allowed_widgets": context["allowed_widgets"],
    }
    if purpose == "CROP_CHAT" and images:
        result["markdown"] = "Recibí tu fotografía, pero no pude analizarla visualmente en este momento. El cultivo registrado en la parcela no permite identificar la planta de la foto. Puedes describirme qué observas o volver a intentar el análisis de la imagen."
    if restricted:
        result["widgets"] = fallback_widgets(context, allowed)
        result["markdown"] = render_widgets(result["widgets"])
    usage = {}
    if version:
        try:
            if purpose == "SATELLITE_ANALYSIS" and context.get("satellite"):
                import base64

                from app.db.models import SatelliteObservation
                from app.services.satellite import local_satellite_image_path

                observation = await db.get(SatelliteObservation, UUID(context["satellite"]["id"]))
                path = local_satellite_image_path(observation) if observation else None
                if path and path.stat().st_size <= 8 * 1024 * 1024:
                    images = [f"data:image/jpeg;base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"]
            generated = await generate_markdown_response(
                (version.system_prompt or default_agent_prompt(context["crop"]["name"], purpose))
                + ("\nResponde en ESPAÑOL CLARO PARA PRODUCTORES. No comiences con listas de siglas, coeficientes o coordenadas. "
                   "Empieza por lo que ocurre y termina con una acción concreta. "
                   "Explica los datos incompletos sin sustituir toda la respuesta por Datos insuficientes. "
                   "Si hay lecturas reales, úsalas y nombra la estación. Si hay clasificación incompleta, NO inventes normalidad ni gravedad. "
                   "Describe índices Sentinel como evidencia orientativa, no como diagnóstico ni certeza. "
                   "No inventes valores ni plagas. Contesta la PREGUNTA CONCRETA del usuario en chat. "
                   "Explica solo lo necesario para decidir qué revisar. "
                   if view_mode == "producer" and purpose != "PDF_REPORT" else
                   "\nResponde en ESPAÑOL PARA TÉCNICOS, empezando con conclusión, condiciones verificadas, faltantes y acción. "
                   "Puedes usar valores, unidades, índices, fechas y limitaciones (no inventarlos). "
                   "Aporta una interpretación agronómica explicada, nunca solo un volcado de datos. "
                   "Responde la pregunta concreta. Máximo 300 palabras. " if purpose != "PDF_REPORT" else "")
                + ("\nALCANCE W3: usa el historial de agua de la etapa actual. No añadas pronóstico ni comparación histórica del tiempo; corresponden al widget 5."
                   if purpose == "PHENOLOGY_ANALYSIS" else
                   "\nALCANCE TEMPORAL: las observaciones corresponden a las últimas " + str(window_hours)
                   + " horas del análisis; el pronóstico a los próximos tres días. No mezcles lluvia registrada con prevista. "
                   "El historial de agua desde la siembra es independiente de esa ventana. Usa solo cifras ya calculadas. "
                   "Compara estimaciones del tiempo solo en el widget de predicción y si cambia una decisión, sin explicar sus métodos.")
                + '\n' + REFERENCE_POLICY
                + producer_voice_instruction(view_mode, purpose)
                + parcel_language_instruction(view_mode, purpose)
                + (guidance_response_contract(allowed if restricted else None) if purpose == "ORCHESTRATOR" else ""),
                context,
                question or QUESTIONS.get(purpose, QUESTIONS["ORCHESTRATOR"]),
                version.model,
                history=history,
                image_data_urls=images,
                temperature=version.temperature,
                max_tokens=version.max_tokens,
                # Length is controlled by the writing prompt, never by slicing
                # a completed explanation or dropping its final sections.
                response_policy=None,
                provider=version.provider or "OPENAI",
                preserve_structured_output=purpose == "ORCHESTRATOR",
            )
            usage = generated.get("usage") or {}
            if not generated.get("fallback"):
                if purpose == "ORCHESTRATOR":
                    guidance = parse_guidance_response(generated["markdown"], context, allowed if restricted else None)
                    result.update(guidance)
                    generated["markdown"] = guidance["markdown"]
                result.update(
                    markdown=generated["markdown"],
                    model=generated.get("model") or version.model,
                    status="completed",
                    fallback_reason=None,
                    images_processed=bool(images),
                )
            else:
                result["fallback_reason"] = "provider_not_configured"
        except Exception as exc:
            usage = getattr(exc, "usage", {})
            result["fallback_reason"] = provider_fallback_reason(exc)
            result["provider_status"] = getattr(exc, "status_code", None)
    full_markdown = result["markdown"]
    result = compact_widget_result(result, purpose, limit=2200 if purpose == "CROP_CHAT" else 1500, preserve_format=True)
    run = AIAgentRun(
        agent_id=agent.id if agent else None,
        agent_version_id=version.id if version else None,
        status=result["status"].upper(),
        model=version.model if version else None,
        input_payload={
            "plot_analysis_id": str(analysis.id),
            "purpose": purpose,
            "question": question,
            "image_count": len(images or []),
            "context": context,
        },
        output_payload={**result, "full_markdown": full_markdown, "usage": usage},
        error_message=result["fallback_reason"] if result["status"] == "fallback" else None,
        duration_ms=int((datetime.now(UTC)-started).total_seconds()*1000),
        sources=sources,
        started_at=started,
        finished_at=datetime.now(UTC),
    )
    db.add(run)
    if not question:
        analysis.agent_results = {**(analysis.agent_results or {}), purpose: result}
    await db.flush()
    return result
