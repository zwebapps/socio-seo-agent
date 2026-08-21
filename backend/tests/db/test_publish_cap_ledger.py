"""What counts towards the weekly volume cap, against real SQL and real RLS.

The count decides whether an approved post goes out, so every one of these targets a way
of getting it wrong that still returns a plausible number: counting a refusal (a rejected
post would consume the allowance its replacement needed), counting another tenant's
publishes (one busy business would silence the rest), or counting rows outside the window
(the cap would never reset).

Connected as `sma_app`, the restricted role, because that is the only role row-level
security applies to — the owner is a superuser locally and would pass the isolation
assertion against a database with no policies at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from backend.app.db.adapters.action_store import PostgresActionStore
from backend.app.db.session import business_session
from backend.app.services.publish_cap import counter_for, window_start

pytestmark = pytest.mark.db

PUBLISHABLE = frozenset({"social.post"})


async def _action(
    business_id: UUID,
    *,
    status: str = "succeeded",
    action_type: str = "social.post",
    age: timedelta = timedelta(minutes=5),
) -> None:
    """One `actions` row, aged by `age`.

    Raw SQL rather than the ORM: what is under test is the COUNT's predicates, and the
    point of each row is its status, type and age rather than a faithful action.
    """
    async with business_session(business_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO actions
                    (id, business_id, action_type, idempotency_key, status, payload,
                     created_at, updated_at)
                VALUES (:i, :b, :type, :key, :status, '{}'::jsonb, :at, :at)
                """
            ),
            {
                "i": uuid4(),
                "b": business_id,
                "type": action_type,
                "key": f"test-{uuid4()}",
                "status": status,
                "at": datetime.now(UTC) - age,
            },
        )


async def _clear(business_id: UUID) -> None:
    async with business_session(business_id) as session:
        await session.execute(
            text("DELETE FROM actions WHERE idempotency_key LIKE 'test-%'"),
        )


async def _count(business_id: UUID) -> int:
    return await counter_for(PostgresActionStore(), action_types=PUBLISHABLE)(business_id)


async def test_a_succeeded_publish_counts(scoped_sessions: None, business_a: UUID) -> None:
    try:
        await _action(business_a)
        assert await _count(business_a) == 1
    finally:
        await _clear(business_a)


async def test_an_in_flight_publish_counts_too(scoped_sessions: None, business_a: UUID) -> None:
    """The conservative direction: a call in flight will almost certainly land, and a cap
    exists to prevent over-publishing, so where the count is uncertain it errs towards
    refusing."""
    try:
        await _action(business_a, status="in_flight")
        assert await _count(business_a) == 1
    finally:
        await _clear(business_a)


@pytest.mark.parametrize("status", ["refused", "failed"])
async def test_a_refusal_or_a_failure_does_not_count(
    scoped_sessions: None, business_a: UUID, status: str
) -> None:
    """Nothing was published, so nothing was produced. Counting a refusal would let a
    rejected post consume the allowance that would have let its replacement out."""
    try:
        await _action(business_a, status=status)
        assert await _count(business_a) == 0
    finally:
        await _clear(business_a)


async def test_another_action_type_does_not_count(scoped_sessions: None, business_a: UUID) -> None:
    """The cap is on published PIECES. An owner notice is not a piece, and counting one
    would let an email silence a channel."""
    try:
        await _action(business_a, action_type="notify.owner")
        await _action(business_a, action_type="publish.page")
        assert await _count(business_a) == 0
    finally:
        await _clear(business_a)


async def test_a_publish_older_than_the_window_does_not_count(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Otherwise the cap never resets and a business is silenced permanently by one busy
    week."""
    try:
        await _action(business_a, age=timedelta(days=8))
        assert await _count(business_a) == 0
    finally:
        await _clear(business_a)


async def test_a_publish_just_inside_the_window_counts(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The boundary in the direction that matters: six days and 23 hours ago is still
    this week, and rounding it out would widen every business's real allowance."""
    try:
        await _action(business_a, age=timedelta(days=6, hours=23))
        assert await _count(business_a) == 1
    finally:
        await _clear(business_a)


async def test_the_window_start_is_seven_days_back() -> None:
    """Pins the arithmetic the SQL is given, since the query itself takes `since` as a
    parameter and cannot be wrong about it on its own."""
    now = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
    assert window_start(now=now) == datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


async def test_one_businesss_publishes_do_not_consume_anothers_allowance(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """The requirement A6 states explicitly, and RLS is what enforces it — exercised here
    rather than assumed, because a count is exactly the shape of query that reads as zero
    when the tenant GUC is unset and as everything when the policy is wrong."""
    try:
        for _ in range(4):
            await _action(business_a)
        await _action(business_b)

        assert await _count(business_a) == 4
        assert await _count(business_b) == 1
    finally:
        await _clear(business_a)
        await _clear(business_b)
