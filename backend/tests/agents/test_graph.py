"""The graph: transitions, loops, interrupts and resumption.

Written before the graph. What is tested here is CONTROL, not content — whether a
node produces a good article is an evaluation question (Phase 12), while whether
the machine can loop forever, skip approval, or lose a run to a crash is a
correctness question, and that is this file.

Nodes are injected as plain callables so the whole graph runs with no model, no
database and no network.

**Every test in this file runs against BOTH runtimes.** `graph.run_graph` is the
hand-written driver and `state_graph.run_state_graph` is the LangGraph-compiled one,
and they are signature-compatible so the executor can choose between them. A fallback
that is not equivalent is not a fallback, so equivalence is asserted here rather than
asserted in prose -- and every branch below (the bounded retry, the two cap exits, the
compliance block, the resume, the event stream) is checked twice, once per driver.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest

from backend.app.agents.graph import GraphResult, build_graph, run_graph
from backend.app.agents.state import AgentState, NodeError, RunCaps, new_state
from backend.app.agents.state_graph import AFTER_REVIEW, build_state_graph, run_state_graph

Node = Callable[[AgentState], Awaitable[dict[str, object]]]

#: One run of the machine, whichever runtime is driving it.
Driver = Callable[..., Awaitable[GraphResult]]


@pytest.fixture(params=["builtin", "langgraph"])
def driver(request: pytest.FixtureRequest) -> Driver:
    """Both runtimes, one test body.

    Named in the parameter id so a failure says which driver broke rather than only
    that something did.
    """
    return run_graph if request.param == "builtin" else run_state_graph


def _state(caps: RunCaps | None = None) -> AgentState:
    return new_state(business_id="22222222-2222-2222-2222-222222222222", goal="leads", caps=caps)


def _ok(name: str, **updates: object) -> Node:
    """A node that succeeds and optionally writes to the state."""

    async def node(state: AgentState) -> dict[str, object]:
        return dict(updates)

    node.__name__ = name
    return node


async def test_a_clean_run_visits_every_node_in_order_and_pauses_for_approval(
    driver: Driver,
) -> None:
    result = await driver(_state(), nodes=_nodes(seo_score=91))

    assert result.state["visited"] == [
        "INTAKE",
        "HARVEST",
        "OPPORTUNITY",
        "PLAN",
        "GENERATE",
        # CONVERT sits between GENERATE and VALIDATE deliberately: it writes the
        # landing page, and VALIDATE is where the regulated-claim gate runs. Placed
        # after VALIDATE it would emit unchecked copy on the surface that carries a
        # promise directly above a form.
        "CONVERT",
        "VALIDATE",
        "REPACK",
        "REVIEW",
    ]
    assert result.state["outcome"] == "awaiting_approval"
    assert result.interrupted is True, "the run must stop at REVIEW, not publish itself"


async def test_a_failing_score_loops_back_to_generate_then_proceeds(driver: Driver) -> None:
    """The retry is the point: VALIDATE must be able to send work back."""
    scores = iter([72, 88])

    result = await driver(_state(), nodes=_nodes(seo_score=lambda: next(scores)))

    visited = result.state["visited"]
    assert visited.count("GENERATE") == 2, "the draft should have been regenerated once"
    assert visited.count("VALIDATE") == 2
    assert result.state["validate_loops"] == 1
    assert result.state["outcome"] == "awaiting_approval"


async def test_the_retry_loop_is_bounded_and_returns_a_partial_not_an_infinite_run(
    driver: Driver,
) -> None:
    """A draft that never reaches the bar must stop, with a reason, not spin."""
    result = await driver(_state(), nodes=_nodes(seo_score=40))

    assert result.state["outcome"] == "partial"
    assert result.state["validate_loops"] == 2, "exactly the documented two loops"
    assert "needs human edit" in (result.state["finished_reason"] or "").lower()
    assert result.interrupted is False


async def test_a_step_cap_ends_the_run_with_a_stated_reason(driver: Driver) -> None:
    caps = RunCaps(max_steps=3, max_usd=Decimal("1"), max_validate_loops=2)
    result = await driver(_state(caps), nodes=_nodes(seo_score=91))

    assert result.state["outcome"] == "partial"
    assert "max_steps" in (result.state["finished_reason"] or "")


async def test_a_budget_cap_ends_the_run_before_the_call_that_would_exceed_it(
    driver: Driver,
) -> None:
    caps = RunCaps(max_steps=99, max_usd=Decimal("0.05"), max_validate_loops=2)
    result = await driver(_state(caps), nodes=_nodes(seo_score=91, cost=Decimal("0.02")))

    assert result.state["outcome"] == "partial"
    assert "max_usd" in (result.state["finished_reason"] or "")
    assert result.state["cost_usd"] <= caps.max_usd, "a refused charge must not be booked"


async def test_a_node_that_raises_degrades_the_run_instead_of_killing_it(driver: Driver) -> None:
    """HARVEST losing one source is normal. It must not end the run."""

    async def flaky_harvest(state: AgentState) -> dict[str, object]:
        raise RuntimeError("SERP quota exhausted")

    result = await driver(_state(), nodes=_nodes(seo_score=91, harvest=flaky_harvest))

    assert result.state["outcome"] == "awaiting_approval", "the run continued"
    codes = [e.code for e in result.state["errors"]]
    assert "node_failed" in codes
    assert any(e.node == "HARVEST" for e in result.state["errors"])


async def test_no_opportunity_ends_the_run_early_and_honestly(driver: Driver) -> None:
    result = await driver(_state(), nodes=_nodes(seo_score=91, opportunity=None))

    assert result.state["outcome"] == "done"
    assert "no opportunity" in (result.state["finished_reason"] or "").lower()
    assert "GENERATE" not in result.state["visited"], "nothing should be written with no target"


async def test_a_run_resumes_from_its_checkpoint_rather_than_starting_over(driver: Driver) -> None:
    """Kill the worker mid-run: the work already paid for must not be repeated."""
    import json

    from backend.app.agents.state import from_checkpoint, to_checkpoint

    first = await driver(_state(), nodes=_nodes(seo_score=91, stop_after="PLAN"))
    assert first.state["outcome"] == "running"

    revived = from_checkpoint(json.loads(json.dumps(to_checkpoint(first.state))))
    resumed = await driver(revived, nodes=_nodes(seo_score=91), resume=True)

    assert resumed.state["visited"].count("INTAKE") == 1, "INTAKE must not run twice"
    assert resumed.state["outcome"] == "awaiting_approval"


async def test_every_sse_event_is_emitted_for_the_timeline(driver: Driver) -> None:
    """The UI timeline and the persisted run_events both read from this stream."""
    events: list[tuple[str, str]] = []

    await driver(
        _state(), nodes=_nodes(seo_score=91), on_event=lambda n, s, _: events.append((n, s))
    )

    assert ("INTAKE", "started") in events
    assert ("INTAKE", "done") in events
    assert [n for n, s in events if s == "done"][-1] == "REVIEW"


def test_the_graph_compiles() -> None:
    assert build_graph() is not None


def test_the_langgraph_runtime_is_actually_a_langgraph_state_graph() -> None:
    """`langgraph` was a declared dependency that nothing imported, and
    `ARCHITECTURE.md` §14 described the machine as a "LangGraph state machine" —
    which was a claim about the shape, not about the code. This asserts the code.

    It checks the compiled artifact rather than the import, because an import proves
    only that a module was loaded: what matters is that the nodes, the branches and
    the human gate are the library's graph rather than our `while` loop.
    """
    from langgraph.graph.state import CompiledStateGraph

    compiled = build_state_graph(_nodes(seo_score=91))

    assert isinstance(compiled, CompiledStateGraph)

    nodes = set(compiled.get_graph().nodes)
    for name in build_graph():
        assert name in nodes, f"{name} is missing from the compiled graph"
    assert AFTER_REVIEW in nodes, "the human gate needs a node to pause before"


def test_the_human_gate_is_the_runtimes_interrupt_and_not_a_return_statement() -> None:
    """The pause is armed by LangGraph, and it is armed only while it is unspent: a
    run whose REVIEW has already been seen by a human must not be parked at the gate
    a second time, or no approval could ever get past it."""
    armed = build_state_graph(_nodes(seo_score=91))
    assert list(armed.interrupt_before_nodes) == [AFTER_REVIEW]

    spent = build_state_graph(_nodes(seo_score=91), arm_interrupt=False)
    assert list(spent.interrupt_before_nodes) == []


# --------------------------------------------------------------------------- #


def _nodes(
    *,
    seo_score: int | Callable[[], int],
    cost: Decimal = Decimal("0"),
    harvest: Node | None = None,
    opportunity: object = ...,
    #: Replace the OPPORTUNITY node itself, rather than the value it returns. Mirrors
    #: `harvest`, and exists because "the node crashed" and "the node chose nothing"
    #: are different states that `opportunity=None` cannot express -- which is exactly
    #: the distinction the graph now has to make.
    opportunity_node: Node | None = None,
    stop_after: str | None = None,
    claim_check: Callable[[], dict[str, object]] | None = None,
) -> dict[str, Any]:
    """A full set of injected node callables, all inert.

    `claim_check` defaults to None, i.e. VALIDATE writes no claim verdict -- which is
    what every test written before the gate existed expects, and is also the honest
    representation of a business with no banned claims configured.
    """
    score = seo_score if callable(seo_score) else (lambda: seo_score)
    opp = {"title": "Notdienst Koblenz"} if opportunity is ... else opportunity

    async def generate(state: AgentState) -> dict[str, object]:
        return {"draft": {"body": "text", "attempt": state["validate_loops"]}, "_cost": cost}

    async def convert(state: AgentState) -> dict[str, object]:
        return {
            "distribution": {
                "destination_url": "https://mueller-sanitaer.de/notdienst",
                "ctas": [{"channel": "linkedin", "text": "Book a callout"}],
            },
            "_cost": cost,
        }

    async def validate(state: AgentState) -> dict[str, object]:
        # Call score() ONCE. Calling it twice consumed two values from the
        # iterator, so `passed` was computed from the NEXT score and the retry
        # never fired -- the test would have silently proved nothing. The same
        # applies to claim_check, which is also often an iterator.
        value = score()
        updates: dict[str, object] = {"seo_report": {"score": value, "passed": value >= 85}}
        if claim_check is not None:
            updates["claim_check"] = claim_check()
        return updates

    return {
        "INTAKE": _ok("INTAKE", dna={"name": "Test"}, _cost=cost),
        "HARVEST": harvest or _ok("HARVEST", facts={"pages": 3}),
        "OPPORTUNITY": opportunity_node or _ok("OPPORTUNITY", opportunity=opp, _cost=cost),
        "PLAN": _ok("PLAN", outline={"h2": ["a"]}, _cost=cost),
        "GENERATE": generate,
        "CONVERT": convert,
        "VALIDATE": validate,
        "REPACK": _ok("REPACK", renderings={"linkedin": "post"}, _cost=cost),
        "REVIEW": _ok("REVIEW"),
        # Inert, like every other node here: what EXPORT publishes and what MEASURE
        # can honestly say are node questions (see `test_export.py`). What belongs in
        # THIS file is that neither of them can run before a human has been asked.
        "EXPORT": _ok("EXPORT", published={"refs": [], "note": "stub"}),
        "MEASURE": _ok("MEASURE", measurement={"published_refs": 0}),
        "_stop_after": stop_after,
    }


# --------------------------------------------------------------------------- #
# After the gate: EXPORT and MEASURE
#
# These two are the first nodes that can change the outside world, so what is tested
# here is the ORDERING against the interrupt: a fresh run must not reach them however
# it ends, and the resume that follows an approval must reach each of them exactly
# once. Both runtimes express the gate differently -- a `return` in one, an
# `interrupt_before` on a sentinel node in the other -- which is precisely why this
# runs twice.
# --------------------------------------------------------------------------- #


def _approved(state: AgentState) -> AgentState:
    """The state as a resumed, approved run sees it: round-tripped and signed off."""
    import json

    from backend.app.agents.state import approve, from_checkpoint, to_checkpoint

    revived = from_checkpoint(json.loads(json.dumps(to_checkpoint(state))))
    return approve(revived, "user:owner-1")


async def test_a_fresh_run_stops_at_the_gate_and_never_reaches_export(driver: Driver) -> None:
    """The load-bearing assertion for the whole publishing half of the machine.

    EXPORT is the only node that can reach an actuator, so "a run cannot reach EXPORT
    without passing a human" is the property that makes the review gate real rather
    than advisory. It is asserted on the machine, not on EXPORT's own manners.
    """
    result = await driver(_state(), nodes=_nodes(seo_score=91))

    assert result.interrupted is True
    assert result.state["visited"][-1] == "REVIEW"
    assert "EXPORT" not in result.state["visited"], "a fresh run must not publish itself"
    assert "MEASURE" not in result.state["visited"]
    assert result.state.get("published") is None, "nothing may claim to have published"


async def test_an_approved_resume_runs_export_then_measure_exactly_once(driver: Driver) -> None:
    first = await driver(_state(), nodes=_nodes(seo_score=91))

    resumed = await driver(_approved(first.state), nodes=_nodes(seo_score=91), resume=True)

    visited = resumed.state["visited"]
    assert visited.count("EXPORT") == 1, "publishing twice is the bug idempotency exists for"
    assert visited.count("MEASURE") == 1
    assert visited.index("EXPORT") < visited.index("MEASURE"), "measure what was published"
    assert resumed.state["outcome"] == "done"
    assert resumed.interrupted is False, "the gate is spent; it must not park the run again"


async def test_an_approved_resume_does_not_rewrite_what_the_human_approved(
    driver: Driver,
) -> None:
    """A resume repeats GENERATE while the gate is still AHEAD, and must not once it is
    behind: the draft, the landing page and the renderings after REVIEW are the exact
    artifacts a person was shown, and publishing a fresh draft written after the
    approval would publish copy nobody approved."""
    scores = iter([72, 91])
    first = await driver(_state(), nodes=_nodes(seo_score=lambda: next(scores)))
    assert first.state["visited"].count("GENERATE") == 2, "the retry ran, so this proves something"

    resumed = await driver(_approved(first.state), nodes=_nodes(seo_score=91), resume=True)

    assert resumed.state["visited"].count("GENERATE") == 2, "the approved draft was rewritten"
    assert resumed.state["visited"].count("EXPORT") == 1


async def test_resuming_twice_does_not_publish_twice(driver: Driver) -> None:
    """The actuator's idempotency key would catch a second publish, and the graph must
    not lean on it: a machine that re-enters EXPORT on every resume publishes twice the
    day somebody edits a post between them, because an edit is a different effect."""
    first = await driver(_state(), nodes=_nodes(seo_score=91))
    once = await driver(_approved(first.state), nodes=_nodes(seo_score=91), resume=True)

    twice = await driver(_approved(once.state), nodes=_nodes(seo_score=91), resume=True)

    assert twice.state["visited"].count("EXPORT") == 1
    assert twice.state["visited"].count("MEASURE") == 1


async def test_a_run_that_crashed_before_review_still_stops_at_the_gate_on_resume(
    driver: Driver,
) -> None:
    """Resuming is not approving. A worker that died mid-run resumes with the human gate
    still ahead of it, and has to stop there like any other run."""
    crashed = await driver(_state(), nodes=_nodes(seo_score=91, stop_after="PLAN"))
    assert crashed.state["outcome"] == "running"

    resumed = await driver(_approved(crashed.state), nodes=_nodes(seo_score=91), resume=True)

    assert resumed.interrupted is True
    assert "EXPORT" not in resumed.state["visited"], (
        "an approval recorded on a run that has not been reviewed yet must not let it "
        "publish; the gate is a place in the machine, not only a field on the state"
    )


async def test_a_blocked_run_never_reaches_export_either(driver: Driver) -> None:
    """The compliance gate and the publishing gate meet here: a run whose copy cannot
    be made publishable stops before REVIEW, so there is no approval to resume with and
    no path to EXPORT at all."""
    result = await driver(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: _claim_verdict(False))
    )

    assert result.state["publication_blocked"] is True
    assert "REVIEW" not in result.state["visited"]
    assert "EXPORT" not in result.state["visited"]


def test_review_reaches_export_only_through_the_gate_node() -> None:
    """Structural, in the LangGraph runtime: the interrupt is armed on the edge OUT of
    REVIEW, so a router that answered EXPORT there would route around the pause. The
    drawn graph must not offer that edge either -- a diagram is what the next person
    reads before they trust the gate."""
    compiled = build_state_graph(_nodes(seo_score=91))
    out_of_review = {edge.target for edge in compiled.get_graph().edges if edge.source == "REVIEW"}

    assert AFTER_REVIEW in out_of_review
    assert "EXPORT" not in out_of_review


# --------------------------------------------------------------------------- #
# The regulated-claim gate: a compliance failure is not a quality failure
# --------------------------------------------------------------------------- #


def _claim_verdict(passed: bool, claim: str = "schmerzfrei") -> dict[str, object]:
    if passed:
        return {"passed": True, "exercised": True, "checked": 1, "hits": [], "fix_hint": ""}
    return {
        "passed": False,
        "exercised": True,
        "checked": 1,
        "hits": [{"claim": claim, "matched": claim, "start": 0, "end": 1, "context": "c"}],
        "fix_hint": f'Remove the forbidden claim "{claim}".',
    }


async def test_a_banned_claim_sends_the_draft_back_to_generate_first(driver: Driver) -> None:
    """The gate is not a hair trigger: the model gets the same two chances it gets on a
    low score, with the offending phrase named."""
    verdicts = iter([_claim_verdict(False), _claim_verdict(True)])

    result = await driver(_state(), nodes=_nodes(seo_score=91, claim_check=lambda: next(verdicts)))

    assert result.state["visited"].count("GENERATE") == 2, "the draft was rewritten once"
    assert result.state["outcome"] == "awaiting_approval", "the rewrite fixed it"
    assert result.state.get("publication_blocked") is not True


async def test_a_draft_that_keeps_the_banned_claim_never_reaches_review(driver: Driver) -> None:
    """The load-bearing assertion of the whole guard. REVIEW is where a human can
    approve, and EXPORT publishes what was approved -- so a run that cannot produce
    compliant copy has to stop BEFORE the approval, not at it."""
    result = await driver(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: _claim_verdict(False))
    )

    assert result.state["outcome"] == "partial"
    assert result.state["publication_blocked"] is True
    assert "REVIEW" not in result.state["visited"]
    assert result.interrupted is False, "an interrupt is an invitation to approve"
    assert "schmerzfrei" in (result.state["finished_reason"] or "")
    assert "NOT sent for approval" in (result.state["finished_reason"] or "")


async def test_a_compliance_block_is_distinguishable_from_a_quality_partial(driver: Driver) -> None:
    """Both end as `partial`, and they mean opposite things to whoever reads the run: a
    weak page is publishable after a human edit, a blocked one is not publishable as
    written. `publication_blocked` is what separates them."""
    weak = await driver(_state(), nodes=_nodes(seo_score=40))
    blocked = await driver(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: _claim_verdict(False))
    )

    assert weak.state["outcome"] == blocked.state["outcome"] == "partial"
    assert weak.state.get("publication_blocked") is not True
    assert blocked.state["publication_blocked"] is True
    assert "needs human edit" in (weak.state["finished_reason"] or "").lower()
    assert "publication blocked" in (blocked.state["finished_reason"] or "").lower()


async def test_a_run_with_no_claim_verdict_behaves_exactly_as_before(driver: Driver) -> None:
    """An absent verdict means VALIDATE has not written one, which must not be read as
    a failure -- that would block every run whose node set predates the gate."""
    result = await driver(_state(), nodes=_nodes(seo_score=91, claim_check=None))

    assert result.state["outcome"] == "awaiting_approval"
    assert result.interrupted is True


async def test_a_clean_claim_check_does_not_consume_a_validate_loop(driver: Driver) -> None:
    result = await driver(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: _claim_verdict(True))
    )

    assert result.state["validate_loops"] == 0
    assert result.state["outcome"] == "awaiting_approval"


async def test_a_banned_claim_blocks_even_when_the_seo_score_passes(driver: Driver) -> None:
    """The two verdicts are independent. A perfect score must not carry a forbidden
    claim past the gate."""
    result = await driver(
        _state(), nodes=_nodes(seo_score=100, claim_check=lambda: _claim_verdict(False))
    )

    report = result.state["seo_report"] or {}
    assert report["passed"] is True
    assert result.state["publication_blocked"] is True
    assert result.state["outcome"] == "partial"


async def test_a_banned_claim_blocks_even_on_a_perfect_score(driver: Driver) -> None:
    """The two verdicts are independent, and the claim check is the hard one. A 95 buys
    nothing: the claim gate covers the article AND the per-channel ask as one verdict,
    and a run carrying a forbidden claim must not reach the screen where a human could
    approve it."""
    result = await driver(
        _state(),
        nodes=_nodes(seo_score=95, claim_check=lambda: _claim_verdict(False)),
    )

    assert result.state["publication_blocked"] is True
    assert "REVIEW" not in result.state["visited"]


# --------------------------------------------------------------------------- #
# A failure to look is not a finding that there was nothing to see
# --------------------------------------------------------------------------- #


async def test_a_failed_opportunity_node_is_not_reported_as_nothing_worth_writing(
    driver: Driver,
) -> None:
    """The fabrication this branch exists to prevent.

    An empty `opportunity` has two causes that mean opposite things. Observed in
    production: OPPORTUNITY failed with `AllProvidersFailedError` because the
    credential's data policy refused every mid-tier model, and the run finished `done`
    telling the owner "No opportunity met the bar for this business" — a judgement
    about their business, manufactured from an outage.

    Same rule the project already applies to share of voice, where `no_answer` is
    excluded from the denominator because a model outage must never be recorded as the
    brand being absent.
    """

    async def exploding_opportunity(state: AgentState) -> dict[str, object]:
        raise RuntimeError("All 2 provider(s) failed for task 'prioritise'")

    result = await driver(
        _state(), nodes=_nodes(seo_score=91, opportunity_node=exploding_opportunity)
    )

    reason = result.state["finished_reason"] or ""
    assert result.state["outcome"] == "partial", (
        "nothing was decided, so the run is not `done`; the audit still ran, so not `failed`"
    )
    assert "could not run" in reason
    assert "provider" in reason.lower(), "the reason must name the actual cause"
    assert "met the bar" not in reason, (
        "this is the fabricated judgement -- it must not appear for a node that crashed"
    )


async def test_an_opportunity_node_that_ran_and_chose_nothing_still_says_so(driver: Driver) -> None:
    """The other half. Without this, the fix above would just relabel every empty result.

    A business genuinely can have nothing worth writing about this week, and saying so
    is the honest outcome — inventing a topic to fill the slot is not.
    """
    result = await driver(_state(), nodes=_nodes(seo_score=91, opportunity=None))

    reason = result.state["finished_reason"] or ""
    assert result.state["outcome"] == "done"
    assert "met the bar" in reason
    assert "could not run" not in reason


async def test_an_unrelated_degradation_does_not_turn_a_judgement_into_a_failure(
    driver: Driver,
) -> None:
    """`record_error` is also used for ordinary degradations, so the code must be matched.

    HARVEST losing one fact source is normal and is recorded the same way. If this
    branch keyed on "are there any errors" rather than on THIS node's `node_failed`,
    a run whose crawl lost a page would report its perfectly good judgement as a
    failure to look.
    """

    async def flaky_harvest(state: AgentState) -> dict[str, object]:
        return {
            "facts": {},
            "fact_gaps": ["website crawl"],
            "errors": [
                *state["errors"],
                NodeError(node="HARVEST", code="source_dead", message="one page 500'd"),
            ],
        }

    result = await driver(
        _state(), nodes=_nodes(seo_score=91, opportunity=None, harvest=flaky_harvest)
    )

    assert result.state["outcome"] == "done"
    assert "met the bar" in (result.state["finished_reason"] or "")


async def test_a_weak_draft_retries_at_generate(driver: Driver) -> None:
    """The routing rule that replaced a two-way branch.

    It used to choose: a landing page failing its own conversion audit needed only
    CONVERT re-run, because the article had passed. That artifact went with the page
    (`CLAUDE.md`, 2026-08-21), so there is one destination now — and a condition that can
    only ever take one value is a branch that looks live and is not, which is why the
    tests for the other arm are gone rather than adapted.
    """
    scores = iter([70, 91])

    result = await driver(_state(), nodes=_nodes(seo_score=lambda: next(scores)))

    visited = result.state["visited"]
    assert visited.count("GENERATE") == 2, "the draft is what gets rewritten"
    assert result.state["outcome"] == "awaiting_approval"
