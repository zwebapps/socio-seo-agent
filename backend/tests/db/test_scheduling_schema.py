"""The scheduling tables are tenant-isolated by the database, and this proves it.

`test_tenant_isolation.py` derives the list of business-scoped tables from the ORM and
asserts each one HAS a policy, which catches a new table that shipped with none. It does
not exercise a policy: it drives all of its reads and writes through `documents`. So a
policy that exists and is wrong -- the wrong column, USING without WITH CHECK, a cast
that raises instead of denying -- passes that file and fails here.

Two things these tests are careful about, both copied from that file because both would
make a test pass while proving nothing:

* They connect as `sma_app`, NOT the owner. The owner is a superuser locally and a
  superuser bypasses RLS entirely, so the same assertions would pass against a database
  with no policies at all.
* The seed rows are inserted as the owner, because RLS is exactly what would stop the
  restricted role planting business A's row in the first place.

`social_posts` gets one extra assertion the other tables do not need: it is reached
through `content_pieces`, so the test proves that a JOIN back to the parent piece does
not smuggle a row past the policy either.
"""

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

_INSERT_PIECE = (
    "INSERT INTO content_pieces (id, business_id, surface, title, body_md, status) "
    "VALUES (:id, :b, 'blog_article', :title, 'body', 'draft')"
)
_INSERT_POST = (
    "INSERT INTO social_posts "
    "(id, business_id, content_piece_id, platform, body, status, scheduled_at) "
    "VALUES (:id, :b, :piece, 'linkedin', :body, 'scheduled', now())"
)
_INSERT_SETTINGS = (
    "INSERT INTO automation_settings "
    "(id, business_id, mode, cadence, day_of_week, hour, timezone) "
    "VALUES (:id, :b, 'scheduled_draft', :cadence, :dow, :hour, 'Europe/Berlin')"
)


async def _scoped(engine: AsyncEngine, business_id: UUID) -> AsyncIterator[AsyncSession]:
    """A session scoped to one business, exactly as the runtime scopes one."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        await s.execute(
            text("SELECT set_config('app.current_business_id', :bid, true)"),
            {"bid": str(business_id)},
        )
        yield s


async def _seed_piece(session: AsyncSession, business_id: UUID, title: str) -> UUID:
    """A content piece for `business_id`, inserted as the OWNER -- RLS is exactly what
    would stop the restricted role planting another business's row."""
    piece_id = uuid4()
    await session.execute(text(_INSERT_PIECE), {"id": piece_id, "b": business_id, "title": title})
    return piece_id


# --------------------------------------------------------------------------- #
# social_posts
# --------------------------------------------------------------------------- #


async def test_business_b_cannot_read_business_a_social_posts(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    business_a, business_b = two_businesses
    piece_a = await _seed_piece(owner_session, business_a, "a-piece")
    post_a = uuid4()
    await owner_session.execute(
        text(_INSERT_POST),
        {"id": post_a, "b": business_a, "piece": piece_a, "body": "A's unpublished draft"},
    )
    await owner_session.commit()

    async for s in _scoped(app_engine, business_a):
        assert (await s.execute(text("SELECT count(*) FROM social_posts"))).scalar_one() == 1, (
            "a business must see its own scheduled posts"
        )

    async for s in _scoped(app_engine, business_b):
        assert (await s.execute(text("SELECT count(*) FROM social_posts"))).scalar_one() == 0, (
            "business B read business A's scheduled posts -- isolation is broken"
        )

        targeted = (
            await s.execute(
                text("SELECT count(*) FROM social_posts WHERE id = :id"), {"id": post_a}
            )
        ).scalar_one()
        assert targeted == 0, "a targeted query bypassed the policy"

        # The route this table is actually read by: post joined to its piece. A policy on
        # only one of the two tables would let the join return the other's columns.
        joined = (
            await s.execute(
                text(
                    "SELECT count(*) FROM social_posts p "
                    "JOIN content_pieces c ON c.id = p.content_piece_id"
                )
            )
        ).scalar_one()
        assert joined == 0, "a join to content_pieces returned another business's post"


async def test_business_b_cannot_insert_a_social_post_into_business_a(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    """WITH CHECK must refuse a forged business_id.

    Without it the policy still filters every read, so the table looks isolated -- while B
    can plant a post that A's own publisher will pick up and send to A's connected
    account.
    """
    business_a, business_b = two_businesses
    piece_a = await _seed_piece(owner_session, business_a, "a-piece")
    await owner_session.commit()

    with pytest.raises(DBAPIError):
        async for s in _scoped(app_engine, business_b):
            await s.execute(
                text(_INSERT_POST),
                {"id": uuid4(), "b": business_a, "piece": piece_a, "body": "forged"},
            )


async def test_business_b_cannot_update_or_delete_business_a_social_posts(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    """Silent no-ops rather than errors, which is why the row count is what is asserted.

    Rewriting the body of another business's scheduled post is the worst available
    outcome for this table: it publishes attacker-chosen text under someone else's brand,
    and every screen keeps showing the row as theirs.
    """
    business_a, business_b = two_businesses
    piece_a = await _seed_piece(owner_session, business_a, "a-piece")
    post_a = uuid4()
    await owner_session.execute(
        text(_INSERT_POST),
        {"id": post_a, "b": business_a, "piece": piece_a, "body": "original body"},
    )
    await owner_session.commit()

    async for s in _scoped(app_engine, business_b):
        updated = cast(
            "CursorResult[Any]",
            await s.execute(
                text("UPDATE social_posts SET body = 'hijacked' WHERE id = :id"), {"id": post_a}
            ),
        )
        assert updated.rowcount == 0, "a cross-tenant UPDATE matched a scheduled post"

        deleted = cast(
            "CursorResult[Any]",
            await s.execute(text("DELETE FROM social_posts WHERE id = :id"), {"id": post_a}),
        )
        assert deleted.rowcount == 0, "a cross-tenant DELETE matched a scheduled post"

    row = (
        await owner_session.execute(
            text("SELECT body, status FROM social_posts WHERE id = :id"), {"id": post_a}
        )
    ).one_or_none()
    assert row is not None and row.body == "original body"


async def test_an_unknown_social_post_status_is_refused(
    two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    """The CHECK, exercised. `sent` and `posted` are the words someone will reach for, and
    a status the publisher does not recognise is a post that either never goes out or goes
    out twice."""
    business_a, _ = two_businesses
    piece_a = await _seed_piece(owner_session, business_a, "a-piece")
    await owner_session.commit()

    with pytest.raises(IntegrityError):
        await owner_session.execute(
            text(
                "INSERT INTO social_posts (id, business_id, content_piece_id, platform, "
                "body, status) VALUES (:id, :b, :piece, 'linkedin', 'x', 'sent')"
            ),
            {"id": uuid4(), "b": business_a, "piece": piece_a},
        )
    await owner_session.rollback()


async def test_deleting_the_content_piece_takes_its_social_posts(
    two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    """CASCADE, exercised. Without it the FK would refuse the piece delete, or -- with SET
    NULL -- leave a scheduled post the publisher would still send for content nobody
    owns."""
    business_a, _ = two_businesses
    piece_a = await _seed_piece(owner_session, business_a, "a-piece")
    await owner_session.execute(
        text(_INSERT_POST),
        {"id": uuid4(), "b": business_a, "piece": piece_a, "body": "body"},
    )
    await owner_session.commit()

    await owner_session.execute(text("DELETE FROM content_pieces WHERE id = :id"), {"id": piece_a})
    await owner_session.commit()

    remaining = (
        await owner_session.execute(
            text("SELECT count(*) FROM social_posts WHERE content_piece_id = :id"), {"id": piece_a}
        )
    ).scalar_one()
    assert remaining == 0


# --------------------------------------------------------------------------- #
# automation_settings
# --------------------------------------------------------------------------- #


async def test_business_b_cannot_read_business_a_automation_settings(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    business_a, business_b = two_businesses
    settings_a = uuid4()
    await owner_session.execute(
        text(_INSERT_SETTINGS),
        {"id": settings_a, "b": business_a, "cadence": "weekly", "dow": 1, "hour": 9},
    )
    await owner_session.commit()

    async for s in _scoped(app_engine, business_a):
        own = (await s.execute(text("SELECT count(*) FROM automation_settings"))).scalar_one()
        assert own == 1, "a business must see its own automation settings"

    async for s in _scoped(app_engine, business_b):
        other = (await s.execute(text("SELECT count(*) FROM automation_settings"))).scalar_one()
        assert other == 0, "business B read business A's automation settings"

        targeted = (
            await s.execute(
                text("SELECT count(*) FROM automation_settings WHERE id = :id"), {"id": settings_a}
            )
        ).scalar_one()
        assert targeted == 0, "a targeted query bypassed the policy"


async def test_business_b_cannot_insert_automation_settings_for_business_a(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID]
) -> None:
    """The forged-row case for this table means writing another business's schedule --
    which, with `mode = scheduled_draft`, means spending their model budget."""
    business_a, business_b = two_businesses

    with pytest.raises(DBAPIError):
        async for s in _scoped(app_engine, business_b):
            await s.execute(
                text(_INSERT_SETTINGS),
                {"id": uuid4(), "b": business_a, "cadence": "weekly", "dow": 1, "hour": 9},
            )


async def test_business_b_cannot_update_or_delete_business_a_automation_settings(
    app_engine: AsyncEngine, two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    business_a, business_b = two_businesses
    settings_a = uuid4()
    await owner_session.execute(
        text(_INSERT_SETTINGS),
        {"id": settings_a, "b": business_a, "cadence": "weekly", "dow": 1, "hour": 9},
    )
    await owner_session.commit()

    async for s in _scoped(app_engine, business_b):
        updated = cast(
            "CursorResult[Any]",
            await s.execute(
                text("UPDATE automation_settings SET mode = 'off' WHERE id = :id"),
                {"id": settings_a},
            ),
        )
        assert updated.rowcount == 0, "a cross-tenant UPDATE switched off another's automation"

        deleted = cast(
            "CursorResult[Any]",
            await s.execute(
                text("DELETE FROM automation_settings WHERE id = :id"), {"id": settings_a}
            ),
        )
        assert deleted.rowcount == 0, "a cross-tenant DELETE matched an automation setting"

    row = (
        await owner_session.execute(
            text("SELECT mode FROM automation_settings WHERE id = :id"), {"id": settings_a}
        )
    ).one_or_none()
    assert row is not None and row.mode == "scheduled_draft"


async def test_a_business_gets_at_most_one_automation_settings_row(
    two_businesses: tuple[UUID, UUID], owner_session: AsyncSession
) -> None:
    """The uniqueness rule, exercised as the owner so RLS cannot be what refuses it.

    Two rows would be two answers to "when does this business publish", and the scheduler
    would honour whichever the planner happened to return first -- so an owner who
    switched to monthly would keep getting weekly runs with nothing on screen to explain
    it.
    """
    business_a, _ = two_businesses
    await owner_session.execute(
        text(_INSERT_SETTINGS),
        {"id": uuid4(), "b": business_a, "cadence": "weekly", "dow": 1, "hour": 9},
    )
    await owner_session.commit()

    with pytest.raises(IntegrityError):
        await owner_session.execute(
            text(_INSERT_SETTINGS),
            {"id": uuid4(), "b": business_a, "cadence": "monthly", "dow": 3, "hour": 18},
        )
    await owner_session.rollback()


@pytest.mark.parametrize(
    ("cadence", "day_of_week", "hour"),
    [
        ("fortnightly", 1, 9),  # a cadence the arithmetic has no rule for
        ("weekly", 7, 9),  # the ISO-vs-Python off-by-one, arriving as data
        ("weekly", -1, 9),
        ("weekly", 1, 24),
        ("weekly", 1, -1),
    ],
)
async def test_out_of_range_schedule_values_are_refused(
    two_businesses: tuple[UUID, UUID],
    owner_session: AsyncSession,
    cadence: str,
    day_of_week: int,
    hour: int,
) -> None:
    """Each of these would otherwise be a settings row that silently never runs: a
    weekday index no date matches, or a cadence `compute_next_run` refuses. The failure
    is invisible -- automation reads as on, and nothing is ever produced."""
    business_a, _ = two_businesses

    with pytest.raises(IntegrityError):
        await owner_session.execute(
            text(_INSERT_SETTINGS),
            {
                "id": uuid4(),
                "b": business_a,
                "cadence": cadence,
                "dow": day_of_week,
                "hour": hour,
            },
        )
    await owner_session.rollback()


# --------------------------------------------------------------------------- #
# Both tables
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", ["social_posts", "automation_settings"])
async def test_the_policy_is_forced_not_merely_enabled(app_engine: AsyncEngine, table: str) -> None:
    """ENABLE without FORCE looks identical in `pg_policies` and protects nothing from the
    role that owns the table, which is the role every migration runs as."""
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            )
        ).one()

    assert row.relrowsecurity is True, f"{table} does not have row-level security enabled"
    assert row.relforcerowsecurity is True, f"{table} has RLS enabled but not FORCEd"


@pytest.mark.parametrize("table", ["social_posts", "automation_settings"])
async def test_an_emptied_tenant_guc_yields_zero_rows_not_an_error(
    app_engine: AsyncEngine, table: str
) -> None:
    """The recycled-connection case: `set_config(..., true)` is transaction-local, so at
    COMMIT the GUC becomes the EMPTY STRING rather than unset. A policy that casts it
    directly (`''::uuid`) raises instead of denying, which turns an authorisation result
    into what looks like an outage -- and only under connection reuse, so it is absent
    from a fresh run. See migration `5b532c05f131`."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as s:
        async with s.begin():
            await s.execute(
                text("SELECT set_config('app.current_business_id', :bid, true)"),
                {"bid": str(uuid4())},
            )
            await s.execute(text(f"SELECT count(*) FROM {table}"))

        # Same connection, now with an EMPTY (not unset) GUC. Must be 0, not an exception.
        after = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()

    assert after == 0
