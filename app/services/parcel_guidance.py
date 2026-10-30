"""Readable parcel guidance and optional, evidence-backed field visit links."""
from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime

from shapely.geometry import Point, shape

from app.services.orchestrator_outputs import parse_widget_outputs, render_widgets


def field_review_candidate(context: dict) -> dict | None:
    """A minimum-value pixel is a candidate, never an automatic visit or diagnosis."""
    satellite = context.get('satellite') or {}
    analysis = satellite.get('analysis') or {}
    point = context.get('inspection_point') or {}
    if point.get('method') != 'lowest_valid_ndvi_pixel':
        return None
    if satellite.get('data_is_stale') is not False or analysis.get('reliability') not in {'alta', 'media'}:
        return None
    if analysis.get('geometry_mode') != 'polygon':
        return None
    if not any(signal.get('level') == 'attention' for signal in analysis.get('signals') or []):
        return None
    if not point.get('captured_at') or point['captured_at'] != satellite.get('captured_at'):
        return None
    lat, lon = point.get('latitude'), point.get('longitude')
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in (lat, lon, point.get('ndvi'))):
        return None
    thresholds = analysis.get('thresholds') or {}
    try:
        captured = datetime.fromisoformat(satellite['captured_at'].replace('Z', '+00:00'))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        current_age = (datetime.now(UTC) - captured).total_seconds() / 86400
        if current_age < 0 or current_age > thresholds.get('max_age_days', 20):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    for value, maximum in ((satellite.get('cloud_percentage'), thresholds.get('max_reliable_cloud_percentage')),
                           (analysis.get('age_days'), thresholds.get('max_age_days'))):
        if value is not None and maximum is not None and value > maximum:
            return None
    try:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180
                and shape(context['plot']['geojson']).covers(Point(lon, lat))):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {
        'latitude': lat, 'longitude': lon, 'captured_at': point['captured_at'],
        'maps_url': f'https://www.google.com/maps/search/?api=1&query={lat:.7f},{lon:.7f}',
        'basis': 'Punto de menor señal de vegetación. No demuestra daño ni una causa; requiere una razón adicional para visitar.',
    }


def guidance_response_contract(allowed: list[str] | None = None) -> str:
    body = ('"widgets":{clave:texto}' if allowed is not None else '"markdown":"explicación completa en Markdown"')
    return (
        '\nCONTRATO DE SALIDA PRIORITARIO PARA EL WIDGET 4: Devuelve únicamente un objeto JSON {'
        + body + ',"field_review":{"recommended":false,"reason":"","check":""}}. '
        'Los textos interiores son lenguaje natural, nunca instrucciones técnicas. '
        + ('Solo se admiten estas claves en widgets: ' + ', '.join(allowed) + '. ' if allowed is not None else '')
        + 'field_review es opcional: recommended solo puede ser true si field_review_candidate existe Y '
        'una comprobación concreta en ese punto puede cambiar una decisión de manejo. '
        'reason explica por qué vale la pena visitar esa zona y check qué observar o comparar allí. '
        'No recomiendes desplazarse por rutina, por tener un punto disponible, por una alerta climática sola '
        'o por tratarse simplemente del valor más bajo. Si no hay motivo suficiente, usa false. '
        'No incluyas enlaces, coordenadas ni destinos en los textos: el servidor añade el enlace validado. '
        'Este contrato sustituye los formatos de salida anteriores.'
    )


def _without_links(text: str) -> str:
    # Sources are displayed separately; locations are owned by the server.
    text = re.sub(r'\[([^\]]+)\]\(https?://[^\s)]+\)', r'\1', text)
    return re.sub(r'https?://\S+', '', text).strip()


def parse_guidance_response(content: str, context: dict, allowed: list[str] | None = None) -> dict:
    text = content.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    # Legacy Markdown remains readable, but cannot create a location link.
    if not text.startswith('{') and allowed is None:
        return {'markdown': _without_links(text)}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError('La interpretación no contiene una explicación válida')
    result = {}
    if allowed is not None:
        sections = parse_widget_outputs(text, allowed)
        result['widgets'] = {key: _without_links(value) for key, value in sections.items()}
        result['markdown'] = render_widgets(result['widgets'])
    else:
        markdown = parsed.get('markdown')
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError('La interpretación no contiene una explicación legible')
        result['markdown'] = _without_links(markdown)
    review = parsed.get('field_review')
    candidate = context.get('field_review_candidate')
    permitted = allowed is None or bool({'diagnosis', 'satellite'} & set(allowed))
    if permitted and candidate and isinstance(review, dict) and review.get('recommended') is True:
        reason, check = review.get('reason'), review.get('check')
        if isinstance(reason, str) and reason.strip() and isinstance(check, str) and check.strip():
            result['field_review'] = {
                **candidate, 'reason': _without_links(reason)[:700], 'check': _without_links(check)[:700],
            }
    return result


def satellite_plain_evidence(context: dict) -> str:
    satellite = context.get('satellite') or {}
    if not satellite:
        return 'No hay una imagen válida de esta parcela para explicar el estado de la vegetación.'
    analysis = satellite.get('analysis') or {}
    messages = []
    translations = {
        ('ndvi_state', 'attention'): 'La vegetación tiene poca cobertura o actividad en la imagen; eso no permite saber por sí solo la causa.',
        ('ndvi_state', 'info'): 'La imagen indica una cobertura o actividad intermedia de la vegetación, que debe compararse con la etapa del cultivo.',
        ('ndvi_state', 'positive'): 'La imagen registra vegetación activa; eso no descarta problemas puntuales.',
        ('ndmi_state', 'attention'): 'La imagen aporta una señal de menor humedad en la vegetación o superficie; no es una medición del agua disponible en las raíces.',
        ('ndvi_trend', 'attention'): 'La señal de vegetación disminuyó respecto de la imagen anterior; conviene comprobar si hay cambios visibles en el cultivo.',
        ('ndmi_trend', 'attention'): 'La señal de humedad disminuyó respecto de la imagen anterior; compárala con la lluvia y las lecturas de suelo.',
        ('ndwi_state', 'attention'): 'Hay una señal de agua o superficie húmeda; hace falta comprobarla antes de afirmar que hay encharcamiento.',
    }
    for signal in analysis.get('signals') or []:
        message = translations.get((signal.get('code'), signal.get('level')))
        if message:
            messages.append(message)
    if not messages:
        messages.append('Hay información satelital disponible, pero falta una interpretación suficiente para afirmar qué ocurre en el cultivo o localizar un problema.')
    if satellite.get('data_is_stale') or analysis.get('reliability') == 'limitada':
        messages.insert(0, 'La imagen es antigua o tiene visibilidad limitada; úsala como antecedente y no como prueba del estado actual.')
    if satellite.get('captured_at'):
        messages.insert(0, 'Fecha de la imagen: ' + str(satellite['captured_at'])[:10] + '.')
    return ' '.join(messages)
