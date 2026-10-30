"""Compact labels separately from complete Markdown explanations."""
from __future__ import annotations
import html
import re

WIDGET_CHARACTER_LIMIT = 300


def widget_summary(value: str | None, limit: int = WIDGET_CHARACTER_LIMIT) -> str:
    text = str(value or '')
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)', '', text)
    text = text.replace('**', '').replace('__', '').replace('`', '')
    text = ' '.join(html.unescape(text).split())
    if len(text) <= limit:
        return text
    prefix = text[:limit-1]
    # Prefer a full sentence, then a word boundary; retain a hard ceiling even
    # for malformed or unbroken provider output.
    sentences = list(re.finditer(r'[.!?](?:\s|$)', prefix))
    if sentences and sentences[-1].end() >= limit // 2:
        return prefix[:sentences[-1].end()].rstrip()
    if ' ' in prefix and prefix.rfind(' ') >= limit // 2:
        prefix = prefix[:prefix.rfind(' ')]
    return prefix.rstrip(' ,;:-') + '…'


def compact_widget_result(result: dict, purpose: str, *, limit: int = WIDGET_CHARACTER_LIMIT, preserve_format: bool = False) -> dict:
    if preserve_format or purpose in {'PDF_REPORT', 'ORCHESTRATOR', 'CROP_CHAT'}:
        return {key: value for key, value in result.items() if key != 'max_characters'}
    return {**result, 'markdown': widget_summary(result.get('markdown'), limit=limit), 'max_characters': limit}
