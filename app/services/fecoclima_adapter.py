from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.core.config import settings

ASUNCION = ZoneInfo("America/Asuncion")
logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class MonthBlock:
    year: int
    month_from: int
    month_to: int


def split_month_blocks(date_from: date, date_to: date, max_months: int = 6) -> list[MonthBlock]:
    if date_to < date_from:
        raise ValueError("date_to no puede ser anterior a date_from")
    result: list[MonthBlock] = []
    year = date_from.year
    while year <= date_to.year:
        start_month = date_from.month if year == date_from.year else 1
        end_month = date_to.month if year == date_to.year else 12
        current = start_month
        while current <= end_month:
            block_end = min(current + max_months - 1, end_month)
            result.append(MonthBlock(year, current, block_end))
            current = block_end + 1
        year += 1
    return result


def _number(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        numeric = float(str(value).replace(",", "."))
        return numeric if isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any, timezone: ZoneInfo = ASUNCION) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        # Invalid optional extrema timestamps must not discard the entire
        # measured sample. normalize_fecoclima_item still requires sampled_at.
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(UTC)


def _json_list(raw_body: bytes | str) -> list[dict[str, Any]]:
    if isinstance(raw_body, bytes):
        text = raw_body.decode("utf-8-sig")
    else:
        text = raw_body
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise RuntimeError("La API FECOCLIMA devolvió un formato inesperado")
    return payload


def _parse_curl_output(output: bytes) -> tuple[int, bytes]:
    marker = b"\n__FECOCLIMA_HTTP_STATUS__:"
    body, separator, status_raw = output.rpartition(marker)
    if not separator:
        raise RuntimeError("No se pudo determinar el código HTTP devuelto por FECOCLIMA")
    try:
        status_code = int(status_raw.strip())
    except ValueError as exc:
        raise RuntimeError("FECOCLIMA devolvió un código HTTP inválido") from exc
    return status_code, body


def _interpret_curl_result(
    output: bytes,
    returncode: int,
    stderr: bytes,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Interpreta la salida de curl incluso si OpenSSL reporta un EOF no limpio.

    Algunos servidores antiguos entregan el cuerpo JSON completo y luego cierran TLS
    sin enviar ``close_notify``. En ese caso curl finaliza con código 56 aunque la
    respuesta útil ya se recibió. Solo se acepta la respuesta degradada cuando el
    cuerpo es JSON completo y tiene exactamente el formato de lista esperado.
    """
    status_code: int | None = None
    body = output
    status_error: Exception | None = None

    try:
        status_code, body = _parse_curl_output(output)
    except RuntimeError as exc:
        # ``--write-out`` normalmente agrega el marcador aun cuando curl falla,
        # pero conservamos compatibilidad por si el proceso termina antes.
        status_error = exc

    if status_code is not None and not 200 <= status_code < 300:
        preview = body.decode("utf-8", errors="replace").strip()[:500]
        raise RuntimeError(f"FECOCLIMA respondió HTTP {status_code}: {preview}")

    try:
        items = _json_list(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as body_error:
        if returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"curl no pudo consultar FECOCLIMA (código {returncode}): "
                f"{error_text[:500]}; el cuerpo recibido no era JSON completo"
            ) from body_error
        if status_error is not None:
            raise status_error from body_error
        raise

    # Si el JSON está completo, puede usarse aunque curl haya reportado el cierre
    # TLS defectuoso. No se acepta contenido truncado porque json.loads fallaría.
    degraded = returncode != 0 or status_code is None
    return items, degraded, status_code


def normalize_fecoclima_item(item: dict[str, Any], *, timezone: ZoneInfo = ASUNCION) -> dict[str, Any]:
    sampled_at = _timestamp(item.get("fechaMuestreo"), timezone)
    if sampled_at is None:
        raise ValueError("La respuesta no contiene fechaMuestreo válida")
    wind_dir = _number(item.get("windDir"))
    return {
        "sampled_at": sampled_at,
        "source": "measured",
        "source_reference": "FECOCLIMA_API",
        "temperature_c": _number(item.get("temp")),
        "feels_like_c": _number(item.get("chill")),
        "heat_index_c": _number(item.get("hIndex")),
        "humidity_pct": _number(item.get("humid")),
        "dew_point_c": _number(item.get("dewpt")),
        "wind_speed_ms": _number(item.get("wind")),
        "wind_gust_ms": _number(item.get("hiWind")),
        "wind_direction_deg": wind_dir,
        "rain_mm": _number(item.get("rain")),
        "total_rain_mm": _number(item.get("totalRain")),
        "pressure_hpa": _number(item.get("barom")),
        "solar_wm2": _number(item.get("solar")),
        "evapotranspiration_mm": _number(item.get("et")),
        "uv_index": _number(item.get("uv")),
        "min_temperature_c": _number(item.get("minTemperatura")),
        "max_temperature_c": _number(item.get("maxTemperatura")),
        "min_temperature_at": _timestamp(item.get("fechaMinTemp"), timezone),
        "max_temperature_at": _timestamp(item.get("fechaMaxTemp"), timezone),
        "soil_temperature_c": _number(item.get("temperaturaSuelo")),
        "soil_moisture_pct": _number(item.get("humedadSuelo")),
        "leaf_sensor_value": _number(item.get("sensorHoja")),
        "raw_payload": item,
    }


class FecoclimaAdapter:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, endpoint_url: str | None = None):
        self.base_url = (base_url or settings.fecoclima_base_url).rstrip("/")
        self.api_key = api_key or settings.fecoclima_api_key
        self.endpoint_url = endpoint_url

    async def _fetch_block_with_curl(self, url: str) -> list[dict[str, Any]]:
        """Compatibilidad con el servidor FECOCLIMA cuando devuelve headers HTTP no RFC.

        La clave se envía por stdin mediante ``--header @-`` para evitar exponerla en
        los argumentos del proceso y en listados de procesos del contenedor.
        """
        if not self.api_key:
            raise RuntimeError("FECOCLIMA_API_KEY no está configurada")
        if "\r" in self.api_key or "\n" in self.api_key:
            raise RuntimeError("FECOCLIMA_API_KEY contiene caracteres no válidos")

        timeout_seconds = max(1, int(settings.fecoclima_timeout_seconds))
        try:
            process = await asyncio.create_subprocess_exec(
                "curl",
                "--silent",
                "--show-error",
                "--location",
                "--http1.1",
                "--compressed",
                "--connect-timeout",
                str(min(timeout_seconds, 15)),
                "--max-time",
                str(timeout_seconds),
                "--header",
                "@-",
                "--write-out",
                "\n__FECOCLIMA_HTTP_STATUS__:%{http_code}",
                url,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "El contenedor no tiene curl disponible para el modo compatible de FECOCLIMA"
            ) from exc

        header_input = f"Accept: application/json\nclima-key: {self.api_key}\n".encode()
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(header_input),
                timeout=timeout_seconds + 5,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("Timeout consultando la API FECOCLIMA") from exc

        items, degraded, status_code = _interpret_curl_result(
            stdout,
            process.returncode or 0,
            stderr,
        )
        if degraded:
            logger.warning(
                "fecoclima_tls_eof_accepted",
                curl_code=process.returncode,
                http_status=status_code,
                bytes_received=len(stdout),
            )
        return items

    async def fetch_block(self, guid: str, block: MonthBlock) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("FECOCLIMA_API_KEY no está configurada")
        url = (
            f"{self.base_url}/estacion/lectura-guid-por-periodo/"
            f"{guid}/{block.year}/{block.month_from}/{block.month_to}"
        )
        if self.endpoint_url:
            url = self.endpoint_url.format(guid=guid, year=block.year,
                                           month_from=block.month_from, month_to=block.month_to)
        headers = {"clima-key": self.api_key, "Accept": "application/json"}
        last_error: Exception | None = None
        attempts = max(1, settings.fecoclima_retry_count)
        async with httpx.AsyncClient(
            timeout=settings.fecoclima_timeout_seconds,
            follow_redirects=True,
        ) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    return _json_list(response.content)
                except httpx.RemoteProtocolError as exc:
                    # El servidor FECOCLIMA actualmente envía un header con espacio antes
                    # de los dos puntos. h11/httpx lo rechaza por no cumplir RFC 7230.
                    logger.warning(
                        "fecoclima_nonstandard_http_headers",
                        block_year=block.year,
                        month_from=block.month_from,
                        month_to=block.month_to,
                        transport="curl_compat",
                    )
                    try:
                        return await self._fetch_block_with_curl(url)
                    except Exception as fallback_exc:
                        last_error = fallback_exc
                        logger.warning(
                            "fecoclima_compat_transport_failed",
                            attempt=attempt,
                            error=str(fallback_exc)[:500],
                        )
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.HTTPStatusError,
                    json.JSONDecodeError,
                    RuntimeError,
                ) as exc:
                    last_error = exc

                if attempt < attempts:
                    await asyncio.sleep(min(8, 2 ** (attempt - 1)))

        if last_error:
            raise last_error
        return []

    async def fetch_range(self, guid: str, date_from: date, date_to: date, *, timezone: str = "America/Asuncion") -> list[dict[str, Any]]:
        local_tz = ZoneInfo(timezone)
        normalized: list[dict[str, Any]] = []
        for block in split_month_blocks(date_from, date_to):
            raw_items = await self.fetch_block(guid, block)
            for item in raw_items:
                try:
                    reading = normalize_fecoclima_item(item, timezone=local_tz)
                except (ValueError, TypeError):
                    continue
                local_date = reading["sampled_at"].astimezone(local_tz).date()
                if date_from <= local_date <= date_to:
                    normalized.append(reading)
        deduplicated = {item["sampled_at"]: item for item in normalized}
        return [deduplicated[key] for key in sorted(deduplicated)]
