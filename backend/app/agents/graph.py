"""The agent graph: a bounded state machine, not an open-ended loop.

Code owns the graph; the model owns the choices inside a node. That is the whole
design, and it is what buys bounded steps, a human interrupt at a defined point,
resumption after a crash, and per-node evaluation -- none of which an
LLM-decides-everything loop gives you (docs/ARCHITECTURE.md section 14).

    INTAKE -> HARVEST -> OPPORTUNITY -> PLAN -> GENERATE -> VALIDATE -> REPACK -> REVIEW
                              |                    ^            |
                        no opportunity             +-- fails ---+  (max 2)
                              v
                        end, honestly

VALIDATE returns two verdicts and they are gated differently. A low SEO score is a
quality problem: after the retries run out the draft is returned as a partial for a
human to finish. A banned claim is a compliance problem: the run ends as a partial
with `publication_blocked` set and NEVER reaches REVIEW, because REVIEW is the point
at which a human can approve, and EXPORT publishes what was approved.

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
                # A regulated claim is a HARD gate, not a score. The draft goes back
                # to GENERATE with the offending phrase named, exactly as a failing
                # score does -- but when the retries run out the two outcomes differ:
                # a weak page is returned for a human to edit and publish, while a
                # page making a forbidden claim must not reach REVIEW at all, because
                # REVIEW is where a human can approve it and EXPORT publishes what
                # was approved.
                blocked = bool(claims) and claims.get("passed") is False
                if blocked or not report.get("passed", False):
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
                                "finished_reason": (
                                    f"The draft scored {report.get('score')} after "
                                    f"{state['validate_loops']} revisions and still needs "
                                    "human edit. Returned with its findings."
                                ),
                            },
                            interrupted=False,
                        )
                    index = ORDER.index("GENERATE")
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
