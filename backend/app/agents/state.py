"""AgentState: what flows through the graph, and the rules that bound a run.

Two things live here and nothing else: the shape of the state, and the three
controls that stop a run from running forever, spending without limit, or losing
work on a crash.

Design notes worth keeping:

* **Money is ``Decimal``.** A run's cost is compared against a cap, and float
  arithmetic makes that comparison lie by fractions of a cent that accumulate.
* **Caps are checked BEFORE the thing they guard**, not after. ``charge()`` raises
  rather than applying a charge that would exceed the budget -- checking afterwards
  is accounting, not control.
* **Errors accumulate, they do not raise.** A fact source that fails should degrade
  a run, not end it, and the UI needs the list to say what is missing.
* **The state must survive a JSON round trip**, because the checkpoint is a JSONB
  column. A state that cannot serialise cannot resume, and a run that cannot resume
  loses work the customer already paid for.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, NotRequired, TypedDict, cast
from uuid import UUID

from pydantic import BaseModel

# From docs/AGENT_RUNTIME.md section 8. Named constants rather than literals at the
# call sites, so the documented numbers and the enforced ones cannot drift.
DEFAULT_MAX_STEPS = 14
DEFAULT_MAX_USD = Decimal("0.50")
DEFAULT_MAX_VALIDATE_LOOPS = 2

NodeName = Literal[
    "INTAKE",
    "HARVEST",
    "OPPORTUNITY",
    "PLAN",
    "GENERATE",
    "CONVERT",
    "VALIDATE",
    "REPACK",
    "REVIEW",
    "EXPORT",
    "MEASURE",
]

Outcome = Literal["running", "awaiting_approval", "done", "partial", "failed"]


class NodeError(BaseModel):
    """A degradation, not a crash.

    ``code`` is machine-readable so the UI can decide what to show; ``message`` is
    for a human. Both are needed: a code alone cannot be rendered, and a message
    alone cannot be branched on.
    """

    node: str
    code: str
    message: str


@dataclass(frozen=True)
class RunCaps:
    """Hard ceilings for one run."""

    max_steps: int = DEFAULT_MAX_STEPS
    max_usd: Decimal = DEFAULT_MAX_USD
    max_validate_loops: int = DEFAULT_MAX_VALIDATE_LOOPS


class CapExceededError(Exception):
    """A run hit one of its ceilings.

    Carries which cap and what the limit was, because "the run stopped" is not an
    answer anyone can act on. The caller converts this into a partial result with a
    stated reason -- it is never an unhandled 500.
    """

    def __init__(self, cap: str, limit: object, attempted: object) -> None:
        self.cap = cap
        self.limit = limit
        self.attempted = attempted
        super().__init__(
            f"run cap {cap} exceeded: limit {limit}, attempted {attempted}. "
            "The run stops here and returns what it has."
        )


class AgentState(TypedDict):
    """The single object the graph threads through every node."""

    business_id: str
    goal: str
    surfaces: list[str]

    # Set at INTAKE, carried everywhere after.
    dna: dict[str, Any]
    remembered: list[str]

    # Engine output: facts, and what could NOT be gathered. `fact_gaps` is why the
    # UI can honestly say "generated without live research" instead of pretending.
    facts: dict[str, Any]
    fact_gaps: list[str]

    # Agent output.
    opportunity: NotRequired[dict[str, Any] | None]
    outline: NotRequired[dict[str, Any] | None]
    draft: NotRequired[dict[str, Any] | None]
    #: The landing page CONVERT wrote: headline, offer, sourced proof points, the
    #: form spec, the primary CTA and one CTA per channel. A `LandingPageSpec`
    #: dumped to primitives, because this is checkpointed to a JSONB column and a
    #: state that cannot serialise cannot resume.
    landing_page: NotRequired[dict[str, Any] | None]
    renderings: dict[str, str]

    # Deterministic verdicts.
    seo_report: NotRequired[dict[str, Any] | None]
    #: The regulated-claim verdict from the `claims` engine, written by VALIDATE.
    #: `None` means VALIDATE has not run yet, which is NOT the same as "clean" --
    #: the graph treats an absent verdict as "nothing checked" and a present
    #: failing one as a publication block.
    claim_check: NotRequired[dict[str, Any] | None]
    #: The deterministic conversion verdict on `landing_page`, written by VALIDATE.
    #: `None` means there was nothing to audit, which is NOT the same as a pass --
    #: the graph gates on a present, failing verdict and leaves an absent one alone.
    landing_report: NotRequired[dict[str, Any] | None]
    #: True when the run ended because content could not be made publishable, as
    #: opposed to ending because the budget or the step count ran out. Kept
    #: separate from `outcome` on purpose: "partial" is the persisted run state
    #: vocabulary (see run_service.RunState), and adding a value to it would be a
    #: schema change, while the REASON a run is partial belongs on the state.
    publication_blocked: NotRequired[bool]

    # Control.
    caps: RunCaps
    step_count: int
    cost_usd: Decimal
    validate_loops: int
    errors: list[NodeError]
    visited: list[str]
    outcome: Outcome
    finished_reason: NotRequired[str | None]


def new_state(
    *,
    business_id: str | UUID,
    goal: str,
    surfaces: list[str] | None = None,
    dna: dict[str, Any] | None = None,
    remembered: list[str] | None = None,
    caps: RunCaps | None = None,
) -> AgentState:
    """A fresh run."""
    return AgentState(
        business_id=str(business_id),
        goal=goal,
        surfaces=surfaces if surfaces is not None else ["google"],
        dna=dna or {},
        remembered=remembered or [],
        facts={},
        fact_gaps=[],
        opportunity=None,
        outline=None,
        draft=None,
        landing_page=None,
        renderings={},
        seo_report=None,
        claim_check=None,
        landing_report=None,
        publication_blocked=False,
        caps=caps or RunCaps(),
        step_count=0,
        cost_usd=Decimal("0"),
        validate_loops=0,
        errors=[],
        visited=[],
        outcome="running",
        finished_reason=None,
    )


def step(state: AgentState, node: str) -> AgentState:
    """Count one node execution, refusing to start one past the ceiling."""
    caps = state["caps"]
    attempted = state["step_count"] + 1
    if attempted > caps.max_steps:
        raise CapExceededError("max_steps", caps.max_steps, attempted)

    return {**state, "step_count": attempted, "visited": [*state["visited"], node]}


def charge(state: AgentState, usd: Decimal) -> AgentState:
    """Book a cost, refusing the charge that WOULD exceed the budget.

    The refused charge is not applied: a run that stops must not also be billed for
    the call it never made.
    """
    caps = state["caps"]
    attempted = state["cost_usd"] + usd
    if attempted > caps.max_usd:
        raise CapExceededError("max_usd", caps.max_usd, attempted)

    return {**state, "cost_usd": attempted}


def record_error(state: AgentState, error: NodeError) -> AgentState:
    """Append a degradation. Never raises -- that is the whole point."""
    return {**state, "errors": [*state["errors"], error]}


def enter_validate_loop(state: AgentState) -> AgentState:
    """Count a VALIDATE -> GENERATE retry, refusing a third."""
    caps = state["caps"]
    attempted = state["validate_loops"] + 1
    if attempted > caps.max_validate_loops:
        raise CapExceededError("max_validate_loops", caps.max_validate_loops, attempted)

    return {**state, "validate_loops": attempted}


def to_checkpoint(state: AgentState) -> dict[str, Any]:
    """JSON-safe form for the ``runs.checkpoint`` column.

    ``Decimal`` becomes a STRING, not a float: round-tripping money through a float
    is how a budget quietly stops matching the ledger.
    """
    payload: dict[str, Any] = dict(state)
    caps = state["caps"]
    payload["caps"] = {
        "max_steps": caps.max_steps,
        "max_usd": str(caps.max_usd),
        "max_validate_loops": caps.max_validate_loops,
    }
    payload["cost_usd"] = str(state["cost_usd"])
    payload["errors"] = [e.model_dump() for e in state["errors"]]
    return payload


def from_checkpoint(payload: dict[str, Any]) -> AgentState:
    """Rebuild a state from its checkpoint. The inverse of :func:`to_checkpoint`."""
    caps_raw = payload.get("caps") or {}
    restored: dict[str, Any] = dict(payload)
    restored["caps"] = RunCaps(
        max_steps=int(caps_raw.get("max_steps", DEFAULT_MAX_STEPS)),
        max_usd=Decimal(str(caps_raw.get("max_usd", DEFAULT_MAX_USD))),
        max_validate_loops=int(caps_raw.get("max_validate_loops", DEFAULT_MAX_VALIDATE_LOOPS)),
    )
    restored["cost_usd"] = Decimal(str(payload.get("cost_usd", "0")))
    restored["errors"] = [NodeError.model_validate(e) for e in payload.get("errors", [])]
    # The payload came from JSON, so the static type is a promise about what
    # to_checkpoint wrote, not a fact the checker can verify. One cast, named.
    return cast("AgentState", restored)
