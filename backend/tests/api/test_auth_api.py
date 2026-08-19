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
* **Logout revokes server-side.** Clearing the cookie only edits the caller's own
  browser, which is no help in the case logout exists for: somebody else has a
  copy. The test keeps the old cookie value, logs out, sends it back, and expects
  401 -- that is the difference between logout as UI and logout as revocation.
* **Both credential routes are throttled, on two independent dimensions.** Login
  and signup each run argon2 at 64 MiB, so either one unthrottled is a
  memory-amplification DoS. Per-IP and per-email are tested separately, each while
  varying the other, because one combined key would stop neither attack.

Every client below gets its OWN rate limiter by default. Sharing the process-wide
one would make the suite's own logins each other's budget, and the failure would
surface in whichever test happened to run thirty-first rather than in the one that
caused it. The throttling tests opt in explicitly by passing a limiter with small
limits.

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
from backend.app.core import rate_limit, security
from backend.app.core.config import Settings, get_settings
from backend.app.core.rate_limit import (
    FixedWindowRateLimiter,
    InMemoryWindowCounter,
    RateLimitRule,
    RedisWindowCounter,
)
from backend.app.main import create_app
from backend.app.services import auth_service

pytestmark = pytest.mark.db

EMAIL_PREFIX = "authapi-test-"
PASSWORD = "correct horse battery"

# Long, for the reason spelled out in tests/core/test_rate_limit.py: every
# throttling test here asserts that a limit TRIPS and none waits for a reset, so a
# short window only buys the chance of straddling a wall-clock bucket edge and
# flaking.
LONG_WINDOW = 3600
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


def _limiter(
    *,
    ip: RateLimitRule | None = None,
    email: RateLimitRule | None = None,
    counter: rate_limit.WindowCounter | None = None,
    fallback: rate_limit.WindowCounter | None = None,
) -> FixedWindowRateLimiter:
    """A limiter with a namespace nothing else shares.

    Generous by default so it cannot interfere with a test that is about something
    else; a throttling test passes its own small rules.
    """
    return FixedWindowRateLimiter(
        rules={
            rate_limit.DIMENSION_IP: ip if ip is not None else RateLimitRule(10_000, LONG_WINDOW),
            rate_limit.DIMENSION_EMAIL: email
            if email is not None
            else RateLimitRule(10_000, LONG_WINDOW),
        },
        counter=counter if counter is not None else InMemoryWindowCounter(),
        fallback=fallback,
        namespace=f"authapi:{uuid4().hex}",
        secret="test-rate-limit-secret",
    )


def _client(
    db: AsyncSession,
    *,
    settings: Settings | None = None,
    login_limiter: FixedWindowRateLimiter | None = None,
    signup_limiter: FixedWindowRateLimiter | None = None,
    peer: tuple[str, int] = ("127.0.0.1", 5555),
) -> httpx.AsyncClient:
    app = create_app()
    app.include_router(auth_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[auth_api.db_session] = override_session
    if settings is not None:
        app.dependency_overrides[auth_api.get_auth_settings] = lambda: settings

    login = login_limiter if login_limiter is not None else _limiter()
    signup = signup_limiter if signup_limiter is not None else _limiter()
    app.dependency_overrides[auth_api.get_login_limiter] = lambda: login
    app.dependency_overrides[auth_api.get_signup_limiter] = lambda: signup

    return httpx.AsyncClient(
        # `client` is what `request.client.host` reports, and therefore what the
        # per-IP dimension keys off. Varying it is how the per-email tests prove
        # they are not quietly being served by the per-IP window.
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url="http://test",
    )


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


# --------------------------------------------------------------------------- #
# Server-side revocation
# --------------------------------------------------------------------------- #


async def test_logout_revokes_the_session_so_a_stolen_cookie_stops_working(
    db: AsyncSession,
) -> None:
    """The gap this closes. Not "the browser forgot" -- "the token is refused".

    The cookie value is kept before logging out and replayed afterwards, which is
    exactly what an attacker holding a copy would do. Before the watermark existed
    this returned 200 for another thirty days.
    """
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        stolen = client.cookies["sma_session"]
        assert (await client.get("/api/v1/auth/me")).status_code == 200

        await client.post("/api/v1/auth/logout")

        client.cookies.clear()
        client.cookies.set("sma_session", stolen)
        replayed = await client.get("/api/v1/auth/me")

    assert replayed.status_code == 401


async def test_logout_writes_the_watermark_to_the_user_row(db: AsyncSession) -> None:
    """The mechanism, asserted directly: without the column write, nothing is revoked."""
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        before = await db.execute(
            text("SELECT sessions_valid_from FROM users WHERE email = :e"), {"e": email}
        )
        assert before.scalar_one() is None

        await client.post("/api/v1/auth/logout")

    after = await db.execute(
        text("SELECT sessions_valid_from FROM users WHERE email = :e"), {"e": email}
    )
    watermark = after.scalar_one()
    assert watermark is not None
    assert abs((watermark - datetime.now(UTC)).total_seconds()) < 60


async def test_logout_revokes_every_session_for_that_user_not_only_this_one(
    db: AsyncSession,
) -> None:
    """A second device's cookie dies too, and that is the intended reading.

    The token carries no device identity, so per-device revocation is not
    something this design can offer. Logging out from a machine you do not trust
    should end the sessions you cannot see, so revoking all of them is the safe
    interpretation rather than a limitation to apologise for.
    """
    email = _email()
    async with _client(db) as first_device:
        await _signup(first_device, email)
        other_device_cookie = first_device.cookies["sma_session"]

        async with _client(db) as second_device:
            second_device.cookies.set("sma_session", other_device_cookie)
            assert (await second_device.get("/api/v1/auth/me")).status_code == 200

            await first_device.post("/api/v1/auth/logout")
            after = await second_device.get("/api/v1/auth/me")

    assert after.status_code == 401


async def test_one_users_logout_does_not_touch_another_users_session(db: AsyncSession) -> None:
    """The watermark is per user. A shared one would be a self-inflicted outage."""
    mine, theirs = _email(), _email()
    async with _client(db) as my_client, _client(db) as their_client:
        await _signup(my_client, mine)
        await _signup(their_client, theirs)

        await my_client.post("/api/v1/auth/logout")
        unaffected = await their_client.get("/api/v1/auth/me")

    assert unaffected.status_code == 200
    assert unaffected.json()["email"] == theirs


async def test_a_session_issued_after_the_watermark_is_accepted(db: AsyncSession) -> None:
    """Logging back in after a logout has to work, or logout is a permanent ban."""
    email = _email()
    async with _client(db) as client:
        await _signup(client, email)
        await client.post("/api/v1/auth/logout")
        client.cookies.clear()

        login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        after = await client.get("/api/v1/auth/me")

    assert login.status_code == 200
    assert after.status_code == 200


async def test_a_replacement_session_minted_in_the_same_second_survives(db: AsyncSession) -> None:
    """The same-second edge case, end to end.

    The token records whole seconds. Revoking at ``now()`` and signing a
    replacement inside the same second would truncate the new token to *before* the
    watermark, so it would be refused by its own revocation -- a password change
    that logs you out. ``revoke_sessions`` returns the watermark precisely so the
    replacement can be stamped with it, and this asserts that the returned value is
    a usable ``issued_at`` while a bare ``now()`` from the same instant is not.
    """
    email = _email()
    async with _client(db) as client:
        signup = await _signup(client, email)
        user_id = UUID(signup.json()["userId"])

        revoked_at = datetime.now(UTC)
        watermark = await auth_service.revoke_sessions(user_id, session=db)
        assert watermark >= revoked_at

        secret = get_settings().session_secret
        # What a password-change endpoint must do.
        client.cookies.clear()
        client.cookies.set(
            "sma_session", security.sign_session(user_id, issued_at=watermark, secret=secret)
        )
        replacement = await client.get("/api/v1/auth/me")

        # What it must NOT do: stamp the replacement with the pre-bump instant.
        client.cookies.clear()
        client.cookies.set(
            "sma_session", security.sign_session(user_id, issued_at=revoked_at, secret=secret)
        )
        naive_attempt = await client.get("/api/v1/auth/me")

    assert replacement.status_code == 200, "a session issued AT the watermark must be honoured"
    assert naive_attempt.status_code == 401, "a session issued before it must not be"


async def test_revoking_twice_never_moves_the_watermark_backwards(db: AsyncSession) -> None:
    """A clock that steps back must not un-revoke sessions already refused."""
    email = _email()
    async with _client(db) as client:
        signup = await _signup(client, email)
        user_id = UUID(signup.json()["userId"])

    first = await auth_service.revoke_sessions(user_id, session=db)
    second = await auth_service.revoke_sessions(user_id, session=db)
    assert second >= first


async def test_revoking_an_unknown_user_is_quiet(db: AsyncSession) -> None:
    """Logout is reachable with a valid cookie for a user who has since been deleted."""
    watermark = await auth_service.revoke_sessions(uuid4(), session=db)
    assert watermark is not None


async def test_logout_with_a_forged_cookie_is_still_204_and_revokes_nothing(
    db: AsyncSession,
) -> None:
    """A signature we did not make is not evidence of anything, including of a user.

    If a forged cookie could bump a watermark, anyone could log any user out by
    guessing their id -- an unauthenticated denial of service.
    """
    email = _email()
    async with _client(db) as victim:
        signup = await _signup(victim, email)
        user_id = signup.json()["userId"]

        async with _client(db) as attacker:
            attacker.cookies.set(
                "sma_session",
                security.sign_session(
                    UUID(user_id), issued_at=datetime.now(UTC), secret="an-attackers-guess"
                ),
            )
            logout = await attacker.post("/api/v1/auth/logout")

        still_signed_in = await victim.get("/api/v1/auth/me")

    assert logout.status_code == 204
    assert still_signed_in.status_code == 200


async def test_logout_with_a_garbage_cookie_is_still_204(db: AsyncSession) -> None:
    """A double-clicked logout, or a truncated cookie, must not show an error page."""
    async with _client(db) as client:
        client.cookies.set("sma_session", "not-even-close")
        response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 204


# --------------------------------------------------------------------------- #
# Throttling the two argon2 routes
# --------------------------------------------------------------------------- #


async def test_login_is_rate_limited_per_ip_and_says_when_to_retry(db: AsyncSession) -> None:
    """The email varies on every attempt, so only the per-IP window can trip.

    That is credential stuffing: one host, a different account each time.
    """
    limiter = _limiter(ip=RateLimitRule(limit=3, window_seconds=LONG_WINDOW))
    async with _client(db, login_limiter=limiter) as client:
        statuses = [
            (
                await client.post(
                    "/api/v1/auth/login", json={"email": _email(), "password": "wrong entirely"}
                )
            ).status_code
            for _ in range(5)
        ]
        blocked = await client.post(
            "/api/v1/auth/login", json={"email": _email(), "password": "wrong entirely"}
        )

    assert statuses == [401, 401, 401, 429, 429], statuses
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["Retry-After"]) <= LONG_WINDOW


async def test_the_login_rate_limit_is_per_email_independently_of_the_ip(
    db: AsyncSession,
) -> None:
    """The targeted attack: one account, a fresh source address every time.

    Each attempt comes from a different peer, so the per-IP window never fills.
    Only an independent per-email window stops it -- which is why the two
    dimensions cannot be one key.
    """
    target = _email()
    limiter = _limiter(
        ip=RateLimitRule(limit=1000, window_seconds=LONG_WINDOW),
        email=RateLimitRule(limit=3, window_seconds=LONG_WINDOW),
    )

    statuses = []
    for index in range(5):
        async with _client(
            db, login_limiter=limiter, peer=(f"198.51.100.{index}", 4000 + index)
        ) as client:
            response = await client.post(
                "/api/v1/auth/login", json={"email": target, "password": "wrong entirely"}
            )
            statuses.append(response.status_code)

    assert statuses == [401, 401, 401, 429, 429], statuses


async def test_a_rate_limited_login_never_says_which_limit_tripped(db: AsyncSession) -> None:
    """Naming the dimension tells an attacker which one to route around.

    On signup it would additionally confirm that an address is one somebody keeps
    trying, so the two routes share one message.

    Asserted by tripping each dimension separately and comparing the two responses
    to *each other* -- the same technique as the unknown-email/wrong-password test
    above, and for the same reason: a substring blocklist would break on an
    innocent reword while still passing for a body that leaked the distinction some
    other way. Byte-identical is the actual property.
    """
    ip_only = _limiter(ip=RateLimitRule(limit=1, window_seconds=LONG_WINDOW))
    email_only = _limiter(email=RateLimitRule(limit=1, window_seconds=LONG_WINDOW))
    target = _email()

    async with _client(db, login_limiter=ip_only) as client:
        # Fresh email every time, so only the per-IP window can be what refuses.
        await client.post("/api/v1/auth/login", json={"email": _email(), "password": "x"})
        by_ip = await client.post("/api/v1/auth/login", json={"email": _email(), "password": "x"})

    statuses = []
    for index in range(2):
        # Fresh peer every time, so only the per-email window can be what refuses.
        async with _client(
            db, login_limiter=email_only, peer=(f"203.0.113.{index}", 5000 + index)
        ) as client:
            response = await client.post(
                "/api/v1/auth/login", json={"email": target, "password": "x"}
            )
            statuses.append(response)
    by_email = statuses[-1]

    assert by_ip.status_code == by_email.status_code == 429
    assert by_ip.json() == by_email.json()
    assert by_ip.json()["detail"]["code"] == "rate_limited"


async def test_a_rate_limited_login_never_reaches_argon2(db: AsyncSession) -> None:
    """The refusal has to come BEFORE the hash, or it rations nothing.

    Asserted by giving a real account its real password: a throttled request must
    be refused anyway, and must not set a session cookie.
    """
    email = _email()
    limiter = _limiter(email=RateLimitRule(limit=1, window_seconds=LONG_WINDOW))
    async with _client(db, login_limiter=limiter) as client:
        await _signup(client, email)
        client.cookies.clear()

        first = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        second = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert not second.headers.get_list("set-cookie")


async def test_signup_is_rate_limited_too(db: AsyncSession) -> None:
    """Easy to forget, because signup does not feel like an attack surface.

    It runs the same 64 MiB argon2 hash, it is equally unauthenticated, and it also
    writes two rows.
    """
    limiter = _limiter(ip=RateLimitRule(limit=2, window_seconds=LONG_WINDOW))
    async with _client(db, signup_limiter=limiter) as client:
        statuses = [(await _signup(client, _email())).status_code for _ in range(4)]

    assert statuses == [201, 201, 429, 429], statuses


async def test_the_signup_and_login_budgets_are_separate(db: AsyncSession) -> None:
    """Exhausting one must not lock the other: they have different policies."""
    email = _email()
    async with _client(
        db,
        signup_limiter=_limiter(ip=RateLimitRule(limit=1, window_seconds=LONG_WINDOW)),
        login_limiter=_limiter(ip=RateLimitRule(limit=5, window_seconds=LONG_WINDOW)),
    ) as client:
        first = await _signup(client, email)
        exhausted = await _signup(client, _email())
        client.cookies.clear()
        login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})

    assert first.status_code == 201
    assert exhausted.status_code == 429
    assert login.status_code == 200


async def test_login_still_works_when_redis_is_unreachable(db: AsyncSession) -> None:
    """Fail OPEN, and the argument is in ``rate_limit``'s module docstring.

    A Redis outage must not become an authentication outage: refusing every login
    when the counter store is down hands an attacker the denial of service the
    limiter exists to prevent, by a cheaper route than guessing a password. The
    memory bound does not depend on Redis -- the concurrency gate is process-local
    -- so what is lost is cross-replica accuracy, not the ceiling.
    """
    email = _email()
    limiter = _limiter(
        counter=RedisWindowCounter("redis://127.0.0.1:6399/0", timeout=0.05),
        fallback=InMemoryWindowCounter(),
    )
    async with _client(db, login_limiter=limiter, signup_limiter=limiter) as client:
        signup = await _signup(client, email)
        client.cookies.clear()
        login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})

    assert signup.status_code == 201
    assert login.status_code == 200


async def test_a_dead_redis_still_counts_in_this_process(db: AsyncSession) -> None:
    """Degrading to a process-local window, not to no window at all."""
    limiter = _limiter(
        ip=RateLimitRule(limit=2, window_seconds=LONG_WINDOW),
        counter=RedisWindowCounter("redis://127.0.0.1:6399/0", timeout=0.05),
        fallback=InMemoryWindowCounter(),
    )
    async with _client(db, login_limiter=limiter) as client:
        statuses = [
            (
                await client.post(
                    "/api/v1/auth/login", json={"email": _email(), "password": "wrong"}
                )
            ).status_code
            for _ in range(4)
        ]

    assert statuses == [401, 401, 429, 429], statuses
