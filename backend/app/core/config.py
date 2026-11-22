from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_secret_key: str = "replace_me"
    frontend_origin: str = "http://localhost:5173"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    source_database_path: str = "data/civicpilot.db"
    source_timeout_seconds: float = Field(default=12.0, ge=1, le=30)
    source_user_agent: str = Field(default="CivicPilotAI-SourceMonitor/1.0", min_length=8, max_length=160)
    database_path: str = "data/civicpilot.db"
    upload_storage_path: str = "data/private_uploads"
    max_upload_mb: int = Field(default=10, ge=1, le=50)
    gemini_embedding_model: str = "gemini-embedding-001"
    admin_email: str = ""
    admin_password: str = ""
    token_expiry_hours: int = Field(default=12, ge=1, le=168)

    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
