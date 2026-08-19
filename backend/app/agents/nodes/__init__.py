"""The real graph nodes, wired to the engines, the services and the model router.

Read alongside docs/AGENT_RUNTIME.md section 3, which tabulates every node with its
model tier, its tools and its failure mode.

Two invariants this package exists to hold:

* **HARVEST and VALIDATE call no model.** They are the trustworthy half of the
  pipeline precisely because they are deterministic — one gathers evidence, the other
  measures the result, and neither can hallucinate.
* **A failed fact source degrades the run and is NAMED.** `fact_gaps` is what lets the
  UI say "generated without live research" instead of quietly implying research
  happened.

Every dependency is injected through :class:`NodeDeps`, so the whole set runs with no
network, no database and no model in tests. Nodes RECEIVE a router; they never build
one, which is why making the provider and model choice configurable elsewhere needs
no change here.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from backend.app.agents.nodes.prompts import (
    GENERATE_TOOL,
    OPPORTUNITY_TOOL,
    PLAN_TOOL,
    REPACK_TOOL,
    fence,
    system,
)
from backend.app.agents.state import AgentState
from backend.app.engines.seo import SeoScoreRequest, score_page
from backend.app.engines.serp import (
    SerpPage,
    discover_competitors,
    expand_keywords,
)
from backend.app.llm import Message, Role, TaskClass, ToolCall, ToolSpec

logger = logging.getLogger(__name__)

Node = Callable[[AgentState], Awaitable[dict[str, Any]]]

#: Hard character ceilings per channel. Length is arithmetic, so Python enforces it
#: after generation rather than asking the model to count -- it will get it wrong, and
#: the platform will reject the post.
CHANNEL_LIMITS: Mapping[str, int] = {
    "linkedin": 3000,
    "facebook": 2000,
    "instagram": 2200,
    "x": 280,
}

DEFAULT_CHANNELS: tuple[str, ...] = ("linkedin", "facebook", "instagram")

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
    channels: tuple[str, ...] = field(default=DEFAULT_CHANNELS)


def _tool_arguments(completion: Any, name: str) -> dict[str, Any] | None:
    """Pull one tool call's arguments, or None if the model answered in prose.

    None is returned rather than raised because each caller has a different correct
    response: OPPORTUNITY treats it as "nothing found", PLAN treats it as a failure.
    """
    for call in completion.tool_calls:
        if isinstance(call, ToolCall) and call.name == name:
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
) -> tuple[dict[str, Any] | None, Decimal]:
    """One model call: assemble, call, return the structured arguments and the cost."""
    messages = [
        system(role, state["dna"], state["remembered"]),
        Message(role=Role.USER, content=body),
    ]
    completion = await deps.router.complete(
        task,
        messages,
        tools=[tool],
        # Current Claude models reject `temperature` outright, and every node here
        # wants the provider default anyway.
        temperature=None,
    )
    return _tool_arguments(completion, tool.name), Decimal(str(completion.usage.usd))


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
        if deps.load_memory:
            try:
                updates["remembered"] = await deps.load_memory()
            except Exception as exc:  # broad on purpose: memory is an enhancement
                logger.warning("intake: could not load memory: %s", exc)
                updates["fact_gaps"] = [*state["fact_gaps"], "remembered preferences"]

        return updates

    async def harvest(state: AgentState) -> dict[str, Any]:
        """Gather evidence from the engines. Engines only — no model, ever.

        Each source is independent and failure is per-source: one dead provider must
        not cost the run the evidence the others returned.
        """
        dna = state["dna"]
        facts: dict[str, Any] = {}
        gaps: list[str] = []

        if deps.crawl_site and dna.get("website"):
            try:
                facts["site"] = await deps.crawl_site(str(dna["website"]))
            except Exception as exc:  # broad on purpose: one dead source must not end the run
                logger.warning("harvest: site crawl failed: %s", exc)
                gaps.append("website crawl")
        elif not dna.get("website"):
            gaps.append("website (none on record)")

        if deps.serp_search:
            seeds = _seed_queries(dna)
            pages: list[SerpPage] = []
            try:
                for seed in seeds[:MAX_SEED_SEARCHES]:
                    pages.append(
                        await deps.serp_search(seed, locale=dna.get("locale", "de"), limit=10)
                    )
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

        if deps.retrieve:
            try:
                trace = await deps.retrieve(state["goal"])
                facts["knowledge"] = {
                    "outcome": getattr(trace, "outcome", None),
                    "chunks": len(getattr(trace, "chunks", []) or []),
                }
            except Exception as exc:  # broad on purpose, per-source degradation
                logger.warning("harvest: retrieval failed: %s", exc)
                gaps.append("uploaded documents")

        return {"facts": facts, "fact_gaps": [*state["fact_gaps"], *gaps]}

    async def opportunity(state: AgentState) -> dict[str, Any]:
        """Rank the evidence into opportunities and take the best.

        An empty list is a legitimate answer, and the graph ends the run honestly on
        it. Inventing a topic to fill the slot is the failure mode here.
        """
        args, cost = await _ask(
            deps,
            task=TaskClass.PRIORITISE,
            role=(
                "You choose what this business should publish next. You rank only what "
                "the evidence supports, and you return an empty list rather than a "
                "weak idea."
            ),
            state=state,
            body=(
                f"Goal: {state['goal']}\n\nEvidence:\n{_evidence(state)}\n\n"
                "Call record_opportunities. Score each 0-100 on likely lead impact "
                "against effort."
            ),
            tool=OPPORTUNITY_TOOL,
        )
        if not args:
            return {"opportunity": None, "_cost": cost}

        ranked = sorted(
            (o for o in args.get("opportunities", []) if o.get("title")),
            key=lambda o: -int(o.get("score", 0)),
        )
        return {"opportunity": ranked[0] if ranked else None, "_cost": cost}

    async def plan(state: AgentState) -> dict[str, Any]:
        """Outline the page. A target keyword is mandatory."""
        opp = state.get("opportunity") or {}
        args, cost = await _ask(
            deps,
            task=TaskClass.PLAN,
            role=(
                "You outline one page. Every section must serve the search intent "
                "behind the target keyword."
            ),
            state=state,
            body=(
                f"Opportunity: {json.dumps(opp, ensure_ascii=False, default=str)}\n\n"
                f"Evidence:\n{_evidence(state)}\n\nCall record_outline."
            ),
            tool=PLAN_TOOL,
        )
        if not args or not str(args.get("target_keyword", "")).strip():
            raise ValueError(
                "the outline has no target keyword; a page without one cannot be "
                "scored, and generating it would waste the run's budget"
            )
        return {"outline": args, "_cost": cost}

    async def generate(state: AgentState) -> dict[str, Any]:
        """Write the page, grounded in the evidence.

        On a retry the SEO findings are passed through VERBATIM. Without that the
        validation loop is theatre: the model would be asked to try again with no idea
        what failed.
        """
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

        args, cost = await _ask(
            deps,
            task=TaskClass.GENERATE,
            role=(
                "You write the page. Every factual claim comes from the evidence or "
                "from the business's own documents; if the evidence does not support a "
                "claim, you leave it out."
            ),
            state=state,
            body=(
                f"Outline: {json.dumps(outline, ensure_ascii=False, default=str)}\n\n"
                f"Evidence:\n{_evidence(state)}{retry}\n\nCall record_page."
            ),
            tool=GENERATE_TOOL,
        )
        if not args:
            raise ValueError("the model did not return a page; expected a record_page tool call")
        return {"draft": args, "_cost": cost}

    async def validate(state: AgentState) -> dict[str, Any]:
        """Score the draft. Deterministic arithmetic, never a model.

        An absent draft scores as failed rather than raising: the graph's retry path
        is the right response, and a crash here would lose the run.
        """
        scorer = deps.score_page or score_page
        draft = state.get("draft") or {}
        outline = state.get("outline") or {}
        html = str(draft.get("html") or "")

        if not html:
            return {
                "seo_report": {
                    "score": 0,
                    "passed": False,
                    "findings": [],
                    "note": "No draft HTML was produced, so there was nothing to score.",
                }
            }

        title = str(draft.get("title") or "")
        meta = str(draft.get("meta_description") or "")
        # The engine scores a document, and GENERATE returns title and meta separately.
        document = (
            f"<html><head><title>{title}</title>"
            f'<meta name="description" content="{meta}">'
            f"</head><body>{html}</body></html>"
        )
        result = scorer(
            SeoScoreRequest(
                html=document,
                target_keyword=str(outline.get("target_keyword") or ""),
                secondary_keywords=list(outline.get("secondary_keywords") or []),
                locale=str(state["dna"].get("locale") or "de"),
            )
        )
        return {"seo_report": result.model_dump(mode="json")}

    async def repack(state: AgentState) -> dict[str, Any]:
        """Render the approved message per channel, then enforce the limits in code."""
        draft = state.get("draft") or {}
        outline = state.get("outline") or {}
        wanted = ", ".join(deps.channels)

        args, cost = await _ask(
            deps,
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
        renderings: dict[str, str] = {}
        for post in (args or {}).get("posts", []):
            channel = str(post.get("channel", "")).lower().strip()
            body = str(post.get("body", "")).strip()
            if not channel or not body:
                continue
            limit = CHANNEL_LIMITS.get(channel)
            if limit and len(body) > limit:
                # Trim on a word boundary. The platform would reject the post
                # outright, so shipping it over-length is not an option.
                body = body[: limit - 1].rsplit(" ", 1)[0] + "…"
            renderings[channel] = body

        return {"renderings": renderings, "_cost": cost}

    async def review(state: AgentState) -> dict[str, Any]:
        """The interrupt point. Nothing to do: the graph pauses here for a human."""
        return {}

    return {
        "INTAKE": intake,
        "HARVEST": harvest,
        "OPPORTUNITY": opportunity,
        "PLAN": plan,
        "GENERATE": generate,
        "VALIDATE": validate,
        "REPACK": repack,
        "REVIEW": review,
    }


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
