"""Role-based access on the platform-admin surface.

Model routing is PLATFORM configuration — it has no business_id — so it must not be
reachable by an ordinary customer who happened to sign up. Before this, any
authenticated user could repoint GENERATE at the most expensive model available and
spend the operator's budget.

Three distinctions the tests pin, because collapsing any of them is a real bug:

  no session          -> 401  (who are you?)
  session, wrong role -> 403  (I know who you are, and no)
  platform_admin      -> 200

401 and 403 are not interchangeable: a 401 tells a client to go and sign in, which for
a signed-in customer is a loop they can never escape.
"""

from typing import Any

import httpx
import pytest

from backend.app.api import admin_models as admin_api
from backend.app.api.auth import current_user
from backend.app.db.models import Role, User
from backend.app.llm.route_config import InMemoryRouteStore, RouteResolver
from backend.app.main import create_app


class FakeWriter:
    async def set_route(self, **kw: Any) -> None: ...
    async def clear_route(self, task_class: str) -> None: ...
    async def set_provider(self, **kw: Any) -> None: ...


def _user(role: Role) -> User:
    return User(email="x@example.test", password_hash="x", is_active=True, role=role)


def _client(*, user: User | None) -> httpx.AsyncClient:
    """`user=None` means no session at all."""
    app = create_app()
    app.dependency_overrides[admin_api.get_writer] = lambda: FakeWriter()

    resolver = RouteResolver(InMemoryRouteStore())

    async def _resolver() -> RouteResolver:
        await resolver.refresh()
        return resolver

    app.dependency_overrides[admin_api.get_resolver] = _resolver

    # Override `current_user`, NOT `require_admin`: the role check is what is under
    # test, so stubbing it out would make every assertion below meaningless.
    if user is None:
        from fastapi import HTTPException

        def _no_session() -> User:
            raise HTTPException(status_code=401, detail={"code": "not_authenticated"})

        app.dependency_overrides[current_user] = _no_session
    else:
        app.dependency_overrides[current_user] = lambda: user

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


PROTECTED = [
    ("GET", "/api/v1/admin/models/routes"),
    ("GET", "/api/v1/admin/models/providers"),
    ("GET", "/api/v1/admin/models/available?provider=anthropic"),
    ("PUT", "/api/v1/admin/models/routes/generate"),
    ("DELETE", "/api/v1/admin/models/routes/generate"),
    ("PUT", "/api/v1/admin/models/providers/openrouter"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_no_session_is_401(method: str, path: str) -> None:
    async with _client(user=None) as client:
        response = await client.request(method, path, json={"enabled": True})
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), PROTECTED)
@pytest.mark.parametrize("role", [Role.MEMBER, Role.OWNER])
async def test_an_ordinary_user_is_403_not_401(method: str, path: str, role: Role) -> None:
    """403, because they ARE signed in. Returning 401 would send a signed-in customer
    back to the login page forever."""
    async with _client(user=_user(role)) as client:
        response = await client.request(
            method,
            path,
            json={
                "enabled": True,
                "tier": "strong",
                "chain": [{"provider": "fake", "model": "fake/strong"}],
            },
        )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "forbidden"
    # The message must not tell a customer to go and get platform admin rights.
    assert "platform" not in detail["message"].lower()


async def test_a_platform_admin_gets_through() -> None:
    async with _client(user=_user(Role.PLATFORM_ADMIN)) as client:
        response = await client.get("/api/v1/admin/models/routes")
    assert response.status_code == 200
    assert response.json()["routes"]


async def test_a_deactivated_platform_admin_does_not_get_through() -> None:
    """Deactivation must beat role, not the other way round."""
    user = _user(Role.PLATFORM_ADMIN)
    user.is_active = False
    async with _client(user=user) as client:
        response = await client.get("/api/v1/admin/models/routes")
    assert response.status_code == 403


async def test_signup_does_not_mint_a_platform_admin() -> None:
    """The privilege-escalation case: if signup could set the role, anyone could grant
    themselves control of the platform's model spend by posting one extra field."""
    from backend.app.api.auth import SignupRequest

    assert "role" not in SignupRequest.model_fields, (
        "signup must not accept a role: it would be self-service privilege escalation"
    )
