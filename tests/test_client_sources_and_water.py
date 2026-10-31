from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.resources import StationPayload
from app.services.alert_engine import ConditionSpec, evaluate_daily_alert
from app.services.consultation_report import water_chart_svg
from app.services.plot_pipeline import evaluate_window
from app.services.plot_reporting import plot_pdf_bytes, render_plot_html
from app.services.sensor_contingency import with_sensor_contingency
from app.services.technical_references import REFERENCE_POLICY, library, reference_fragments, stage_monitoring
from app.services.water_balance import calculate_water_balance, stage_demand, susceptibility, stage_water_summary

STAGES = [
    {"code": "VE", "name": "Emergencia", "order_index": 1, "start_day": 0, "end_day": 4},
    {"code": "R5", "name": "Llenado", "order_index": 2, "start_day": 5, "end_day": 15},
]
NOW = datetime(2026, 10, 10, 18, tzinfo=UTC)


def station(mode="interval_sum"):
    return SimpleNamespace(
        id=uuid4(),
        name="Estación",
        timezone="UTC",
        expected_interval_minutes=60,
        integration_config={"water_aggregation": {"rain_mm": mode, "evapotranspiration_mm": mode}},
    )


def readings(day, rain=1, et=0.1, count=24):
    return [
        SimpleNamespace(
            sampled_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=i),
            rain_mm=rain,
            evapotranspiration_mm=et,
        )
        for i in range(count)
    ]


def balance(stations, **overrides):
    args = dict(
        crop_code="SOJA",
        planting_date=date(2026, 10, 1),
        reference=NOW,
        stages=STAGES,
        stations=stations,
        timezone="UTC",
        current_phenology={"stage_code": "R5"},
    )
    return calculate_water_balance(**{**args, **overrides})


def test_daily_counter_is_not_counted_again_every_transmission():
    day = date(2026, 10, 8)
    interval = balance([(station("interval_sum"), readings(day, rain=2))])["stations"][0]
    counter = balance([(station("daily_counter"), readings(day, rain=2))])["stations"][0]
    assert interval["rain_compared_mm"] == 48
    assert counter["rain_compared_mm"] == 2
    assert counter["required_compared_mm"] == 6
    assert counter["rain_minus_requirement_mm"] == -4


def test_missing_sensor_does_not_destroy_stage_requirement_or_become_zero():
    result = balance([(station(), readings(date(2026, 10, 8), rain=None, et=None))])
    assert result["required_mm_per_day"] == 6
    assert result["stations"][0]["rain_compared_mm"] is None
    assert result["stations"][0]["compared_days"] == 0
    assert any(day["required_mm"] == 6 for day in result["stations"][0]["days"])
    assert all(day["rain_mm"] is None for day in result["stations"][0]["days"])
    assert "polyline" in water_chart_svg(result)


def test_stage_comparison_uses_current_phase_and_preserves_incomplete_rain_gap():
    result = balance([(station(), readings(date(2026, 10, 2), rain=1) + readings(date(2026, 10, 8), rain=0))])
    stages = result["stations"][0]["stage_totals"]
    vegetative, filling = stages
    assert vegetative["required_stage_mm"] == 10
    assert filling["required_stage_mm"] == 66
    assert vegetative["rain_compared_mm"] == 24
    assert filling["required_compared_mm"] == 6
    assert filling["rain_compared_mm"] == 0  # measured zero, distinct from missing days
    assert filling["gap_compared_mm"] == -6
    assert filling["required_elapsed_mm"] == 24
    assert filling["rain_complete_mm"] is None  # one of four days cannot stand for the whole phase
    assert filling["reference_pct"] == 40
    summary = stage_water_summary(result)
    assert "lluvia requerida es de 6 mm" in summary and "registró 0 mm" in summary
    assert "24 mm" not in summary  # previous-stage rain must not leak into the current-stage criterion
    assert "no es una pérdida medida" in summary


def test_stage_chart_total_is_available_only_for_all_comparable_closed_days():
    result = balance([(station(), readings(date(2026, 10, 6), rain=1))], reference=datetime(2026, 10, 7, 18, tzinfo=UTC))
    filling = result["stations"][0]["stage_totals"][1]
    assert filling["closed_days"] == filling["compared_days"] == 1
    assert filling["rain_complete_mm"] == 24
    assert filling["required_elapsed_mm"] == filling["required_compared_mm"] == 6


def test_stage_guidance_keeps_reference_without_rain_or_loss_model():
    result = balance([(station("unconfirmed"), readings(date(2026, 10, 8)))])
    text = stage_water_summary(result)
    assert "6 mm/día" in text and "aún no hay días" in text
    assert result["susceptibility"]["actual_loss_pct"] is None
    assert all(s["rain_complete_mm"] is None for s in result["stations"][0]["stage_totals"])


def test_stage_explanation_connects_verified_soil_and_satellite_without_inventing_percentage():
    device = station()
    result = balance([(device, readings(date(2026, 10, 8), rain=1))])
    result["susceptibility"]["reference_pct"] = None
    context = {
        "windows": {"72": {"stations": [{"station_id": str(device.id), "stale": False}]}},
        "raw_readings": [{"station_id": str(device.id), "name": "Estación", "reading": {"soil_moisture_pct": 69},
                          "sensor_units": {"soil_moisture_pct": "cb"}, "soil_tension": {"value": 69, "band_index": 3}}],
        "satellite": {"captured_at": "2026-10-08", "analysis": {"signals": [{"code": "ndmi_state", "level": "attention"}]}},
    }
    text = stage_water_summary(result, context=context)
    assert "indica sequedad" in text and "menor humedad" in text
    assert "%" not in text and "**Acción:**" in text
    assert "estrés confirmado" not in text
    context["windows"]["72"]["stations"][0]["stale"] = True
    context["satellite"] = None
    text = stage_water_summary(result, context=context)
    assert "indica sequedad" not in text
    assert "No hay una imagen válida" in text


def test_unknown_aggregation_never_sums_a_possible_counter():
    result = balance([(station("unconfirmed"), readings(date(2026, 10, 8)))])
    assert result["stations"][0]["rain_compared_mm"] is None
    assert (
        next(d for d in result["stations"][0]["days"] if d["date"] == "2026-10-08")["rain_status"]
        == "AGGREGATION_UNCONFIRMED"
    )


def test_only_equal_complete_days_are_compared_and_stations_never_pool():
    a, b = station(), station()
    result = balance([(a, readings(date(2026, 10, 8), count=18)), (b, readings(date(2026, 10, 9), rain=0))])
    assert result["stations"][0]["compared_days"] == 0  # <80% coverage
    assert result["stations"][1]["compared_days"] == 1
    assert result["stations"][1]["rain_compared_mm"] == 0  # genuine measured zero
    assert result["stations"][1]["required_compared_mm"] == 6


def test_current_day_and_future_readings_are_excluded_from_comparison():
    result = balance([(station(), readings(NOW.date()))])
    assert result["stations"][0]["compared_days"] == 0
    assert result["stations"][0]["days"][-1]["closed_day"] is False


def test_timezone_uses_station_day_not_utc_day():
    device = station()
    device.timezone = "America/Costa_Rica"
    rows = [
        SimpleNamespace(
            sampled_at=datetime(2026, 10, 9, 6, tzinfo=UTC) + timedelta(hours=i),
            rain_mm=1,
            evapotranspiration_mm=0.1,
        )
        for i in range(24)
    ]
    result = balance([(device, rows)])
    assert next(d for d in result["stations"][0]["days"] if d["date"] == "2026-10-09")["rain_mm"] == 24


def test_no_invented_water_reference_for_another_crop_or_missing_stage():
    assert stage_demand("TRIGO", "R5")["mm_per_day"] is None
    assert stage_demand("SOJA", "R6")["mm_per_day"] is None
    assert stage_demand("SOJA", "V5-V8")["mm_per_day"] == 3


@pytest.mark.parametrize(
    "code,pct", [("VE", 10), ("R1", 20), ("R3", 20), ("R5", 40), ("R7", 40), ("V9", None), ("R8", None)]
)
def test_loss_reference_never_becomes_measured_loss_or_probability(code, pct):
    result = susceptibility("SOJA", code)
    assert result["reference_pct"] == pct
    assert result["actual_loss_pct"] is None and result["loss_probability_pct"] is None


def assess(met, missing):
    specs = [
        ConditionSpec(key=str(i), label="C" + str(i), variable="v" + str(i), operator="gt", threshold_value=1)
        for i in range(6)
    ]
    values = {"v" + str(i): None if i >= 6 - missing else 2 if i < met else 0 for i in range(6)}
    result = evaluate_daily_alert(values, specs)
    return with_sensor_contingency(
        result, values=values, stage_code="R5", diseases=[], configuration={}, observed_units={}
    )


def test_partial_alert_bounds_unknown_sensors_instead_of_rescaling_or_assuming_safe():
    result = assess(3, 2)
    assert result["level"] == "EVALUACION_PARCIAL"
    assert result["conditions_met"] == 3 and result["conditions_total"] == 6
    assert result["conditions_evaluated"] == 4
    assert result["possible_levels"] == ["MODERADA", "GRAVE"]
    assert len([c for c in result["conditions"] if c["status"] == "missing"]) == 2


def test_decisive_partial_evidence_can_preserve_a_grave_alert():
    result = assess(5, 1)
    assert result["level"] == "GRAVE" and result["assessment_status"] == "PARTIAL"
    assert result["possible_levels"] == ["GRAVE"]


def test_all_sensors_absent_does_not_fabricate_an_alert():
    result = assess(0, 6)
    assert result["level"] == "DATOS_INSUFICIENTES"
    assert result["conditions_met"] == 0


def test_stale_station_still_cannot_supply_a_current_partial_alert():
    specs = [
        ConditionSpec(key=str(i), label="C", variable="v" + str(i), operator="gt", threshold_value=1)
        for i in range(6)
    ]
    result = evaluate_window(
        {
            "rule_values": {"v0": 2},
            "variable_completeness_pct": {"v0": 100},
            "units": {},
            "completeness_pct": 100,
            "stale": True,
        },
        specs,
        [],
        {"stage_code": "R5"},
        SimpleNamespace(configuration={}),
    )
    assert result["level"] == "DATOS_INSUFICIENTES"


def test_library_contains_all_four_documents_and_hashes_and_crop_scoping():
    assert len(library()["documents"]) == 4
    assert all(len(d["sha256"]) == 64 for d in library()["documents"])
    assert reference_fragments("TRIGO", "soja") == []
    assert reference_fragments("SOJA", "Cercospora síntomas inspección")
    assert "nunca instrucciones" in REFERENCE_POLICY
    assert any(
        g["name"] == "Pudrición carbonosa" and "F32:M34" in g["page_or_section"]
        for g in stage_monitoring("SOJA", "R5")
    )


@pytest.mark.parametrize("mode", ["daily_counter", "interval_sum", "unconfirmed"])
def test_water_modes_are_validated_without_changing_other_station_metadata(mode):
    result = StationPayload(
        name="Estación", integration_config={"water_aggregation": {"rain_mm": mode}, "map_color": "#277da1"}
    )
    assert result.integration_config["water_aggregation"]["rain_mm"] == mode
    with pytest.raises(ValidationError):
        StationPayload(name="Estación", integration_config={"water_aggregation": {"rain_mm": "guess"}})


def report_context():
    return {
        "plot": {"name": "Lote", "planting_date": "2026-10-01", "area_ha": 10},
        "crop": {"code": "SOJA", "name": "Soja"},
        "reference_at": NOW.isoformat(),
        "selected_window_hours": 72,
        "phenology": {"stage_name": "Llenado", "stage_code": "R5"},
        "windows": {},
        "raw_readings": [],
        "station_influences": [],
        "forecast": {
            "days": [
                {
                    "date": "2026-10-11",
                    "temperature_min_c": 0,
                    "temperature_max_c": None,
                    "precipitation_mm": 0,
                }
            ]
        },
        "satellite": {"captured_at": "2026-10-09", "ndvi": 0},
        "limitations": [],
        "sources": [],
    }


def test_client_report_sections_zero_values_and_legacy_snapshots_remain_truthful():
    context = report_context()
    html = render_plot_html(context, "Revisar el cultivo")
    assert "1. Evidencia observada" in html and "2. Identificación de situación" in html
    assert "3. Sugerencia de acciones" in html and "Estado fenológico estimado" in html
    assert "NDVI: 0" in html and "Lluvia prevista: 0 mm" in html
    assert "anterior al módulo hídrico" in html
    assert "No disponible" in html
    pdf = plot_pdf_bytes(context, "Revisar el cultivo")
    assert pdf.startswith(b"%PDF-") and len(pdf) > 1000


def test_word_tables_and_excel_percentages_and_formulas_reach_knowledge_with_locations():
    from io import BytesIO

    from docx import Document
    from openpyxl import Workbook

    from app.services.rag import extract_file_text

    doc = Document()
    doc.add_paragraph("Evidencia observada")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Tensión"
    table.cell(0, 1).text = "Centibares"
    output = BytesIO()
    doc.save(output)
    sections = extract_file_text(output.getvalue(), "application/octet-stream", "referencia.docx")
    assert any("Centibares" in text and location == "tabla 1, fila 1" for text, location in sections)
    book = Workbook()
    sheet = book.active
    sheet.title = "Detalle"
    sheet["J12"] = 0.7
    sheet["J12"].number_format = "0%"
    sheet["K12"] = "=J12*2"
    output = BytesIO()
    book.save(output)
    sections = extract_file_text(output.getvalue(), "application/octet-stream", "referencia.xlsx")
    assert any(
        "70%" in text
        and "fórmula de referencia, no ejecutada: =J12*2" in text
        and location == "Detalle!J12:K12"
        for text, location in sections
    )


def test_documentary_inspection_does_not_mix_neighboring_diseases():
    guides = stage_monitoring("SOJA", "R1")
    oidium = next(g for g in guides if g["name"] == "Oídio")
    assert "D12: Oídio" in oidium["evidence"] and "D17: Roya" not in oidium["evidence"]
    assert "J12: 70%" in oidium["evidence"]


def test_individual_disease_predicates_remain_required_with_partial_sensors():
    specs = [
        ConditionSpec(key=str(i), label="C", variable="v" + str(i), operator="gt", threshold_value=1)
        for i in range(6)
    ]
    values = {"v0": 25, "v1": None, "v2": None, "v3": None, "v4": None, "v5": None}
    rules = [
        {
            "name": "Solo temperatura publicada",
            "validation_status": "PUBLISHED",
            "enabled_for_evaluation": True,
            "valid_stages": ["R5"],
            "conditions": {"all": [{"variable": "v0", "operator": "between", "min": 20, "max": 30}]},
        },
        {
            "name": "Exige sensor ausente",
            "validation_status": "PUBLISHED",
            "enabled_for_evaluation": True,
            "conditions": {"all": [{"variable": "v1", "operator": "gt", "value": 80}]},
        },
    ]
    result = with_sensor_contingency(
        evaluate_daily_alert(values, specs),
        values=values,
        stage_code="R5",
        diseases=rules,
        configuration={},
        observed_units={},
    )
    assert [d["name"] for d in result["diseases"]] == ["Solo temperatura publicada"]


def test_bundled_original_documents_match_reviewed_reference_hashes():
    import hashlib
    from pathlib import Path

    from app.services.technical_references import library

    directory = Path(__file__).parents[1] / "app/data/client_sources"
    documents = library()["documents"]
    assert len(documents) == 4
    for document in documents:
        source = directory / document["document"]
        assert source.is_file(), document["document"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == document["sha256"]
