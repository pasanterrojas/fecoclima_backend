from datetime import date, datetime, UTC
from types import SimpleNamespace

from app.api.endpoints.public import _dashboard_evaluation_for_window


def _evaluation(level: str, day: int):
    return SimpleNamespace(
        level=level,
        evaluation_date=date(2026, 8, day),
        evaluated_at=datetime(2026, 8, day, 12, tzinfo=UTC),
    )


def test_dashboard_uses_same_evaluation_as_worst_72h_headline() -> None:
    evaluations = [
        _evaluation("MODERADA", 2),
        _evaluation("GRAVE", 1),
        _evaluation("NINGUNA", 31),
    ]

    selected, maximum = _dashboard_evaluation_for_window(evaluations)

    assert maximum == "GRAVE"
    assert selected is evaluations[1]


def test_dashboard_uses_most_recent_evaluation_when_worst_level_ties() -> None:
    evaluations = [
        _evaluation("GRAVE", 2),
        _evaluation("GRAVE", 1),
        _evaluation("MODERADA", 31),
    ]

    selected, maximum = _dashboard_evaluation_for_window(evaluations)

    assert maximum == "GRAVE"
    assert selected is evaluations[0]


def test_dashboard_returns_no_data_for_empty_window() -> None:
    selected, maximum = _dashboard_evaluation_for_window([])

    assert selected is None
    assert maximum == "DATOS_INSUFICIENTES"
