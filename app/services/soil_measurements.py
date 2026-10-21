"""Explicit soil sensor semantics for legacy storage and deterministic rules.

The database column named ``soil_moisture_*pct`` predates stations measuring
water tension. Keep that stored number unchanged, and expose it to rules only
under the physical quantity actually measured. No cb-to-percent conversion is
performed here.
"""

from __future__ import annotations

from typing import Any

SOIL_MOISTURE_VARIABLE = "soil_moisture_avg_pct"
SOIL_TENSION_VARIABLE = "soil_tension_avg_cb"
SOIL_VARIABLE_UNITS = {SOIL_MOISTURE_VARIABLE: "%", SOIL_TENSION_VARIABLE: "cb"}


def normalize_soil_unit(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"cb", "cbar", "centibar", "centibars", "centibares"}:
        return "cb"
    if normalized in {"%", "percent", "percentage", "porcentaje"}:
        return "%"
    # Unknown units, including "cp", must not silently become percentages.
    return normalized


def soil_rule_values(values: dict[str, float | None], measured_unit: str) -> dict[str, float | None]:
    """Map the legacy soil channel to one unambiguous rule input."""
    result = dict(values)
    unit = normalize_soil_unit(measured_unit)
    measured = values.get(SOIL_MOISTURE_VARIABLE)
    result[SOIL_MOISTURE_VARIABLE] = measured if unit == "%" else None
    result[SOIL_TENSION_VARIABLE] = measured if unit == "cb" else None
    return result


def soil_rule_units(measured_unit: str) -> dict[str, str]:
    """Describe both rule channels, preserving incompatible/unknown readings."""
    actual = normalize_soil_unit(measured_unit)
    return {SOIL_MOISTURE_VARIABLE: actual, SOIL_TENSION_VARIABLE: actual}
