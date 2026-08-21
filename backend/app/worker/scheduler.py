"""The scheduler: what makes "autonomous" true rather than a setting.

`automation_settings` has held a cadence and a `next_run_at` since the scheduling
schema landed, and nothing ever read them — so an owner could switch automation on and
nothing would happen, which is the worst shape of missing feature: a control that
reports success and does nothing.

**No queue library, and that is a decision with a hard reason behind it.**
`docs/ROADMAP.md` names ARQ/Redis, and ARQ is genuinely the right *kind* of tool — but
it is uninstallable here: `arq>=0.27` requires `redis[hiredis]>=4.2,<6` and this project
pins `redis>=8.1.0`, so the resolver refuses. That left three options and only one is
defensible. Downgrading `redis` across the whole application to satisfy a scheduler is
the tail wagging the dog, on a dependency the rate limiter depends on. A second
virtualenv is what `evals/` had to do for Ragas, and it works there because the eval
arm is a separate process with a separate job; a worker needs THIS application's code,
so a second environment would mean two resolutions of the same app.

The third option is the one taken, and it is not a workaround so much as a smaller
design: **the database is already the queue.** Work is not pushed anywhere — it is
discovered by asking which automations are due, which is a `next_run_at` index scan. A
job queue would add a second place for the same fact to live, and the failure mode of
two sources of "when does this run" is a run that fires twice or not at all. Redis
stays where it earns its place: rate-limit token buckets.

Three properties worth naming, because each prevents a specific failure:

**The slot is claimed BEFORE the run starts.** `next_run_at` is advanced first, so a
crash mid-run costs that one cycle rather than repeating it forever. The alternative —
advance on success — turns any reproducible failure into an infinite loop that spends
the business's whole model budget on the same broken run.

**The claim is conditional on the value that was read.** Two schedulers running by
accident would both see the same due row; the `WHERE next_run_at = :seen` makes exactly
one of them win, without a lock and without a queue.

**One business's failure costs that business.** Every step is per-business and wrapped,
in the same posture HARVEST applies to a dead fact source: a tenant whose run cannot
start must not stop the tick that would have served the other forty.

**The monthly ceiling is checked here, before the slot is claimed.** It has to be: the
per-business USD cap was enforced in `api/runs.py`, so for as long as this worker called
`RunService.start` directly, an automation spent past a ceiling a human pressing the same
button was refused at. The decision is `cost_service.monthly_cap_state`, shared with
those routes; what differs is the consequence, because a scheduler has nobody to return a
409 to. So an over-ceiling automation is PAUSED with the figures stated, which is exactly
what `automation_settings.paused_reason` is for — the owner sees why on their own screen
instead of the refusal existing only in a log line nobody reads.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import CursorResult

from backend.app.agents.state import normalise_channels
from backend.app.db.session import business_session, session
from backend.app.services.automation_service import compute_next_run
from backend.app.services.automation_settings_service import pause_automation
from backend.app.services.cost_service import monthly_cap_state

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "STRANDED_AFTER",
    "TickReport",
    "run_forever",
    "tick",
]

#: How often the scheduler looks for work. A minute is far finer than any cadence this
#: product offers (the shortest is weekly), so the interval is not about precision — it
#: is about how long a due run waits after its slot, and a minute is invisible to
#: somebody expecting a post that day.
DEFAULT_INTERVAL_S: Final = 60.0

#: A run still `running` after this long is presumed dead. Deliberately generous: a real
#: run makes several model calls and a slow provider legitimately takes minutes, so a
#: tight bound would mark healthy runs failed and the resume path would fight the sweep.
STRANDED_AFTER: Final = timedelta(hours=2)

#: The goal used when an automation names none. A default rather than a refusal because
#: the alternative is an automation the owner switched on that silently never fires.
DEFAULT_GOAL: Final = "more local enquiries from the services we already offer"

#: Why a swept run says it failed. A sentence rather than a code, because it is rendered
#: verbatim on the runs list and the owner is the reader.
STRANDED_REASON: Final = (
    "no progress for two hours; the process running it is presumed gone. Resume to pick it up."
)


@dataclass(slots=True)
class TickReport:
    """What one pass did. Returned rather than only logged, so a test can assert it."""

    started: list[UUID] = field(default_factory=list)
    #: Businesses whose slot was claimed but whose run could not be started. Named
    #: separately from `started` because a scheduler reporting "42 due" while starting
    #: none is the failure this field exists to make visible.
    failed: list[UUID] = field(default_factory=list)
    #: Businesses whose automation was paused this pass because their monthly ceiling
    #: is used up. Reported rather than only logged for the same reason `failed` is: a
    #: tick that silently declined to run half its work is indistinguishable from a tick
    #: with no work to do.
    paused: list[UUID] = field(default_factory=list)
    stranded_marked: int = 0
    connections_expired: int = 0


async def _due() -> list[Any]:
    """The businesses whose automation is due.

    Through `due_automations()`, the SECURITY DEFINER function, because this is the one
    question no tenant-scoped session can answer: `automation_settings` has FORCE RLS
    keyed on `app.current_business_id`, so an ordinary session with that GUC unset reads
    ZERO rows and raises nothing. A plain SELECT here would find no work forever and log
    nothing wrong — green output and a scheduler that never runs.
    """
    async with session() as db:
        return list((await db.execute(text("SELECT * FROM due_automations()"))).all())


async def _claim(row: Any, *, now: datetime) -> datetime | None:
    """Advance this automation's `next_run_at`, and say whether we won it.

    Conditional on the value that was read, so two schedulers cannot both claim the same
    slot. Returns the new time on success and `None` when somebody else got there first
    — which is not an error and is not logged as one.

    The write is scoped by `business_session`, so RLS applies in the ordinary way: the
    definer function said WHICH businesses are due and nothing more.
    """
    following = compute_next_run(
        cadence=row.cadence,
        day_of_week=row.day_of_week,
        hour=row.hour,
        timezone=row.timezone,
        after=now,
        last_run_at=row.last_run_at,
    )
    async with business_session(row.business_id) as db:
        result = await db.execute(
            text(
                """
                UPDATE automation_settings
                   SET next_run_at = :following,
                       last_run_at = :now,
                       updated_at = now()
                 WHERE business_id = :business_id
                   AND next_run_at = :seen
                """
            ),
            {
                "following": following,
                "now": now,
                "business_id": row.business_id,
                "seen": row.next_run_at,
            },
        )
        # `rowcount` lives on `CursorResult`, which is what an UPDATE returns; the
        # declared type is the wider `Result`. Cast rather than `getattr`, so a future
        # SQLAlchemy that moved it fails the typecheck instead of silently reading 0 and
        # reporting that nobody won the claim.
        if cast("CursorResult[Any]", result).rowcount == 0:
            return None
    return following


async def _pause_over_ceiling(business_id: UUID) -> bool:
    """Pause this business's automation if its monthly ceiling is used up.

    Returns whether it was paused, so the caller skips it without needing to know how the
    decision was made. Called BEFORE `_claim`, which is what stops the slot being spent
    on a run that must not start: a claimed-then-refused slot would silently skip a cycle
    the owner never got.

    **The reason names both figures**, because the panel renders this string verbatim and
    "paused: over budget" gives an owner nothing to act on. `state.sentence` is the same
    sentence the API's 409 quotes, so the two cannot disagree about one ledger.

    Nothing here un-pauses. An automation resumes when the owner switches it back on
    (`save_automation` clears `paused_reason` on an explicit enable and recomputes the
    slot), which is deliberate: the window rolling forward is not, by itself, evidence
    that the owner still wants the runs they were refused.
    """
    state = await monthly_cap_state(business_id)
    if not state.exceeded:
        return False

    async with business_session(business_id) as db:
        await pause_automation(
            business_id,
            session=db,
            reason=(
                f"{state.sentence}, so scheduled runs are paused. Switch automation back "
                "on once the window has rolled forward."
            ),
        )
    logger.info("scheduler: paused automation for %s -- monthly ceiling used up", business_id)
    return True


async def _start_run(row: Any, *, submit: Any) -> UUID:
    """Create the run row and hand it to the executor.

    Channels come from the automation's own list, normalised through the same function
    the API validator and the checkpoint reader use — so an automation configured with a
    channel whose spec has since been removed renders for the channels that can actually
    be validated rather than failing at REPACK.
    """
    from backend.app.db.adapters.run_store import PostgresRunStore
    from backend.app.services.run_service import RunService

    channels = normalise_channels(row.channels)
    goal = str(row.goal_template or "").strip() or DEFAULT_GOAL
    service = RunService(PostgresRunStore(row.business_id))
    run = await service.start(business_id=row.business_id, goal=goal, channels=channels)
    submit(run.id, row.business_id, goal)
    return run.id


async def _sweep_stranded(*, now: datetime) -> int:
    """Mark runs that a dead process left `running`.

    `BACKLOG.md` section D records this as deferred with the reason "that is a worker's
    job", and this is the worker. Without it a run stranded by a crash stays `running`
    forever: it shows as live in the list, the timeline never advances, and nothing tells
    the owner it is over — indistinguishable from a run that is merely slow.

    **Read cross-tenant through the definer, then write per-tenant.** The first version
    of this was a single unscoped `UPDATE runs`, and it matched ZERO rows every time:
    `runs` has FORCE RLS, so an unscoped write reports success and changes nothing. A
    sweep that cleans nothing looks exactly like a sweep with nothing to clean, which is
    why a test asserting the row actually changed is the only way that bug surfaces.

    `finished_reason` says what happened rather than leaving a bare `failed`, because a
    run that failed for a reason nobody recorded is a support ticket. The resume route
    still works afterwards, so the sweep closes the run without closing the recovery
    path.
    """
    minutes = max(int(STRANDED_AFTER.total_seconds() // 60), 1)
    async with session() as db:
        stranded = list(
            (
                await db.execute(
                    text("SELECT id, business_id FROM stranded_runs(:minutes)"),
                    {"minutes": minutes},
                )
            ).all()
        )

    marked = 0
    for row in stranded:
        try:
            async with business_session(row.business_id) as db:
                await db.execute(
                    text(
                        """
                        UPDATE runs
                           SET state = 'failed',
                               finished_reason = :reason,
                               updated_at = now()
                         WHERE id = :id
                           AND state = 'running'
                        """
                    ),
                    {"id": row.id, "reason": STRANDED_REASON},
                )
            marked += 1
        except Exception:
            # One tenant's write failing must not abandon the rest of the sweep.
            logger.exception("scheduler: could not close stranded run %s", row.id)
    return marked


async def tick(*, submit: Any, now: datetime | None = None) -> TickReport:
    """One pass. Never raises.

    A scheduler that dies on a bad row stops being a scheduler, so every per-business
    step is wrapped and the loop continues. `submit` is injected rather than imported so
    a test drives this without an executor, a graph or a model.
    """
    moment = now or datetime.now(UTC)
    report = TickReport()

    try:
        due = await _due()
    except Exception:
        # Loud, and the tick ends: if the scan itself is broken there is no work to do
        # and retrying inside this pass would just fail again a millisecond later.
        logger.exception("scheduler: could not read due automations")
        return report

    for row in due:
        try:
            # Before the claim, and before anything that can reach a provider. See the
            # module docstring: this worker used to be the one run-starting path that
            # did not ask.
            if await _pause_over_ceiling(row.business_id):
                report.paused.append(row.business_id)
                continue
            if await _claim(row, now=moment) is None:
                continue
            report.started.append(await _start_run(row, submit=submit))
        except Exception:
            # One tenant's failure costs that tenant. The slot is already claimed, so
            # this business waits for its next slot rather than being retried in a loop
            # that would spend its whole model budget on the same broken run.
            logger.exception("scheduler: could not start a run for %s", row.business_id)
            report.failed.append(row.business_id)

    try:
        report.stranded_marked = await _sweep_stranded(now=moment)
    except Exception:
        logger.exception("scheduler: the stranded-run sweep failed")

    return report


async def run_forever(
    *, submit: Any, interval_s: float = DEFAULT_INTERVAL_S, stop: asyncio.Event | None = None
) -> None:
    """Tick until asked to stop.

    The sleep is at the END of the loop, so starting the process does a pass immediately
    rather than waiting a minute to discover it works. `stop` is injected so a test can
    end the loop deterministically instead of by cancelling a task and hoping.
    """
    logger.info("scheduler: started, every %.0fs", interval_s)
    while True:
        report = await tick(submit=submit)
        if report.started or report.failed or report.paused or report.stranded_marked:
            logger.info(
                "scheduler: started=%d failed=%d paused=%d stranded=%d",
                len(report.started),
                len(report.failed),
                len(report.paused),
                report.stranded_marked,
            )
        if stop is not None and stop.is_set():
            return
        try:
            if stop is not None:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
                return
            await asyncio.sleep(interval_s)
        except TimeoutError:
            continue
