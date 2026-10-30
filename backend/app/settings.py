from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_env: str = "development"
    frontend_origin: str = "http://localhost:5173"
    database_url: str = "sqlite:///./self_healing.db"
    redis_url: str = "redis://localhost:6379/0"
    github_webhook_secret: str = ""
    github_app_slug: str = ""
    github_app_id: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    github_callback_url: str = ""
    github_private_key_path: str = ""
    nebius_base_url: str = ""
    nebius_api_key: str = ""
    nebius_model: str = ""
    millisa_base_url: str = "https://millisa.codly.in/api/v1"
    millisa_api_key: str = ""
    millisa_model: str = "millisa-auto"
    nebius_input_price_per_million: float | None = None
    nebius_output_price_per_million: float | None = None
    validation_docker_bin: str = "docker"
    validation_image: str = ""
    validation_timeout_seconds: int = 300
    validation_memory_limit: str = "2g"
    validation_cpu_limit: str = "2"
    validation_workspace_limit: str = "512m"
    validation_temp_limit: str = "256m"
    milisa_fix_ttl_days: int = Field(default=365, ge=0, le=3650)
    repair_concurrency_per_org: int = Field(default=1, ge=1, le=20)
    repair_slot_lease_seconds: int = Field(default=120, ge=30, le=600)
    repair_queue_wait_seconds: int = Field(default=30, ge=5, le=300)
    repair_job_timeout_seconds: int = Field(default=1800, ge=300, le=3600)
    session_cookie_secure: bool = False
    session_ttl_hours: int = 24
    session_cookie_name: str = "patchwork_session"

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    @model_validator(mode="after")
    def enforce_production_security(self) -> "Settings":
        if self.app_env.lower() == "production":
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be true in production")
            if self.database_url.startswith("sqlite"):
                raise ValueError("A PostgreSQL DATABASE_URL is required in production")
        return self


settings = Settings()
