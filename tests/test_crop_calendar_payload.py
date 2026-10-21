"""Los meses y la disponibilidad se incluyen en el contrato de escritura del CMS."""

import pytest
from pydantic import ValidationError

from app.schemas.resources import CropPayload


def test_months_remain_in_payload_and_availability_can_be_false():
    crop = CropPayload(name="Trigo", code="TRIGO", planting_months=[11, 4, 4, 2], active=False)
    assert crop.model_dump()["planting_months"] == [2, 4, 11]
    assert crop.model_dump()["active"] is False
    restored = CropPayload.model_validate(crop.model_dump())
    assert restored.planting_months == [2, 4, 11]
    assert restored.active is False


def test_invalid_month_is_rejected():
    with pytest.raises(ValidationError):
        CropPayload(name="Trigo", code="TRIGO", planting_months=[0, 13])


def test_empty_months_are_allowed_for_unconfigured_crop():
    assert CropPayload(name="Trigo", code="TRIGO").planting_months == []
