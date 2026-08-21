"""The automation row an owner writes, against real SQL.

Every test here targets an outcome that would look like working automation. A row saved
with the switch off but a live `next_run_at` is a business that publishes after being
told not to. A `next_run_at` carried forward from the previous cadence keeps publishing
on the schedule the owner just replaced. And a settings write that touches `last_run_at`
moves a fortnightly schedule by two weeks as a side effect of fixing a typo.

They run as `sma_app`, the restricted role, because that is the only role row-level
security applies to — the owner is a superuser locally and would pass these assertions
against a database with no policies at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from backend.app.db.models import AutomationMode, Cadence
from backend.app.db.session import business_session
from backend.app.services import automation_settings_service as svc

pytestmark = pytest.mark.db

#: A Wednesday, 12:00 UTC. Fixed rather than `now()`: the whole point of the injected
#: clock is that the interesting cases are specific days.
WEDNESDAY = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


async def _save(business_id: UUID, **kwargs: object) -> svc.AutomationRecord:
    async with business_session(business_id) as session:
        return await svc.save_automation(business_id, session=session, now=WEDNESDAY, **kwargs)  # type: ignore[arg-type]


async def _load(business_id: UUID) -> svc.AutomationRecord:
    async with business_session(business_id) as session:
        return await svc.load_automation(business_id, session=session)


# --------------------------------------------------------------------------- #
# Reading a business that has never configured anything
# --------------------------------------------------------------------------- #


async def test_an_unconfigured_business_reads_defaults_and_says_they_are_defaults(
    scoped_sessions: None, business_a: UUID
) -> None:
    """`configured=False` is what stops a panel showing a schedule nobody chose."""
    record = await _load(business_a)

    assert record.configured is False
    assert record.enabled is False
    assert record.mode == AutomationMode.OFF
    assert record.cadence == Cadence.WEEKLY
    assert record.day_of_week == svc.DEFAULT_DAY_OF_WEEK
    assert record.hour == svc.DEFAULT_HOUR
    assert record.next_run_at is None


async def test_reading_does_not_create_the_row(scoped_sessions: None, business_a: UUID) -> None:
    """A row materialised by opening a settings page is a decision nobody made."""
    await _load(business_a)

    async with business_session(business_a) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM automation_settings WHERE business_id = :b"),
                {"b": business_a},
            )
        ).scalar_one()
    assert count == 0


# --------------------------------------------------------------------------- #
# Turning it on
# --------------------------------------------------------------------------- #


async def test_enabling_computes_the_next_slot_and_the_scheduler_can_see_it(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The end-to-end property: a saved automation appears in `due_automations()`.

    Asserted through the definer function the worker actually calls, not through a
    SELECT, because the row being present is not the same as the row being DUE — and
    the gap between those two is where an automation that "is on" never runs.
    """
    saved = await _save(business_a, enabled=True, day_of_week=3, hour=8, channels=["linkedin"])

    assert saved.enabled is True
    assert saved.configured is True
    assert saved.next_run_at is not None
    # Thursday 08:00 Europe/Berlin is 06:00 UTC, the day after WEDNESDAY.
    assert saved.next_run_at == datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
    assert saved.channels == ["linkedin"]

    # Backdate the slot rather than waiting for it: `due_automations()` reads `now()`
    # itself and takes no parameters, so a test controls it from the data.
    async with business_session(business_a) as session:
        await session.execute(
            text(
                "UPDATE automation_settings SET next_run_at = now() - interval '1 minute' "
                "WHERE business_id = :b"
            ),
            {"b": business_a},
        )
    async with business_session(business_a) as session:
        due = list((await session.execute(text("SELECT * FROM due_automations()"))).all())

    assert [row.business_id for row in due] == [business_a]


async def test_the_slot_is_recomputed_from_now_not_carried_forward(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Changing the day must move the next run, or the old schedule outlives the edit."""
    first = await _save(business_a, enabled=True, day_of_week=3, hour=8)
    second = await _save(business_a, enabled=True, day_of_week=4, hour=8)

    assert first.next_run_at != second.next_run_at
    assert second.next_run_at == datetime(2026, 8, 21, 6, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Turning it off
# --------------------------------------------------------------------------- #


async def test_disabling_clears_the_slot_as_well_as_the_mode(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Two independent reasons the scheduler cannot pick it up. See the module docstring."""
    await _save(business_a, enabled=True, day_of_week=3, hour=8)
    off = await _save(business_a, enabled=False, day_of_week=3, hour=8)

    assert off.enabled is False
    assert off.mode == AutomationMode.OFF
    assert off.next_run_at is None

    async with business_session(business_a) as session:
        due = list((await session.execute(text("SELECT * FROM due_automations()"))).all())
    assert due == []


async def test_disabling_keeps_the_schedule_so_turning_it_back_on_remembers(
    scoped_sessions: None, business_a: UUID
) -> None:
    """An owner pausing for August should not have to re-enter their cadence in September."""
    await _save(business_a, enabled=True, cadence="biweekly", day_of_week=3, hour=7)
    await _save(business_a, enabled=False, cadence="biweekly", day_of_week=3, hour=7)

    stored = await _load(business_a)
    assert stored.cadence == Cadence.BIWEEKLY
    assert stored.day_of_week == 3
    assert stored.hour == 7


# --------------------------------------------------------------------------- #
# The self-pause, and who may clear it
# --------------------------------------------------------------------------- #


async def test_enabling_clears_a_system_pause(scoped_sessions: None, business_a: UUID) -> None:
    """Otherwise a paused automation is invisible forever with no control to revive it."""
    await _save(business_a, enabled=True, day_of_week=3, hour=8)
    async with business_session(business_a) as session:
        await session.execute(
            text(
                "UPDATE automation_settings SET paused_reason = 'budget exhausted' "
                "WHERE business_id = :b"
            ),
            {"b": business_a},
        )
    assert (await _load(business_a)).enabled is False

    revived = await _save(business_a, enabled=True, day_of_week=3, hour=8)

    assert revived.paused_reason is None
    assert revived.enabled is True


async def test_saving_with_the_switch_off_does_not_clear_a_system_pause(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Editing the goal of a self-paused automation must not quietly restart it."""
    await _save(business_a, enabled=True, day_of_week=3, hour=8)
    async with business_session(business_a) as session:
        await session.execute(
            text(
                "UPDATE automation_settings SET paused_reason = 'budget exhausted' "
                "WHERE business_id = :b"
            ),
            {"b": business_a},
        )

    edited = await _save(
        business_a, enabled=False, day_of_week=3, hour=8, goal_template="more calls"
    )

    assert edited.paused_reason == "budget exhausted"
    assert edited.goal_template == "more calls"


# --------------------------------------------------------------------------- #
# What a settings write must never touch
# --------------------------------------------------------------------------- #


async def test_saving_never_touches_last_run_at(scoped_sessions: None, business_a: UUID) -> None:
    """It is the worker's record, and biweekly parity is computed from it."""
    await _save(business_a, enabled=True, cadence="biweekly", day_of_week=3, hour=8)
    ran_at = WEDNESDAY - timedelta(days=7)
    async with business_session(business_a) as session:
        await session.execute(
            text("UPDATE automation_settings SET last_run_at = :t WHERE business_id = :b"),
            {"t": ran_at, "b": business_a},
        )

    await _save(business_a, enabled=True, cadence="biweekly", day_of_week=3, hour=8)

    assert (await _load(business_a)).last_run_at == ran_at


async def test_a_second_save_updates_rather_than_duplicating(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Two open tabs are ordinary. `ON CONFLICT` is what keeps that from being a 500."""
    await _save(business_a, enabled=True, day_of_week=3, hour=8)
    await _save(business_a, enabled=True, day_of_week=3, hour=10)

    async with business_session(business_a) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM automation_settings WHERE business_id = :b"),
                {"b": business_a},
            )
        ).scalar_one()
    assert count == 1
    assert (await _load(business_a)).hour == 10


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("day_of_week", 7),  # the ISO-vs-Python off-by-one, arriving as data
        ("hour", 24),
        ("timezone", "Europe/Nowhere"),
        ("cadence", "daily"),
    ],
)
async def test_an_unstorable_schedule_is_refused_with_the_bound_it_broke(
    scoped_sessions: None, business_a: UUID, field: str, value: object
) -> None:
    with pytest.raises(svc.InvalidScheduleError):
        await _save(business_a, enabled=True, **{field: value})

    async with business_session(business_a) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM automation_settings WHERE business_id = :b"),
                {"b": business_a},
            )
        ).scalar_one()
    assert count == 0


async def test_an_invalid_schedule_is_refused_even_when_switching_off(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The fields are stored either way, so refusing later would blame the wrong day."""
    with pytest.raises(svc.InvalidScheduleError):
        await _save(business_a, enabled=False, day_of_week=9)


async def test_an_unknown_channel_is_refused_rather_than_dropped(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Same rule as starting a run: a silent drop reads as a success that produced nothing."""
    with pytest.raises(svc.InvalidScheduleError, match="threads"):
        await _save(business_a, enabled=True, channels=["linkedin", "threads"])


async def test_channels_are_canonicalised_and_deduplicated(
    scoped_sessions: None, business_a: UUID
) -> None:
    saved = await _save(
        business_a, enabled=True, channels=["Facebook_post", "facebook", "LinkedIn"]
    )

    assert saved.channels == ["facebook", "linkedin"]


async def test_a_blank_goal_stores_null_not_an_empty_string(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The scheduler already reads both as "nobody chose"; storing `''` looks like a choice."""
    saved = await _save(business_a, enabled=True, goal_template="   ")

    assert saved.goal_template is None


async def test_an_over_long_goal_is_refused_at_the_limit_a_run_accepts(
    scoped_sessions: None, business_a: UUID
) -> None:
    with pytest.raises(svc.InvalidScheduleError, match=str(svc.MAX_GOAL_LENGTH)):
        await _save(business_a, enabled=True, goal_template="x" * (svc.MAX_GOAL_LENGTH + 1))


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_one_business_cannot_read_or_overwrite_anothers_automation(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """RLS, exercised rather than asserted to exist. See `test_scheduling_schema.py`."""
    await _save(business_a, enabled=True, day_of_week=3, hour=8)

    seen_by_b = await _load(business_b)
    assert seen_by_b.configured is False

    await _save(business_b, enabled=True, day_of_week=5, hour=20)

    assert (await _load(business_a)).hour == 8
    assert (await _load(business_b)).hour == 20
