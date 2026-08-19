"""The four auth routes and the ``current_user`` dependency.

Written before the routes. The interesting assertions are not about the happy
path:

* **The cookie has no ``domain`` attribute.** A ``domain=.example.com`` cookie is
  sent to every subdomain, and in a multi-tenant product that means a session
  travelling to customer-facing hosts we do not control the content of. The
  absence of one attribute is a real control, so it is a real test.
* **Unknown email and wrong password return byte-identical responses.** Asserted
  by comparing the two responses to each other, not by checking each against a
  string -- that way the test still holds if the copy is reworded.
* **A 422 never echoes the password back.** FastAPI's own validation errors
  include the offending input, so the request models deliberately carry no field
  constraints and the service owns validation instead.

Database-backed, so marked ``db``. The engine is function-scoped: an asyncpg pool
belongs to the event loop that created it, and pytest-asyncio gives every test a
fresh loop.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.api import auth as auth_api
from backend.app.core import security
from backend.app.core.config import Settings, get_settings
from backend.app.main import create_app

pytestmark = pytest.mark.db

EMAIL_PREFIX = "authapi-test-"
PASSWORD = "correct horse battery"
_CONNECTION_FAILURES = (OperationalError, ConnectionRefusedError, OSError)


def _email() -> str:
    return f"{EMAIL_PREFIX}{uuid4().hex}@example.test"


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().app_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except _CONNECTION_FAILURES as exc:
        await engine.dispose()
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")
    except InterfaceError:
        await engine.dispose()
        raise

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(
                text("DELETE FROM users WHERE email LIKE :prefix"),
                {"prefix": f"{EMAIL_PREFIX}%"},
            )
            await session.commit()
    await engine.dispose()


def _client(db: AsyncSession, *, settings: Settings | None = None) -> httpx.AsyncClient:
    app = create_app()
    app.include_router(auth_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[auth_api.db_session] = override_session
    if settings is not None:
        app.dependency_overrides[auth_api.get_auth_settings] = lambda: settings

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _cookie_header(response: httpx.Response) -> str:
    headers = response.headers.get_list("set-cookie")
    assert len(headers) == 1, headers
    return headers[0]


async def _signup(client: httpx.AsyncClient, email: str, **over: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "email": email,
        "password": PASSWORD,
        "businessName": "Müller Sanitär GmbH",
    }
    payload.update(over)
    return await client.post("/api/v1/auth/signup", json=payload)


# --------------------------------------------------------------------------- #
# Signup
# --------------------------------------------------------------------------- #


async def test_signup_creates_the_account_and_logs_the_caller_in(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        response = await _signup(client, email)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == email
    assert body["userId"] and body["businessId"]
    # Signing up already proved who they are; asking them to log in again next
    # would be ceremony, not security.
    assert (
        security.verify_session(
            response.cookies["sma_session"],
            secret=get_settings().session_secret,
            max_age=timedelta(days=30),
        )
        is not None
    )


async def test_signup_rejects_a_duplicate_with_409(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        first = await _signup(client, email)
        second = await _signup(client, email)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "email_taken"
    # Neutral copy: it must not name the address or its owner. It does still
    # reveal that the address is in use -- see the service docstring.
    message = second.json()["detail"]["message"].lower()
    assert email not in message
    assert "already" not in message


async def test_signup_rejects_a_short_password_with_422(db: AsyncSession) -> None:
    async with _client(db) as client:
        response = await _signup(client, _email(), password="short")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "weak_password"


async def test_signup_rejects_a_common_password(db: AsyncSession) -> None:
    async with _client(db) as client:
        response = await _signup(client, _email(), password="passwordpassword")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "weak_password"


async def test_a_rejected_signup_never_echoes_the_password(db: AsyncSession) -> None:
    """FastAPI's stock 422 includes the offending input. Ours must not."""
    secret_password = "hunter2hunter2-do-not-echo"
    too_short = "n0t-12"
    async with _client(db) as client:
        responses = [
            await _signup(client, "not-an-email", password=secret_password),
            await _signup(client, _email(), password=too_short),
            await _signup(client, _email(), password=secret_password, businessName="  "),
        ]

    for response in responses:
        assert response.status_code == 422
        assert secret_password not in response.text
        assert too_short not in response.text


async def test_signup_rejects_a_malformed_email(db: AsyncSession) -> None:
    async with _client(db) as client:
        response = await _signup(client, "not-an-email")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_email"


async def test_signup_rejects_a_blank_business_name(db: AsyncSession) -> None:
    async with _client(db) as client:
        response = await _signup(client, _email(), businessName="   ")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_business_name"


# --------------------------------------------------------------------------- #
# Login and the cookie
# --------------------------------------------------------------------------- #


async def test_login_sets_a_session_cookie_with_the_right_flags(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        client.cookies.clear()
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )

    assert response.status_code == 200
    header = _cookie_header(response)
    assert header.startswith("sma_session=")
    assert "HttpOnly" in header
    assert "samesite=lax" in header.lower()
    assert "Path=/" in header
    assert "Max-Age=2592000" in header  # 30 days, in seconds


async def test_the_session_cookie_carries_no_domain_attribute(db: AsyncSession) -> None:
    """The load-bearing assertion in this file.

    With ``domain=.example.com`` the session is attached to every subdomain,
    including per-customer hosts. Host-only is the whole isolation guarantee, and
    it is the absence of an attribute -- exactly the kind of thing a refactor
    removes without noticing.
    """
    email = _email()
    async with _client(db) as client:
        signup = await _signup(client, email)
        client.cookies.clear()
        login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        logout = await client.post("/api/v1/auth/logout")

    for response in (signup, login, logout):
        assert "domain" not in _cookie_header(response).lower()


async def test_the_cookie_is_not_secure_locally_but_is_outside_local(db: AsyncSession) -> None:
    """``Secure`` on plain-HTTP localhost would make the cookie undeliverable."""
    email = _email()
    local = get_settings().model_copy(update={"environment": "local"})
    production = get_settings().model_copy(update={"environment": "production"})

    async with _client(db, settings=local) as client:
        assert "secure" not in _cookie_header(await _signup(client, email)).lower()

    async with _client(db, settings=production) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert "Secure" in _cookie_header(response)


async def test_unknown_email_and_wrong_password_are_indistinguishable(db: AsyncSession) -> None:
    """Compared to each other, so rewording the copy cannot break the property."""
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        client.cookies.clear()
        wrong_password = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "definitely not it"}
        )
        unknown_email = await client.post(
            "/api/v1/auth/login", json={"email": _email(), "password": PASSWORD}
        )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert not wrong_password.headers.get_list("set-cookie")
    assert not unknown_email.headers.get_list("set-cookie")


async def test_a_deactivated_user_cannot_log_in_and_looks_the_same(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        await db.execute(text("UPDATE users SET is_active = false WHERE email = :e"), {"e": email})
        await db.commit()
        client.cookies.clear()

        deactivated = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        unknown = await client.post(
            "/api/v1/auth/login", json={"email": _email(), "password": PASSWORD}
        )

    assert deactivated.status_code == 401
    assert deactivated.json() == unknown.json()


async def test_login_with_a_malformed_email_is_401_not_422(db: AsyncSession) -> None:
    """A login form is public: garbage in it is a failed login, not a schema report."""
    async with _client(db) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": "not-an-email", "password": "x"}
        )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# /me and logout
# --------------------------------------------------------------------------- #


async def test_me_is_401_without_a_cookie(db: AsyncSession) -> None:
    async with _client(db) as client:
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_me_returns_the_user_with_a_cookie(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert body["isActive"] is True
    assert "passwordHash" not in body
    assert "password_hash" not in response.text


async def test_me_is_401_for_a_tampered_cookie(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        good = client.cookies["sma_session"]
        body, _, signature = good.rpartition(".")
        flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
        # Cleared first: httpx would otherwise send BOTH the good cookie and
        # this one, and the test would pass for the wrong reason.
        client.cookies.clear()
        client.cookies.set("sma_session", f"{body}.{flipped}")

        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_me_is_401_for_an_expired_cookie(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        signup = await _signup(client, email)
        user_id = signup.json()["userId"]
        stale = security.sign_session(
            UUID(user_id),
            issued_at=datetime.now(UTC) - timedelta(days=31),
            secret=get_settings().session_secret,
        )
        client.cookies.clear()
        client.cookies.set("sma_session", stale)

        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_me_is_401_for_a_cookie_signed_with_another_secret(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        signup = await _signup(client, email)
        forged = security.sign_session(
            UUID(signup.json()["userId"]),
            issued_at=datetime.now(UTC),
            secret="an-attackers-guess",
        )
        client.cookies.clear()
        client.cookies.set("sma_session", forged)

        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_me_is_401_after_the_user_is_deactivated(db: AsyncSession) -> None:
    """401 rather than 403: the session is no longer valid, so re-authenticate.

    403 would say "we know who you are, and no" -- which invites a support call
    about permissions when the real answer is that the account is switched off.
    """
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        await db.execute(text("UPDATE users SET is_active = false WHERE email = :e"), {"e": email})
        await db.commit()

        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_me_is_401_for_a_validly_signed_cookie_naming_no_one(db: AsyncSession) -> None:
    """A leaked secret is fatal, but a stale user id must not 500."""
    async with _client(db) as client:
        client.cookies.clear()
        client.cookies.set(
            "sma_session",
            security.sign_session(
                uuid4(), issued_at=datetime.now(UTC), secret=get_settings().session_secret
            ),
        )
        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_logout_clears_the_cookie(db: AsyncSession) -> None:
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        logout = await client.post("/api/v1/auth/logout")

        assert logout.status_code == 204
        header = _cookie_header(logout)
        assert "sma_session=" in header
        assert "Max-Age=0" in header or "expires=" in header.lower()

        after = await client.get("/api/v1/auth/me")

    assert after.status_code == 401


async def test_logout_without_a_session_is_still_204(db: AsyncSession) -> None:
    """Logging out must be idempotent, or a double-click shows an error page."""
    async with _client(db) as client:
        response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 204
