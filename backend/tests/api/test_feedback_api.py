"""The three feedback routes, end to end.

The database-backed test at the bottom is the Phase 13 demo run through HTTP: three
rejections of the same theme, a proposal appearing in the panel, an approval, and
the rule then present in business memory where the next run's system prompt will
read it. Everything above it is the refusals, which is where an API is actually
attacked:

  no session                 -> 401  (who are you?)
  another business's id      -> 404  (not 403 — the id's existence is information)
  a rating outside the scale -> 422, and nothing written

The 422 tests are hermetic on purpose, and the fake session opener is an assertion
rather than a shortcut: it raises if anything reaches the database, so a passing
test proves a malformed rubric is refused BEFORE a transaction is opened.

``current_user`` is overridden rather than a real cookie being minted, so these
tests do not move when the session-token format does. Authentication itself is
tested in ``test_auth_api.py``; what is under test here is that these routes
require it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from backend.app.api import feedback as feedback_api
from backend.app.api import runs as runs_api
from backend.app.api.auth import current_user
from backend.app.core.config import get_settings
from backend.app.db import session as db_session
from backend.app.db.models import Role, User
from backend.app.main import create_app

EMAIL_PREFIX = "fbapi-test-"
_CONNECTION_FAILURES = (OperationalError, ConnectionRefusedError, OSError)

EXCLAIMS = "Too many exclamation marks"
EXPECTED_RULE = "Never use exclamation marks."


def _user(user_id: UUID | None = None) -> User:
    user = User(email="owner@example.test", password_hash="x", is_active=True, role=Role.OWNER)
    if user_id is not None:
        user.id = user_id
    return user


class _NeverOpened:
    """A session that fails the test if the route reaches the database."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused request opened a query")


@asynccontextmanager
async def _never_used(business_id: UUID) -> AsyncIterator[Any]:
    del business_id
    yield _NeverOpened()


def _app(
    *,
    user: User | None,
    business_id: UUID | None = None,
    opener: Any = None,
) -> httpx.AsyncClient:
    """``user=None`` means no session. ``business_id=None`` means resolve it for real."""
    app = create_app()
    app.include_router(feedback_api.router)

    if user is None:

        def _no_session() -> User:
            raise HTTPException(status_code=401, detail={"code": "not_authenticated"})

        app.dependency_overrides[current_user] = _no_session
    else:
        app.dependency_overrides[current_user] = lambda: user

    if business_id is not None:
        app.dependency_overrides[runs_api.current_business] = lambda: business_id
    if opener is not None:
        app.dependency_overrides[feedback_api.get_business_session_opener] = lambda: opener

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


PROTECTED = [
    ("POST", f"/api/v1/content/{uuid4()}/feedback"),
    ("GET", f"/api/v1/businesses/{uuid4()}/proposals"),
    ("POST", f"/api/v1/proposals/{uuid4()}/approve"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_every_route_needs_a_session(method: str, path: str) -> None:
    async with _app(user=None) as client:
        response = await client.request(method, path, json={"verdict": "approved"})
    assert response.status_code == 401


@pytest.mark.parametrize("rating", [0, 6, -3, 42])
async def test_a_rating_outside_the_scale_is_422_and_writes_nothing(rating: int) -> None:
    """The fake opener raises on any query, so this also proves the refusal happens
    before a transaction is opened."""
    async with _app(user=_user(), business_id=uuid4(), opener=_never_used) as client:
        response = await client.post(
            f"/api/v1/content/{uuid4()}/feedback",
            json={"verdict": "rejected", "axes": {"onBrand": rating}, "rejectReason": "x"},
        )

    assert response.status_code == 422


async def test_an_unknown_axis_is_422() -> None:
    async with _app(user=_user(), business_id=uuid4(), opener=_never_used) as client:
        response = await client.post(
            f"/api/v1/content/{uuid4()}/feedback",
            json={"verdict": "approved", "axes": {"vibes": 5}},
        )

    assert response.status_code == 422


async def test_an_unknown_verdict_is_422() -> None:
    async with _app(user=_user(), business_id=uuid4(), opener=_never_used) as client:
        response = await client.post(
            f"/api/v1/content/{uuid4()}/feedback", json={"verdict": "meh", "axes": {}}
        )

    assert response.status_code == 422


@pytest.mark.parametrize("spelling", ["onBrand", "on_brand"])
async def test_an_axis_may_be_spelled_either_way_on_the_wire(spelling: str) -> None:
    """The rest of the wire is camelCase, and a dict KEY is the one place an alias
    generator does not reach — so both spellings are accepted rather than leaving a
    TypeScript client to guess which half of the payload changes case.

    Asserted through a REFUSAL: the message names the axis, which is the cheapest
    way to see that the key was understood as an axis at all rather than dropped or
    treated as unknown.
    """
    async with _app(user=_user(), business_id=uuid4(), opener=_never_used) as client:
        response = await client.post(
            f"/api/v1/content/{uuid4()}/feedback",
            json={"verdict": "rejected", "axes": {spelling: 99}},
        )

    # 422 because 99 is out of range -- not because the axis was unrecognised.
    assert response.status_code == 422
    detail = response.json()["detail"]["message"]
    assert "on_brand" in detail
    assert "between" in detail


async def test_proposals_for_a_business_you_do_not_own_is_404() -> None:
    """404, not 403: whether that business exists is not the caller's business."""
    async with _app(user=_user(), business_id=uuid4(), opener=_never_used) as client:
        response = await client.get(f"/api/v1/businesses/{uuid4()}/proposals")

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Database-backed: the Phase 13 demo, over HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
async def _reset_process_engine() -> AsyncIterator[None]:
    """Dispose the process-wide engine after every test in this module.

    The routes under test use the real ``db.session`` engine, which is a module
    global with a connection pool. pytest-asyncio gives each test its own event
    loop, and an asyncpg connection belongs to the loop that opened it — so the
    second database test in this file would inherit a pooled connection whose loop
    is closed and fail with ``RuntimeError: Event loop is closed``, which looks like
    a database problem and is not one.

    Reaching into the module's privates is deliberate and is the honest fix here: a
    process-wide engine is right for a server that has one loop for its whole life,
    and wrong for a test suite that has one per test.
    """
    yield
    engine = db_session._engine
    if engine is not None:
        await engine.dispose()
        db_session._engine = None
        db_session._session_factory = None


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except _CONNECTION_FAILURES as exc:
        await engine.dispose()
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")
    except InterfaceError:
        await engine.dispose()
        raise
    yield engine
    await engine.dispose()


@pytest.fixture
async def seeded(owner_engine: AsyncEngine) -> AsyncIterator[tuple[User, UUID, UUID]]:
    """A real user, business and content piece. Yields ``(user, business, piece)``."""
    user_id, business_id, piece_id = uuid4(), uuid4(), uuid4()
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        owner_engine, expire_on_commit=False
    )

    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active) "
                "VALUES (:id, :email, 'x', true)"
            ),
            {"id": user_id, "email": f"{EMAIL_PREFIX}{user_id.hex}@example.test"},
        )
        await s.execute(
            text(
                "INSERT INTO businesses (id, owner_id, name, slug, locale) VALUES "
                "(:id, :o, :n, 'fixture-' || "
                "left(replace(cast(gen_random_uuid() AS text), '-', ''), 12), 'de')"
            ),
            {"id": business_id, "o": user_id, "n": "fbapi business"},
        )
        await s.execute(
            text(
                "INSERT INTO content_pieces (id, business_id, surface, title, body_md, status) "
                "VALUES (:id, :b, 'google', 'piece', 'body', 'draft')"
            ),
            {"id": piece_id, "b": business_id},
        )
        await s.commit()

    yield _user(user_id), business_id, piece_id

    async with factory() as s:
        await s.execute(
            text("DELETE FROM users WHERE email LIKE :prefix"), {"prefix": f"{EMAIL_PREFIX}%"}
        )
        await s.commit()


async def _dna(engine: AsyncEngine, business_id: UUID) -> dict[str, Any]:
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        stored = (
            await s.execute(text("SELECT dna FROM businesses WHERE id = :id"), {"id": business_id})
        ).scalar_one()
    return dict(stored or {})


@pytest.mark.db
async def test_three_rejections_then_an_approval_puts_a_rule_into_memory(
    owner_engine: AsyncEngine, seeded: tuple[User, UUID, UUID]
) -> None:
    """The demo. Note what is asserted between the steps, not only at the end: after
    three rejections the rule is PROPOSED and ``dna`` is still empty. An agent that
    applied it here would be changing how the business sounds without being asked."""
    user, business_id, piece_id = seeded

    # `current_business` is deliberately NOT overridden: resolving the caller's
    # business from the session is part of what this test covers.
    async with _app(user=user) as client:
        first = await client.post(
            f"/api/v1/content/{piece_id}/feedback",
            json={
                "verdict": "rejected",
                "axes": {"onBrand": 2, "accuracy": 5, "seo": 4, "usefulness": 3},
                "rejectReason": EXCLAIMS,
            },
        )
        assert first.status_code == 201
        assert first.json()["proposedRules"] == []

        second = await client.post(
            f"/api/v1/content/{piece_id}/feedback",
            json={"verdict": "rejected", "rejectReason": "please stop with the exclamation marks"},
        )
        assert second.json()["proposedRules"] == [], "two is not yet a pattern"

        third = await client.post(
            f"/api/v1/content/{piece_id}/feedback",
            json={"verdict": "rejected", "rejectReason": "bitte keine Ausrufezeichen mehr"},
        )
        assert third.json()["proposedRules"] == [EXPECTED_RULE]

        # Proposed, and NOT in force.
        assert (await _dna(owner_engine, business_id)).get("preferences", []) == []

        listed = await client.get(f"/api/v1/businesses/{business_id}/proposals")
        assert listed.status_code == 200
        proposals = listed.json()
        assert [p["rule"] for p in proposals] == [EXPECTED_RULE]
        assert proposals[0]["status"] == "proposed"
        assert len(proposals[0]["derivedFrom"]) == 3

        approved = await client.post(f"/api/v1/proposals/{proposals[0]['id']}/approve")
        assert approved.status_code == 200
        assert approved.json() == {"rule": EXPECTED_RULE, "applied": True}

    # Now, and only now, it is what the next run's system prompt will carry.
    assert (await _dna(owner_engine, business_id))["preferences"] == [EXPECTED_RULE]


@pytest.mark.db
async def test_rating_a_piece_that_is_not_yours_is_404(seeded: tuple[User, UUID, UUID]) -> None:
    user, _, _ = seeded
    async with _app(user=user) as client:
        response = await client.post(
            f"/api/v1/content/{uuid4()}/feedback",
            json={"verdict": "approved", "axes": {"seo": 5}},
        )

    assert response.status_code == 404


@pytest.mark.db
async def test_approving_an_unknown_proposal_is_404(seeded: tuple[User, UUID, UUID]) -> None:
    user, _, _ = seeded
    async with _app(user=user) as client:
        response = await client.post(f"/api/v1/proposals/{uuid4()}/approve")

    assert response.status_code == 404


@pytest.mark.db
async def test_an_approval_is_still_recorded_when_nothing_recurs(
    owner_engine: AsyncEngine, seeded: tuple[User, UUID, UUID]
) -> None:
    """An approval carries no reason, so it can never distil into a rule — and it
    must not be refused for that."""
    user, business_id, piece_id = seeded
    async with _app(user=user) as client:
        response = await client.post(
            f"/api/v1/content/{piece_id}/feedback",
            json={"verdict": "approved", "axes": {"onBrand": 5, "accuracy": 5}},
        )

    assert response.status_code == 201
    assert response.json()["proposedRules"] == []
    assert (await _dna(owner_engine, business_id)).get("preferences", []) == []
