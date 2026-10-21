from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


class StationPayload(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    code: str | None = Field(default=None, min_length=2, max_length=80)
    external_guid: str | None = None
    endpoint_url: str | None = Field(default=None, max_length=1000)
    description: str | None = None
    country: str = "Paraguay"
    department: str | None = None
    district: str | None = None
    locality: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    altitude_m: float | None = None
    timezone: str = "America/Asuncion"
    source_type: str = "MANUAL"
    expected_interval_minutes: int = Field(default=10, ge=1, le=1440)
    import_config: dict[str, Any] = Field(default_factory=dict)
    integration_config: dict[str, Any] = Field(default_factory=dict)
    available_variables: list[str] = Field(default_factory=list)
    leaf_sensor_config: dict[str, Any] = Field(default_factory=dict)
    influence_geojson: dict[str, Any] | str | None = None
    influence_radius_m: int | None = Field(default=750, ge=10, le=100000)
    active: bool = True

    @field_validator("code", mode="before")
    @classmethod
    def optional_station_code(cls, value):
        if value is None:
            return None
        return str(value).strip().upper() or None

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint(cls, value):
        if not value:
            return None
        # GUID extraction happens with the other fields in validate_source.
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError("Seleccione una zona horaria válida, por ejemplo America/Asuncion") from exc
        return value

    @field_validator("integration_config")
    @classmethod
    def validate_soil_sensor_configuration(cls, value: dict[str, Any]) -> dict[str, Any]:
        from math import isfinite
        from app.services.soil_measurements import normalize_soil_unit
        config = dict(value)
        if "map_color" in config:
            import re
            if not isinstance(config["map_color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", config["map_color"]):
                raise ValueError("Seleccione un color de influencia hexadecimal válido (#RRGGBB)")
            config["map_color"] = config["map_color"].lower()
        if "soil_moisture_unit" in config:
            unit = normalize_soil_unit(config["soil_moisture_unit"])
            if unit not in {"%", "cb"}:
                raise ValueError("Confirme la unidad del sensor de suelo: % o cb (centibares); cp no es una unidad reconocida")
            config["soil_moisture_unit"] = unit
        threshold = config.get("soil_tension_wet_max_cb")
        if threshold is not None:
            try:
                numeric = float(threshold)
            except (TypeError, ValueError) as exc:
                raise ValueError("El umbral de tensión debe ser un número finito entre 0 y 200 cb") from exc
            if isinstance(threshold, bool) or not isfinite(numeric) or not 0 <= numeric <= 200:
                raise ValueError("El umbral de tensión debe estar entre 0 y 200 cb")
            config["soil_tension_wet_max_cb"] = numeric
        if config.get("soil_tension_rule_validated") is True and (
            config.get("soil_moisture_unit") != "cb"
            or config.get("soil_tension_proxy_enabled") is not True
            or threshold is None
            or not str(config.get("soil_tension_rule_reference") or "").strip()
        ):
            raise ValueError("Una alternativa de tensión validada requiere sensor cb, activación, umbral explícito y referencia técnica")
        return config

    @model_validator(mode="after")
    def validate_source(self):
        if self.endpoint_url:
            from app.services.station_registration import normalize_station_endpoint
            self.endpoint_url, self.external_guid = normalize_station_endpoint(self.endpoint_url, self.external_guid)
        if self.source_type not in {"FECOCLIMA_API", "EXCEL", "CSV", "MANUAL"}:
            raise ValueError("La fuente de la estación no está soportada")
        if self.active and self.source_type == "FECOCLIMA_API" and not self.external_guid:
            raise ValueError("Pegue la URL completa de la estación que incluye su GUID o complete el campo GUID externo proporcionado por FECOPROD")
        return self

    @field_validator("influence_geojson", mode="before")
    @classmethod
    def parse_influence_geojson(cls, value):
        if value in (None, ""):
            return None
        if isinstance(value, str):
            import json
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("El polígono debe ser un GeoJSON válido") from exc
        geometry = value.get("geometry") if isinstance(value, dict) and value.get("type") == "Feature" else value
        if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError("El polígono debe ser GeoJSON Polygon o MultiPolygon")
        coordinates = geometry.get("coordinates")
        if not coordinates:
            raise ValueError("El polígono no contiene coordenadas")
        return value


class PlotPayload(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    map_color: str = Field(default="#22a680", pattern=r"^#[0-9a-fA-F]{6}$")
    producer_name: str | None = Field(default=None, max_length=180)
    producer_document: str | None = Field(default=None, min_length=4, max_length=30, pattern=r"^[0-9A-Za-z.\- ]+$")
    crop_id: UUID | None = None
    station_id: UUID | None = None
    planting_date: date | None = None
    area_ha: float | None = Field(default=None, ge=0)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    geojson: dict[str, Any] | str | None = None
    influence_radius_m: int | None = Field(default=None, ge=10, le=100000)
    active: bool = True

    @field_validator("map_color")
    @classmethod
    def normalize_map_color(cls, value: str) -> str:
        return value.lower()

    @field_validator("geojson", mode="before")
    @classmethod
    def parse_geojson(cls, value):
        if value in (None, ""):
            return None
        from app.services.geospatial import validate_polygon
        return validate_polygon(value)


class CropPayload(BaseModel):
    code: str = Field(min_length=2, max_length=30)
    name: str = Field(min_length=2, max_length=120)
    description: str | None = None
    active: bool = True
    planting_months: list[int] = Field(default_factory=list)

    @field_validator("planting_months")
    @classmethod
    def validate_planting_months(cls, value: list[int]) -> list[int]:
        if any(month < 1 or month > 12 for month in value):
            raise ValueError("Seleccione meses entre enero y diciembre")
        return sorted(set(value))


class CropCyclePayload(BaseModel):
    name: str
    plot_id: UUID | None = None
    station_id: UUID
    crop_id: UUID
    planting_date: date
    sowing_type: str | None = None
    cycle_type: str | None = "normal"
    variety: str | None = None
    estimated_harvest_date: date | None = None
    current_stage: str | None = None
    stage_method: str = "CALCULATED"
    status: str = "ACTIVE"
    observations: str | None = None


class RulePayload(BaseModel):
    crop_id: UUID
    name: str
    description: str | None = None
    version: str
    effective_from: date | None = None
    status: str = "DRAFT"
    configuration: dict[str, Any] | str = Field(default_factory=dict)

    @field_validator("configuration", mode="before")
    @classmethod
    def parse_config(cls, value):
        if isinstance(value, str) and value.strip():
            import json
            return json.loads(value)
        return value or {}


class RuleClonePayload(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=2, max_length=180)
    description: str | None = None
    effective_from: date | None = None


class AgentPayload(BaseModel):
    name: str
    code: str
    crop_id: UUID | None = None
    purpose: str = "CROP_ANALYSIS"
    allowed_widgets: list[str] = Field(default_factory=lambda: ["raw_data", "alerts", "phenology", "diagnosis", "satellite", "diseases", "forecast", "chat"])
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    provider: str = "OPENAI"
    html_template: str | None = Field(default=None, max_length=100000)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=1400, ge=100, le=20000)
    output_schema: dict[str, Any] | str = Field(default_factory=dict)
    response_detail_level: str = "BRIEF"
    response_max_words: int = Field(default=260, ge=100, le=1200)
    response_max_sections: int = Field(default=4, ge=2, le=8)
    response_tone: str = "DIRECT_FRIENDLY"
    response_include_observed: bool = True
    response_include_forecast: bool = True
    response_include_satellite: bool = True
    response_include_year_comparison: bool = True
    response_include_sources: bool = False
    response_show_technical_values: bool = False
    response_show_system_details: bool = False
    response_use_emojis: bool = True
    response_custom_instructions: str | None = Field(default=None, max_length=6000)
    rag_collection: str | None = None
    active: bool = True

    @field_validator("purpose", mode="before")
    @classmethod
    def normalize_purpose(cls, value):
        return str(value or "CROP_ANALYSIS").strip().upper()

    @field_validator("output_schema", mode="before")
    @classmethod
    def parse_schema(cls, value):
        if isinstance(value, str) and value.strip():
            import json
            return json.loads(value)
        return value or {}

    @field_validator("response_detail_level", "response_tone", mode="before")
    @classmethod
    def normalize_response_controls(cls, value):
        return str(value or "").strip().upper()

    @model_validator(mode="after")
    def validate_scope(self):
        from app.services.agent_defaults import AGENT_PURPOSES
        allowed = set(AGENT_PURPOSES)
        if not set(self.allowed_widgets) <= {"raw_data", "alerts", "phenology", "diagnosis", "satellite", "diseases", "forecast", "chat"}:
            raise ValueError("Widgets permitidos no válidos")
        if self.provider not in {"OPENAI", "EDGE"}:
            raise ValueError("Proveedor debe ser OPENAI o EDGE")
        if self.purpose not in allowed:
            raise ValueError("Propósito de agente no soportado")
        if self.purpose in {"RAG_CROP", "ALERT_ANALYSIS", "DISEASE_ANALYSIS", "PHENOLOGY_ANALYSIS", "CROP_ANALYSIS", "DAILY_DIGEST"} and self.crop_id is None:
            raise ValueError("Este propósito requiere seleccionar un cultivo")
        if self.response_detail_level not in {"BRIEF", "BALANCED", "DETAILED"}:
            raise ValueError("Nivel de detalle de respuesta no soportado")
        if self.response_tone not in {"DIRECT_FRIENDLY", "SIMPLE_TECHNICAL", "INSTITUTIONAL"}:
            raise ValueError("Tono de respuesta no soportado")
        return self


class KnowledgePayload(BaseModel):
    title: str
    crop_id: UUID | None = None
    institution: str | None = None
    year: int | None = Field(default=None, ge=1800, le=2200)
    version: str | None = None
    source_text: str | None = None
    active: bool = True


class IntegrationPayload(BaseModel):
    name: str
    integration_type: str
    provider: str
    base_url: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    retry_count: int = Field(default=3, ge=0, le=10)
    sync_frequency_minutes: int | None = Field(default=None, ge=1)
    non_sensitive_config: dict[str, Any] | str = Field(default_factory=dict)
    secret_value: str | None = None
    active: bool = True

    @field_validator("sync_frequency_minutes", mode="before")
    @classmethod
    def empty_frequency_to_none(cls, value):
        return None if value in ("", None) else value

    @field_validator("base_url", "secret_value", mode="before")
    @classmethod
    def empty_string_to_none(cls, value):
        return None if value == "" else value

    @field_validator("non_sensitive_config", mode="before")
    @classmethod
    def parse_config(cls, value):
        if isinstance(value, str) and value.strip():
            import json
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("Parámetros JSON debe contener un objeto JSON válido") from exc
            if not isinstance(parsed, dict):
                raise ValueError("Parámetros JSON debe ser un objeto JSON")
            return parsed
        if value is None or value == "":
            return {}
        if not isinstance(value, dict):
            raise ValueError("Parámetros JSON debe ser un objeto JSON")
        return value


class SettingPayload(BaseModel):
    key: str
    value: dict[str, Any] | str = Field(default_factory=dict)
    description: str | None = None
    public: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def parse_value(cls, value):
        if isinstance(value, str):
            import json
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {"value": value}
        return value or {}


class ReportCreate(BaseModel):
    station_id: UUID
    crop_code: str | None = None
    crop_id: UUID | None = None
    planting_date: date
    date_from: date
    date_to: date

    @field_validator("date_to")
    @classmethod
    def validate_dates(cls, value: date, info):
        start = info.data.get("date_from")
        if start and value < start:
            raise ValueError("date_to no puede ser anterior a date_from")
        if start and (value - start).days > 366:
            raise ValueError("El reporte no puede superar 366 días")
        planting_date = info.data.get("planting_date")
        if planting_date and planting_date > value:
            raise ValueError("La fecha de siembra no puede ser posterior al final del reporte")
        return value


class PhenologyCalculate(BaseModel):
    crop_code: str
    planting_date: date
    on_date: date | None = None
