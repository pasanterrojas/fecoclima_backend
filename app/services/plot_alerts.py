"""Restricted Spanish rules compiled and evaluated without LLMs or eval()."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plot, PlotAlertDelivery, PlotAlertRule, PlotAnalysis
from app.services.plot_pipeline import build_plot_analysis, utc
from app.services.producer_notifications import (
    _send_email,
    _send_telegram,
    get_notification_integration,
    telegram_bot_token,
)
from app.services.weather import VALID_RANGES

VARIABLES = {
    "humedad": "humidity_pct",
    "humedad relativa": "humidity_pct",
    "temperatura": "temperature_c",
    "temp": "temperature_c",
    "lluvia": "rain_mm",
    "humedad del suelo": "soil_moisture_pct",
    "temperatura del suelo": "soil_temperature_c",
    "viento": "wind_speed_ms",
}
OPERATORS = {
    "supera": "gt",
    "es mayor a": "gt",
    "es mayor que": "gt",
    ">": "gt",
    "es menor a": "lt",
    "es menor que": "lt",
    "baja de": "lt",
    "<": "lt",
    "es al menos": "gte",
    ">=": "gte",
    "es como maximo": "lte",
    "<=": "lte",
    "es igual a": "eq",
    "=": "eq",
}
UNITS = {
    "humidity_pct": "%",
    "temperature_c": "°C",
    "rain_mm": "mm",
    "soil_moisture_pct": "%",
    "soil_temperature_c": "°C",
    "wind_speed_ms": "m/s",
}


def compile_rule(text: str) -> dict:
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", text.lower().strip()) if unicodedata.category(c) != "Mn"
    )
    normalized = re.sub(r"^(?:avisame|notificame|alertame)\s+(?:si|cuando)\s+", "", normalized).rstrip(". ")
    scope = "latest"
    window_match = re.search(r"\s+en\s+(60|72|96|120)\s+horas$", normalized)
    if window_match:
        scope = window_match.group(1)
        normalized = normalized[: window_match.start()]
    pieces = re.split(r"\s+(y|o)\s+", normalized)
    connectors = set(pieces[1::2])
    if len(connectors) > 1 or len(pieces[::2]) > 4:
        raise ValueError("Use hasta cuatro condiciones unidas solo por 'y' o solo por 'o'")
    conditions = []
    var_pattern = "|".join(re.escape(v) for v in sorted(VARIABLES, key=len, reverse=True))
    op_pattern = "|".join(re.escape(v) for v in sorted(OPERATORS, key=len, reverse=True))
    for piece in pieces[::2]:
        match = re.fullmatch(
            rf"(?:la |el )?({var_pattern})\s*({op_pattern})\s*(-?\d+(?:[.,]\d+)?)\s*(%|°c|c|mm|m/s)?",
            piece.strip(),
        )
        if not match:
            raise ValueError(
                "Regla no reconocida. Ejemplo: Avísame si la humedad supera 80% "
                "y la temperatura es mayor a 25°C"
            )
        variable, operator, number, unit = match.groups()
        key = VARIABLES[variable]
        if unit and unit.replace("°", "").lower() != UNITS[key].replace("°", "").lower():
            raise ValueError(f"Unidad inválida para {variable}; use {UNITS[key]}")
        value = float(number.replace(",", "."))
        if abs(value) > 100000 or (key.endswith("_pct") and not 0 <= value <= 100):
            raise ValueError("Umbral fuera de rango")
        if scope != "latest" and key in {"rain_mm", "wind_speed_ms"}:
            raise ValueError("Para lluvia o viento use la última lectura")
        conditions.append(
            {"variable": key, "operator": OPERATORS[operator], "value": value, "unit": UNITS[key]}
        )
    return {
        "version": 1,
        "scope": scope,
        "mode": "any" if connectors == {"o"} else "all",
        "conditions": conditions,
    }


def matches_rule(expression: dict, context: dict) -> bool:
    from operator import eq, ge, gt, le, lt

    operators = {"gt": gt, "gte": ge, "lt": lt, "lte": le, "eq": eq}
    scope = expression["scope"]
    if scope == "latest":
        reference = datetime.fromisoformat(context["reference_at"])
        sources = []
        for entry in context.get("raw_readings", []):
            row = entry.get("reading")
            if row and timedelta(0) <= reference - datetime.fromisoformat(row["sampled_at"]) <= timedelta(
                hours=1
            ):
                values = dict(row)
                if entry.get("sensor_units", {}).get("soil_moisture_pct", "%") != "%":
                    values["soil_moisture_pct"] = None
                sources.append(values)
    else:
        keys = {
            "humidity_pct": "humidity_avg_pct",
            "temperature_c": "temperature_avg_c",
            "soil_moisture_pct": "soil_moisture_avg_pct",
            "soil_temperature_c": "soil_temperature_avg_c",
        }
        sources = [
            {
                key: (
                    None
                    if key == "soil_moisture_pct"
                    and row.get("units", {}).get("soil_moisture_avg_pct", "%") != "%"
                    else row["values"].get(target)
                )
                for key, target in keys.items()
            }
            for row in context.get("windows", {}).get(scope, {}).get("stations", [])
            if not row["stale"] and row["completeness_pct"] >= 80
        ]
    for values in sources:
        checks = []
        for condition in expression["conditions"]:
            value = values.get(condition["variable"])
            valid = isinstance(value, int | float) and math.isfinite(value)
            bounds = VALID_RANGES.get(condition["variable"])
            if valid and bounds:
                valid = bounds[0] <= value <= bounds[1]
            checks.append(valid and operators[condition["operator"]](value, condition["value"]))
        if checks and (all(checks) if expression["mode"] == "all" else any(checks)):
            return True
    return False


async def monitor_plot_rules(db: AsyncSession) -> dict:
    now = datetime.now(UTC)
    rules = list(
        (
            await db.scalars(
                select(PlotAlertRule)
                .where(PlotAlertRule.active.is_(True), PlotAlertRule.deleted_at.is_(None))
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    analyses = {}
    queued = 0
    for rule in rules:
        if rule.last_triggered_at and now - utc(rule.last_triggered_at) < timedelta(
            minutes=rule.cooldown_minutes
        ):
            continue
        plot = await db.get(Plot, rule.plot_id)
        if (
            not plot
            or not plot.active
            or plot.deleted_at
            or not plot.geojson
            or not plot.crop_id
            or not plot.planting_date
        ):
            continue
        if plot.id not in analyses:
            try:
                analyses[plot.id] = await build_plot_analysis(db, plot, reference=now)
            except ValueError:
                analyses[plot.id] = None
        analysis = analyses[plot.id]
        if analysis is None:
            continue
        if matches_rule(rule.expression, analysis.context):
            db.add(PlotAlertDelivery(rule_id=rule.id, analysis_id=analysis.id, status="PENDING", attempts=0))
            rule.last_triggered_at = now
            queued += 1
    await db.commit()
    return {"rules": len(rules), "queued": queued}


async def dispatch_plot_alerts(db: AsyncSession) -> dict:
    now = datetime.now(UTC)
    deliveries = list(
        (
            await db.scalars(
                select(PlotAlertDelivery)
                .where(
                    PlotAlertDelivery.attempts < 3,
                    or_(
                        PlotAlertDelivery.status == "PENDING",
                        (PlotAlertDelivery.status == "FAILED")
                        & (PlotAlertDelivery.updated_at < now - timedelta(minutes=5)),
                        (PlotAlertDelivery.status == "SENDING")
                        & (PlotAlertDelivery.updated_at < now - timedelta(minutes=10)),
                    ),
                )
                .order_by(PlotAlertDelivery.created_at)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    ids = [row.id for row in deliveries]
    for row in deliveries:
        row.status = "SENDING"
        row.attempts += 1
    await db.commit()
    sent = 0
    for delivery_id in ids:
        row = await db.get(PlotAlertDelivery, delivery_id)
        rule = await db.get(PlotAlertRule, row.rule_id)
        analysis = await db.get(PlotAnalysis, row.analysis_id)
        plot = await db.get(Plot, analysis.plot_id)
        if not rule or not rule.active or rule.deleted_at or not plot or not plot.active or plot.deleted_at:
            row.status = "CANCELLED"
            await db.commit()
            continue
        message = (
            f"FECOCLIMA IA · {analysis.context['plot']['name']}\n\n{rule.natural_language}\n\n"
            f"Condición detectada en datos de estación a {analysis.context['reference_at']}. "
            "Revise la parcela y confirme el estado en campo."
        )
        try:
            if rule.channel == "EMAIL":
                await _send_email([rule.recipient], "FECOCLIMA IA · Aviso de parcela", message)
            else:
                token = telegram_bot_token(await get_notification_integration(db))
                if not token:
                    raise ValueError("Telegram no configurado")
                await _send_telegram(token, rule.recipient, message)
            row.status, row.sent_at, row.error = "SENT", datetime.now(UTC), None
            sent += 1
        except Exception:
            row.status, row.error = "FAILED", "No se pudo entregar; compruebe el canal y destinatario"
        await db.commit()
    return {"sent": sent, "attempted": len(ids)}
