"""Application configuration.

Every value is environment-driven. No secret is ever hardcoded here, and no
secret is ever exposed to the browser -- see docs/ARCHITECTURE.md section 9.
"""

import os
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The obviously-unsafe default. Named rather than inlined so `create_app` can
#: refuse to boot on it outside local development, comparing against the same
#: constant the field defaults to instead of a duplicated literal that drifts.
DEFAULT_SESSION_SECRET = "insecure-local-development-secret-change-me"  # noqa: S105

#: Ceiling on what ONE business may spend on model calls inside the reporting
#: window, in USD. `docs/ARCHITECTURE.md` section 7.4 states a per-business
#: monthly cap as one of three cap levels; this is the number it is held to.
#:
#: A platform-wide default rather than a column on `businesses`, deliberately:
#: nothing in the product can set a per-business ceiling -- there is no admin
#: screen and no route for it -- so a column would be an unsettable value that
#: every read would then have to defend against being NULL. A setting is the
#: same shape every other tunable here has, and the day a business genuinely
#: needs its own ceiling, this constant becomes the fallback for the column
#: rather than being replaced by it.
#:
#: $25.00 is fifty runs at the $0.50 per-run ceiling
#: (`agents.state.DEFAULT_MAX_USD`, not imported here so configuration stays
#: independent of the agent package) -- comfortably more than a small business
#: does in a month, and far below a bill anyone would want to discover after
#: the fact.
DEFAULT_BUSINESS_MONTHLY_CAP_USD = Decimal("25.00")

#: Published pieces per business per rolling week. `ARCHITECTURE.md` §7.4's third cap,
#: and the only one of the three that is NOT a cost control: it exists because
#: `ROADMAP.md` §10 names scaled content abuse as a real risk to mitigate in the
#: architecture rather than in a disclaimer, and §15 states plainly that this product
#: "will not mass-produce, and shouldn't".
#:
#: **Ten, and the arithmetic is the defence.** The product offers six publishable
#: channels and three cadences (weekly, biweekly, monthly), so the most an automation can
#: legitimately produce is six pieces in a week. Ten leaves an owner four more for manual
#: publishes from the calendar, and refuses the shapes that look like mass production —
#: two full runs in one week (12), or anything daily (42). A number that permitted every
#: conceivable use would not be a cap.
#:
#: A non-positive value refuses every publish, which is the kill switch:
#: `BUSINESS_WEEKLY_PUBLISH_CAP=0` stops all publishing for every business without a
#: deploy, in the same shape as the monthly USD one.
DEFAULT_BUSINESS_WEEKLY_PUBLISH_CAP = 10

Environment = Literal["local", "ci", "staging", "production"]


AgentRuntime = Literal["langgraph", "builtin"]


class Settings(BaseSettings):
    """Runtime settings, loaded from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        # ENV_FILE lets the test suite point this at a file that does not exist,
        # so a developer's real .env cannot leak into a test run.
        env_file=os.environ.get("ENV_FILE", ".env"),
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

    # Where this API is reachable from the public internet. It is CONFIGURATION and
    # never derived from a request: `Host` is caller-controlled, so building a
    # campaign's absolute URLs from it would let a poisoned header point every
    # tracked link in a business's Instagram bio at somebody else's domain (the same
    # reasoning `api/links.py` documents for returning relative paths).
    #
    # Used to build the `target_url` of a short link, which must be absolute because
    # it becomes the `Location` of a public 302.
    public_base_url: str = "http://localhost:8100"

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

    # Which graph runtime drives a run. `langgraph` compiles the machine with the
    # library (`agents/state_graph.py`); `builtin` is the hand-written driver
    # (`agents/graph.py`) that predates it.
    #
    # A setting rather than a deletion, and the reason is the risk profile: both are
    # asserted equivalent by the same parametrised test suite, so this is a lever to
    # pull if the library's behaviour surprises us in production, not a fork to
    # maintain. If it goes a release without being touched, the builtin driver should
    # go with it.
    agent_runtime: AgentRuntime = "langgraph"

    # The Meta app behind the Facebook/Instagram OAuth adapter
    # (`services/platform_oauth_meta.py`). BOTH optional, and both absent is the
    # ordinary state: with either one missing, `get_oauth_provider` returns
    # `FakeOAuthProvider` and `oauth_status()` says so. There is deliberately no
    # "enable Meta" flag -- selection is by credential and by nothing else, so a
    # setting cannot disagree with whether an app exists.
    #
    # The app id is not a secret (it travels in the consent-dialog URL, which a
    # customer's browser shows them). The secret is, so it is `SecretStr`: pydantic
    # masks it in `repr` and in a JSON dump, which is what stops it reaching a startup
    # log line or an exception rendering the settings object. Note that the provider
    # itself reads `os.environ`, not this object -- `asgi.py` calls `load_dotenv`
    # before importing the app, so a `.env` value is in the environment either way,
    # and a credential that never enters a settings instance cannot be leaked by
    # anything that serialises one (the rule `core/token_cipher.py` records for
    # `PLATFORM_CREDENTIAL_KEY`). These fields exist so the two variables are
    # declared and typed in the one place configuration is documented.
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None

    # LinkedIn, on the same terms and for the same reason: declared and typed here so
    # the variables are documented in one place, while
    # `services/platform_oauth_linkedin.py` reads `os.environ` directly so a credential
    # never enters a settings instance that something might serialise.
    linkedin_client_id: str | None = None
    linkedin_client_secret: SecretStr | None = None

    # The per-business ceiling, in USD. `Decimal`, never `float`: this number is
    # compared against a sum of `Numeric(12, 8)` ledger rows, and a binary float
    # would make the comparison at the boundary a matter of luck. pydantic parses
    # the environment string straight into `Decimal`, so no float is ever formed.
    #
    # A non-positive value refuses every run. That is intended and is the kill
    # switch: `BUSINESS_MONTHLY_CAP_USD=0` stops all model spend for every
    # business without a deploy.
    business_monthly_cap_usd: Decimal = DEFAULT_BUSINESS_MONTHLY_CAP_USD

    # Published pieces per business per rolling week — the quality control, not a cost
    # control. An `int` because it counts rows, and the rows it counts are the `actions`
    # ledger's succeeded publishes; see `services/publish_cap.py` for the window and the
    # reason the count is taken before the idempotency key is claimed.
    business_weekly_publish_cap: int = DEFAULT_BUSINESS_WEEKLY_PUBLISH_CAP


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
