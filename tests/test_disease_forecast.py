from app.services.disease_projection import evaluate_disease_projection


def _roya_rule():
    return {
        "name": "Roya de la soja",
        "conditions": {
            "all": [
                {
                    "variable": "leaf_wetness_hours",
                    "operator": "between",
                    "min": 6,
                    "max": 8,
                    "unit": "h",
                },
                {
                    "variable": "temperature_avg_c",
                    "operator": "between",
                    "min": 15,
                    "max": 28,
                    "unit": "°C",
                },
            ]
        },
        "valid_stages": [],
        "validation_status": "PUBLISHED",
        "enabled_for_evaluation": True,
        "source_reference": "Plantilla de soja",
    }


def test_forecast_disease_requires_all_variables():
    matched, unresolved = evaluate_disease_projection(
        {"temperature_avg_c": 24, "leaf_wetness_hours": None},
        "R1",
        [_roya_rule()],
        "MODERADA",
    )
    assert matched == []
    assert unresolved[0]["name"] == "Roya de la soja"
    assert "leaf_wetness_hours" in unresolved[0]["missing_variables"]


def test_forecast_disease_matches_complete_rule():
    matched, unresolved = evaluate_disease_projection(
        {"temperature_avg_c": 24, "leaf_wetness_hours": 7},
        "R1",
        [_roya_rule()],
        "MODERADA",
    )
    assert unresolved == []
    assert matched[0]["name"] == "Roya de la soja"
    assert matched[0]["risk"] == "moderado"
    assert matched[0]["condition_coverage_pct"] == 100
