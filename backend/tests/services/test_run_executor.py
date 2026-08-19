"""``RunExecutor``: the piece that makes a started run actually run.

Every test here uses `InMemoryRunStore` and a stub graph, so the suite stays hermetic
-- no model, no database, no network. What is being tested is the JOIN: that a
submitted run reaches a terminal state, that its events land in order, and that a
failure is recorded rather than swallowed by a task nobody awaits.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest

from backend.app.agents.nodes import NodeDeps
from backend.app.services import run_executor as executor_module
from backend.app.services.run_executor import EXECUTOR_NODE, RunExecutor, summarise_crawl
from backend.app.services.run_service import (
    MAX_FINISHED_REASON,
    InMemoryRunStore,
    RunService,
    clamp_reason,
)

BUSINESS = UUID("11111111-1111-4111-8111-111111111111")


class _Graph:
    """Stands in for `run_graph`.

    Records how it was called, emits the events it is told to, and returns whatever
    outcome the test wants -- including raising, which is the case that matters most.
    """

    def __init__(
        self,
        *,
        events: list[tuple[str, str]] | None = None,
        interrupted: bool = False,
        outcome: str = "done",
        raises: Exception | None = None,
        visited: list[str] | None = None,
    ) -> None:
        self.events = events or []
        self.interrupted = interrupted
        self.outcome = outcome
        self.raises = raises
        self.visited = visited or ["INTAKE"]
        self.reason: str | None = None
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, state: Any, *, nodes: Any, on_event: Any = None, resume: bool = False
    ) -> Any:
        # Snapshot, not a reference: this stub mutates `state` a few lines down, and
        # recording the object meant every assertion read the POST-run value. That is
        # what made the resume tests look like the checkpoint had not been restored.
        self.calls.append(
            {
                "visited_at_entry": list(state.get("visited") or []),
                "goal": state.get("goal"),
                "resume": resume,
            }
        )
        for node, status in self.events:
            if on_event is not None:
                on_event(node, status, {"node": node})
            # Yield, so a naive fire-and-forget sink would have every chance to
            # interleave and reveal itself.
            await asyncio.sleep(0)
        if self.raises is not None:
            raise self.raises
        state["outcome"] = self.outcome
        state["visited"] = self.visited
        if self.reason is not None:
            state["finished_reason"] = self.reason
        return type("Result", (), {"state": state, "interrupted": self.interrupted})()


@pytest.fixture
def store() -> InMemoryRunStore:
    return InMemoryRunStore()


@pytest.fixture
def service(store: InMemoryRunStore) -> RunService:
    return RunService(store)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise node construction; the graph stub does not need real nodes."""
    monkeypatch.setattr(executor_module, "build_nodes", lambda deps: {})


async def _deps(business_id: UUID, usage_sink: object = None) -> NodeDeps:
    return NodeDeps(router=object())


def _executor(service: RunService, graph: _Graph, monkeypatch: pytest.MonkeyPatch) -> RunExecutor:
    monkeypatch.setattr(executor_module, "run_graph", graph)
    return RunExecutor(service_factory=lambda _bid: service, deps_factory=_deps)


# --------------------------------------------------------------------------- #
# The gap this module closes
# --------------------------------------------------------------------------- #


async def test_a_submitted_run_reaches_a_terminal_state(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the executor existed, a started run stayed `queued` forever."""
    graph = _Graph(outcome="done", visited=["INTAKE", "HARVEST"])
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    finished = await service.get(run.id)
    assert finished is not None
    assert finished.state == "done"
    assert finished.current_node == "HARVEST", "the checkpoint should name where it got to"


async def test_events_are_persisted_in_the_order_the_graph_emitted_them(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`seq` is derived from the current maximum, so concurrent writes would collide.

    The sink is synchronous while `record_event` is async, and the tempting fix -- a
    task per event -- would race here and produce duplicate sequence numbers or holes.
    A hole is worse than a duplicate: a resumed run reads it as a node that never ran.
    """
    emitted = [
        ("INTAKE", "started"),
        ("INTAKE", "done"),
        ("HARVEST", "started"),
        ("HARVEST", "done"),
        ("OPPORTUNITY", "started"),
    ]
    graph = _Graph(events=emitted)
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    events = await service.events(run.id)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs), "sequence numbers must be monotonic"
    assert len(seqs) == len(set(seqs)), "a duplicate seq means two writers raced"
    # The graph's own events, in order, after the executor's opening status line.
    graph_events = [(e.node, e.status) for e in events if e.node != EXECUTOR_NODE]
    assert graph_events == emitted


async def test_a_failing_run_is_recorded_as_failed_with_a_reason(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE case that justifies the guard.

    The run executes in a task nobody awaits. Without an explicit catch, the exception
    goes to asyncio's "never retrieved" handler and the row keeps saying `running` --
    which is indistinguishable from a slow run, forever.
    """
    graph = _Graph(raises=RuntimeError("provider exploded"))
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    failed = await service.get(run.id)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.finished_reason is not None
    assert "provider exploded" in failed.finished_reason, "the reason has to be actionable"


async def test_a_failing_run_still_persists_the_events_it_emitted(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sentinel is in a `finally`, so a crash does not lose the trail to it.

    This is also what stops the drain task leaking: without the `finally` it would wait
    on a queue nothing will ever close.
    """
    graph = _Graph(events=[("INTAKE", "done"), ("HARVEST", "started")], raises=ValueError("boom"))
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    nodes = [e.node for e in await service.events(run.id) if e.node != EXECUTOR_NODE]
    assert nodes == ["INTAKE", "HARVEST"], "the last events before the crash are the diagnosis"


async def test_an_interrupted_run_parks_for_a_human(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW is not a finish. Marking it `done` would publish past the approval gate."""
    graph = _Graph(interrupted=True, visited=["INTAKE", "REVIEW"])
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    parked = await service.get(run.id)
    assert parked is not None
    assert parked.state == "awaiting_approval"


async def test_a_partial_outcome_is_carried_through_not_flattened_to_done(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run stopped by a cap is `partial`, and saying `done` would hide the cap."""
    graph = _Graph(outcome="partial")
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    result = await service.get(run.id)
    assert result is not None
    assert result.state == "partial"


# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #


async def test_resuming_restores_the_checkpoint_and_counts_the_resumption(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rising `resumed_count` is how instability becomes visible instead of inferred."""
    first = _Graph(interrupted=True, visited=["INTAKE", "HARVEST"])
    ex = _executor(service, first, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")
    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    second = _Graph(outcome="done")
    ex2 = _executor(service, second, monkeypatch)
    ex2.submit(run.id, BUSINESS, run.goal, resume=True)
    await ex2.drain()

    assert second.calls[0]["resume"] is True
    assert second.calls[0]["visited_at_entry"] == ["INTAKE", "HARVEST"], (
        "the second run must START from the first run's checkpoint"
    )
    resumed = await service.get(run.id)
    assert resumed is not None
    assert resumed.resumed_count == 1


async def test_resuming_a_run_with_no_checkpoint_starts_fresh_rather_than_stalling(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing would leave the run stuck forever, which is worse than repeating work.

    It must NOT be counted as a resumption, though: `resumed_count` is a measure of
    work recovered, and incrementing it here would report a recovery that did not happen.
    """
    graph = _Graph(outcome="done")
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal, resume=True)
    await ex.drain()

    assert graph.calls[0]["visited_at_entry"] == [], "a fresh state, not a restored one"
    restarted = await service.get(run.id)
    assert restarted is not None
    assert restarted.state == "done"
    assert restarted.resumed_count == 0


# --------------------------------------------------------------------------- #
# Concurrency and task lifetime
# --------------------------------------------------------------------------- #


async def test_the_executor_holds_a_reference_to_every_in_flight_run(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`create_task` hands back the only strong reference.

    Dropping it lets the loop collect a run mid-flight, which presents as a run that
    silently stops at a random node -- the hardest possible bug to reproduce.
    """
    release = asyncio.Event()

    class _Blocking(_Graph):
        async def __call__(self, state, *, nodes, on_event=None, resume=False):  # type: ignore[no-untyped-def]
            await release.wait()
            return await super().__call__(state, nodes=nodes, on_event=on_event, resume=resume)

    ex = _executor(service, _Blocking(), monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await asyncio.sleep(0)
    assert ex.in_flight == 1

    release.set()
    await ex.drain()
    assert ex.in_flight == 0


async def test_only_the_configured_number_of_runs_execute_at_once(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run holds database sessions and calls a provider; unbounded means both exhausted."""
    live = 0
    peak = 0
    release = asyncio.Event()

    class _Counting(_Graph):
        async def __call__(self, state, *, nodes, on_event=None, resume=False):  # type: ignore[no-untyped-def]
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                await release.wait()
            finally:
                live -= 1
            return await super().__call__(state, nodes=nodes, on_event=on_event, resume=resume)

    monkeypatch.setattr(executor_module, "run_graph", _Counting())
    ex = RunExecutor(service_factory=lambda _bid: service, deps_factory=_deps, max_concurrent=2)
    runs = [await service.start(business_id=BUSINESS, goal=f"goal {i}") for i in range(5)]

    for run in runs:
        ex.submit(run.id, BUSINESS, run.goal)
    for _ in range(20):
        await asyncio.sleep(0)

    assert peak == 2, f"the limit is 2, {peak} ran at once"
    release.set()
    await ex.drain()


# --------------------------------------------------------------------------- #
# The crawl summary, because the checkpoint is rewritten on every node
# --------------------------------------------------------------------------- #


class _Page:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.status = 200
        self.title = "Rohrreinigung Koblenz"
        self.meta_description = "Notdienst"
        self.word_count = 900
        self.h_tree = [type("H", (), {"level": 2, "text": f"H{i}"})() for i in range(12)]
        self.main_text = text


class _Crawl:
    def __init__(self, pages: list[_Page]) -> None:
        self.start_url = "https://mueller.example"
        self.pages = pages
        self.errors = ["one 500"]
        self.truncated = True


def test_the_crawl_summary_bounds_what_enters_the_checkpoint() -> None:
    """A whole site's text, rewritten into JSONB on every node, is not a checkpoint.

    `PageFacts.main_text` is a full page body. Fifty of those, ten times per run, is
    megabytes of writes and a prompt nobody can afford -- so the summary is lossy, and
    which losses are chosen is the point.
    """
    long_text = "x" * 5000
    summary = summarise_crawl(_Crawl([_Page(f"https://x/{i}", long_text) for i in range(40)]))

    assert summary["page_count"] == 40, "the true count survives even when the pages do not"
    assert summary["pages_summarised"] == 25
    assert len(summary["pages"]) == 25
    assert summary["truncated"] is True, "HARVEST turns this into a fact_gap"
    assert summary["error_count"] == 1

    page = summary["pages"][0]
    assert len(page["excerpt"]) == 800
    assert page["excerpt_truncated"] is True
    assert len(page["headings"]) == 8
    assert page["title"] == "Rohrreinigung Koblenz", "titles are what the SEO work reasons about"


def test_the_summary_is_json_serialisable() -> None:
    """`to_checkpoint` has to serialise whatever is in state.

    A pydantic model left in there either raises or silently stringifies, and the
    second is worse: the run resumes with a string where it expects a structure.
    """
    import json

    summary = summarise_crawl(_Crawl([_Page("https://x/1", "short")]))

    json.dumps(summary)  # must not raise
    assert summary["pages"][0]["excerpt_truncated"] is False


class _DepthTrackingService:
    """A `RunService` that records how many event writes overlap.

    Getting to a test that could actually FAIL took three attempts, and the dead ends
    are worth recording:

    * Asserting distinct, ordered sequence numbers passed even with a deliberately
      fire-and-forget sink -- `InMemoryRunStore.next_seq` never awaits, so the writers
      serialise by accident and the race cannot appear.
    * Adding a yield inside `record_event` did not help either: `create_task` bodies
      still ran to completion one at a time under cooperative scheduling.
    * A barrier ("wait until N writers have arrived") would DEADLOCK the correct
      implementation, because correct behaviour here is the *absence* of concurrency.

    Which is the insight: the property is not "the sequence numbers came out right on
    this run", it is "at most one write is ever in flight". That is deterministic,
    directly expresses what the drain is for, and does fail when the drain is removed.
    """

    def __init__(self, inner: RunService) -> None:
        self._inner = inner
        self.depth = 0
        self.peak = 0

    async def record_event(self, run_id, *, node, status, payload):  # type: ignore[no-untyped-def]
        self.depth += 1
        self.peak = max(self.peak, self.depth)
        try:
            await asyncio.sleep(0)  # the window a real database round trip opens
            return await self._inner.record_event(run_id, node=node, status=status, payload=payload)
        finally:
            self.depth -= 1

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)


async def test_only_one_event_write_is_ever_in_flight(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the drain is FOR, stated as the thing that can be observed.

    `next_seq` reads the current maximum and adds one, so two overlapping writers hand
    out the same number. The timeline then has a duplicate, or -- worse -- a hole that a
    resumed run reads as a node which never executed. Serialising the writes is the
    guarantee; this asserts the guarantee rather than one lucky outcome of it.
    """
    tracker = _DepthTrackingService(service)

    class _Burst(_Graph):
        """Emits back-to-back with NO yield between events.

        This detail is what makes the test able to fail. The base stub awaits between
        events, and that single yield is enough for a fire-and-forget sink to interleave
        its tasks without ever OVERLAPPING them -- so the bug hid. A real node emits
        `started` and `done` around synchronous work, with nothing awaited in between,
        which is precisely the window two writers can share.
        """

        async def __call__(self, state, *, nodes, on_event=None, resume=False):  # type: ignore[no-untyped-def]
            self.calls.append(
                {
                    "visited_at_entry": list(state.get("visited") or []),
                    "goal": state.get("goal"),
                    "resume": resume,
                }
            )
            for node, status in self.events:
                if on_event is not None:
                    on_event(node, status, {})
            await asyncio.sleep(0)
            state["outcome"] = self.outcome
            state["visited"] = self.visited
            return type("Result", (), {"state": state, "interrupted": False})()

    graph = _Burst(events=[(f"NODE{i}", "done") for i in range(8)])
    monkeypatch.setattr(executor_module, "run_graph", graph)
    ex = RunExecutor(service_factory=lambda _bid: cast("RunService", tracker), deps_factory=_deps)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    assert tracker.peak == 1, (
        f"{tracker.peak} event writes overlapped; they must be serialised or seq collides"
    )
    seqs = [e.seq for e in await service.events(run.id)]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


# --------------------------------------------------------------------------- #
# The cost ledger is actually flushed
# --------------------------------------------------------------------------- #


class _SpyRecorder:
    """Stands in for `UsageRecorder`, counting flushes instead of writing rows."""

    instances: ClassVar[list[_SpyRecorder]] = []

    def __init__(self, *, run_id: UUID, business_id: UUID) -> None:
        self.run_id = run_id
        self.business_id = business_id
        self.flushes = 0
        self.sunk: list[tuple[Any, Any]] = []
        _SpyRecorder.instances.append(self)

    def sink(self, usage: Any, context: Any) -> None:
        self.sunk.append((usage, context))

    async def flush(self) -> None:
        self.flushes += 1


async def test_the_usage_ledger_is_flushed_on_every_node_boundary(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This wiring was NOT covered until this test existed.

    Removing the executor's per-node `flush()` broke nothing: the recorder's own tests
    call `flush()` directly, and the other executor tests never look at usage. So the one
    line joining the router's sink to the database had no guard at all -- exactly the
    shape of the gap that left `model_usage` unwritten in the first place.

    Per node rather than only at the end, so a run that dies mid-flight still has a
    ledger for the nodes that finished -- which is when somebody is most likely to ask
    what it spent.
    """
    _SpyRecorder.instances.clear()
    monkeypatch.setattr(executor_module, "UsageRecorder", _SpyRecorder)
    graph = _Graph(events=[("INTAKE", "started"), ("INTAKE", "done"), ("HARVEST", "done")])
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    assert len(_SpyRecorder.instances) == 1, "one recorder per run"
    recorder = _SpyRecorder.instances[0]
    assert recorder.run_id == run.id
    assert recorder.business_id == BUSINESS
    # Two node completions ("done"), plus the final flush after the graph returns. The
    # "started" event must NOT trigger one -- there is nothing to write yet.
    assert recorder.flushes == 3


async def test_the_router_gets_the_recorders_sink(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the router has nowhere to report a call and the ledger stays empty.

    Asserted through the deps factory, because that is the seam: the router is built per
    run precisely so it can be handed a sink that knows which run to bill.
    """
    _SpyRecorder.instances.clear()
    monkeypatch.setattr(executor_module, "UsageRecorder", _SpyRecorder)
    seen: list[object] = []

    async def capturing_deps(business_id: UUID, usage_sink: object = None) -> NodeDeps:
        seen.append(usage_sink)
        return NodeDeps(router=object())

    monkeypatch.setattr(executor_module, "run_graph", _Graph())
    ex = RunExecutor(service_factory=lambda _bid: service, deps_factory=capturing_deps)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    assert len(seen) == 1
    assert seen[0] == _SpyRecorder.instances[0].sink, "the sink must be the recorder's"


# --------------------------------------------------------------------------- #
# A long reason must not strand the run
# --------------------------------------------------------------------------- #


def test_a_reason_longer_than_the_column_is_clamped() -> None:
    """`runs.finished_reason` is VARCHAR(255); exceeding it makes the UPDATE raise."""
    clamped = clamp_reason("x" * 900)

    assert clamped is not None
    assert len(clamped) <= MAX_FINISHED_REASON
    assert clamped.endswith("..."), "a reader must be able to tell it was cut"


def test_a_short_reason_is_left_exactly_as_written() -> None:
    """No cosmetic rewriting of a message that already fits."""
    assert clamp_reason("No opportunity met the bar for this business.") == (
        "No opportunity met the bar for this business."
    )
    assert clamp_reason(None) is None


# The end-to-end version of this lives in `tests/db/test_run_store.py`, deliberately.
# `InMemoryRunStore` is a dict with no column width, so a test here passes whether or not
# the reason is clamped -- it cannot fail for its own reason. Only real Postgres raises
# `StringDataRightTruncationError`, which is the failure being guarded against.
