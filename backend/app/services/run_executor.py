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
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import text

from backend.app.actuators import Actuator
from backend.app.actuators.owner_notice import (
    SENDER_ENV,
    OwnerNoticeIdentity,
    owner_notice_sender,
)
from backend.app.agents.graph import GraphResult, run_graph
from backend.app.agents.nodes import NodeDeps, build_nodes
from backend.app.agents.state import AgentState, new_state
from backend.app.agents.state_graph import run_state_graph
from backend.app.core.config import get_settings
from backend.app.db.adapters.action_store import PostgresActionStore
from backend.app.db.session import session
from backend.app.engines.nap import extract_nap_listings
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

#: The ledger label for the retrieval loop's own model calls.
#:
#: Not a graph node, and deliberately not borrowing one: the calls belong to the
#: agentic retrieval loop, which runs inside whichever node asked for grounding, and
#: attributing them to HARVEST would put another node's spend in its column. Same
#: reasoning as `EXECUTOR_NODE` below.
KB_NODE: Final = "KB"

#: The timeline label for the executor's own bookkeeping lines.
#:
#: Not a graph node, and deliberately not borrowing one: `event.node` is a free
#: string, so attributing this to INTAKE would silently interleave a status message
#: with that node's real entries and make the timeline lie about what ran.
EXECUTOR_NODE: Final = "EXECUTOR"


#: One run of the machine. Both runtimes satisfy it, which is the whole point.
GraphRuntime = Callable[..., Awaitable[GraphResult]]


def select_runtime(runtime: str | None = None) -> GraphRuntime:
    """The graph runtime this deployment drives runs with.

    `langgraph` compiles the machine with the library; `builtin` is the hand-written
    driver that predates it. Chosen by configuration rather than by import so the
    fallback is one environment variable away at 3am, and asserted equivalent by
    `tests/agents/test_graph.py`, which runs every branch of the machine against both.

    An unrecognised value takes the LangGraph path rather than raising: this is read
    on the way into a run, and refusing to start a run over a typo in an optional
    setting would be the wrong trade.
    """
    resolved = runtime if runtime is not None else get_settings().agent_runtime
    if resolved == "builtin":
        logger.info("driving runs with the builtin graph driver")
        return run_graph
    return run_state_graph


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
        # The NAP the site publishes about itself, extracted HERE because this is the
        # last point at which the full page facts exist. The summary deliberately
        # drops `jsonld_blocks` and keeps only an excerpt of the body, so an extractor
        # running later would find nothing -- and widening the summary to carry every
        # JSON-LD block would put them in the checkpoint, which is rewritten on every
        # node. A handful of listings is small; the blocks are not.
        "nap_sources": [listing.model_dump(mode="json") for listing in extract_nap_listings(pages)],
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
        retrieve=await _build_retrieve(business_id, router),
        load_memory=_build_load_memory(business_id),
        geo_probe=_build_geo_probe(business_id, router),
        actuator_for=_build_actuator_resolver(),
        actuator_store=PostgresActionStore(),
        owner_notice=await _resolve_owner_notice(business_id),
        # Same rule as `serp_search` and for the same reason: a FAKE search result
        # reaching a draft is worse than no search at all, because afterwards nothing
        # can tell it apart from a real one. GENERATE then reports that the search did
        # not run rather than treating an empty answer as a finding.
        web_search=serp_search,
    )


def _build_actuator_resolver() -> Callable[[str], Actuator | None]:
    """Resolve a dotted action type to the actuator that will perform it.

    A resolver rather than a mapping because the answer is per action type AND per
    deployment: email is a real send the moment `RESEND_API_KEY` is set, while every
    social channel is still gated on per-platform App Review (`docs/CHANNELS.md` §2). So
    one run can legitimately have a real emailer and a simulated publisher, and EXPORT
    has to be able to say which was which.

    **Nothing here silently becomes a no-op.** `notify.owner` -- the transactional type
    EXPORT actually uses -- and `notify.email` are each built by their own credential check,
    which returns a FAKE that names the missing variable when there is no key;
    `social.post` simulates when no `SocialPublisher` is configured,
    and its `Outcome.fake` reaches `published.simulated` and the timeline sentence, so a
    surface cannot report "Published 3 of 3" about three posts that never left the
    process. An unknown action type gets `None`, which EXPORT records as unwired rather
    than guessing.

    `publish.page` is the exception and the only real publish here: this app serves the
    landing page, so there is no credential to be missing and nothing to simulate. That
    makes the mixed case the interesting one -- a single run now carries a REAL published
    page beside a SIMULATED social post, which is exactly the pair the Delivery tab has to
    keep apart.
    """
    from backend.app.actuators.email import build_email_actuator
    from backend.app.actuators.landing import LandingPageActuator
    from backend.app.actuators.owner_notice import ACTION_TYPE as OWNER_NOTICE_ACTION
    from backend.app.actuators.owner_notice import build_owner_notice_actuator
    from backend.app.actuators.social import SocialPostActuator
    from backend.app.db.adapters.connection_store import PostgresConnectionStore
    from backend.app.db.adapters.content_store import PostgresContentStore
    from backend.app.db.adapters.lead_store import PostgresLeadStore

    def resolve(action_type: str) -> Actuator | None:
        if action_type == OWNER_NOTICE_ACTION:
            # The TRANSACTIONAL type, and the only one EXPORT asks for. `notify.email` is
            # the marketing type: it demands a consent basis and an in-body unsubscribe
            # link, which is why it refused every owner notice this node ever built, and
            # why widening its `CONSENT_BASES` was the wrong fix.
            return build_owner_notice_actuator()
        if action_type == "notify.email":
            return build_email_actuator()
        if action_type == "social.post":
            # `publisher=None` is the only configuration that exists: no real
            # `SocialPublisher` is written, because none could be exercised without a
            # platform approval nobody has yet. It simulates and says so.
            #
            # The connection store is still passed, and that matters: the actuator's
            # refusals (no connection, expired, revoked) are evaluated BEFORE the
            # simulate shortcut, so a run against a business with no LinkedIn connection
            # reports "not connected" rather than a cheerful simulated post.
            return SocialPostActuator(connections=PostgresConnectionStore())
        if action_type == "publish.page":
            # The landing page is served by THIS app (`api/pages.py`), so publishing it
            # needs no credential, no third party and no network -- which is why this is
            # the one integration here that is real rather than gated on somebody else's
            # approval queue, and why `LandingPageActuator.fake` is permanently False.
            #
            # It was simulated only because nothing called `publish_landing_page`, and the
            # cost of that gap was the whole conversion chain: no landing `content_pieces`
            # row was ever written by a run, `GET /p/{id}` could serve nothing, and no
            # tracked short link was minted outside a hand-written test.
            return LandingPageActuator(
                content_store=PostgresContentStore(), link_store=PostgresLeadStore()
            )
        return None

    return resolve


#: The authenticated account holder's address for one business.
#:
#: `users`/`businesses` carry no `business_id` and no RLS policy -- `businesses` IS the
#: tenant table -- so this is read on the unscoped session, the same reasoning
#: `lead_store.business_for_owner` documents. Inactive owners are excluded: a deactivated
#: account is not somebody we mail.
_ACCOUNT_EMAIL = text(
    """
    SELECT u.email
    FROM businesses AS b
    JOIN users AS u ON u.id = b.owner_id
    WHERE b.id = :business_id AND u.is_active
    """
)


async def _resolve_owner_notice(
    business_id: UUID, env: Mapping[str, str] | None = None
) -> OwnerNoticeIdentity | None:
    """Who EXPORT's owner notice goes to, and who it comes from -- or None.

    **This is the security half of the owner-notice fix.** The node used to take the
    recipient from `state["dna"]["email"]`, which is a contact address extracted from the
    business's own homepage by the crawler. That makes a page we do not control the
    authority over where our operational mail goes: change the address in an Impressum and
    the next run's notice follows it. Our own transactional mail must go to the
    AUTHENTICATED account, so it is resolved here, from `businesses.owner_id -> users.email`,
    and injected -- which also keeps the node database-free and its tests hermetic.

    `None` is returned when either half is missing, and EXPORT then reports a named note
    rather than skipping. Both halves, because a sender with no recipient (or the reverse)
    is not half a notifier: it is one the actuator would refuse. The refusal would be
    correct and the run would carry a confusing failure instead of an honest "not
    configured", which is the distinction `NO_ACTUATOR_NOTE` already exists to preserve.
    """
    sender = owner_notice_sender(env)
    if sender is None:
        logger.info("%s is not set, so no owner notice can be sent; EXPORT will say so", SENDER_ENV)
        return None
    try:
        async with session() as db:
            row = (await db.execute(_ACCOUNT_EMAIL, {"business_id": str(business_id)})).first()
    except Exception:
        # Not fatal, and the direction matters: a failed read means NO notice, never a
        # guess. The alternative -- falling back to whatever address the crawler found --
        # is the defect this function exists to remove.
        logger.exception("could not resolve the account address for business %s", business_id)
        return None
    if row is None:
        logger.warning("business %s has no active owner, so nobody can be told", business_id)
        return None
    return OwnerNoticeIdentity(account_email=str(row[0]), sender=sender)


def _build_geo_probe(business_id: UUID, router: ModelRouter) -> Callable[..., Awaitable[Any]]:
    """AI answer-engine visibility, as a compact fact HARVEST can carry.

    `geo.probe` has been in HARVEST's allowlist since the allowlist was written and
    was implemented by nothing, so share of voice was measurable only from the
    standalone probe screen and never as evidence a run could reason about -- which is
    the point of measuring it: an opportunity is worth more when the business is
    ABSENT from the answers people already get.

    Returns a SUMMARY, never the raw answers. The checkpoint is rewritten on every
    node, so anything in state is paid for repeatedly, and a probe's answers are
    paragraphs of model prose.

    `using_fake_provider` and the caveats travel with the number, because a share of
    voice measured against a deterministic fake is not a measurement and a screen that
    shows the figure without the caveat presents it as one. `no_answer` stays out of
    the denominator inside the service: a model outage recorded as brand absence is
    the difference between a measurement and a fabrication.
    """

    async def geo_probe(dna: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        from backend.app.db.adapters.probe_store import PostgresProbeStore
        from backend.app.engines.geo import BrandIdentity, build_prompt_set
        from backend.app.services.geo_service import probe_visibility

        name = str(dna.get("name") or "").strip()
        city = str(dna.get("city") or "").strip()
        if not name or not city:
            # `build_prompt_set` refuses a blank brand or city, and it is right to: a
            # prompt set built around an empty string still runs, still costs money
            # and measures nothing. Raising here lets HARVEST name the gap.
            raise ValueError(
                "an AI-visibility probe needs a business name and a city; this "
                "business profile has " + ("no name" if not name else "no city")
            )

        prompts = build_prompt_set(
            business_name=name,
            city=city,
            services=[str(service) for service in (dna.get("services") or [])],
            locale=str(dna.get("locale") or "de"),
        )
        report = await probe_visibility(
            business_id=business_id,
            brand=BrandIdentity(
                name=name,
                aliases=[str(alias) for alias in (dna.get("aliases") or [])],
                domains=[str(dna["website"])] if dna.get("website") else [],
            ),
            prompts=prompts,
            store=PostgresProbeStore(),
            router=router,
        )
        sov = report.share_of_voice
        return {
            # `headline` rather than a bare percentage, and that is the engine's own
            # rule enforced at the call site: it is a rendering that physically cannot
            # print a share without its denominator, so a model reading this evidence
            # cannot write "22% of AI answers" off the back of nine usable samples.
            "headline": sov.headline,
            "mention_share_pct": sov.mention_share_pct,
            "unprompted_mention_share_pct": sov.unprompted_mention_share_pct,
            "usable_answers": sov.usable_answers,
            "no_answer_count": sov.no_answer_count,
            "probes_planned": report.probes_planned,
            "probes_run": report.probes_run,
            "models": report.models,
            "using_fake_provider": report.using_fake_provider,
            "caveats": list(report.caveats),
        }

    return geo_probe


def _build_load_memory(business_id: UUID) -> Callable[[], Awaitable[list[str]]]:
    """Business memory, as the prompt lines INTAKE carries for the whole run.

    Wired unconditionally, unlike retrieval and search. There is no honesty problem to
    manage here: this is our own data, not a provider that might be faked, so an empty
    result means "this business has remembered nothing yet" and nothing else. INTAKE
    already degrades to a `fact_gap` if the read fails.

    Until this existed, `state["remembered"]` was threaded correctly into the system
    prompt and nothing ever populated it in a real run: the `/memory` screen wrote
    preferences that no model ever saw, which made "the agent updates persistent
    business preferences from explicit feedback" true of the test suite only.
    """

    async def load_memory() -> list[str]:
        from backend.app.db.session import business_session
        from backend.app.services.memory_service import load_memory as read_memory
        from backend.app.services.memory_service import to_prompt_lines

        async with business_session(business_id) as session:
            memory = await read_memory(business_id, session=session)
        return to_prompt_lines(memory)

    return load_memory


async def _build_retrieve(
    business_id: UUID, router: ModelRouter
) -> Callable[..., Awaitable[Any]] | None:
    """Agentic retrieval over this business's own documents -- or None if it has none.

    **Wired only when there is something to retrieve**, which is the same rule this
    function applies to the search provider one screen up, and for the same reason. A
    retriever over an empty store answers "nothing relevant", and that reads as a
    business whose own material had nothing to say about the topic. `None` instead
    makes HARVEST record "uploaded documents" in `fact_gaps`, which the review screen
    shows the customer under what the work was written WITHOUT. One is a measurement;
    the other is a fabrication with a friendly face.

    The count is one indexed-chunk query per run, which is cheap next to a run.
    """
    from backend.app.db.adapters.chunk_store import PgVectorChunkStore
    from backend.app.db.adapters.document_store import PostgresDocumentStore
    from backend.app.llm.embedder import RouterEmbedder
    from backend.app.services.kb_service import RETRIEVAL_PROMPT_VERSION
    from backend.app.services.kb_service import retrieve as retrieve_chunks

    try:
        indexed = await PostgresDocumentStore(business_id).chunk_count()
    except Exception:
        logger.exception("could not count indexed chunks; leaving retrieval unwired")
        return None

    if not indexed:
        return None

    embedder = RouterEmbedder(router=router)
    store = PgVectorChunkStore()

    async def retrieve(question: str, **kwargs: Any) -> Any:
        return await retrieve_chunks(
            question,
            business_id=business_id,
            router=router,
            embedder=embedder,
            store=store,
            # So the loop's cheap calls are ATTRIBUTABLE. Found by driving a real run
            # the first time retrieval was wired in: the loop makes three calls per
            # attempt and every one wrote a `model_usage` row with an empty `node`, so
            # eighteen of twenty-six ledger rows belonged to no step. `KB` rather than a
            # graph node name, because the calls belong to the retrieval loop and
            # borrowing HARVEST's name would put another node's spend in its column --
            # the same reason `EXECUTOR` is not a node.
            trace={"node": KB_NODE, "prompt_version": RETRIEVAL_PROMPT_VERSION},
            **kwargs,
        )

    return retrieve


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
            drive = select_runtime()
            result = await drive(state, nodes=build_nodes(deps), on_event=sink, resume=resume)
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
                    "geo.probe": deps.geo_probe is not None,
                    "web_search": deps.web_search is not None,
                    # EXPORT's two, and they need BOTH halves: `actuate()` claims an
                    # idempotency key in the ledger before it calls anything, so an
                    # actuator with no store is not a publisher. Reported here for the
                    # same reason as the rest -- a run that published nothing because
                    # this deployment has no integration looks, afterwards, exactly
                    # like a run whose posts were rejected.
                    "publish": deps.actuator_for is not None and deps.actuator_store is not None,
                    # Notification needs a THIRD thing the publish path does not: the
                    # account holder's address plus a sending identity. Reported
                    # separately because "no notifier" and "no address to notify" send
                    # somebody to fix different files.
                    "notify": (
                        deps.actuator_for is not None
                        and deps.actuator_store is not None
                        and deps.owner_notice is not None
                    ),
                }.items()
                if is_wired
            )
            summary = "tools wired: " + (", ".join(wired) or "none")
            if deps.retrieve is not None:
                # Retrieval with hash-arithmetic vectors is not retrieval, and a
                # timeline that does not say so presents it as though it were. Same
                # rule as the search provider: name the fake rather than hide it.
                from backend.app.llm.embedder import RouterEmbedder

                if RouterEmbedder(router=deps.router).using_fake:
                    summary += " (embeddings: FAKE provider — vectors are arithmetic "
                    summary += "over a hash, so retrieval quality is not meaningful)"
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
                payload={"summary": summary},
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
            # Channels come from the ROW, not from a parameter, and deliberately so.
            # `submit` is also what the scheduled worker calls, and a job payload that
            # has to carry the channel set is a payload that can disagree with the row
            # it names. One primary-key read per fresh run buys "the run targets what
            # the row says it targets", which stays true however the run was started.
            record = await service.get(run_id)
            return new_state(
                business_id=business_id,
                goal=goal,
                run_id=run_id,
                channels=record.channels if record is not None else None,
            )

        restored = await service.restore(run_id)
        if restored is None:
            # Nothing to resume from. Starting fresh is the right answer -- refusing
            # would leave the run stuck forever -- but it must be counted, because a
            # resume that silently restarts has thrown away the work it was meant to
            # preserve.
            logger.warning("run %s has no checkpoint to resume from; starting fresh", run_id)
            return new_state(business_id=business_id, goal=goal, run_id=run_id)

        await service.mark_resumed(run_id)
        # Stamped, not read. This is the ONE place that knows the run's identity for
        # certain -- the checkpoint was fetched BY this id -- and a checkpoint written
        # before `run_id` existed has none at all. Overwriting rather than defaulting is
        # what makes those old rows publish attributed: EXPORT runs only on a resume, so
        # a state restored here is the state that actuates, and a `None` left in place
        # would put another NULL in `content_pieces.run_id` for every pre-key run
        # anybody approves from now on.
        return cast("AgentState", {**restored, "run_id": str(run_id)})

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
