from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DataImport, DataImportError, Station
from app.services.weather import upsert_readings

ALLOWED_MIME = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
CANONICAL_FIELDS = {
    "sampled_at",
    "temperature_c",
    "feels_like_c",
    "heat_index_c",
    "humidity_pct",
    "dew_point_c",
    "wind_speed_ms",
    "wind_gust_ms",
    "wind_direction_deg",
    "rain_mm",
    "total_rain_mm",
    "pressure_hpa",
    "solar_wm2",
    "evapotranspiration_mm",
    "uv_index",
    "min_temperature_c",
    "max_temperature_c",
    "soil_temperature_c",
    "soil_moisture_pct",
    "leaf_sensor_value",
}


def read_tabular(content: bytes, filename: str) -> pd.DataFrame:
    if filename.lower().endswith(".csv"):
        try:
            return pd.read_csv(io.BytesIO(content), sep=None, engine="python")
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(content), sep=None, engine="python", encoding="latin-1")
    if filename.lower().endswith(".xls"):
        raise ValueError("El formato XLS antiguo no está habilitado; convierta el archivo a XLSX o CSV")
    return pd.read_excel(io.BytesIO(content), engine="openpyxl")


def _json_safe(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def preview_tabular(content: bytes, filename: str) -> dict[str, Any]:
    frame = read_tabular(content, filename)
    rows = [{str(key): _json_safe(value) for key, value in row.items()} for row in frame.head(20).to_dict(orient="records")]
    return {
        "columns": [str(column) for column in frame.columns],
        "rows": rows,
        "total_rows": len(frame),
        "suggested_mapping": suggest_mapping(frame.columns),
    }


def suggest_mapping(columns) -> dict[str, str]:
    aliases = {
        "sampled_at": ["fecha", "fechamuestreo", "datetime", "fecha hora"],
        "temperature_c": ["temperatura", "temp", "temperature"],
        "feels_like_c": ["sens termica", "sensación térmica", "chill"],
        "heat_index_c": ["indice calor", "índice calor", "hindex"],
        "humidity_pct": ["humedad", "humid", "humedad relativa"],
        "dew_point_c": ["punto rocio", "punto de rocío", "dewpt"],
        "wind_speed_ms": ["viento", "wind"],
        "wind_gust_ms": ["rafaga", "ráfaga", "hiwind"],
        "wind_direction_deg": ["dir viento", "winddir", "direccion viento"],
        "rain_mm": ["lluvia", "rain"],
        "total_rain_mm": ["lluvia acumulada", "totalrain"],
        "pressure_hpa": ["presion", "presión", "barom"],
        "solar_wm2": ["solar", "radiacion", "radiación"],
        "evapotranspiration_mm": ["et", "evapotranspiracion", "evapotranspiración"],
        "uv_index": ["uv", "indice uv", "índice uv"],
        "soil_temperature_c": ["temp suelo", "temperatura suelo"],
        "leaf_sensor_value": ["sensor hoja", "mojado foliar"],
        "soil_moisture_pct": ["humedad suelo"],
        
    }
    normalized = {str(column).strip().lower(): str(column) for column in columns}
    result: dict[str, str] = {}
    for canonical, options in aliases.items():
        for column_key, original in normalized.items():
            if any(alias in column_key for alias in options):
                result[canonical] = original
                break
    return result


def _write_error_report(import_id, errors: list[dict[str, Any]]) -> str | None:
    if not errors:
        return None
    directory = settings.storage_path / "error-reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"import_{import_id}_errors.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["row", "column", "code", "error", "raw_row"])
        writer.writeheader()
        for item in errors:
            writer.writerow(
                {
                    "row": item.get("row"),
                    "column": item.get("column"),
                    "code": item.get("code"),
                    "error": item.get("error"),
                    "raw_row": json.dumps(item.get("raw_row", {}), ensure_ascii=False),
                }
            )
    return str(path)


async def import_weather_file(
    db: AsyncSession,
    station: Station,
    content: bytes,
    filename: str,
    mapping: dict[str, str],
    user_id=None,
) -> dict:
    if "sampled_at" not in mapping:
        raise ValueError("Debe mapear la columna de fecha y hora")
    unknown = set(mapping) - CANONICAL_FIELDS
    if unknown:
        raise ValueError(f"Campos no admitidos: {', '.join(sorted(unknown))}")
    frame = read_tabular(content, filename)
    missing_columns = sorted({column for column in mapping.values() if column not in frame.columns})
    if missing_columns:
        raise ValueError("Columnas no encontradas: " + ", ".join(missing_columns))
    checksum = hashlib.sha256(content).hexdigest()
    record = DataImport(
        station_id=station.id,
        filename=filename,
        checksum_sha256=checksum,
        status="PROCESSING",
        rows_total=len(frame),
        mapping=mapping,
        imported_by=user_id,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    timezone = ZoneInfo(station.timezone or "America/Asuncion")
    readings: list[dict] = []
    errors: list[dict[str, Any]] = []
    for row_number, (_, row) in enumerate(frame.iterrows(), start=2):
        raw_row = {str(key): _json_safe(value) for key, value in row.to_dict().items()}
        try:
            raw_date = row[mapping["sampled_at"]]
            parsed = pd.to_datetime(raw_date, errors="raise").to_pydatetime()
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone)
            reading = {
                "sampled_at": parsed.astimezone(UTC),
                "source": "imported",
                "source_reference": filename,
                "raw_payload": raw_row,
                "imported_by": user_id,
            }
            for canonical, source_column in mapping.items():
                if canonical == "sampled_at":
                    continue
                value = row[source_column]
                reading[canonical] = None if pd.isna(value) or value == "" else float(str(value).replace(",", "."))
            readings.append(reading)
        except Exception as exc:
            errors.append(
                {
                    "row": row_number,
                    "column": None,
                    "code": "ROW_PARSE_ERROR",
                    "error": str(exc),
                    "raw_row": raw_row,
                }
            )
    try:
        created, updated = await upsert_readings(db, station.id, readings)
        record.rows_created = created
        record.rows_updated = updated
        record.rows_failed = len(errors)
        record.status = "COMPLETED_WITH_ERRORS" if errors else "COMPLETED"
        record.error_report_path = _write_error_report(record.id, errors)
        for item in errors:
            db.add(
                DataImportError(
                    import_id=record.id,
                    row_number=item["row"],
                    column_name=item["column"],
                    error_code=item["code"],
                    message=item["error"],
                    raw_row=item["raw_row"],
                )
            )
        await db.commit()
    except Exception as exc:
        record.status = "FAILED"
        record.rows_failed = len(frame)
        record.error_report_path = _write_error_report(
            record.id,
            [{"row": 0, "column": None, "code": "IMPORT_FAILED", "error": str(exc), "raw_row": {}}],
        )
        await db.commit()
        raise
    return {
        "import_id": str(record.id),
        "rows_total": len(frame),
        "created": record.rows_created,
        "updated": record.rows_updated,
        "failed": record.rows_failed,
        "status": record.status,
        "errors": errors[:100],
        "error_report_available": bool(record.error_report_path),
    }
