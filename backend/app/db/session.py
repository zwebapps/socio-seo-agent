"""Async engine, session factory, and the tenant-scoped session helper.

The tenant helper is the important part. Row-level security keys off a
transaction-local GUC, so a session that forgets to set it sees nothing -- which
is the safe direction, but only if every read path goes through one place. That
place is ``business_session``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        # app_database_url, never database_url: the runtime connects as the
        # restricted role so row-level security applies to it.
        _engine = create_async_engine(
            settings.app_database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """An unscoped session.

    Use only for tables that are not business-scoped. For anything carrying
    ``business_id``, use :func:`business_session` so RLS is active.
    """
    factory = get_session_factory()
    async with factory() as s:
        yield s


@asynccontextmanager
async def business_session(business_id: UUID) -> AsyncIterator[AsyncSession]:
    """A session scoped to one business for the life of the transaction.

    ``SET LOCAL`` is transaction-scoped, so the setting cannot leak to the next
    user of a pooled connection -- which a plain ``SET`` would do, and which
    would be a cross-tenant data leak rather than a bug.

    The parameter is bound rather than interpolated: ``SET LOCAL`` does not accept
    bind parameters, so the value is passed through ``set_config`` instead. Never
    build this statement with an f-string.
    """
    factory = get_session_factory()
    async with factory() as s:
        async with s.begin():
            await s.execute(
                text("SELECT set_config('app.current_business_id', :bid, true)"),
                {"bid": str(business_id)},
            )
            yield s
