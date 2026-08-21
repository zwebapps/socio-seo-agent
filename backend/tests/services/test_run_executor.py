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
from backend.app.engines.crawl.contract import (
    CrawlErrorInfo,
    CrawlResult,
    Heading,
    PageFacts,
)
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
                # The run's own id, as the graph receives it. Snapshotted here for the
                # same reason as `visited`: this is the value EXPORT puts on every
                # `Actuation`, so reading it after the run would read whatever the stub
                # left behind rather than what the executor supplied.
                "run_id_at_entry": state.get("run_id"),
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
    # `select_runtime` is the seam, not `run_graph`: the executor now picks a runtime
    # per run (LangGraph by default, the builtin driver by configuration), so patching
    # one of the two would leave the other one running for real.
    monkeypatch.setattr(executor_module, "select_runtime", lambda: graph)
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
# Attribution: the run id the graph publishes under
#
# This is the ONE place that knows a run's identity for certain -- the state is built
# for, or restored by, an id the executor was handed. Everything downstream depends on
# it: `nodes._actuate` puts it on every `Actuation`, the ledger writes it to
# `actions.run_id`, and the landing actuator writes it to `content_pieces.run_id`. A
# `None` here is a published page nobody can join back to the run that made it.
# --------------------------------------------------------------------------- #


async def test_a_fresh_run_carries_its_own_id_into_the_state(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state a run starts with knows which run it is.

    Asserted at the graph boundary rather than on `new_state`, because the argument
    being omitted at the ONE call site that has a real run id is exactly the bug: the
    key can exist, the default can be right, and every page can still land unattributed.
    """
    graph = _Graph(outcome="done")
    ex = _executor(service, graph, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")

    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    assert graph.calls[0]["run_id_at_entry"] == str(run.id)


async def test_a_checkpoint_written_before_the_run_id_existed_resumes_attributed(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backward-compatibility case, and the one with the most value in it.

    Nothing migrates a JSONB column, so a run parked at the review gate before this key
    existed has no run id in its checkpoint -- and EXPORT runs ONLY on a resume, so that
    older row is precisely the one that is about to publish. Defaulting to `None` would
    resume it (which is the minimum) and then write another NULL (which is the bug). The
    executor stamps the id it fetched the row BY, so an old checkpoint publishes
    attributed.
    """
    first = _Graph(interrupted=True, visited=["INTAKE", "HARVEST"])
    ex = _executor(service, first, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")
    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    # Exactly what an older row looks like: the key is simply not in the JSON.
    parked = await service.get(run.id)
    assert parked is not None and parked.checkpoint
    parked.checkpoint.pop("run_id", None)
    assert "run_id" not in parked.checkpoint

    second = _Graph(outcome="done")
    ex2 = _executor(service, second, monkeypatch)
    ex2.submit(run.id, BUSINESS, run.goal, resume=True)
    await ex2.drain()

    assert second.calls[0]["visited_at_entry"] == ["INTAKE", "HARVEST"], (
        "the resume must still restore the work the checkpoint holds"
    )
    assert second.calls[0]["run_id_at_entry"] == str(run.id), (
        "a pre-key checkpoint must publish attributed, not merely survive"
    )


async def test_a_checkpoint_naming_a_different_run_is_overwritten_not_trusted(
    service: RunService, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint is data; the id it was fetched by is the fact.

    A `runs.checkpoint` column can hold whatever a hand-run UPDATE, a restore from a
    backup or a copied row put there. Trusting it would attribute this run's publish to
    another run -- a wrong join is worse than a missing one, because it reads as an
    answer.
    """
    first = _Graph(interrupted=True, visited=["INTAKE"])
    ex = _executor(service, first, monkeypatch)
    run = await service.start(business_id=BUSINESS, goal="more leads")
    ex.submit(run.id, BUSINESS, run.goal)
    await ex.drain()

    parked = await service.get(run.id)
    assert parked is not None and parked.checkpoint
    parked.checkpoint["run_id"] = "99999999-9999-4999-8999-999999999999"

    second = _Graph(outcome="done")
    ex2 = _executor(service, second, monkeypatch)
    ex2.submit(run.id, BUSINESS, run.goal, resume=True)
    await ex2.drain()

    assert second.calls[0]["run_id_at_entry"] == str(run.id)


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

    counting = _Counting()
    monkeypatch.setattr(executor_module, "select_runtime", lambda: counting)
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


def _Page(url: str, text: str) -> PageFacts:  # noqa: N802 - reads as the old stub did
    """One crawled page, as the REAL contract.

    These were hand-rolled duck-typed stubs, and they stopped being adequate when
    `summarise_crawl` began running the SEO audit: the audit reads `images` and
    `jsonld_blocks`, which a stub carrying six attributes silently lacked. The stub
    would have kept passing while the production path crashed, because
    `summarise_crawl` reaches everything else through `getattr` with a default.
    Building the real model means the test exercises the shape production passes.
    """
    return PageFacts(
        url=url,
        status=200,
        title="Rohrreinigung Koblenz",
        meta_description="Notdienst",
        word_count=900,
        h_tree=[Heading(level=2, text=f"H{i}") for i in range(12)],
        main_text=text,
    )


def _Crawl(pages: list[PageFacts]) -> CrawlResult:  # noqa: N802 - as above
    return CrawlResult(
        start_url="https://mueller.example",
        pages=pages,
        errors=[
            CrawlErrorInfo(
                url="https://mueller.example/boom",
                code="http_status",
                message="the blog returned 500",
                status=500,
            )
        ],
        truncated=True,
    )


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
    monkeypatch.setattr(executor_module, "select_runtime", lambda: graph)
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

    stub = _Graph()
    monkeypatch.setattr(executor_module, "select_runtime", lambda: stub)
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


# --------------------------------------------------------------------------- #
# Which runtime drives a run
# --------------------------------------------------------------------------- #


def test_the_default_runtime_is_the_langgraph_one() -> None:
    """`langgraph` was a declared dependency nothing imported. The compiled graph is
    now the default path a real run takes, not an alternative sitting beside it."""
    from backend.app.agents.state_graph import run_state_graph
    from backend.app.services.run_executor import select_runtime

    assert select_runtime() is run_state_graph


def test_the_builtin_driver_stays_reachable_by_configuration() -> None:
    """The fallback has to be one environment variable away, or it is not a fallback.
    Equivalence between the two is asserted in `tests/agents/test_graph.py`, which
    runs every branch of the machine against both."""
    from backend.app.agents.graph import run_graph
    from backend.app.services.run_executor import select_runtime

    assert select_runtime("builtin") is run_graph


def test_an_unrecognised_runtime_still_starts_the_run() -> None:
    """Read on the way into a run: refusing to start one over a typo in an optional
    setting would cost a run to save nothing."""
    from backend.app.agents.state_graph import run_state_graph
    from backend.app.services.run_executor import select_runtime

    assert select_runtime("lnagraph") is run_state_graph


# --------------------------------------------------------------------------- #
# What the resolver wires, which is not the same question as what an actuator does
# --------------------------------------------------------------------------- #


def test_publish_page_resolves_to_a_real_actuator_not_a_simulation() -> None:
    """The wiring guard for `publish.page`.

    `tests/db/test_landing_actuator.py` proves the actuator publishes, but it INJECTS
    that actuator — so every one of its assertions would still pass if this resolver
    went back to handing out a `FakeActuator`, and the product would quietly return to
    simulating a page it is perfectly capable of publishing. That regression is
    invisible from the actuator's own tests, so it is asserted here.

    `fake is False` is the load-bearing assertion rather than the class name: what must
    not regress is the OUTCOME reaching `published.simulated` and the Delivery tab, and
    a future real publisher for some other surface must be free to replace the class.
    """
    resolve = executor_module._build_actuator_resolver()

    page = resolve("publish.page")

    assert page is not None, "publish.page must be wired: this app serves the page"
    assert page.action_type == "publish.page"
    assert page.fake is False, (
        "publish.page needs no credential -- this app serves the landing page, so a "
        "simulated outcome here is a lie the Delivery tab would render as delivered"
    )


def test_social_post_still_simulates_so_the_two_stay_distinguishable() -> None:
    """The other half of the same guarantee, and the reason `fake` has to mean something.

    `social.post` is gated on per-platform App Review nobody has (`docs/CHANNELS.md`
    §2), so it must keep reporting `fake`. If both actions ever returned the same flag
    the flag would carry no information, and a run's report could not tell a real
    publish from a simulated post — which is the single thing the actuator layer exists
    to prevent.
    """
    resolve = executor_module._build_actuator_resolver()

    social = resolve("social.post")
    page = resolve("publish.page")

    assert social is not None
    assert social.fake is True
    assert page is not None
    assert social.fake != page.fake


def test_an_unknown_action_type_is_unwired_rather_than_guessed() -> None:
    """EXPORT records `None` as unwired. Guessing an actuator would publish somewhere."""
    assert executor_module._build_actuator_resolver()("publish.telepathy") is None


def test_the_owner_notice_resolves_to_the_transactional_actuator() -> None:
    """The wiring guard for A4, and the reason it needs one.

    EXPORT's notify tests INJECT their actuator, so every one of them would still pass if
    this resolver handed out the marketing `EmailActuator` for `notify.owner` -- and the
    product would go back to what it did for months: an owner notice refused on every run
    for want of a consent basis and an unsubscribe link it must not have.
    """
    from backend.app.actuators.email import EmailActuator

    resolve = executor_module._build_actuator_resolver()

    notice = resolve("notify.owner")

    assert notice is not None, "notify.owner must be wired, or nobody is ever told"
    assert notice.action_type == "notify.owner"
    assert not isinstance(notice, EmailActuator), (
        "notify.owner must NOT be performed by the marketing email actuator: it demands a "
        "consent basis and an in-body unsubscribe link, which is why every owner notice "
        "the node ever built was refused"
    )


def test_the_marketing_type_is_still_wired_and_is_still_a_different_actuator() -> None:
    """Two types, two actuators. `notify.email` is not deleted -- it is just not this."""
    resolve = executor_module._build_actuator_resolver()

    assert resolve("notify.email") is not None
    assert resolve("notify.email") is not resolve("notify.owner")


# --------------------------------------------------------------------------- #
# Who the owner notice is addressed to
#
# `tests/db/test_owner_notice_recipient.py` owns the SQL. These two own the ordering
# and the absence, which are the parts a database cannot demonstrate.
# --------------------------------------------------------------------------- #


async def test_no_sender_configured_is_answered_without_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cheap check first, and asserted rather than assumed.

    With no sending identity there is no notice to send, so reading the account holder's
    address would be a query whose answer cannot be used. The session is replaced with one
    that raises, so this test fails if the order is ever reversed.
    """

    def exploding_session() -> None:
        raise AssertionError("the account address must not be read when no sender is set")

    monkeypatch.setattr(executor_module, "session", exploding_session)

    assert await executor_module._resolve_owner_notice(BUSINESS, env={}) is None


async def test_a_failed_read_means_no_notice_rather_than_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direction is the point: a database that will not answer must not be answered
    with the crawled contact address. EXPORT reports a named note instead."""
    from backend.app.actuators.owner_notice import SENDER_ENV

    def exploding_session() -> None:
        raise RuntimeError("no database today")

    monkeypatch.setattr(executor_module, "session", exploding_session)

    resolved = await executor_module._resolve_owner_notice(
        BUSINESS, env={SENDER_ENV: "SMA <notices@sma.test>"}
    )

    assert resolved is None
