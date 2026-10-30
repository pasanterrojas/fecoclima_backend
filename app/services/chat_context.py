"""Keep user observations, visual evidence and calculated parcel facts distinct."""
import math
from typing import Any


def irrigation_fallback(context: dict) -> str:
    """Explain saved soil evidence without turning disease rules into irrigation rules."""
    window = (context.get('windows') or {}).get(str(context.get('selected_window_hours', 72))) or {}
    stations = {row.get('station_id'): row for row in window.get('stations') or []}
    paragraphs = []
    dry_readings = 0
    usable_readings = 0
    for row in context.get('raw_readings') or []:
        reading = row.get('reading') or {}
        value = reading.get('soil_moisture_pct')
        unit = (row.get('sensor_units') or {}).get('soil_moisture_pct')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not unit:
            continue
        station = stations.get(row.get('station_id')) or {}
        name = row.get('name') or 'La estación'
        at = reading.get('sampled_at')
        text = f"{name} registró **{value:g} {unit}**" + (f" el {at}" if at else '') + '.'
        tension = row.get('soil_tension') or {}
        # Classification must belong to this reading and to the same unit.
        band = tension.get('band_index') if unit == 'cb' and tension.get('value') == value else None
        if station.get('stale') is True:
            text += ' Esa lectura está atrasada; hace falta actualizarla para decidir el riego actual.'
        elif station.get('stale') is not False:
            text += ' No se pudo verificar la vigencia de esa lectura; actualízala y usa una referencia calibrada para decidir.'
        elif band is not None:
            usable_readings += 1
            text += f" La clasificación orientativa es **{str(tension.get('label') or '').lower()}**."
            if band >= 3:
                dry_readings += 1
                text += ' Es una señal para revisar el agua disponible en las raíces y preparar el riego si se confirma la sequedad.'
            elif band == 0:
                text += ' Esta lectura indica saturación: no apoya añadir más agua en ese punto.'
            elif band == 1:
                text += ' Esta lectura por sí sola no indica necesidad inmediata de riego en ese punto.'
            else:
                text += ' Conviene contrastarla con el umbral de riego calibrado para el suelo y cultivo.'
        else:
            text += ' Falta una referencia calibrada para interpretar si el cultivo necesita riego con esta medida.'
        rain = (station.get('values') or {}).get('rain_sum_mm')
        if rain is not None:
            text += f" La lluvia acumulada de esa estación en las {window.get('hours', context.get('selected_window_hours', 72))} horas analizadas fue {rain:g} mm."
        paragraphs.append(text)
    if not paragraphs:
        return ('No hay una medición de suelo con unidad conocida que permita decidir el riego en este análisis. '
                'Comprueba si la zona de raíces está seca o húmeda antes de añadir agua. '
                '¿Qué humedad encuentras a la profundidad de las raíces?')
    days = [day for day in (context.get('forecast') or {}).get('days') or []
            if day.get('precipitation_mm') is not None]
    if days:
        paragraphs.append('El pronóstico guardado prevé ' + '; '.join(
            f"{day['precipitation_mm']:g} mm para {day['date']}" for day in days[:3]
        ) + '. Es lluvia prevista, no agua que ya haya recibido la parcela.')
    else:
        paragraphs.append('El análisis no incluye un pronóstico de lluvia para contrastar la decisión.')
    if dry_readings:
        paragraphs.append('Si esa sequedad se mantiene en la zona de raíces, conviene regar según el manejo calibrado del lote. '
                          'La lectura no determina cuántos litros ni cuánto tiempo: depende del suelo, la profundidad y el sistema. '
                          '¿A qué profundidad está instalado el sensor?')
    elif usable_readings:
        paragraphs.append('Usa una referencia calibrada para tu suelo y cultivo antes de cambiar el riego; '
                          'cada estación describe su punto de medición, no toda la parcela.')
    return '\n\n'.join(paragraphs)


def widget_context(results: dict, window_hours: int) -> dict[str, str]:
    return {
        purpose: str(result['markdown'])
        for purpose, result in results.items()
        if purpose != 'CROP_CHAT' and isinstance(result, dict)
        and result.get('window_hours') == window_hours and result.get('markdown')
    }


def conversation_observations(history: list[dict] | None) -> list[dict[str, Any]]:
    """Unverified user statements, not automatically accepted agronomic facts."""
    return [
        {'statement': str(item.get('content') or '')[:1000],
         'transcription': str(item.get('transcription') or '')[:1000],
         'photo_count': len(item.get('images') or []),
         'source': 'user_statement_not_verified'}
        for item in (history or [])[-100:]
        if item.get('role') == 'user'
    ]
