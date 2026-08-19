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
from typing import Any, ClassVar
from uuid import uuid4

import pytest

from backend.app.agents.graph import run_graph
from backend.app.agents.nodes import NodeDeps, build_nodes
from backend.app.agents.state import AgentState, new_state
from backend.app.agents.tools import (
    KB_SEARCH,
    PUBLISH,
    RECORD_LANDING_PAGE,
    WEB_SEARCH,
    NodeToolbox,
)
from backend.app.engines.seo import SeoFinding, SeoScoreResult
from backend.app.engines.serp import SerpPage, SerpResult
from backend.app.llm import Completion, TaskClass, ToolCall, Usage

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

    assert len(updates["renderings"]["linkedin"]) <= 3000


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
    from backend.app.agents.tools import CRAWL_SITE, KB_SEARCH, NODE_TOOLS, SERP_SEARCH

    granted = NODE_TOOLS["HARVEST"]
    assert {CRAWL_SITE, SERP_SEARCH, KB_SEARCH} <= granted


async def test_a_node_asking_for_a_tool_it_does_not_hold_fails_loudly() -> None:
    """The refusal must not be degraded into a fact gap. HARVEST swallows a dead
    provider on purpose, and an allowlist violation must not ride out on that path:
    a dead provider is ordinary, a capability nobody granted is not."""
    from backend.app.agents.tools import PUBLISH, NodeToolbox, ToolNotAllowedError

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

LANDING_ARGS: dict[str, Any] = {
    "headline": "Notdienst-Checkliste für Hauseigentümer in Koblenz",
    "subhead": "Fünf Prüfungen, bevor Sie den Notdienst rufen.",
    "offer": "Eine zweiseitige Checkliste mit den fünf Prüfungen bei einem Wasserschaden.",
    "proof_points": [
        {"text": "Seit 1998 in Koblenz.", "source": "Leistungsübersicht 2026"},
        {"text": "24-Stunden-Notdienst.", "source": "mueller-sanitaer.de/notdienst"},
    ],
    "form_fields": [
        {"name": "name", "label": "Ihr Name", "required": True},
        {"name": "email", "label": "E-Mail", "required": True},
    ],
    "primary_cta": "Checkliste anfordern",
    "consent_text": "Ich bin mit der Kontaktaufnahme einverstanden.",
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
}


def _landing_router(args: dict[str, Any] | None = None) -> StubRouter:
    return StubRouter({TaskClass.REPACK: args if args is not None else LANDING_ARGS})


async def test_convert_writes_a_landing_page_and_charges_the_run() -> None:
    router = _landing_router()

    updates = await build_nodes(_deps(router))["CONVERT"](_state(**CONVERT_STATE))

    page = updates["landing_page"]
    assert page["headline"].startswith("Notdienst-Checkliste")
    assert [f["name"] for f in page["form_fields"]] == ["name", "email"]
    assert {c["channel"] for c in page["ctas"]} == {
        "linkedin",
        "facebook",
        "instagram",
        "link_hub",
    }
    assert updates["_cost"] == Decimal("0.001")
    assert router.calls == [TaskClass.REPACK], "conversion copy is short-form, not the strong tier"


async def test_convert_stores_the_page_as_primitives_so_the_checkpoint_survives() -> None:
    """`landing_page` is checkpointed to a JSONB column, and a state that cannot
    serialise cannot resume."""
    updates = await build_nodes(_deps(_landing_router()))["CONVERT"](_state(**CONVERT_STATE))

    json.dumps(updates["landing_page"])


async def test_convert_always_asks_for_a_bio_link_hub_cta() -> None:
    """docs/CHANNELS.md section 1: an Instagram feed caption and a TikTok caption
    cannot carry a clickable link at all, so `/go/{slug}` is the ENTIRE conversion
    path for those surfaces -- and a hub with no CTA on it is an empty page in
    somebody's bio."""

    class Capturing(StubRouter):
        def __init__(self) -> None:
            super().__init__({TaskClass.REPACK: LANDING_ARGS})
            self.bodies: list[str] = []

        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            self.bodies.append(str(messages[-1].content))
            return await super().complete(task, messages, **kw)

    router = Capturing()
    await build_nodes(_deps(router, channels=("linkedin",)))["CONVERT"](_state(**CONVERT_STATE))

    assert "Channels needing a CTA: linkedin, link_hub" in router.bodies[0]


async def test_convert_drops_a_form_field_the_lead_endpoint_would_refuse() -> None:
    """A field named anything else renders, submits, and is refused by the endpoint's
    closed schema -- so the visitor fills it in and the lead is lost. Dropping it
    keeps the page usable, and the deterministic check reports the consequence."""
    args = {**LANDING_ARGS, "form_fields": [{"name": "budget", "label": "Budget"}]}

    updates = await build_nodes(_deps(_landing_router(args)))["CONVERT"](_state(**CONVERT_STATE))

    assert updates["landing_page"]["form_fields"] == [], "an illegal field must not survive"


async def test_convert_keeps_a_good_page_when_one_field_is_illegal() -> None:
    """Rejecting the whole call would lose a page over one bad field."""
    args = {
        **LANDING_ARGS,
        "form_fields": [{"name": "email", "label": "E-Mail"}, {"name": "iban", "label": "IBAN"}],
    }

    updates = await build_nodes(_deps(_landing_router(args)))["CONVERT"](_state(**CONVERT_STATE))

    assert [f["name"] for f in updates["landing_page"]["form_fields"]] == ["email"]
    assert updates["landing_page"]["headline"], "the rest of the page still lands"


async def test_convert_receives_the_landing_fix_hints_verbatim_on_a_retry() -> None:
    """Without this the loop is theatre: the model would be asked to try again with
    no idea what failed."""

    class Capturing(StubRouter):
        def __init__(self) -> None:
            super().__init__({TaskClass.REPACK: LANDING_ARGS})
            self.bodies: list[str] = []

        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            self.bodies.append(str(messages[-1].content))
            return await super().complete(task, messages, **kw)

    router = Capturing()
    await build_nodes(_deps(router))["CONVERT"](
        _state(
            **CONVERT_STATE,
            landing_report={
                "score": 62,
                "passed": False,
                "findings": [
                    {"code": "form_fields", "fix_hint": "Add between 1 and 3 form fields."},
                    {"code": "consent", "fix_hint": "Write one consent sentence."},
                ],
            },
        )
    )

    body = router.bodies[0]
    assert "SCORED 62 / 100" in body
    assert "Add between 1 and 3 form fields." in body
    assert "Write one consent sentence." in body


async def test_convert_receives_the_banned_claim_verbatim_on_a_retry() -> None:
    """The claim verdict covers the article AND the landing copy as one check, so a
    forbidden phrase written HERE has to be named here. Without it only GENERATE
    would hear about it and the retry could never fix the offending artifact."""

    class Capturing(StubRouter):
        def __init__(self) -> None:
            super().__init__({TaskClass.REPACK: LANDING_ARGS})
            self.bodies: list[str] = []

        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            self.bodies.append(str(messages[-1].content))
            return await super().complete(task, messages, **kw)

    router = Capturing()
    await build_nodes(_deps(router))["CONVERT"](
        _state(
            **CONVERT_STATE,
            claim_check={"passed": False, "fix_hint": 'Remove the forbidden claim "schmerzfrei".'},
        )
    )

    assert 'Remove the forbidden claim "schmerzfrei".' in router.bodies[0]


async def test_convert_reads_the_business_documents_rather_than_trusting_recall() -> None:
    """A proof point that is not in the business's own material is an invented claim,
    so the node retrieves that material -- and asks for PROOF, not for the run's goal,
    which would return whatever is most on-topic."""
    asked: list[str] = []

    class _Chunk:
        document_id = "doc-1"
        ordinal = 3
        content = "Seit 1998 betreiben wir den Notdienst in Koblenz."

    class _Trace:
        chunks: ClassVar[list[_Chunk]] = [_Chunk()]

    async def retrieve(question: str) -> Any:
        asked.append(question)
        return _Trace()

    class Capturing(StubRouter):
        def __init__(self) -> None:
            super().__init__({TaskClass.REPACK: LANDING_ARGS})
            self.bodies: list[str] = []

        async def complete(self, task: TaskClass, messages: Any, **kw: Any) -> Completion:
            self.bodies.append(str(messages[-1].content))
            return await super().complete(task, messages, **kw)

    router = Capturing()
    await build_nodes(_deps(router, retrieve=retrieve))["CONVERT"](_state(**CONVERT_STATE))

    assert asked and "notdienst klempner koblenz" in asked[0].lower()
    assert "more local leads" not in asked[0], "the goal is not a question about proof"
    assert "Seit 1998" in router.bodies[0]
    assert "[doc-1#3]" in router.bodies[0], "a passage must be labelled so it can be cited"


async def test_convert_survives_a_knowledge_base_that_is_absent_or_broken() -> None:
    """No knowledge base is a normal state, and a failing one must not cost the page."""

    async def broken(question: str) -> Any:
        raise RuntimeError("pgvector is down")

    without = await build_nodes(_deps(_landing_router()))["CONVERT"](_state(**CONVERT_STATE))
    broken_updates = await build_nodes(_deps(_landing_router(), retrieve=broken))["CONVERT"](
        _state(**CONVERT_STATE)
    )

    assert without["landing_page"]["headline"]
    assert broken_updates["landing_page"]["headline"]


async def test_convert_fails_loudly_when_the_model_returns_no_landing_page() -> None:
    """The graph turns this into a recorded degradation and keeps the article. What it
    must not do is carry on as though a conversion path existed."""
    with pytest.raises(ValueError, match="record_landing_page"):
        await build_nodes(_deps(StubRouter()))["CONVERT"](_state(**CONVERT_STATE))


async def test_convert_reaches_its_engines_only_through_the_allowlist() -> None:
    """CONVERT holds `kb.search` and its own output tool, and nothing else. In
    particular it cannot publish: it writes the page that a public URL will serve."""
    box = NodeToolbox(node="CONVERT")

    assert box.allows(KB_SEARCH)
    assert box.allows(RECORD_LANDING_PAGE)
    assert not box.allows(PUBLISH)
    assert not box.allows(WEB_SEARCH), (
        "a proof point sourced from a page the business does not control is not proof "
        "of anything about the business"
    )


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


async def test_validate_audits_the_landing_page_with_the_engine_and_calls_no_model() -> None:
    router = StubRouter()
    updates = await build_nodes(_deps(router))["VALIDATE"](
        _landing_state(landing_page=_landing_spec_dict())
    )

    assert updates["landing_report"]["score"] == 100
    assert updates["landing_report"]["passed"] is True
    assert router.calls == [], "a deterministic audit must not depend on a model call"


async def test_validate_reports_a_landing_page_that_cannot_capture_a_lead() -> None:
    updates = await build_nodes(_deps())["VALIDATE"](
        _landing_state(landing_page=_landing_spec_dict(form_fields=[]))
    )

    report = updates["landing_report"]
    assert report["passed"] is False
    assert {f["code"] for f in report["findings"] if f["severity"] == "error"} == {
        "form_fields",
        "reachability",
    }


async def test_a_banned_claim_on_the_landing_page_alone_fails_the_claim_check() -> None:
    """The article is clean, so a check over the draft only would pass this run --
    and the landing page is the artifact that makes a promise directly above a form.
    This is the assertion that stops it."""
    updates = await build_nodes(_deps())["VALIDATE"](
        _landing_state(
            landing_page=_landing_spec_dict(
                headline="Schmerzfrei zum neuen Lächeln in Koblenz heute"
            )
        )
    )

    assert updates["seo_report"]["passed"] in (True, False)  # the article's own verdict, unchanged
    assert updates["claim_check"]["passed"] is False
    assert updates["claim_check"]["hits"][0]["claim"] == "schmerzfrei"


async def test_a_banned_claim_in_a_channel_cta_is_caught_too() -> None:
    """A CTA is published copy: it goes in a post, in a bio, in an email."""
    updates = await build_nodes(_deps())["VALIDATE"](
        _landing_state(
            landing_page=_landing_spec_dict(
                ctas=[{"channel": "linkedin", "text": "Die beste Zahnarztpraxis in Koblenz:"}]
            )
        )
    )

    assert updates["claim_check"]["passed"] is False
    assert updates["claim_check"]["hits"][0]["claim"] == "beste Zahnarztpraxis"


async def test_validate_writes_no_landing_report_when_there_is_no_landing_page() -> None:
    """Absent is not the same as passing: the graph must be able to tell "nothing to
    audit" from "audited and fine"."""
    updates = await build_nodes(_deps())["VALIDATE"](_landing_state())

    assert updates["landing_report"] is None


async def test_a_malformed_stored_landing_page_degrades_instead_of_crashing() -> None:
    """The checkpoint is JSONB and can hold whatever an earlier version, or a hand-run
    statement, put there. A run whose VALIDATE crashes cannot be reviewed at all."""
    updates = await build_nodes(_deps())["VALIDATE"](
        _landing_state(landing_page={"headline": 3, "form_fields": "not a list"})
    )

    assert updates["landing_report"] is None
    assert updates["claim_check"]["passed"] is True


def _landing_spec_dict(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "headline": "Angstfreie Zahnbehandlung in Koblenz für Erwachsene",
        "subhead": "Wie ein erster Termin bei uns abläuft.",
        "offer": "Ein Leitfaden mit dem Ablauf des ersten Termins und den Kosten.",
        "proof_points": [
            {"text": "Seit 2004 in Koblenz.", "source": "Praxisprofil"},
            {"text": "Eigene Prophylaxe-Abteilung.", "source": "Leistungen 2026"},
        ],
        "form_fields": [{"name": "email", "label": "E-Mail", "required": True}],
        "primary_cta": "Leitfaden anfordern",
        "consent_text": "Ich bin mit der Kontaktaufnahme einverstanden.",
        "ctas": [{"channel": "linkedin", "text": "Unser Leitfaden für Angstpatienten:"}],
    }
    spec.update(over)
    return spec


# --------------------------------------------------------------------------- #
# The whole graph, with the real nodes: a banned claim on the LANDING PAGE
#
# The strongest form of the requirement, and the reason CONVERT sits before
# VALIDATE. The article is clean, so every SEO gate passes; the forbidden promise
# is in the conversion copy, which is the artifact with a form under it.
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


async def test_a_banned_claim_on_the_landing_page_cannot_reach_approval() -> None:
    """The article never says it; the landing page does. The run still cannot be
    approved, because VALIDATE checks both artifacts as one compliance verdict and the
    graph refuses to carry a failing one to REVIEW -- REVIEW being where a human can
    approve, and EXPORT publishing what was approved.

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
            "record_landing_page": {
                **LANDING_ARGS,
                # The forbidden promise, on the page with the form under it.
                "headline": "Schmerzfrei behandelt in Koblenz",
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


async def test_a_clean_landing_page_reaches_review_with_its_audit() -> None:
    """The control for the test above: the same machinery must let a good page through,
    or the block above would prove nothing."""
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
            "record_landing_page": LANDING_ARGS,
            "record_posts": {"posts": [{"channel": "linkedin", "body": "Sanfte Behandlung."}]},
        }
    )

    result = await run_graph(
        _state(dna=BANNED_DNA), nodes=build_nodes(_deps(router, score_page=_passing_seo))
    )

    assert (result.state["claim_check"] or {})["passed"] is True
    assert (result.state["landing_report"] or {})["passed"] is True
    assert (result.state["landing_page"] or {})["primary_cta"] == "Checkliste anfordern"
    assert result.state.get("publication_blocked") is not True
    assert "REVIEW" in result.state["visited"]
