from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.satellite import _feature_cloud, _feature_datetime, _feature_preview, plot_geometry


def test_catalog_feature_helpers():
    feature = {
        "properties": {"datetime": "2026-07-20T14:00:00Z", "eo:cloud_cover": 12.5},
        "assets": {"thumbnail": {"href": "https://example.test/preview.jpg", "type": "image/jpeg"}},
    }
    assert _feature_datetime(feature) == datetime(2026, 7, 20, 14, tzinfo=UTC)
    assert _feature_cloud(feature) == 12.5
    assert _feature_preview(feature) == "https://example.test/preview.jpg"


def test_point_plot_becomes_explicit_approximation_polygon():
    plot = SimpleNamespace(geojson=None, latitude=-26.15851, longitude=-55.27218, influence_radius_m=750)
    geometry = plot_geometry(plot)
    assert geometry["type"] == "Polygon"
    assert len(geometry["coordinates"][0]) == 5
