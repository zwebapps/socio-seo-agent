"""Admin API for model routing.

Written before the routes. Two properties matter more than the CRUD:

  * it must be behind authentication — model choice moves real money, so an
    unauthenticated caller must not be able to point GENERATE at an expensive model;
  * it must never accept or echo an API key. Keys live in the environment; a key
    arriving in a request body would end up in a database row, a log line and a backup.
"""

from typing import Any

import httpx
import pytest

from backend.app.api import admin_models as admin_api
from backend.app.llm.route_config import (
    InMemoryRouteStore,
    ProviderSettingRecord,
    RouteResolver,
)
from backend.app.main import create_app


class FakeWriter:
    def __init__(self) -> None:
        self.routes: list[dict[str, Any]] = []
        self.providers: list[dict[str, Any]] = []
        self.cleared: list[str] = []

    async def set_route(self, *, task_class: str, tier: str, chain: Any, **kw: Any) -> None:
        self.routes.append({"task_class": task_class, "tier": tier, "chain": list(chain)})

    async def clear_route(self, task_class: str) -> None:
        self.cleared.append(task_class)

    async def set_provider(self, *, provider: str, enabled: bool, **kw: Any) -> None:
        self.providers.append({"provider": provider, "enabled": enabled, **kw})


def _client(
    *,
    authenticated: bool = True,
    writer: FakeWriter | None = None,
    store: InMemoryRouteStore | None = None,
) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[admin_api.get_writer] = lambda: writer or FakeWriter()

    # The resolver MUST be overridden or these tests reach the real database, which
    # surfaces as "Future attached to a different loop" rather than as anything
    # resembling the actual problem.
    resolver = RouteResolver(store or InMemoryRouteStore())

    async def _resolver() -> RouteResolver:
        await resolver.refresh()
        return resolver

    app.dependency_overrides[admin_api.get_resolver] = _resolver
    if authenticated:
        app.dependency_overrides[admin_api.require_admin] = lambda: None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_listing_routes_shows_every_task_with_its_source() -> None:
    """Unconfigured tasks appear too, marked `default`, so the screen shows the whole
    picture rather than only the rows someone happened to edit."""
    async with _client() as client:
        response = await client.get("/api/v1/admin/models/routes")

    assert response.status_code == 200
    body = response.json()
    tasks = {r["taskClass"] for r in body["routes"]}
    assert {"generate", "plan", "classify"} <= tasks
    assert all(r["source"] in ("configured", "default") for r in body["routes"])


async def test_a_route_can_be_saved() -> None:
    writer = FakeWriter()
    async with _client(writer=writer) as client:
        response = await client.put(
            "/api/v1/admin/models/routes/generate",
            json={"tier": "strong", "chain": [{"provider": "ollama", "model": "llama3.1:70b"}]},
        )

    assert response.status_code == 200
    assert writer.routes[0]["task_class"] == "generate"
    assert writer.routes[0]["chain"] == [{"provider": "ollama", "model": "llama3.1:70b"}]


async def test_an_unknown_task_class_is_rejected() -> None:
    async with _client() as client:
        response = await client.put(
            "/api/v1/admin/models/routes/not-a-task",
            json={"tier": "strong", "chain": [{"provider": "ollama", "model": "x"}]},
        )
    assert response.status_code == 422


async def test_an_unknown_provider_is_rejected_rather_than_stored() -> None:
    """A typo'd provider name would silently route the task to nothing."""
    async with _client() as client:
        response = await client.put(
            "/api/v1/admin/models/routes/generate",
            json={"tier": "strong", "chain": [{"provider": "opnrouter", "model": "x"}]},
        )
    assert response.status_code == 422


async def test_a_route_can_be_reverted_to_the_default() -> None:
    writer = FakeWriter()
    async with _client(writer=writer) as client:
        response = await client.delete("/api/v1/admin/models/routes/generate")

    assert response.status_code == 204
    assert writer.cleared == ["generate"]


async def test_providers_report_availability_and_whether_cost_can_be_reported() -> None:
    async with _client() as client:
        body = (await client.get("/api/v1/admin/models/providers")).json()

    names = {p["provider"] for p in body["providers"]}
    assert {"openrouter", "anthropic", "ollama", "fake"} <= names
    for p in body["providers"]:
        assert "available" in p and "requiresKey" in p


async def test_a_provider_can_be_disabled() -> None:
    writer = FakeWriter()
    async with _client(writer=writer) as client:
        response = await client.put(
            "/api/v1/admin/models/providers/openrouter", json={"enabled": False}
        )
    assert response.status_code == 200
    saved = writer.providers[0]
    assert saved["provider"] == "openrouter"
    assert saved["enabled"] is False


async def test_an_api_key_in_the_body_is_refused_not_ignored() -> None:
    """Silently dropping it would teach a user that pasting a key here is fine."""
    async with _client() as client:
        response = await client.put(
            "/api/v1/admin/models/providers/openrouter",
            json={"enabled": True, "apiKey": "sk-secret"},
        )

    assert response.status_code == 422
    assert "sk-secret" not in response.text, "the refusal must not echo the key back"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/models/routes"),
        ("GET", "/api/v1/admin/models/providers"),
        ("PUT", "/api/v1/admin/models/routes/generate"),
        ("DELETE", "/api/v1/admin/models/routes/generate"),
        ("PUT", "/api/v1/admin/models/providers/openrouter"),
    ],
)
async def test_every_admin_route_requires_authentication(method: str, path: str) -> None:
    """Model choice moves real money. None of this may be reachable anonymously."""
    async with _client(authenticated=False) as client:
        response = await client.request(method, path, json={"enabled": True})

    assert response.status_code == 401, f"{method} {path} was reachable without a session"


async def test_ollama_shows_available_once_it_has_an_address() -> None:
    """A local provider has no key, so only configuration can say whether it is usable."""
    store = InMemoryRouteStore(
        providers=[
            ProviderSettingRecord(
                provider="ollama", enabled=True, base_url="http://localhost:11434/v1"
            )
        ]
    )
    async with _client(store=store) as client:
        body = (await client.get("/api/v1/admin/models/providers")).json()

    ollama = next(p for p in body["providers"] if p["provider"] == "ollama")
    assert ollama["available"] is True
    assert ollama["requiresKey"] is False
    assert ollama["baseUrl"] == "http://localhost:11434/v1"


async def test_a_chain_with_an_unpriced_model_is_flagged_as_not_cost_reportable() -> None:
    """Otherwise the UI shows $0.00 for real spend, which is worse than showing nothing."""
    from backend.app.llm import ModelTier, TaskClass
    from backend.app.llm.route_config import RouteRecord

    store = InMemoryRouteStore(
        routes=[
            RouteRecord(
                task_class=TaskClass.GENERATE,
                tier=ModelTier.STRONG,
                chain=[{"provider": "ollama", "model": "llama3.1:70b"}],
            )
        ]
    )
    async with _client(store=store) as client:
        body = (await client.get("/api/v1/admin/models/routes")).json()

    generate = next(r for r in body["routes"] if r["taskClass"] == "generate")
    assert generate["costReportable"] is False


async def test_the_catalogue_lists_pickable_models_for_a_provider() -> None:
    async with _client() as client:
        body = (await client.get("/api/v1/admin/models/available?provider=anthropic")).json()

    assert body["provider"] == "anthropic"
    assert body["models"], "an admin screen must never be handed an empty picker"
    assert all("id" in m and "priced" in m for m in body["models"])


async def test_an_unconfigured_ollama_is_not_probed_at_all() -> None:
    """Two reasons, and the second is why this test exists at all:

    the app must not reach out to a machine the operator never pointed it at; and
    defaulting to localhost made this very test non-hermetic — it found a real Ollama
    server running on the developer's laptop and returned its models.
    """
    async with _client() as client:
        response = await client.get("/api/v1/admin/models/available?provider=ollama")

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert body["live"] is False
    assert "No address is configured" in (body["message"] or "")


async def test_an_unknown_provider_in_the_catalogue_is_422() -> None:
    async with _client() as client:
        response = await client.get("/api/v1/admin/models/available?provider=nope")
    assert response.status_code == 422
