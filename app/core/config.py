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
    copernicus_stac_search_url: str | None = None
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
    frontend_base_url: str = "http://localhost:3000"

    storage_path: Path = Path("/data")
    max_upload_mb: int = 15
    report_retention_days: int = 365
    load_demo_data: bool = False
    scheduler_sync_minutes: int = 30
    scheduler_backup_enabled: bool = False
    scheduler_backup_hour_utc: int = 2
    worker_poll_seconds: int = 4
    job_lock_timeout_minutes: int = 30
    metrics_enabled: bool = True

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
    return config


settings = get_settings()
