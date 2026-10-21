"""FECOCLIMA station endpoints supplied as concrete URLs or PDF templates."""
from __future__ import annotations

import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4


def generated_station_code() -> str:
    return f"EST-{uuid4().hex[:12].upper()}"


def normalize_station_endpoint(value: str, external_guid: str | None = None) -> tuple[str, str | None]:
    value = value.strip()
    for source, target in {
        "{id-estacion}": "{guid}", "{id_estacion}": "{guid}",
        "{año}": "{year}", "{anio}": "{year}",
        "{mesDesde}": "{month_from}", "{mesHasta}": "{month_to}",
    }.items():
        value = value.replace(source, target)
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("La URL de API debe ser HTTPS, sin claves, credenciales ni parámetros de consulta")
    if external_guid and (url.path.rstrip('/').endswith('/clima/api') or url.path.rstrip('/').endswith('/estacion/lectura-guid-por-periodo')):
        prefix = value.rstrip('/')
        if prefix.endswith('/clima/api'):
            prefix += '/estacion/lectura-guid-por-periodo'
        value = f"{prefix}/{external_guid}/{{year}}/{{month_from}}/{{month_to}}"
    # Accept a station-specific endpoint without year/month suffix as well.
    short = re.fullmatch(r"(.*/estacion/lectura-guid-por-periodo)/([^/]+)/?", value)
    if short:
        value = f"{short[1]}/{short[2]}/{{year}}/{{month_from}}/{{month_to}}"
    match = re.fullmatch(
        r"(.*/estacion/lectura-guid-por-periodo)/([^/]+)/([^/]+)/([^/]+)/([^/]+)/?", value
    )
    if match:
        prefix, identifier, year, month_from, month_to = match.groups()
        if identifier != "{guid}":
            try:
                found_guid = str(UUID(identifier)).upper()
            except ValueError as exc:
                raise ValueError("La URL debe incluir el GUID real de la estación") from exc
            if external_guid and str(UUID(external_guid)).upper() != found_guid:
                raise ValueError("El GUID indicado no coincide con la estación de la URL")
            external_guid = found_guid
        if year != "{year}" and not re.fullmatch(r"\d{4}", year):
            raise ValueError("Año inválido en la URL de la estación")
        for month, placeholder in ((month_from, "{month_from}"), (month_to, "{month_to}")):
            if month != placeholder and (not month.isdigit() or not 1 <= int(month) <= 12):
                raise ValueError("Los meses de la URL deben estar entre 1 y 12")
        value = f"{prefix}/{{guid}}/{{year}}/{{month_from}}/{{month_to}}"
    from string import Formatter
    fields = {field for _, field, _, _ in Formatter().parse(value) if field}
    if not fields <= {"guid", "year", "month_from", "month_to"}:
        raise ValueError("Marcadores permitidos: guid, year, month_from, month_to")
    return value, external_guid
