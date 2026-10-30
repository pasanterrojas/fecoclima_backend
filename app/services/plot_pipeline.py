"""Deterministic parcel pipeline. Completes every calculation before any LLM call."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BaseCondition,
    Crop,
    CropRuleVersion,
    DiseaseRule,
    PhenologyStage,
    Plot,
    PlotAnalysis,
    Station,
    WeatherReading,
)
from app.services.alert_engine import LEVEL_ORDER, ConditionSpec, evaluate_daily_alert
from app.services.forecast import fetch_open_meteo
from app.services.geospatial import coverage_percentage, sync_plot_influences
from app.services.phenology import calculate_stage
from app.services.plot_satellite_context import plot_satellite_context
from app.services.serialization import model_dict
from app.services.soil_tension import soil_tension
from app.services.alert_engine import select_soil_criterion
from app.services.soil_measurements import (
    SOIL_TENSION_VARIABLE,
    SOIL_VARIABLE_UNITS,
    normalize_soil_unit,
    soil_rule_units,
    soil_rule_values,
)
from app.services.weather import (
    VALID_RANGES,
    condition_readings_frame,
    effective_leaf_sensor_config,
    effective_soil_moisture_unit,
    summarize_readings_for_conditions,
)

WINDOWS = (60, 72, 96, 120)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def next_three_forecast_days(days: list[dict], local_date: date) -> list[dict]:
    """Select only tomorrow through day three; never reuse yesterday's horizon."""
    by_date = {str(day.get("date")): day for day in days}
    return [by_date[key] for offset in range(1, 4)
            if (key := (local_date + timedelta(days=offset)).isoformat()) in by_date]


def rolling_windows(
    readings: list[WeatherReading], station: Station, reference: datetime,
    *, window_hours: int | None = None,
) -> dict[str, dict]:
    """(reference - h, reference]; per-day equivalents preserve daily rule units.

    Station streams are never pooled. No last-reading anchoring: old readings
    cannot masquerade as current data. Duplicate timestamps count once.
    """
    if window_hours is not None and window_hours not in WINDOWS:
        raise ValueError("Período de análisis no soportado")
    reference = utc(reference)
    unique = {utc(row.sampled_at): row for row in readings if utc(row.sampled_at) <= reference}
    result = {}
    for hours in (WINDOWS if window_hours is None else (window_hours,)):
        start = reference - timedelta(hours=hours)
        rows = [row for at, row in sorted(unique.items()) if start < at <= reference]
        values = summarize_readings_for_conditions(rows, station)
        # Six published conditions were calibrated as daily values. Keep the
        # observed cumulative values AND explicit normalized rule inputs.
        rule_values = dict(values)
        cumulative_keys = ["rain_sum_mm"]
        if effective_leaf_sensor_config(station).get("mode") != "hours":
            cumulative_keys.append("leaf_wetness_hours")
        for key in cumulative_keys:
            if rule_values.get(key) is not None:
                rule_values[key] = round(rule_values[key] * 24 / hours, 3)
        expected = max(1, hours * 60 // max(1, station.expected_interval_minutes))
        clean = condition_readings_frame(rows, station)
        variable_coverage = {
            key: round(min(100, int(clean[column].count()) * 100 / expected), 2)
            for key, column in {
                "temperature_avg_c": "temperature_c",
                "humidity_avg_pct": "humidity_pct",
                "rain_sum_mm": "rain_mm",
                "soil_temperature_avg_c": "soil_temperature_c",
                "soil_moisture_avg_pct": "soil_moisture_pct",
                "leaf_wetness_hours": "leaf_sensor_value",
            }.items()
        }
        variable_coverage[SOIL_TENSION_VARIABLE] = variable_coverage["soil_moisture_avg_pct"]
        latest = max((utc(row.sampled_at) for row in rows), default=None)
        age = (reference - latest).total_seconds() / 60 if latest else None
        result[str(hours)] = {
            "hours": hours,
            "from": start.isoformat(),
            "to": reference.isoformat(),
            "samples": len(rows),
            "expected_samples": expected,
            "completeness_pct": round(min(100, len(rows) * 100 / expected), 2),
            "variable_completeness_pct": variable_coverage,
            "latest_at": latest.isoformat() if latest else None,
            "age_minutes": round(age, 1) if age is not None else None,
            "stale": age is None or age > max(60, station.expected_interval_minutes * 3),
            "values": values,
            "units": {"soil_moisture_avg_pct": effective_soil_moisture_unit(station)},
            "outliers": sum(1 for row in rows for key, bounds in VALID_RANGES.items()
                            if (value := getattr(row, key, None)) is not None
                            and not bounds[0] <= value <= (100 if key == 'soil_moisture_pct' and effective_soil_moisture_unit(station) == '%' else bounds[1])),
            "rule_values": rule_values,
            "rule_basis": "daily_equivalent_for_precipitation_and_leaf_wetness",
        }
    return result


def historical_regression(
    readings: list[WeatherReading], reference: datetime, timezone: str, *, expected_interval_minutes: int = 60
) -> dict:
    """OLS on at least 7 complete local calendar days, never an API forecast."""
    tz = ZoneInfo(timezone)
    today = utc(reference).astimezone(tz).date()
    columns = ("temperature_c", "humidity_pct", "rain_mm")
    if not readings:
        return {"status": "INSUFFICIENT_HISTORY", "days": []}
    frame = pd.DataFrame(
        [
            {
                "date": utc(row.sampled_at).astimezone(tz).date(),
                **{key: getattr(row, key) for key in columns},
            }
            for row in readings
            if utc(row.sampled_at) <= utc(reference)
        ],
        columns=["date", *columns],
    )
    frame = frame[frame.date < today]
    forecasts = [{"date": (today + timedelta(days=i)).isoformat(), "values": {}} for i in range(1, 4)]
    fitted = {}
    for key in columns:
        frame[key] = pd.to_numeric(frame[key], errors="coerce").replace([np.inf, -np.inf], np.nan)
        if key in VALID_RANGES:
            frame[key] = frame[key].where(frame[key].between(*VALID_RANGES[key]))
        # Partial rain days cannot be treated as daily totals.
        series = (
            frame.groupby("date")[key].sum(min_count=1)
            if key == "rain_mm"
            else frame.groupby("date")[key].mean()
        ).dropna()
        counts = frame.groupby("date")[key].count()
        # Require 80% of the expected daily samples for each variable.
        minimum = max(1, int(1440 / expected_interval_minutes * 0.8))
        series = series[counts.reindex(series.index) >= minimum]
        if len(series) < 7:
            fitted[key] = {"status": "INSUFFICIENT_HISTORY", "days": len(series)}
            continue
        x = np.array([(day - today).days for day in series.index], dtype=float)
        y = series.to_numpy(dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        error = float(np.sqrt(np.mean((y - (slope * x + intercept)) ** 2)))
        fitted[key] = {
            "status": "COMPLETED",
            "days": len(series),
            "slope_per_day": round(float(slope), 4),
            "rmse": round(error, 3),
        }
        for offset, row in enumerate(forecasts, 1):
            value = float(slope * offset + intercept)
            if key in {"humidity_pct", "rain_mm"}:
                value = max(0, value)
            if key == "humidity_pct":
                value = min(100, value)
            row["values"][key] = round(value, 2)
    return {
        "status": "COMPLETED" if any(r["values"] for r in forecasts) else "INSUFFICIENT_HISTORY",
        "method": "ordinary_least_squares_local_daily_means_rain_totals",
        "interpretation": "Tendencia estadística; no sustituye el pronóstico meteorológico",
        "variables": fitted,
        "days": forecasts,
    }



def soil_wet_tension_threshold(station: Station) -> float | None:
    """Station-specific wet-soil proxy, not a conversion from cb to percent.

    Tensiometer values are inverse to wetness: low cb means wetter soil.
    An explicit threshold and a recorded technical validation are required.
    Existing UI defaults (including 30 cb) are not a validated crop rule.
    Prefer publishing a crop condition using ``soil_tension_avg_cb`` instead.
    """
    if effective_soil_moisture_unit(station) != "cb":
        return None
    config = getattr(station, "integration_config", None) or {}
    if (config.get("soil_tension_proxy_enabled") is not True
            or config.get("soil_tension_rule_validated") is not True
            or not str(config.get("soil_tension_rule_reference") or "").strip()):
        return None
    threshold = config.get("soil_tension_wet_max_cb")
    if isinstance(threshold, bool):
        return None
    try:
        value = float(threshold)
    except (ValueError, TypeError):
        return None
    return value if np.isfinite(value) and 0 <= value <= 200 else None


def adapt_soil_condition_for_station(specs: list[ConditionSpec], station: Station) -> tuple[list[ConditionSpec], list[dict]]:
    """Keep all six conditions while evaluating cb stations on their own scale.

    Only the standard 50%-based *soil_moisture* condition is replaced.
    Custom percentage rules and disease models are never implicitly converted.
    """
    threshold = soil_wet_tension_threshold(station)
    adapted: list[ConditionSpec] = []
    adaptations: list[dict] = []
    for spec in specs:
        configured = select_soil_criterion(spec, effective_soil_moisture_unit(station))
        if configured is not spec:
            adapted.append(configured)
            adaptations.append({"condition": spec.key, "station_id": str(station.id),
                "method": "crop_soil_unit_criterion", "sensor_unit": configured.unit,
                "source_reference": spec.soil_alternative["source_reference"],
                "message": f"Se usa el criterio del cultivo en {configured.unit}, correspondiente al sensor. No se convirtió la lectura."})
            continue
        if (threshold is not None and spec.key == "soil_moisture"
                and spec.variable == "soil_moisture_avg_pct"
                and spec.operator == "gte" and spec.threshold_value == 50
                and (spec.unit or "").strip() == "%"):
            adapted.append(replace(
                spec, label=f"Tensión del suelo ≤ {threshold:g} cb (criterio local validado)",
                variable=SOIL_TENSION_VARIABLE,
                operator="lte", threshold_min=None, threshold_max=None,
                threshold_value=threshold, unit="cb",
            ))
            adaptations.append({
                "condition": spec.key, "station_id": str(station.id),
                "method": "soil_tension_explicit_local_alternative",
                "sensor_unit": "cb", "original_unit": "%",
                "threshold_cb": threshold,
                "source_reference": str(station.integration_config["soil_tension_rule_reference"]).strip(),
                "message": (
                    f"La estación entrega tensión en cb. Se evalúa suelo húmedo con "
                    f"tensión ≤ {threshold:g} cb como criterio local validado, NO como "
                    "conversión de la regla original de humedad ≥ 50%. "
                    "El umbral se ajusta en Administración > Estaciones según suelo/cultivo."
                ),
            })
        else:
            adapted.append(spec)
    return adapted, adaptations


def diagnose_window(
    window: dict,
    specs: list[ConditionSpec],
    station: Station,
    *,
    has_rule: bool = True,
    has_phenology: bool = True,
) -> list[dict[str, str]]:
    """Expose truthful, actionable reasons; never replace missing measurements with estimates.

    These issues describe *why* a window cannot be fully evaluated. A station
    can have a recent temperature reading yet lack a different mandatory sensor.
    """
    issues: list[dict[str, str]] = []

    def add(code: str, message: str, action: str, variable: str = '') -> None:
        if not any(item['code'] == code and item['variable'] == variable for item in issues):
            issues.append({'code': code, 'variable': variable, 'message': message, 'action': action})

    if not has_rule or len(specs) != 6:
        add('RULE_INCOMPLETE', 'El cultivo no tiene seis condiciones activas en una regla publicada.',
            'Revise y publique la regla completa en Administración > Cultivos > Reglas y alertas.')
    if not has_phenology:
        add('PHENOLOGY_UNAVAILABLE', 'No hay una etapa fenológica definida para la fecha de siembra.',
            'Revise fecha de siembra y etapas del cultivo en Administración.')
    if window['samples'] == 0:
        add('NO_READINGS', f'No se recibieron lecturas de esta estación en las últimas {window["hours"]} horas.',
            'Compruebe la última sincronización, el GUID y la conexión de la estación.')
    else:
        if window['stale']:
            add('STALE_READINGS', 'La estación tiene lecturas, pero la última ya no es reciente.',
                'Sincronice la estación y revise la hora de su última transmisión.')
        if window['completeness_pct'] < 80:
            add('LOW_SAMPLE_COVERAGE',
                f'Se recibieron {window["samples"]} de {window["expected_samples"]} lecturas esperadas '
                f'({window["completeness_pct"]}%). La evaluación requiere al menos 80%.',
                'Revise la frecuencia esperada configurada y los periodos sin transmisión.')

    observed_unit = window.get('units', {}).get('soil_moisture_avg_pct', '%')
    leaf_mode = effective_leaf_sensor_config(station).get('mode', 'unknown')
    for spec in specs:
        coverage_key = 'soil_moisture_avg_pct' if spec.variable == SOIL_TENSION_VARIABLE else spec.variable
        coverage = window.get('variable_completeness_pct', {}).get(coverage_key)
        if coverage is None:
            add('UNKNOWN_RULE_VARIABLE', f'La variable «{spec.label}» no es compatible con este motor de análisis.',
                'Corrija la variable en Administración > Cultivos > Reglas y alertas.', spec.variable)
        elif spec.variable in SOIL_VARIABLE_UNITS and (
            normalize_soil_unit(observed_unit) != SOIL_VARIABLE_UNITS[spec.variable]
            or (spec.unit and normalize_soil_unit(spec.unit) != SOIL_VARIABLE_UNITS[spec.variable])
        ):
            expected_unit = spec.unit or SOIL_VARIABLE_UNITS[spec.variable]
            action = 'Confirme con el proveedor la unidad del sensor antes de configurar la regla.'
            if normalize_soil_unit(observed_unit) == 'cb':
                action = 'Publique una condición de tensión en cb para este cultivo con umbral validado; no convierta cb a % sin calibración.'
            elif normalize_soil_unit(observed_unit) == '%':
                action = 'Seleccione la variable porcentual y valide su umbral en el cultivo.'
            add('SOIL_UNIT_MISMATCH',
                f'«{spec.label}» exige {expected_unit}, pero la estación entrega {observed_unit}. '
                'Tensión y porcentaje no son equivalentes.',
                action, spec.variable)
        elif spec.variable == 'leaf_wetness_hours' and leaf_mode not in {'binary_wet', 'threshold', 'hours'}:
            add('LEAF_SENSOR_UNCONFIGURED',
                'Hay que configurar cómo interpretar la señal del sensor de hoja.',
                'Defina modo y umbral del sensor de hoja en Administración > Estaciones.', spec.variable)
        elif coverage is not None and coverage < 80:
            if coverage == 0:
                add('VARIABLE_NO_READINGS', f'No hay mediciones válidas para «{spec.label}» en el periodo.',
                    'Verifique si la estación reporta este sensor y su correspondencia con la API.', spec.variable)
            else:
                add('VARIABLE_PARTIAL',
                    f'«{spec.label}» tiene {coverage}% de las mediciones esperadas (mínimo 80%).',
                    'Revise la transmisión, los valores fuera de rango y la frecuencia del sensor.', spec.variable)
    return issues


def evaluate_window(
    window: dict,
    specs: list[ConditionSpec],
    diseases: list[dict],
    phenology: dict,
    rule: CropRuleVersion | None,
) -> dict:
    if rule is None or len(specs) != 6:
        return {
            "level": "DATOS_INSUFICIENTES",
            "conditions": [],
            "diseases": [],
            "simple_explanation": "Se requieren seis condiciones publicadas para el cultivo.",
        }
    actual_unit = window.get("units", {}).get("soil_moisture_avg_pct", "%")
    rule_values = soil_rule_values(window["rule_values"], actual_unit)
    for spec in specs:
        # Unknown variables are *not* silently considered 100% complete.
        coverage_key = "soil_moisture_avg_pct" if spec.variable == SOIL_TENSION_VARIABLE else spec.variable
        if window.get("variable_completeness_pct", {}).get(coverage_key, 0) < 80:
            rule_values[spec.variable] = None
    result = evaluate_daily_alert(
        rule_values,
        specs,
        stage_code=phenology.get("stage_code"),
        disease_rules=diseases,
        data_completeness=window["completeness_pct"] / 100,
        alert_configuration=rule.configuration,
        observed_units=soil_rule_units(actual_unit),
    )
    missing = [item["explanation"] for item in result.get("conditions", []) if item.get("status") == "missing"]
    result["missing_reasons"] = missing
    if missing:
        result["simple_explanation"] = missing[0]
    if window["stale"] or window["completeness_pct"] < 80 or not phenology.get("stage_code"):
        insufficient = result["alert_configuration"]["levels"]["DATOS_INSUFICIENTES"]
        result.update(level="DATOS_INSUFICIENTES", level_label=insufficient["label"], level_color=insufficient["color"])
        result["simple_explanation"] = "Faltan datos recientes, cobertura temporal suficiente o fenología."
        result["diseases"] = []
    return result


async def build_plot_analysis(
    db: AsyncSession,
    plot: Plot,
    *,
    created_by=None,
    reference: datetime | None = None,
    refresh_forecast: bool = False,
    window_hours: int = 72,
    satellite_observation_id: UUID | None = None,
) -> PlotAnalysis:
    if window_hours not in WINDOWS:
        raise ValueError("Período de análisis no soportado")
    reference = utc(reference or datetime.now(UTC))
    if not plot.geojson or not plot.crop_id or not plot.planting_date:
        raise ValueError("Configure polígono, cultivo y fecha de siembra de la parcela")
    crop = await db.get(Crop, plot.crop_id)
    if not crop or not crop.active or crop.deleted_at:
        raise ValueError("Seleccione un cultivo activo del catálogo")
    stations, influences = await sync_plot_influences(db, plot)
    stages = list(
        (
            await db.scalars(
                select(PhenologyStage)
                .where(
                    PhenologyStage.crop_id == crop.id, PhenologyStage.active.is_(True),
                    PhenologyStage.deleted_at.is_(None), PhenologyStage.cycle_type == "normal",
                )
                .order_by(PhenologyStage.order_index)
            )
        ).all()
    )
    timezone = next((s.timezone for s in stations if s.id == plot.station_id), "America/Asuncion")
    local_date = reference.astimezone(ZoneInfo(timezone)).date()
    phenology = calculate_stage(
        plot.planting_date,
        local_date,
        [
            {
                key: getattr(stage, key)
                for key in ("code", "name", "description", "order_index", "start_day", "end_day")
            }
            for stage in stages
        ],
    )
    rule = await db.scalar(
        select(CropRuleVersion)
        .where(
            CropRuleVersion.crop_id == crop.id,
            CropRuleVersion.status == "PUBLISHED",
            (CropRuleVersion.effective_from.is_(None) | (CropRuleVersion.effective_from <= local_date)),
            (CropRuleVersion.effective_to.is_(None) | (CropRuleVersion.effective_to >= local_date)),
        )
        .order_by(CropRuleVersion.created_at.desc())
        .limit(1)
    )
    conditions = (
        list(
            (
                await db.scalars(
                    select(BaseCondition)
                    .where(BaseCondition.rule_version_id == rule.id, BaseCondition.active.is_(True))
                    .order_by(BaseCondition.order_index)
                )
            ).all()
        )
        if rule
        else []
    )
    disease_rows = (
        list(
            (
                await db.scalars(
                    select(DiseaseRule).where(
                        DiseaseRule.rule_version_id == rule.id, DiseaseRule.active.is_(True)
                    )
                )
            ).all()
        )
        if rule
        else []
    )
    specs = [
        ConditionSpec(
            **{
                key: getattr(item, key)
                for key in (
                    "key",
                    "label",
                    "variable",
                    "operator",
                    "threshold_min",
                    "threshold_max",
                    "threshold_value",
                    "unit",
                    "required",
                    "soil_alternative",
                )
            }
        )
        for item in conditions
    ]
    diseases = [model_dict(item) for item in disease_rows]
    raw, station_windows, regressions = [], {}, []
    for station in stations:
        readings = list(
            (
                await db.scalars(
                    select(WeatherReading)
                    .where(
                        WeatherReading.station_id == station.id,
                        WeatherReading.sampled_at > reference - timedelta(days=30),
                        WeatherReading.sampled_at <= reference,
                    )
                    .order_by(WeatherReading.sampled_at)
                )
            ).all()
        )
        latest = (
            readings[-1]
            if readings
            else await db.scalar(
                select(WeatherReading)
                .where(WeatherReading.station_id == station.id, WeatherReading.sampled_at <= reference)
                .order_by(WeatherReading.sampled_at.desc())
                .limit(1)
            )
        )
        raw.append(
            {
                "station_id": str(station.id),
                "name": unescape(station.name),
                "reading": model_dict(latest, exclude={"raw_payload", "imported_by"}) if latest else None,
                "sensor_units": {
                    "soil_moisture_pct": effective_soil_moisture_unit(station),
                    "leaf_sensor_value": str(effective_leaf_sensor_config(station).get("unit") or ("h" if effective_leaf_sensor_config(station).get("mode") == "hours" else "señal")),
                },
                "soil_tension": soil_tension(latest.soil_moisture_pct if latest else None, effective_soil_moisture_unit(station)),
            }
        )
        station_windows[str(station.id)] = rolling_windows(readings, station, reference, window_hours=window_hours)
        station_specs, adaptations = adapt_soil_condition_for_station(specs, station)
        for window in station_windows[str(station.id)].values():
            window["condition_adaptations"] = adaptations
            window["data_issues"] = diagnose_window(
                window, station_specs, station, has_rule=bool(rule),
                has_phenology=bool(phenology.get('stage_code')),
            )
            window["alert"] = evaluate_window(window, station_specs, diseases, phenology, rule)
            window["alert"]["condition_adaptations"] = adaptations
            if adaptations:
                window["alert"]["classification_basis"] = "explicit_soil_unit_criterion"
                window["alert"]["simple_explanation"] = (
                    window["alert"].get("simple_explanation", "") +
                    " La condición del suelo usa el criterio configurado para la unidad del sensor; "
                    "no se convirtió la lectura entre porcentaje y tensión."
                ).strip()
            alert = window['alert']
            alert['confidence_pct'] = round(alert.get('confidence', 0) * 100, 1)
            window['quality'] = {
                'completion_percentage': alert.get('completion_percentage', 0),
                'record_completeness_percentage': window['completeness_pct'],
                'variable_completeness_percentage': alert.get('completion_percentage', 0),
                'missing_variables': [condition['label'] for condition in alert.get('conditions', []) if condition['status'] == 'missing'],
                'outliers': window['outliers'],
                'notes': [item['message'] for item in window['data_issues']],
                'data_issues': window['data_issues'],
                'missing_reasons': alert.get('missing_reasons', []),
            }
        regressions.append(
            {
                "station_id": str(station.id),
                "name": unescape(station.name),
                **historical_regression(
                    readings,
                    reference,
                    station.timezone,
                    expected_interval_minutes=station.expected_interval_minutes,
                ),
            }
        )
    spatial_coverage = coverage_percentage(plot.geojson, influences)
    windows = {}
    for hours in (window_hours,):
        rows = [{"station_id": sid, **items[str(hours)]} for sid, items in station_windows.items()]
        valid = [row for row in rows if row["alert"]["level"] != "DATOS_INSUFICIENTES"]
        valid_ids = {row['station_id'] for row in valid}
        valid_coverage = coverage_percentage(
            plot.geojson, [item for item in influences if item['station_id'] in valid_ids]
        )
        # A peripheral station missing a sensor must not erase a complete,
        # independently usable station covering the whole polygon.
        complete = bool(valid) and valid_coverage >= 99
        worst = max(valid, key=lambda row: LEVEL_ORDER.get(row["alert"]["level"], -1), default=None)
        windows[str(hours)] = {
            "hours": hours,
            "stations": rows,
            "usable_coverage_pct": valid_coverage,
            "level": worst["alert"]["level"] if complete and worst else "DATOS_INSUFICIENTES",
            "representative_alert": worst["alert"] if complete and worst else (rows[0]["alert"] if rows else None),
            "aggregation": "worst_station_without_merging_measurements_if_valid_coverage_at_least_99_pct",
        }
    forecast: dict[str, Any] = {"status": "NOT_AVAILABLE", "days": []}
    previous = await db.scalar(
        select(PlotAnalysis)
        .where(PlotAnalysis.plot_id == plot.id)
        .order_by(PlotAnalysis.created_at.desc())
        .limit(1)
    )
    if previous:
        old = previous.context
        if (
            old.get("plot", {}).get("geojson") == plot.geojson
            and old.get("forecast", {}).get("status") == "COMPLETED"
            and old.get("forecast", {}).get("generated_at")
            and timedelta(0)
            <= reference - datetime.fromisoformat(old["forecast"]["generated_at"])
            < timedelta(hours=6)
            and len(next_three_forecast_days(old["forecast"].get("days", []), local_date)) == 3
        ):
            forecast = {**old["forecast"], "days": next_three_forecast_days(old["forecast"]["days"], local_date)}
    if refresh_forecast and forecast["status"] != "COMPLETED":
        try:
            days = await fetch_open_meteo(float(plot.latitude), float(plot.longitude), days=4)
            forecast = {
                "status": "COMPLETED",
                "provider": "OPEN_METEO",
                "generated_at": reference.isoformat(),
                "latitude": plot.latitude,
                "longitude": plot.longitude,
                "days": next_three_forecast_days(days, local_date),
            }
        except Exception:
            forecast = {"status": "UNAVAILABLE", "days": []}
    satellite, radar_data, point = await plot_satellite_context(
        db, plot, reference, **({'observation_id': satellite_observation_id} if satellite_observation_id else {}),
    )
    context = {
        "protocol": "plot-intelligence-v1",
        "alert_scale": "six-conditions-v2",
        "calculation_engine": "python",
        "reference_at": reference.isoformat(),
        "plot": {**model_dict(plot, exclude={"deleted_at", "created_by", "producer_document", "browser_session_id", "geometry_key"}), "crop_code": crop.code},
        "crop": {"id": str(crop.id), "code": crop.code, "name": crop.name},
        "station_influences": influences,
        "spatial_coverage_pct": spatial_coverage,
        "raw_readings": raw,
        "selected_window_hours": window_hours,
        "windows": windows,
        "phenology": phenology,
        "rule_version": rule.version if rule else None,
        "radar": radar_data,
        "satellite": satellite,
        "satellite_selection": {"mode": "selected" if satellite_observation_id else "latest",
                                "observation_id": str(satellite_observation_id) if satellite_observation_id else None},
        "inspection_point": point,
        "forecast": forecast,
        "historical_regression": regressions,
        "possible_diseases": [
            {"hours": h, "station_id": sid, "diseases": w["alert"].get("diseases", [])}
            for sid, ws in station_windows.items()
            for h, w in ws.items()
        ],
        "limitations": [
            "Etapa fenológica estimada por calendario; confirmar en campo.",
            "Riesgo de enfermedad no equivale a diagnóstico confirmado.",
            "Las condiciones de lluvia y mojado se comparan como equivalentes diarios.",
        ]
        + (["Cobertura espacial incompleta."] if spatial_coverage < 99 else []),
        "llm_contract": (
            "Interpretar exclusivamente; prohibido calcular, extrapolar cifras o inventar coordenadas."
        ),
    }
    analysis = PlotAnalysis(
        plot_id=plot.id, reference_at=reference, context=context, agent_results={}, created_by=created_by
    )
    db.add(analysis)
    await db.flush()
    await db.refresh(analysis)
    return analysis
