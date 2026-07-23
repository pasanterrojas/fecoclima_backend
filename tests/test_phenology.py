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
