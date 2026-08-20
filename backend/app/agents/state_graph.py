"""The same machine, compiled as a LangGraph ``StateGraph``.

``langgraph`` has been a declared dependency since Phase 6 and nothing imported it:
``graph.run_graph`` is a hand-written driver, and the architecture document's
"LangGraph state machine" was a claim about the SHAPE (bounded steps, a defined
human interrupt, resumable, per-node evaluation) rather than about the library. This
module closes that gap by compiling the identical machine with the library, so the
claim is about the code.

**The nodes do not change, and that is the reason this is cheap.** A node here has
always been ``async (AgentState) -> dict`` of updates, which is exactly LangGraph's
node signature, and ``AgentState`` is already a ``TypedDict``. So what this module
adds is wiring, not a rewrite:

* ``add_conditional_edges(START, ...)`` is the resume entry -- it routes to the first
  node the checkpoint says has not run, which is how a revived run stops paying twice
  for work it already did;
* the VALIDATE retry is a conditional edge back to GENERATE or CONVERT, which is what
  the module docstring of `graph.py` describes in prose;
* ``interrupt_before=["REVIEW"]`` is the human gate, now enforced by the runtime
  rather than by a ``return`` statement;
* every early exit is an edge to ``END`` taken because a node wrapper already wrote
  the outcome and the reason into the state.

**The decision logic is imported, never re-implemented.** ``verdicts``,
``opportunity_exit``, ``validate_exit`` and ``cap_exit`` live in `graph.py` and both
drivers call them. That is deliberate and recent experience: this repo has just spent
a commit removing two copies of a channel-limit table that disagreed, and two copies
of "is this run publication-blocked" would be a far more expensive disagreement.

**Caps and cost are enforced in the wrapper, not by LangGraph.** ``step`` and
``charge`` raise :class:`CapExceededError`, and a raise out of a node would end the
run without a reason. The wrapper catches it and writes the partial outcome, so the
router sees a finished state and routes to ``END``. The wrapper also strips the
private ``_cost`` key: it is not a state channel and LangGraph would reject it.

**No LangGraph checkpointer over Postgres, on purpose.** Durable state is our own
``runs.checkpoint`` column, written by `RunService.checkpoint` after every node, and
that column is what the review screen and the resume path read. An
``InMemorySaver`` is attached per invocation because ``interrupt_before`` requires a
checkpointer to have somewhere to pause; it lives for one ``ainvoke`` and holds
nothing anyone else reads. Adding a `BaseCheckpointSaver` over our table would give
us a second, competing source of truth for the same state -- which is the bug this
module's own docstring is warning about, one layer down.
"""

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from backend.app.agents.graph import (
    ORDER,
    EventSink,
    GraphResult,
    Node,
    cap_exit,
    emit,
    opportunity_exit,
    validate_exit,
    verdicts,
)
from backend.app.agents.state import (
    AgentState,
    CapExceededError,
    NodeError,
    charge,
    enter_validate_loop,
    record_error,
    step,
)

__all__ = ["build_state_graph", "run_state_graph"]

#: The one thread this runtime ever uses.
#:
#: LangGraph keys its checkpointer by thread, and ours lives for a single
#: ``ainvoke`` -- so a constant is correct and a per-run id would imply a durability
#: this saver does not have. The run's real identity is the ``runs`` row.
_THREAD = "run"

#: The one non-primitive in ``AgentState``, named for the checkpoint serialiser.
#:
#: ``RunCaps`` is a dataclass, and LangGraph's default serde accepts an unregistered
#: type with a warning that says it "will be blocked in a future version". A warning
#: on every node of every run is noise, and a future block would be an outage, so the
#: type is declared rather than tolerated. Everything else in the state is JSON
#: primitives, `Decimal` (which the serialiser handles) or pydantic models.
_ALLOWED_STATE_TYPES = (("backend.app.agents.state", "RunCaps"),)

#: The node the run pauses BEFORE while it waits for a human.
#:
#: Not one of `ORDER`'s nine and deliberately not REVIEW itself. REVIEW has to RUN --
#: the timeline's last event is REVIEW/done and `visited` ends with it, which is the
#: contract the run screen and the resume rule are both written against -- so the
#: interrupt has to sit one edge later, at the point the builtin driver expresses as
#: the `return` after REVIEW. This node is that point: "what happens once a human has
#: approved". It does nothing, and it is never wrapped, so it never counts a step or
#: appears in `visited`.
AFTER_REVIEW = "APPROVED"


#: A LangGraph node callable. Deliberately loose: the library's `add_node` overloads
#: are generic over half a dozen node protocols, and pinning one here buys a type
#: error rather than a guarantee -- the shape that matters (`AgentState` in, updates
#: out) is asserted by the tests that drive the compiled graph.
Wrapped = Callable[[AgentState], Awaitable[dict[str, Any]]]


def _wrap(name: str, node: Node, sink: EventSink | None) -> Any:
    """One node, with the step count, the budget, the event stream and the caps.

    Everything a raise would cost is handled here rather than left to the runtime:

    * a node that crashes becomes a recorded degradation, because HARVEST losing one
      fact source is ordinary and the run should continue with less evidence and say
      so;
    * a cap reached becomes a terminal outcome with a stated reason, because a
      :class:`CapExceededError` escaping into LangGraph would end the run with a
      traceback instead of an explanation;
    * ``_cost`` is charged and then dropped, because it is a private convention
      between nodes and the driver and not a state channel.
    """

    async def run(state: AgentState) -> dict[str, Any]:
        try:
            stepped = step(state, name)
        except CapExceededError as exc:
            return cap_exit(exc)

        # `step` advances the count AND appends to `visited`, and both have to reach
        # the state or the caps never bind: a `step_count` left behind in a local
        # means `max_steps` can never be exceeded, which is a cap that silently is
        # not one.
        merged: dict[str, Any] = {
            "step_count": stepped["step_count"],
            "visited": stepped["visited"],
        }

        emit(sink, name, "started", {})
        try:
            updates: Mapping[str, Any] = await node(stepped)
        except Exception as exc:
            # A node that raises must not end the run: HARVEST losing one fact source
            # is ordinary, and the right answer is to carry on with less evidence and
            # say so. It still falls through to the exit checks below, because
            # "OPPORTUNITY crashed" is exactly the case those checks exist to tell
            # apart from "OPPORTUNITY chose nothing".
            emit(sink, name, "failed", {"error": str(exc)})
            failed = record_error(
                stepped, NodeError(node=name, code="node_failed", message=str(exc))
            )
            merged["errors"] = failed["errors"]
        else:
            merged.update({key: value for key, value in updates.items() if not key.startswith("_")})

            cost = updates.get("_cost")
            if isinstance(cost, Decimal) and cost > 0:
                try:
                    charged = charge(stepped, cost)
                except CapExceededError as exc:
                    # The refused charge is not applied, so the run is not billed for
                    # the call it did not make -- `state.charge`'s own rule, kept here.
                    return {**merged, **cap_exit(exc)}
                merged["cost_usd"] = charged["cost_usd"]

            emit(
                sink,
                name,
                "done",
                {"cost_usd": str(merged.get("cost_usd", stepped["cost_usd"]))},
            )

        here = cast("AgentState", {**stepped, **merged})

        if name == "OPPORTUNITY":
            exit_updates = opportunity_exit(here)
            if exit_updates is not None:
                return {**merged, **exit_updates}

        if name == "VALIDATE":
            blocked, weak_draft, weak_landing = verdicts(here)
            if weak_draft or weak_landing:
                try:
                    looped = enter_validate_loop(here)
                except CapExceededError:
                    return {**merged, **validate_exit(here, blocked=blocked)}
                merged["validate_loops"] = looped["validate_loops"]

        return merged

    return run


def build_state_graph(
    nodes: Mapping[str, Any],
    *,
    on_event: EventSink | None = None,
    resume: bool = False,
    arm_interrupt: bool = True,
) -> Any:
    """Compile the machine. Same nodes, same order, same branches as `graph.ORDER`.

    ``resume`` makes every edge skip what the checkpoint says already ran;
    ``arm_interrupt`` is what decides whether the human gate is still ahead of this
    run. They are separate because a CRASHED run resumes with the gate still to come,
    while an APPROVED one resumes with it behind — and conflating them would either
    park an approved run at the gate forever or let a revived one publish without
    passing it.
    """
    stop_after = nodes.get("_stop_after")
    builder: StateGraph[AgentState, Any, Any, Any] = StateGraph(AgentState)

    for name in ORDER:
        builder.add_node(name, _wrap(name, nodes[name], on_event))

    async def approved(state: AgentState) -> dict[str, Any]:
        """The continuation point after the human gate. Deliberately empty."""
        return {}

    builder.add_node(AFTER_REVIEW, approved)

    def entry(state: AgentState) -> str:
        """The first node that still has to run.

        On a fresh run that is INTAKE. On a revived one it is whatever the checkpoint
        has not recorded as visited -- which is what makes resumption cheaper than
        starting over, and is the same rule `graph.run_graph` applies node by node,
        GENERATE's exemption included: a run revived mid-validation has to be able to
        write again.
        """
        if not resume:
            return ORDER[0]
        return _next_unvisited(state, 0, resume=True)

    builder.add_conditional_edges(START, entry, [*ORDER, AFTER_REVIEW])

    for index, name in enumerate(ORDER):
        builder.add_conditional_edges(
            name,
            _router(name, index, stop_after, resume=resume),
            [*ORDER, AFTER_REVIEW, END],
        )
    builder.add_edge(AFTER_REVIEW, END)

    return builder.compile(
        checkpointer=InMemorySaver(
            serde=JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_STATE_TYPES)
        ),
        # Armed only while the pause is unspent. A run whose REVIEW has already run is
        # a run a human has already been shown, so re-arming would park it at the gate
        # a second time and no approval could ever get past it. This is the same fact
        # the builtin driver reads when it skips a visited node on resume.
        interrupt_before=[AFTER_REVIEW] if arm_interrupt else None,
    )


def _next_unvisited(state: AgentState, index: int, *, resume: bool) -> str:
    """The next node at or after ``index`` that still has work to do.

    Only ever skips on a resumed run: the work was paid for once, and paying again is
    the bug resumption exists to prevent. GENERATE is exempt because a run revived
    mid-validation has to be able to write a new draft -- which is also why a resumed,
    already-approved run re-runs it once, matching the builtin driver exactly rather
    than quietly improving on it in one runtime only.
    """
    for name in ORDER[index:]:
        if not resume or name not in state["visited"] or name == "GENERATE":
            return name
    return AFTER_REVIEW


def _router(
    name: str, index: int, stop_after: str | None, *, resume: bool
) -> Callable[[AgentState], str]:
    """Where to go after ``name``. A pure function of the state, as a router must be.

    It never decides an outcome -- the wrapper has already written one if there is one
    -- so this reads ``outcome`` and gets out of the way. The one branch it does own
    is the retry target, and that is genuinely a routing question: the shortest edge
    that can fix what failed, which is CONVERT when only the landing page is weak
    (the article passed, and GENERATE is the strong-tier node) and GENERATE otherwise.
    """

    def route(state: AgentState) -> str:
        if state["outcome"] != "running":
            return END
        if stop_after is not None and name == stop_after:
            # Test hook standing in for a worker that died mid-run.
            return END
        if name == "VALIDATE":
            _, weak_draft, weak_landing = verdicts(state)
            if weak_draft or weak_landing:
                return "GENERATE" if weak_draft else "CONVERT"
        return _next_unvisited(state, index + 1, resume=resume)

    return route


async def run_state_graph(
    state: AgentState,
    *,
    nodes: Mapping[str, Any],
    on_event: EventSink | None = None,
    resume: bool = False,
) -> GraphResult:
    """Drive the compiled graph to its next stopping point.

    Signature-compatible with :func:`graph.run_graph` on purpose: the executor picks
    one by configuration, and a swap that needed a different call site could not be a
    fallback.
    """
    compiled = build_state_graph(
        nodes,
        on_event=on_event,
        resume=resume,
        arm_interrupt="REVIEW" not in state["visited"],
    )
    config = {"configurable": {"thread_id": _THREAD}}

    final = cast("AgentState", await compiled.ainvoke(dict(state), config=config))

    snapshot = await compiled.aget_state(config)
    if AFTER_REVIEW in (snapshot.next or ()):
        # The interrupt fired: REVIEW ran, and the run is parked before whatever
        # happens after approval. `run_service` records that as `awaiting_approval`.
        return GraphResult(
            state=cast("AgentState", {**final, "outcome": "awaiting_approval"}),
            interrupted=True,
        )

    if final["outcome"] != "running":
        # A node wrapper already wrote the outcome and the reason.
        return GraphResult(state=final, interrupted=False)

    stop_after = nodes.get("_stop_after")
    if stop_after is not None and final["visited"][-1:] == [stop_after]:
        # The `_stop_after` hook fired: a worker that died mid-run, simulated. The run
        # is still `running`, which is exactly the state resumption has to recover
        # from -- and calling it `done` would make the crash indistinguishable from a
        # finished run, which is the one thing this hook exists to reproduce.
        return GraphResult(state=final, interrupted=False)

    return GraphResult(state=cast("AgentState", {**final, "outcome": "done"}), interrupted=False)
