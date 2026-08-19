"""FastAPI application entry point.

Layering rule (docs/ARCHITECTURE.md section 4):

    api -> services -> {engines, actuators, agents} -> adapters

Routes stay thin. They never reach into an engine or an adapter directly.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api import auth, health, onboarding
from backend.app.core.config import DEFAULT_SESSION_SECRET, Settings, get_settings


#: Below this length an HMAC key is brute-forceable, and the signature is only
#: ever as good as the key behind it.
MIN_SESSION_SECRET_LENGTH = 32


class InsecureConfigurationError(RuntimeError):
    """The application refuses to start with a configuration that is not safe.

    Failing at boot rather than serving is deliberate. A misconfigured deployment
    that starts is a deployment nobody notices, and the failure mode here -- a
    known signing key -- lets anyone mint a session for any user.
    """


def _assert_secure(settings: Settings) -> None:
    if settings.environment == "local":
        return

    if settings.session_secret == DEFAULT_SESSION_SECRET:
        raise InsecureConfigurationError(
            f"SESSION_SECRET is still the built-in development default while "
            f"ENVIRONMENT={settings.environment}. Anyone holding that value can sign "
            "a session cookie for any user. Set SESSION_SECRET to a random 32+ "
            "character value (openssl rand -hex 32) and restart."
        )

    if len(settings.session_secret) < MIN_SESSION_SECRET_LENGTH:
        raise InsecureConfigurationError(
            f"SESSION_SECRET is {len(settings.session_secret)} characters while "
            f"ENVIRONMENT={settings.environment}; at least "
            f"{MIN_SESSION_SECRET_LENGTH} are required. The HMAC is only as strong "
            "as its key."
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    A factory rather than a module-level singleton so tests can construct an
    isolated app without importing process-wide state.
    """
    settings = settings or get_settings()
    _assert_secure(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Growth agent for small businesses: SEO content, AI-answer "
            "visibility, social content, and lead capture."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(onboarding.router)
    return app


app = create_app()
