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

import base64
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import islice
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.app.agents.state import AgentState, approve, from_checkpoint, to_checkpoint

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


#: How many runs a list returns when the caller does not say, and the most it may ask
#: for. Twenty is about a screenful; the ceiling exists because `limit` arrives from a
#: query parameter, and an unbounded one is a request to serialise every run a business
#: has ever made. Same shape and same reasoning as `DEFAULT_LEAD_LIMIT` next door.
DEFAULT_RUN_LIST_LIMIT: Final = 20
MAX_RUN_LIST_LIMIT: Final = 100


class RunSummaryRecord(BaseModel):
    """One run as a LIST needs it: enough to choose between runs, and no checkpoint.

    Deliberately not :class:`RunRecord`. That model carries ``checkpoint``, which is the
    entire serialised agent state -- the draft HTML included -- so a list of twenty of
    them is megabytes of JSONB read, decoded and discarded in order to render a column of
    goals. It is the same rule that keeps the draft out of the polled timeline payload,
    applied at the surface where it would cost the most.

    ``business_id`` stays on it because the route compares it to the caller's, exactly as
    ``_require_own_run`` does for a single run: the database's scoping is the guarantee,
    and that comparison is the defence in depth behind it.

    ``state`` and ``finished_reason`` are both here on purpose. A run on a deployment
    whose credential cannot reach the mid tier legitimately ends ``partial``, and a list
    that could show only the state would leave every screen built on it announcing a
    terminal state with no account of why.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    business_id: UUID
    goal: str
    state: RunState
    current_node: str | None = None
    resumed_count: int = 0
    finished_reason: str | None = None
    created_at: datetime


#: Where a page of runs starts: the ``(created_at, id)`` of the last row of the
#: previous page. KEYSET, not an offset, and the reason is correctness rather than
#: speed at this scale: a run started while somebody is reading page one shifts every
#: offset by a row, so `OFFSET 50` then skips a run that has moved down into it. A
#: cursor names a position in the ORDER, which new rows above it cannot move.
RunCursor = tuple[datetime, UUID]


def encode_cursor(cursor: RunCursor) -> str:
    """The cursor as one URL-safe string the client hands back unchanged.

    base64url, and not for obfuscation: a timestamp's ISO form contains ``+`` for its
    UTC offset, and a ``+`` in a query string decodes as a SPACE. A raw cursor
    therefore breaks the moment anything builds a URL by concatenation, which is what
    every client does. Encoding it removes a whole class of "the next-page button
    sometimes 422s" bug.

    Deliberately NOT signed. It carries no authority: the store is already scoped to
    one tenant by row-level security, so the worst a forged cursor can do is ask for a
    page of the caller's OWN runs starting somewhere odd. Signing it would imply it was
    a capability, which invites treating it as one.
    """
    stamp, run_id = cursor
    raw = f"{stamp.isoformat()}|{run_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(raw: str) -> RunCursor:
    """Parse a cursor, or raise ``ValueError``.

    Raises rather than silently returning the first page, because a client that mistyped
    a cursor wants to be told: quietly restarting the list looks exactly like the "older
    runs" button not working, which is the bug report nobody can reproduce.
    """
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("a run cursor is base64url of '<timestamp>|<uuid>'") from exc

    stamp, separator, run_id = decoded.partition("|")
    if not separator or not stamp or not run_id:
        raise ValueError("a run cursor is base64url of '<timestamp>|<uuid>'")
    return datetime.fromisoformat(stamp), UUID(run_id)


def check_list_limit(limit: int) -> None:
    """Refuse a limit outside the band, in the STORE rather than only at the route.

    The route in front of this today declares the same bounds to FastAPI, so a bad value
    is normally a 422 and never arrives. This is here for the caller that is not that
    route -- a script, a future endpoint, the executor -- because a bound enforced in
    exactly one place is a bound that disappears the first time something else calls in.
    """
    if not 1 <= limit <= MAX_RUN_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_RUN_LIST_LIMIT}, got {limit}")


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

    async def list_runs(
        self, *, limit: int = DEFAULT_RUN_LIST_LIMIT, before: RunCursor | None = None
    ) -> Sequence[RunSummaryRecord]: ...


class InMemoryRunStore:
    """For tests and for a process with no database.

    ``business_id`` is OPTIONAL, and the asymmetry with :class:`PostgresRunStore` -- where
    it is required -- is deliberate. The real store is constructed per request and leaves
    tenant isolation to row-level security; passing the same id here lets the fake model
    that shape, so a test can exercise "a scoped store lists only its own runs" without a
    database. Left unset, the fake holds runs for every business at once, which is how the
    existing route tests check that a caller cannot reach another business's run by
    knowing its id -- and it is why :func:`list_runs` on THIS class cannot be the only
    thing standing between the route and a cross-tenant read.
    """

    def __init__(self, business_id: UUID | None = None) -> None:
        self._runs: dict[UUID, RunRecord] = {}
        self._events: dict[UUID, list[RunEventRecord]] = {}
        self._created_at: dict[UUID, datetime] = {}
        self._scope = business_id

    async def create(self, run: RunRecord) -> RunRecord:
        self._runs[run.id] = run
        self._events[run.id] = []
        self._created_at[run.id] = datetime.now(UTC)
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

    async def list_runs(
        self, *, limit: int = DEFAULT_RUN_LIST_LIMIT, before: RunCursor | None = None
    ) -> Sequence[RunSummaryRecord]:
        """Newest first, by INSERTION order rather than by the stamp.

        The Postgres store orders by ``created_at DESC``, which is the same intent; here
        insertion order is used because two runs started in the same test land on the same
        clock reading often enough to make a timestamp sort flap. The stamp is still
        carried, because the screen renders it -- it is just not what the order rests on.

        ``before`` therefore has to be applied by POSITION here rather than by comparing
        stamps: with several runs sharing a clock reading, a `created_at < cursor` filter
        in this store would drop the rest of that batch. The Postgres store, whose order
        really is `(created_at DESC, id DESC)`, compares the tuple.
        """
        check_list_limit(limit)
        newest_first = list(reversed(list(self._runs.values())))
        if before is not None:
            _, cursor_id = before
            position = next(
                (index for index, run in enumerate(newest_first) if run.id == cursor_id), None
            )
            newest_first = newest_first[position + 1 :] if position is not None else []
        in_scope = (
            run for run in newest_first if self._scope is None or run.business_id == self._scope
        )
        return [
            RunSummaryRecord(
                id=run.id,
                business_id=run.business_id,
                goal=run.goal,
                state=run.state,
                current_node=run.current_node,
                resumed_count=run.resumed_count,
                finished_reason=run.finished_reason,
                created_at=self._created_at[run.id],
            )
            for run in islice(in_scope, limit)
        ]


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

    async def recent(
        self, *, limit: int = DEFAULT_RUN_LIST_LIMIT, before: RunCursor | None = None
    ) -> list[RunSummaryRecord]:
        """This store's runs, newest first.

        Named ``recent`` rather than ``list``: the word describes the ORDER, which is the
        part a caller has to know, and it does not shadow the builtin at a call site.

        There is no ``business_id`` argument and there must not be one. The store already
        knows its tenant -- ``PostgresRunStore`` takes it in the constructor precisely so
        that row-level security answers "whose runs are these", rather than a lookup
        carved out from under RLS. Accepting one here would put that question back in the
        caller's hands, which is the bug the store's docstring exists to describe.

        ``before`` continues an earlier page. See :data:`RunCursor` for why it is a
        keyset position rather than an offset.
        """
        return list(await self._store.list_runs(limit=limit, before=before))

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

    async def record_approval(self, run_id: UUID, *, approver: str) -> bool:
        """Write the approver into the parked run's checkpoint. Returns whether it stuck.

        This is the step that makes `approved_by` a real fact rather than a seam. EXPORT
        publishes nothing without it (and says so), so until something called this, every
        real run correctly published nothing.

        **It writes to the CHECKPOINT rather than to a column**, because the checkpoint is
        what a resume restores: an approver stored anywhere else would have to be threaded
        back into `AgentState` by the executor, which is a second path to keep in step
        with the first. `to_checkpoint` round-trips it already.

        Returns False when there is no checkpoint to write into, which the route turns
        into a refusal. That is not a hypothetical: a run can be parked before its first
        checkpoint if it fails early, and approving one would be approving nothing.

        The run's `state` is deliberately NOT changed here. `resume` is what starts it,
        and doing both in one method would make "approved but not yet running" impossible
        to represent -- which is exactly the state a run is in between the two calls.
        """
        run = await self._require(run_id)
        if not run.checkpoint:
            return False

        state = approve(from_checkpoint(run.checkpoint), approver=approver)
        run.checkpoint = to_checkpoint(state)
        await self._store.update(run)
        return True

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
    "DEFAULT_RUN_LIST_LIMIT",
    "MAX_FINISHED_REASON",
    "MAX_RUN_LIST_LIMIT",
    "EventStatus",
    "InMemoryRunStore",
    "RunCursor",
    "RunEventRecord",
    "RunRecord",
    "RunService",
    "RunState",
    "RunStore",
    "RunSummaryRecord",
    "check_list_limit",
    "clamp_reason",
    "decode_cursor",
    "encode_cursor",
]
