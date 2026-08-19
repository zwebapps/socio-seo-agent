"""The agent graph: a bounded state machine, not an open-ended loop.

Code owns the graph; the model owns the choices inside a node. That is the whole
design, and it is what buys bounded steps, a human interrupt at a defined point,
resumption after a crash, and per-node evaluation -- none of which an
LLM-decides-everything loop gives you (docs/ARCHITECTURE.md section 14).

    INTAKE -> HARVEST -> OPPORTUNITY -> PLAN -> GENERATE -> CONVERT -> VALIDATE -> REPACK -> REVIEW
                              |                    ^           ^           |
                        no opportunity             |           +- fails ---+  (max 2)
                              v                   +-- draft failed --------+
                        end, honestly

VALIDATE returns three verdicts and they are gated differently.

* A low SEO score is a **quality** problem: after the retries run out the draft is
  returned as a partial for a human to finish.
* A failing landing-page audit is also a quality problem, but it is attributable to
  a different node, so the backward edge is shorter -- see below.
* A banned claim is a **compliance** problem: the run ends as a partial with
  `publication_blocked` set and NEVER reaches REVIEW, because REVIEW is the point at
  which a human can approve, and EXPORT publishes what was approved.

**The retry goes back to the earliest node whose output actually failed.** A failing
score or claim check means the draft has to change, so the edge is to GENERATE (and
CONVERT re-runs after it, because it sits downstream). A failing landing-page audit
alone means the article is fine and only the conversion surface is wrong, so the edge
is to CONVERT: sending it to GENERATE would pay for a strong-tier rewrite of a page
that passed. The claim check deliberately covers the draft AND the landing page as
one verdict, and a compliance failure therefore always takes the longer edge -- that
is conservative rather than precise, and it is the right direction to be imprecise
in.

Nodes are injected rather than imported. A node is just
``async (AgentState) -> dict`` of state updates, so the entire machine is testable
with no model, no database and no network -- and so a node can be swapped or stubbed
without touching the machine.

Two conventions in the update dict:
* ``_cost``   a Decimal charged against the run budget before the next node runs
* everything else is merged into the state
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from backend.app.agents.state import (
    AgentState,
    CapExceededError,
    NodeError,
    charge,
    enter_validate_loop,
    record_error,
    step,
)

logger = logging.getLogger(__name__)

Node = Callable[[AgentState], Awaitable[Mapping[str, Any]]]
EventSink = Callable[[str, str, Mapping[str, Any]], None]

#: The linear spine. Branching (the VALIDATE retry, the early exits) is expressed
#: in `run_graph`, because a table cannot express "go back two nodes, but only
#: twice".
ORDER: tuple[str, ...] = (
    "INTAKE",
    "HARVEST",
    "OPPORTUNITY",
    "PLAN",
    "GENERATE",
    "CONVERT",
    "VALIDATE",
    "REPACK",
    "REVIEW",
)

#: Passing score for the deterministic SEO gate. Matches the seo engine.
PASSING_SCORE = 85


@dataclass
class GraphResult:
    """The outcome of one run."""

    state: AgentState
    interrupted: bool


def build_graph() -> tuple[str, ...]:
    """The node order, exposed so a diagram or a test can assert on it."""
    return ORDER


def _emit(sink: EventSink | None, node: str, status: str, payload: Mapping[str, Any]) -> None:
    if sink is not None:
        sink(node, status, payload)


async def _run_node(
    name: str,
    node: Node,
    state: AgentState,
    sink: EventSink | None,
) -> AgentState:
    """Execute one node, converting a crash into a degradation.

    A node that raises must not end the run. HARVEST losing one fact source is
    ordinary, and the correct response is to carry on with less evidence and say
    so -- which is why the error is appended to the state rather than propagated.
    """
    _emit(sink, name, "started", {})
    try:
        updates = await node(state)
    except Exception as exc:
        logger.warning("node %s failed: %s", name, exc, exc_info=True)
        state = record_error(state, NodeError(node=name, code="node_failed", message=str(exc)))
        _emit(sink, name, "failed", {"error": str(exc)})
        return state

    cost = updates.get("_cost")
    if isinstance(cost, Decimal) and cost > 0:
        state = charge(state, cost)  # raises CapExceededError, caught by the driver

    merged = {k: v for k, v in updates.items() if not k.startswith("_")}
    state = {**state, **merged}  # type: ignore[typeddict-item]
    _emit(sink, name, "done", {"cost_usd": str(state["cost_usd"])})
    return state


def _node_failure(state: AgentState, node: str) -> str | None:
    """The message from this node's own crash, if it crashed.

    `_run_node` converts a raise into a `NodeError(code="node_failed")` so one dead
    fact source cannot end a run. That is right for HARVEST, where carrying on with
    less evidence is the correct answer -- but it means a caller cannot tell "produced
    nothing" from "never ran" by looking at the output alone. This reads the record.

    Matched on `code`, not on the message: `record_error` is also used for ordinary
    degradations, and treating "one page 500'd" as "the node failed" would send a run
    down the failure path for something it handled correctly.
    """
    for error in reversed(state.get("errors") or []):
        if error.node == node and error.code == "node_failed":
            return error.message
    return None


def _quality_reason(
    *,
    report: Mapping[str, Any],
    landing: Mapping[str, Any],
    loops: int,
    weak_draft: bool,
    weak_landing: bool,
) -> str:
    """Why a run ran out of retries, naming each artifact that is still short.

    Two artifacts can fail independently now, so one sentence about "the draft"
    would be wrong half the time -- and this string is what the owner reads to
    decide what to edit.
    """
    parts: list[str] = []
    if weak_draft:
        parts.append(f"the draft scored {report.get('score')} of 100")
    if weak_landing:
        parts.append(f"the landing page scored {landing.get('score')} of 100")
    subject = "they are" if len(parts) > 1 else "it is"
    return (
        f"After {loops} revisions "
        + " and ".join(parts)
        + f", and still needs human edit. Returned with its findings, so {subject} "
        "editable rather than lost. Nothing was blocked from publication."
    )


async def run_graph(
    state: AgentState,
    *,
    nodes: Mapping[str, Any],
    on_event: EventSink | None = None,
    resume: bool = False,
) -> GraphResult:
    """Drive the machine to its next stopping point.

    Stops at exactly one of: REVIEW (awaiting a human), an early exit (nothing worth
    writing), a cap (partial, with a stated reason), or the end. It never stops
    silently and never fails to stop.
    """
    stop_after = nodes.get("_stop_after")
    index = 0

    try:
        while index < len(ORDER):
            name = ORDER[index]

            # On resume, skip what the checkpoint says already ran. The work was
            # paid for once; paying again is the bug resumption exists to prevent.
            if resume and name in state["visited"] and name != "GENERATE":
                index += 1
                continue

            state = step(state, name)
            state = await _run_node(name, nodes[name], state, on_event)

            if name == "OPPORTUNITY" and not state.get("opportunity"):
                # An empty `opportunity` has TWO causes and they mean opposite things:
                # the node ran and judged that nothing was worth writing about, or the
                # node could not run at all. Reporting the second as the first tells a
                # customer their business has no story in it, on the strength of a
                # provider outage.
                #
                # Observed in production, which is why this branch exists: OPPORTUNITY
                # failed with `AllProvidersFailedError` (the credential's data policy
                # refused every mid-tier model) and the run finished `done` saying "No
                # opportunity met the bar for this business."
                #
                # This is the same rule the project already applies to share of voice,
                # where `no_answer` is excluded from the denominator because "a model
                # outage must never be recorded as the brand being absent -- that is the
                # difference between a measurement and a fabrication". A judgement the
                # agent never made is exactly that kind of fabrication.
                failure = _node_failure(state, name)
                if failure is not None:
                    return GraphResult(
                        state={
                            **state,
                            # `partial`, not `failed`: HARVEST's audit findings are real
                            # work and are returned. Not `done` either -- nothing was
                            # decided, so the run did not do what it set out to do.
                            "outcome": "partial",
                            "finished_reason": (
                                "Opportunity selection could not run, so no topic was "
                                f"chosen: {failure}. This is a failure to look, NOT a "
                                "finding that nothing was worth writing about. The audit "
                                "findings gathered before it are returned."
                            ),
                        },
                        interrupted=False,
                    )

                # Nothing worth writing about. Returning the audit is the honest
                # outcome; inventing a topic to fill the slot is not.
                return GraphResult(
                    state={
                        **state,
                        "outcome": "done",
                        "finished_reason": (
                            "No opportunity met the bar for this business. The audit "
                            "findings are returned instead."
                        ),
                    },
                    interrupted=False,
                )

            if name == "VALIDATE":
                report = state.get("seo_report") or {}
                claims = state.get("claim_check") or {}
                landing = state.get("landing_report") or {}
                # A regulated claim is a HARD gate, not a score. The draft goes back
                # to GENERATE with the offending phrase named, exactly as a failing
                # score does -- but when the retries run out the two outcomes differ:
                # a weak page is returned for a human to edit and publish, while a
                # page making a forbidden claim must not reach REVIEW at all, because
                # REVIEW is where a human can approve it and EXPORT publishes what
                # was approved.
                blocked = bool(claims) and claims.get("passed") is False
                # An ABSENT landing report is not a failure: a run whose CONVERT node
                # produced nothing has already recorded that as an error, and there
                # is no verdict to gate on. A present, failing one is a quality
                # failure attributable to CONVERT alone.
                weak_landing = bool(landing) and landing.get("passed") is False
                weak_draft = blocked or not report.get("passed", False)
                if weak_draft or weak_landing:
                    try:
                        state = enter_validate_loop(state)
                    except CapExceededError:
                        if blocked:
                            found = ", ".join(
                                sorted({str(hit.get("claim")) for hit in claims.get("hits", [])})
                            )
                            return GraphResult(
                                state={
                                    **state,
                                    "outcome": "partial",
                                    "publication_blocked": True,
                                    "finished_reason": (
                                        "Publication blocked: after "
                                        f"{state['validate_loops']} revisions the draft "
                                        f"still makes the forbidden claim(s) {found}. "
                                        "The draft is returned for a human to rewrite "
                                        "and was NOT sent for approval."
                                    ),
                                },
                                interrupted=False,
                            )
                        return GraphResult(
                            state={
                                **state,
                                "outcome": "partial",
                                "finished_reason": _quality_reason(
                                    report=report,
                                    landing=landing,
                                    loops=state["validate_loops"],
                                    weak_draft=weak_draft,
                                    weak_landing=weak_landing,
                                ),
                            },
                            interrupted=False,
                        )
                    # The shortest edge that can fix what failed. See the module
                    # docstring: a landing page that failed on its own does not need
                    # the article rewritten, and GENERATE is the strong-tier node.
                    index = ORDER.index("GENERATE" if weak_draft else "CONVERT")
                    continue

            if name == "REVIEW":
                return GraphResult(
                    state={**state, "outcome": "awaiting_approval"}, interrupted=True
                )

            if stop_after is not None and name == stop_after:
                # Test hook standing in for a worker that died mid-run.
                return GraphResult(state={**state, "outcome": "running"}, interrupted=False)

            index += 1

    except CapExceededError as exc:
        return GraphResult(
            state={
                **state,
                "outcome": "partial",
                "finished_reason": (
                    f"Run stopped: {exc.cap} reached (limit {exc.limit}). "
                    "Returning what was produced so far."
                ),
            },
            interrupted=False,
        )

    return GraphResult(state={**state, "outcome": "done"}, interrupted=False)
