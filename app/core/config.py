from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    environment: str = "development"
    app_name: str = "FECOCLIMA IA API"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://fecoclima:fecoclima@localhost:5432/fecoclimaia"

    jwt_secret: str = "development-only-change-me"
    access_token_minutes: int = 20
    refresh_token_days: int = 14
    cookie_secure: bool = False
    cookie_domain: str | None = None
    # Ruta pública donde se monta la aplicación. En producción bajo un
    # subdirectorio use, por ejemplo, /fecoclima-ia para aislar las cookies
    # de otras aplicaciones alojadas en el mismo dominio.
    cookie_path: str = "/"
    access_cookie_name: str = "access_token"
    refresh_cookie_name: str = "refresh_token"
    csrf_cookie_name: str = "csrf_token"
    # NoDecode evita que pydantic-settings intente interpretar estas variables
    # como JSON antes de ejecutar el validador. Se admiten CSV y JSON.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    trusted_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    encryption_key: str | None = None

    superadmin_email: str = "admin@fecoclima.local"
    superadmin_password: str = "ChangeMe123456!"
    superadmin_name: str = "Administrador FECOCLIMA"

    fecoclima_base_url: str = "https://fecoclima.fecoprod.com.py/clima/api"
    fecoclima_api_key: str | None = None
    fecoclima_timeout_seconds: int = 30
    fecoclima_retry_count: int = 3

    open_meteo_base_url: str = "https://api.open-meteo.com/v1"
    copernicus_stac_search_url: str = "https://stac.dataspace.copernicus.eu/v1/search"
    copernicus_client_id: str | None = None
    copernicus_client_secret: str | None = None
    copernicus_token_url: str = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    copernicus_process_url: str = "https://sh.dataspace.copernicus.eu/process/v1"
    copernicus_statistics_url: str = "https://sh.dataspace.copernicus.eu/statistics/v1"
    satellite_lookback_days: int = 60
    satellite_max_cloud_percentage: float = 40.0
    satellite_auto_radius_m: int = 750
    satellite_refresh_hours: int = 24
    external_http_timeout_seconds: int = 45
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_chat_path: str = "/chat/completions"
    ai_embeddings_path: str = "/embeddings"
    ai_default_model: str | None = None
    ai_embedding_model: str | None = None
    ai_embedding_dimensions: int = 1536

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "no-reply@example.com"
    smtp_starttls: bool = True
    frontend_base_url: str = "http://localhost:3000/fecoclima-ia"

    storage_path: Path = Path("/data")
    max_upload_mb: int = 15
    report_retention_days: int = 365
    load_demo_data: bool = False
    scheduler_sync_minutes: int = 30
    public_analysis_sync_enabled: bool = True
    public_analysis_sync_cooldown_minutes: int = 10
    public_analysis_sync_lookback_days: int = 3
    scheduler_backup_enabled: bool = False
    scheduler_backup_hour_utc: int = 2
    worker_poll_seconds: int = 4
    job_lock_timeout_minutes: int = 30
    metrics_enabled: bool = True

    @field_validator("access_cookie_name", "refresh_cookie_name", "csrf_cookie_name")
    @classmethod
    def validate_cookie_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character.isspace() or character in "=;," for character in normalized):
            raise ValueError("Los nombres de cookie deben ser identificadores HTTP válidos")
        return normalized

    @field_validator("cookie_path", mode="before")
    @classmethod
    def normalize_cookie_path(cls, value):
        raw = str(value or "/").strip()
        if raw == "/":
            return "/"
        normalized = "/" + raw.strip("/")
        if any(character in normalized for character in ("?", "#", ";")):
            raise ValueError("COOKIE_PATH debe ser una ruta URL válida sin query string ni fragmento")
        return normalized

    @property
    def auth_cookie_path(self) -> str:
        api_prefix = "/" + self.api_v1_prefix.strip("/")
        if self.cookie_path == "/":
            return f"{api_prefix}/auth"
        return f"{self.cookie_path}{api_prefix}/auth"

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def parse_string_list(cls, value):
        """Acepta listas JSON o valores separados por comas desde .env."""
        if not isinstance(value, str):
            return value

        raw = value.strip()
        if not raw:
            return []

        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("Debe ser una lista JSON válida o una lista separada por comas") from exc
            if not isinstance(parsed, list):
                raise ValueError("Debe ser una lista JSON o una lista separada por comas")
            return [str(part).strip() for part in parsed if str(part).strip()]

        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    def validate_for_production(self) -> None:
        if not self.is_production:
            return
        errors: list[str] = []
        if len(self.jwt_secret) < 64 or "change" in self.jwt_secret.lower():
            errors.append("JWT_SECRET debe tener al menos 64 caracteres y no ser el valor de ejemplo")
        if not self.cookie_secure:
            errors.append("COOKIE_SECURE debe ser true")
        if len({self.access_cookie_name, self.refresh_cookie_name, self.csrf_cookie_name}) != 3:
            errors.append("ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME y CSRF_COOKIE_NAME deben ser distintos")
        if "*" in self.cors_origins:
            errors.append("CORS_ORIGINS no puede contener *")
        if "cambiar" in self.database_url.lower() or "change" in self.database_url.lower():
            errors.append("DATABASE_URL conserva una contraseña de ejemplo")
        if len(self.superadmin_password) < 16 or any(word in self.superadmin_password.lower() for word in ("cambiar", "change")):
            errors.append("SUPERADMIN_PASSWORD debe ser una contraseña fuerte de al menos 16 caracteres")
        if not self.encryption_key or len(self.encryption_key) < 32:
            errors.append("ENCRYPTION_KEY debe configurarse separadamente y tener al menos 32 caracteres")
        if not self.trusted_hosts or all(host in {"localhost", "127.0.0.1"} for host in self.trusted_hosts):
            errors.append("TRUSTED_HOSTS debe incluir el dominio de producción")
        if not self.frontend_base_url.lower().startswith("https://"):
            errors.append("FRONTEND_BASE_URL debe usar HTTPS")
        if errors:
            raise RuntimeError("Configuración insegura: " + "; ".join(errors))


@lru_cache

def get_settings() -> Settings:
    config = Settings()
    config.storage_path.mkdir(parents=True, exist_ok=True)
    (config.storage_path / "reports").mkdir(parents=True, exist_ok=True)
    (config.storage_path / "uploads").mkdir(parents=True, exist_ok=True)
    (config.storage_path / "backups").mkdir(parents=True, exist_ok=True)
    (config.storage_path / "exports").mkdir(parents=True, exist_ok=True)
    (config.storage_path / "error-reports").mkdir(parents=True, exist_ok=True)
    (config.storage_path / "satellite").mkdir(parents=True, exist_ok=True)
    return config


settings = get_settings()
