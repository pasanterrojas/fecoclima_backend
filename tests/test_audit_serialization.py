from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from app.services.audit import _json_safe


class ExampleLevel(str, Enum):
    MODERATE = "MODERATE"


def test_audit_payload_is_json_serializable() -> None:
    identifier = uuid4()
    payload = {
        "last_run_at": datetime(2026, 7, 24, 23, 11, 10, tzinfo=UTC),
        "delivery_date": date(2026, 7, 24),
        "identifier": identifier,
        "threshold": Decimal("0.75"),
        "level": ExampleLevel.MODERATE,
        "nested": [{"created_at": datetime(2026, 7, 24, 23, 12, tzinfo=UTC)}],
    }

    encoded = _json_safe(payload)

    assert encoded["last_run_at"] == "2026-07-24T23:11:10Z"
    assert encoded["delivery_date"] == "2026-07-24"
    assert encoded["identifier"] == str(identifier)
    assert encoded["level"] == "MODERATE"
    json.dumps(encoded)
