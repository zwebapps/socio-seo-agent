"""Fixtures for database-backed tests.

These are the only tests in the suite that need a live service. They are marked
``db`` and skipped when Postgres is genuinely unreachable, so the suite still
passes on a machine with nothing running -- but CI runs a Postgres service, so
they are never quietly skipped where it matters.

Two deliberate choices, both learned by getting them wrong first:

* **Engines are function-scoped.** pytest-asyncio gives each test its own event
  loop, and an asyncpg connection pool is bound to the loop that created it. A
  session-scoped engine therefore fails on the second test with an opaque
  ``InterfaceError`` -- which looks like a database problem and is not one.

* **The skip guard is narrow.** It skips only on connection-level failures and
  re-raises everything else. A blanket ``except Exception`` here turned five real
  bugs into five green "skipped" lines, which is worse than a red suite.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from backend.app.core.config import get_settings

# Only these mean "there is no database to talk to". Anything else is a bug.
_CONNECTION_FAILURES = (OperationalError, ConnectionRefusedError, OSError)


async def _connectable(url: str) -> AsyncEngine:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except _CONNECTION_FAILURES as exc:
        await engine.dispose()
        pytest.skip(f"Postgres unreachable at {url.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    except InterfaceError:
        await engine.dispose()
        raise
    return engine


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """Engine for the RESTRICTED role -- the only one row-level security applies to."""
    engine = await _connectable(get_settings().app_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    """Engine for the table owner. Used only to seed rows RLS would otherwise block."""
    engine = await _connectable(get_settings().database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def owner_session(owner_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
async def two_businesses(owner_session: AsyncSession) -> AsyncIterator[tuple[UUID, UUID]]:
    """Two businesses under two different users, seeded as the owner role.

    Cleaned up on the way out so the suite is re-runnable.
    """
    a_user, b_user = uuid4(), uuid4()
    a_biz, b_biz = uuid4(), uuid4()

    for user_id, biz_id, label in ((a_user, a_biz, "a"), (b_user, b_biz, "b")):
        await owner_session.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active) "
                "VALUES (:id, :email, 'x', true)"
            ),
            {"id": user_id, "email": f"{label}-{user_id}@example.test"},
        )
        await owner_session.execute(
            text("INSERT INTO businesses (id, owner_id, name, locale) VALUES (:id, :o, :n, 'de')"),
            {"id": biz_id, "o": user_id, "n": f"business-{label}"},
        )
    await owner_session.commit()

    yield a_biz, b_biz

    await owner_session.execute(
        text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": [a_user, b_user]}
    )
    await owner_session.commit()
