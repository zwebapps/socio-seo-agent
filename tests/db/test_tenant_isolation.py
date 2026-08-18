"""Cross-business isolation is a database guarantee, and this proves it.

The claim under test: with row-level security in force and the runtime connected
as the restricted role, business B cannot read, update, or delete business A's
rows -- and cannot forge a row into A's tenancy either.

Two things this test is careful about, because both would make it pass while
proving nothing:

* It connects as ``sma_app``, NOT the owner. The owner is a superuser locally,
  and a superuser bypasses RLS entirely, so the same assertions would pass
  against a database with no policies at all.
* It asserts the guard is real by first checking that the *unset* case returns
  zero rows. A policy comparing against a NULL GUC denies everything, so a test
  that only ever sets the GUC could not distinguish a working policy from a
  broken one.
"""

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.db import models  # noqa: F401  -- registers every table on the metadata
from backend.app.db.base import Base

pytestmark = pytest.mark.db


def _business_scoped_tables() -> list[str]:
    """Every table carrying ``business_id``, derived from the ORM.

    Derived rather than hardcoded on purpose. A hardcoded list is a list someone
    forgets to update, and the thing they would forget is a cross-tenant policy --
    so a new business-scoped table now fails this test automatically instead of
    quietly shipping without protection.
    """
    return sorted(name for name, table in Base.metadata.tables.items() if "business_id" in table.c)


BUSINESS_SCOPED_TABLES = _business_scoped_tables()


async def _scoped(engine: AsyncEngine, business_id: UUID) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        await s.execute(
            text("SELECT set_config('app.current_business_id', :bid, true)"),
            {"bid": str(business_id)},
        )
        yield s


async def test_every_business_scoped_table_has_a_policy(app_engine: AsyncEngine) -> None:
    """The list above must match reality, in both directions."""
    async with app_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_policies WHERE schemaname = 'public'")
        )
        with_policy = {r[0] for r in rows}

    assert BUSINESS_SCOPED_TABLES, (
        "derivation found no business-scoped tables; the test would be vacuous"
    )

    missing = set(BUSINESS_SCOPED_TABLES) - with_policy
    assert not missing, f"business-scoped tables without an RLS policy: {sorted(missing)}"


async def test_runtime_role_cannot_bypass_rls(app_engine: AsyncEngine) -> None:
    """If the runtime role ever gains superuser or BYPASSRLS, every policy is void."""
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()

    assert row.rolsuper is False, "the runtime role is a superuser; RLS does not apply to it"
    assert row.rolbypassrls is False, "the runtime role has BYPASSRLS; every policy is decorative"


async def test_unscoped_session_reads_nothing(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    """With no GUC set, the policy compares against NULL and denies everything."""
    business_a, _ = two_businesses
    await owner_session.execute(
        text(
            "INSERT INTO documents (id, business_id, filename, kind, status, chunk_count) "
            "VALUES (:id, :b, 'a.pdf', 'pdf', 'indexed', 1)"
        ),
        {"id": uuid4(), "b": business_a},
    )
    await owner_session.commit()

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as s:
        count = (await s.execute(text("SELECT count(*) FROM documents"))).scalar_one()

    assert count == 0, "an unscoped session must see nothing, not everything"


async def test_business_b_cannot_read_business_a(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    business_a, business_b = two_businesses
    doc_a = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO documents (id, business_id, filename, kind, status, chunk_count) "
            "VALUES (:id, :b, 'secret-a.pdf', 'pdf', 'indexed', 3)"
        ),
        {"id": doc_a, "b": business_a},
    )
    await owner_session.commit()

    async for s in _scoped(app_engine, business_a):
        own = (await s.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        assert own == 1, "a business must see its own rows"

    async for s in _scoped(app_engine, business_b):
        other = (await s.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        assert other == 0, "business B read business A's documents -- isolation is broken"

        # An explicit WHERE naming A's id must not help.
        targeted = (
            await s.execute(text("SELECT count(*) FROM documents WHERE id = :id"), {"id": doc_a})
        ).scalar_one()
        assert targeted == 0, "a targeted query bypassed the policy"


async def test_business_b_cannot_write_into_business_a(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID]
) -> None:
    """WITH CHECK must stop a forged business_id on insert."""
    business_a, business_b = two_businesses

    with pytest.raises(DBAPIError):
        async for s in _scoped(app_engine, business_b):
            await s.execute(
                text(
                    "INSERT INTO documents (id, business_id, filename, kind, status, chunk_count) "
                    "VALUES (:id, :b, 'forged.pdf', 'pdf', 'indexed', 1)"
                ),
                {"id": uuid4(), "b": business_a},
            )


async def test_business_b_cannot_update_or_delete_business_a(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    """Silent no-ops, not errors -- which is why the row count is asserted."""
    business_a, business_b = two_businesses
    doc_a = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO documents (id, business_id, filename, kind, status, chunk_count) "
            "VALUES (:id, :b, 'original.pdf', 'pdf', 'indexed', 1)"
        ),
        {"id": doc_a, "b": business_a},
    )
    await owner_session.commit()

    async for s in _scoped(app_engine, business_b):
        updated = cast(
            "CursorResult[Any]",
            await s.execute(
                text("UPDATE documents SET filename = 'hijacked.pdf' WHERE id = :id"),
                {"id": doc_a},
            ),
        )
        assert updated.rowcount == 0, "a cross-tenant UPDATE matched a row"

        deleted = cast(
            "CursorResult[Any]",
            await s.execute(text("DELETE FROM documents WHERE id = :id"), {"id": doc_a}),
        )
        assert deleted.rowcount == 0, "a cross-tenant DELETE matched a row"

    row = (
        await owner_session.execute(
            text("SELECT filename FROM documents WHERE id = :id"), {"id": doc_a}
        )
    ).one_or_none()
    assert row is not None and row.filename == "original.pdf"
