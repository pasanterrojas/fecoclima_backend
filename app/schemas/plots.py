from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.schemas.resources import PlotPayload


class ParcelPayload(PlotPayload):
    crop_id: UUID
    planting_date: date
    geojson: dict[str, Any] | str


class ParcelCoveragePayload(BaseModel):
    geojson: dict[str, Any] | str


class ParcelAnalysisPayload(BaseModel):
    window_hours: Literal[60, 72, 96, 120] = 72


class PlotInterpretPayload(BaseModel):
    purpose: str = "ORCHESTRATOR"
    force: bool = False
    view_mode: Literal["producer", "technical"] = "producer"
    window_hours: Literal[60, 72, 96, 120] = 72


class CompileRulePayload(BaseModel):
    natural_language: str = Field(min_length=5, max_length=1000)


class PlotAlertPayload(CompileRulePayload):
    channel: Literal["EMAIL", "TELEGRAM"]
    recipient: str = Field(min_length=1, max_length=255)
    cooldown_minutes: int = Field(default=180, ge=30, le=10080)
    active: bool = True

    @model_validator(mode="after")
    def validate_recipient(self):
        self.recipient = self.recipient.strip()
        if self.channel == "EMAIL":
            from pydantic import TypeAdapter

            self.recipient = str(TypeAdapter(EmailStr).validate_python(self.recipient))
        elif not re.fullmatch(r"-?\d{4,30}", self.recipient):
            raise ValueError("Telegram requiere un chat_id numérico")
        from app.services.plot_alerts import compile_rule

        compile_rule(self.natural_language)
        return self
