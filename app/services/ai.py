from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import settings

OUTPUT_SCHEMA = {
    "resumen": "string",
    "explicacion_alerta": "string",
    "enfermedades": [{"nombre": "string", "nivel_riesgo": "bajo|moderado|alto", "justificacion": "string"}],
    "acciones_recomendadas": ["string"],
    "variables_a_vigilar": ["string"],
    "limitaciones": ["string"],
    "fuentes": [{"documento": "string", "pagina_o_seccion": "string"}],
}


def fallback_recommendation(context: dict[str, Any]) -> dict[str, Any]:
    alert = context.get("deterministic_result") or {}
    level = alert.get("level", "DATOS_INSUFICIENTES")
    actions = ["Revise en campo el estado general del cultivo y registre observaciones con fecha.", "Vuelva a consultar cuando la estación tenga nuevas lecturas."]
    if level in {"MODERADA", "GRAVE"}:
        actions.insert(0, "Aumente la frecuencia de monitoreo de hojas, tallos y síntomas en sectores representativos.")
    return {
        "resumen": alert.get("simple_explanation", "El análisis determinístico está disponible."),
        "explicacion_alerta": "Esta explicación de respaldo resume el motor de reglas y no altera su resultado.",
        "enfermedades": [
            {"nombre": d.get("name", "Enfermedad potencial"), "nivel_riesgo": d.get("risk", "moderado"), "justificacion": d.get("reason", "Coincidencia con una regla publicada.")}
            for d in alert.get("diseases", [])
        ],
        "acciones_recomendadas": actions,
        "variables_a_vigilar": [c.get("label") for c in alert.get("conditions", []) if c.get("status") in {"met", "missing"}],
        "limitaciones": ["La recomendación no reemplaza una inspección agronómica ni autoriza aplicaciones fitosanitarias."],
        "fuentes": context.get("sources", []),
    }


async def generate_recommendation(system_prompt: str, context: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    if not settings.ai_base_url or not settings.ai_api_key or not (model or settings.ai_default_model):
        return fallback_recommendation(context)
    payload = {
        "model": model or settings.ai_default_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({"context": context, "required_output": OUTPUT_SCHEMA}, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{settings.ai_base_url.rstrip('/')}{settings.ai_chat_path}", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    for key in OUTPUT_SCHEMA:
        if key not in parsed:
            raise ValueError(f"El agente no devolvió el campo {key}")
    return parsed


async def create_embedding(text: str) -> list[float] | None:
    if not settings.ai_base_url or not settings.ai_api_key or not settings.ai_embedding_model:
        return None
    headers = {"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"}
    payload = {"model": settings.ai_embedding_model, "input": text}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{settings.ai_base_url.rstrip('/')}{settings.ai_embeddings_path}", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    return data["data"][0]["embedding"]
