"""Run persistence and the event timeline.

Two design points do all the work here.

**Events are persisted as well as streamed.** The SSE stream is a convenience; the table
is the truth. A browser that reloads mid-run must see the same timeline it saw a moment
ago, and a run whose worker died must be resumable from what was actually recorded rather
than from what a client happened to still hold.

**The payload on an event is operational, never content.** Cost, duration, a short
summary — never the generated HTML. Putting draft text in the timeline would duplicate the
content store and make every stream frame large for no benefit, and the filtering happens
here rather than at each call site so a caller cannot get it wrong.

The store is a Protocol with an in-memory implementation, so the service is testable
without a database and the Postgres adapter is a separate, replaceable thing.
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.app.agents.state import AgentState, from_checkpoint, to_checkpoint

logger = logging.getLogger(__name__)

RunState = Literal["queued", "running", "awaiting_approval", "done", "failed", "partial"]
EventStatus = Literal["started", "done", "failed", "skipped"]

#: Keys allowed on an event payload. An allowlist rather than a denylist: a new node that
#: returns something large should not be able to leak it into every stream frame just
#: because nobody thought to exclude it.
ALLOWED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"cost_usd", "duration_ms", "summary", "error", "score", "count", "tier", "model"}
)


class RunRecord(BaseModel):
    """One run, as stored."""

    model_config = ConfigDict(frozen=False)

    id: UUID
    business_id: UUID
    goal: str
    state: RunState = "queued"
    current_node: str | None = None
    resumed_count: int = 0
    finished_reason: str | None = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class RunEventRecord(BaseModel):
    """One line in the timeline."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    seq: int
    node: str
    status: EventStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime


class RunStore(Protocol):
    """Persistence for runs and their events."""

    async def create(self, run: RunRecord) -> RunRecord: ...

    async def get(self, run_id: UUID) -> RunRecord | None: ...

    async def update(self, run: RunRecord) -> None: ...

    async def append_event(self, event: RunEventRecord) -> None: ...

    async def next_seq(self, run_id: UUID) -> int: ...

    async def list_events(
        self, run_id: UUID, *, after_seq: int = 0
    ) -> Sequence[RunEventRecord]: ...


class InMemoryRunStore:
    """For tests and for a process with no database."""

    def __init__(self) -> None:
        self._runs: dict[UUID, RunRecord] = {}
        self._events: dict[UUID, list[RunEventRecord]] = {}

    async def create(self, run: RunRecord) -> RunRecord:
        self._runs[run.id] = run
        self._events[run.id] = []
        return run

    async def get(self, run_id: UUID) -> RunRecord | None:
        return self._runs.get(run_id)

    async def update(self, run: RunRecord) -> None:
        if run.id not in self._runs:
            raise KeyError(run.id)
        self._runs[run.id] = run

    async def append_event(self, event: RunEventRecord) -> None:
        if event.run_id not in self._events:
            raise KeyError(event.run_id)
        self._events[event.run_id].append(event)

    async def next_seq(self, run_id: UUID) -> int:
        if run_id not in self._events:
            raise KeyError(run_id)
        return len(self._events[run_id]) + 1

    async def list_events(self, run_id: UUID, *, after_seq: int = 0) -> Sequence[RunEventRecord]:
        return [e for e in self._events.get(run_id, []) if e.seq > after_seq]


def _clean_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only operational fields, and keep them JSON-safe.

    Dropped keys are logged at debug rather than silently vanishing, so a developer
    wondering where their field went can find out.
    """
    if not payload:
        return {}
    kept: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in ALLOWED_PAYLOAD_KEYS:
            logger.debug("run event payload key %r dropped (not operational)", key)
            continue
        try:
            json.dumps(value)
            kept[key] = value
        except (TypeError, ValueError):
            kept[key] = str(value)
    return kept


#: Matches `runs.finished_reason` (VARCHAR(255)). Not a style choice -- exceeding it
#: makes the UPDATE raise, which strands the run in `running`.
MAX_FINISHED_REASON: Final = 255

#: Marks a clamped reason, so a reader can tell a truncated message from a terse one.
_TRUNCATION_MARK: Final = " ..."


def clamp_reason(reason: str | None) -> str | None:
    """Fit a reason into the column, marking it when something was cut.

    Keeps the HEAD, because every reason in this codebase is written with the
    human-readable sentence first and the machine detail after -- so what survives
    truncation is the part somebody reads.
    """
    if reason is None or len(reason) <= MAX_FINISHED_REASON:
        return reason
    keep = MAX_FINISHED_REASON - len(_TRUNCATION_MARK)
    return reason[:keep].rstrip() + _TRUNCATION_MARK


class RunService:
    """The lifecycle of a run: start, record, checkpoint, finish."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    async def start(self, *, business_id: UUID, goal: str) -> RunRecord:
        return await self._store.create(
            RunRecord(id=uuid4(), business_id=business_id, goal=goal, state="queued")
        )

    async def get(self, run_id: UUID) -> RunRecord | None:
        """One run, or None.

        A real method rather than letting callers reach into ``_store``: three route
        handlers were doing exactly that, and a private attribute accessed from three
        places is a public API with the wrong name on it.
        """
        return await self._store.get(run_id)

    async def record_event(
        self,
        run_id: UUID,
        *,
        node: str,
        status: EventStatus,
        payload: dict[str, Any] | None = None,
    ) -> RunEventRecord:
        """Append one event. Raises for an unknown run rather than dropping it.

        A silently-dropped event is worse than an error: the timeline would simply be
        missing a step, and nobody would know which.
        """
        seq = await self._store.next_seq(run_id)
        event = RunEventRecord(
            run_id=run_id,
            seq=seq,
            node=node,
            status=status,
            payload=_clean_payload(payload),
            at=datetime.now(UTC),
        )
        await self._store.append_event(event)
        return event

    async def events(self, run_id: UUID, *, after_seq: int = 0) -> list[RunEventRecord]:
        return list(await self._store.list_events(run_id, after_seq=after_seq))

    async def checkpoint(
        self, run_id: UUID, *, state: AgentState, current_node: str | None = None
    ) -> None:
        """Store the resume point. Money survives as a string, never a float."""
        run = await self._require(run_id)
        run.checkpoint = to_checkpoint(state)
        run.current_node = current_node
        run.state = "running"
        await self._store.update(run)

    async def restore(self, run_id: UUID) -> AgentState | None:
        run = await self._store.get(run_id)
        if run is None or not run.checkpoint:
            return None
        return from_checkpoint(run.checkpoint)

    async def await_approval(self, run_id: UUID) -> None:
        """Park the run for a human.

        Its own state, not a flavour of running or done: the queue must neither re-run it
        nor forget it while somebody decides.
        """
        run = await self._require(run_id)
        run.state = "awaiting_approval"
        await self._store.update(run)

    async def finish(self, run_id: UUID, *, outcome: RunState, reason: str | None = None) -> None:
        """Write the terminal state. The reason is CLAMPED to the column width.

        Found the hard way. `runs.finished_reason` is VARCHAR(255), and a reason that
        embeds a provider error can be far longer -- an `AllProvidersFailedError` naming
        two refused models with their 404 bodies is about 700 characters. The UPDATE
        raised `StringDataRightTruncationError`, so `finish` failed; the executor's
        failure handler then tried to record THAT exception as the reason, which was
        longer still and failed the same way. The run was left saying `running` forever.

        Clamping here rather than at each call site because every caller has the same
        problem and only this one knows the column: a caller that composes a reason from
        an exception cannot know how long the exception will be. Truncating a message is
        a cosmetic loss; failing to record a terminal state strands the run.
        """
        run = await self._require(run_id)
        run.state = outcome
        run.finished_reason = clamp_reason(reason)
        await self._store.update(run)

    async def mark_resumed(self, run_id: UUID) -> None:
        """Count a resumption. A rising number means the workers are unstable, which is
        only visible if it is recorded rather than inferred."""
        run = await self._require(run_id)
        run.resumed_count += 1
        await self._store.update(run)

    async def _require(self, run_id: UUID) -> RunRecord:
        run = await self._store.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run


__all__ = [
    "ALLOWED_PAYLOAD_KEYS",
    "MAX_FINISHED_REASON",
    "EventStatus",
    "InMemoryRunStore",
    "RunEventRecord",
    "RunRecord",
    "RunService",
    "RunState",
    "RunStore",
    "clamp_reason",
]
