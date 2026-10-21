from datetime import UTC, datetime
from math import inf, nan
from types import SimpleNamespace

import pytest

from app.services import satellite
from app.services.spectral_palettes import (
    INDEX_CLASSES, VEGETATION_COLORS, VISUAL_VERSION, WATER_COLORS, classify_index, index_legend,
)


@pytest.mark.parametrize("name", INDEX_CLASSES)
def test_boundaries_belong_to_exactly_one_class_and_invalid_data_stays_unclassified(name):
    config = INDEX_CLASSES[name]
    for position, boundary in enumerate(config["breaks"]):
        assert classify_index(name, boundary - 1e-8) == position
        assert classify_index(name, boundary) == position
        assert classify_index(name, boundary + 1e-8) == position + 1
    for value in (None, nan, inf, -inf):
        assert classify_index(name, value) is None


def test_client_ranges_and_hex_colors_are_not_auto_stretched_between_dates():
    expected = {
        "ndvi": (.3, .4, .5, .6), "ndmi": (-.2, 0, .2, .4),
        "ndwi": (-.5, -.2, 0, .3), "evi": (.15, .3, .45, .6),
        "savi": (.15, .3, .45, .6), "ndre": (.1, .2, .3, .4),
        "recl": (1, 2, 3, 4), "gndvi": (.2, .35, .5, .65), "nbr": (-.2, 0, .2, .4), "msi": (.4, .8, 1.2, 1.6),
    }
    for name, breaks in expected.items():
        legend = index_legend(name)
        assert INDEX_CLASSES[name]["breaks"] == breaks
        assert [entry["upper_inclusive"] for entry in legend["classes"][:-1]] == list(breaks)
        assert tuple(entry["color"] for entry in legend["classes"]) == (
            WATER_COLORS if name in {"ndmi", "ndwi", "msi"} else VEGETATION_COLORS
        )
        assert legend["version"] == VISUAL_VERSION
    assert "McFeeters" in index_legend("ndwi")["formula"]
    assert "Gao" in index_legend("ndmi")["formula"]


def test_visual_grid_keeps_parcel_aspect_ratio_and_provider_limits():
    geometry = {"type": "Polygon", "coordinates": [[
        [-55.2, -26.1], [-55.196, -26.1], [-55.196, -26.099], [-55.2, -26.099], [-55.2, -26.1],
    ]]}
    grid = satellite._visual_grid(geometry)
    assert grid["output_resolution_m"] == 5
    assert 3 < grid["width"] / grid["height"] < 4
    geometry["coordinates"][0] = [[-56, -27], [-54, -27], [-54, -25], [-56, -25], [-56, -27]]
    grid = satellite._visual_grid(geometry)
    assert max(grid["width"], grid["height"]) <= 2500
    assert grid["output_resolution_m"] > 5


def test_legacy_or_partial_cache_never_gets_a_new_legend_for_old_colors(monkeypatch):
    monkeypatch.setattr(satellite, "_local_index_paths", lambda _row: {"ndvi": "old.png", "ndmi": "new.png"})
    row = SimpleNamespace(raw_payload={"index_visual_versions": {"ndmi": VISUAL_VERSION}})
    metadata = satellite._visual_metadata(row)
    assert metadata["index_visual_versions"]["ndvi"] == "legacy-continuous"
    assert set(metadata["index_legends"]) == {"ndmi"}
    assert metadata["classification_warning"]
    assert satellite._current_visual_paths(row) == {"ndmi": "new.png"}
    assert satellite.versioned_index_url("/scene", "ndmi", {"ndmi": VISUAL_VERSION}).endswith(f"?v={VISUAL_VERSION}")
    assert satellite.versioned_index_url("/scene", "ndvi", {}) == "/scene/ndvi.png"


def test_resampling_preserves_categorical_cloud_mask_and_exact_aoi():
    geometry = {"type": "Polygon", "coordinates": [[
        [-55.2, -26.1], [-55.196, -26.1], [-55.196, -26.099], [-55.2, -26.1],
    ]]}
    start = datetime(2026, 10, 1, tzinfo=UTC)
    payload = satellite._process_payload(geometry, start, start, 40,
        evalscript=satellite._index_visual_evalscript("ndvi"), media_type="image/png")
    assert payload["input"]["bounds"]["geometry"] == geometry
    assert payload["input"]["data"][0]["processing"] == {"upsampling": "NEAREST", "downsampling": "NEAREST"}
    assert payload["output"]["width"] != payload["output"]["height"]


def test_recl_last_class_starts_strictly_above_four_and_preserves_source():
    assert classify_index("recl", 4) == 3
    assert classify_index("recl", 4.000001) == 4
    assert classify_index("recl", 5) == 4
    legend = index_legend("recl")
    assert legend["classes"][-1]["label"] == "> 4"
    assert "B07 / B05" in legend["formula"]
    assert "confirmado" in legend["source"]
    assert legend["formula_source"].endswith("/chl_rededge/")
    assert "B07" in satellite._index_visual_evalscript("recl")
    assert 'recl:[Math.abs(s.B05)' in satellite.INDEX_EVALSCRIPT


def test_existing_nine_index_scene_stays_available_as_refresh_fallback(monkeypatch):
    monkeypatch.setattr(satellite, "local_satellite_image_path", lambda _row: "original.jpg")
    monkeypatch.setattr(satellite, "_local_index_paths", lambda _row: {
        name: f"{name}.png" for name in satellite.INDEX_NAMES if name != "recl"
    })
    row = SimpleNamespace(raw_payload={"processing_status":"PROCESSED", "indices":{"ndvi":.6}},
                          ndvi=.6, ndmi=.1, ndwi=-.2)
    assert satellite.satellite_observation_is_complete(row)


def test_undefined_index_statistics_do_not_create_invented_values_or_invalid_json():
    import json
    result = {"data":[{"outputs":{"recl":{"bands":{"B0":{"stats":{
        "mean":nan,"min":nan,"max":inf,"stDev":nan,"sampleCount":1,"noDataCount":1,
        "percentiles":{"50.0":nan},
    }}}}}}]}
    stats = satellite._parse_output_statistics(result,"recl")
    assert all(stats[key] is None for key in ("mean","min","max","stdev"))
    assert stats["percentiles"] == {}
    json.dumps(stats, allow_nan=False)


@pytest.mark.parametrize("has_recl", [False, True])
async def test_statistics_cache_is_refreshed_only_when_new_index_is_missing(monkeypatch, has_recl):
    names = [name for name in satellite.INDEX_NAMES if has_recl or name != "recl"]
    quality = {"valid_pixels": 100, "total_pixels": 100, "valid_coverage_percentage": 100}
    row = SimpleNamespace(raw_payload={"scene_id": "scene", "quality": quality,
        "indices": {name: .5 for name in names}, "statistics": {name: {"mean": .5} for name in names}},
        ndvi=.5, ndmi=.5, ndwi=.5)

    class Database:
        async def scalars(self, _query):
            return SimpleNamespace(all=lambda: [row])

    calls = []

    async def process(*_args, **_kwargs):
        calls.append(True)
        return {"indices": {name: .5 for name in satellite.INDEX_NAMES}, "quality": quality}

    monkeypatch.setattr(satellite, "_index_statistics", process)
    from uuid import uuid4
    feature = {"id": "scene", "properties": {"datetime": "2026-10-20T12:00:00Z", "eo:cloud_cover": 0}}
    runtime = {"scene_quality_candidates": 1, "provider_tile_cloud_ceiling_percentage": 90,
               "statistics_resolution_m": 10, "minimum_valid_pixels": 25,
               "minimum_valid_coverage_percentage": 20, "preferred_valid_coverage_percentage": 60}
    selected, stats, _ = await satellite._evaluate_catalog_candidates(
        Database(), SimpleNamespace(id=uuid4()), [feature], {}, None, runtime)
    assert selected == feature
    assert "recl" in stats["indices"]
    assert len(calls) == (0 if has_recl else 1)
