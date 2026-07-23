from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import AIAgent, AIAgentRun, AIAgentVersion, AlertConditionResult, AlertEvaluation, Crop, Station
from app.services.ai import generate_recommendation
from app.services.rag import retrieve_chunks


async def run_agent_for_alert(
    db: AsyncSession,
    alert_id: UUID,
    *,
    forecast: list[dict[str, Any]] | None = None,
    satellite: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    alert = await db.get(AlertEvaluation, alert_id)
    if not alert:
        raise ValueError("Evaluación de alerta no encontrada")
    if not force:
        cached = await db.scalar(
            select(AIAgentRun)
            .where(
                AIAgentRun.alert_evaluation_id == alert.id,
                AIAgentRun.status.in_(["COMPLETED", "FALLBACK"]),
            )
            .order_by(AIAgentRun.created_at.desc())
            .limit(1)
        )
        if cached:
            return {**cached.output_payload, "_agent_status": cached.status.lower(), "_run_id": str(cached.id)}
    crop = await db.get(Crop, alert.crop_id)
    station = await db.get(Station, alert.station_id)
    agent = await db.scalar(
        select(AIAgent).where(
            AIAgent.crop_id == alert.crop_id,
            AIAgent.active.is_(True),
            AIAgent.deleted_at.is_(None),
        )
    )
    if not agent:
        return None
    version = await db.scalar(
        select(AIAgentVersion)
        .where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True))
        .order_by(AIAgentVersion.created_at.desc())
        .limit(1)
    )
    if not version:
        return None
    condition_rows = list(
        (
            await db.scalars(
                select(AlertConditionResult)
                .where(AlertConditionResult.alert_evaluation_id == alert.id)
                .order_by(AlertConditionResult.created_at)
            )
        ).all()
    )
    chunks = await retrieve_chunks(
        db,
        f"{crop.name if crop else ''} {alert.stage_code or ''} {alert.explanation or ''}",
        alert.crop_id,
        limit=6,
    )
    sources = [
        {"documento": chunk["document"], "pagina_o_seccion": chunk["page_or_section"]}
        for chunk in chunks
    ]
    context = {
        "crop": crop.name if crop else None,
        "station": station.name if station else None,
        "stage": alert.stage_code,
        "period": alert.evaluation_date.isoformat(),
        "deterministic_result": {
            "level": alert.level,
            "conditions_met": alert.conditions_met,
            "conditions_total": alert.conditions_total,
            "completion_percentage": alert.completion_pct,
            "confidence": alert.confidence,
            "conditions": [
                {
                    "key": row.key,
                    "label": row.label,
                    "status": row.status,
                    "value": row.observed_value,
                    "unit": row.unit,
                    "explanation": row.explanation,
                }
                for row in condition_rows
            ],
            "diseases": alert.diseases,
            "simple_explanation": alert.explanation,
        },
        "forecast": forecast or [],
        "satellite": satellite,
        "sources": sources,
        "retrieved_fragments": chunks,
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
        result = await generate_recommendation(version.system_prompt, context, version.model)
        run.status = "COMPLETED" if settings.ai_base_url and settings.ai_api_key else "FALLBACK"
        run.output_payload = result
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        await db.commit()
        return {**result, "_agent_status": run.status.lower(), "_run_id": str(run.id)}
    except Exception as exc:
        run.status = "FAILED"
        run.error_message = str(exc)[:3000]
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        await db.commit()
        return None
