"""72-hour semantics, sensor independence and bounded, non-WebGL map rasters."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from PIL import Image

from app.services import map_terrain, regional_map

END = datetime(2026, 10, 30, 12, tzinfo=UTC)
POLYGON = {"type": "Polygon", "coordinates": [[[-56, -26], [-54, -26], [-54, -24], [-56, -24], [-56, -26]]]}


def station(**values):
    data = dict(
        id=uuid4(),
        name="Estación de prueba",
        latitude=-25,
        longitude=-55,
        altitude_m=100,
        influence_geojson=POLYGON,
        influence_radius_m=1000,
        timezone="UTC",
        expected_interval_minutes=60,
        external_guid=None,
        integration_config={
            "water_aggregation": {"rain_mm": "daily_counter", "evapotranspiration_mm": "interval_sum"}
        },
    )
    return SimpleNamespace(**(data | values))


def readings():
    result = []
    for i in range(75):
        at = END - timedelta(hours=74 - i)
        data = {s["field"]: None for s in regional_map.VARIABLES.values()}
        data.update(
            temperature_c=25,
            humidity_pct=None,
            rain_mm=float(at.hour),
            soil_moisture_pct=88,
            evapotranspiration_mm=0.2,
            wind_speed_ms=0,
        )
        result.append(SimpleNamespace(sampled_at=at, **data))
    return result


def test_counter_baseline_midday_and_midnight_resets():
    rows = readings()
    s = station()
    # First partial day = 23 - 12, then 23 + 23, final partial day = 12.
    # Hourly reporting leaves the last hour before each reset unmeasured.
    value, status = regional_map.cumulative_window(rows, s, "rain_mm", END - timedelta(hours=72), END)
    assert (value, status) == (69, "AVAILABLE")
    s.integration_config["water_aggregation"]["rain_mm"] = "interval_sum"
    assert regional_map.cumulative_window(rows, s, "rain_mm", END - timedelta(hours=72), END)[0] == 828


def test_no_humidity_does_not_remove_rain_temperature_or_calm_wind():
    result = regional_map.station_summary(station(), readings(), END)
    assert result["values"]["temperature"] == 25
    assert result["values"]["humidity"] is None
    assert result["statuses"]["humidity"] == "NOT_REPORTED"
    assert result["values"]["rain"] == 69
    assert result["values"]["wind"] == 0
    assert result["values"]["et"] == 14.4


@pytest.mark.parametrize(
    "change,reason",
    [
        ("history", "INCOMPLETE_HISTORY"),
        ("decrease", "COUNTER_INCONSISTENT"),
    ],
)
def test_invalid_counter_never_painted_as_zero(change, reason):
    rows = readings()
    if change == "history":
        rows = rows[::2]
    if change == "decrease":
        rows[-2].rain_mm = 0
    assert regional_map.cumulative_window(rows, station(), "rain_mm", END - timedelta(hours=72), END) == (
        None,
        reason,
    )


def test_unknown_aggregation_and_soil_units_are_independent():
    s = station(integration_config={"soil_moisture_unit": "cb"})
    result = regional_map.station_summary(s, readings(), END)
    assert result["values"]["rain"] is None
    assert result["statuses"]["rain"] == "AGGREGATION_UNCONFIRMED"
    assert result["values"]["soil_moisture"] is None
    assert result["values"]["soil_tension"] == 88
    assert result["values"]["temperature"] == 25
    s.integration_config = {}
    s.external_guid = "C2694D32-5A4B-48C0-A3B6-8345D9E4935B"
    assert regional_map.station_summary(s, readings(), END)["values"]["soil_tension"] == 88


def test_stale_duplicate_nonfinite_and_future_samples():
    rows = readings()
    rows.append(rows[-1])
    rows.append(
        SimpleNamespace(**(vars(rows[-1]) | {"sampled_at": END + timedelta(days=1), "temperature_c": 999}))
    )
    result = regional_map.station_summary(station(), rows, END)
    assert result["samples"] == 72 and result["values"]["temperature"] == 25
    assert (
        regional_map.station_summary(station(), rows[:-4], END + timedelta(hours=5))["statuses"]["rain"]
        == "STALE"
    )
    for r in rows:
        r.humidity_pct = float("nan")
    assert regional_map.station_summary(station(), rows, END)["values"]["humidity"] is None


def test_idw_inside_only_no_missing_sensor_contribution_and_optional_lapse_rate():
    first = regional_map.station_summary(station(), readings(), END)
    second = dict(first, longitude=-54.5, values=dict(first["values"], temperature=35))
    missing = dict(first, values=dict(first["values"], temperature=None))
    snapshot = {"stations": [first, second, missing]}
    lon = np.array([[-55, -54.5, -53]])
    lat = np.array([[-25, -25, -25]])
    values, mask = regional_map.interpolate(snapshot, "temperature", lon, lat)
    assert mask.tolist() == [[True, True, False]]
    assert values[0, 0] == pytest.approx(25, abs=0.001)
    assert values[0, 1] == pytest.approx(35, abs=0.001)
    adjusted, _ = regional_map.interpolate(snapshot, "temperature", lon, lat, np.full(lon.shape, 1100), True)
    assert adjusted[0, 0] == pytest.approx(values[0, 0] - 6.5)
    _, mask = regional_map.interpolate(snapshot, "humidity", lon, lat)
    assert not mask.any()


def test_terrarium_decode_exact_units_and_terrain_png():
    rgb = np.zeros((256, 256, 3), dtype=np.uint8)
    rgb[:] = [128, 100, 128]
    heights = map_terrain.decode_terrarium(map_terrain.png(rgb))
    assert heights[0, 0] == 100.5
    for kind in ["altitude", "slope", "aspect", "hillshade"]:
        with Image.open(BytesIO(map_terrain.terrain_image(heights, kind, 8, 128))) as image:
            assert image.size == (256, 256)
            assert image.mode == "RGBA"
            if kind == "aspect":
                assert np.asarray(image)[:, :, 3].max() == 0


@pytest.mark.asyncio
async def test_climate_raster_is_transparent_outside_any_influence():
    snapshot = {"stations": [], "variables": [{"id": "rain", "minimum": 0, "maximum": 100}]}
    with Image.open(BytesIO(await regional_map.climate_tile(snapshot, "rain", 6, 22, 36))) as image:
        assert image.size == (256, 256)
        assert np.asarray(image)[:, :, 3].max() == 0


@pytest.mark.asyncio
async def test_source_cache_and_size_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(map_terrain.settings, "storage_path", tmp_path)
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b"terrain")

    monkeypatch.setattr(
        map_terrain.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    assert await map_terrain.cached_download("https://source.test/data", "test/data") == b"terrain"
    assert await map_terrain.cached_download("https://source.test/data", "test/data") == b"terrain"
    assert len(requests) == 1
    with pytest.raises(ValueError):
        await map_terrain.cached_download("https://source.test/large", "test/large", max_bytes=3)
    assert not (map_terrain.cache_dir() / "test/large").exists()


@pytest.mark.asyncio
async def test_open_source_failure_remains_a_safe_map_response(monkeypatch, tmp_path):
    from app.api.endpoints import regional_map as endpoints
    from app.api.endpoints.agent_public import _rate_windows

    _rate_windows.clear()
    monkeypatch.setattr(map_terrain.settings, "storage_path", tmp_path)
    original = httpx.AsyncClient

    def fail(request):
        return httpx.Response(503, text="private provider diagnostics")

    monkeypatch.setattr(
        map_terrain.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(fail), **kw)
    )
    app = FastAPI()
    app.include_router(endpoints.router)
    async with original(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/public/map/terrain/slope/8/80/140.png")
        assert response.status_code == 503 and "private" not in response.text
        assert (await client.get("/public/map/terrain/slope/20/0/0.png")).status_code == 404
        assert (
            await client.get("/public/map/rivers?west=-180&south=-85&east=180&north=85")
        ).status_code == 422
        response = await client.get("/public/map/rivers?west=-56&south=-26&east=-54&north=-24")
        assert response.status_code == 503 and "private" not in response.text


def test_short_opening_fraction_is_explicitly_partial_not_a_missing_sensor():
    end = END.replace(hour=23, minute=57)
    rows = readings()
    rows = [SimpleNamespace(**(vars(r) | {"sampled_at": r.sampled_at + timedelta(hours=11)})) for r in rows]
    # Rebuild daily counters after moving the sample timestamps.
    for r in rows:
        r.rain_mm = float(r.sampled_at.hour)
    value, status = regional_map.cumulative_window(rows, station(), "rain_mm", end - timedelta(hours=72), end)
    assert value == 69 and status == "PARTIAL_BOUNDARY"
    start = end - timedelta(hours=72)
    missing = [r for r in rows if r.sampled_at > start]
    assert regional_map.cumulative_window(missing, station(), "rain_mm", start, end) == (
        None,
        "INCOMPLETE_HISTORY",
    )


def test_daily_reset_reading_at_midnight_is_available_in_the_current_day():
    end = END.replace(hour=0, minute=3)
    rows = readings()
    for r in rows:
        r.sampled_at -= timedelta(hours=12)
        r.rain_mm = float(r.sampled_at.hour)
    assert regional_map.cumulative_window(rows, station(), "rain_mm", end - timedelta(hours=72), end) == (
        69,
        "AVAILABLE",
    )


def test_counter_without_opening_reading_keeps_only_observed_increase():
    rows = [r for r in readings() if r.sampled_at > END - timedelta(hours=72)]
    assert regional_map.cumulative_window(rows, station(), "rain_mm", END - timedelta(hours=72), END) == (
        68,
        "PARTIAL_BOUNDARY",
    )


def test_counter_stops_at_latest_observation_before_a_midnight_gap():
    end = END.replace(hour=0, minute=3)
    rows = readings()
    for r in rows:
        r.sampled_at -= timedelta(hours=12)
        r.rain_mm = float(r.sampled_at.hour)
    rows = [r for r in rows if r.sampled_at < end.replace(minute=0)]
    assert regional_map.cumulative_window(rows, station(), "rain_mm", end - timedelta(hours=72), end) == (
        69,
        "PARTIAL_BOUNDARY",
    )


def test_relative_color_scale_matches_observations_without_amplifying_tiny_differences():
    rows = [{"values": {"temperature": 24.1}}, {"values": {"temperature": 27.4}}]
    spec = next(s for s in regional_map.map_variables(rows) if s["id"] == "temperature")
    assert spec["minimum"] == 24.1 and spec["maximum"] == 27.4
    assert spec["spread"] == pytest.approx(3.3)
    tiny = next(
        s
        for s in regional_map.map_variables(
            [{"values": {"temperature": 25.001}}, {"values": {"temperature": 25.002}}]
        )
        if s["id"] == "temperature"
    )
    assert tiny["maximum"] - tiny["minimum"] == pytest.approx(1)
    assert tiny["small_difference"]
    zero = next(
        s
        for s in regional_map.map_variables([{"values": {"rain": 0}}, {"values": {"rain": None}}])
        if s["id"] == "rain"
    )
    assert zero["available_stations"] == 1 and zero["minimum"] == 0


def test_regional_trend_interpolates_gaps_and_does_not_extrapolate_beyond_stations():
    from shapely.geometry import Point, mapping

    def point(lon, lat, value):
        return {
            "longitude": lon,
            "latitude": lat,
            "values": {"temperature": value, "humidity": None},
            "influence_geojson": mapping(Point(lon, lat).buffer(0.005)),
        }

    snapshot = {"stations": [point(-55, -25, 20), point(-54.6, -25, 30), point(-54.8, -24.6, 25)]}
    lon = np.array([[-54.95, -54.65, -53, -54.8]])
    lat = np.array([[-24.98, -24.98, -25, -24.85]])
    old, old_mask = regional_map.interpolate(snapshot, "temperature", lon, lat)
    values, mask = regional_map.interpolate(snapshot, "temperature", lon, lat, regional=True)
    assert not old_mask.any()  # All test points lie between the small influence areas.
    assert mask.tolist() == [[True, True, False, True]]
    assert 20 < values[0, 0] < values[0, 1] < 30
    assert values[0, 3] == pytest.approx(25, abs=0.01)
    assert not regional_map.interpolate(snapshot, "humidity", lon, lat, regional=True)[1].any()


def test_regional_trend_fades_in_long_unobserved_gaps():
    snapshot = {
        "stations": [
            {"longitude": lon, "latitude": lat, "values": {"temperature": 25}}
            for lon, lat in [(-60, -20), (-50, -20), (-55, -30)]
        ]
    }
    _, opacity = regional_map.interpolate_region(
        snapshot, "temperature", np.array([[-55.0]]), np.array([[-23.0]]), return_opacity=True
    )
    assert 0 <= opacity[0, 0] < .01


def test_two_stations_keep_their_measured_influence_areas():
    from shapely.geometry import Point, mapping

    snapshot = {
        "stations": [
            {
                "longitude": lon,
                "latitude": -25,
                "values": {"temperature": value},
                "influence_geojson": mapping(Point(lon, -25).buffer(0.01)),
            }
            for lon, value in [(-55, 20), (-54.5, 30)]
        ]
    }
    _, mask = regional_map.interpolate(
        snapshot, "temperature", np.array([[-55.0, -54.75]]), np.array([[-25.0, -25.0]]), regional=True
    )
    assert mask.tolist() == [[True, False]]


def test_regional_surface_and_alpha_have_no_hard_cut_at_one_hundred_km():
    snapshot = {"stations": [{"longitude": lon, "latitude": lat, "values": {"temperature": value}}
                for lon, lat, value in [(-56,-25,20),(-52,-25,30),(-54,-21,25)]]}
    lon=np.array([[-55.,-55.001,-55.002]])
    lat=np.array([[-24.1,-24.1,-24.1]])
    values, alpha=regional_map.interpolate_region(snapshot,"temperature",lon,lat,return_opacity=True)
    assert np.max(np.abs(np.diff(values))) < .1
    assert np.max(np.abs(np.diff(alpha))) < .01
    assert (alpha>0).all() and (alpha<1).all()
