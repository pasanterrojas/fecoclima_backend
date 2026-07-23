from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Station, WeatherReading

EXPORT_COLUMNS = [
    "sampled_at",
    "source",
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
    "soil_temperature_c",
    "soil_moisture_pct",
    "leaf_sensor_value",
    "quality_flags",
]


async def load_readings(db: AsyncSession, station_id: UUID, date_from: date, date_to: date) -> tuple[Station, list[dict]]:
    station = await db.get(Station, station_id)
    if not station:
        raise ValueError("Estación no encontrada")
    start = datetime.combine(date_from, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(date_to, datetime.max.time(), tzinfo=UTC)
    rows = list(
        (
            await db.scalars(
                select(WeatherReading)
                .where(
                    WeatherReading.station_id == station_id,
                    WeatherReading.sampled_at >= start,
                    WeatherReading.sampled_at <= end,
                )
                .order_by(WeatherReading.sampled_at)
            )
        ).all()
    )
    data = []
    for row in rows:
        item = {column: getattr(row, column) for column in EXPORT_COLUMNS}
        item["sampled_at"] = row.sampled_at.isoformat()
        item["quality_flags"] = ";".join(row.quality_flags or [])
        data.append(item)
    return station, data


async def export_readings(db: AsyncSession, station_id: UUID, date_from: date, date_to: date, fmt: str) -> dict:
    station, data = await load_readings(db, station_id, date_from, date_to)
    fmt = fmt.lower()
    if fmt not in {"csv", "xlsx"}:
        raise ValueError("Formato no soportado; use csv o xlsx")
    export_dir = settings.storage_path / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".csv" if fmt == "csv" else ".xlsx"
    filename = f"{station.code}_{date_from.isoformat()}_{date_to.isoformat()}_{uuid4().hex[:8]}{suffix}"
    path = export_dir / filename
    if fmt == "csv":
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(data)
        mime_type = "text/csv"
    else:
        frame = pd.DataFrame(data, columns=EXPORT_COLUMNS)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name="Lecturas")
            metadata = pd.DataFrame(
                [
                    ["Estación", station.name],
                    ["Código", station.code],
                    ["Fecha desde", date_from.isoformat()],
                    ["Fecha hasta", date_to.isoformat()],
                    ["Generado UTC", datetime.now(UTC).isoformat()],
                    ["Registros", len(data)],
                ],
                columns=["Campo", "Valor"],
            )
            metadata.to_excel(writer, index=False, sheet_name="Metadatos")
        mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "filename": filename,
        "path": str(path),
        "mime_type": mime_type,
        "size_bytes": path.stat().st_size,
        "checksum_sha256": checksum,
        "records": len(data),
    }
