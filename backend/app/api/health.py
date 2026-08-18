"""Health endpoints.

Exposed twice on purpose: `/health` unversioned for container and load-balancer
probes, and `/api/v1/health` inside the versioned surface that clients consume.
An app in a store keeps calling the version it shipped with, so the versioned
namespace exists from the first commit.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.app.core.config import Settings, get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    """Liveness payload. Deliberately free of anything an attacker could use."""

    status: str
    service: str
    version: str
    environment: str


def _health() -> HealthResponse:
    settings: Settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get("/health", tags=["health"], summary="Unversioned liveness probe")
def health() -> HealthResponse:
    return _health()


@router.get("/api/v1/health", tags=["health"], summary="Versioned liveness probe")
def health_v1() -> HealthResponse:
    return _health()
