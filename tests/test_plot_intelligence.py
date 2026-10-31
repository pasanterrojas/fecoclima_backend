from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import numpy as np
import pytest
from jinja2.exceptions import SecurityError
from pydantic import ValidationError
from shapely.geometry import Point, shape

from app.core.config import settings
from app.db.models import Station, WeatherReading
from app.schemas.plots import ParcelPayload, PlotAlertPayload
from app.schemas.resources import AgentPayload, StationPayload
from app.services import ai
from app.services.geospatial import (
    calculate_influences,
    coverage_percentage,
    parcel_metrics,
    validate_polygon,
)
from app.services.plot_alerts import compile_rule, matches_rule
from app.services.plot_pipeline import historical_regression, rolling_windows, next_three_forecast_days
from app.services.plot_reporting import blocked_resource, plot_pdf_bytes, render_plot_html
from app.services.plot_satellite import inspection_point, pixel_inspection_point

POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-55.01, -25.01], [-55, -25.01], [-55, -25], [-55.01, -25], [-55.01, -25.01]]],
}
NOW = datetime(2026, 10, 19, 18, tzinfo=UTC)


def test_forecast_uses_only_the_next_three_dates_without_inventing_missing_days():
    today = NOW.date()
    days = [{'date': (today + timedelta(days=offset)).isoformat(), 'precipitation_mm': offset}
            for offset in (4, -1, 3, 0, 1, 2, 2)]
    result = next_three_forecast_days(days, today)
    assert [day['precipitation_mm'] for day in result] == [1, 2, 3]
    assert len(next_three_forecast_days(result, today + timedelta(days=1))) == 2


def station(**kwargs):
    return Station(
        id=uuid4(),
        name="Estación",
        code=str(uuid4()),
        active=True,
        deleted_at=None,
        latitude=-25.005,
        longitude=-55.005,
        influence_radius_m=2000,
        expected_interval_minutes=60,
        timezone="America/Asuncion",
        leaf_sensor_config={"mode": "hours"},
        **kwargs,
    )


def reading(at, **kwargs):
    defaults = dict(
        sampled_at=at,
        temperature_c=25,
        humidity_pct=85,
        rain_mm=1,
        soil_temperature_c=22,
        soil_moisture_pct=65,
        leaf_sensor_value=12,
        solar_wm2=0,
        evapotranspiration_mm=None,
    )
    return WeatherReading(**{**defaults, **kwargs})


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [-55, -25]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [999, 1], [1, 0], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [float("nan"), 1], [1, 0], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [True, 1], [1, 0], [0, 0]]]},
    ],
)
def test_invalid_geometry_is_rejected(geometry):
    with pytest.raises(ValueError):
        validate_polygon(geometry)


def test_influence_requires_real_intersection_and_active_station():
    inside = station()
    outside = station()
    outside.longitude = -57
    disabled = station()
    disabled.active = False
    rows = calculate_influences(POLYGON, [outside, disabled, inside])
    assert len(rows) == 1
    assert rows[0]["station_id"] == str(inside.id)
    assert rows[0]["coverage_pct"] == pytest.approx(100)
    assert coverage_percentage(POLYGON, rows) == pytest.approx(100)
    assert calculate_influences(POLYGON, [outside]) == []


def test_partial_intersections_union_does_not_double_count():
    left = station()
    left.influence_geojson = {
        "type": "Polygon",
        "coordinates": [
            [[-55.02, -25.02], [-55.005, -25.02], [-55.005, -24.99], [-55.02, -24.99], [-55.02, -25.02]]
        ],
    }
    other = station()
    other.influence_geojson = left.influence_geojson
    rows = calculate_influences(POLYGON, [left, other])
    assert len(rows) == 2
    assert coverage_percentage(POLYGON, rows) == pytest.approx(50, abs=0.1)


def test_holes_and_interior_reference_are_respected():
    geometry = {
        "type": "Polygon",
        "coordinates": [
            POLYGON["coordinates"][0],
            [
                [-55.007, -25.007],
                [-55.003, -25.007],
                [-55.003, -25.003],
                [-55.007, -25.003],
                [-55.007, -25.007],
            ],
        ],
    }
    metrics = parcel_metrics(validate_polygon(geometry))
    assert metrics["area_ha"] > 0
    assert shape(geometry).covers(Point(metrics["longitude"], metrics["latitude"]))
    assert metrics["area_ha"] < parcel_metrics(POLYGON)["area_ha"]


def test_windows_have_exact_bounds_deduplicate_and_reject_future():
    rows = [reading(NOW - timedelta(hours=i)) for i in range(121)]
    rows += [rows[0], reading(NOW + timedelta(minutes=1), temperature_c=1000)]
    windows = rolling_windows(rows, station(), NOW)
    assert [windows[str(h)]["samples"] for h in (72, 96, 120)] == [72, 96, 120]
    assert windows["72"]["values"]["rain_sum_mm"] == 72
    assert windows["72"]["rule_values"]["rain_sum_mm"] == 24
    assert windows["72"]["values"]["temperature_avg_c"] == 25
    assert windows["72"]["completeness_pct"] == 100
    assert windows["72"]["stale"] is False


def test_empty_and_stale_windows_never_invent_values():
    empty = rolling_windows([], station(), NOW)["72"]
    assert empty["values"]["rain_sum_mm"] is None
    assert empty["stale"] is True
    old = rolling_windows([reading(NOW - timedelta(days=2))], station(), NOW)["72"]
    assert old["stale"] is True
    assert old["to"] == NOW.isoformat()


@pytest.mark.parametrize('hours', [60, 72, 96, 120])
def test_selected_window_has_only_requested_bounds_and_samples(hours):
    rows = [reading(NOW - timedelta(hours=i)) for i in range(121)]
    windows = rolling_windows(rows, station(), NOW, window_hours=hours)
    assert set(windows) == {str(hours)}
    assert windows[str(hours)]['samples'] == hours
    assert windows[str(hours)]['from'] == (NOW - timedelta(hours=hours)).isoformat()


def test_selected_window_rejects_unsupported_duration():
    with pytest.raises(ValueError, match='Período'):
        rolling_windows([], station(), NOW, window_hours=48)


def test_regression_uses_completed_days_and_reports_insufficient_history():
    assert historical_regression([], NOW, "UTC")["status"] == "INSUFFICIENT_HISTORY"
    rows = [
        reading(
            NOW.replace(hour=0) - timedelta(days=day) + timedelta(hours=h),
            temperature_c=30 - day,
            humidity_pct=50,
        )
        for day in range(1, 9)
        for h in range(24)
    ]
    result = historical_regression(rows, NOW, "UTC")
    assert result["status"] == "COMPLETED"
    assert result["days"][0]["values"]["temperature_c"] == pytest.approx(31)
    sparse = historical_regression(rows[::24], NOW, "UTC")
    assert sparse["status"] == "INSUFFICIENT_HISTORY"


def test_spanish_rule_compiles_units_connectors_and_window():
    result = compile_rule("Avísame si la humedad supera 80% y la temperatura es mayor a 25°C")
    assert result["mode"] == "all" and result["scope"] == "latest"
    assert result["conditions"][0]["variable"] == "humidity_pct"
    assert compile_rule("humedad >= 80 en 96 horas")["scope"] == "96"
    assert compile_rule("temp baja de 10 o temp supera 35")["mode"] == "any"
    assert compile_rule("temperatura es menor que 12,5 °C")["conditions"][0]["value"] == 12.5


@pytest.mark.parametrize(
    "text",
    [
        "humedad supera 120",
        "temp supera 20 mm",
        "temp supera 20 y humedad supera 80 o lluvia supera 3",
        "temp supera 20; import os",
        "humedad supera X y temp Y",
        "viento supera 5 en 72 horas",
        "lluvia supera 20 en 120 horas",
    ],
)
def test_ambiguous_or_unsupported_rules_are_rejected(text):
    with pytest.raises(ValueError):
        compile_rule(text)


def test_alert_evaluation_does_not_mix_stations_or_use_stale_readings():
    expression = compile_rule("humedad supera 80 y temp supera 25")
    context = {
        "reference_at": NOW.isoformat(),
        "raw_readings": [
            {"reading": {"sampled_at": NOW.isoformat(), "humidity_pct": 95, "temperature_c": 20}},
            {"reading": {"sampled_at": NOW.isoformat(), "humidity_pct": 60, "temperature_c": 30}},
        ],
    }
    assert not matches_rule(expression, context)
    context["raw_readings"][0]["reading"]["temperature_c"] = 30
    assert matches_rule(expression, context)
    context["raw_readings"][0]["reading"]["sampled_at"] = (NOW - timedelta(hours=2)).isoformat()
    assert not matches_rule(expression, context)


def test_satellite_point_is_inside_polygon_and_invalid_pixels_are_skipped():
    pixels = np.array([[np.nan, 0.4], [0.2, 0.7]])
    point = pixel_inspection_point(pixels, POLYGON)
    assert point["ndvi"] == 0.2
    assert shape(POLYGON).covers(Point(point["longitude"], point["latitude"]))
    assert point["method"] == "lowest_valid_ndvi_pixel"
    reference = inspection_point(POLYGON, None)
    assert reference["method"] == "interior_reference_only"
    assert pixel_inspection_point(np.full((2, 2), np.nan), POLYGON) is None


def test_parcel_and_provider_configuration_are_validated():
    with pytest.raises(ValidationError):
        ParcelPayload(name="Parcela", crop_id=uuid4(), planting_date=date.today(), geojson={"type": "Point"})
    for provider in ("OPENAI", "EDGE"):
        assert (
            AgentPayload(name="Orquestador", code="ORCH", purpose="ORCHESTRATOR", provider=provider).provider
            == provider
        )
    with pytest.raises(ValidationError):
        AgentPayload(name="Prueba", code="P", purpose="ORCHESTRATOR", provider="UNKNOWN")
    with pytest.raises(ValidationError):
        PlotAlertPayload(natural_language="temp supera 20", channel="EMAIL", recipient="not-an-email")
    assert StationPayload(
        name="Estación", code="A1", endpoint_url="https://example.test/{guid}/{year}/{month_from}/{month_to}"
    )
    with pytest.raises(ValidationError):
        StationPayload(name="Estación", code="A1", endpoint_url="https://key:secret@example.test/")


async def test_edge_routing_never_uses_cloud_credentials(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Respuesta Edge"}}]})

    monkeypatch.setattr(ai.httpx, "AsyncClient", Client)
    monkeypatch.setattr(settings, "edge_ai_base_url", "http://edge.test/v1")
    monkeypatch.setattr(settings, "edge_ai_api_key", None)
    monkeypatch.setattr(settings, "ai_api_key", "cloud-secret-test")
    result = await ai.generate_markdown_response("Interpretar", {}, "Consulta", "gemma3", provider="EDGE")
    assert result["fallback"] is False
    assert captured["url"] == "http://edge.test/v1/chat/completions"
    assert "Authorization" not in captured["headers"]
    assert "max_tokens" in captured["payload"] and "max_completion_tokens" not in captured["payload"]
    assert "Nunca realices operaciones matemáticas" in captured["payload"]["messages"][0]["content"]


def test_pdf_templates_are_escaped_and_cannot_read_files_or_calculate():
    assert "&lt;script&gt;" in render_plot_html({"title": "<script>"}, "", "{{ context.title }}")
    with pytest.raises(ValueError):
        render_plot_html({}, "", "{{ 2 * 7 }}")
    with pytest.raises(SecurityError):
        render_plot_html({}, "", "{{ context.__class__.__mro__ }}")
    with pytest.raises(ValueError):
        blocked_resource("file:///etc/passwd")
    pdf = plot_pdf_bytes(
        {"name": "Lote 1"}, "Revisar el cultivo", "<h1>{{ context.name }}</h1><p>{{ narrative }}</p>"
    )
    assert pdf.startswith(b"%PDF-") and len(pdf) > 500


def test_sensor_units_are_not_silently_converted_to_percent():
    from app.services.alert_engine import ConditionSpec
    from app.services.plot_pipeline import evaluate_window

    device = station()
    device.integration_config = {"soil_moisture_unit": "cb"}
    readings = [reading(NOW - timedelta(hours=i)) for i in range(120)]
    window = rolling_windows(readings, device, NOW)["72"]
    variables = [
        "temperature_avg_c",
        "humidity_avg_pct",
        "rain_sum_mm",
        "soil_temperature_avg_c",
        "soil_moisture_avg_pct",
        "leaf_wetness_hours",
    ]
    specs = [
        ConditionSpec(
            key=str(i),
            label=variable,
            variable=variable,
            operator="gt",
            threshold_value=1,
            unit="%" if "moisture" in variable else None,
        )
        for i, variable in enumerate(variables)
    ]
    result = evaluate_window(window, specs, [], {"stage_code": "V1"}, SimpleNamespace(configuration={}))
    # Five measured signals already satisfy the published grave threshold;
    # the incompatible soil measurement remains missing, never converted.
    assert result["level"] == "GRAVE"
    assert result['assessment_status'] == 'PARTIAL'
    assert result["values_used"]["soil_moisture_avg_pct"] is None
    expression = compile_rule("humedad del suelo supera 50%")
    context = {
        "reference_at": NOW.isoformat(),
        "raw_readings": [
            {
                "sensor_units": {"soil_moisture_pct": "cb"},
                "reading": {"sampled_at": NOW.isoformat(), "soil_moisture_pct": 80},
            }
        ],
    }
    assert not matches_rule(expression, context)


def test_chat_images_are_durable_private_and_reused(monkeypatch, tmp_path):
    from app.services.plot_chat import public_messages, recent_image_urls, store_images

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    attachments = store_images(
        [{"filename": "hoja.png", "content_type": "image/png", "content": b"test-png"}], uuid4(), uuid4(), uuid4()
    )
    history = [{"role": "user", "content": "¿Qué ves?", "images": attachments}]
    assert len(recent_image_urls(history, [])) == 1
    assert "storage_path" not in public_messages(history)[0]["images"][0]
    assert recent_image_urls([], [{"content_type": "image/png", "storage_path": "/etc/passwd"}]) == []


async def test_radar_request_uses_radar_bands_and_exact_parcel(monkeypatch):
    from app.services import plot_satellite

    captured = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            captured.update(json)
            return httpx.Response(200, content=b"tiff-placeholder", request=httpx.Request("POST", url))

    monkeypatch.setattr(plot_satellite, "_access_token", AsyncMock(return_value="test-token"))
    monkeypatch.setattr(plot_satellite.httpx, "AsyncClient", Client)
    await plot_satellite._raster(POLYGON, NOW, radar=True, integration=None)
    assert captured["input"]["bounds"]["geometry"] == POLYGON
    assert captured["input"]["data"][0]["type"] == "sentinel-1-grd"
    assert captured["input"]["data"][0]["dataFilter"]["polarization"] == "DV"
    assert '"VV","VH","dataMask"' in captured["evalscript"]
    assert "NDVI" not in captured["evalscript"]


async def test_edge_without_endpoint_never_falls_back_to_cloud(monkeypatch):
    monkeypatch.setattr(settings, "edge_ai_base_url", None)
    monkeypatch.setattr(settings, "ai_base_url", "https://cloud.example/v1")
    monkeypatch.setattr(settings, "ai_api_key", "test-key")
    request = AsyncMock()
    monkeypatch.setattr(ai, "_post_chat", request)
    result = await ai.generate_markdown_response("Interpretar", {}, "Consulta", "gemma3", provider="EDGE")
    assert result["fallback"] is True
    request.assert_not_awaited()


async def test_station_endpoint_template_overrides_default(monkeypatch):
    from app.services.fecoclima_adapter import FecoclimaAdapter, MonthBlock

    captured = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, *, headers):
            captured["url"] = url
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr("app.services.fecoclima_adapter.httpx.AsyncClient", Client)
    adapter = FecoclimaAdapter(
        endpoint_url="https://station.example/{guid}/{year}/{month_from}/{month_to}", api_key="test-only"
    )
    await adapter.fetch_block("station-guid", MonthBlock(2026, 9, 10))
    assert captured["url"] == "https://station.example/station-guid/2026/9/10"


@pytest.mark.parametrize('age_minutes', [17, 240])
def test_viewer_timezone_cannot_change_window_age_or_staleness(age_minutes):
    from zoneinfo import ZoneInfo
    reference = datetime(2026, 10, 20, 22, 47, tzinfo=UTC)
    sample = reference - timedelta(minutes=age_minutes)
    device = station()
    baseline = rolling_windows([reading(sample)], device, reference, window_hours=72)['72']
    for zone in ('America/Asuncion', 'America/Costa_Rica', 'Asia/Tokyo'):
        result = rolling_windows([reading(sample.astimezone(ZoneInfo(zone)))], device,
                                 reference.astimezone(ZoneInfo(zone)), window_hours=72)['72']
        assert result == baseline
        assert result['age_minutes'] == age_minutes
        assert result['stale'] == (age_minutes > 180)
