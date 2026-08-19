"""The graph: transitions, loops, interrupts and resumption.

Written before the graph. What is tested here is CONTROL, not content — whether a
node produces a good article is an evaluation question (Phase 12), while whether
the machine can loop forever, skip approval, or lose a run to a crash is a
correctness question, and that is this file.

Nodes are injected as plain callables so the whole graph runs with no model, no
database and no network.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from backend.app.agents.graph import build_graph, run_graph
from backend.app.agents.state import AgentState, RunCaps, new_state

Node = Callable[[AgentState], Awaitable[dict[str, object]]]


def _state(caps: RunCaps | None = None) -> AgentState:
    return new_state(business_id="22222222-2222-2222-2222-222222222222", goal="leads", caps=caps)


def _ok(name: str, **updates: object) -> Node:
    """A node that succeeds and optionally writes to the state."""

    async def node(state: AgentState) -> dict[str, object]:
        return dict(updates)

    node.__name__ = name
    return node


async def test_a_clean_run_visits_every_node_in_order_and_pauses_for_approval() -> None:
    result = await run_graph(_state(), nodes=_nodes(seo_score=91))

    assert result.state["visited"] == [
        "INTAKE",
        "HARVEST",
        "OPPORTUNITY",
        "PLAN",
        "GENERATE",
        "VALIDATE",
        "REPACK",
        "REVIEW",
    ]
    assert result.state["outcome"] == "awaiting_approval"
    assert result.interrupted is True, "the run must stop at REVIEW, not publish itself"


async def test_a_failing_score_loops_back_to_generate_then_proceeds() -> None:
    """The retry is the point: VALIDATE must be able to send work back."""
    scores = iter([72, 88])

    result = await run_graph(_state(), nodes=_nodes(seo_score=lambda: next(scores)))

    visited = result.state["visited"]
    assert visited.count("GENERATE") == 2, "the draft should have been regenerated once"
    assert visited.count("VALIDATE") == 2
    assert result.state["validate_loops"] == 1
    assert result.state["outcome"] == "awaiting_approval"


async def test_the_retry_loop_is_bounded_and_returns_a_partial_not_an_infinite_run() -> None:
    """A draft that never reaches the bar must stop, with a reason, not spin."""
    result = await run_graph(_state(), nodes=_nodes(seo_score=40))

    assert result.state["outcome"] == "partial"
    assert result.state["validate_loops"] == 2, "exactly the documented two loops"
    assert "needs human edit" in (result.state["finished_reason"] or "").lower()
    assert result.interrupted is False


async def test_a_step_cap_ends_the_run_with_a_stated_reason() -> None:
    caps = RunCaps(max_steps=3, max_usd=Decimal("1"), max_validate_loops=2)
    result = await run_graph(_state(caps), nodes=_nodes(seo_score=91))

    assert result.state["outcome"] == "partial"
    assert "max_steps" in (result.state["finished_reason"] or "")


async def test_a_budget_cap_ends_the_run_before_the_call_that_would_exceed_it() -> None:
    caps = RunCaps(max_steps=99, max_usd=Decimal("0.05"), max_validate_loops=2)
    result = await run_graph(_state(caps), nodes=_nodes(seo_score=91, cost=Decimal("0.02")))

    assert result.state["outcome"] == "partial"
    assert "max_usd" in (result.state["finished_reason"] or "")
    assert result.state["cost_usd"] <= caps.max_usd, "a refused charge must not be booked"


async def test_a_node_that_raises_degrades_the_run_instead_of_killing_it() -> None:
    """HARVEST losing one source is normal. It must not end the run."""

    async def flaky_harvest(state: AgentState) -> dict[str, object]:
        raise RuntimeError("SERP quota exhausted")

    result = await run_graph(_state(), nodes=_nodes(seo_score=91, harvest=flaky_harvest))

    assert result.state["outcome"] == "awaiting_approval", "the run continued"
    codes = [e.code for e in result.state["errors"]]
    assert "node_failed" in codes
    assert any(e.node == "HARVEST" for e in result.state["errors"])


async def test_no_opportunity_ends_the_run_early_and_honestly() -> None:
    result = await run_graph(_state(), nodes=_nodes(seo_score=91, opportunity=None))

    assert result.state["outcome"] == "done"
    assert "no opportunity" in (result.state["finished_reason"] or "").lower()
    assert "GENERATE" not in result.state["visited"], "nothing should be written with no target"


async def test_a_run_resumes_from_its_checkpoint_rather_than_starting_over() -> None:
    """Kill the worker mid-run: the work already paid for must not be repeated."""
    import json

    from backend.app.agents.state import from_checkpoint, to_checkpoint

    first = await run_graph(_state(), nodes=_nodes(seo_score=91, stop_after="PLAN"))
    assert first.state["outcome"] == "running"

    revived = from_checkpoint(json.loads(json.dumps(to_checkpoint(first.state))))
    resumed = await run_graph(revived, nodes=_nodes(seo_score=91), resume=True)

    assert resumed.state["visited"].count("INTAKE") == 1, "INTAKE must not run twice"
    assert resumed.state["outcome"] == "awaiting_approval"


async def test_every_sse_event_is_emitted_for_the_timeline() -> None:
    """The UI timeline and the persisted run_events both read from this stream."""
    events: list[tuple[str, str]] = []

    await run_graph(
        _state(), nodes=_nodes(seo_score=91), on_event=lambda n, s, _: events.append((n, s))
    )

    assert ("INTAKE", "started") in events
    assert ("INTAKE", "done") in events
    assert [n for n, s in events if s == "done"][-1] == "REVIEW"


def test_the_graph_compiles() -> None:
    assert build_graph() is not None


# --------------------------------------------------------------------------- #


def _nodes(
    *,
    seo_score: int | Callable[[], int],
    cost: Decimal = Decimal("0"),
    harvest: Node | None = None,
    opportunity: object = ...,
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
        "OPPORTUNITY": _ok("OPPORTUNITY", opportunity=opp, _cost=cost),
        "PLAN": _ok("PLAN", outline={"h2": ["a"]}, _cost=cost),
        "GENERATE": generate,
        "VALIDATE": validate,
        "REPACK": _ok("REPACK", renderings={"linkedin": "post"}, _cost=cost),
        "REVIEW": _ok("REVIEW"),
        "_stop_after": stop_after,
    }


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


async def test_a_banned_claim_sends_the_draft_back_to_generate_first() -> None:
    """The gate is not a hair trigger: the model gets the same two chances it gets on a
    low score, with the offending phrase named."""
    verdicts = iter([_claim_verdict(False), _claim_verdict(True)])

    result = await run_graph(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: next(verdicts))
    )

    assert result.state["visited"].count("GENERATE") == 2, "the draft was rewritten once"
    assert result.state["outcome"] == "awaiting_approval", "the rewrite fixed it"
    assert result.state.get("publication_blocked") is not True


async def test_a_draft_that_keeps_the_banned_claim_never_reaches_review() -> None:
    """The load-bearing assertion of the whole guard. REVIEW is where a human can
    approve, and EXPORT publishes what was approved -- so a run that cannot produce
    compliant copy has to stop BEFORE the approval, not at it."""
    result = await run_graph(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: _claim_verdict(False))
    )

    assert result.state["outcome"] == "partial"
    assert result.state["publication_blocked"] is True
    assert "REVIEW" not in result.state["visited"]
    assert result.interrupted is False, "an interrupt is an invitation to approve"
    assert "schmerzfrei" in (result.state["finished_reason"] or "")
    assert "NOT sent for approval" in (result.state["finished_reason"] or "")


async def test_a_compliance_block_is_distinguishable_from_a_quality_partial() -> None:
    """Both end as `partial`, and they mean opposite things to whoever reads the run: a
    weak page is publishable after a human edit, a blocked one is not publishable as
    written. `publication_blocked` is what separates them."""
    weak = await run_graph(_state(), nodes=_nodes(seo_score=40))
    blocked = await run_graph(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: _claim_verdict(False))
    )

    assert weak.state["outcome"] == blocked.state["outcome"] == "partial"
    assert weak.state.get("publication_blocked") is not True
    assert blocked.state["publication_blocked"] is True
    assert "needs human edit" in (weak.state["finished_reason"] or "").lower()
    assert "publication blocked" in (blocked.state["finished_reason"] or "").lower()


async def test_a_run_with_no_claim_verdict_behaves_exactly_as_before() -> None:
    """An absent verdict means VALIDATE has not written one, which must not be read as
    a failure -- that would block every run whose node set predates the gate."""
    result = await run_graph(_state(), nodes=_nodes(seo_score=91, claim_check=None))

    assert result.state["outcome"] == "awaiting_approval"
    assert result.interrupted is True


async def test_a_clean_claim_check_does_not_consume_a_validate_loop() -> None:
    result = await run_graph(
        _state(), nodes=_nodes(seo_score=91, claim_check=lambda: _claim_verdict(True))
    )

    assert result.state["validate_loops"] == 0
    assert result.state["outcome"] == "awaiting_approval"


async def test_a_banned_claim_blocks_even_when_the_seo_score_passes() -> None:
    """The two verdicts are independent. A perfect score must not carry a forbidden
    claim past the gate."""
    result = await run_graph(
        _state(), nodes=_nodes(seo_score=100, claim_check=lambda: _claim_verdict(False))
    )

    report = result.state["seo_report"] or {}
    assert report["passed"] is True
    assert result.state["publication_blocked"] is True
    assert result.state["outcome"] == "partial"
