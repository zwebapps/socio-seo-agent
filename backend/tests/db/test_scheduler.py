"""The scheduler, against real SQL and real RLS.

`db`-marked because the interesting properties are all database properties. The most
important one cannot be tested any other way: `due_automations()` exists because
`automation_settings` has FORCE RLS, so a plain cross-business SELECT reads ZERO rows
and raises nothing — a scheduler that would find no work forever while logging nothing
wrong. A test with a mocked session could not tell those apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from backend.app.core.config import get_settings
from backend.app.db.session import business_session, session
from backend.app.services import cost_service
from backend.app.worker.scheduler import STRANDED_AFTER, tick

pytestmark = pytest.mark.db


class _Submit:
    """Records what was handed to the executor, and never runs a graph."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, str]] = []

    def __call__(self, run_id: UUID, business_id: UUID, goal: str, **kwargs: Any) -> None:
        self.calls.append((run_id, business_id, goal))


async def _automation(
    business_id: UUID,
    *,
    mode: str = "scheduled_draft",
    next_run_at: datetime | None,
    channels: str = '["linkedin"]',
    goal: str | None = "more emergency callouts",
    paused_reason: str | None = None,
) -> None:
    async with business_session(business_id) as db:
        await db.execute(
            text(
                """
                INSERT INTO automation_settings
                    (id, business_id, mode, cadence, day_of_week, hour, timezone,
                     channels, goal_template, next_run_at, paused_reason,
                     created_at, updated_at)
                VALUES (:i, :b, :mode, 'weekly', 1, 9, 'Europe/Berlin',
                        (:channels)::jsonb, :goal, :next_run_at, :paused,
                        now(), now())
                ON CONFLICT (business_id) DO UPDATE
                    SET mode = :mode, next_run_at = :next_run_at,
                        channels = (:channels)::jsonb, goal_template = :goal,
                        paused_reason = :paused
                """
            ),
            {
                "i": uuid4(),
                "b": business_id,
                "mode": mode,
                "channels": channels,
                "goal": goal,
                "next_run_at": next_run_at,
                "paused": paused_reason,
            },
        )


async def _settings(business_id: UUID) -> Any:
    async with business_session(business_id) as db:
        return (
            await db.execute(
                text(
                    "SELECT next_run_at, last_run_at FROM automation_settings "
                    "WHERE business_id = :b"
                ),
                {"b": business_id},
            )
        ).one()


async def _clear(business_id: UUID) -> None:
    async with business_session(business_id) as db:
        await db.execute(
            text("DELETE FROM automation_settings WHERE business_id = :b"), {"b": business_id}
        )


# --------------------------------------------------------------------------- #
# the cross-business read
# --------------------------------------------------------------------------- #


async def test_a_plain_select_sees_nothing_which_is_why_the_definer_exists(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The failure this whole design is shaped around.

    `automation_settings` has FORCE RLS keyed on `app.current_business_id`. An ordinary
    session with that GUC unset reads zero rows and raises NOTHING, so a scheduler built
    on a plain SELECT would report "no work due" forever with clean logs. Asserted
    rather than assumed, because it is the difference between a working scheduler and one
    that has never run.
    """
    await _automation(business_a, next_run_at=datetime.now(UTC) - timedelta(minutes=5))
    try:
        async with session() as db:
            unscoped = (
                await db.execute(text("SELECT count(*) FROM automation_settings"))
            ).scalar_one()
            through_definer = (
                await db.execute(text("SELECT count(*) FROM due_automations()"))
            ).scalar_one()

        assert unscoped == 0, "RLS hides it from an unscoped session, silently"
        assert through_definer >= 1, "the definer is how the scheduler can see it at all"
    finally:
        await _clear(business_a)


# --------------------------------------------------------------------------- #
# what is and is not due
# --------------------------------------------------------------------------- #


async def test_a_due_automation_starts_a_run_with_its_own_channels(
    scoped_sessions: None, business_a: UUID
) -> None:
    await _automation(
        business_a,
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        channels='["linkedin"]',
    )
    submit = _Submit()
    try:
        report = await tick(submit=submit)

        assert business_a in {call[1] for call in submit.calls}
        assert len(report.started) >= 1
        goal = next(call[2] for call in submit.calls if call[1] == business_a)
        assert goal == "more emergency callouts"
    finally:
        await _clear(business_a)


async def test_an_automation_that_is_not_yet_due_is_left_alone(
    scoped_sessions: None, business_a: UUID
) -> None:
    await _automation(business_a, next_run_at=datetime.now(UTC) + timedelta(days=3))
    submit = _Submit()
    try:
        await tick(submit=submit)

        assert business_a not in {call[1] for call in submit.calls}
    finally:
        await _clear(business_a)


async def test_mode_off_is_never_started(scoped_sessions: None, business_a: UUID) -> None:
    """The switch has to actually switch it off, even with a stale due date behind it."""
    await _automation(business_a, mode="off", next_run_at=datetime.now(UTC) - timedelta(days=1))
    submit = _Submit()
    try:
        await tick(submit=submit)

        assert business_a not in {call[1] for call in submit.calls}
    finally:
        await _clear(business_a)


async def test_a_paused_automation_is_skipped(scoped_sessions: None, business_a: UUID) -> None:
    """`paused_reason` is set when something is wrong — a lapsed credential, a spend
    ceiling. Running anyway would be the scheduler overruling the reason it was paused."""
    await _automation(
        business_a,
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        paused_reason="monthly ceiling reached",
    )
    submit = _Submit()
    try:
        await tick(submit=submit)

        assert business_a not in {call[1] for call in submit.calls}
    finally:
        await _clear(business_a)


async def test_a_null_next_run_at_is_not_due(scoped_sessions: None, business_a: UUID) -> None:
    """ "Never scheduled" is not "overdue since the epoch"."""
    await _automation(business_a, next_run_at=None)
    submit = _Submit()
    try:
        await tick(submit=submit)

        assert business_a not in {call[1] for call in submit.calls}
    finally:
        await _clear(business_a)


# --------------------------------------------------------------------------- #
# claiming the slot
# --------------------------------------------------------------------------- #


async def test_the_slot_is_advanced_so_the_next_tick_does_not_repeat_it(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Claimed BEFORE the run starts. The alternative — advance on success — turns any
    reproducible failure into an infinite loop spending the business's model budget on
    the same broken run."""
    was = datetime.now(UTC) - timedelta(minutes=1)
    await _automation(business_a, next_run_at=was)
    submit = _Submit()
    try:
        await tick(submit=submit)
        after_first = await _settings(business_a)
        assert after_first.next_run_at > was
        assert after_first.last_run_at is not None

        # A second pass immediately afterwards must find nothing.
        second = _Submit()
        await tick(submit=second)
        assert business_a not in {call[1] for call in second.calls}
    finally:
        await _clear(business_a)


async def test_two_schedulers_cannot_both_claim_the_same_slot(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The conditional claim, which is what replaces a lock and a queue.

    Both ticks read the same due row; the `WHERE next_run_at = :seen` means the second
    UPDATE matches nothing, so exactly one run is started rather than two posts being
    written for one slot.
    """
    import asyncio

    await _automation(business_a, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    first, second = _Submit(), _Submit()
    try:
        await asyncio.gather(tick(submit=first), tick(submit=second))

        started = [c for c in (*first.calls, *second.calls) if c[1] == business_a]
        assert len(started) == 1, f"one slot, one run; got {len(started)}"
    finally:
        await _clear(business_a)


async def test_another_business_is_untouched(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """The definer names which businesses are due; every WRITE still goes through a
    tenant-scoped session, so B's row cannot be advanced by A's tick."""
    await _automation(business_a, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    await _automation(business_b, next_run_at=datetime.now(UTC) + timedelta(days=30))
    before = await _settings(business_b)
    try:
        await tick(submit=_Submit())

        assert (await _settings(business_b)).next_run_at == before.next_run_at
    finally:
        await _clear(business_a)
        await _clear(business_b)


# --------------------------------------------------------------------------- #
# the stranded-run sweep
# --------------------------------------------------------------------------- #


async def _run_row(business_id: UUID, *, state: str, age: timedelta) -> UUID:
    run_id = uuid4()
    # The instant is computed in Python rather than as `now() - :age`: a bound
    # `timedelta` reaches Postgres as an interval and the column is a timestamptz, so
    # the server refuses the subtraction. Binding the timestamp itself is also the more
    # honest fixture — the test controls the clock rather than the database doing so.
    stamped = datetime.now(UTC) - age
    async with business_session(business_id) as db:
        await db.execute(
            text(
                """
                INSERT INTO runs (id, business_id, goal, state, created_at, updated_at)
                VALUES (:i, :b, 'stranded', :state, :stamped, :stamped)
                """
            ),
            {"i": run_id, "b": business_id, "state": state, "stamped": stamped},
        )
    return run_id


async def _state_of(business_id: UUID, run_id: UUID) -> Any:
    async with business_session(business_id) as db:
        return (
            await db.execute(
                text("SELECT state, finished_reason FROM runs WHERE id = :i"), {"i": run_id}
            )
        ).one()


async def test_a_run_left_running_by_a_dead_process_is_closed_with_a_reason(
    scoped_sessions: None, business_a: UUID
) -> None:
    """BACKLOG section D deferred this with the reason "that is a worker's job". Without
    it a crashed run stays `running` forever: live in the list, timeline frozen, and
    indistinguishable from a run that is merely slow."""
    run_id = await _run_row(business_a, state="running", age=STRANDED_AFTER + timedelta(minutes=5))

    report = await tick(submit=_Submit())

    assert report.stranded_marked >= 1
    row = await _state_of(business_a, run_id)
    assert row.state == "failed"
    assert row.finished_reason is not None, "a failure nobody recorded is a support ticket"


async def test_a_recent_running_run_is_left_alone(scoped_sessions: None, business_a: UUID) -> None:
    """The bound is generous on purpose: a real run makes several model calls, and a
    tight sweep would mark healthy runs failed while they were still working."""
    run_id = await _run_row(business_a, state="running", age=timedelta(minutes=3))

    await tick(submit=_Submit())

    assert (await _state_of(business_a, run_id)).state == "running"


async def test_a_finished_run_is_never_reopened_by_the_sweep(
    scoped_sessions: None, business_a: UUID
) -> None:
    run_id = await _run_row(business_a, state="done", age=timedelta(days=9))

    await tick(submit=_Submit())

    assert (await _state_of(business_a, run_id)).state == "done"


# --------------------------------------------------------------------------- #
# the monthly ceiling
# --------------------------------------------------------------------------- #
#
# The bug these exist for: the per-business USD ceiling was enforced in `api/runs.py`,
# and this worker started runs through `RunService.start` directly — so an automation
# spent past a ceiling a human pressing the same button was refused at. A test asserting
# only "an over-ceiling business does not start" would have passed on the broken code
# whenever the ledger happened to be empty, which is why each of these seeds real spend.


async def _spend(business_id: UUID, usd: str) -> None:
    """A real `model_usage` row, because the guard reads the ledger and not a flag."""
    async with business_session(business_id) as db:
        await db.execute(
            text(
                """
                INSERT INTO model_usage
                    (id, business_id, provider, model, tokens_in, tokens_out, usd,
                     latency_ms, created_at)
                VALUES (:i, :b, 'openrouter', 'openai/gpt-4.1-mini', 100, 200, :usd,
                        250, now())
                """
            ),
            {"i": uuid4(), "b": business_id, "usd": Decimal(usd)},
        )


async def _reason(business_id: UUID) -> str | None:
    async with business_session(business_id) as db:
        reason = (
            await db.execute(
                text("SELECT paused_reason FROM automation_settings WHERE business_id = :b"),
                {"b": business_id},
            )
        ).scalar_one()
    return None if reason is None else str(reason)


async def test_an_over_ceiling_automation_is_paused_instead_of_started(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The whole point of N2: the worker asks the same question the API asks.

    Four things are asserted together because each alone would pass on a broken fix: no
    run submitted, the pause recorded, the REASON stating both figures, and the slot NOT
    claimed — a claimed-then-refused slot would silently skip a cycle the owner never got.
    """
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    await _automation(business_a, next_run_at=due_at)
    await _spend(business_a, "40.00")
    submit = _Submit()
    try:
        report = await tick(submit=submit)

        assert submit.calls == [], "a run started past the ceiling is money we refused a human"
        assert report.paused == [business_a]
        assert report.started == []

        reason = await _reason(business_a)
        assert reason is not None
        assert "40.00" in reason, "the owner needs the figures, not 'over budget'"
        assert "25.00" in reason
        assert (await _settings(business_a)).next_run_at == due_at, "the slot must survive"
    finally:
        await _clear(business_a)


async def test_a_paused_automation_leaves_the_workers_list_entirely(
    scoped_sessions: None, business_a: UUID
) -> None:
    """`due_automations()` requires `paused_reason IS NULL`, so the pause is what stops
    the next tick re-reading the same row and re-pausing it forever."""
    await _automation(business_a, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    await _spend(business_a, "40.00")
    try:
        await tick(submit=_Submit())

        async with session() as db:
            still_due = (
                await db.execute(
                    text("SELECT count(*) FROM due_automations() WHERE business_id = :b"),
                    {"b": business_a},
                )
            ).scalar_one()
        assert still_due == 0

        second = await tick(submit=_Submit())
        assert second.paused == [], "a paused automation is not re-paused every minute"
    finally:
        await _clear(business_a)


async def test_spend_under_the_ceiling_still_starts_the_run(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The direction of the guard, pinned. Without this, an inverted comparison — or a
    guard that fired on any ledger row at all — would pass every test above."""
    await _automation(business_a, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    await _spend(business_a, "1.50")
    submit = _Submit()
    try:
        report = await tick(submit=submit)

        assert report.paused == []
        assert [call[1] for call in submit.calls] == [business_a]
    finally:
        await _clear(business_a)


async def test_one_business_over_its_ceiling_does_not_pause_another(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """The ceiling is per business, and the tick must not treat one ledger as the fleet's."""
    await _automation(business_a, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    await _automation(business_b, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    await _spend(business_a, "40.00")
    submit = _Submit()
    try:
        report = await tick(submit=submit)

        assert report.paused == [business_a]
        assert [call[1] for call in submit.calls] == [business_b]
        assert await _reason(business_b) is None
    finally:
        await _clear(business_a)
        await _clear(business_b)


async def test_the_kill_switch_pauses_automation_too(
    scoped_sessions: None, business_a: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`BUSINESS_MONTHLY_CAP_USD=0` is documented as stopping all model spend for every
    business. It has to stop the spend nobody is watching as well as the spend somebody
    just clicked — otherwise the switch says "off" and the scheduler keeps going.

    Patched on `cost_service`, which is where the ceiling is read now that two callers
    enforce it.
    """
    zero = get_settings().model_copy(update={"business_monthly_cap_usd": Decimal("0")})
    monkeypatch.setattr(cost_service, "get_settings", lambda: zero)
    await _automation(business_a, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    submit = _Submit()
    try:
        report = await tick(submit=submit)

        assert submit.calls == []
        assert report.paused == [business_a]
    finally:
        await _clear(business_a)
