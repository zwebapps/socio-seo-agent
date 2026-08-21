"""When the scheduler should next run for a business, computed rather than asked.

`automation_settings` stores an intention -- "every other Tuesday at 09:00, Europe/Berlin"
-- and the scheduler needs a UTC instant. Turning one into the other is arithmetic, so it
is arithmetic here and not a query, a clock read, or (least of all) a model call: see
docs/ARCHITECTURE.md section 14, "if the answer is computable, compute it".

Everything in this module is pure. No database, no `datetime.now()`, no timezone read
from the process -- the caller passes the instant it wants the answer relative to. That is
what makes a daylight-saving boundary testable at all: the interesting cases are two
specific Sundays a year, and a function that read the clock could only be tested on those
two days.

**The three decisions worth defending.**

*The arithmetic happens on the local wall clock, and UTC conversion happens last.* The
tempting shortcut is `previous_utc + timedelta(days=7)`, and it is wrong twice a year:
"every Tuesday at 09:00" would become 08:00 for the whole of winter, because a fixed UTC
stride cannot know the offset moved. Adding days to the local *date* and re-resolving the
hour against the zone is what keeps 09:00 meaning 09:00.

*`biweekly` needs an anchor and the other two do not.* "Every other week" has no meaning
without a parity reference, so `last_run_at` supplies it; `weekly` and `monthly` are
stateless, which matters because a stateless rule cannot drift after a missed run. The
alternative -- ISO-week parity -- is stateless but produces a three-week gap at some year
ends, which is a schedule nobody asked for.

*`monthly` means the first configured weekday of the month, not "28 days".* The settings
row stores a day-of-week and no day-of-month, so a month cannot be expressed as a date;
and 28 days is not a month, it is four weeks wearing its name. "First Tuesday of the
month" is a real editorial cadence and it never drifts.

Invalid input raises. An unknown IANA key is not defaulted to UTC and a naive `after` is
not assumed to be UTC, because both of those failures publish content at the wrong time
while every screen shows a plausible schedule -- the class of bug this codebase treats as
worse than a crash.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.app.db.models import Cadence

__all__ = [
    "CADENCE_STRIDE_DAYS",
    "MAX_DAY_OF_WEEK",
    "MAX_HOUR",
    "compute_next_run",
    "resolve_zone",
]

#: Monday=0 .. Sunday=6, matching :meth:`datetime.date.weekday` and
#: ``automation_settings.day_of_week``. NOT ISO (Monday=1) and NOT Postgres ``EXTRACT(DOW)``
#: (Sunday=0); see the model docstring for why that distinction is called out everywhere.
MAX_DAY_OF_WEEK: Final = 6
MAX_HOUR: Final = 23

#: Whole weeks between runs, for the cadences that are a fixed stride. ``MONTHLY`` is
#: absent deliberately -- it is not a number of days, which is the point of the rule.
CADENCE_STRIDE_DAYS: Final[dict[Cadence, int]] = {
    Cadence.WEEKLY: 7,
    Cadence.BIWEEKLY: 14,
}


def resolve_zone(timezone: str) -> ZoneInfo:
    """The IANA zone named by ``timezone``, or ``ValueError``.

    Separated out so a settings form can validate a zone before storing it. Raising
    beats falling back to UTC: a business in Europe/Berlin silently scheduled in UTC
    publishes an hour or two early all year and nothing on any screen says so.
    """
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # `ValueError` too: ZoneInfo rejects keys containing `..` or a leading slash with
        # ValueError rather than ZoneInfoNotFoundError, and a caller should not have to
        # know which malformed key produces which exception type.
        raise ValueError(f"unknown IANA timezone: {timezone!r}") from exc


def compute_next_run(
    *,
    cadence: Cadence | str,
    day_of_week: int,
    hour: int,
    timezone: str,
    after: datetime,
    last_run_at: datetime | None = None,
) -> datetime:
    """The next scheduled instant strictly after ``after``, in UTC.

    ``after`` must be timezone-aware; it is normally "now", passed in by the caller.
    ``last_run_at`` is used only by :attr:`Cadence.BIWEEKLY`, which needs it for parity --
    with no previous run, the first biweekly run is simply the next matching weekday,
    which is also what a business turning automation on expects to see.

    Strictly after, never equal: called with ``after = last_run_at`` -- which is exactly
    what a scheduler does when it finishes a run -- an inclusive comparison would return
    the slot that just ran and the run would repeat immediately, forever.
    """
    resolved = _validate_cadence(cadence)
    _validate_field("day_of_week", day_of_week, 0, MAX_DAY_OF_WEEK)
    _validate_field("hour", hour, 0, MAX_HOUR)
    if after.tzinfo is None or after.utcoffset() is None:
        # Not defaulted to UTC: a naive instant read from somewhere that meant local time
        # would shift every computed slot by the offset, and the answer would still look
        # like a valid schedule.
        raise ValueError("`after` must be timezone-aware; a naive instant has no zone to be in")
    zone = resolve_zone(timezone)

    if resolved is Cadence.MONTHLY:
        return _next_monthly(day_of_week, hour, zone, after)

    return _next_by_stride(
        stride_days=CADENCE_STRIDE_DAYS[resolved],
        day_of_week=day_of_week,
        hour=hour,
        zone=zone,
        after=after,
        last_run_at=last_run_at,
    )


# --------------------------------------------------------------------------- #
# The two schedule rules
# --------------------------------------------------------------------------- #


def _next_by_stride(
    *,
    stride_days: int,
    day_of_week: int,
    hour: int,
    zone: ZoneInfo,
    after: datetime,
    last_run_at: datetime | None,
) -> datetime:
    """Fixed-stride cadences: weekly, and biweekly anchored on the previous run."""
    after_local_date = after.astimezone(zone).date()

    if last_run_at is not None and stride_days > 7:
        # Anchor on the previous run so the parity of "every other week" survives. The
        # anchor is snapped FORWARD onto the configured weekday, which is a no-op in the
        # ordinary case and is what stops an owner who changes `day_of_week` from getting
        # a run on the old day: without the snap the stride would carry the old weekday
        # forward for as long as automation stays on.
        anchor = _snap_to_weekday(last_run_at.astimezone(zone).date(), day_of_week)
    else:
        anchor = _snap_to_weekday(after_local_date, day_of_week)

    # Jump most of the way in one step. A business paused for a year would otherwise take
    # 26 loop iterations to catch up, and an unbounded `while` driven by a stored
    # timestamp is a hang waiting for one bad row.
    behind_days = (after_local_date - anchor).days
    if behind_days > 0:
        anchor += timedelta(days=(behind_days // stride_days) * stride_days)

    # At most two iterations after the jump: one for the remainder, one for the case where
    # the slot is today but the hour has already passed.
    candidate = _slot_utc(anchor, hour, zone)
    while candidate <= after:
        anchor += timedelta(days=stride_days)
        candidate = _slot_utc(anchor, hour, zone)
    return candidate


def _next_monthly(day_of_week: int, hour: int, zone: ZoneInfo, after: datetime) -> datetime:
    """The first configured weekday of a month, at ``hour`` local."""
    local_date = after.astimezone(zone).date()
    year, month = local_date.year, local_date.month

    # Twice at most: this month's first matching weekday, else next month's.
    for _ in range(2):
        first_matching = _snap_to_weekday(date(year, month, 1), day_of_week)
        candidate = _slot_utc(first_matching, hour, zone)
        if candidate > after:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    raise AssertionError("unreachable: the first weekday of next month is always in the future")


# --------------------------------------------------------------------------- #
# Local-time helpers
# --------------------------------------------------------------------------- #


def _snap_to_weekday(day: date, day_of_week: int) -> date:
    """``day`` itself if it is already the wanted weekday, else the next one that is."""
    return day + timedelta(days=(day_of_week - day.weekday()) % 7)


def _slot_utc(day: date, hour: int, zone: ZoneInfo) -> datetime:
    """``day`` at ``hour`` in ``zone``, as a UTC instant.

    The two daylight-saving edges resolve by PEP 495's ``fold=0``, which is deliberate
    rather than merely default:

    * **Spring forward**, where the local hour does not exist (Europe/Berlin jumps 02:00
      to 03:00): the instant is resolved with the offset in force *before* the
      transition, so a 02:00 setting runs at 03:00 local that day. It runs once, on the
      correct date, an hour late -- which is the mildest of the available wrong answers.
    * **Autumn back**, where the local hour happens twice: the FIRST occurrence wins.
      Also once, and deterministically the same once on every machine.

    Neither case is skipped and neither is duplicated, which is the property that
    matters: a scheduler that misses a run is a silent outage, and one that fires twice
    publishes twice.
    """
    local = datetime(day.year, day.month, day.day, hour, tzinfo=zone)
    return local.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _validate_cadence(cadence: Cadence | str) -> Cadence:
    """Accept the enum or its stored string; refuse anything else.

    The string form exists because a row read straight out of Postgres is a `str`, and
    forcing every caller to convert would put a conversion in each of them instead of one
    here. An unrecognised value raises rather than defaulting to weekly: the database
    CHECK and this function are meant to admit the same set, and a mismatch is a bug in
    one of them that a fallback would hide.
    """
    try:
        return Cadence(cadence)
    except ValueError as exc:
        known = ", ".join(sorted(c.value for c in Cadence))
        raise ValueError(f"unknown cadence {cadence!r}; expected one of: {known}") from exc


def _validate_field(name: str, value: int, low: int, high: int) -> None:
    """Range check with the bound in the message.

    `day_of_week = 7` is the ISO-vs-Python off-by-one arriving as an argument. Unchecked
    it is not an error: `(7 - weekday) % 7` is a valid rotation, so the function would
    return a confident instant on the wrong day.
    """
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
