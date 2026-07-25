from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class DateRangePayload(BaseModel):
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def validate_range(self):
        if self.date_to < self.date_from:
            raise ValueError("date_to no puede ser anterior a date_from")
        if (self.date_to - self.date_from).days > 3660:
            raise ValueError("El rango no puede superar 10 años")
        return self


class StationSyncPayload(DateRangePayload):
    crop_id: UUID | None = None
    planting_date: date | None = None
    run_in_background: bool = True


class AlertEvaluatePayload(DateRangePayload):
    station_id: UUID
    crop_id: UUID
    planting_date: date
    force_recalculate: bool = False
    run_in_background: bool = False


class ForecastRefreshPayload(BaseModel):
    station_id: UUID
    run_in_background: bool = True


class SatelliteObservationPayload(BaseModel):
    plot_id: UUID
    captured_at: datetime
    provider: str = Field(min_length=2, max_length=120)
    cloud_percentage: float | None = Field(default=None, ge=0, le=100)
    ndvi: float | None = Field(default=None, ge=-1, le=1)
    ndwi: float | None = Field(default=None, ge=-1, le=1)
    ndmi: float | None = Field(default=None, ge=-1, le=1)
    lai: float | None = Field(default=None, ge=0, le=15)
    previous_comparison: dict[str, Any] = Field(default_factory=dict)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    source_url: str | None = Field(default=None, max_length=1000)
    resolution_m: float | None = Field(default=None, gt=0)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class SatelliteSyncPayload(BaseModel):
    plot_id: UUID
    date_from: date
    date_to: date
    provider: Literal["COPERNICUS_DATA_SPACE", "MANUAL"] = "COPERNICUS_DATA_SPACE"
    max_cloud_percentage: float = Field(default=40, ge=0, le=100)
    run_in_background: bool = True

    @model_validator(mode="after")
    def validate_range(self):
        if self.date_to < self.date_from:
            raise ValueError("date_to no puede ser anterior a date_from")
        if (self.date_to - self.date_from).days > 366:
            raise ValueError("La consulta satelital no puede superar 366 días")
        return self


class PublicSatelliteRefreshPayload(BaseModel):
    """Refresh Copernicus evidence for the public analysis context.

    The crop is required because a station can have more than one geozone and
    the satellite evidence shown to the agent must correspond to the selected
    crop whenever a crop-specific parcel exists.
    """

    station_id: UUID
    crop_code: str = Field(min_length=1, max_length=30)
    force: bool = True


class PublicSatelliteMapProcessPayload(BaseModel):
    station_id: UUID
    crop_code: str = Field(min_length=1, max_length=30)
    scene_id: str | None = Field(default=None, min_length=1, max_length=500)
    captured_date: date | None = None
    history_days: int | None = Field(default=None, ge=7, le=366)
    force: bool = True

    @model_validator(mode="after")
    def validate_scene(self):
        if not self.scene_id and not self.captured_date:
            raise ValueError("Seleccione una escena o una fecha para procesar")
        return self


class PublicSatelliteMapAgentPayload(BaseModel):
    station_id: UUID
    crop_code: str = Field(min_length=1, max_length=30)
    observation_id: UUID
    force: bool = False


class PhenologyStagePayload(BaseModel):
    crop_id: UUID
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=2, max_length=180)
    description: str | None = None
    order_index: int = Field(ge=0)
    duration_days: int = Field(ge=0, le=500)
    start_day: int = Field(ge=0, le=1000)
    end_day: int = Field(ge=0, le=1500)
    cycle_type: str = Field(default="normal", min_length=1, max_length=80)
    active: bool = True

    @model_validator(mode="after")
    def validate_days(self):
        if self.end_day < self.start_day:
            raise ValueError("end_day no puede ser anterior a start_day")
        return self


class CycleStageUpdatePayload(BaseModel):
    stage_code: str = Field(min_length=1, max_length=80)
    method: Literal["MANUAL", "CALCULATED", "IMPORTED"] = "MANUAL"
    notes: str | None = Field(default=None, max_length=3000)


class BaseConditionPayload(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=2, max_length=180)
    variable: str = Field(min_length=1, max_length=100)
    operator: Literal["between", "gt", "gte", "lt", "lte", "eq"]
    threshold_min: float | None = None
    threshold_max: float | None = None
    threshold_value: float | None = None
    unit: str | None = Field(default=None, max_length=40)
    aggregation: str = Field(default="daily", max_length=50)
    required: bool = True
    order_index: int = Field(ge=1, le=50)
    active: bool = True

    @model_validator(mode="after")
    def validate_threshold(self):
        if self.operator == "between":
            if self.threshold_min is None or self.threshold_max is None:
                raise ValueError("between requiere threshold_min y threshold_max")
            if self.threshold_max < self.threshold_min:
                raise ValueError("threshold_max no puede ser menor que threshold_min")
        elif self.threshold_value is None:
            raise ValueError(f"{self.operator} requiere threshold_value")
        return self


class DiseaseRulePayload(BaseModel):
    name: str = Field(min_length=2, max_length=220)
    causal_agent: str | None = Field(default=None, max_length=300)
    conditions: dict[str, Any] = Field(default_factory=dict)
    valid_stages: list[str] = Field(default_factory=list)
    validation_status: Literal["DRAFT", "PENDING_TECHNICAL", "PUBLISHED", "INACTIVE"] = "PENDING_TECHNICAL"
    source_reference: str | None = None
    enabled_for_evaluation: bool = False
    active: bool = True

    @model_validator(mode="after")
    def validate_publish(self):
        if self.enabled_for_evaluation and self.validation_status != "PUBLISHED":
            raise ValueError("Una enfermedad solo puede evaluarse cuando validation_status es PUBLISHED")
        if self.validation_status == "PUBLISHED" and not self.conditions:
            raise ValueError("Una enfermedad publicada requiere condiciones medibles")
        return self


class RuleSimulationPayload(DateRangePayload):
    station_id: UUID
    planting_date: date | None = None
    persist: bool = False


class BackgroundJobPayload(BaseModel):
    job_type: Literal[
        "STATION_SYNC",
        "AGGREGATE_ALERTS",
        "REPORT_GENERATE",
        "RAG_INDEX",
        "FORECAST_REFRESH",
        "SATELLITE_SYNC",
        "DATABASE_BACKUP",
        "CLEANUP_FILES",
        "DAILY_PRODUCER_DIGEST",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=1, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=10)


class BackupCreatePayload(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    run_in_background: bool = True


class PublicReportAccessPayload(BaseModel):
    public_access: bool


class PhenologyAgentPayload(BaseModel):
    station_id: UUID
    crop_code: str = Field(min_length=1, max_length=30)
    planting_date: date
    cycle_type: str = Field(default="normal", min_length=1, max_length=80)
    force: bool = False


class NotificationContextPayload(BaseModel):
    station_id: UUID
    crop_code: str = Field(min_length=1, max_length=30)
    planting_date: date
    label: str | None = Field(default=None, max_length=180)


class ProducerNotificationSettingsPayload(BaseModel):
    active: bool = False
    timezone: str = Field(default="America/Asuncion", min_length=1, max_length=80)
    send_hour: int = Field(default=6, ge=0, le=23)
    email_enabled: bool = False
    recipients: list[EmailStr] = Field(default_factory=list)
    telegram_enabled: bool = False
    telegram_bot_token: str | None = Field(default=None, max_length=300)
    telegram_minimum_level: Literal["MODERADA", "GRAVE"] = "MODERADA"
    contexts: list[NotificationContextPayload] = Field(default_factory=list)

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_telegram_token(cls, value: str | None):
        if value is None:
            return None
        token = value.strip()
        if not token:
            return None
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError(
                "Pegue el token completo entregado por BotFather, incluyendo el ID numérico y los dos puntos"
            )
        return token

    @model_validator(mode="after")
    def validate_channels(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("La zona horaria no es válida") from exc
        self.recipients = sorted({str(item).strip().lower() for item in self.recipients if str(item).strip()})
        if self.email_enabled and not self.recipients:
            raise ValueError("Agregue al menos un destinatario de correo")
        if self.active and not self.contexts:
            raise ValueError("Agregue al menos una estación, cultivo y fecha de siembra")
        return self


class AgentAnalysisPayload(BaseModel):
    alert_id: UUID
    period: Literal["24h", "72h", "7d", "30d"] = "72h"
    force: bool = False
