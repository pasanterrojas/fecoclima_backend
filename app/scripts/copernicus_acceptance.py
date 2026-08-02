from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.core.logging import configure_logging
from app.db.models import Crop, Station
from app.db.session import AsyncSessionLocal
from app.services.satellite import (
    INDEX_NAMES,
    latest_satellite_observation,
    local_satellite_image_path,
    local_satellite_index_image_path,
    satellite_observation_payload,
    sync_satellite_for_station,
)


@dataclass
class Result:
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, **details: Any) -> None:
        self.checks.append({"name": name, "ok": ok, **details})

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)

    def payload(self) -> dict[str, Any]:
        return {"status": "PASSED" if self.ok else "FAILED", "checks": self.checks}


async def run(args: argparse.Namespace) -> Result:
    result = Result()
    station_id = UUID(args.station_id)

    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("SELECT 1"))
            result.add("database", True)
        except Exception as exc:
            result.add("database", False, error=str(exc)[:1000])
            return result

        station = await db.scalar(
            select(Station).where(
                Station.id == station_id,
                Station.active.is_(True),
                Station.deleted_at.is_(None),
            )
        )
        if not station:
            result.add("station", False, error="La estación no existe o no está activa")
            return result
        result.add("station", True, station=station.name, station_id=str(station.id))

        crop = None
        if args.crop_code:
            crop = await db.scalar(
                select(Crop).where(
                    Crop.code == args.crop_code.upper(),
                    Crop.active.is_(True),
                    Crop.deleted_at.is_(None),
                )
            )
            if not crop:
                result.add("crop", False, error=f"No existe el cultivo {args.crop_code}")
                return result
            result.add("crop", True, crop=crop.name, crop_code=crop.code)

        try:
            refresh = await sync_satellite_for_station(
                db,
                station.id,
                crop_id=crop.id if crop else None,
                force=args.force_provider,
            )
            result.add(
                "refresh",
                refresh.get("status") == "PROCESSED",
                status=refresh.get("status"),
                observation_id=refresh.get("observation_id"),
                captured_at=refresh.get("captured_at"),
                data_source_mode=refresh.get("data_source_mode"),
                fallback_used=refresh.get("fallback_used"),
                data_warning=refresh.get("data_warning"),
                quality=refresh.get("quality"),
                candidate_checks=refresh.get("candidate_checks"),
                processing_error=refresh.get("processing_error"),
            )
        except Exception as exc:
            result.add("refresh", False, error=str(exc)[:3000])
            return result

        plot, observation = await latest_satellite_observation(
            db,
            station.id,
            crop.id if crop else None,
        )
        if not plot or not observation:
            result.add("stored_observation", False, error="No se encontró una observación persistida")
            return result

        payload = await satellite_observation_payload(db, observation, plot)
        indices = payload.get("indices") or {}
        missing_indices = [name for name in INDEX_NAMES if indices.get(name) is None]
        missing_layers = [
            name
            for name in INDEX_NAMES
            if local_satellite_index_image_path(observation, name) is None
        ]
        quality = payload.get("quality") or {}
        result.add(
            "stored_observation",
            payload.get("processing_status") == "PROCESSED",
            processing_status=payload.get("processing_status"),
            scene_id=payload.get("scene_id"),
            captured_at=payload.get("captured_at"),
        )
        result.add(
            "numeric_indices",
            not missing_indices,
            available=len(INDEX_NAMES) - len(missing_indices),
            expected=len(INDEX_NAMES),
            missing=missing_indices,
            values=indices,
        )
        result.add(
            "visual_products",
            local_satellite_image_path(observation) is not None and not missing_layers,
            rgb_available=local_satellite_image_path(observation) is not None,
            available_layers=len(INDEX_NAMES) - len(missing_layers),
            expected_layers=len(INDEX_NAMES),
            missing_layers=missing_layers,
        )
        result.add(
            "valid_pixel_quality",
            int(quality.get("valid_pixels") or 0) > 0
            and float(quality.get("valid_coverage_percentage") or 0) > 0,
            **quality,
        )
        result.add(
            "widget_freshness",
            bool(payload.get("catalog_checked_at")),
            data_source_mode=payload.get("data_source_mode"),
            fallback_used=payload.get("fallback_used"),
            data_age_days=payload.get("data_age_days"),
            data_is_stale=payload.get("data_is_stale"),
            latest_catalog_scene_at=payload.get("latest_catalog_scene_at"),
            valid_scene_at=payload.get("captured_at"),
            data_warning=payload.get("data_warning"),
        )

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prueba de aceptación del producto Sentinel-2 mostrado por FECOCLIMA IA."
    )
    parser.add_argument("--station-id", required=True, help="UUID de una estación activa")
    parser.add_argument("--crop-code", default=None, help="Código opcional: TRIGO, SOJA o MAIZ")
    parser.add_argument(
        "--force-provider",
        action="store_true",
        help="Ignora el caché una vez y ejecuta una consulta real a Copernicus",
    )
    return parser.parse_args()


async def main() -> int:
    result = await run(parse_args())
    print(json.dumps(result.payload(), ensure_ascii=False, indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    configure_logging()
    raise SystemExit(asyncio.run(main()))
