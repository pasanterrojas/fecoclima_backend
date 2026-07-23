from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.inspection import inspect


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def model_dict(obj, *, exclude: set[str] | None = None) -> dict:
    excluded = exclude or set()
    result = {}
    for column in inspect(obj).mapper.column_attrs:
        key = column.key
        if key in excluded:
            continue
        result[key] = json_value(getattr(obj, key))
    return result
