"""Recover legacy display cuts from the matching saved run, without calling AI."""
from __future__ import annotations

import re

from sqlalchemy import select

from app.db.models import AIAgentRun


def needs_text_recovery(result: dict) -> bool:
    text = str(result.get('markdown') or '').strip()
    return bool(text.endswith(('…', '...')) or text and re.match(r'^#{1,6}\s+', text.splitlines()[-1]))


def recover_widget_text(result: dict, output: dict) -> dict | None:
    # Same analysis is enforced by the caller; also match scope and exact text
    # so an older or different interpretation never substitutes another answer.
    for key in ('purpose', 'window_hours', 'view_mode', 'version_id', 'version_updated_at', 'language_revision'):
        if result.get(key) != output.get(key):
            return None
    displayed = str(result.get('markdown') or '').strip()
    if displayed != str(output.get('markdown') or '').strip():
        return None
    full = output.get('full_markdown')
    if not isinstance(full, str) or not full.strip():
        return None
    prefix = re.sub(r'(?:…|\.{3})$', '', displayed).rstrip()
    if not full.strip().startswith(prefix):
        return None
    restored = {key: value for key, value in result.items() if key != 'max_characters'}
    full = full.strip()
    last_line = full.splitlines()[-1].strip()
    restored.update(markdown=full, text_complete=not (
        full.endswith(('…', '...')) or re.match(r'^#{1,6}\s+', last_line)
    ))
    return restored


async def recover_analysis_widget_text(db, analysis) -> dict:
    results = dict(analysis.agent_results or {})
    pending = {purpose for purpose, result in results.items()
               if isinstance(result, dict) and needs_text_recovery(result)}
    for purpose, result in results.items():
        if isinstance(result, dict) and purpose not in pending and 'max_characters' in result:
            results[purpose] = {key: value for key, value in result.items() if key != 'max_characters'}
    if not pending:
        return results
    # Only the authorized analysis's runs, only output fields, newest first.
    rows = await db.execute(select(
        AIAgentRun.input_payload['purpose'].as_string(), AIAgentRun.output_payload,
    ).where(
        AIAgentRun.input_payload['plot_analysis_id'].as_string() == str(analysis.id),
        AIAgentRun.input_payload['purpose'].as_string().in_(pending),
    ).order_by(AIAgentRun.created_at.desc()).limit(100))
    for purpose, output in rows:
        if purpose not in pending or not isinstance(output, dict):
            continue
        restored = recover_widget_text(results[purpose], output)
        if restored:
            results[purpose] = restored
            pending.remove(purpose)
    for purpose in pending:
        results[purpose] = {**results[purpose], 'text_complete': False}
    return results
