"""The real nodes, wired to engines and the router.

Written before the modules. Dependencies are injected, so every test here is
hermetic: no network, no database, no model.

The properties worth testing are the boundaries, not the prose:
  * HARVEST and VALIDATE must call NO model — they are the trustworthy half of the
    pipeline precisely because they are deterministic;
  * a failed fact source must degrade the run and be NAMED in fact_gaps, so the UI
    can say "generated without live research" instead of pretending;
  * GENERATE must actually receive the SEO fix hints on a retry, or the validation
    loop is theatre.
"""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.agents.graph import run_graph
from backend.app.agents.nodes import (
    MAX_RETRIEVAL_TRACES,
    RETRIEVAL_REASON_CHARS,
    NodeDeps,
    append_retrieval_trace,
    build_nodes,
    summarise_retrieval,
)
from backend.app.agents.state import AgentState, from_checkpoint, new_state, to_checkpoint
from backend.app.agents.state_graph import run_state_graph
from backend.app.agents.tools import (
    KB_SEARCH,
    PUBLISH,
    NodeToolbox,
)
from backend.app.engines.seo import SeoFinding, SeoScoreResult
from backend.app.engines.serp import SerpPage, SerpResult
from backend.app.llm import Completion, TaskClass, ToolCall, Usage
from backend.app.services.kb_service import (
    ChunkGrade,
    GroundingChunk,
    RetrievalAttempt,
    RetrievalTrace,
)

BUSINESS = uuid4()


def _usage(usd: str = "0.001") -> Usage:
    return Usage(
        provider="stub",
        model="stub/m",
        tokens_in=50,
        tokens_out=25,
        usd=Decimal(usd),
        latency_ms=4,
    )


class StubRouter:
    """Records every call, answers with a queued tool call per task class."""

    def __init__(self, answers: dict[TaskClass, dict[str, Any]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[TaskClass] = []

    async def complete(
        self,
        task: TaskClass,
        messages: Any,
        *,
        tools: Any = None,
        budget: Any = None,
        temperature: Any = None,
        max_tokens: Any = None,
        # The nodes now pass trace context (business_id, node, prompt_version) so that
        # llm spans and `model_usage` rows are attributable. Accepted here to keep this
        # double matching the real signature; the tests that care about the VALUE assert
        # on it explicitly.
        trace: Any = None,
    ) -> Completion:
        self.calls.append(task)
        payload = self.answers.get(task)
        if payload is None:
            return Completion(text="prose", tool_calls=[], usage=_usage(), is_final=True)
        name = next(iter(tools)).name if tools else "unknown"
        return Completion(
            text=None,
            tool_calls=[ToolCall(name=name, arguments=payload, call_id="c1")],
            usage=_usage(),
            is_final=False,
        )


def _state(**over: Any) -> AgentState:
    state = new_state(
        business_id=BUSINESS,
        goal="more local leads",
        dna={
            "name": "Müller Sanitär GmbH",
            "city": "Koblenz",
            "locale": "de",
            "services": ["Sanitärnotdienst", "Badsanierung"],
            "website": "https://mueller-sanitaer.de",
            "tone": "professional",
        },
    )
    state.update(over)  # type: ignore[typeddict-item]
    return state


def _serp_page(query: str) -> SerpPage:
    return SerpPage(
        query=query,
        locale="de",
        results=[
            SerpResult(
                position=1, url="https://rival.de/x", title="Rival", snippet="", host="rival.de"
            )
        ],
        related_queries=[f"{query} kosten", f"wie funktioniert {query}"],
    )


def _deps(router: StubRouter | None = None, **over: Any) -> NodeDeps:
    async def crawl(url: str) -> dict[str, Any]:
        return {
            "pages": 12,
            "title": "Müller Sanitär",
            "issues": ["6 pages without a meta description"],
        }

    async def search(query: str, *, locale: str, limit: int) -> SerpPage:
        return _serp_page(query)

    base: dict[str, Any] = {
        "router": router or StubRouter(),
        "crawl_site": crawl,
        "serp_search": search,
        "score_page": None,  # default: the real deterministic engine
        "retrieve": None,  # default: skipped when no store is configured
    }
    base.update(over)
    return NodeDeps(**base)


# --------------------------------------------------------------------------- #
# HARVEST — engines only
# --------------------------------------------------------------------------- #


async def test_harvest_gathers_facts_and_calls_no_model() -> None:
    router = StubRouter()
    nodes = build_nodes(_deps(router))

    updates = await nodes["HARVEST"](_state())

    assert updates["facts"]["site"]["pages"] == 12
    assert updates["facts"]["keywords"], "expansion produced nothing"
    assert updates["facts"]["competitors"][0]["host"] == "rival.de"
    assert router.calls == [], "HARVEST must not call a model — it is the deterministic half"


async def test_harvest_names_a_failed_source_in_fact_gaps_and_keeps_going() -> None:
    """A dead SERP provider is ordinary. The run continues with less evidence and the
    UI is told which evidence is missing."""

    async def broken_search(query: str, *, locale: str, limit: int) -> SerpPage:
        raise RuntimeError("search quota exhausted")

    updates = await build_nodes(_deps(serp_search=broken_search))["HARVEST"](_state())

    assert "keywords" not in updates["facts"] or not updates["facts"].get("keywords")
    assert any("search" in gap for gap in updates["fact_gaps"])
    assert updates["facts"]["site"]["pages"] == 12, "the surviving source still landed"


async def test_harvest_with_every_source_down_still_returns_rather_than_raising() -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    updates = await build_nodes(_deps(crawl_site=boom, serp_search=boom))["HARVEST"](_state())

    assert len(updates["fact_gaps"]) >= 2
    assert updates["facts"] == {} or all(not v for v in updates["facts"].values())


# --------------------------------------------------------------------------- #
# OPPORTUNITY
# --------------------------------------------------------------------------- #


async def test_opportunity_picks_the_highest_scoring_candidate() -> None:
    router = StubRouter(
        {
            TaskClass.PRIORITISE: {
                "opportunities": [
                    {
                        "title": "Blog about pipes",
                        "rationale": "r",
                        "target_keywords": ["pipes"],
                        "expected_impact": "low",
                        "effort": "low",
                        "score": 30,
                    },
                    {
                        "title": "Notdienst Koblenz page",
                        "rationale": "r",
                        "target_keywords": ["notdienst klempner koblenz"],
                        "expected_impact": "high",
                        "effort": "medium",
                        "score": 88,
                    },
                ]
            }
        }
    )
    updates = await build_nodes(_deps(router))["OPPORTUNITY"](_state(facts={"keywords": []}))

    assert updates["opportunity"]["title"] == "Notdienst Koblenz page"
    assert updates["_cost"] > 0, "the model call must be charged to the run"


async def test_opportunity_returns_none_when_the_model_finds_nothing() -> None:
    """The graph ends the run honestly on this. Inventing a topic is the failure mode."""
    router = StubRouter({TaskClass.PRIORITISE: {"opportunities": []}})
    updates = await build_nodes(_deps(router))["OPPORTUNITY"](_state())
    assert updates["opportunity"] is None


async def test_opportunity_returns_none_when_the_model_answers_in_prose() -> None:
    updates = await build_nodes(_deps(StubRouter()))["OPPORTUNITY"](_state())
    assert updates["opportunity"] is None


# --------------------------------------------------------------------------- #
# PLAN
# --------------------------------------------------------------------------- #


async def test_plan_rejects_an_outline_with_no_target_keyword() -> None:
    router = StubRouter({TaskClass.PLAN: {"headings": ["H2 one"], "target_keyword": ""}})
    with pytest.raises(ValueError, match="target keyword"):
        await build_nodes(_deps(router))["PLAN"](_state(opportunity={"title": "t"}))


async def test_plan_keeps_the_target_keyword_and_headings() -> None:
    router = StubRouter(
        {
            TaskClass.PLAN: {
                "target_keyword": "notdienst klempner koblenz",
                "secondary_keywords": ["rohrbruch koblenz"],
                "headings": ["Wann rufen", "Was kostet"],
                "answer_blocks": ["Ein Notdienst kostet…"],
                "cta": "Jetzt anrufen",
            }
        }
    )
    updates = await build_nodes(_deps(router))["PLAN"](_state(opportunity={"title": "t"}))

    assert updates["outline"]["target_keyword"] == "notdienst klempner koblenz"
    assert len(updates["outline"]["headings"]) == 2


# --------------------------------------------------------------------------- #
# GENERATE — and the retry that makes VALIDATE mean something
# --------------------------------------------------------------------------- #


async def test_generate_produces_html_and_charges_the_run() -> None:
    router = StubRouter(
        {
            TaskClass.GENERATE: {
                "title": "Notdienst Klempner Koblenz",
                "meta_description": "d" * 150,
                "html": "<h1>Notdienst</h1><p>text</p>",
            }
        }
    )
    updates = await build_nodes(_deps(router))["GENERATE"](
        _state(outline={"target_keyword": "notdienst klempner koblenz", "headings": []})
    )

    assert "<h1>" in updates["draft"]["html"]
    assert updates["_cost"] > 0


async def test_generate_receives_the_seo_fix_hints_verbatim_on_a_retry() -> None:
    """Without this the validation loop is theatre: the model would be asked to try
    again with no idea what failed."""
    captured: list[str] = []

    class CapturingRouter(StubRouter):
        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            captured.append("\n".join(str(m.content) for m in messages))
            return await super().complete(task, messages, **kw)

    router = CapturingRouter(
        {TaskClass.GENERATE: {"title": "t", "meta_description": "d" * 150, "html": "<h1>x</h1>"}}
    )
    report = SeoScoreResult(
        score=71,
        passed=False,
        findings=[
            SeoFinding(
                code="meta_length",
                severity="warn",
                message="Meta description is 118 characters.",
                fix_hint=(
                    "Extend the meta description to 140-160 characters; it currently stops at 118."
                ),
                measured=118.0,
                expected="140-160",
            )
        ],
    )

    await build_nodes(_deps(router))["GENERATE"](
        _state(
            outline={"target_keyword": "k", "headings": []},
            seo_report=report.model_dump(),
            validate_loops=1,
        )
    )

    prompt = captured[0]
    assert "it currently stops at 118" in prompt, "the fix hint was not passed through"
    assert "71" in prompt, "the model should be told the score it has to beat"


# --------------------------------------------------------------------------- #
# VALIDATE — deterministic, no model
# --------------------------------------------------------------------------- #


async def test_validate_scores_with_the_engine_and_calls_no_model() -> None:
    router = StubRouter()
    nodes = build_nodes(_deps(router))

    updates = await nodes["VALIDATE"](
        _state(
            outline={"target_keyword": "notdienst", "headings": []},
            draft={
                "html": "<html><head><title>Notdienst Klempner Koblenz Sanitär</title></head>"
                "<body><h1>Notdienst</h1><p>Notdienst text</p></body></html>"
            },
        )
    )

    assert isinstance(updates["seo_report"]["score"], int)
    assert "passed" in updates["seo_report"]
    assert router.calls == [], "scoring is arithmetic, not a language task"


async def test_validate_on_an_empty_draft_fails_rather_than_crashing() -> None:
    updates = await build_nodes(_deps())["VALIDATE"](
        _state(outline={"target_keyword": "k", "headings": []}, draft=None)
    )
    assert updates["seo_report"]["passed"] is False


# --------------------------------------------------------------------------- #
# REPACK
# --------------------------------------------------------------------------- #


async def test_repack_renders_one_post_per_requested_channel() -> None:
    router = StubRouter(
        {
            TaskClass.REPACK: {
                "posts": [
                    {"channel": "linkedin", "body": "b" * 400},
                    {"channel": "facebook", "body": "b" * 200},
                ]
            }
        }
    )
    updates = await build_nodes(_deps(router))["REPACK"](
        _state(
            draft={"html": "<h1>x</h1>", "title": "t"},
            outline={"target_keyword": "k", "headings": []},
        )
    )

    assert set(updates["renderings"]) == {"linkedin", "facebook"}


async def test_repack_truncates_an_over_length_post_rather_than_shipping_it() -> None:
    """Length is arithmetic, so Python enforces it after generation: the platform
    would reject an over-length post outright."""
    router = StubRouter(
        {TaskClass.REPACK: {"posts": [{"channel": "linkedin", "body": "x" * 5000}]}}
    )
    updates = await build_nodes(_deps(router))["REPACK"](
        _state(
            draft={"html": "<h1>x</h1>", "title": "t"},
            outline={"target_keyword": "k", "headings": []},
        )
    )

    # The BODY, not the rendering dict. `len()` of the dict is 5 whatever the post
    # says, so asserting on it would be a test that could never fail.
    assert len(updates["renderings"]["linkedin"]["body"]) <= 3000


async def test_repack_keeps_the_hashtags_it_asked_the_model_for() -> None:
    """`REPACK_TOOL` accepts `posts[].hashtags` and the node used to store only the
    body, so they never reached the checkpoint and the social tab could not show
    them. Declared tags survive, in the model's order, prefixed."""
    router = StubRouter(
        {
            TaskClass.REPACK: {
                "posts": [
                    {
                        "channel": "linkedin",
                        "body": "Kurz erklärt, was ein Notar beurkundet.",
                        "hashtags": ["Notar", "#Koblenz"],
                    }
                ]
            }
        }
    )
    updates = await build_nodes(_deps(router))["REPACK"](
        _state(
            draft={"html": "<h1>x</h1>", "title": "t"},
            outline={"target_keyword": "k", "headings": []},
        )
    )

    assert updates["renderings"]["linkedin"]["hashtags"] == ["#Notar", "#Koblenz"]


async def test_repack_enforces_the_hashtag_cap_in_code_and_says_how_many_it_cut() -> None:
    """The `channel` engine measured a model producing 21 hashtags against a prompt
    whose last line said "Keine Hashtags". LinkedIn's cap is 3, so an eight-tag post
    is brought inside it -- and `hashtags_removed` reports that it had to be, because
    a clean post shown without that number credits the model for the renderer's work.
    """
    body = "Was ein Notar beurkundet. " + " ".join(f"#tag{n}" for n in range(8))
    router = StubRouter({TaskClass.REPACK: {"posts": [{"channel": "linkedin", "body": body}]}})
    updates = await build_nodes(_deps(router))["REPACK"](
        _state(
            draft={"html": "<h1>x</h1>", "title": "t"},
            outline={"target_keyword": "k", "headings": []},
        )
    )

    rendering = updates["renderings"]["linkedin"]
    assert len(rendering["hashtags"]) == 3
    assert rendering["hashtags_removed"] == 5
    assert rendering["body"].count("#") == 3, "the cut tags must be gone from the body too"


async def test_repack_reports_a_hashtag_shortfall_and_never_invents_one() -> None:
    """Instagram wants at least three. Given one, the node says two are missing and
    leaves the copy alone: fabricating a tag here would be a node writing marketing."""
    router = StubRouter(
        {
            TaskClass.REPACK: {
                "posts": [{"channel": "instagram", "body": "Sanfte Behandlung. #Praxis"}]
            }
        }
    )
    updates = await build_nodes(_deps(router))["REPACK"](
        _state(
            draft={"html": "<h1>x</h1>", "title": "t"},
            outline={"target_keyword": "k", "headings": []},
        )
    )

    rendering = updates["renderings"]["instagram"]
    assert rendering["hashtags"] == ["#Praxis"]
    assert rendering["hashtags_shortfall"] == 2
    assert rendering["hashtags_removed"] == 0


async def test_repack_reports_being_over_the_editorial_target_without_truncating_to_it() -> None:
    """1,900 characters publishes fine on LinkedIn and is longer than it should be.
    The platform limit is 3,000, so the post is NOT cut -- being over the target is a
    fact about the copy, and truncating to it would be enforcement by preference."""
    router = StubRouter(
        {TaskClass.REPACK: {"posts": [{"channel": "linkedin", "body": "w" * 1900}]}}
    )
    updates = await build_nodes(_deps(router))["REPACK"](
        _state(
            draft={"html": "<h1>x</h1>", "title": "t"},
            outline={"target_keyword": "k", "headings": []},
        )
    )

    rendering = updates["renderings"]["linkedin"]
    assert rendering["over_target"] is True
    assert len(rendering["body"]) == 1900


# --------------------------------------------------------------------------- #
# The four tools docs/AGENT_RUNTIME.md granted and nothing implemented
# --------------------------------------------------------------------------- #


class _Trace:
    """A retrieval trace, in the shape `_passages` reads."""

    outcome = "sufficient"

    def __init__(self, text: str = "Notdienst rund um die Uhr, 89 EUR Anfahrt.") -> None:
        self.chunks = [
            type(
                "Chunk", (), {"document_id": "doc-1", "ordinal": 0, "text": text, "source": None}
            )()
        ]


async def test_opportunity_reads_the_businesss_own_documents() -> None:
    """It held `kb.search` and never called it, so a topic was chosen from the crawl
    and the SERP alone — which is exactly the evidence a competitor also has."""
    asked: list[str] = []

    async def retrieve(question: str, **kwargs: Any) -> Any:
        asked.append(question)
        return _Trace()

    router = StubRouter({TaskClass.PRIORITISE: {"opportunities": [{"title": "t", "score": 70}]}})
    await build_nodes(_deps(router, retrieve=retrieve))["OPPORTUNITY"](_state())

    assert asked == ["more local leads"], "the run goal is what a topic is chosen against"


async def test_plan_retrieves_against_the_chosen_opportunity_not_the_run_goal() -> None:
    """The goal is "more local leads"; retrieving against it returns whatever is most
    on-topic in general. An outline needs the material about the thing being written."""
    asked: list[str] = []

    async def retrieve(question: str, **kwargs: Any) -> Any:
        asked.append(question)
        return _Trace()

    router = StubRouter({TaskClass.PLAN: {"target_keyword": "notdienst koblenz"}})
    await build_nodes(_deps(router, retrieve=retrieve))["PLAN"](
        _state(opportunity={"title": "Sanitärnotdienst nachts"})
    )

    assert asked == ["Sanitärnotdienst nachts"]


async def test_generate_retrieves_against_the_target_keyword() -> None:
    """This is the node where a missing passage becomes an invented sentence, so it is
    the node where the unwired grant mattered most."""
    asked: list[str] = []

    async def retrieve(question: str, **kwargs: Any) -> Any:
        asked.append(question)
        return _Trace()

    router = StubRouter(
        {TaskClass.GENERATE: {"title": "t", "meta_description": "m", "html": "<h1>t</h1>"}}
    )
    await build_nodes(_deps(router, retrieve=retrieve))["GENERATE"](
        _state(outline={"target_keyword": "notdienst koblenz", "headings": []})
    )

    assert asked == ["notdienst koblenz"]


async def test_a_retrieval_failure_costs_the_passages_and_not_the_run() -> None:
    """Grounding is an enhancement to a prompt. Losing it must not cost the run the
    work every other source produced."""

    async def exploding(question: str, **kwargs: Any) -> Any:
        raise RuntimeError("pgvector is unreachable")

    router = StubRouter(
        {TaskClass.GENERATE: {"title": "t", "meta_description": "m", "html": "<h1>t</h1>"}}
    )
    updates = await build_nodes(_deps(router, retrieve=exploding))["GENERATE"](
        _state(outline={"target_keyword": "k", "headings": []})
    )

    assert updates["draft"]["title"] == "t"


async def test_harvest_audits_the_address_the_site_publishes_about_itself() -> None:
    """`nap.audit` was granted to HARVEST and implemented by nothing, because nothing
    produced listings. They come from the crawl summary now."""

    async def crawl(url: str) -> dict[str, Any]:
        return {
            "pages": [],
            "nap_sources": [
                {
                    "source": "website_jsonld",
                    "trading_name": "Müller Sanitär GmbH",
                    "postcode": "56068",
                    "city": "Koblenz",
                    "phone": "+49 261 999999",
                }
            ],
        }

    updates = await build_nodes(_deps(crawl_site=crawl))["HARVEST"](_state())

    nap = updates["facts"]["nap"]
    assert nap["sources"] == ["website_jsonld"]
    assert isinstance(nap["consistency_score"], int)
    # The scope travels with the score, because "94" will otherwise be read as "your
    # address is consistent online" — which is a claim about directories we did not check.
    assert "does not check external directories" in nap["scope"]


async def test_a_site_that_publishes_no_address_is_a_gap_and_not_a_clean_audit() -> None:
    """An audit of zero listings scores 100, which would tell a business its address is
    consistent everywhere it is not listed."""

    async def crawl(url: str) -> dict[str, Any]:
        return {"pages": [], "nap_sources": []}

    updates = await build_nodes(_deps(crawl_site=crawl))["HARVEST"](_state())

    assert "nap" not in updates["facts"]
    assert any("address consistency" in gap for gap in updates["fact_gaps"])


async def test_harvest_carries_ai_visibility_as_evidence_with_its_denominator() -> None:
    """An opportunity is worth more when the business is ABSENT from the answers people
    already get, which is why this is evidence and not only a dashboard tile."""

    async def probe(dna: Any, **kwargs: Any) -> dict[str, Any]:
        return {"headline": "12.5% — mentioned in 1 of 8 answers", "usable_answers": 8}

    updates = await build_nodes(_deps(geo_probe=probe))["HARVEST"](_state())

    assert updates["facts"]["visibility"]["usable_answers"] == 8


async def test_an_unconfigured_probe_is_named_as_a_gap() -> None:
    """The review screen shows this under what the work was written WITHOUT. Silence
    would let the draft imply a visibility check that never happened."""
    updates = await build_nodes(_deps())["HARVEST"](_state())

    assert any("AI answer-engine visibility" in gap for gap in updates["fact_gaps"])


class SearchingRouter(StubRouter):
    """Asks for `web_search` once, then records the page.

    The default `StubRouter` answers with `tools[0]`, which is the OUTPUT tool — that
    ordering is what keeps every other test in this file on the normal path. This one
    deliberately reaches for the search first.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__({TaskClass.GENERATE: payload})
        self.searched = 0

    async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
        self.calls.append(task)
        offered = {spec.name for spec in (kw.get("tools") or [])}
        if "web_search" in offered and self.searched == 0:
            self.searched += 1
            return Completion(
                text=None,
                tool_calls=[
                    ToolCall(
                        name="web_search",
                        arguments={"query": "notdienst preise koblenz"},
                        call_id="s1",
                    )
                ],
                usage=_usage(),
                is_final=False,
            )
        return Completion(
            text=None,
            tool_calls=[
                ToolCall(
                    name="record_page", arguments=self.answers[TaskClass.GENERATE], call_id="c1"
                )
            ],
            usage=_usage(),
            is_final=False,
        )


async def test_generate_can_search_mid_draft_and_then_write() -> None:
    """`web_search` was in GENERATE's allowlist with nothing behind it, so the node
    could never do the thing the design says it can: notice that a fact is missing and
    check it rather than guessing."""
    queries: list[str] = []

    async def search(query: str, **kwargs: Any) -> SerpPage:
        queries.append(query)
        return _serp_page(query)

    router = SearchingRouter({"title": "t", "meta_description": "m", "html": "<h1>t</h1>"})
    updates = await build_nodes(_deps(router, web_search=search))["GENERATE"](
        _state(outline={"target_keyword": "k", "headings": []})
    )

    assert queries == ["notdienst preise koblenz"]
    assert updates["draft"]["title"] == "t", "the search must not cost the page"


async def test_an_unconfigured_search_is_never_offered_to_the_model() -> None:
    """docs/AGENT_RUNTIME.md §4: a tool the node will not get is REMOVED rather than
    refused on call, so the model never plans around a capability it cannot have. An
    unwired search is exactly that — and handing back an empty result instead would be
    read as "there is nothing to find", which the model would write around as though
    the absence were a fact."""
    router = SearchingRouter({"title": "t", "meta_description": "m", "html": "<h1>t</h1>"})
    updates = await build_nodes(_deps(router, web_search=None))["GENERATE"](
        _state(outline={"target_keyword": "k", "headings": []})
    )

    # The page still gets written, and the model was told the search did not happen.
    assert updates["draft"]["title"] == "t"
    assert router.searched == 0, "a tool that is not wired is not offered in the first place"


async def test_the_search_loop_is_bounded() -> None:
    """A model offered a third search after two will take it, and the node needs a
    page, not a bibliography."""
    calls = 0

    class AlwaysSearching(StubRouter):
        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            nonlocal calls
            calls += 1
            offered = {spec.name for spec in (kw.get("tools") or [])}
            if "web_search" in offered:
                return Completion(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            name="web_search", arguments={"query": "more"}, call_id=f"s{calls}"
                        )
                    ],
                    usage=_usage(),
                    is_final=False,
                )
            return Completion(text="prose", tool_calls=[], usage=_usage(), is_final=True)

    async def search(query: str, **kwargs: Any) -> SerpPage:
        return _serp_page(query)

    with pytest.raises(ValueError, match="did not return a page"):
        await build_nodes(_deps(AlwaysSearching(), web_search=search))["GENERATE"](
            _state(outline={"target_keyword": "k", "headings": []})
        )

    # Two search rounds, then one final ask with the search withdrawn.
    assert calls == 3


# --------------------------------------------------------------------------- #
# The set is complete and the graph can drive it
# --------------------------------------------------------------------------- #


def test_every_node_the_graph_expects_is_provided() -> None:
    from backend.app.agents.graph import ORDER

    nodes = build_nodes(_deps())
    missing = [name for name in ORDER if name not in nodes]
    assert not missing, f"the graph would KeyError on: {missing}"


async def test_intake_loads_memory_into_the_state_so_it_reaches_the_prompt() -> None:
    """Before this wiring, `remembered` was read by prompts.system() but written by
    nothing — the feature existed on both sides of a gap with nothing across it."""

    async def load() -> list[str]:
        return ["never use exclamation marks", "always mention the 24h Notdienst"]

    nodes = build_nodes(_deps(load_memory=load))
    updates = await nodes["INTAKE"](_state())

    assert updates["remembered"] == [
        "never use exclamation marks",
        "always mention the 24h Notdienst",
    ]


async def test_a_remembered_preference_actually_appears_in_the_system_prompt() -> None:
    """The end-to-end claim, asserted on the assembled prompt rather than inferred."""
    router = StubRouter(
        {
            TaskClass.PLAN: {
                "target_keyword": "k",
                "headings": ["h"],
            }
        }
    )
    captured: list[str] = []

    class Capturing(StubRouter):
        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            captured.append("\n".join(str(m.content) for m in messages))
            return await super().complete(task, messages, **kw)

    capturing = Capturing({TaskClass.PLAN: {"target_keyword": "k", "headings": ["h"]}})
    nodes = build_nodes(_deps(capturing))
    await nodes["PLAN"](
        _state(opportunity={"title": "t"}, remembered=["never use exclamation marks"])
    )

    assert "never use exclamation marks" in captured[0]
    assert "- never use exclamation marks" in captured[0], "one rule per line"
    assert router is not None


async def test_memory_that_fails_to_load_degrades_the_run_rather_than_ending_it() -> None:
    async def broken() -> list[str]:
        raise RuntimeError("database unreachable")

    updates = await build_nodes(_deps(load_memory=broken))["INTAKE"](_state())

    assert "remembered" not in updates
    assert any("remembered" in gap for gap in updates["fact_gaps"])


# --------------------------------------------------------------------------- #
# The tool allowlist, at the node level
#
# `test_tool_allowlist.py` tests the gate; these test that the nodes actually go
# THROUGH it. A perfectly correct allowlist nothing calls is worth nothing.
# --------------------------------------------------------------------------- #


async def test_harvest_reaches_its_engines_only_through_the_allowlist() -> None:
    """Every source HARVEST uses is one of its granted tools, so the grant list is the
    complete description of what the node can touch."""
    from backend.app.agents.tools import CRAWL_SITE, NODE_TOOLS, SERP_SEARCH

    granted = NODE_TOOLS["HARVEST"]
    assert {CRAWL_SITE, SERP_SEARCH, KB_SEARCH} <= granted


async def test_a_node_asking_for_a_tool_it_does_not_hold_fails_loudly() -> None:
    """The refusal must not be degraded into a fact gap. HARVEST swallows a dead
    provider on purpose, and an allowlist violation must not ride out on that path:
    a dead provider is ordinary, a capability nobody granted is not."""
    from backend.app.agents.tools import ToolNotAllowedError

    box = NodeToolbox(node="HARVEST", implementations={PUBLISH: lambda: None})
    with pytest.raises(ToolNotAllowedError):
        await box.call(PUBLISH)


async def test_a_model_tool_call_outside_the_allowlist_is_dropped_and_recorded() -> None:
    """The node keeps the legitimate output and records the refusal, rather than
    executing the call or failing the run."""

    class Rogue(StubRouter):
        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            self.calls.append(task)
            return Completion(
                text=None,
                tool_calls=[
                    ToolCall(name="publish", arguments={}, call_id="c0"),
                    ToolCall(
                        name="record_outline",
                        arguments={"target_keyword": "notdienst", "headings": ["h"]},
                        call_id="c1",
                    ),
                ],
                usage=_usage(),
                is_final=False,
            )

    updates = await build_nodes(_deps(Rogue()))["PLAN"](_state(opportunity={"title": "t"}))

    assert updates["outline"]["target_keyword"] == "notdienst"
    codes = [e.code for e in updates["errors"]]
    assert codes == ["tool_not_allowed"]
    assert "publish" in updates["errors"][0].message


async def test_a_refusal_appends_to_the_existing_errors_rather_than_replacing_them() -> None:
    """The graph MERGES a node's updates into the state, so returning `errors` as a
    fresh list would silently delete every earlier degradation."""
    from backend.app.agents.state import NodeError

    class Rogue(StubRouter):
        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            self.calls.append(task)
            return Completion(
                text=None,
                tool_calls=[
                    ToolCall(name="notify", arguments={}, call_id="c0"),
                    ToolCall(
                        name="record_outline",
                        arguments={"target_keyword": "k", "headings": ["h"]},
                        call_id="c1",
                    ),
                ],
                usage=_usage(),
                is_final=False,
            )

    earlier = NodeError(node="HARVEST", code="node_failed", message="serp down")
    updates = await build_nodes(_deps(Rogue()))["PLAN"](
        _state(opportunity={"title": "t"}, errors=[earlier])
    )

    assert [e.code for e in updates["errors"]] == ["node_failed", "tool_not_allowed"]


# --------------------------------------------------------------------------- #
# VALIDATE's second verdict: the regulated-claim gate
# --------------------------------------------------------------------------- #

BANNED_DNA = {
    "name": "Zahnarztpraxis Koblenz",
    "city": "Koblenz",
    "locale": "de",
    "services": ["Prophylaxe"],
    "website": "https://praxis.de",
    "tone": "professional",
    "banned_claims": ["schmerzfrei", "beste Zahnarztpraxis"],
}


async def test_validate_blocks_a_draft_that_makes_a_banned_claim_and_calls_no_model() -> None:
    """The claim list is in the system prompt as well, but a prompt is a request.
    This is the control: arithmetic over the list, downstream of the model."""
    router = StubRouter()
    updates = await build_nodes(_deps(router))["VALIDATE"](
        _state(
            dna=BANNED_DNA,
            outline={"target_keyword": "zahnarzt", "headings": []},
            draft={
                "title": "Zahnarzt Koblenz",
                "meta_description": "d" * 150,
                "html": "<h1>Praxis</h1><p>Eine schmerzfreie Behandlung.</p>",
            },
        )
    )

    assert updates["claim_check"]["passed"] is False
    assert updates["claim_check"]["hits"][0]["claim"] == "schmerzfrei"
    assert router.calls == [], "a compliance gate must not depend on a model call"


async def test_validate_checks_the_meta_description_not_only_the_body() -> None:
    """The meta description lives in an ATTRIBUTE of the assembled document, so a
    markup-stripping matcher would drop it -- and it is the one line Google shows."""
    updates = await build_nodes(_deps())["VALIDATE"](
        _state(
            dna=BANNED_DNA,
            outline={"target_keyword": "zahnarzt", "headings": []},
            draft={
                "title": "Zahnarzt Koblenz",
                "meta_description": "Die beste Zahnarztpraxis in Koblenz, jetzt Termin buchen.",
                "html": "<h1>Praxis</h1><p>Sanfte Behandlung.</p>",
            },
        )
    )

    assert updates["claim_check"]["passed"] is False
    assert updates["claim_check"]["hits"][0]["claim"] == "beste Zahnarztpraxis"


async def test_validate_reports_a_clean_draft_as_checked_rather_than_unexercised() -> None:
    updates = await build_nodes(_deps())["VALIDATE"](
        _state(
            dna=BANNED_DNA,
            outline={"target_keyword": "zahnarzt", "headings": []},
            draft={"title": "t", "meta_description": "d" * 150, "html": "<h1>Sanft</h1>"},
        )
    )

    assert updates["claim_check"]["passed"] is True
    assert updates["claim_check"]["exercised"] is True


async def test_a_business_with_no_banned_claims_is_reported_as_not_exercised() -> None:
    """A vacuous pass must not render as a compliance tick on the review screen."""
    updates = await build_nodes(_deps())["VALIDATE"](
        _state(
            outline={"target_keyword": "k", "headings": []},
            draft={"title": "t", "meta_description": "d" * 150, "html": "<h1>x</h1>"},
        )
    )

    assert updates["claim_check"]["passed"] is True
    assert updates["claim_check"]["exercised"] is False


async def test_an_empty_draft_still_produces_a_claim_verdict() -> None:
    """Otherwise the graph would see `claim_check is None` and read "nothing checked"
    on a path where nothing was checked for a different reason."""
    updates = await build_nodes(_deps())["VALIDATE"](
        _state(dna=BANNED_DNA, outline={"target_keyword": "k", "headings": []}, draft=None)
    )

    assert updates["seo_report"]["passed"] is False
    assert updates["claim_check"]["passed"] is True
    assert updates["claim_check"]["exercised"] is True


async def test_generate_receives_the_banned_claim_verbatim_on_a_retry() -> None:
    """Same reasoning as the SEO fix hints: without the phrase, the retry is a guess.
    The hint must also forbid paraphrasing, or the model produces copy that passes the
    matcher while making the same forbidden promise."""
    captured: list[str] = []

    class Capturing(StubRouter):
        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            captured.append("\n".join(str(m.content) for m in messages))
            return await super().complete(task, messages, **kw)

    router = Capturing(
        {TaskClass.GENERATE: {"title": "t", "meta_description": "d" * 150, "html": "<h1>x</h1>"}}
    )
    await build_nodes(_deps(router))["GENERATE"](
        _state(
            dna=BANNED_DNA,
            outline={"target_keyword": "k", "headings": []},
            claim_check={
                "passed": False,
                "exercised": True,
                "checked": 2,
                "hits": [
                    {
                        "claim": "schmerzfrei",
                        "matched": "schmerzfreie",
                        "start": 0,
                        "end": 12,
                        "context": "c",
                    }
                ],
                "detail": "d",
                "fix_hint": (
                    'Remove the forbidden claim "schmerzfrei" (it appears as '
                    '"schmerzfreie"). Do not paraphrase it.'
                ),
            },
            validate_loops=1,
        )
    )

    prompt = captured[0]
    assert "schmerzfreie" in prompt, "the model must be shown the words it wrote"
    assert "do not paraphrase" in prompt.lower()


# --------------------------------------------------------------------------- #
# REPACK: a social post is separate content, and gets its own check
# --------------------------------------------------------------------------- #


async def test_repack_withholds_a_post_that_makes_a_banned_claim() -> None:
    """A page can pass VALIDATE and a post derived from it can still carry a forbidden
    claim. There is no per-channel retry in the graph, so the post is dropped rather
    than published."""
    router = StubRouter(
        {
            TaskClass.REPACK: {
                "posts": [
                    {"channel": "linkedin", "body": "Unsere Praxis behandelt sanft. " * 5},
                    {"channel": "facebook", "body": "Eine schmerzfreie Behandlung, versprochen."},
                ]
            }
        }
    )
    updates = await build_nodes(_deps(router))["REPACK"](
        _state(
            dna=BANNED_DNA,
            draft={"html": "<h1>x</h1>", "title": "t"},
            outline={"target_keyword": "k", "headings": []},
        )
    )

    assert "linkedin" in updates["renderings"]
    assert "facebook" not in updates["renderings"], "the offending post must not be published"
    blocked = [e for e in updates["errors"] if e.code == "banned_claim"]
    assert len(blocked) == 1
    assert "facebook" in blocked[0].message, "the withheld channel must be named"
    assert "schmerzfrei" in blocked[0].message


# --------------------------------------------------------------------------- #
# CONVERT: the conversion surface
#
# The properties worth testing are the same three as everywhere else in this file
# -- the boundary, the degradation, and whether the retry actually carries the
# information it claims to. Prose quality is an evaluation question, not this one.
# --------------------------------------------------------------------------- #

DISTRIBUTION_ARGS: dict[str, Any] = {
    "destination_url": "https://mueller-sanitaer.de/notdienst",
    "ctas": [
        {"channel": "linkedin", "text": "Unsere Notdienst-Checkliste:"},
        {"channel": "facebook", "text": "Wasserschaden? Erst diese fünf Prüfungen:"},
        {"channel": "instagram", "text": "Checkliste im Profil-Link:"},
        {"channel": "link_hub", "text": "Notdienst-Checkliste, kostenlos"},
    ],
}

CONVERT_STATE: dict[str, Any] = {
    "outline": {"target_keyword": "notdienst klempner koblenz", "headings": ["h"], "cta": "call"},
    "draft": {
        "title": "Notdienst Klempner Koblenz",
        "meta_description": "d" * 150,
        "html": "<h1>x</h1>",
    },
    "facts": {
        "site": {
            "pages": [
                {"url": "https://mueller-sanitaer.de/"},
                {"url": "https://mueller-sanitaer.de/notdienst"},
            ]
        }
    },
}


def _distribution_router(args: dict[str, Any] | None = None) -> StubRouter:
    return StubRouter({TaskClass.REPACK: args if args is not None else DISTRIBUTION_ARGS})


async def test_convert_writes_a_distribution_plan_and_charges_the_run() -> None:
    router = _distribution_router()

    updates = await build_nodes(_deps(router))["CONVERT"](_state(**CONVERT_STATE))

    plan = updates["distribution"]
    assert plan["destination_url"] == "https://mueller-sanitaer.de/notdienst"
    assert {cta["channel"] for cta in plan["ctas"]} == {
        "linkedin",
        "facebook",
        "instagram",
        "link_hub",
    }
    assert updates["_cost"] > 0


async def test_convert_stores_primitives_so_the_checkpoint_survives() -> None:
    """The state is serialised to JSONB. A model in there either raises on the way out or
    silently stringifies, and the second is worse: the run resumes with a string where it
    expects a structure."""
    import json

    updates = await build_nodes(_deps(_distribution_router()))["CONVERT"](_state(**CONVERT_STATE))

    json.dumps(updates["distribution"])  # must not raise


async def test_convert_offers_the_crawled_pages_rather_than_inviting_invention() -> None:
    """A URL the model made up is refused at publish time by the origin check — safe, and
    a wasted run. So the pages the crawl actually found are in the prompt."""

    class Capturing(StubRouter):
        def __init__(self) -> None:
            super().__init__({TaskClass.REPACK: DISTRIBUTION_ARGS})
            self.bodies: list[str] = []

        async def complete(self, task: Any, messages: Any, **kw: Any) -> Completion:
            self.bodies.append(str(messages[-1].content))
            return await super().complete(task, messages, **kw)

    router = Capturing()
    await build_nodes(_deps(router))["CONVERT"](_state(**CONVERT_STATE))

    assert "https://mueller-sanitaer.de/notdienst" in router.bodies[0]


async def test_convert_always_asks_for_a_bio_link_hub_cta() -> None:
    """An Instagram or TikTok caption cannot carry a clickable link at all, so the hub is
    the ENTIRE conversion path for those channels — and a hub with no ask on it is an
    empty page in somebody's bio."""

    class Capturing(StubRouter):
        def __init__(self) -> None:
            super().__init__({TaskClass.REPACK: DISTRIBUTION_ARGS})
            self.bodies: list[str] = []

        async def complete(self, task: Any, messages: Any, **kw: Any) -> Completion:
            self.bodies.append(str(messages[-1].content))
            return await super().complete(task, messages, **kw)

    router = Capturing()
    await build_nodes(_deps(router))["CONVERT"](_state(**CONVERT_STATE, channels=["linkedin"]))

    assert "Channels needing an ask: linkedin, link_hub" in router.bodies[0]


async def test_convert_falls_back_to_the_homepage_when_the_model_names_no_page() -> None:
    """A plan with no target has reach and no conversion path. The homepage is the one
    page every business has, so it is the honest default rather than an error."""
    args = {**DISTRIBUTION_ARGS, "destination_url": "  "}

    updates = await build_nodes(_deps(_distribution_router(args)))["CONVERT"](
        _state(**CONVERT_STATE)
    )

    assert updates["distribution"]["destination_url"] == "https://mueller-sanitaer.de"


async def test_convert_canonicalises_a_channel_alias_in_the_ask() -> None:
    """`facebook_post` and `facebook` are one channel. Two asks for one channel would
    produce two posts from one rendering."""
    args = {
        **DISTRIBUTION_ARGS,
        "ctas": [{"channel": "facebook_post", "text": "Wasserschaden?"}],
    }

    updates = await build_nodes(_deps(_distribution_router(args)))["CONVERT"](
        _state(**CONVERT_STATE)
    )

    assert [cta["channel"] for cta in updates["distribution"]["ctas"]] == ["facebook"]


async def test_convert_drops_an_ask_with_no_text() -> None:
    """An empty ask is a channel whose post carries no call to action, which is the one
    thing the node exists to produce."""
    args = {
        **DISTRIBUTION_ARGS,
        "ctas": [
            {"channel": "linkedin", "text": "   "},
            {"channel": "facebook", "text": "Wasserschaden?"},
        ],
    }

    updates = await build_nodes(_deps(_distribution_router(args)))["CONVERT"](
        _state(**CONVERT_STATE)
    )

    assert [cta["channel"] for cta in updates["distribution"]["ctas"]] == ["facebook"]


async def test_convert_receives_the_banned_claim_verbatim_on_a_retry() -> None:
    """The fix hint is the whole value of the retry: a retry that only says "try again"
    spends a model call to produce the same copy."""

    class Capturing(StubRouter):
        def __init__(self) -> None:
            super().__init__({TaskClass.REPACK: DISTRIBUTION_ARGS})
            self.bodies: list[str] = []

        async def complete(self, task: Any, messages: Any, **kw: Any) -> Completion:
            self.bodies.append(str(messages[-1].content))
            return await super().complete(task, messages, **kw)

    router = Capturing()
    await build_nodes(_deps(router))["CONVERT"](
        _state(
            **CONVERT_STATE,
            claim_check={"passed": False, "fix_hint": "remove 'garantiert schmerzfrei'"},
        )
    )

    assert "remove 'garantiert schmerzfrei'" in router.bodies[0]


async def test_convert_fails_loudly_when_the_model_returns_no_plan() -> None:
    """Silence here would leave the run with posts whose links point nowhere."""
    with pytest.raises(ValueError, match="distribution plan"):
        await build_nodes(_deps(_distribution_router({})))["CONVERT"](_state(**CONVERT_STATE))


# --------------------------------------------------------------------------- #
# VALIDATE now judges two artifacts
# --------------------------------------------------------------------------- #


def _landing_state(**over: Any) -> AgentState:
    return _state(
        dna=BANNED_DNA,
        outline={"target_keyword": "zahnarzt", "headings": []},
        draft={
            "title": "Zahnarzt Koblenz",
            "meta_description": "d" * 150,
            "html": "<h1>Sanft</h1>",
        },
        **over,
    )


async def test_a_banned_claim_in_the_ask_alone_fails_the_claim_check() -> None:
    """The article is clean, so a check over the draft only would pass this run.

    The ask is the sentence somebody acts on — it goes in a post, in a bio, in an email
    — so it is checked with the article as ONE verdict, because "may this run be
    published" has one answer. This is the assertion that stops it.
    """
    updates = await build_nodes(_deps())["VALIDATE"](
        _distribution_state(
            ctas=[{"channel": "linkedin", "text": "Schmerzfrei zum neuen Lächeln, heute"}]
        )
    )

    assert updates["seo_report"]["passed"] in (True, False), "the article's own verdict"
    assert updates["claim_check"]["passed"] is False
    assert updates["claim_check"]["hits"][0]["claim"] == "schmerzfrei"


async def test_a_banned_claim_in_any_channels_ask_blocks_the_whole_run() -> None:
    """Not just that channel. A forbidden claim is a compliance gate on the run, and the
    graph refuses to carry a failing verdict to REVIEW at all."""
    updates = await build_nodes(_deps())["VALIDATE"](
        _distribution_state(
            ctas=[
                {"channel": "linkedin", "text": "Unser Leitfaden für Angstpatienten:"},
                {"channel": "facebook", "text": "Die beste Zahnarztpraxis in Koblenz:"},
            ]
        )
    )

    assert updates["claim_check"]["passed"] is False
    assert updates["claim_check"]["hits"][0]["claim"] == "beste Zahnarztpraxis"


def _distribution_state(**over: Any) -> AgentState:
    """A state whose ARTICLE is clean and whose ask is whatever the test supplies."""
    return _landing_state(
        distribution={
            "destination_url": "https://zahnarzt-koblenz.example/erstberatung",
            **over,
        }
    )


# --------------------------------------------------------------------------- #
# The whole graph, with the real nodes: a banned claim in the ASK
#
# The strongest form of the requirement, and the reason CONVERT sits before
# VALIDATE. The article is clean, so every SEO gate passes; the forbidden promise
# is in the conversion copy, which is the line somebody acts on.
# --------------------------------------------------------------------------- #


def _passing_seo(request: Any) -> SeoScoreResult:
    """A passing SEO verdict, so a claim-gate test measures the claim gate.

    The `seo` engine has its own suite; hand-writing an article that scores 85 here
    would make these tests fail for reasons that have nothing to do with what they
    assert.
    """
    return SeoScoreResult(score=91, findings=[], passed=True)


class _ToolRouter(StubRouter):
    """Answers by the TOOL it is offered rather than by task class.

    CONVERT and REPACK both run on ``TaskClass.REPACK`` -- cheap-tier short-form
    adaptation is the same kind of work, which is what the task classes are named
    after -- so a router keyed on task alone cannot give them different answers.
    """

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.by_tool = answers
        self.offered: list[str] = []
        #: Every message this router was sent, so a test can assert what actually
        #: reached the PROMPT -- which is a different question from what reached the
        #: checkpoint, and the retrieval summariser has to get both right.
        self.seen_messages: list[Any] = []

    async def complete(
        self,
        task: TaskClass,
        messages: Any,
        *,
        tools: Any = None,
        budget: Any = None,
        temperature: Any = None,
        max_tokens: Any = None,
        # The nodes now pass trace context (business_id, node, prompt_version) so that
        # llm spans and `model_usage` rows are attributable. Accepted here to keep this
        # double matching the real signature; the tests that care about the VALUE assert
        # on it explicitly.
        trace: Any = None,
    ) -> Completion:
        name = next(iter(tools)).name if tools else "unknown"
        self.offered.append(name)
        self.seen_messages.extend(messages)
        payload = self.by_tool.get(name)
        if payload is None:
            return Completion(text="prose", tool_calls=[], usage=_usage(), is_final=True)
        return Completion(
            text=None,
            tool_calls=[ToolCall(name=name, arguments=payload, call_id="c1")],
            usage=_usage(),
            is_final=False,
        )


CLEAN_ARTICLE_HTML = (
    "<h1>Angstfreie Zahnbehandlung in Koblenz</h1>"
    "<h2>Ablauf</h2><p>Wir erklären jeden Schritt vorab und arbeiten in Ihrem Tempo. "
    'Mehr dazu auf <a href="/leistungen">unseren Leistungsseiten</a> und bei der '
    '<a href="https://www.kzbv.de">KZBV</a> sowie der '
    '<a href="https://www.bzaek.de">BZÄK</a>.</p>'
    "<h2>Kosten</h2><p>Die Kosten hängen vom Befund ab; wir rechnen sie vorab auf. "
    "Zahnarzt Koblenz bedeutet für uns Aufklärung vor Behandlung.</p>"
)


async def test_a_banned_claim_in_the_ask_cannot_reach_approval() -> None:
    """The article never says it; the ask does. The run still cannot be approved, because
    VALIDATE checks both as one compliance verdict and the graph refuses to carry a
    failing one to REVIEW -- REVIEW being where a human can approve, and EXPORT
    publishing what was approved.

    This is what makes CONVERT's position in the graph load-bearing rather than
    stylistic: after VALIDATE it would produce this exact copy with nothing checking
    it.
    """
    router = _ToolRouter(
        {
            "record_opportunities": {
                "opportunities": [
                    {
                        "title": "Angstpatienten",
                        "rationale": "r",
                        "target_keywords": ["zahnarzt koblenz"],
                        "score": 90,
                    }
                ]
            },
            "record_outline": {"target_keyword": "zahnarzt koblenz", "headings": ["Ablauf"]},
            "record_page": {
                "title": "Angstfreie Zahnbehandlung in Koblenz",
                "meta_description": "d" * 150,
                "html": CLEAN_ARTICLE_HTML,
            },
            "record_distribution": {
                **DISTRIBUTION_ARGS,
                # The forbidden promise, in the line somebody acts on.
                "ctas": [{"channel": "linkedin", "text": "Schmerzfrei behandelt in Koblenz"}],
            },
            "record_posts": {"posts": [{"channel": "linkedin", "body": "Sanfte Behandlung."}]},
        }
    )
    # The SEO scorer is stubbed to a pass so that the ONLY thing that can stop this run
    # is the claim gate. With the real scorer the hand-written article also fails on
    # length and density, and the test would prove the retry loop rather than the gate.
    result = await run_graph(
        _state(dna=BANNED_DNA), nodes=build_nodes(_deps(router, score_page=_passing_seo))
    )

    claims = result.state["claim_check"] or {}
    assert claims["passed"] is False
    assert claims["hits"][0]["claim"] == "schmerzfrei"
    assert result.state["publication_blocked"] is True
    assert result.state["outcome"] == "partial"
    assert "REVIEW" not in result.state["visited"], (
        "a run that cannot produce compliant conversion copy must stop BEFORE the "
        "point where a human can approve it"
    )
    assert "schmerzfrei" in (result.state["finished_reason"] or "")
    assert result.state["visited"].count("CONVERT") == 3, (
        "the two documented retries happen first: the gate is not a hair trigger"
    )


# --------------------------------------------------------------------------- #
# The retrieval trace leaves the process
#
# `_retrieved` used to call `kb.search` and reduce the answer to prompt text on the
# next line, so the rewritten queries, the per-chunk grades and the fallback decision
# were computed and then dropped at the call site. They existed in a log line and
# nowhere a reviewer could reach, which makes "our retrieval is agentic" a claim about
# code nobody can check -- the one kind of claim this project is not allowed to make.
#
# What is asserted here is therefore the ROUND TRIP, not the summariser in isolation:
# real nodes, the real `RetrievalTrace` model, the real `to_checkpoint`, and both graph
# drivers. A test over a hand-written trace dict would keep passing on the day
# `RetrievalAttempt.query` is renamed, and the panel would go blank in production.
# --------------------------------------------------------------------------- #

#: The chunk body. It is a sentinel: the whole point of the summariser is that this
#: string reaches the model's prompt and NEVER the checkpoint, so every assertion
#: about text-dropping is "this exact string is absent".
CHUNK_BODY = (
    "Sanitaernotdienst rund um die Uhr. Anfahrt 89 EUR pauschal, Arbeitszeit "
    "ab 95 EUR je Stunde, Werktage von 08 bis 18 Uhr zum Normaltarif."
)

#: The grader's justification. Deliberately shares no phrase with `CHUNK_BODY`, so a
#: test that finds the body has found the body and not a quotation of it.
GRADE_REASON = "Names a call-out charge, which is what the query asked for."

CHUNK_A = UUID("aaaaaaaa-0000-4000-8000-000000000001")
CHUNK_B = UUID("bbbbbbbb-0000-4000-8000-000000000002")
CHUNK_C = UUID("cccccccc-0000-4000-8000-000000000003")
DOCUMENT = UUID("dddddddd-0000-4000-8000-000000000004")


def _grade(chunk_id: UUID, grade: str, ordinal: int) -> ChunkGrade:
    return ChunkGrade(
        chunk_id=chunk_id,
        document_id=DOCUMENT,
        ordinal=ordinal,
        distance=0.21 + ordinal / 100,
        grade=grade,  # type: ignore[arg-type]
        reason=GRADE_REASON,
        # The field the summariser must drop. `kb_service` puts a 240-character
        # excerpt here for rendering; the checkpoint cannot afford it eleven times.
        excerpt=CHUNK_BODY,
    )


def _real_trace(question: str) -> RetrievalTrace:
    """A two-attempt retrieval that ended in the web fallback.

    The interesting case, and the one the review panel most has to be able to show: the
    business's own documents did NOT answer, so the loop rewrote its query, graded the
    second batch, gave up, and said so. A trace that always succeeded would let a
    summariser that drops `outcome` pass.
    """
    return RetrievalTrace(
        question=question,
        business_id=BUSINESS,
        needed=True,
        need_reason="The page states a price, so it needs the business's own figures.",
        attempts=[
            RetrievalAttempt(
                attempt=1,
                query="notdienst anfahrt kosten",
                query_rationale="Nouns the price list would use, not the node's question.",
                limit=6,
                grades=[_grade(CHUNK_A, "irrelevant", 1)],
                relevant=0,
                partial=0,
                irrelevant=1,
                decision="retry",
                decision_reason="Nothing graded relevant, so the query was widened.",
            ),
            RetrievalAttempt(
                attempt=2,
                query="sanitaer notdienst preisliste koblenz",
                query_rationale="Adds the city and the document's own word for a price list.",
                limit=12,
                grades=[_grade(CHUNK_B, "partial", 2), _grade(CHUNK_C, "irrelevant", 3)],
                relevant=0,
                partial=1,
                irrelevant=1,
                decision="exhausted",
                decision_reason="Two attempts is the ceiling; one partial is not grounding.",
            ),
        ],
        outcome="fallback_to_web",
        outcome_reason=(
            "No passage graded relevant after two attempts, so the run continues on "
            "live research and says so."
        ),
        chunks=[
            GroundingChunk(
                chunk_id=CHUNK_B,
                document_id=DOCUMENT,
                ordinal=2,
                content=CHUNK_BODY,
                distance=0.23,
                grade="partial",
                reason=GRADE_REASON,
            )
        ],
        model_calls=3,
        cost_usd=Decimal("0.0042"),
    )


def _retrieving_router() -> _ToolRouter:
    return _ToolRouter(
        {
            "record_opportunities": {
                "opportunities": [
                    {
                        "title": "Angstpatienten",
                        "rationale": "r",
                        "target_keywords": ["zahnarzt koblenz"],
                        "score": 90,
                    }
                ]
            },
            "record_outline": {"target_keyword": "zahnarzt koblenz", "headings": ["Ablauf"]},
            "record_page": {
                "title": "Angstfreie Zahnbehandlung in Koblenz",
                "meta_description": "d" * 150,
                "html": CLEAN_ARTICLE_HTML,
            },
            "record_distribution": DISTRIBUTION_ARGS,
            "record_posts": {"posts": [{"channel": "linkedin", "body": "Sanfte Behandlung."}]},
        }
    )


@pytest.fixture(params=["builtin", "langgraph"])
def driver(request: pytest.FixtureRequest) -> Any:
    """Both graph runtimes, one test body.

    A fallback that behaves differently is not a fallback, and `retrieval_traces` is a
    list every retrieving node rewrites -- exactly the shape a merge rule can get wrong
    in one driver and right in the other.
    """
    return run_graph if request.param == "builtin" else run_state_graph


async def _run_with_retrieval(driver: Any) -> tuple[AgentState, list[str]]:
    asked: list[str] = []

    async def retrieve(question: str, **kwargs: Any) -> Any:
        asked.append(question)
        return _real_trace(question)

    result = await driver(
        _state(dna=BANNED_DNA),
        nodes=build_nodes(_deps(_retrieving_router(), retrieve=retrieve, score_page=_passing_seo)),
    )
    return result.state, asked


async def test_the_checkpoint_holds_the_queries_the_grades_and_the_fallback_decision(
    driver: Any,
) -> None:
    """The A2a contract, through the real serialiser.

    Everything asserted here was being thrown away one line after it was computed.
    """
    state, asked = await _run_with_retrieval(driver)
    checkpoint = to_checkpoint(state)

    assert asked, "the nodes must actually have called the knowledge base"

    traces = checkpoint["retrieval_traces"]
    by_node = {entry["node"]: entry for entry in traces}
    assert set(by_node) == {"HARVEST", "OPPORTUNITY", "PLAN", "GENERATE", "CONVERT"}, (
        "every node granted kb.search must leave its evidence behind, not only the "
        "one that happened to be wired first"
    )

    # GENERATE is the node where a missing passage becomes an invented sentence, so it
    # is the node whose evidence matters most.
    generate = by_node["GENERATE"]

    # 1. The rewritten queries -- the field the whole "agentic" claim rests on.
    assert [attempt["query"] for attempt in generate["attempts"]] == [
        "notdienst anfahrt kosten",
        "sanitaer notdienst preisliste koblenz",
    ]
    assert generate["attempts"][1]["query"] != generate["question"], (
        "a system that embeds the node's own words is doing vector search; the rewrite "
        "is what makes it retrieval the agent steered"
    )
    assert all(attempt["query_rationale"] for attempt in generate["attempts"])

    # 2. A grade per chunk id.
    graded = {
        grade["chunk_id"]: grade["grade"]
        for attempt in generate["attempts"]
        for grade in attempt["grades"]
    }
    assert graded == {
        str(CHUNK_A): "irrelevant",
        str(CHUNK_B): "partial",
        str(CHUNK_C): "irrelevant",
    }
    assert all(
        grade["reason"] for attempt in generate["attempts"] for grade in attempt["grades"]
    ), "a grade with no reason is unreviewable, which is the opposite of evidence"

    # 3. The fallback decision, and the per-attempt decisions that led to it.
    assert generate["outcome"] == "fallback_to_web"
    assert "live research" in generate["outcome_reason"]
    assert [attempt["decision"] for attempt in generate["attempts"]] == ["retry", "exhausted"]


async def test_the_stored_trace_carries_no_chunk_body_text(driver: Any) -> None:
    """The other half of A2a: bounded, which for a JSONB column means text-free.

    The checkpoint is rewritten on EVERY node, so a chunk body carried here is written
    eleven times a run and then sent to a model. The id, the ordinal and the grade are
    the auditable part; the body is one indexed lookup away in `document_chunks`.
    """
    state, _ = await _run_with_retrieval(driver)
    checkpoint = to_checkpoint(state)

    serialised = json.dumps(checkpoint, default=str)
    assert CHUNK_BODY not in serialised, "chunk bodies must not reach the checkpoint"
    # And not by accident of an empty trace: the evidence IS there, minus the text.
    assert GRADE_REASON in serialised
    assert str(CHUNK_B) in serialised

    for entry in checkpoint["retrieval_traces"]:
        for attempt in entry["attempts"]:
            for grade in attempt["grades"]:
                assert "excerpt" not in grade
                assert "content" not in grade
                assert "text" not in grade


async def test_the_passages_still_reach_the_prompt_the_trace_was_summarised_out_of(
    driver: Any,
) -> None:
    """The control. Dropping the text from the CHECKPOINT must not drop it from the
    PROMPT -- the model still has to see the passage it is grounding a sentence in, and
    a summariser that quietly starved the prompt would look identical in every other
    assertion here."""
    router = _retrieving_router()

    async def retrieve(question: str, **kwargs: Any) -> Any:
        return _real_trace(question)

    await driver(
        _state(dna=BANNED_DNA),
        nodes=build_nodes(_deps(router, retrieve=retrieve, score_page=_passing_seo)),
    )

    prompts = "\n".join(str(message.content) for message in router.seen_messages if message.content)
    assert CHUNK_BODY in prompts


async def test_both_drivers_record_the_same_retrieval_evidence() -> None:
    """`retrieval_traces` is a list every retrieving node REPLACES with the whole
    appended value, which is the shape a merge rule gets wrong in exactly one driver.
    Asserted rather than assumed, because a fallback that records less is not one."""

    async def retrieve(question: str, **kwargs: Any) -> Any:
        return _real_trace(question)

    def _shape(state: AgentState) -> list[tuple[int, str, str, tuple[str, ...]]]:
        return [
            (
                entry["seq"],
                entry["node"],
                entry["outcome"],
                tuple(attempt["query"] for attempt in entry["attempts"]),
            )
            for entry in state["retrieval_traces"]
        ]

    builtin = await run_graph(
        _state(dna=BANNED_DNA),
        nodes=build_nodes(_deps(_retrieving_router(), retrieve=retrieve, score_page=_passing_seo)),
    )
    compiled = await run_state_graph(
        _state(dna=BANNED_DNA),
        nodes=build_nodes(_deps(_retrieving_router(), retrieve=retrieve, score_page=_passing_seo)),
    )

    assert _shape(builtin.state) == _shape(compiled.state)
    assert len(_shape(builtin.state)) == 5


async def test_a_checkpoint_written_before_this_key_existed_still_resumes(
    driver: Any,
) -> None:
    """Nothing migrates a JSONB column, so an older run has no `retrieval_traces` at
    all -- and a run that cannot resume loses work a customer already paid for."""
    state, _ = await _run_with_retrieval(driver)
    old = to_checkpoint(state)
    # Exactly what an older row looks like: the key is simply not there.
    del old["retrieval_traces"]
    assert "retrieval_traces" not in old

    revived = from_checkpoint(old)
    assert revived["retrieval_traces"] == [], "a missing key reads as no evidence, not a crash"

    async def retrieve(question: str, **kwargs: Any) -> Any:
        return _real_trace(question)

    resumed = await driver(
        revived,
        nodes=build_nodes(_deps(_retrieving_router(), retrieve=retrieve, score_page=_passing_seo)),
        resume=True,
    )

    assert resumed.state["outcome"] != "failed"
    assert "retrieval_traces" in to_checkpoint(resumed.state)


def test_a_hand_edited_checkpoint_cannot_smuggle_junk_into_the_graph() -> None:
    """This column can also hold whatever a hand-run UPDATE put there. One malformed
    entry must not travel into the graph as an entry."""
    revived = from_checkpoint(
        {"retrieval_traces": ["not a trace", None, {"node": "PLAN"}, 7], "errors": []}
    )
    assert revived["retrieval_traces"] == [{"node": "PLAN"}]

    assert (
        from_checkpoint({"retrieval_traces": "clearly not a list", "errors": []})[
            "retrieval_traces"
        ]
        == []
    )


def test_the_documented_caps_are_the_enforced_ones() -> None:
    """The literals are HERE on purpose, and the first version of this file did not have
    them -- which is why it passed with the cap raised to 999.

    A cap asserted only against itself is not a cap: the behaviour (trim the oldest,
    keep `seq`) is identical at 12 and at 999, so nothing but the number can catch a
    silent raise. And the number is what the module's own arithmetic rests on: 12 traces
    x 3 attempts x 12 grades x 160 characters is the checkpoint budget, written eleven
    times a run.
    """
    assert MAX_RETRIEVAL_TRACES == 12
    assert RETRIEVAL_REASON_CHARS == 160


def test_a_full_run_of_traces_stays_inside_the_checkpoint_budget() -> None:
    """The caps expressed as the thing they are FOR, so a change to any one of them --
    the count, the attempts, the grades, the reason length, or a body text creeping back
    in -- is caught by arithmetic rather than by remembering to update a literal."""
    state = _state()
    for _ in range(MAX_RETRIEVAL_TRACES * 2):
        state["retrieval_traces"] = append_retrieval_trace(
            state, summarise_retrieval(_real_trace("q" * 200), node="GENERATE")
        )

    stored = len(json.dumps(state["retrieval_traces"]))
    assert stored < 80_000, (
        f"retrieval evidence is {stored} bytes and is rewritten on every node of every "
        "run; the caps in agents.nodes exist to keep this small"
    )


def test_the_count_cap_drops_the_oldest_and_seq_says_that_it_did() -> None:
    """A cap that trims evidence silently is worse than one that trims loudly: the
    reader has to be able to tell "this is the whole retrieval" from "this is the tail
    of it", and `seq` is what says which."""
    state = _state()
    for _ in range(MAX_RETRIEVAL_TRACES + 3):
        state["retrieval_traces"] = append_retrieval_trace(
            state, summarise_retrieval(_real_trace("q"), node="GENERATE")
        )

    stored = state["retrieval_traces"]
    assert len(stored) == MAX_RETRIEVAL_TRACES
    assert [entry["seq"] for entry in stored] == list(range(4, MAX_RETRIEVAL_TRACES + 4))
    assert stored[0]["seq"] != 1, "a trimmed panel must not read as the whole retrieval"


def test_the_summariser_keeps_the_grounding_ids_and_bounds_the_grade_reason() -> None:
    """`grounding_chunk_ids` is the citable half -- relevant only, never partials -- and
    the grader's reason is model prose that nothing on the way out clamps."""
    trace = RetrievalTrace(
        question="q",
        business_id=BUSINESS,
        needed=True,
        need_reason="r",
        attempts=[
            RetrievalAttempt(
                attempt=1,
                query="rewritten",
                query_rationale="why",
                limit=6,
                grades=[
                    ChunkGrade(
                        chunk_id=CHUNK_A,
                        document_id=DOCUMENT,
                        ordinal=1,
                        distance=0.1,
                        grade="relevant",
                        reason="x" * (RETRIEVAL_REASON_CHARS + 400),
                        excerpt=CHUNK_BODY,
                    )
                ],
                relevant=1,
                partial=0,
                irrelevant=0,
                decision="sufficient",
                decision_reason="one relevant passage is enough to ground one claim",
            )
        ],
        outcome="sufficient",
        outcome_reason="answered from the business's own documents",
        chunks=[
            GroundingChunk(
                chunk_id=CHUNK_A,
                document_id=DOCUMENT,
                ordinal=1,
                content=CHUNK_BODY,
                distance=0.1,
                grade="relevant",
                reason="x",
            ),
            GroundingChunk(
                chunk_id=CHUNK_B,
                document_id=DOCUMENT,
                ordinal=2,
                content=CHUNK_BODY,
                distance=0.4,
                grade="partial",
                reason="x",
            ),
        ],
    )

    summary = summarise_retrieval(trace, node="GENERATE")

    assert summary["outcome"] == "sufficient"
    assert summary["grounding_chunk_ids"] == [str(CHUNK_A)], "partials are not citable"
    assert summary["chunk_count"] == 2, "both were carried into the prompt, though"
    reason = summary["attempts"][0]["grades"][0]["reason"]
    assert len(reason) == RETRIEVAL_REASON_CHARS
    assert summary["cost_usd"] == "0", "money is a string on the way to a JSONB column"


def test_the_summariser_survives_a_trace_double_that_is_not_the_real_class() -> None:
    """`retrieve` is injected, so this has to be duck-typed the way `summarise_crawl`
    is -- a summariser that only works against the real class cannot be exercised
    without the real one."""
    summary = summarise_retrieval(_Trace(), node="PLAN")

    assert summary["node"] == "PLAN"
    # `_Trace` has an `outcome` and a `chunks` list and nothing else -- no `attempts`,
    # no `grounding_chunk_ids`, no `cost_usd`. Every one of those has to read as absent
    # rather than raise, or the summariser is untestable without a database.
    assert summary["outcome"] == "sufficient"
    assert summary["attempts"] == []
    assert summary["attempts_total"] == 0
    assert summary["chunk_count"] == 1
    assert summary["grounding_chunk_ids"] == []
    assert summary["question"] == ""
    assert summary["model_calls"] == 0
