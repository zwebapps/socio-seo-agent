"""Application configuration.

Every value is environment-driven. No secret is ever hardcoded here, and no
secret is ever exposed to the browser -- see docs/ARCHITECTURE.md section 9.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

#: The obviously-unsafe default. Named rather than inlined so `create_app` can
#: refuse to boot on it outside local development, comparing against the same
#: constant the field defaults to instead of a duplicated literal that drifts.
DEFAULT_SESSION_SECRET = "insecure-local-development-secret-change-me"  # noqa: S105

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

    # HMAC key for session cookies. The default below is deliberately obvious
    # rubbish so that a machine which forgot to set it is caught by reading the
    # value, not by a subtle failure later: anyone holding this string can mint a
    # session for any user id. MUST be overridden in staging and production with
    # a high-entropy value (`openssl rand -hex 32`). Rotating it logs everyone
    # out, which is also how you revoke every session at once.
    # ruff's S105 fires here and is right in general: this IS a hardcoded
    # credential. It is silenced rather than removed because a required setting
    # would make `pytest` and `make dev` fail on a fresh checkout, and the usual
    # workaround for that is a shared secret in a committed .env -- which is worse.
    session_secret: str = DEFAULT_SESSION_SECRET


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
