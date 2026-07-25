from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.satellite import build_satellite_analysis


def observation(**overrides):
    values = {
        "captured_at": datetime(2026, 7, 20, tzinfo=UTC),
        "cloud_percentage": 8.0,
        "ndvi": 0.62,
        "ndmi": 0.24,
        "ndwi": -0.08,
        "raw_payload": {
            "geometry_mode": "polygon",
            "indices": {
                "ndvi": 0.62,
                "ndmi": 0.24,
                "ndwi": -0.08,
                "evi": 0.51,
                "savi": 0.48,
                "ndre": 0.33,
                "gndvi": 0.57,
                "nbr": 0.44,
                "msi": 0.72,
            },
            "statistics": {"ndvi": {"sample_count": 100}},
        },
        "previous_comparison": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def plot(**overrides):
    values = {"geojson": {"type": "Polygon", "coordinates": []}}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_satellite_analysis_builds_agent_evidence_and_high_quality():
    result = build_satellite_analysis(
        observation(),
        plot(),
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert result["reliability"] == "alta"
    assert result["quality_score"] >= 80
    assert any("NDVI medio" in item for item in result["evidence_for_agent"])
    assert any(signal["code"] == "ndmi_state" for signal in result["signals"])


def test_satellite_analysis_marks_decline_as_attention():
    previous = observation(
        captured_at=datetime(2026, 7, 10, tzinfo=UTC),
        ndvi=0.70,
        ndmi=0.30,
    )
    current = observation(ndvi=0.58, ndmi=0.22)
    result = build_satellite_analysis(
        current,
        plot(),
        previous=previous,
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )
    ndvi_trend = next(item for item in result["signals"] if item["code"] == "ndvi_trend")
    assert ndvi_trend["level"] == "attention"
    assert ndvi_trend["value"] == -0.12


def test_approximate_old_cloudy_geozone_is_limited():
    current = observation(
        captured_at=datetime(2026, 6, 1, tzinfo=UTC),
        cloud_percentage=72.0,
        raw_payload={"geometry_mode": "point_buffer_approximation", "statistics": {}},
    )
    result = build_satellite_analysis(
        current,
        SimpleNamespace(geojson=None),
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert result["reliability"] == "limitada"
    joined = " ".join(result["limitations"]).lower()
    assert "nubosidad" in joined
    assert "antigua" in joined
    assert "aproximación" in joined
