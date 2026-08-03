from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.core.config import settings
from app.db.models import (
    AIAgent,
    AIAgentRun,
    AIAgentVersion,
    AlertEvaluation,
    Crop,
    AgentKnowledgeDocument,
    KnowledgeDocument,
    Station,
)
from app.services.ai import generate_markdown_response, grounded_fallback_markdown
from app.services.analysis_context import build_complete_analysis_context
from app.services.agent_defaults import normalize_response_policy, response_policy_prompt
from app.services.rag import retrieve_chunks
from app.services.satellite import latest_satellite_observation, local_satellite_image_path
from app.services.year_comparison import build_year_comparison

SATELLITE_CONTEXT_POLICY = """
Política obligatoria para Copernicus:
- Distingue estación puntual, pronóstico y observación espacial.
- Explica NDVI, NDMI, NDWI, tendencias, nubosidad, antigüedad, resolución y geometría.
- Usa la imagen únicamente como evidencia visual complementaria.
- No diagnostiques una enfermedad, plaga, deficiencia o causa por un índice o una imagen aislados.
- Reduce el peso del análisis cuando la escena sea antigua, nubosa o la geozona sea aproximada.
""".strip()

RUNTIME_PROTOCOL_VERSION = "markdown-v4-admin-structure"
logger = structlog.get_logger(__name__)


PURPOSE_QUESTIONS = {
    "CROP_ANALYSIS": (
        "Entrega una recomendación práctica para el productor sin cambiar el nivel determinístico. "
        "Respeta primero los títulos, nombres de secciones, orden y temas definidos en el prompt del agente y en "
        "las instrucciones editables del administrador. Si no existe una estructura específica, abre con el nivel "
        "de atención y la primera acción, resume qué revisar hoy y menciona solo el evento próximo que cambie una decisión."
    ),
    "SATELLITE_ANALYSIS": (
        "Analiza exclusivamente el aporte de Copernicus a esta consulta. Describe los índices y tendencias, "
        "comenta lo observable en la imagen satelital cuando esté disponible, relaciónalo con la etapa y el "
        "clima, y entrega recomendaciones de monitoreo de la geozona."
    ),
    "PHENOLOGY_ANALYSIS": (
        "Resume de forma breve y muy clara cómo las condiciones ambientales desde la fecha de siembra pudieron "
        "afectar el desarrollo hasta la etapa actual. Señala periodos favorables, limitantes, datos faltantes y "
        "qué debe revisar el productor en campo."
    ),
    "YEAR_COMPARISON": (
        "Compara el periodo actual con el mismo intervalo del año anterior. Explica diferencias de temperatura, "
        "humedad, lluvia, suelo y evapotranspiración, distingue baja cobertura de cambios reales y relaciona los "
        "contrastes con la etapa y los riesgos del cultivo sin inventar causalidad."
    ),
    "DAILY_DIGEST": (
        "Redacta un boletín para productores sobre las últimas 72 horas: situación observada, alerta según reglas, "
        "pronóstico, enfermedades que ameritan vigilancia, recomendaciones generales y limitaciones."
    ),
}


def _effective_prompt(system_prompt: str, purpose: str, response_policy: dict[str, Any]) -> str:
    extra = SATELLITE_CONTEXT_POLICY if purpose in {"CROP_ANALYSIS", "SATELLITE_ANALYSIS", "DAILY_DIGEST"} else ""
    grounding_protocol = """
PROTOCOLO DE FUNDAMENTACIÓN OBLIGATORIO:
- Usa exclusivamente el contexto estructurado suministrado: estación, fechas, lecturas, cálculos de librerías, reglas publicadas, fenología, pronóstico, Copernicus y fragmentos documentales recuperados.
- No inventes valores, fuentes, síntomas, fechas ni causas. Un dato ausente debe declararse como ausente.
- Identifica explícitamente qué es observado, calculado, pronosticado, satelital o documental.
- Si la lectura está atrasada, indica su fecha y antigüedad; nunca la presentes como actual.
- El nivel determinístico y sus condiciones no pueden ser cambiados por el agente.
- Una asociación o índice espectral no prueba causalidad ni diagnostica enfermedad. Formula hipótesis de monitoreo y sus limitaciones.
- Toda recomendación debe poder rastrearse a una evidencia incluida en el contexto.
""".strip()
    output_protocol = """
PROTOCOLO DE SALIDA PRIORITARIO:
- Devuelve únicamente una respuesta final en Markdown legible para productores.
- No devuelvas JSON, objetos, claves entre comillas, bloques ```json ni esquemas de datos.
- Usa títulos, párrafos, listas y emojis moderados cuando aporten claridad.
- Esta instrucción reemplaza cualquier requisito anterior del prompt que solicite JSON o una estructura serializada.
""".strip()
    presentation = response_policy_prompt(response_policy, purpose)
    return "\n\n".join(
        part for part in [system_prompt.strip(), grounding_protocol, extra, presentation, output_protocol] if part
    ).strip()


def _apply_response_scope(context: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    scoped = dict(context)
    if not policy.get("include_observed"):
        scoped.pop("observed_series_summary", None)
    if not policy.get("include_forecast"):
        scoped.pop("forecast", None)
        scoped.pop("forecast_risk", None)
    if not policy.get("include_satellite"):
        scoped.pop("satellite", None)
        scoped.pop("satellite_context_status", None)
    if not policy.get("include_year_comparison"):
        scoped.pop("year_comparison", None)
    if not policy.get("show_system_details"):
        deterministic = dict(scoped.get("deterministic_result") or {})
        for key in ("conditions_met", "conditions_total", "completion_percentage", "confidence"):
            deterministic.pop(key, None)
        scoped["deterministic_result"] = deterministic
        scoped.pop("rule_version", None)
    scoped["response_policy"] = policy
    return scoped


async def select_agent_version(
    db: AsyncSession,
    crop_id: UUID | None,
    purpose: str,
    *,
    fallback_purpose: str | None = None,
) -> tuple[AIAgent | None, AIAgentVersion | None]:
    purposes = [purpose]
    if fallback_purpose and fallback_purpose not in purposes:
        purposes.append(fallback_purpose)
    for current_purpose in purposes:
        scopes: list[UUID | None] = [crop_id]
        # El boletín puede configurarse por cultivo o, como respaldo, con un
        # único agente general sin cultivo desde el CMS.
        if current_purpose == "DAILY_DIGEST" and crop_id is not None:
            scopes.append(None)
        for current_crop_id in scopes:
            query = select(AIAgent).where(
                AIAgent.purpose == current_purpose,
                AIAgent.active.is_(True),
                AIAgent.deleted_at.is_(None),
            )
            if current_crop_id is not None:
                query = query.where(AIAgent.crop_id == current_crop_id)
            else:
                query = query.where(AIAgent.crop_id.is_(None))
            agent = await db.scalar(query.order_by(AIAgent.updated_at.desc()).limit(1))
            if not agent:
                continue
            version = await db.scalar(
                select(AIAgentVersion)
                .where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True))
                .order_by(AIAgentVersion.created_at.desc())
                .limit(1)
            )
            if version:
                return agent, version
    return None, None


async def _context_with_rag(
    db: AsyncSession,
    alert: AlertEvaluation,
    context: dict[str, Any],
    *,
    extra_query: str | None = None,
    agent_id: UUID | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    crop = await db.get(Crop, alert.crop_id)
    query = " ".join(
        part
        for part in [
            crop.name if crop else "",
            alert.stage_code or "",
            alert.explanation or "",
            ", ".join(item.get("name", "") for item in (alert.diseases or [])),
            extra_query or "",
        ]
        if part
    )
    try:
        chunks = await retrieve_chunks(db, query, alert.crop_id, limit=6, agent_id=agent_id)
        retrieval_status = {"status": "COMPLETED", "fragments": len(chunks)}
    except Exception as exc:
        # Documentary enrichment is optional evidence. A missing index, a
        # pending migration or a provider/vector issue must never suppress the
        # deterministic weather analysis.
        logger.warning("agent_rag_retrieval_failed", error=str(exc)[:1000])
        chunks = []
        retrieval_status = {"status": "FAILED", "error": str(exc)[:1000], "fragments": 0}
    sources = [
        {"documento": chunk["document"], "pagina_o_seccion": chunk["page_or_section"]}
        for chunk in chunks
    ]
    return {
        **context,
        "sources": sources,
        "retrieved_fragments": chunks,
        "knowledge_retrieval": retrieval_status,
    }, sources


async def _knowledge_revision(
    db: AsyncSession,
    crop_id: UUID,
    agent_id: UUID | None = None,
) -> Any:
    if agent_id:
        return await db.scalar(
            select(func.max(KnowledgeDocument.updated_at))
            .join(
                AgentKnowledgeDocument,
                AgentKnowledgeDocument.document_id == KnowledgeDocument.id,
            )
            .where(
                AgentKnowledgeDocument.agent_id == agent_id,
                KnowledgeDocument.active.is_(True),
                KnowledgeDocument.deleted_at.is_(None),
            )
        )
    return await db.scalar(
        select(func.max(KnowledgeDocument.updated_at)).where(
            KnowledgeDocument.crop_id == crop_id,
            KnowledgeDocument.active.is_(True),
            KnowledgeDocument.deleted_at.is_(None),
        )
    )


def _signature(version: AIAgentVersion, knowledge_revision: Any, context: dict[str, Any], purpose: str) -> str:
    clean = {key: value for key, value in context.items() if key != "context_generated_at"}
    payload = {
        "purpose": purpose,
        "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
        "version_id": str(version.id),
        "version_updated_at": version.updated_at.isoformat() if version.updated_at else None,
        "knowledge_revision": knowledge_revision.isoformat() if knowledge_revision else None,
        "context": clean,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


async def _image_data_url_for_alert(db: AsyncSession, alert: AlertEvaluation) -> str | None:
    _, observation = await latest_satellite_observation(db, alert.station_id, alert.crop_id)
    if not observation:
        return None
    path = local_satellite_image_path(observation)
    if not path:
        return None
    content = path.read_bytes()
    if len(content) > 8 * 1024 * 1024:
        return None
    return f"data:image/jpeg;base64,{base64.b64encode(content).decode('ascii')}"


async def run_agent_for_alert(
    db: AsyncSession,
    alert_id: UUID,
    *,
    period: str = "72h",
    purpose: str = "CROP_ANALYSIS",
    full_context: dict[str, Any] | None = None,
    force: bool = False,
    forecast: list[dict[str, Any]] | None = None,
    forecast_risk: dict[str, Any] | None = None,
    phenology: dict[str, Any] | None = None,
    satellite: dict[str, Any] | None = None,
    year_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alert = await db.get(AlertEvaluation, alert_id)
    if not alert:
        raise ValueError("Evaluación de alerta no encontrada")

    base_context = full_context or await build_complete_analysis_context(db, alert.id, period=period)
    if forecast is not None:
        base_context["forecast"] = forecast
    if forecast_risk is not None:
        base_context["forecast_risk"] = forecast_risk
    if phenology is not None:
        base_context["phenology"] = phenology
    if satellite is not None:
        base_context["satellite"] = satellite
    if year_comparison is not None:
        base_context["year_comparison"] = year_comparison

    fallback_purpose = "CROP_ANALYSIS" if purpose != "CROP_ANALYSIS" else None
    agent, version = await select_agent_version(
        db,
        alert.crop_id,
        purpose,
        fallback_purpose=fallback_purpose,
    )
    response_policy = normalize_response_policy(
        version.output_schema if version else {},
        purpose,
    )

    needs_year_comparison = purpose == "YEAR_COMPARISON" or (
        purpose == "CROP_ANALYSIS" and response_policy.get("include_year_comparison")
    )
    if needs_year_comparison and not base_context.get("year_comparison"):
        station = await db.get(Station, alert.station_id)
        if station:
            try:
                base_context["year_comparison"] = await build_year_comparison(
                    db,
                    station,
                    period=period,
                    refresh_historical_if_missing=False,
                )
            except Exception as exc:
                logger.warning("agent_year_comparison_failed", error=str(exc)[:1000])
                base_context["year_comparison_status"] = {
                    "status": "FAILED",
                    "error": str(exc)[:1000],
                }

    base_context["agent_purpose"] = purpose
    base_context = _apply_response_scope(base_context, response_policy)

    context, sources = await _context_with_rag(
        db,
        alert,
        base_context,
        extra_query=f"{purpose} {PURPOSE_QUESTIONS.get(purpose, '')}",
        agent_id=agent.id if agent else None,
    )
    if not response_policy.get("include_sources"):
        context["display_sources"] = False

    if not agent or not version:
        started = datetime.now(UTC)
        payload = {
            "markdown": grounded_fallback_markdown(
                context,
                purpose=purpose,
                reason="agent_not_configured",
            ),
            "sources": sources,
            "has_satellite_image": False,
        }
        run = AIAgentRun(
            alert_evaluation_id=alert.id,
            status="FALLBACK",
            input_payload=context,
            output_payload=payload,
            sources=sources,
            started_at=started,
            finished_at=datetime.now(UTC),
            error_message=f"No existe un agente/version activo para {purpose}",
        )
        run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return {
            **payload,
            "_agent_status": "fallback",
            "_fallback_reason": "agent_not_configured",
            "_run_id": str(run.id),
            "_agent_name": "Respaldo determinístico",
            "_agent_purpose": purpose,
            "_model": None,
            "_cached": False,
        }

    try:
        revision = await _knowledge_revision(db, alert.crop_id, agent.id)
    except Exception as exc:
        logger.warning("agent_knowledge_revision_failed", error=str(exc)[:1000])
        revision = None
        context["knowledge_revision"] = {"status": "FAILED", "error": str(exc)[:1000]}
    context_signature = _signature(version, revision, base_context, purpose)
    context["_context_signature"] = context_signature
    if not force:
        cached = await db.scalar(
            select(AIAgentRun)
            .where(
                AIAgentRun.alert_evaluation_id == alert.id,
                AIAgentRun.agent_version_id == version.id,
                AIAgentRun.status.in_(["COMPLETED", "FALLBACK"]),
            )
            .order_by(AIAgentRun.created_at.desc())
            .limit(1)
        )
        if cached and (cached.input_payload or {}).get("_context_signature") == context_signature:
            return {
                **(cached.output_payload or {}),
                "_agent_status": cached.status.lower(),
                "_run_id": str(cached.id),
                "_agent_name": agent.name,
                "_agent_purpose": purpose,
                "_model": cached.model,
                "_cached": True,
            }

    run = AIAgentRun(
        agent_id=agent.id,
        agent_version_id=version.id,
        alert_evaluation_id=alert.id,
        status="RUNNING",
        model=version.model or settings.ai_default_model,
        input_payload=context,
        sources=sources,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    try:
        image_data_url = await _image_data_url_for_alert(db, alert) if purpose == "SATELLITE_ANALYSIS" else None
        token_ceiling = max(1400, int(response_policy["max_words"]) * 5)
        result = await generate_markdown_response(
            _effective_prompt(version.system_prompt, purpose, response_policy),
            context,
            PURPOSE_QUESTIONS.get(purpose, PURPOSE_QUESTIONS["CROP_ANALYSIS"]),
            version.model,
            image_data_url=image_data_url,
            temperature=version.temperature,
            max_tokens=min(max(version.max_tokens, 700), token_ceiling, 3000),
            response_policy=response_policy,
        )
        payload = {
            "markdown": result.get("markdown", ""),
            "sources": sources,
            "has_satellite_image": bool(image_data_url),
        }
        run.status = "FALLBACK" if result.get("fallback") else "COMPLETED"
        run.output_payload = payload
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        await db.commit()
        return {
            **payload,
            "_agent_status": run.status.lower(),
            "_fallback_reason": "provider_not_configured" if result.get("fallback") else None,
            "_run_id": str(run.id),
            "_agent_name": agent.name,
            "_agent_purpose": purpose,
            "_model": run.model,
            "_cached": False,
        }
    except Exception as exc:
        payload = {
            "markdown": grounded_fallback_markdown(
                context,
                purpose=purpose,
                reason="provider_error",
            ),
            "sources": sources,
            "has_satellite_image": False,
        }
        run.status = "FALLBACK"
        run.error_message = str(exc)[:3000]
        run.output_payload = payload
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        await db.commit()
        return {
            **payload,
            "_agent_status": "fallback",
            "_fallback_reason": "provider_error",
            "_run_id": str(run.id),
            "_agent_name": agent.name,
            "_agent_purpose": purpose,
            "_model": run.model,
            "_cached": False,
        }


async def chat_with_agent_for_alert(
    db: AsyncSession,
    alert_id: UUID,
    *,
    question: str,
    period: str = "72h",
    history: list[dict[str, str]] | None = None,
    image_data_url: str | None = None,
) -> dict[str, Any]:
    alert = await db.get(AlertEvaluation, alert_id)
    if not alert:
        raise ValueError("Evaluación de alerta no encontrada")

    base_context = await build_complete_analysis_context(db, alert.id, period=period)
    agent, version = await select_agent_version(db, alert.crop_id, "CROP_ANALYSIS")
    response_policy = normalize_response_policy(
        version.output_schema if version else {},
        "CROP_ANALYSIS",
    )
    base_context["agent_purpose"] = "CROP_ANALYSIS"
    base_context = _apply_response_scope(base_context, response_policy)
    context, sources = await _context_with_rag(
        db,
        alert,
        base_context,
        extra_query=question,
        agent_id=agent.id if agent else None,
    )
    if not response_policy.get("include_sources"):
        context["display_sources"] = False
    context["producer_question"] = question

    started = datetime.now(UTC)
    run = AIAgentRun(
        agent_id=agent.id if agent else None,
        agent_version_id=version.id if version else None,
        alert_evaluation_id=alert.id,
        status="RUNNING" if version else "FALLBACK",
        model=(version.model or settings.ai_default_model) if version else None,
        input_payload={
            "question": question,
            "period": period,
            "history_messages": len(history or []),
            "has_image": bool(image_data_url),
            "context": context,
        },
        sources=sources,
        started_at=started,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    if not agent or not version:
        markdown = grounded_fallback_markdown(
            context,
            purpose="CROP_ANALYSIS",
            reason="agent_not_configured",
        )
        if image_data_url:
            markdown += "\n\n> La imagen adjunta no fue interpretada porque no existe un agente general activo. No se infirió contenido visual."
        run.output_payload = {
            "markdown": markdown,
            "model": None,
            "usage": {},
            "has_image": False,
        }
        run.error_message = "El cultivo no tiene un agente general/version activo"
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
        await db.commit()
        return {
            "markdown": markdown,
            "fallback": True,
            "fallback_reason": "agent_not_configured",
            "run_id": str(run.id),
            "agent_name": "Respaldo determinístico",
            "agent_code": "DETERMINISTIC_FALLBACK",
            "sources": sources,
            "has_image": False,
        }

    try:
        token_ceiling = max(1400, int(response_policy["max_words"]) * 5)
        result = await generate_markdown_response(
            _effective_prompt(version.system_prompt, "CROP_ANALYSIS", response_policy),
            context,
            question,
            version.model,
            history=history,
            image_data_url=image_data_url,
            temperature=version.temperature,
            max_tokens=min(max(version.max_tokens, 600), token_ceiling, 3000),
            response_policy=response_policy,
        )
        run.status = "FALLBACK" if result.get("fallback") else "COMPLETED"
        run.output_payload = {
            "markdown": result.get("markdown"),
            "model": result.get("model"),
            "usage": result.get("usage", {}),
            "has_image": bool(image_data_url),
        }
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        await db.commit()
        return {
            **result,
            "run_id": str(run.id),
            "agent_name": agent.name,
            "agent_code": agent.code,
            "sources": sources,
            "has_image": bool(image_data_url),
        }
    except Exception as exc:
        markdown = grounded_fallback_markdown(
            context,
            purpose="CROP_ANALYSIS",
            reason="provider_error",
        )
        if image_data_url:
            markdown += "\n\n> La imagen adjunta no pudo ser procesada por el proveedor IA. No se infirió contenido visual."
        run.status = "FALLBACK"
        run.error_message = str(exc)[:3000]
        run.output_payload = {
            "markdown": markdown,
            "model": run.model,
            "usage": {},
            "has_image": False,
        }
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        await db.commit()
        return {
            "markdown": markdown,
            "fallback": True,
            "fallback_reason": "provider_error",
            "run_id": str(run.id),
            "agent_name": agent.name,
            "agent_code": agent.code,
            "sources": sources,
            "has_image": False,
        }
