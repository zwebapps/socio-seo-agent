"""FastAPI application entry point.

Layering rule (docs/ARCHITECTURE.md section 4):

    api -> services -> {engines, actuators, agents} -> adapters

Routes stay thin. They never reach into an engine or an adapter directly.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api import health
from backend.app.core.config import get_settings


def create_app() -> FastAPI:
    """Build the ASGI application.

    A factory rather than a module-level singleton so tests can construct an
    isolated app without importing process-wide state.
    """
    settings = get_settings()

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
    return app


app = create_app()
