from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base

JSONType = JSON().with_variant(JSONB, "postgresql")


def uuid4() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SoftDeleteMixin:
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Role(Base, TimestampMixin):
    __tablename__ = "roles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(180))
    password_hash: Mapped[str] = mapped_column(String(500))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"), index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_version: Mapped[int] = mapped_column(Integer, default=1)


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Station(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "stations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(180), index=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    external_guid: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    description: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(String(80), default="Paraguay")
    department: Mapped[str | None] = mapped_column(String(120))
    district: Mapped[str | None] = mapped_column(String(120))
    locality: Mapped[str | None] = mapped_column(String(120))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(80), default="America/Asuncion")
    source_type: Mapped[str] = mapped_column(String(50), default="MANUAL", index=True)
    expected_interval_minutes: Mapped[int] = mapped_column(Integer, default=10)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    import_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    integration_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    available_variables: Mapped[list[str]] = mapped_column(JSONType, default=list)
    leaf_sensor_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    influence_geojson: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    influence_radius_m: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class StationIntegration(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "station_integrations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    integration_type: Mapped[str] = mapped_column(String(50), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("station_id", "integration_type", "provider", name="uq_station_integration"),)


class WeatherReading(Base, TimestampMixin):
    __tablename__ = "weather_readings"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(40), default="measured", index=True)
    source_reference: Mapped[str | None] = mapped_column(String(255))
    temperature_c: Mapped[float | None] = mapped_column(Float)
    feels_like_c: Mapped[float | None] = mapped_column(Float)
    heat_index_c: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)
    dew_point_c: Mapped[float | None] = mapped_column(Float)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float)
    wind_gust_ms: Mapped[float | None] = mapped_column(Float)
    wind_direction_deg: Mapped[float | None] = mapped_column(Float)
    rain_mm: Mapped[float | None] = mapped_column(Float)
    total_rain_mm: Mapped[float | None] = mapped_column(Float)
    pressure_hpa: Mapped[float | None] = mapped_column(Float)
    solar_wm2: Mapped[float | None] = mapped_column(Float)
    evapotranspiration_mm: Mapped[float | None] = mapped_column(Float)
    uv_index: Mapped[float | None] = mapped_column(Float)
    min_temperature_c: Mapped[float | None] = mapped_column(Float)
    max_temperature_c: Mapped[float | None] = mapped_column(Float)
    min_temperature_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_temperature_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    soil_temperature_c: Mapped[float | None] = mapped_column(Float)
    soil_moisture_pct: Mapped[float | None] = mapped_column(Float)
    leaf_sensor_value: Mapped[float | None] = mapped_column(Float)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    quality_flags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    imported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (
        UniqueConstraint("station_id", "sampled_at", name="uq_reading_station_sampled"),
        Index("ix_weather_station_sampled_desc", "station_id", sampled_at.desc()),
    )


class WeatherDailySummary(Base, TimestampMixin):
    __tablename__ = "weather_daily_summaries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    summary_date: Mapped[date] = mapped_column(Date, index=True)
    timezone: Mapped[str] = mapped_column(String(80), default="America/Asuncion")
    temperature_avg_c: Mapped[float | None] = mapped_column(Float)
    humidity_avg_pct: Mapped[float | None] = mapped_column(Float)
    rain_sum_mm: Mapped[float | None] = mapped_column(Float)
    soil_temperature_avg_c: Mapped[float | None] = mapped_column(Float)
    soil_moisture_avg_pct: Mapped[float | None] = mapped_column(Float)
    leaf_wetness_hours: Mapped[float | None] = mapped_column(Float)
    solar_sum_wm2_samples: Mapped[float | None] = mapped_column(Float)
    evapotranspiration_sum_mm: Mapped[float | None] = mapped_column(Float)
    records_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_records: Mapped[int] = mapped_column(Integer, default=0)
    completeness_pct: Mapped[float] = mapped_column(Float, default=0)
    outlier_count: Mapped[int] = mapped_column(Integer, default=0)
    sources: Mapped[list[str]] = mapped_column(JSONType, default=list)
    __table_args__ = (UniqueConstraint("station_id", "summary_date", name="uq_daily_station_date"),)


class Crop(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "crops"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    rules_status: Mapped[str] = mapped_column(String(40), default="pending")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Plot(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "plots"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(180), index=True)
    producer_name: Mapped[str | None] = mapped_column(String(180))
    crop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crops.id", ondelete="SET NULL"), index=True)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="RESTRICT"), index=True)
    area_ha: Mapped[float | None] = mapped_column(Numeric(12, 3))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geojson: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    influence_radius_m: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CropCycle(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "crop_cycles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(180))
    plot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plots.id", ondelete="SET NULL"), index=True)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="RESTRICT"), index=True)
    crop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crops.id", ondelete="RESTRICT"), index=True)
    planting_date: Mapped[date] = mapped_column(Date, index=True)
    sowing_type: Mapped[str | None] = mapped_column(String(80))
    cycle_type: Mapped[str | None] = mapped_column(String(80))
    variety: Mapped[str | None] = mapped_column(String(120))
    estimated_harvest_date: Mapped[date | None] = mapped_column(Date)
    current_stage: Mapped[str | None] = mapped_column(String(80))
    stage_method: Mapped[str] = mapped_column(String(50), default="CALCULATED")
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True)
    observations: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class PhenologyStage(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "phenology_stages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    crop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crops.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str | None] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer)
    duration_days: Mapped[int] = mapped_column(Integer)
    start_day: Mapped[int] = mapped_column(Integer)
    end_day: Mapped[int] = mapped_column(Integer)
    cycle_type: Mapped[str] = mapped_column(String(80), default="normal")
    __table_args__ = (UniqueConstraint("crop_id", "code", "cycle_type", name="uq_phenology_crop_code_cycle"),)


class PhenologyHistory(Base, TimestampMixin):
    __tablename__ = "phenology_history"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    crop_cycle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crop_cycles.id", ondelete="CASCADE"), index=True)
    previous_stage: Mapped[str | None] = mapped_column(String(80))
    new_stage: Mapped[str] = mapped_column(String(80))
    method: Mapped[str] = mapped_column(String(50))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notes: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CropRuleSet(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "crop_rule_sets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    crop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CropRuleVersion(Base, TimestampMixin):
    __tablename__ = "crop_rule_versions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    rule_set_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crop_rule_sets.id", ondelete="CASCADE"), index=True)
    crop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", index=True)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    substitution_rules: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("crop_id", "version", name="uq_rule_crop_version"),)


class BaseCondition(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "base_conditions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crop_rule_versions.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(180))
    variable: Mapped[str] = mapped_column(String(100))
    operator: Mapped[str] = mapped_column(String(30))
    threshold_min: Mapped[float | None] = mapped_column(Float)
    threshold_max: Mapped[float | None] = mapped_column(Float)
    threshold_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    aggregation: Mapped[str] = mapped_column(String(50), default="average")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("rule_version_id", "key", name="uq_base_condition_version_key"),)


class DiseaseRule(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "disease_rules"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crop_rule_versions.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(220), index=True)
    causal_agent: Mapped[str | None] = mapped_column(String(300))
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    valid_stages: Mapped[list[str]] = mapped_column(JSONType, default=list)
    validation_status: Mapped[str] = mapped_column(String(50), default="PENDING_TECHNICAL", index=True)
    source_reference: Mapped[str | None] = mapped_column(Text)
    enabled_for_evaluation: Mapped[bool] = mapped_column(Boolean, default=False)


class AlertEvaluation(Base, TimestampMixin):
    __tablename__ = "alert_evaluations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    crop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crops.id", ondelete="CASCADE"), index=True)
    crop_cycle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crop_cycles.id", ondelete="SET NULL"), index=True)
    planting_date: Mapped[date | None] = mapped_column(Date, index=True)
    rule_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crop_rule_versions.id", ondelete="SET NULL"))
    evaluation_date: Mapped[date] = mapped_column(Date, index=True)
    stage_code: Mapped[str | None] = mapped_column(String(80))
    level: Mapped[str] = mapped_column(String(40), index=True)
    conditions_met: Mapped[int] = mapped_column(Integer, default=0)
    conditions_total: Mapped[int] = mapped_column(Integer, default=6)
    completion_pct: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    values_used: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    diseases: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    data_sources: Mapped[list[str]] = mapped_column(JSONType, default=list)
    explanation: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("station_id", "crop_id", "planting_date", "evaluation_date", name="uq_alert_context_planting_date"),
        Index("ix_alert_station_crop_date", "station_id", "crop_id", evaluation_date.desc()),
    )


class AlertConditionResult(Base, TimestampMixin):
    __tablename__ = "alert_condition_results"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    alert_evaluation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_evaluations.id", ondelete="CASCADE"), index=True)
    condition_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("base_conditions.id", ondelete="SET NULL"))
    key: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(30))
    observed_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    explanation: Mapped[str | None] = mapped_column(Text)


class AIAgent(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ai_agents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(180))
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    crop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crops.id", ondelete="SET NULL"), index=True)
    purpose: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    current_version: Mapped[str] = mapped_column(String(40), default="1.0.0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class AIAgentVersion(Base, TimestampMixin):
    __tablename__ = "ai_agent_versions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_agents.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(40))
    system_prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(120))
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1400)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    rag_collection: Mapped[str | None] = mapped_column(String(120))
    allowed_rules: Mapped[list[str]] = mapped_column(JSONType, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_version"),)


class AIAgentRun(Base, TimestampMixin):
    __tablename__ = "ai_agent_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_agents.id", ondelete="SET NULL"), index=True)
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_agent_versions.id", ondelete="SET NULL"), index=True)
    alert_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("alert_evaluations.id", ondelete="SET NULL"), index=True)
    report_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reports.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    model: Mapped[str | None] = mapped_column(String(120))
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)


class KnowledgeDocument(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "knowledge_documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(300), index=True)
    crop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crops.id", ondelete="SET NULL"), index=True)
    institution: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[str | None] = mapped_column(String(80))
    filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    storage_path: Mapped[str | None] = mapped_column(String(500))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    source_text: Mapped[str | None] = mapped_column(Text)
    index_status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    index_error: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class AgentKnowledgeDocument(Base, TimestampMixin):
    __tablename__ = "agent_knowledge_documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_agents.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (UniqueConstraint("agent_id", "document_id", name="uq_agent_knowledge_document"),)


class KnowledgeChunk(Base, TimestampMixin):
    __tablename__ = "knowledge_chunks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    crop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crops.id", ondelete="SET NULL"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    page_or_section: Mapped[str | None] = mapped_column(String(120))
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.ai_embedding_dimensions), nullable=True)
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunk_document_index"),)


class IntegrationConfig(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "integration_configs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(180), unique=True)
    integration_type: Mapped[str] = mapped_column(String(60), index=True)
    provider: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str | None] = mapped_column(String(500))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    retry_count: Mapped[int] = mapped_column(Integer, default=3)
    sync_frequency_minutes: Mapped[int | None] = mapped_column(Integer)
    non_sensitive_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    encrypted_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_hint: Mapped[str | None] = mapped_column(String(40))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(40))
    request_limit: Mapped[int | None] = mapped_column(Integer)
    cache_strategy: Mapped[str | None] = mapped_column(String(120))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class IntegrationLog(Base, TimestampMixin):
    __tablename__ = "integration_logs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    integration_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("integration_configs.id", ondelete="SET NULL"), index=True)
    station_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stations.id", ondelete="SET NULL"), index=True)
    operation: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    records_received: Mapped[int] = mapped_column(Integer, default=0)
    records_created: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SatelliteObservation(Base, TimestampMixin):
    __tablename__ = "satellite_observations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    plot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plots.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider: Mapped[str] = mapped_column(String(120))
    cloud_percentage: Mapped[float | None] = mapped_column(Float)
    ndvi: Mapped[float | None] = mapped_column(Float)
    ndwi: Mapped[float | None] = mapped_column(Float)
    ndmi: Mapped[float | None] = mapped_column(Float)
    lai: Mapped[float | None] = mapped_column(Float)
    previous_comparison: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    anomalies: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    resolution_m: Mapped[float | None] = mapped_column(Float)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class WeatherForecast(Base, TimestampMixin):
    __tablename__ = "weather_forecasts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    forecast_date: Mapped[date] = mapped_column(Date, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider: Mapped[str] = mapped_column(String(120))
    temperature_min_c: Mapped[float | None] = mapped_column(Float)
    temperature_max_c: Mapped[float | None] = mapped_column(Float)
    precipitation_mm: Mapped[float | None] = mapped_column(Float)
    precipitation_probability_pct: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float)
    evapotranspiration_mm: Mapped[float | None] = mapped_column(Float)
    estimated_soil_moisture_pct: Mapped[float | None] = mapped_column(Float)
    disease_favorability: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    __table_args__ = (UniqueConstraint("station_id", "forecast_date", "provider", name="uq_forecast_station_date_provider"),)


class Report(Base, TimestampMixin):
    __tablename__ = "reports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(300))
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="RESTRICT"), index=True)
    crop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crops.id", ondelete="RESTRICT"), index=True)
    crop_cycle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crop_cycles.id", ondelete="SET NULL"), index=True)
    planting_date: Mapped[date | None] = mapped_column(Date, index=True)
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    rule_version: Mapped[str | None] = mapped_column(String(40))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ReportFile(Base, TimestampMixin):
    __tablename__ = "report_files"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(120), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64))


class BackgroundJob(Base, TimestampMixin):
    __tablename__ = "background_jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    job_type: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(180))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (Index("ix_jobs_claim", "status", "scheduled_at", "priority"),)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(120), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(120), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(80))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SystemSetting(Base, TimestampMixin):
    __tablename__ = "system_settings"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    description: Mapped[str | None] = mapped_column(Text)
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class DataImport(Base, TimestampMixin):
    __tablename__ = "data_imports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_created: Mapped[int] = mapped_column(Integer, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    error_report_path: Mapped[str | None] = mapped_column(String(500))
    imported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class DataImportError(Base, TimestampMixin):
    __tablename__ = "data_import_errors"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_imports.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer, index=True)
    column_name: Mapped[str | None] = mapped_column(String(180))
    error_code: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text)
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class DatabaseBackup(Base, TimestampMixin):
    __tablename__ = "database_backups"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(40), index=True)
    responsible_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    storage_location: Mapped[str | None] = mapped_column(String(500))
    restore_test_status: Mapped[str | None] = mapped_column(String(60))
    restore_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
