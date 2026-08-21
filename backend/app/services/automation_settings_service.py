"""The standing automation instruction, as a row an owner can read and write.

`services/automation_service` answers *when* the next run falls and is deliberately
pure — no database, no clock, no ambient state — because the interesting cases are two
specific Sundays a year. This module is the other half: it loads and stores the row that
arithmetic is applied to, and it is the ONLY writer of that row besides the scheduler's
own claim.

**Why this is a separate module rather than four more functions over there.** Purity is
load-bearing in `automation_service`: a daylight-saving test that had to reach Postgres
to assert a schedule would be a test nobody runs on the machine where it matters. Adding
a session parameter to that module would end that property for every function in it.

**`next_run_at` is never accepted from a caller. It is computed here, always.** The
column is documented as a CACHE of the arithmetic — it exists so the scheduler's due
query is an index scan rather than a recomputation across every business. A route that
let a client post a timestamp would make the cache authoritative for one write and the
function authoritative for every other, and the two would disagree the first time an
owner changed their cadence.

**Switching automation OFF clears `next_run_at`, and that redundancy is deliberate.**
`due_automations()` already filters on `mode = 'scheduled_draft'`, so clearing the
timestamp changes nothing about what the scheduler picks up today. It is a second,
independent reason the row cannot be selected — and the cheap kind: an owner who turns
automation off has asked for nothing to happen, which is exactly the promise that must
not rest on one predicate in one function.

**Switching automation ON clears `paused_reason`.** That column is how the system pauses
itself (budget exhausted, repeated failures) and it is distinct from `mode` precisely so
that a self-pause and an owner's choice are different events. But `due_automations()`
requires `paused_reason IS NULL`, so without a clear point a paused automation would stay
invisible forever with no control anywhere that could revive it. The owner deliberately
enabling their automation is that acknowledgement — and if the condition still holds, the
scheduler pauses it again and says so, which is the honest loop.

**`last_run_at` is never written here.** It is the worker's record of what happened, and
the fortnightly parity in `compute_next_run` reads it. A settings write that touched it
would move a schedule by a fortnight as a side effect of an owner fixing a typo in a goal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import AutomationMode, AutomationSetting, Cadence
from backend.app.engines.channel.specs import canonicalise_known
from backend.app.services.automation_service import compute_next_run, resolve_zone

__all__ = [
    "DEFAULT_CADENCE",
    "DEFAULT_DAY_OF_WEEK",
    "DEFAULT_HOUR",
    "DEFAULT_TIMEZONE",
    "MAX_GOAL_LENGTH",
    "AutomationRecord",
    "InvalidScheduleError",
    "load_automation",
    "pause_automation",
    "save_automation",
]

#: The values a business that has never configured automation is shown, and the ones a
#: first write applies for anything it leaves out. They MIRROR the column defaults in
#: `db/models.AutomationSetting` rather than inventing a second set: a GET that showed
#: Tuesday 09:00 followed by an INSERT that stored Monday 00:00 would be a screen lying
#: about a row it had just created.
DEFAULT_CADENCE: Final = Cadence.WEEKLY
#: Monday=0 .. Sunday=6. Tuesday, for the reason the model gives: Monday is the day a
#: small-business owner has the least attention to spare for a review queue.
DEFAULT_DAY_OF_WEEK: Final = 1
DEFAULT_HOUR: Final = 9
DEFAULT_TIMEZONE: Final = "Europe/Berlin"

#: The same ceiling `StartRunRequest.goal` carries, because this string becomes that one.
#: Bounded here as well as on the wire so a caller that reaches the service directly
#: cannot store a goal no run would accept.
MAX_GOAL_LENGTH: Final = 500


class InvalidScheduleError(ValueError):
    """A schedule that cannot be stored, with a message written for the owner.

    Wraps the refusals `automation_service` already makes — an unknown cadence, a
    day-of-week that is really an ISO weekday, an IANA key that does not resolve — so a
    route maps ONE exception type to 422 instead of guessing which `ValueError` came from
    where. The messages are kept verbatim: they name the bound they refused.
    """


@dataclass(frozen=True, slots=True)
class _Schedule:
    """The four schedule fields, once they are known to be storable.

    A typed holder rather than a `dict`, so the fields reach `compute_next_run` and the
    INSERT as the types they are. A `**kwargs` dict here typechecks as `object` and would
    have made this module the one place the schedule's shape was unverified.
    """

    cadence: str
    day_of_week: int
    hour: int
    timezone: str


@dataclass(frozen=True, slots=True)
class AutomationRecord:
    """One business's automation, as every reader sees it.

    `configured` is the field a screen most needs and the one a bare row cannot supply:
    it is False when no row exists, so the values alongside it are the defaults a first
    save would apply rather than a schedule somebody chose. Without it a panel would
    render "every Tuesday at 09:00" for a business that has never opened the page.
    """

    business_id: UUID
    mode: str
    cadence: str
    day_of_week: int
    hour: int
    timezone: str
    channels: list[str]
    goal_template: str | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    paused_reason: str | None
    configured: bool

    @property
    def enabled(self) -> bool:
        """Whether the scheduler will act on this row.

        Both halves, because either alone is a wrong answer to what an owner is asking:
        `mode` is their switch and `paused_reason` is the system's, and a screen that
        reported only the first would show "on" for an automation that has stopped.
        """
        return self.mode == AutomationMode.SCHEDULED_DRAFT and self.paused_reason is None


def _defaults(business_id: UUID) -> AutomationRecord:
    return AutomationRecord(
        business_id=business_id,
        mode=AutomationMode.OFF,
        cadence=DEFAULT_CADENCE,
        day_of_week=DEFAULT_DAY_OF_WEEK,
        hour=DEFAULT_HOUR,
        timezone=DEFAULT_TIMEZONE,
        channels=[],
        goal_template=None,
        next_run_at=None,
        last_run_at=None,
        paused_reason=None,
        configured=False,
    )


def _project(row: AutomationSetting) -> AutomationRecord:
    return AutomationRecord(
        business_id=row.business_id,
        mode=row.mode,
        cadence=row.cadence,
        day_of_week=row.day_of_week,
        hour=row.hour,
        timezone=row.timezone,
        channels=list(row.channels or []),
        goal_template=row.goal_template,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        paused_reason=row.paused_reason,
        configured=True,
    )


async def load_automation(business_id: UUID, *, session: AsyncSession) -> AutomationRecord:
    """This business's automation, or the defaults a first save would apply.

    Reads nothing and writes nothing when the row is absent. Creating it on read would
    be convenient and wrong: `automation_settings` is the scheduler's work list, and a
    row materialised by somebody opening a settings page is a business that appears in
    that list's table for a decision nobody made. `configured=False` carries the absence
    instead.
    """
    row = (
        await session.execute(
            select(AutomationSetting).where(AutomationSetting.business_id == business_id)
        )
    ).scalar_one_or_none()
    return _defaults(business_id) if row is None else _project(row)


async def save_automation(
    business_id: UUID,
    *,
    session: AsyncSession,
    enabled: bool,
    cadence: str = DEFAULT_CADENCE,
    day_of_week: int = DEFAULT_DAY_OF_WEEK,
    hour: int = DEFAULT_HOUR,
    timezone: str = DEFAULT_TIMEZONE,
    channels: list[str] | None = None,
    goal_template: str | None = None,
    now: datetime,
) -> AutomationRecord:
    """Store this business's automation and return what the scheduler will now see.

    `now` is injected rather than read, for the same reason `compute_next_run` takes
    `after`: a schedule computed from an ambient clock can only be tested on the days
    that make it interesting.

    **The schedule is validated even when automation is being switched off**, which looks
    like wasted work and is not: the fields are stored either way, so an owner who saves
    an invalid hour with the switch off would find their automation refusing to start
    later, at the moment they turned it on, with no hint of which field was to blame.

    One statement, `ON CONFLICT (business_id)`, because the row is unique per business
    and two open tabs are an ordinary event. Select-then-insert would race into an
    `IntegrityError` that reads to the owner as a server fault rather than as their own
    second save. `flush`, never `commit`: the caller's session owns the transaction, so a
    request refused after this point writes nothing.
    """
    schedule = _validated(cadence=cadence, day_of_week=day_of_week, hour=hour, timezone=timezone)
    known = _validated_channels(channels)
    goal = _validated_goal(goal_template)

    mode = AutomationMode.SCHEDULED_DRAFT if enabled else AutomationMode.OFF
    existing = await load_automation(business_id, session=session)

    # Computed from `now` and NOT from the previous `next_run_at`: an owner who changes
    # the cadence is telling us the old slot is wrong, so carrying it forward would keep
    # publishing on the schedule they just replaced. `last_run_at` supplies the
    # fortnightly parity and comes from the row, never from this call.
    next_run_at = (
        compute_next_run(
            cadence=schedule.cadence,
            day_of_week=schedule.day_of_week,
            hour=schedule.hour,
            timezone=schedule.timezone,
            after=now,
            last_run_at=existing.last_run_at,
        )
        if enabled
        else None
    )

    values: dict[str, object] = {
        "business_id": business_id,
        "mode": mode.value,
        "cadence": schedule.cadence,
        "day_of_week": schedule.day_of_week,
        "hour": schedule.hour,
        "timezone": schedule.timezone,
        "channels": known,
        "goal_template": goal,
        "next_run_at": next_run_at,
        # Cleared on an explicit enable, preserved otherwise: see the module docstring.
        # Preserved rather than always cleared, so editing the goal of a self-paused
        # automation does not quietly restart it.
        "paused_reason": None if enabled else existing.paused_reason,
    }
    statement = (
        insert(AutomationSetting)
        .values(id=uuid4(), **values)
        .on_conflict_do_update(
            index_elements=[AutomationSetting.business_id],
            set_={key: value for key, value in values.items() if key != "business_id"},
        )
        .returning(AutomationSetting)
    )
    row = (await session.execute(statement)).scalar_one()
    await session.flush()
    return _project(row)


async def pause_automation(
    business_id: UUID, *, session: AsyncSession, reason: str
) -> AutomationRecord:
    """Record that the SYSTEM stopped this automation, and why.

    The counterpart to an owner's switch, and the reason `paused_reason` is a separate
    column from `mode`: an owner turning automation off and the platform stopping it are
    different events, and only one of them has to be explained back. `due_automations()`
    requires `paused_reason IS NULL`, so writing one is what takes the row out of the
    worker's list — no mode change, so the owner's own setting is preserved and reading
    the panel still shows the schedule they chose.

    **`next_run_at` is deliberately left where it is.** Advancing it would claim a slot
    that was never used, and clearing it would lose the fact that a run was due. Nothing
    can act on a stale timestamp while `paused_reason` is set, and the only thing that
    clears that — an owner deliberately switching automation back on — recomputes the
    slot from the current time anyway.

    Idempotent by intent rather than by check: writing the same reason twice is the same
    row. A no-op guard would need to compare the text, and a paused automation being
    re-pausable with a FRESHER reason is the more useful behaviour (the figures in a
    budget message move).

    Returns the record so a caller can log or render what the owner will now see, rather
    than re-reading the row it just wrote.
    """
    row = (
        await session.execute(
            select(AutomationSetting).where(AutomationSetting.business_id == business_id)
        )
    ).scalar_one_or_none()
    if row is None:
        # Nothing to pause. Not an error: the automation may have been switched off, or
        # the row deleted, between the worker selecting it as due and this write — and
        # inventing a row to hold a pause reason would put a business into the
        # scheduler's table for a decision nobody made.
        return _defaults(business_id)
    row.paused_reason = reason
    await session.flush()
    return _project(row)


def _validated(*, cadence: str, day_of_week: int, hour: int, timezone: str) -> _Schedule:
    """The schedule fields, refused as one unit by the arithmetic that will use them.

    Validation is delegated to `compute_next_run`'s own guards rather than repeated here:
    a second copy of "day_of_week is 0..6" is a second thing to keep in step with the
    database CHECK, and the one that drifts is the one nobody is testing.
    """
    try:
        resolve_zone(timezone)
        compute_next_run(
            cadence=cadence,
            day_of_week=day_of_week,
            hour=hour,
            timezone=timezone,
            # A fixed, arbitrary aware instant: this call is a validation probe and its
            # RESULT is discarded. The real computation happens in `save_automation`
            # against the caller's `now`.
            after=datetime.fromtimestamp(0, tz=resolve_zone("UTC")),
        )
    except ValueError as exc:
        raise InvalidScheduleError(str(exc)) from exc
    return _Schedule(
        cadence=Cadence(cadence).value,
        day_of_week=day_of_week,
        hour=hour,
        timezone=timezone,
    )


def _validated_channels(channels: list[str] | None) -> list[str]:
    """Channels an automated run may target, or `InvalidScheduleError`.

    `None` and `[]` both store empty, which the scheduler reads as "nobody chose" and
    resolves to the default set — the same reading `runs.channels` has. Refusing an
    unknown name is `canonicalise_known`'s rule, shared with `POST /runs`, so a channel
    cannot be acceptable to start a run with and refused to schedule one with.
    """
    try:
        return canonicalise_known(channels or [])
    except ValueError as exc:
        raise InvalidScheduleError(str(exc)) from exc


def _validated_goal(goal_template: str | None) -> str | None:
    """The goal text, trimmed, or `None`.

    Blank collapses to `None` rather than to `""`, because the scheduler's fallback is
    `str(row.goal_template or "").strip() or DEFAULT_GOAL` — an empty string and a null
    already mean the same thing there, and storing the empty one would put a value in the
    column that reads like a choice.
    """
    if goal_template is None:
        return None
    trimmed = goal_template.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_GOAL_LENGTH:
        raise InvalidScheduleError(
            f"the goal is {len(trimmed)} characters; the limit is {MAX_GOAL_LENGTH}, "
            "which is what a run itself accepts"
        )
    return trimmed
