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
    #
    # TWO database URLs, and the distinction is a security boundary, not tidiness:
    #
    #   app_database_url  the RUNTIME connection, as the restricted `sma_app`
    #                     role: no superuser, no BYPASSRLS, not the table owner.
    #                     Row-level security therefore actually applies. All
    #                     application queries use this.
    #
    #   database_url      the OWNER connection, used by Alembic only. It is a
    #                     superuser locally, which is precisely why the runtime
    #                     must not use it -- a superuser bypasses every policy,
    #                     and an isolation test run as one would pass while
    #                     proving nothing.
    app_database_url: str = "postgresql+asyncpg://sma_app:sma_app@localhost:5435/sma"
    database_url: str = "postgresql+asyncpg://sma:sma@localhost:5435/sma"
    redis_url: str = "redis://localhost:6381/0"

    # Browser origins allowed to call the API.
    cors_origins: tuple[str, ...] = ("http://localhost:3100",)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
