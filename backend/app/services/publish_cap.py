"""The weekly published-pieces cap: `ARCHITECTURE.md` §7.4's third ceiling.

The other two caps are cost controls. This one is not, and the difference decides
everything about where it goes. `ROADMAP.md` §10 names scaled content abuse — Google's
spam policy targets it, not AI as such — as a risk this architecture has to mitigate
rather than disclaim, and §15 states that this product "will not mass-produce, and
shouldn't". So the cap is a QUALITY control, enforced at the moment of publication, and a
refusal is a sentence an owner reads rather than a silent skip.

**Where it is checked, and why not one step later.** Before `store.claim`, never after.
`actuate()`'s documented order puts the approval check *after* the claim, deliberately, so
that a refusal leaves an auditable row — and a cap check in that position would be a bug
rather than a symmetry. `actions.idempotency_key` is uniquely indexed and `claim` returns
the outcome a key already holds, so a cap refusal recorded there would poison the key: the
same post could never be published again, in any later week, because every future attempt
would replay the stored refusal instead of calling. The cap says *not this week*, and a
mechanism that turned that into *not ever* would be the opposite of a quality control.

**So no `actions` row is written for a cap refusal, and that is the deliberate reading of
what that table is for.** It is the ledger of attempted side effects, keyed by a lock; a
capped publish attempts nothing. The refusal is reported where the project already reports
what happened to finished content — on the run's `published` report and as a named
`NodeError` the review screen renders, or on the `PublishResult` the calendar button gets
back. Nothing is lost: the pieces stay queued, and the count that refused them is stated.

**One decision, two consequences, in the shape `cost_service.monthly_cap_state` uses.**
The count and the boundary live here; EXPORT partially publishes up to the headroom and
names what it did not send, and the calendar's publish button refuses the one post it was
asked about. Two callers computing "am I over?" separately is exactly how the per-business
USD ceiling came to be enforced on the HTTP path and not on the scheduled one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from uuid import UUID

from backend.app.core.config import get_settings

__all__ = [
    "WEEKLY_WINDOW_DAYS",
    "PublishCounter",
    "PublishLedger",
    "WeeklyPublishState",
    "weekly_publish_state",
]

#: A ROLLING seven days, not a calendar week. A calendar week would hand every business a
#: full allowance at midnight on Monday and invite a burst against the reset; a rolling
#: window has no reset to game and needs no timezone to agree on.
WEEKLY_WINDOW_DAYS: Final = 7


class PublishLedger(Protocol):
    """Counts this business's recent publishes.

    Its own protocol rather than a method added to
    :class:`~backend.app.actuators.contract.ActuatorStore`, because that one is
    deliberately two methods wide and every test double in the suite implements it. A
    consumer should ask for exactly what it needs; `PostgresActionStore` satisfies both.
    """

    async def published_since(
        self, business_id: UUID, *, action_types: Collection[str], since: datetime
    ) -> int: ...


#: The seam the graph gets, so the nodes stay database-free and every node test stays
#: hermetic — the same shape as `NodeDeps.publish_distribution`.
type PublishCounter = Callable[[UUID], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class WeeklyPublishState:
    """How much of this business's weekly allowance is left.

    A value object rather than a bare number, because every caller needs the FIGURES to
    say anything useful: "over the cap" with no count and no window gives an owner nothing
    to act on and gives support nothing to check.
    """

    published: int
    cap: int
    window_days: int = WEEKLY_WINDOW_DAYS

    @property
    def headroom(self) -> int:
        """How many more pieces may go out now. Never negative.

        Clamped because a cap LOWERED below what a business already published this week
        would otherwise produce a negative allowance, and a negative headroom would slice
        a list of pieces from the wrong end.
        """
        return max(0, self.cap - self.published)

    @property
    def exhausted(self) -> bool:
        return self.headroom == 0

    @property
    def sentence(self) -> str:
        """The shared statement of fact, with no consequence attached.

        Stops before "so ...", exactly as `MonthlyCapState.sentence` does: EXPORT appends
        "these were not sent" and the calendar appends "this post stays queued", and
        neither may drift on how the count is stated.
        """
        return (
            f"This business has published {self.published} of its {self.cap} pieces "
            f"for the last {self.window_days} days"
        )


async def weekly_publish_state(
    business_id: UUID,
    *,
    count: PublishCounter,
    cap: int | None = None,
) -> WeeklyPublishState:
    """Count this business's recent publishes and report where it stands.

    `count` is injected rather than a store method called directly, so the graph can hold
    a callable and stay free of a database. `cap` defaults to the configured ceiling, read
    HERE so two callers cannot enforce two different numbers — the defect this module's
    shape exists to prevent.
    """
    ceiling = cap if cap is not None else get_settings().business_weekly_publish_cap
    return WeeklyPublishState(published=await count(business_id), cap=ceiling)


def window_start(*, now: datetime | None = None) -> datetime:
    """The oldest instant inside the rolling window.

    Takes `now` so a test can place a publish at either edge of the window rather than
    waiting a week, which is the same reason `compute_next_run` takes `after`.
    """
    moment = now or datetime.now(UTC)
    return moment - timedelta(days=WEEKLY_WINDOW_DAYS)


def counter_for(ledger: PublishLedger, *, action_types: Collection[str]) -> PublishCounter:
    """Bind a ledger and the publishable action types into the seam the graph holds.

    `action_types` is passed in rather than imported, because the canonical set lives in
    `agents/nodes` (`PUBLISHABLE_ACTIONS`) and a service importing the agent package would
    invert the dependency direction the engine-boundary test exists to protect.
    """

    async def count(business_id: UUID) -> int:
        return await ledger.published_since(
            business_id, action_types=action_types, since=window_start()
        )

    return count
