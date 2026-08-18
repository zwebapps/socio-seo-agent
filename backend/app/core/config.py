"""Application configuration.

Every value is environment-driven. No secret is ever hardcoded here, and no
secret is ever exposed to the browser -- see docs/ARCHITECTURE.md section 9.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "ci", "staging", "production"]


class Settings(BaseSettings):
    """Runtime settings, loaded from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Social Marketing Agent"
    app_version: str = "0.1.0"
    environment: Environment = "local"

    # Infra. Ports are deliberately non-default so this project cannot collide
    # with a Postgres or Redis instance another project is already running.
    database_url: str = "postgresql+asyncpg://sma:sma@localhost:5435/sma"
    redis_url: str = "redis://localhost:6381/0"

    # Browser origins allowed to call the API.
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
