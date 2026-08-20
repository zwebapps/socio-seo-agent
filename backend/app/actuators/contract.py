"""The actuator contract: the only way anything in this system touches the outside world.

`docs/ARCHITECTURE.md` §3 names three component kinds — Engines compute, Agents decide,
Actuators *act* — and says why the third exists: idempotency, approval policy and the
audit log all apply to exactly one class of operation, the ones with external side
effects, and naming that class puts those three concerns in one place instead of
scattering them through the graph. Until now the class was named and empty. The
`publish` and `notify` tools were string constants, `EXPORT` held them in its allowlist,
and there was no `EXPORT` node in the graph.

Four rules, and they are the whole reason this module exists rather than each publisher
calling an HTTP client:

**1. The record is written BEFORE the call.** `actions.idempotency_key` is uniquely
indexed, and that index IS the lock. A crash mid-call leaves an `in_flight` row that a
human or a reconciler can ask the provider about; the alternative is an invisible gap
that a retry turns into a double post. This is the one ordering in the file that cannot
be relaxed.

**2. A replay returns the FIRST result, and never calls again.** Not an error — the
caller asked for the same effect twice and the effect has happened, so the honest answer
is the outcome it already has. `Outcome.replayed` says which it was, because "posted" and
"already posted" are different facts about this run even when they are the same fact
about the world.

**3. Nothing publishes without approval.** `Actuation.approved_by` is required, and a
missing one is a REFUSAL recorded as such, not an exception. The graph's human interrupt
at REVIEW is what produces it; an actuator that could be called without one would make
that gate decorative.

**4. A missing credential means a FAKE actuator that SAYS SO** — the same posture as the
model router (`CLAUDE.md`): never a silent no-op, never a crash, and never a report that
a post went live when nothing left the process. `Outcome.fake` carries that upward so
every surface can say it.

No LLM here, and no engine may import this module — `tests/test_engine_boundary.py`
fails the build on the second one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

__all__ = [
    "ACTION_STATUSES",
    "Actuation",
    "ActuationRefusedError",
    "Actuator",
    "ActuatorError",
    "ActuatorStore",
    "Outcome",
    "OutcomeStatus",
    "idempotency_key",
]


class OutcomeStatus(StrEnum):
    """What happened, in the vocabulary the `actions` table already checks.

    Mirrors the CHECK constraint on `actions.status` deliberately: a status this code
    can produce and the database will reject is a write that fails at 3am, so the two
    lists are the same list.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"


#: The statuses a row can hold, `in_flight` included. `in_flight` is not an `Outcome`
#: because it is not an outcome: it is the state a row is in while we do not know.
ACTION_STATUSES: Final[frozenset[str]] = frozenset(
    {"in_flight", *(status.value for status in OutcomeStatus)}
)


class ActuatorError(Exception):
    """A side effect could not be performed. Carries whether retrying is sane.

    `retryable` is the field callers actually branch on: a rate limit is worth another
    attempt in a minute and a rejected token is not, and treating them alike is how a
    publisher either gives up on a transient blip or hammers a permanent failure.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ActuationRefusedError(ActuatorError):
    """The actuator declined on POLICY rather than failing.

    Named with the `Error` suffix because it IS an exception (ruff N818), even though
    what it reports is the system working as designed.

    Its own type because the two must not be logged the same way: a refusal is the
    system working (no approval, a banned claim, a channel that cannot carry a link),
    and an alert on it would train everyone to ignore alerts. Never retryable — the
    thing that has to change is the request, not the timing.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True, slots=True)
class Actuation:
    """One requested side effect: what, where, for whom, and on whose authority.

    `approved_by` is not optional and not defaulted. An actuator call that could omit it
    would make the REVIEW interrupt decorative, and a default would put the decision in
    this file instead of in front of a person.
    """

    business_id: UUID
    #: `social.post`, `notify.email`, `publish.wordpress`. Dotted, target-first, so the
    #: name says which integration owns it — the same convention as the tool allowlist.
    action_type: str
    #: The channel or destination within that integration (`linkedin`, an address, a
    #: site id). Part of the idempotency key, so posting the same body to two channels
    #: is two actions rather than one replay.
    target: str
    #: What to send. Whatever the actuator needs, JSON-serialisable because it is
    #: persisted verbatim: an audit row that cannot say what was sent is not an audit.
    payload: Mapping[str, Any]
    #: Who approved it. A user id, or `"policy:<name>"` for something a written policy
    #: permits without a human.
    approved_by: str
    run_id: UUID | None = None
    #: Set to reuse a key across attempts of the same logical effect. Left None, the key
    #: is derived from the content — see `idempotency_key`.
    key: str | None = None

    def idempotency_key(self) -> str:
        """This actuation's key: the caller's, or one derived from its content."""
        return self.key or idempotency_key(
            business_id=self.business_id,
            action_type=self.action_type,
            target=self.target,
            payload=self.payload,
        )


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of one actuation, as stored and as reported.

    `external_ref` is the thing a customer can check — a post URL, a message id. It is
    the difference between "we published it" and "we published it, here".
    """

    status: OutcomeStatus
    action_type: str
    target: str
    external_ref: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    #: True when this call did NOT reach the provider because the same key had already
    #: succeeded. The world is unchanged; the caller's expectation is satisfied.
    replayed: bool = False
    #: True when a FAKE actuator produced this because no credential is configured.
    #: Carried all the way to the screen: a report that cannot distinguish a real post
    #: from a simulated one is worse than no report.
    fake: bool = False
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def succeeded(self) -> bool:
        return self.status is OutcomeStatus.SUCCEEDED

    def summary(self) -> str:
        """One line for a timeline or a log. Never claims more than happened."""
        where_to = f"{self.action_type} → {self.target}"
        why = self.error or "no reason given"
        if self.status is OutcomeStatus.REFUSED:
            return f"{where_to}: refused ({why})"
        if self.status is OutcomeStatus.FAILED:
            return f"{where_to}: failed ({why})"
        prefix = "already done" if self.replayed else "done"
        where = f" → {self.external_ref}" if self.external_ref else ""
        fake = " (SIMULATED — no credential configured)" if self.fake else ""
        return f"{self.action_type} → {self.target}: {prefix}{where}{fake}"


class ActuatorStore(Protocol):
    """Persistence for the `actions` ledger. A protocol, so actuators need no database.

    `claim` is the load-bearing method and its contract is unusual on purpose: it either
    reserves the key (returning None) or hands back the outcome that key already has. A
    store that answered "exists / does not exist" would leave the caller to fetch the
    prior result separately, and the gap between those two calls is where a double post
    lives.
    """

    async def claim(self, actuation: Actuation) -> Outcome | None: ...

    async def settle(self, actuation: Actuation, outcome: Outcome) -> None: ...


class Actuator(Protocol):
    """Something that performs one kind of external side effect.

    Deliberately narrow: `perform` does the call and nothing else — no idempotency, no
    persistence, no approval check. Those live in `actuate()`, once, so a new integration
    cannot forget them.
    """

    @property
    def action_type(self) -> str:
        """The dotted name this actuator answers to."""
        ...

    @property
    def fake(self) -> bool:
        """True when it is simulating because no credential is configured."""
        ...

    async def perform(self, actuation: Actuation) -> Outcome:
        """Do it. Raise `ActuatorError` (or `ActuationRefusedError`) on failure."""
        ...


def idempotency_key(
    *,
    business_id: UUID,
    action_type: str,
    target: str,
    payload: Mapping[str, Any],
) -> str:
    """A stable key for one logical side effect.

    Derived from the CONTENT, so re-running a node that produced the same post does not
    post twice, while an edited post is a different effect and does go out. That is the
    behaviour a customer expects from "publish" and it is not what a random uuid gives:
    a uuid per attempt makes every retry a fresh post.

    Sorted, separator-delimited JSON so two dicts that differ only in key order hash
    alike — otherwise the same post assembled by a different code path would look new.
    """
    material = json.dumps(
        {
            "business_id": str(business_id),
            "action_type": action_type,
            "target": target,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(material.encode()).hexdigest()
    # Prefixed so a human reading the `actions` table can tell what a row is without
    # joining anything, and short enough to leave room in the 512-char column.
    return f"{action_type}:{target}:{digest[:32]}"
