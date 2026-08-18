"""Health endpoint tests.

Uses httpx's ASGI transport so the whole app is exercised in-process: no port
binding, no live server, no network. Every external call in this test suite is
faked -- CI must never depend on a third party being up.
"""

import httpx
import pytest

from backend.app.main import create_app


@pytest.fixture
def client() -> httpx.AsyncClient:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
async def test_health_returns_ok(client: httpx.AsyncClient, path: str) -> None:
    async with client:
        response = await client.get(path)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["version"]
    assert body["environment"]


async def test_health_leaks_no_configuration(client: httpx.AsyncClient) -> None:
    """A liveness probe must not become a configuration disclosure endpoint."""
    async with client:
        response = await client.get("/api/v1/health")

    body = response.json()
    assert set(body) == {"status", "service", "version", "environment"}
    serialised = response.text.lower()
    for leaked in ("password", "secret", "postgresql", "redis://", "token", "key"):
        assert leaked not in serialised
