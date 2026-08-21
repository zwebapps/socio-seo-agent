"""Next-run arithmetic. No database, no clock -- the instant is always an argument.

The tests worth reading are the ones that encode a decision rather than a signature:

* **The daylight-saving pair.** A schedule of "every Tuesday at 09:00 Europe/Berlin"
  must still be 09:00 local in January, and the naive implementation
  (``previous_utc + timedelta(days=7)``) passes every other test in this file while being
  an hour wrong for five months of the year. Two tests pin the UTC instants either side
  of a transition, because that is the only way the difference is visible.
* **The two transition-day edges.** A local hour that does not exist (spring forward) and
  one that happens twice (autumn back) must each produce exactly one run: a scheduler
  that skips is a silent outage, one that fires twice publishes twice.
* **Strictly after.** Called with ``after = last_run_at``, which is precisely what a
  scheduler does when a run finishes, an inclusive comparison returns the slot that just
  ran and the run repeats forever.
* **The refusals.** An unknown timezone and a naive ``after`` both raise rather than
  defaulting, because both of those defaults produce a confident instant on the wrong
  clock and nothing downstream can tell.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.app.db.models import Cadence
from backend.app.services.automation_service import (
    CADENCE_STRIDE_DAYS,
    compute_next_run,
    resolve_zone,
)


def _repo_root() -> Path:
    """The repo root, found by walking up to `pyproject.toml`.

    Same helper and same reason as `tests/test_engine_boundary.py`: counting `parents[n]`
    breaks silently when a file moves, and a cwd-relative path breaks depending on where
    pytest was invoked from.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate the repo root (no pyproject.toml above this file)")


BERLIN = ZoneInfo("Europe/Berlin")

#: Monday=0 .. Sunday=6, matching `date.weekday()`.
TUESDAY = 1
SUNDAY = 6


def _berlin(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A Berlin wall-clock instant, as UTC. Reads as the calendar a business thinks in."""
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


def _weekly(after: datetime) -> datetime:
    """The default schedule under test: Tuesdays at 09:00, Europe/Berlin."""
    return compute_next_run(
        cadence=Cadence.WEEKLY,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=after,
    )


# --------------------------------------------------------------------------- #
# weekly
# --------------------------------------------------------------------------- #


def test_weekly_finds_the_next_configured_weekday() -> None:
    # 2026-08-21 is a Friday; the next Tuesday is the 25th.
    assert _weekly(_berlin(2026, 8, 21, 12)) == _berlin(2026, 8, 25, 9)


def test_weekly_on_the_configured_day_before_the_hour_runs_today() -> None:
    """08:00 on a Tuesday: today's 09:00 slot has not passed, so it is the answer."""
    assert _weekly(_berlin(2026, 8, 25, 8)) == _berlin(2026, 8, 25, 9)


def test_weekly_on_the_configured_day_after_the_hour_waits_a_week() -> None:
    assert _weekly(_berlin(2026, 8, 25, 10)) == _berlin(2026, 9, 1, 9)


def test_weekly_at_exactly_the_slot_moves_on_rather_than_returning_it() -> None:
    """The scheduler's own call: it finishes a run and asks what is next.

    An inclusive comparison here returns the instant that just ran, and the run repeats
    immediately and forever. Nothing about the returned value would look wrong.
    """
    slot = _berlin(2026, 8, 25, 9)

    assert _weekly(slot) == _berlin(2026, 9, 1, 9)


# --------------------------------------------------------------------------- #
# Daylight saving -- the reason this module does date arithmetic in local time
# --------------------------------------------------------------------------- #


def test_the_local_hour_survives_the_autumn_transition() -> None:
    """09:00 Berlin before the change and 09:00 Berlin after it are 60 minutes apart in
    UTC, and the schedule must follow the wall clock rather than the offset.

    Europe/Berlin leaves CEST (+02:00) for CET (+01:00) on 2026-10-25. A UTC-stride
    implementation returns 07:00 UTC for the following Tuesday -- 08:00 Berlin -- which is
    an hour early every week until spring and is visible nowhere except a calendar.
    """
    # From the Monday, so `before` is the Tuesday still on CEST and `after` is the first
    # Tuesday on CET.
    before = _weekly(_berlin(2026, 10, 19, 12))
    after = _weekly(before)

    assert before == datetime(2026, 10, 20, 7, tzinfo=UTC), "09:00 CEST is 07:00 UTC"
    assert after == datetime(2026, 10, 27, 8, tzinfo=UTC), "09:00 CET is 08:00 UTC"
    assert after - before == timedelta(days=7, hours=1), "a UTC stride would give exactly 7 days"
    assert after.astimezone(BERLIN).hour == 9, "the wall clock is what the owner configured"


def test_the_local_hour_survives_the_spring_transition() -> None:
    """The same property in the other direction: CET (+01:00) to CEST (+02:00)."""
    before = _weekly(_berlin(2026, 3, 23, 12))
    after = _weekly(before)

    assert before == datetime(2026, 3, 24, 8, tzinfo=UTC)
    assert after == datetime(2026, 3, 31, 7, tzinfo=UTC)
    assert after - before == timedelta(days=7) - timedelta(hours=1)
    assert after.astimezone(BERLIN).hour == 9


def test_an_hour_that_does_not_exist_still_produces_exactly_one_run() -> None:
    """Europe/Berlin has no 02:30 on 2026-03-29: the clocks go 02:00 -> 03:00.

    A schedule of Sunday 02:00 must not vanish on that date. PEP 495 ``fold=0`` resolves
    the gap with the pre-transition offset, so the run lands at 03:00 local -- once, on
    the right date, an hour late, which is the mildest of the available wrong answers.
    Skipping it would be a silent outage on one Sunday a year.
    """
    # Saturday the 28th, so the next Sunday 02:00 slot is the transition itself.
    result = compute_next_run(
        cadence=Cadence.WEEKLY,
        day_of_week=SUNDAY,
        hour=2,
        timezone="Europe/Berlin",
        after=_berlin(2026, 3, 28, 12),
    )

    assert result == datetime(2026, 3, 29, 1, tzinfo=UTC)
    assert result.astimezone(BERLIN).hour == 3, "the non-existent 02:00 resolves forward to 03:00"


def test_an_hour_that_happens_twice_produces_exactly_one_run() -> None:
    """2026-10-25 has two Berlin 02:30s. ``fold=0`` picks the first, deterministically.

    The failure this pins is the duplicate: resolving both occurrences, or resolving
    ambiguously per machine, publishes the same content twice on one Sunday a year.
    """
    result = compute_next_run(
        cadence=Cadence.WEEKLY,
        day_of_week=SUNDAY,
        hour=2,
        timezone="Europe/Berlin",
        after=_berlin(2026, 10, 24, 12),
    )

    assert result == datetime(2026, 10, 25, 0, tzinfo=UTC), "the first 02:00, at +02:00"
    assert result.astimezone(BERLIN).fold == 0


# --------------------------------------------------------------------------- #
# biweekly -- the only cadence with an anchor
# --------------------------------------------------------------------------- #


def test_biweekly_without_a_previous_run_behaves_like_weekly() -> None:
    """Turning automation on should not mean waiting a fortnight for the first run."""
    first = compute_next_run(
        cadence=Cadence.BIWEEKLY,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 8, 21, 12),
    )

    assert first == _berlin(2026, 8, 25, 9)


def test_biweekly_keeps_the_parity_of_the_previous_run() -> None:
    last = _berlin(2026, 8, 25, 9)

    following = compute_next_run(
        cadence=Cadence.BIWEEKLY,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 8, 26, 9),
        last_run_at=last,
    )

    assert following == _berlin(2026, 9, 8, 9), "14 days on from the anchor, not 7"


def test_biweekly_after_a_long_pause_lands_on_the_anchors_parity() -> None:
    """A row untouched for months must not take one loop iteration per fortnight, and
    must land on a date the original anchor would have reached."""
    last = _berlin(2026, 1, 6, 9)  # a Tuesday

    result = compute_next_run(
        cadence=Cadence.BIWEEKLY,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 8, 21, 12),
        last_run_at=last,
    )

    assert result.astimezone(BERLIN).hour == 9
    elapsed_days = (result.astimezone(BERLIN).date() - last.astimezone(BERLIN).date()).days
    assert elapsed_days % 14 == 0, f"drifted off the fortnightly parity by {elapsed_days % 14} days"
    assert result > _berlin(2026, 8, 21, 12)


def test_biweekly_moves_to_the_new_weekday_when_the_owner_changes_it() -> None:
    """The anchor is snapped forward onto the configured weekday.

    Without the snap the stride carries the OLD weekday forward indefinitely, so an owner
    who switches from Tuesday to Thursday keeps getting Tuesdays and the settings screen
    shows Thursday.
    """
    last = _berlin(2026, 8, 25, 9)  # Tuesday
    thursday = 3

    result = compute_next_run(
        cadence=Cadence.BIWEEKLY,
        day_of_week=thursday,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 8, 26, 9),
        last_run_at=last,
    )

    assert result.astimezone(BERLIN).weekday() == thursday


# --------------------------------------------------------------------------- #
# monthly
# --------------------------------------------------------------------------- #


def test_monthly_is_the_first_configured_weekday_of_the_month() -> None:
    """2026-09-01 is a Tuesday, so September's slot is the 1st."""
    result = compute_next_run(
        cadence=Cadence.MONTHLY,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 8, 21, 12),
    )

    assert result == _berlin(2026, 9, 1, 9)


def test_monthly_rolls_to_next_month_once_this_months_slot_has_passed() -> None:
    result = compute_next_run(
        cadence=Cadence.MONTHLY,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 9, 2, 9),
    )

    assert result == _berlin(2026, 10, 6, 9), "the first Tuesday of October"


def test_monthly_crosses_the_year_boundary() -> None:
    """December to January is the one month increment that also changes the year."""
    result = compute_next_run(
        cadence=Cadence.MONTHLY,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 12, 2, 9),
    )

    assert result == _berlin(2027, 1, 5, 9), "the first Tuesday of January 2027"


# --------------------------------------------------------------------------- #
# Every cadence is schedulable, and refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cadence", list(Cadence))
def test_every_cadence_the_database_accepts_produces_a_future_instant(cadence: Cadence) -> None:
    """Guard the guard: a cadence added to the enum (and so to the CHECK constraint)
    without a rule here would be a settings row that silently never schedules."""
    after = _berlin(2026, 8, 21, 12)

    result = compute_next_run(
        cadence=cadence,
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=after,
    )

    assert result > after
    assert result.tzinfo is UTC
    assert result.astimezone(BERLIN).weekday() == TUESDAY


def test_a_cadence_string_from_the_database_is_accepted() -> None:
    """A row read out of Postgres is a `str`; converting in every caller instead of once
    here is how one caller ends up not converting."""
    assert _weekly(_berlin(2026, 8, 21, 12)) == compute_next_run(
        cadence="weekly",
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=_berlin(2026, 8, 21, 12),
    )


def test_an_unknown_cadence_raises_rather_than_defaulting() -> None:
    with pytest.raises(ValueError, match="unknown cadence"):
        compute_next_run(
            cadence="fortnightly",
            day_of_week=TUESDAY,
            hour=9,
            timezone="Europe/Berlin",
            after=_berlin(2026, 8, 21, 12),
        )


def test_an_unknown_timezone_raises_rather_than_falling_back_to_utc() -> None:
    """Silently scheduling a Berlin business in UTC publishes an hour or two early all
    year, and no screen would say so."""
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        compute_next_run(
            cadence=Cadence.WEEKLY,
            day_of_week=TUESDAY,
            hour=9,
            timezone="Europe/Atlantis",
            after=_berlin(2026, 8, 21, 12),
        )


def test_a_utc_offset_string_is_not_a_timezone() -> None:
    """The plausible-looking wrong value. ``+02:00`` cannot express a DST change, which is
    the entire reason the column stores an IANA key."""
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        resolve_zone("+02:00")


def test_a_naive_after_raises() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_next_run(
            cadence=Cadence.WEEKLY,
            day_of_week=TUESDAY,
            hour=9,
            timezone="Europe/Berlin",
            after=datetime(2026, 8, 21, 12),  # the naive instant IS the failure under test
        )


@pytest.mark.parametrize("day_of_week", [-1, 7, 8])
def test_an_out_of_range_day_of_week_raises(day_of_week: int) -> None:
    """7 is the ISO-vs-Python off-by-one. Unchecked it is not an error -- ``(7 - weekday)
    % 7`` is a valid rotation -- so the function would return a confident wrong day."""
    with pytest.raises(ValueError, match="day_of_week"):
        compute_next_run(
            cadence=Cadence.WEEKLY,
            day_of_week=day_of_week,
            hour=9,
            timezone="Europe/Berlin",
            after=_berlin(2026, 8, 21, 12),
        )


@pytest.mark.parametrize("hour", [-1, 24])
def test_an_out_of_range_hour_raises(hour: int) -> None:
    with pytest.raises(ValueError, match="hour"):
        compute_next_run(
            cadence=Cadence.WEEKLY,
            day_of_week=TUESDAY,
            hour=hour,
            timezone="Europe/Berlin",
            after=_berlin(2026, 8, 21, 12),
        )


def test_the_stride_table_covers_every_non_monthly_cadence() -> None:
    """`MONTHLY` is absent on purpose -- a month is not a number of days -- and every
    other member must be present or `compute_next_run` raises `KeyError` at runtime."""
    assert set(CADENCE_STRIDE_DAYS) == set(Cadence) - {Cadence.MONTHLY}


def test_the_module_touches_neither_a_database_nor_the_clock() -> None:
    """Purity is the property that makes every test above possible.

    A database would need Postgres for arithmetic. A clock read would make the two
    interesting cases in this file -- the daylight-saving Sundays -- reachable on two days
    a year and untestable on the other 363. `tests/test_engine_boundary.py` guards
    engines; nothing else guards a service, so it is guarded here.

    ``backend.app.db.models`` is permitted and nothing else under ``backend.app.db`` is:
    the models module is declarations, and importing ``Cadence`` from beside the CHECK
    constraint that admits it is what stops the two drifting. ``db.session`` is the line
    that would turn this into a database module.
    """
    # Anchored on THIS file, not on the working directory. `tests/test_engine_boundary.py`
    # already learned this the hard way and records it: a cwd-relative path here turns a
    # purity check into a `FileNotFoundError` the moment pytest is invoked from `backend/`,
    # and an ERROR reads as "the test is broken" rather than "the module is impure".
    source = (_repo_root() / "backend/app/services/automation_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = ("sqlalchemy", "asyncpg", "psycopg", "alembic", "backend.app.db.session")
    assert not [
        name for name in imported if any(name == f or name.startswith(f"{f}.") for f in forbidden)
    ]
    assert imported <= {"__future__", "datetime", "typing", "zoneinfo", "backend.app.db.models"}, (
        f"an unexpected dependency appeared: {sorted(imported)}"
    )

    # A clock read, found as a call rather than as text, so the module docstring is free
    # to name the thing it refuses to do.
    clock_reads = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "utcnow", "today"}
    ]
    assert not clock_reads, f"the scheduler reads the clock: {clock_reads}"


def test_a_naive_last_run_at_is_refused_like_a_naive_after() -> None:
    """The hole the sibling guard left open.

    `after` was hard-refused when naive, with a strong rationale. `last_run_at` then
    reached `astimezone()` with no check, where Python reads a naive value as HOST LOCAL
    TIME — so the fortnightly parity flipped by a week depending on the machine's
    timezone, and disagreed with itself across machines. Demonstrated before the fix:
    the same arguments gave 2026-09-01 under UTC and 2026-09-08 under Pacific/Auckland.
    """
    with pytest.raises(ValueError, match="last_run_at"):
        compute_next_run(
            cadence="biweekly",
            day_of_week=1,
            hour=9,
            timezone="Europe/Berlin",
            after=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
            # Naive: no zone to be in.
            last_run_at=datetime(2026, 8, 25, 23, 0),
        )


def test_an_aware_last_run_at_in_any_zone_gives_the_same_answer() -> None:
    """The property the guard protects: the same instant expressed in two zones is one
    instant, so it must produce one schedule.

    Arguments are repeated rather than shared through a dict, because a `**kwargs` dict
    widens to `dict[str, object]` and `mypy --strict` rejects the unpack — the duplication
    is what keeps the call site type-checked.
    """
    after = datetime(2026, 8, 26, 9, 0, tzinfo=BERLIN)

    as_utc = compute_next_run(
        cadence="biweekly",
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=after,
        last_run_at=datetime(2026, 8, 25, 21, 0, tzinfo=UTC),
    )
    as_berlin = compute_next_run(
        cadence="biweekly",
        day_of_week=TUESDAY,
        hour=9,
        timezone="Europe/Berlin",
        after=after,
        # 23:00 Berlin IS 21:00 UTC on that date. One instant, two spellings.
        last_run_at=datetime(2026, 8, 25, 23, 0, tzinfo=BERLIN),
    )

    assert as_utc == as_berlin
