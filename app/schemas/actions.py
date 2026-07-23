from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


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
    cycle_id: UUID | None = None
    run_in_background: bool = True


class AlertEvaluatePayload(DateRangePayload):
    station_id: UUID
    crop_id: UUID
    crop_cycle_id: UUID | None = None
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
    crop_cycle_id: UUID | None = None
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
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=1, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=10)


class BackupCreatePayload(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    run_in_background: bool = True


class PublicReportAccessPayload(BaseModel):
    public_access: bool
