from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.satellite as satellite_service
from app.db.models import SatelliteObservation
from app.schemas.actions import PublicSatelliteMapProcessPayload, PublicSatelliteRefreshPayload
from app.services.satellite import (
    _parse_output_statistics,
    _bounded_statistics_resolution_m,
    _quality_acceptance,
    _statistics_quality,
    _statistics_resolution_degrees,
    _statistics_time_range,
    satellite_catalog_cache_is_stale,
)


class _EmptyScalarResult:
    def all(self):
        return []


class _CandidateDb:
    async def scalars(self, _query):
        return _EmptyScalarResult()


def _polygon() -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [-55.2, -26.1],
            [-55.1, -26.1],
            [-55.1, -26.0],
            [-55.2, -26.0],
            [-55.2, -26.1],
        ]],
    }


def _runtime() -> dict:
    return {
        "scene_quality_candidates": 12,
        "max_cloud_percentage": 80,
        "provider_tile_cloud_ceiling_percentage": 90,
        "statistics_resolution_m": 10,
        "minimum_valid_pixels": 25,
        "minimum_valid_coverage_percentage": 20,
        "preferred_valid_coverage_percentage": 60,
    }


def test_statistics_time_range_uses_complete_utc_day() -> None:
    captured_at = datetime(2026, 7, 18, 13, 59, 55, tzinfo=UTC)

    start, end = _statistics_time_range(captured_at)

    assert start == datetime(2026, 7, 18, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 19, 0, 0, tzinfo=UTC)
    assert (end - start).total_seconds() == 86400


def test_statistics_resolution_converts_ten_metres_to_wgs84_degrees() -> None:
    resx, resy = _statistics_resolution_degrees(_polygon(), 10)

    assert 0.00009 < resx < 0.00011
    assert 0.00008 < resy < 0.00010
    assert resx != 10
    assert resy != 10


def test_large_station_area_uses_provider_safe_resolution() -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[-56.2, -27], [-54.2, -27], [-54.2, -25], [-56.2, -25], [-56.2, -27]]],
    }
    resolution_m = _bounded_statistics_resolution_m(geometry, 10)
    resx, resy = _statistics_resolution_degrees(geometry, resolution_m)
    assert resolution_m > 10
    assert 2 / resx <= 1200
    assert 2 / resy <= 1200
    assert _bounded_statistics_resolution_m(_polygon(), 10) == 10


def test_quality_rejects_one_pixel_grid_for_large_aoi() -> None:
    quality = {
        "status": "COMPLETED",
        "total_pixels": 1,
        "valid_pixels": 1,
        "estimated_aoi_pixels": 5000,
        "valid_coverage_percentage": 100,
    }

    accepted, reason = _quality_acceptance(quality, _runtime())

    assert accepted is False
    assert "un solo píxel" in reason


def test_quality_accepts_clear_single_pixel_for_genuinely_small_aoi() -> None:
    quality = {
        "status": "COMPLETED",
        "total_pixels": 1,
        "valid_pixels": 1,
        "estimated_aoi_pixels": 1,
        "valid_coverage_percentage": 100,
    }

    accepted, reason = _quality_acceptance(quality, _runtime())

    assert accepted is True
    assert reason == "Escena aceptada"
    assert quality["effective_minimum_valid_pixels"] == 1


def test_statistics_parser_exposes_valid_pixel_coverage() -> None:
    response = {
        "data": [
            {
                "outputs": {
                    "ndvi": {
                        "bands": {
                            "B0": {
                                "stats": {
                                    "min": 0.2,
                                    "max": 0.8,
                                    "mean": 0.6,
                                    "stDev": 0.1,
                                    "sampleCount": 100,
                                    "noDataCount": 25,
                                    "percentiles": {"50.0": 0.62},
                                }
                            }
                        }
                    }
                }
            }
        ]
    }

    stats = _parse_output_statistics(response, "ndvi")
    quality = _statistics_quality({"ndvi": stats}, interval_count=1)

    assert stats["valid_pixel_count"] == 75
    assert stats["valid_coverage_percentage"] == 75.0
    assert quality == {
        "status": "COMPLETED",
        "interval_count": 1,
        "total_pixels": 100,
        "valid_pixels": 75,
        "no_data_pixels": 25,
        "valid_coverage_percentage": 75.0,
    }


def test_quality_rejects_small_or_cloud_obscured_scene() -> None:
    runtime = {
        "minimum_valid_pixels": 25,
        "minimum_valid_coverage_percentage": 20,
    }

    accepted, reason = _quality_acceptance(
        {
            "status": "NO_VALID_PIXELS",
            "valid_pixels": 8,
            "valid_coverage_percentage": 10,
        },
        runtime,
    )

    assert accepted is False
    assert "8 píxeles" in reason


def test_quality_accepts_latest_scene_above_thresholds() -> None:
    runtime = {
        "minimum_valid_pixels": 25,
        "minimum_valid_coverage_percentage": 20,
    }

    accepted, reason = _quality_acceptance(
        {
            "status": "COMPLETED",
            "valid_pixels": 150,
            "valid_coverage_percentage": 63.5,
        },
        runtime,
    )

    assert accepted is True
    assert reason == "Escena aceptada"


def test_public_refresh_payloads_use_shared_cache_by_default() -> None:
    station_id = uuid4()

    refresh = PublicSatelliteRefreshPayload(station_id=station_id, crop_code="TRIGO")
    map_process = PublicSatelliteMapProcessPayload(station_id=station_id, crop_code="TRIGO")

    assert refresh.force is False
    assert map_process.force is False
    assert map_process.scene_id is None
    assert map_process.captured_date is None


def test_catalog_check_timestamp_prevents_repeated_provider_calls() -> None:
    observation = SatelliteObservation(
        plot_id=uuid4(),
        captured_at=datetime(2026, 7, 18, 13, 59, 55, tzinfo=UTC),
        raw_payload={"catalog_checked_at": "2026-07-25T00:00:00Z"},
    )
    runtime = {"refresh_hours": 6}

    assert satellite_catalog_cache_is_stale(
        observation,
        runtime,
        now=datetime(2026, 7, 25, 5, 59, 59, tzinfo=UTC),
    ) is False
    assert satellite_catalog_cache_is_stale(
        observation,
        runtime,
        now=datetime(2026, 7, 25, 6, 0, 1, tzinfo=UTC),
    ) is True


@pytest.mark.asyncio
async def test_candidate_scan_falls_back_to_newest_valid_scene(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_statistics(_geometry, captured_at, _max_cloud, _integration, **_kwargs):
        calls.append(captured_at.date().isoformat())
        if captured_at.date().isoformat() == "2026-07-18":
            quality = {
                "status": "NO_VALID_PIXELS",
                "valid_pixels": 0,
                "valid_coverage_percentage": 0,
            }
            ndvi = None
        else:
            quality = {
                "status": "COMPLETED",
                "valid_pixels": 120,
                "valid_coverage_percentage": 72,
            }
            ndvi = 0.64
        return {
            "indices": {
                name: (ndvi if name == "ndvi" else 0.1)
                for name in satellite_service.INDEX_NAMES
            },
            "statistics": {},
            "quality": quality,
            "statistics_time_range": {},
        }

    monkeypatch.setattr(satellite_service, "_index_statistics", fake_statistics)
    features = [
        {
            "id": "latest-cloudy",
            "properties": {
                "datetime": "2026-07-18T13:59:55Z",
                "eo:cloud_cover": 15,
            },
        },
        {
            "id": "older-valid",
            "properties": {
                "datetime": "2026-07-13T13:59:55Z",
                "eo:cloud_cover": 25,
            },
        },
    ]

    selected, stats, attempts = await satellite_service._evaluate_catalog_candidates(
        _CandidateDb(),
        SimpleNamespace(id=uuid4()),
        features,
        _polygon(),
        None,
        _runtime(),
    )

    assert selected["id"] == "older-valid"
    assert stats["indices"]["ndvi"] == 0.64
    assert [item["accepted"] for item in attempts] == [False, True]
    assert attempts[1]["quality_band"] == "PREFERRED"
    assert calls == ["2026-07-18", "2026-07-13"]


@pytest.mark.asyncio
async def test_candidate_scan_evaluates_only_newest_scene_per_day(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_statistics(_geometry, captured_at, _max_cloud, _integration, **_kwargs):
        calls.append(captured_at.isoformat())
        return {
            "indices": {name: None for name in satellite_service.INDEX_NAMES},
            "statistics": {},
            "quality": {
                "status": "NO_VALID_PIXELS",
                "total_pixels": 100,
                "valid_pixels": 0,
                "valid_coverage_percentage": 0,
            },
            "statistics_time_range": {},
        }

    monkeypatch.setattr(satellite_service, "_index_statistics", fake_statistics)
    features = [
        {"id": "tile-a", "properties": {"datetime": "2026-07-18T13:59:55Z"}},
        {"id": "tile-b", "properties": {"datetime": "2026-07-18T13:58:10Z"}},
    ]

    selected, stats, attempts = await satellite_service._evaluate_catalog_candidates(
        _CandidateDb(),
        SimpleNamespace(id=uuid4()),
        features,
        _polygon(),
        None,
        _runtime(),
    )

    assert selected is None
    assert stats is None
    assert len(attempts) == 1
    assert attempts[0]["scene_id"] == "tile-a"
    assert calls == ["2026-07-18T13:59:55+00:00"]
