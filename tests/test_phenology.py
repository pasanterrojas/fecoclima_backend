from datetime import date
from app.services.phenology import calculate_stage


def test_stage_is_calculated_from_planting_date():
    stages = [
        {"code": "VE", "name": "Emergencia", "description": "", "order_index": 0, "start_day": 0, "end_day": 5},
        {"code": "V1", "name": "V1", "description": "", "order_index": 1, "start_day": 6, "end_day": 14},
    ]
    result = calculate_stage(date(2026, 7, 1), date(2026, 7, 10), stages)
    assert result["stage_code"] == "V1"
    assert result["days_after_planting"] == 9


def test_missing_calendar_is_explicit():
    result = calculate_stage(date(2026, 7, 1), date(2026, 7, 10), [])
    assert result["stage_code"] is None
    assert result["stage_name"] == "Configuración pendiente"


def test_future_planting_date_is_not_started():
    stages = [
        {"code": "VE", "name": "Emergencia", "description": "", "order_index": 0, "start_day": 0, "end_day": 5},
    ]
    result = calculate_stage(date(2026, 7, 20), date(2026, 7, 10), stages)
    assert result["stage_code"] == "NO_INICIADO"
    assert result["stage_progress_pct"] == 0


def test_stage_timeline_contains_estimated_dates():
    stages = [
        {"code": "VE", "name": "Emergencia", "description": "", "order_index": 0, "start_day": 0, "end_day": 5},
    ]
    result = calculate_stage(date(2026, 7, 1), date(2026, 7, 2), stages)
    assert result["timeline"][0]["estimated_start_date"] == "2026-07-01"
    assert result["timeline"][0]["estimated_end_date"] == "2026-07-06"


def test_calendar_gap_does_not_invent_the_last_stage():
    stages = [
        {"code": "VE", "name": "Emergencia", "order_index": 0, "start_day": 0, "end_day": 5},
        {"code": "R1", "name": "Floración", "order_index": 1, "start_day": 10, "end_day": 30},
    ]
    result = calculate_stage(date(2026, 7, 1), date(2026, 7, 8), stages)
    assert result["stage_code"] is None
    assert "día 7" in result["description"]
    assert result["cycle_finished"] is False


def test_overlapping_calendar_does_not_choose_an_arbitrary_stage():
    stages = [
        {"code": "VE", "name": "Emergencia", "order_index": 0, "start_day": 0, "end_day": 10},
        {"code": "R1", "name": "Floración", "order_index": 1, "start_day": 8, "end_day": 30},
    ]
    result = calculate_stage(date(2026, 7, 1), date(2026, 7, 10), stages)
    assert result["stage_code"] is None
    assert "superpuestas" in result["description"]


def test_finished_calendar_keeps_last_stage_and_labels_finished_cycle():
    stages = [{"code": "M", "name": "Madurez", "order_index": 0, "start_day": 0, "end_day": 10}]
    result = calculate_stage(date(2026, 7, 1), date(2026, 7, 20), stages)
    assert result["stage_code"] == "M" and result["cycle_finished"] is True
