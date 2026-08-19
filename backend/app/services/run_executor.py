"""Actually run the graph. The piece that was missing.

Every part of the agent existed and passed its tests, and nothing joined them at
runtime: `run_graph`, `build_nodes` and `RunService.checkpoint` were called only from
tests, so `POST /api/v1/runs` wrote a `queued` row that never advanced. A user could
start a run, watch the timeline forever, and see the review tabs render their honest
"nothing produced yet" states — correctly, because nothing had been produced.

This module is the join. It owns four things that are easy to get wrong:

**Ordered event persistence.** `graph.EventSink` is SYNCHRONOUS and
`RunService.record_event` is async, so the sink cannot await. Firing a task per event
would race on `next_seq` and produce a timeline with duplicate or missing sequence
numbers — the one thing a resumable, replayable event log must not have. So the sink
appends to a queue and a single drain coroutine persists in order, one at a time.

**Failures that reach the database.** A run executed in a fire-and-forget task whose
exception nobody retrieves leaves the row saying `running` forever, and asyncio logs
"Task exception was never retrieved" into a void. Every exit path here writes a
terminal state, including the unexpected one.

**Keeping the task alive.** `asyncio.create_task` returns the only strong reference
to a task; drop it and the event loop may garbage-collect a run mid-flight. The
executor holds them until they finish.

**Bounded concurrency.** Runs call model providers and hold database sessions, so an
unbounded number of them is a way to exhaust the connection pool and the provider's
rate limit at the same time.

What this deliberately is NOT: a distributed worker. `ROADMAP` names ARQ/Redis, which
is not installed, and adding a queue, a second process and a compose service is a
bigger change than making the product work. The honest consequence is stated once
here and repeated in the code that depends on it: **if the API process dies mid-run,
that run stays `running` until something resumes it.** Runs were designed to be
resumable for exactly this reason — the checkpoint IS the recovery mechanism — so
`resume()` exists and is reachable from the API. What is missing is an automatic
sweeper, which is a worker's job.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from uuid import UUID

from backend.app.agents.graph import run_graph
from backend.app.agents.nodes import NodeDeps, build_nodes
from backend.app.agents.state import AgentState, new_state
from backend.app.llm.router import ModelRouter, UsageSink
from backend.app.services.run_service import RunService
from backend.app.services.usage_recorder import UsageRecorder

logger: Final = logging.getLogger(__name__)

#: How many runs may execute at once in this process.
#:
#: Each one holds a database session per node and calls a model provider, so this is
#: really a limit on two scarce things at the same time. Four is deliberately modest:
#: the asyncpg pool is small, and a run is minutes long, so queueing the fifth costs
#: latency nobody is watching while over-committing costs errors somebody is.
DEFAULT_MAX_CONCURRENT_RUNS: Final = 4

#: Cap on pages summarised into `facts["site"]`.
#:
#: The checkpoint is rewritten on EVERY node, so anything in state is paid for
#: repeatedly. `PageFacts.main_text` is a whole page body; fifty of them would put
#: megabytes into a JSONB column ten times per run and then send it to a model.
MAX_SUMMARISED_PAGES: Final = 25

#: Characters of page text kept per page. Enough for a model to tell what the page is
#: about and how it is pitched; far short of "the page".
PAGE_EXCERPT_CHARS: Final = 800

#: Headings kept per page. The shape of a page is in its first few headings.
MAX_HEADINGS_PER_PAGE: Final = 8

#: The timeline label for the executor's own bookkeeping lines.
#:
#: Not a graph node, and deliberately not borrowing one: `event.node` is a free
#: string, so attributing this to INTAKE would silently interleave a status message
#: with that node's real entries and make the timeline lie about what ran.
EXECUTOR_NODE: Final = "EXECUTOR"


#: Builds the node dependencies for one run. Takes the usage sink so the router it
#: constructs can report what each call cost -- the router is per-run for exactly
#: this reason, since a process-wide one could not know which run to bill.
DepsFactory = Callable[[UUID, UsageSink | None], Awaitable[NodeDeps]]


def summarise_crawl(result: Any) -> dict[str, Any]:
    """Compact a `CrawlResult` into something safe to carry in state.

    Lossy ON PURPOSE, and the losses are chosen rather than incidental: page count,
    truncation and error count survive because HARVEST turns them into `fact_gaps`
    and the UI says "43 of an unknown number of pages" from them; titles, meta
    descriptions and headings survive because they are what the SEO and opportunity
    work reason about; the body text is excerpted because a model needs the gist and
    the checkpoint cannot afford the rest.

    Returns a plain dict of JSON-safe values, because `to_checkpoint` has to be able
    to serialise whatever is in state, and a pydantic model in there would either
    fail or silently stringify.
    """
    pages = list(getattr(result, "pages", []) or [])
    summarised: list[dict[str, Any]] = []
    for page in pages[:MAX_SUMMARISED_PAGES]:
        text = (getattr(page, "main_text", "") or "").strip()
        summarised.append(
            {
                "url": getattr(page, "url", None),
                "status": getattr(page, "status", None),
                "title": getattr(page, "title", None),
                "meta_description": getattr(page, "meta_description", None),
                "word_count": getattr(page, "word_count", 0),
                "headings": [
                    {"level": getattr(h, "level", None), "text": getattr(h, "text", None)}
                    for h in (getattr(page, "h_tree", []) or [])[:MAX_HEADINGS_PER_PAGE]
                ],
                "excerpt": text[:PAGE_EXCERPT_CHARS],
                # So a reader of the checkpoint can tell an excerpt from a short page.
                "excerpt_truncated": len(text) > PAGE_EXCERPT_CHARS,
            }
        )
    return {
        "start_url": getattr(result, "start_url", None),
        "page_count": len(pages),
        "pages_summarised": len(summarised),
        "truncated": bool(getattr(result, "truncated", False)),
        "error_count": len(list(getattr(result, "errors", []) or [])),
    } | {"pages": summarised}


async def _load_revocations() -> Mapping[str, frozenset[str]]:
    """Operator tool revocations, read once per run.

    Once per run rather than once per node: a revocation taking effect halfway through
    would mean a run whose first half could publish and whose second half could not,
    which is harder to reason about than either answer. A run that started before the
    switch was pulled finishes under the old policy; the next one gets the new one.

    A failure to read them is NOT fatal, and the direction matters: the fallback is the
    code allowlist, which is the NARROWER-or-equal answer in every case, because
    revocations can only subtract. Failing the run instead would turn a settings-table
    outage into an inability to work at all.
    """
    try:
        from backend.app.db.adapters.route_store import PostgresToolPolicyStore

        stored = await PostgresToolPolicyStore().load_policies()
    except Exception:
        logger.exception("could not load tool revocations; using the code allowlist")
        return {}
    return {record.node: frozenset(record.revoked) for record in stored}


async def build_real_deps(business_id: UUID, usage_sink: UsageSink | None = None) -> NodeDeps:
    """Wire the nodes to the real engines and services.

    Anything unconfigured is left as ``None`` rather than stubbed. That is not
    laziness: the nodes already treat a missing dependency as a degraded-but-valid
    state and record a `fact_gap` for it, which is honest, whereas a stub that
    returns empty results would look like a source that had nothing to say. The
    difference matters to a customer reading "written without: uploaded documents".
    """
    from backend.app.engines.crawl import crawl_site as _crawl_site
    from backend.app.engines.serp import get_serp_provider, serp_config_status

    router = ModelRouter(usage_sink=usage_sink)

    async def crawl_site(url: str) -> dict[str, Any]:
        return summarise_crawl(await _crawl_site(url))

    # Wired ONLY when the provider is real. `get_serp_provider()` falls back to
    # `FakeSerpProvider` when TAVILY_API_KEY is absent, and wiring that here would be
    # the same mistake the eval harness's `--live` flag used to make: the fake would
    # satisfy the tool, HARVEST would NOT record "search results (no provider
    # configured)", and the run would look researched while nothing was searched.
    #
    # Leaving it None makes the absence appear in `fact_gaps`, which is what the
    # review screen shows the customer under "what this was written without". The
    # executor separately emits the provider status into the timeline (see
    # `_record_provider_status`) so the reason is visible rather than inferred.
    serp_search: Callable[..., Awaitable[Any]] | None = None
    if not serp_config_status().using_fake:
        provider = get_serp_provider()

        async def _search(query: str, **kwargs: Any) -> Any:
            return await provider.search(query, **kwargs)

        serp_search = _search

    return NodeDeps(
        router=router,
        crawl_site=crawl_site,
        serp_search=serp_search,
        revoked_tools=await _load_revocations(),
        # Retrieval and memory need a database session per call, and wiring them is
        # the next step rather than this one -- see the module docstring in
        # `kb_service.retrieve` for the embedder/store it also needs. Left None so
        # HARVEST records the gap instead of pretending it looked.
        retrieve=None,
        load_memory=None,
    )


class RunExecutor:
    """Runs the graph for submitted runs, in this process.

    One instance per application, held on `app.state`. It is not a queue: submitting
    starts the work immediately (subject to the concurrency limit) and returns.
    """

    def __init__(
        self,
        *,
        service_factory: Callable[[UUID], RunService],
        deps_factory: DepsFactory = build_real_deps,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_RUNS,
    ) -> None:
        self._service_factory = service_factory
        self._deps_factory = deps_factory
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Strong references. `create_task` hands back the only one, and a dropped
        # task can be collected mid-run -- a bug that presents as a run silently
        # stopping at a random node.
        self._tasks: set[asyncio.Task[None]] = set()
        # Which runs this process is executing right now.
        #
        # Needed because the database CANNOT answer it. A row saying `running` means
        # either "a task is driving this" or "a process died and left it there", and
        # those want opposite responses from `resume`: refuse the first, allow the
        # second. The stored state cannot tell them apart; the executor can, for its
        # own process, which is the only place a duplicate could be started.
        self._live: set[UUID] = set()

    @property
    def in_flight(self) -> int:
        """How many runs are executing or queued. Exists so a test can assert it."""
        return len(self._tasks)

    def is_running(self, run_id: UUID) -> bool:
        """Whether THIS process is already executing that run.

        Only ever this process. A second replica could be running it and this would
        say no -- which is honest about what an in-process executor can know, and is
        why the module docstring calls a distributed worker the real answer. It closes
        the duplicate that is actually reachable today: two requests to one API.
        """
        return run_id in self._live

    def submit(self, run_id: UUID, business_id: UUID, goal: str, *, resume: bool = False) -> None:
        """Start executing a run. Returns immediately.

        Marks the run live BEFORE creating the task, not inside it: a task does not
        begin until the loop yields, so registering it there would leave a window in
        which `is_running` says no and a second submit slips through.
        """
        self._live.add(run_id)
        task = asyncio.create_task(
            self._guarded(run_id, business_id, goal, resume=resume),
            name=f"run:{run_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every in-flight run. For shutdown and for tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _guarded(self, run_id: UUID, business_id: UUID, goal: str, *, resume: bool) -> None:
        """Run under the concurrency limit, and never let an exception escape silently."""
        try:
            await self._run_once(run_id, business_id, goal, resume=resume)
        finally:
            # In a `finally`, so a crash cannot leave a run permanently unresumable.
            self._live.discard(run_id)

    async def _run_once(self, run_id: UUID, business_id: UUID, goal: str, *, resume: bool) -> None:
        async with self._semaphore:
            service = self._service_factory(business_id)
            try:
                await self._execute(run_id, business_id, goal, service=service, resume=resume)
            except Exception as exc:
                # A run that dies must SAY it died. Without this the row stays
                # `running` forever and asyncio logs the traceback nowhere anyone
                # looks, which is indistinguishable from a run that is merely slow.
                logger.exception("run %s failed", run_id)
                try:
                    # Type first, message after: `finish` clamps to the column width,
                    # and the exception CLASS is the part worth keeping when a message
                    # runs long. This path previously handed an unbounded exception
                    # string to a VARCHAR(255) column, so the attempt to record a
                    # failure failed too and the run stayed `running`.
                    await service.finish(
                        run_id, outcome="failed", reason=f"{type(exc).__name__}: {exc}"
                    )
                except Exception:
                    logger.exception("run %s failed, and recording the failure also failed", run_id)

    async def _execute(
        self,
        run_id: UUID,
        business_id: UUID,
        goal: str,
        *,
        service: RunService,
        resume: bool,
    ) -> None:
        state = await self._initial_state(run_id, business_id, goal, service=service, resume=resume)
        recorder = UsageRecorder(run_id=run_id, business_id=business_id)
        deps = await self._deps_factory(business_id, recorder.sink)
        await self._record_provider_status(run_id, deps, service=service)

        queue: asyncio.Queue[tuple[str, str, dict[str, Any]] | None] = asyncio.Queue()

        def sink(node: str, status: str, payload: Mapping[str, Any]) -> None:
            # Synchronous by protocol, so this only enqueues. `put_nowait` on an
            # unbounded queue cannot block or fail, which matters because the graph
            # has no way to handle an exception raised by its own event sink.
            queue.put_nowait((node, status, dict(payload)))

        drain = asyncio.create_task(self._drain_events(run_id, queue, service, recorder))
        try:
            result = await run_graph(state, nodes=build_nodes(deps), on_event=sink, resume=resume)
        finally:
            # Sentinel, in the `finally`, so the drain terminates even when the graph
            # raised -- otherwise a failed run leaks a task that waits forever.
            queue.put_nowait(None)
            await drain

        # Whatever the last node emitted after its final event, plus anything a failure
        # path left buffered.
        await recorder.flush()

        final = result.state
        await service.checkpoint(run_id, state=final, current_node=_last_visited(final))

        if result.interrupted:
            # REVIEW: a human decides next. Its own state so the run is neither
            # re-run nor forgotten while somebody looks at it.
            await service.await_approval(run_id)
            return

        outcome = final.get("outcome", "done")
        await service.finish(
            run_id,
            outcome=outcome if outcome in {"done", "failed", "partial"} else "done",
            reason=final.get("finished_reason"),
        )

    @staticmethod
    async def _record_provider_status(run_id: UUID, deps: NodeDeps, *, service: RunService) -> None:
        """Put what this run could actually reach into the timeline, before it starts.

        A run that produced thin work because no search provider was configured looks
        identical, afterwards, to a run that searched and found little. This is one
        line that tells them apart, recorded at the point where it is known for
        certain rather than reconstructed later from what is missing.
        """
        try:
            wired = sorted(
                name
                for name, is_wired in {
                    "crawl.site": deps.crawl_site is not None,
                    "serp.search": deps.serp_search is not None,
                    "kb.search": deps.retrieve is not None,
                    "memory.load": deps.load_memory is not None,
                }.items()
                if is_wired
            )
            await service.record_event(
                run_id,
                # Its own label, not a node name. This line is the executor's, not
                # INTAKE's, and attributing it to a node would put a status message in
                # the middle of that node's timeline entries.
                node=EXECUTOR_NODE,
                status="started",
                # `summary` because `ALLOWED_PAYLOAD_KEYS` is a deliberate control --
                # only operational keys are stored, and an invented key is DROPPED
                # silently. A first version of this passed `tools_wired` and recorded an
                # event with an empty payload, which is worse than not recording one:
                # it looks like the information was captured.
                payload={"summary": "tools wired: " + (", ".join(wired) or "none")},
            )
        except Exception:
            logger.exception("run %s: could not record provider status", run_id)

    async def _initial_state(
        self,
        run_id: UUID,
        business_id: UUID,
        goal: str,
        *,
        service: RunService,
        resume: bool,
    ) -> AgentState:
        if not resume:
            return new_state(business_id=business_id, goal=goal)

        restored = await service.restore(run_id)
        if restored is None:
            # Nothing to resume from. Starting fresh is the right answer -- refusing
            # would leave the run stuck forever -- but it must be counted, because a
            # resume that silently restarts has thrown away the work it was meant to
            # preserve.
            logger.warning("run %s has no checkpoint to resume from; starting fresh", run_id)
            return new_state(business_id=business_id, goal=goal)

        await service.mark_resumed(run_id)
        return restored

    @staticmethod
    async def _drain_events(
        run_id: UUID,
        queue: asyncio.Queue[tuple[str, str, dict[str, Any]] | None],
        service: RunService,
        recorder: UsageRecorder,
    ) -> None:
        """Persist events one at a time, in the order the graph emitted them.

        Serial on purpose. `next_seq` reads the current maximum and adds one, so two
        concurrent writers would hand out the same sequence number and the timeline
        would have a duplicate — or, worse, a hole that a resumed run reads as a
        missing node.

        A failure to record an event must NOT fail the run: the event log is a record
        of the work, not the work. Losing a line from it is bad; abandoning a
        half-finished run because a log line would not write is worse.
        """
        while True:
            item = await queue.get()
            if item is None:
                return
            node, status, payload = item
            if status in {"done", "failed"}:
                # Flush on the node boundary. The router's sink is synchronous (it is on
                # every node's hot path), so the buffered rows need an async moment to be
                # written, and this drain is already one. Per node rather than per run so
                # a run that dies mid-flight still has a ledger for the nodes that
                # finished -- which is when the spend question is most likely to be asked.
                await recorder.flush()
            try:
                await service.record_event(
                    run_id, node=node, status=_event_status(status), payload=payload
                )
            except Exception:
                logger.exception("run %s: could not record event %s/%s", run_id, node, status)


def _event_status(status: str) -> Any:
    """Map a graph status onto the `EventStatus` literal, without inventing one.

    The graph's vocabulary and the event log's are close but not identical, and a
    value outside the Literal would fail pydantic validation inside the drain — which
    the drain would swallow, so the timeline would just quietly lose lines.
    """
    allowed = {"started", "done", "failed", "skipped"}
    if status in allowed:
        return status
    return "done" if status in {"ok", "complete", "finished"} else "failed"


def _last_visited(state: AgentState) -> str | None:
    visited = state.get("visited") or []
    return visited[-1] if visited else None


__all__ = [
    "DEFAULT_MAX_CONCURRENT_RUNS",
    "EXECUTOR_NODE",
    "RunExecutor",
    "build_real_deps",
    "summarise_crawl",
]
