"""The real graph nodes, wired to the engines, the services and the model router.

Read alongside docs/AGENT_RUNTIME.md section 3, which tabulates every node with its
model tier, its tools and its failure mode.

Four invariants this package exists to hold:

* **HARVEST and VALIDATE call no model.** They are the trustworthy half of the
  pipeline precisely because they are deterministic — one gathers evidence, the other
  measures the result, and neither can hallucinate.
* **A failed fact source degrades the run and is NAMED.** `fact_gaps` is what lets the
  UI say "generated without live research" instead of quietly implying research
  happened.
* **Every tool call goes through the node's allowlist.** No node reaches an engine
  or accepts a model's tool call directly: both go through a
  :class:`~backend.app.agents.tools.NodeToolbox` built from `tools.NODE_TOOLS`, so
  the capability table in docs/AGENT_RUNTIME.md section 3 is enforced rather than
  merely described. A refusal is logged and lands in `state["errors"]`.
* **A banned claim stops publication, deterministically.** The claim list is in the
  system prompt too, but a prompt is a request: untrusted page text can argue a
  model out of it, and a model can simply forget. VALIDATE re-checks the finished
  draft with the `claims` engine, and the graph will not carry a failing verdict
  to REVIEW.

Every dependency is injected through :class:`NodeDeps`, so the whole set runs with no
network, no database and no model in tests. Nodes RECEIVE a router; they never build
one, which is why making the provider and model choice configurable elsewhere needs
no change here.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, get_args

from backend.app.agents.nodes.prompts import (
    GENERATE_TOOL,
    LANDING_TOOL,
    OPPORTUNITY_TOOL,
    PLAN_TOOL,
    PROMPT_VERSION,
    REPACK_TOOL,
    WEB_SEARCH_TOOL,
    fence,
    system,
)
from backend.app.agents.state import AgentState, NodeError
from backend.app.agents.tools import (
    CHANNEL_VALIDATE,
    CLAIMS_CHECK,
    CRAWL_SITE,
    GEO_PROBE,
    KB_SEARCH,
    LANDING_CHECK,
    MEMORY_LOAD,
    NAP_AUDIT,
    SEO_SCORE,
    SERP_SEARCH,
    WEB_SEARCH,
    NodeToolbox,
    ToolImpl,
    ToolNotAllowedError,
)
from backend.app.engines.channel import (
    CHANNEL_SPECS,
    ChannelSpec,
    canonical_channel,
    enforce_hashtags,
    hard_char_limits,
)
from backend.app.engines.claims import ClaimCheckRequest, check_claims
from backend.app.engines.landing import (
    ChannelCta,
    FormField,
    FormFieldName,
    LandingCheckRequest,
    LandingPageSpec,
    ProofPoint,
    check_landing_page,
)
from backend.app.engines.nap import (
    CanonicalNap,
    DirectoryListing,
    RawNap,
    audit_nap,
    normalise_nap,
)
from backend.app.engines.seo import SeoScoreRequest, score_page
from backend.app.engines.serp import (
    SerpPage,
    discover_competitors,
    expand_keywords,
)
from backend.app.llm import Message, Role, TaskClass, ToolCall, ToolSpec
from backend.app.services.link_service import KNOWN_CHANNELS

logger = logging.getLogger(__name__)

Node = Callable[[AgentState], Awaitable[dict[str, Any]]]

#: Hard character ceilings per channel. Length is arithmetic, so Python enforces it
#: after generation rather than asking the model to count -- it will get it wrong, and
#: the platform will reject the post.
#:
#: DERIVED, not declared. This used to be its own table and it disagreed with the one
#: the eval harness graded against -- different channel names, and LinkedIn's 3,000
#: was a plain ceiling here and a *hard* max there. It is now the platform reject
#: thresholds out of `engines/channel/specs.py`, which the rubric reads too. Note what
#: that changed: Facebook's ceiling is its real 63,206 rather than the 2,000 that used
#: to live here, because 2,000 is an editorial target and truncating good copy at a
#: target is not enforcement. Being over the target is REPORTED instead (see `repack`).
CHANNEL_LIMITS: Mapping[str, int] = hard_char_limits()

DEFAULT_CHANNELS: tuple[str, ...] = ("linkedin", "facebook", "instagram")

#: The bio-link hub always gets its own CTA, whatever channels a run renders posts
#: for. docs/CHANNELS.md section 1: an Instagram feed caption and a TikTok caption
#: cannot carry a clickable link at all, so ``/go/{slug}`` is the ENTIRE conversion
#: path for those surfaces -- and a hub with no CTA on it is an empty page in
#: somebody's bio.
HUB_CHANNEL: str = "link_hub"

#: The form field names a generated form may use, derived from the `landing` engine's
#: own contract rather than repeated here. A second copy of this list is exactly how a
#: generated form and the endpoint that receives it would drift apart -- and the
#: failure mode is silent: the visitor fills the field in and the submission is
#: refused.
_LEGAL_FORM_FIELDS: Final[frozenset[str]] = frozenset(get_args(FormFieldName.__value__))

#: How many keyword seeds HARVEST searches. Each is a provider call, so this is the
#: main lever on harvest cost.
MAX_SEED_SEARCHES = 3


@dataclass
class NodeDeps:
    """Everything the nodes reach outside themselves.

    Injected rather than imported so the set is testable, and so a node cannot quietly
    acquire a dependency nobody declared.
    """

    router: Any
    crawl_site: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    serp_search: Callable[..., Awaitable[SerpPage]] | None = None
    #: Defaults to the real deterministic engine. Overridable only for tests.
    score_page: Callable[[SeoScoreRequest], Any] | None = None
    #: Agentic retrieval over the business's own documents. None = no knowledge base
    #: configured yet, which is a normal state, not an error.
    retrieve: Callable[..., Awaitable[Any]] | None = None
    #: Long-term business memory, already rendered as prompt lines.
    #:
    #: A callable rather than a session, so the nodes stay database-free and the tests
    #: stay hermetic. Read at INTAKE and carried in state for the whole run: reading it
    #: per node would let a mid-run edit change the brief halfway through, and produce a
    #: page written to two different sets of rules.
    load_memory: Callable[[], Awaitable[list[str]]] | None = None
    #: AI-visibility probe over a fixed prompt set. None = no probe store or router
    #: configured for this run; HARVEST then records the gap rather than reporting a
    #: share of voice nobody measured.
    geo_probe: Callable[..., Awaitable[Any]] | None = None
    #: A live web search GENERATE may ask for mid-draft, to check a fact it is about
    #: to write. Wired only when the SERP provider is real, for the same reason
    #: `serp_search` is: a fake result that reads as research is worse than no
    #: research, because it cannot be told apart from the real thing afterwards.
    web_search: Callable[..., Awaitable[Any]] | None = None
    channels: tuple[str, ...] = field(default=DEFAULT_CHANNELS)
    #: node -> tools an operator has revoked, loaded from `node_tool_policies`.
    #:
    #: Injected like every other dependency rather than read here, because the nodes
    #: must stay database-free: a `_toolbox` that queried a table would put I/O behind
    #: a property and make every node test need Postgres.
    revoked_tools: Mapping[str, frozenset[str]] | None = None


def _implementations(deps: NodeDeps) -> dict[str, ToolImpl]:
    """Map tool NAMES to the callables that implement them.

    The indirection is what makes the allowlist enforceable: a node asks for
    `crawl.site` and cannot reach `deps.crawl_site` any other way, so there is
    exactly one place where "may this node do this" is decided. Unconfigured
    dependencies are omitted rather than mapped to None, so `available()` answers
    "granted and wired" in one call.
    """
    candidates: dict[str, ToolImpl | None] = {
        CRAWL_SITE: deps.crawl_site,
        SERP_SEARCH: deps.serp_search,
        KB_SEARCH: deps.retrieve,
        MEMORY_LOAD: deps.load_memory,
        SEO_SCORE: deps.score_page or score_page,
        # Deterministic and always available: the guard must not be able to be
        # "not configured", or a business could be published unchecked by omission.
        CLAIMS_CHECK: check_claims,
        # Same reasoning: a landing page whose conversion audit was skipped because
        # nobody wired it would look like a page that passed.
        LANDING_CHECK: check_landing_page,
        # Granted to REPACK since the allowlist was written, and implemented by
        # nothing until now -- so `enforce_hashtags` had never once run inside a run,
        # and the engine's own measured finding (a prompt ending in the literal words
        # "Keine Hashtags" produced 21 of them) was going uncorrected in the product
        # while being corrected in the eval harness. Deterministic, so always
        # available for the same reason the two guards above are.
        CHANNEL_VALIDATE: enforce_hashtags,
        # Deterministic, so always available -- and now it has a SOURCE. The audit
        # shipped in Phase 4 and HARVEST held the grant with nothing behind it,
        # because nothing produced listings to diff. `engines/nap/extract` reads the
        # business's own `LocalBusiness` JSON-LD and its Impressum, which is a
        # narrower claim than a directory audit and one we can actually support.
        NAP_AUDIT: _audit_own_nap,
        GEO_PROBE: deps.geo_probe,
        WEB_SEARCH: deps.web_search,
    }
    return {name: impl for name, impl in candidates.items() if impl is not None}


def _toolbox(node: str, deps: NodeDeps) -> NodeToolbox:
    """The gate this node's tool calls pass through.

    This is the one call site the tool-policy service's docstring pointed at: until it
    passed `revoked`, an operator's revocation was stored, displayed and computed but
    NOT honoured by the running graph -- a kill switch that did nothing, which is worse
    than no kill switch because somebody would believe they had pulled it.
    """
    revoked = (deps.revoked_tools or {}).get(node, frozenset())
    return NodeToolbox(
        node=node, implementations=_implementations(deps), revoked=frozenset(revoked)
    )


def _with_refusals(state: AgentState, box: NodeToolbox, updates: dict[str, Any]) -> dict[str, Any]:
    """Surface any refused tool call on the run state.

    Appended to the existing list rather than assigned, because the graph MERGES a
    node's updates into the state: assigning `errors` would silently discard every
    degradation recorded by earlier nodes.
    """
    refused = box.node_errors()
    if refused:
        existing: list[NodeError] = list(updates.get("errors") or state["errors"])
        updates["errors"] = [*existing, *refused]
    return updates


def _tool_arguments(completion: Any, name: str, box: NodeToolbox) -> dict[str, Any] | None:
    """Pull one tool call's arguments, or None if the model answered in prose.

    Every returned call is run past the node's allowlist first. That is the
    backstop against an induced tool call: text inside the untrusted envelope can
    ask the model to call `publish`, and the model may comply, but a node that does
    not hold `publish` drops the call and records the refusal instead of executing
    it. Dropping rather than raising is deliberate — the run should continue with
    the legitimate output, and an attacker must not be able to end a run by
    smuggling one instruction into a competitor's page.

    None is returned rather than raised because each caller has a different correct
    response: OPPORTUNITY treats it as "nothing found", PLAN treats it as a failure.
    """
    for call in completion.tool_calls:
        if not isinstance(call, ToolCall):
            continue
        if not box.accept(call.name):
            continue
        if call.name == name:
            return dict(call.arguments)
    return None


async def _ask(
    deps: NodeDeps,
    *,
    task: TaskClass,
    role: str,
    state: AgentState,
    body: str,
    tool: ToolSpec,
    box: NodeToolbox,
    extra_tools: Sequence[ToolSpec] = (),
    max_tool_rounds: int = 2,
) -> tuple[dict[str, Any] | None, Decimal]:
    """One model exchange: assemble, call, and return the structured arguments and cost.

    The tool list handed to the model is filtered through the node's allowlist
    (docs/AGENT_RUNTIME.md section 4: a tool the node cannot have is REMOVED, not
    refused on call, so the model never plans around a capability it will not get).
    A node whose own output tool is not in its allowlist is a wiring bug, and it
    raises rather than silently calling the model with no tools at all.

    ``extra_tools`` turns this into a bounded LOOP rather than a single exchange. It
    exists for GENERATE's `web_search`: a model part-way through a draft can notice
    that a fact is missing and ask for it, which is the difference between research
    and a plausible guess. Four rules keep the loop from being an open-ended agent:

    * **the output tool is offered FIRST**, and that is load-bearing rather than
      tidy — the deterministic `FakeProvider` answers with `tools[0]`, so putting the
      record tool first is what keeps every hermetic test in this repo exercising the
      normal path instead of the search path;
    * **the loop is capped** at ``max_tool_rounds`` requests, after which the extra
      tools are withdrawn and the model is asked for its answer with what it has;
    * **an unwired or refused tool is answered honestly**, with a message saying the
      search did not run, so the model does not silently treat an empty result as
      "there is nothing to find";
    * **every result is fenced as untrusted**, because a search result is text from a
      page the business does not control — the same envelope harvested facts get.
    """
    # Extra tools are offered only when they are granted AND WIRED. `box.offer`
    # filters on the allowlist alone, which is right for an output tool -- a node
    # missing its own is a wiring bug worth raising over -- but wrong for a capability
    # the deployment simply does not have: docs/AGENT_RUNTIME.md §4's rule is that a
    # tool the node will not get is REMOVED, so the model never plans around it. The
    # "this search did not run" answer in `_tool_result` stays as the backstop for a
    # revocation that lands between the offer and the call.
    offered = box.offer([tool, *(spec for spec in extra_tools if box.available(spec.name))])
    if not any(spec.name == tool.name for spec in offered):
        raise ToolNotAllowedError(box.node, tool.name, box.allowed)

    messages: list[Message] = [
        system(role, state["dna"], state["remembered"]),
        Message(role=Role.USER, content=body),
    ]
    trace = {
        "business_id": str(state.get("business_id") or ""),
        "node": box.node,
        "prompt_version": PROMPT_VERSION,
    }
    spent = Decimal("0")

    for round_index in range(max_tool_rounds + 1):
        # The last round withdraws everything except the output tool: a model that has
        # already searched twice and is offered a third search will take it, and the
        # node needs a page, not a bibliography.
        tools = offered if round_index < max_tool_rounds else [tool]

        completion = await deps.router.complete(
            task,
            messages,
            tools=tools,
            # Current Claude models reject `temperature` outright, and every node here
            # wants the provider default anyway.
            temperature=None,
            # Nothing passed this before, so EVERY llm span was recorded with an empty
            # run_id, business_id and node -- `llm_span_fields` defaults them to "" and
            # the no-op tracer meant nobody saw it. It is also what a `model_usage` row
            # needs to be attributable to anything, so the ledger could not have been
            # written correctly even once a writer existed.
            trace=trace,
        )
        spent += Decimal(str(completion.usage.usd))

        args = _tool_arguments(completion, tool.name, box)
        if args is not None:
            return args, spent

        requests = _requested(completion, {spec.name for spec in tools} - {tool.name}, box)
        if not requests:
            # Prose, or a tool nobody granted. Either way there is nothing more this
            # exchange can produce, and each caller has its own correct response to
            # `None` -- OPPORTUNITY treats it as "nothing found", PLAN as a failure.
            return None, spent

        # An empty assistant turn carrying only the tool calls, which is what both
        # adapters expect to see replayed before the results.
        messages.append(Message(role=Role.ASSISTANT, content="", tool_calls=list(requests)))
        for call in requests:
            messages.append(
                Message(
                    role=Role.TOOL,
                    content=await _tool_result(deps, box, call),
                    tool_call_id=call.call_id,
                )
            )

    return None, spent


def _requested(completion: Any, names: set[str], box: NodeToolbox) -> list[ToolCall]:
    """The model's calls for tools OTHER than the output tool, allowlist-filtered.

    Filtered through :meth:`NodeToolbox.accept`, which is the backstop against an
    induced call: crawled page text can ask the model to reach for something, and the
    node drops what it does not hold and records the refusal rather than executing it.
    """
    return [
        call
        for call in completion.tool_calls
        if isinstance(call, ToolCall) and call.name in names and box.accept(call.name)
    ]


async def _tool_result(deps: NodeDeps, box: NodeToolbox, call: ToolCall) -> str:
    """Run one model-requested tool and render its result as an untrusted payload.

    Never raises. A search that fails, is unwired, or is refused comes back as a
    SENTENCE saying so — because the alternative is handing the model an empty result,
    which it will read as "there is nothing to find" and write around as though the
    absence were a fact.
    """
    if call.name != WEB_SEARCH:
        return f"{call.name} is not available in this step."

    query = str(call.arguments.get("query", "")).strip()
    if not query:
        return "No query was given, so no search ran."
    if not box.available(WEB_SEARCH):
        return (
            "Live web search is not configured on this deployment, so this search did "
            "NOT run. Do not treat that as evidence either way — write only what the "
            "supplied evidence supports, and leave the rest out."
        )

    try:
        page = await box.call(WEB_SEARCH, query)
    except ToolNotAllowedError:
        raise
    except Exception as exc:  # broad on purpose: a dead search must not end the run
        logger.warning("%s: web search failed: %s", box.node, exc)
        return (
            f"The search for {query!r} failed and returned nothing. Do not treat that "
            "as evidence either way."
        )

    results = getattr(page, "results", None) or []
    if not results:
        return f"The search for {query!r} returned no results."

    lines = [
        f"- {getattr(result, 'title', '') or 'untitled'} ({getattr(result, 'url', '')}): "
        f"{(getattr(result, 'snippet', '') or '')[:300]}"
        for result in results[:5]
    ]
    # Fenced, like every other thing a third party wrote. A search result is a page
    # the business does not control, so it is quoted evidence and never instruction.
    return f"Search results for {query!r}:\n" + fence("\n".join(lines))


def _evidence(state: AgentState) -> str:
    """Harvested facts, compact and fenced as untrusted."""
    facts = state.get("facts") or {}
    gaps = state.get("fact_gaps") or []
    payload = json.dumps(facts, ensure_ascii=False, indent=2, default=str)[:6000]
    missing = f"\n\nNOT AVAILABLE (do not invent these): {', '.join(gaps)}" if gaps else ""
    return fence(payload) + missing


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def build_nodes(deps: NodeDeps) -> dict[str, Node]:
    """Build the node set the graph driver executes."""

    async def intake(state: AgentState) -> dict[str, Any]:
        """Normalise the request. No model: there is nothing here to decide."""
        box = _toolbox("INTAKE", deps)
        dna = state["dna"]
        if not dna.get("name"):
            return {
                "fact_gaps": [*state["fact_gaps"], "business profile"],
            }
        surfaces = state["surfaces"] or ["google"]
        updates: dict[str, Any] = {"surfaces": surfaces}

        # Memory is loaded here and nowhere else. Before this, `state["remembered"]` was
        # threaded into the system prompt correctly but nothing ever populated it, so a
        # remembered preference reached no model — the feature existed on both sides of a
        # gap with nothing across it.
        if box.available(MEMORY_LOAD):
            try:
                updates["remembered"] = await box.call(MEMORY_LOAD)
            except ToolNotAllowedError:
                # Never swallowed into a fact gap: an allowlist refusal is a wiring
                # fault or an attack, and either way it must be loud.
                raise
            except Exception as exc:  # broad on purpose: memory is an enhancement
                logger.warning("intake: could not load memory: %s", exc)
                updates["fact_gaps"] = [*state["fact_gaps"], "remembered preferences"]

        return _with_refusals(state, box, updates)

    async def harvest(state: AgentState) -> dict[str, Any]:
        """Gather evidence from the engines. Engines only — no model, ever.

        Each source is independent and failure is per-source: one dead provider must
        not cost the run the evidence the others returned.
        """
        box = _toolbox("HARVEST", deps)
        dna = state["dna"]
        facts: dict[str, Any] = {}
        gaps: list[str] = []

        if box.available(CRAWL_SITE) and dna.get("website"):
            try:
                facts["site"] = await box.call(CRAWL_SITE, str(dna["website"]))
            except ToolNotAllowedError:
                # A refusal is NOT a dead source. Degrading it into a fact gap would
                # hide the one failure mode this allowlist exists to make visible.
                raise
            except Exception as exc:  # broad on purpose: one dead source must not end the run
                logger.warning("harvest: site crawl failed: %s", exc)
                gaps.append("website crawl")
        elif not dna.get("website"):
            gaps.append("website (none on record)")

        if box.available(SERP_SEARCH):
            seeds = _seed_queries(dna)
            pages: list[SerpPage] = []
            try:
                for seed in seeds[:MAX_SEED_SEARCHES]:
                    pages.append(
                        await box.call(SERP_SEARCH, seed, locale=dna.get("locale", "de"), limit=10)
                    )
            except ToolNotAllowedError:
                raise
            except Exception as exc:  # broad on purpose, per-source degradation
                logger.warning("harvest: search failed: %s", exc)
                gaps.append("search results (keywords, competitors)")

            if pages:
                city = dna.get("city")
                candidates = expand_keywords(seeds[0], pages=pages, city=city)
                facts["keywords"] = [c.model_dump(mode="json") for c in candidates[:40]]
                own = _own_host(dna)
                facts["competitors"] = [
                    c.model_dump(mode="json")
                    for c in discover_competitors(pages, own_host=own)[:10]
                ]
        else:
            gaps.append("search results (no provider configured)")

        if box.available(KB_SEARCH):
            try:
                trace = await box.call(KB_SEARCH, state["goal"])
                facts["knowledge"] = {
                    "outcome": getattr(trace, "outcome", None),
                    "chunks": len(getattr(trace, "chunks", []) or []),
                }
            except ToolNotAllowedError:
                raise
            except Exception as exc:  # broad on purpose, per-source degradation
                logger.warning("harvest: retrieval failed: %s", exc)
                gaps.append("uploaded documents")
        else:
            gaps.append("uploaded documents")

        # The NAP audit needs no provider and no model: it diffs the business's own
        # published address against itself. Its listings come from the crawl that just
        # ran, so it is skipped -- and NAMED as skipped -- when the crawl produced
        # nothing, rather than reporting a clean audit of zero listings.
        if box.available(NAP_AUDIT) and facts.get("site"):
            try:
                audit = await box.call(NAP_AUDIT, dna, facts["site"])
            except ToolNotAllowedError:
                raise
            except Exception as exc:  # broad on purpose, per-source degradation
                logger.warning("harvest: nap audit failed: %s", exc)
                gaps.append("address consistency")
            else:
                if audit is None:
                    # Nothing published a NAP we could read. Not a finding, and not a
                    # pass either: an audit of no listings would score 100 and tell a
                    # business its address is consistent everywhere it is not listed.
                    gaps.append("address consistency (no address published on the site)")
                else:
                    facts["nap"] = audit

        if box.available(GEO_PROBE):
            try:
                facts["visibility"] = await box.call(GEO_PROBE, dna)
            except ToolNotAllowedError:
                raise
            except Exception as exc:  # broad on purpose, per-source degradation
                logger.warning("harvest: visibility probe failed: %s", exc)
                gaps.append("AI answer-engine visibility")
        else:
            gaps.append("AI answer-engine visibility (no probe configured)")

        return _with_refusals(
            state, box, {"facts": facts, "fact_gaps": [*state["fact_gaps"], *gaps]}
        )

    async def opportunity(state: AgentState) -> dict[str, Any]:
        """Rank the evidence into opportunities and take the best.

        An empty list is a legitimate answer, and the graph ends the run honestly on
        it. Inventing a topic to fill the slot is the failure mode here.
        """
        box = _toolbox("OPPORTUNITY", deps)
        # The business's own material, against the run's goal. `docs/AGENT_RUNTIME.md`
        # granted this node `kb.search` and nothing called it, so the ranking was made
        # from the crawl and the SERP alone -- which is exactly the evidence a
        # competitor also has. What a business can uniquely write about is in its own
        # documents, so a topic chosen without reading them is a topic anyone could
        # have chosen.
        passages = await _retrieved(box, state["goal"], "opportunity")
        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.PRIORITISE,
            role=(
                "You choose what this business should publish next. You rank only what "
                "the evidence supports, and you return an empty list rather than a "
                "weak idea."
            ),
            state=state,
            body=(
                f"Goal: {state['goal']}\n\nEvidence:\n{_evidence(state)}\n"
                f"{passages}\n"
                "Call record_opportunities. Score each 0-100 on likely lead impact "
                "against effort."
            ),
            tool=OPPORTUNITY_TOOL,
        )
        if not args:
            return _with_refusals(state, box, {"opportunity": None, "_cost": cost})

        ranked = sorted(
            (o for o in args.get("opportunities", []) if o.get("title")),
            key=lambda o: -int(o.get("score", 0)),
        )
        return _with_refusals(
            state, box, {"opportunity": ranked[0] if ranked else None, "_cost": cost}
        )

    async def plan(state: AgentState) -> dict[str, Any]:
        """Outline the page. A target keyword is mandatory."""
        box = _toolbox("PLAN", deps)
        opp = state.get("opportunity") or {}
        # Retrieved against the CHOSEN opportunity, not the run goal: the goal is "more
        # local leads" and retrieving against it returns whatever is most on-topic for
        # the business in general. An outline needs the material about the thing being
        # written, which is what makes a section heading answerable from evidence.
        question = str(opp.get("title") or "").strip() or state["goal"]
        passages = await _retrieved(box, question, "plan")
        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.PLAN,
            role=(
                "You outline one page. Every section must serve the search intent "
                "behind the target keyword."
            ),
            state=state,
            body=(
                f"Opportunity: {json.dumps(opp, ensure_ascii=False, default=str)}\n\n"
                f"Evidence:\n{_evidence(state)}\n{passages}\nCall record_outline."
            ),
            tool=PLAN_TOOL,
        )
        if not args or not str(args.get("target_keyword", "")).strip():
            raise ValueError(
                "the outline has no target keyword; a page without one cannot be "
                "scored, and generating it would waste the run's budget"
            )
        return _with_refusals(state, box, {"outline": args, "_cost": cost})

    async def generate(state: AgentState) -> dict[str, Any]:
        """Write the page, grounded in the evidence.

        On a retry the SEO findings are passed through VERBATIM. Without that the
        validation loop is theatre: the model would be asked to try again with no idea
        what failed.
        """
        box = _toolbox("GENERATE", deps)
        outline = state.get("outline") or {}
        report = state.get("seo_report") or {}
        retry = ""
        if report and not report.get("passed", True):
            hints = [
                f"- {f.get('fix_hint')}" for f in report.get("findings", []) if f.get("fix_hint")
            ]
            retry = (
                f"\n\nYOUR PREVIOUS DRAFT SCORED {report.get('score')} / 100 AND MUST "
                "REACH 85. Fix exactly these, and change nothing else:\n" + "\n".join(hints)
            )

        # A failed claim check is passed through verbatim for the same reason the SEO
        # hints are: the model is told exactly which phrase is forbidden, so the retry
        # is a correction rather than a guess. It is appended AFTER the SEO block and
        # phrased as non-negotiable, because a low score is a quality problem and a
        # banned claim is a legal one.
        claims = state.get("claim_check") or {}
        if claims and not claims.get("passed", True) and claims.get("fix_hint"):
            retry += "\n\n" + str(claims["fix_hint"])

        # The business's own material, against the keyword the page is being written
        # for. This is the node where a missing passage becomes an invented sentence,
        # so it is the node where the grant mattered most and had nothing behind it.
        keyword = str(outline.get("target_keyword") or "").strip() or state["goal"]
        passages = await _retrieved(box, keyword, "generate")

        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.GENERATE,
            role=(
                "You write the page. Every factual claim comes from the evidence or "
                "from the business's own documents; if the evidence does not support a "
                "claim, you leave it out."
            ),
            state=state,
            body=(
                f"Outline: {json.dumps(outline, ensure_ascii=False, default=str)}\n\n"
                f"Evidence:\n{_evidence(state)}\n{passages}{retry}\n\n"
                "Call record_page. If a fact you need is genuinely missing, you may "
                "call web_search ONCE for it rather than guessing."
            ),
            tool=GENERATE_TOOL,
            extra_tools=(WEB_SEARCH_TOOL,),
        )
        if not args:
            raise ValueError("the model did not return a page; expected a record_page tool call")
        return _with_refusals(state, box, {"draft": args, "_cost": cost})

    async def convert(state: AgentState) -> dict[str, Any]:
        """Write the landing page the content converts on, and the ask per channel.

        This is CONVERSION -- link three of the lead chain in docs/FEATURES.md
        section 0, and the link competitors skip. A tracked short link pointing at a
        page that does not exist earns nothing, so this node produces the page the
        click lands on and the copy that earns the click.

        It sits BETWEEN GENERATE and VALIDATE deliberately. VALIDATE is where the
        deterministic verdicts are taken, and the regulated-claim gate must see the
        landing page before REVIEW: a landing page is the most claim-dangerous
        artifact in the product ("garantierte Heilung" on a page with a form under
        it), and REVIEW is where a human can approve. A node placed after VALIDATE
        would produce copy nothing had checked.

        Only the WRITING happens here. Whether the result can convert, and what
        markup it becomes, are computable and live in ``engines/landing``.
        """
        box = _toolbox("CONVERT", deps)
        outline = state.get("outline") or {}
        draft = state.get("draft") or {}
        channels = _cta_channels(deps.channels)

        # Proof points must come from the business's own material, so the node reads
        # that material rather than trusting the model to remember it. A missing
        # knowledge base is a normal state: the page is then written from the DNA and
        # the harvested facts, and the deterministic check will report however many
        # sourced proof points actually exist.
        passages = ""
        if box.available(KB_SEARCH):
            try:
                trace = await box.call(KB_SEARCH, _proof_question(state))
            except ToolNotAllowedError:
                raise
            except Exception as exc:  # broad on purpose: proof is an enhancement
                logger.warning("convert: retrieval failed: %s", exc)
            else:
                passages = _passages(trace)

        report = state.get("landing_report") or {}
        retry = ""
        if report and not report.get("passed", True):
            hints = [
                f"- {f.get('fix_hint')}" for f in report.get("findings", []) if f.get("fix_hint")
            ]
            retry = (
                f"\n\nYOUR PREVIOUS LANDING PAGE SCORED {report.get('score')} / 100 AND MUST "
                "REACH 85. Fix exactly these, and change nothing else:\n" + "\n".join(hints)
            )
        # The claim verdict covers the page AND the landing copy as one check, so a
        # forbidden phrase written HERE is named here too. Without this, only GENERATE
        # would hear about it and the retry could never fix the offending artifact.
        claims = state.get("claim_check") or {}
        if claims and not claims.get("passed", True) and claims.get("fix_hint"):
            retry += "\n\n" + str(claims["fix_hint"])

        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.REPACK,
            role=(
                "You write the landing page this content converts on: one offer, one "
                "form, one ask. Every proof point comes from the business's own "
                "documents or profile and names where it came from; if the evidence "
                "supports none, you return none rather than inventing one."
            ),
            state=state,
            body=(
                f"Page title: {draft.get('title')}\n"
                f"Target keyword: {outline.get('target_keyword')}\n"
                f"Outline CTA: {outline.get('cta')}\n"
                f"Page: {str(draft.get('html'))[:1500]}\n\n"
                f"Channels needing a CTA: {', '.join(channels)}\n\n"
                f"Business evidence:\n{_evidence(state)}\n"
                f"{passages}{retry}\n\nCall record_landing_page."
            ),
            tool=LANDING_TOOL,
        )
        if not args:
            raise ValueError(
                "the model did not return a landing page; expected a "
                "record_landing_page tool call. Without one the tracked links have "
                "nowhere to point, so the run has reach and no conversion path."
            )
        spec = _landing_spec(args)
        return _with_refusals(
            state, box, {"landing_page": spec.model_dump(mode="json"), "_cost": cost}
        )

    async def validate(state: AgentState) -> dict[str, Any]:
        """Score the draft and check it for forbidden claims. No model, ever.

        Two deterministic verdicts, and they are different KINDS of verdict. The SEO
        score is a quality measure with a threshold: a weak page is publishable after
        a retry. The claim check is a compliance gate: a draft carrying a claim the
        business is not allowed to make cannot be published at any score, so the
        graph refuses to carry it to REVIEW (see `graph.run_graph`).

        An absent draft fails both rather than raising: the graph's retry path is the
        right response, and a crash here would lose the run.
        """
        box = _toolbox("VALIDATE", deps)
        draft = state.get("draft") or {}
        outline = state.get("outline") or {}
        html = str(draft.get("html") or "")
        title = str(draft.get("title") or "")
        meta = str(draft.get("meta_description") or "")

        # The landing page is audited and claim-checked here too, not in CONVERT.
        # CONVERT writes; this node judges. Keeping both verdicts in one place is what
        # makes "the deterministic half of the pipeline" a place rather than a habit.
        landing_spec = _stored_landing_spec(state)
        landing_report: dict[str, Any] | None = None
        if landing_spec is not None:
            verdict = await box.call(
                LANDING_CHECK,
                LandingCheckRequest(spec=landing_spec, known_channels=sorted(KNOWN_CHANNELS)),
            )
            landing_report = verdict.model_dump(mode="json")

        # Title and meta are checked alongside the body. They are NOT folded into the
        # assembled HTML document for this: the meta description lives in an
        # attribute, and stripping markup would drop it, so a forbidden claim in the
        # meta description would have gone unnoticed on the one line Google shows.
        #
        # The landing page's text joins the same check rather than getting its own
        # verdict, because "may this run be published" has one answer. A landing page
        # is the most claim-dangerous artifact here -- it makes a promise directly
        # above a form -- and the graph refuses to carry a failing verdict to REVIEW,
        # so a banned claim in the conversion copy cannot be approved either.
        claim_result = await box.call(
            CLAIMS_CHECK,
            ClaimCheckRequest(
                content="\n".join(
                    [title, meta, html, landing_spec.claim_text() if landing_spec else ""]
                ),
                banned_claims=[str(c) for c in (state["dna"].get("banned_claims") or [])],
                contains_markup=True,
            ),
        )
        claim_check = claim_result.model_dump(mode="json")

        if not html:
            return _with_refusals(
                state,
                box,
                {
                    "claim_check": claim_check,
                    "landing_report": landing_report,
                    "seo_report": {
                        "score": 0,
                        "passed": False,
                        "findings": [],
                        "note": "No draft HTML was produced, so there was nothing to score.",
                    },
                },
            )

        # The engine scores a document, and GENERATE returns title and meta separately.
        document = (
            f"<html><head><title>{title}</title>"
            f'<meta name="description" content="{meta}">'
            f"</head><body>{html}</body></html>"
        )
        result = await box.call(
            SEO_SCORE,
            SeoScoreRequest(
                html=document,
                target_keyword=str(outline.get("target_keyword") or ""),
                secondary_keywords=list(outline.get("secondary_keywords") or []),
                locale=str(state["dna"].get("locale") or "de"),
            ),
        )
        return _with_refusals(
            state,
            box,
            {
                "seo_report": result.model_dump(mode="json"),
                "claim_check": claim_check,
                "landing_report": landing_report,
            },
        )

    async def repack(state: AgentState) -> dict[str, Any]:
        """Render the approved message per channel, then enforce the limits in code."""
        box = _toolbox("REPACK", deps)
        draft = state.get("draft") or {}
        outline = state.get("outline") or {}
        wanted = ", ".join(deps.channels)

        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.REPACK,
            role=(
                "You adapt one message for each channel. The claim stays identical "
                "across channels; only the register and the length change."
            ),
            state=state,
            body=(
                f"Title: {draft.get('title')}\n"
                f"Target keyword: {outline.get('target_keyword')}\n"
                f"Page: {str(draft.get('html'))[:2500]}\n\n"
                f"Channels: {wanted}\n\nCall record_posts."
            ),
            tool=REPACK_TOOL,
        )
        banned = [str(c) for c in (state["dna"].get("banned_claims") or [])]
        renderings: dict[str, dict[str, Any]] = {}
        blocked: list[NodeError] = []
        for post in (args or {}).get("posts", []):
            channel = str(post.get("channel", "")).lower().strip()
            body = str(post.get("body", "")).strip()
            if not channel or not body:
                continue
            spec = CHANNEL_SPECS.get(canonical_channel(channel))
            limit = CHANNEL_LIMITS.get(canonical_channel(channel))
            if limit and len(body) > limit:
                # Trim on a word boundary. The platform would reject the post
                # outright, so shipping it over-length is not an option.
                body = body[: limit - 1].rsplit(" ", 1)[0] + "…"

            # Hashtags: the model's declared list AND whatever it wrote inline, both
            # brought inside the channel's range in code. The engine's docstring
            # explains why this is not the model's job -- a count is arithmetic, and a
            # negative count instruction is the one kind a model reliably disobeys.
            body, tags, removed, shortfall = await _bring_hashtags_in_range(
                box, spec, body, post.get("hashtags")
            )

            # Checked AFTER trimming, because the trimmed text is what would be
            # published. A rendering is separate content from the page: the page can
            # pass VALIDATE and a post derived from it can still carry a forbidden
            # claim, and there is no per-channel retry in the graph -- so the post is
            # DROPPED and the loss is named, rather than published or silently lost.
            verdict = await box.call(
                CLAIMS_CHECK,
                ClaimCheckRequest(content=body, banned_claims=banned, contains_markup=False),
            )
            if not verdict.passed:
                blocked.append(
                    NodeError(
                        node="REPACK",
                        code="banned_claim",
                        message=(
                            f"The {channel} post was withheld: it makes the forbidden "
                            f"claim(s) {', '.join(verdict.claims_found)}. "
                            "Nothing was published for that channel."
                        ),
                    )
                )
                continue
            renderings[channel] = {
                "body": body,
                "hashtags": list(tags),
                "hashtags_removed": removed,
                "hashtags_shortfall": shortfall,
                # Over the EDITORIAL target, which is not the same as over the
                # platform's limit and must not be treated as one: a 1,900-character
                # LinkedIn post publishes fine and is simply longer than it should be.
                # Reported so the review screen can say so; never truncated to it.
                "over_target": bool(spec and len(body) > spec.max_chars),
            }

        updates: dict[str, Any] = {"renderings": renderings, "_cost": cost}
        if blocked:
            updates["errors"] = [*state["errors"], *blocked]
        return _with_refusals(state, box, updates)

    async def review(state: AgentState) -> dict[str, Any]:
        """The interrupt point. Nothing to do: the graph pauses here for a human."""
        return {}

    return {
        "INTAKE": intake,
        "HARVEST": harvest,
        "OPPORTUNITY": opportunity,
        "PLAN": plan,
        "GENERATE": generate,
        "CONVERT": convert,
        "VALIDATE": validate,
        "REPACK": repack,
        "REVIEW": review,
    }


async def _bring_hashtags_in_range(
    box: NodeToolbox,
    spec: ChannelSpec | None,
    body: str,
    declared: Any,
) -> tuple[str, list[str], int, int]:
    """Enforce the channel's hashtag range, and return the tags that survived.

    Two sources have to be reconciled, because the model uses both: hashtags written
    INLINE in the post, and the separate ``hashtags`` array `REPACK_TOOL` asks for.
    The body is the published artifact, so `channel.validate` runs on that; the
    declared array then tops the list up to the cap, in the order the model gave, and
    only with tags the body does not already carry.

    A shortfall is reported and never filled. Inventing ``#Zahnarzt`` here would be a
    node writing marketing copy from a counter, and the engine refuses to do it for
    exactly that reason -- so a channel wanting three tags and given one says so.

    A channel with no spec (a run configured for something the spec table does not
    cover) is left alone rather than defaulted to zero: silently stripping every
    hashtag off a post because we have no numbers for it would be enforcement by
    accident.
    """
    if spec is None:
        return body, _clean_tags(declared), 0, 0

    if box.available(CHANNEL_VALIDATE):
        result = await box.call(
            CHANNEL_VALIDATE, body, minimum=spec.hashtags_min, maximum=spec.hashtags_max
        )
        body = result.text
        inline = list(result.kept)
        removed = result.removed
    else:
        # Refused or unwired: the counters must then say nothing was enforced rather
        # than reporting a clean zero that looks like a compliant post.
        inline = []
        removed = 0

    tags = list(inline)
    for tag in _clean_tags(declared):
        if len(tags) >= spec.hashtags_max:
            break
        if tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)

    return body, tags, removed, max(0, spec.hashtags_min - len(tags))


def _clean_tags(declared: Any) -> list[str]:
    """The model's ``hashtags`` array, de-duplicated and prefixed, order preserved."""
    if not isinstance(declared, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in declared:
        text = str(item).strip().lstrip("#").strip()
        if not text:
            continue
        tag = f"#{text}"
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out


async def _retrieved(box: NodeToolbox, question: str, node: str) -> str:
    """Passages from the business's own documents, fenced and labelled — or nothing.

    The same shape CONVERT has used since it shipped, lifted out so OPPORTUNITY, PLAN
    and GENERATE can use it too. Those three held `kb.search` in their allowlist and
    never called it, which is the difference between a documented capability and a
    working one.

    A missing knowledge base is a NORMAL state and returns an empty string: the caller
    then writes from the DNA and the harvested facts, which is what a business with no
    uploaded documents legitimately has. A retrieval that raises is logged and also
    returns empty — grounding is an enhancement to a prompt, and losing it must not
    cost the run the work every other source produced. An allowlist refusal is
    re-raised, because that is a wiring fault or an attack and must be loud.
    """
    if not box.available(KB_SEARCH):
        return ""
    try:
        trace = await box.call(KB_SEARCH, question)
    except ToolNotAllowedError:
        raise
    except Exception as exc:  # broad on purpose: grounding is an enhancement
        logger.warning("%s: retrieval failed: %s", node, exc)
        return ""
    return _passages(trace)


def _audit_own_nap(dna: Mapping[str, Any], site: Any) -> dict[str, Any] | None:
    """Diff the business's own published NAP against the profile it confirmed.

    The `nap.audit` implementation, and the reason HARVEST's grant is no longer empty.
    Returns ``None`` when the site published no readable address, because an audit of
    zero listings scores 100 -- which would tell a business its address is consistent
    everywhere it is not listed.

    The canonical record comes from the Business DNA the owner confirmed at
    onboarding, which is the right authority: it is the one version of the address a
    human has looked at and approved. Everything else -- the JSON-LD, the Impressum --
    is a copy that may have drifted from it.
    """
    raw = site.get("nap_sources") if isinstance(site, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return None
    # Extracted by `run_executor.summarise_crawl`, which is the last place the full
    # page facts exist: the summary carried in state drops the JSON-LD blocks and keeps
    # only an excerpt of each body, so extracting here would find nothing. The node
    # only ever DIFFS -- which is the engine boundary working as intended.
    listings = [DirectoryListing.model_validate(entry) for entry in raw]

    canonical = _canonical_nap(dna)
    result = audit_nap(canonical, listings)
    return {
        "consistency_score": result.consistency_score,
        "sources_checked": result.sources_checked,
        "sources": sorted({listing.source for listing in listings}),
        "findings": [finding.model_dump(mode="json") for finding in result.findings],
        # Stated in the payload, not only in a docstring, because this number will be
        # read as "your address is consistent online" if nothing says otherwise.
        "scope": (
            "Compares the address published on your own website (structured data and "
            "Impressum) against your confirmed profile. It does not check external "
            "directories."
        ),
    }


def _canonical_nap(dna: Mapping[str, Any]) -> CanonicalNap:
    """The confirmed profile as the one true NAP record.

    `normalise_nap` produces both the display and the comparison forms, so the audit
    can be brutal about equivalence while the advice stays correct German -- which is
    the distinction the `nap` engine's contract exists to preserve.
    """
    return normalise_nap(
        RawNap(
            legal_name=_optional_str(dna.get("legal_name")) or _optional_str(dna.get("name")),
            trading_name=_optional_str(dna.get("name")),
            street=_optional_str(dna.get("street")),
            house_number=_optional_str(dna.get("house_number")),
            postcode=_optional_str(dna.get("postcode")),
            city=_optional_str(dna.get("city")),
            phone=_optional_str(dna.get("phone")),
            email=_optional_str(dna.get("email")),
        )
    )


def _optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _cta_channels(channels: tuple[str, ...]) -> tuple[str, ...]:
    """The channels CONVERT writes an ask for: the run's own, plus the bio-link hub.

    Order-preserving de-duplication, so a run already rendering for ``link_hub`` does
    not get two CTAs for it -- which the deterministic check would (correctly) refuse
    as splitting one channel's clicks across two links.
    """
    return tuple(dict.fromkeys([*channels, HUB_CHANNEL]))


def _proof_question(state: AgentState) -> str:
    """What to ask the knowledge base for, when looking for proof rather than prose.

    Deliberately not the run's goal: the goal is "more local leads", and retrieving
    against it returns whatever is most on-topic. Proof points need evidence about the
    business's track record on the thing being offered, so the question is built from
    the offer's own subject.
    """
    outline = state.get("outline") or {}
    keyword = str(outline.get("target_keyword") or "").strip()
    name = str(state["dna"].get("name") or "").strip()
    subject = keyword or str((state.get("draft") or {}).get("title") or "").strip()
    return f"Belege, Referenzen und Zahlen zu {subject} bei {name}".strip()


def _passages(trace: Any) -> str:
    """Retrieved passages, fenced as untrusted, each labelled so it can be cited.

    The label is the document id and the passage ordinal, which is what the retrieval
    trace actually knows. The model is asked for a human-readable source, so the label
    is a fallback rather than the expected output -- and the engine can only enforce
    that a source is NAMED, never that it is true. That limit is stated in the
    landing contract and is not papered over here.
    """
    chunks = list(getattr(trace, "chunks", []) or [])
    if not chunks:
        return ""
    lines = [
        f"[{getattr(chunk, 'document_id', '?')}#{getattr(chunk, 'ordinal', 0)}] "
        f"{str(getattr(chunk, 'content', ''))[:600]}"
        for chunk in chunks[:6]
    ]
    return "\nOwn documents (cite these as sources):\n" + fence("\n\n".join(lines))


def _landing_spec(args: Mapping[str, Any]) -> LandingPageSpec:
    """Build a spec from a model's tool arguments, dropping only what is unusable.

    Lenient on purpose, and the leniency is bounded. A form field named something the
    lead endpoint does not accept, or a CTA with no text, is DROPPED -- the same
    treatment REPACK gives a malformed post. Rejecting the whole call instead would
    lose a good page over one bad field, and silently keeping the bad field would
    produce a form the visitor fills in and the server refuses.

    What is dropped is not swallowed: the deterministic check then reports the
    consequence ("there is no form", "the form asks for no email address") with a fix
    hint, so the retry is a correction rather than a guess.
    """
    proof = [
        ProofPoint(text=str(item.get("text", "")), source=str(item.get("source", "")))
        for item in args.get("proof_points") or []
        if isinstance(item, Mapping)
    ]
    fields: list[FormField] = []
    for item in args.get("form_fields") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip().lower()
        if name not in _LEGAL_FORM_FIELDS:
            logger.warning("convert: dropped form field %r, which the lead form refuses", name)
            continue
        fields.append(
            FormField(
                name=name,  # type: ignore[arg-type]  # narrowed by the membership test above
                label=str(item.get("label", "")) or name,
                required=bool(item.get("required", False)),
            )
        )
    ctas = [
        ChannelCta(
            channel=str(item.get("channel", "")).strip().lower(),
            text=str(item.get("text", "")).strip(),
        )
        for item in args.get("ctas") or []
        if isinstance(item, Mapping) and str(item.get("text", "")).strip()
    ]
    return LandingPageSpec(
        headline=str(args.get("headline", "")),
        subhead=str(args.get("subhead", "")),
        offer=str(args.get("offer", "")),
        proof_points=proof,
        form_fields=fields,
        primary_cta=str(args.get("primary_cta", "")),
        consent_text=str(args.get("consent_text", "")),
        ctas=ctas,
    )


def _stored_landing_spec(state: AgentState) -> LandingPageSpec | None:
    """The landing page CONVERT stored, or None.

    Read defensively: the state may have come back from a JSONB checkpoint written by
    an earlier version of this code, and a run whose review screen cannot load is a
    run nobody can finish. A malformed spec degrades to "there was nothing to audit",
    which the graph treats as an absent verdict rather than as a pass.
    """
    stored = state.get("landing_page")
    if not isinstance(stored, Mapping):
        return None
    try:
        return LandingPageSpec.model_validate(dict(stored))
    except Exception as exc:  # broad: any malformed checkpoint, not one shape of it
        logger.warning("validate: stored landing page could not be read: %s", exc)
        return None


def _seed_queries(dna: Mapping[str, Any]) -> list[str]:
    """Search seeds from the business profile, most commercially useful first."""
    city = str(dna.get("city") or "").strip()
    services = [str(s).strip() for s in (dna.get("services") or []) if str(s).strip()]
    seeds = [f"{s} {city}".strip() for s in services]
    if not seeds:
        seeds = [str(dna.get("industry") or dna.get("name") or "").strip()]
    return [s for s in seeds if s]


def _own_host(dna: Mapping[str, Any]) -> str | None:
    website = str(dna.get("website") or "")
    if not website:
        return None
    return website.removeprefix("https://").removeprefix("http://").split("/")[0]


__all__ = ["CHANNEL_LIMITS", "DEFAULT_CHANNELS", "Node", "NodeDeps", "build_nodes"]
