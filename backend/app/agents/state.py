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
* **Anything carried here is written once per node, so it is bounded once per node.**
  The checkpoint is rewritten eleven times a run, so a field that grows with the
  evidence -- the crawl, the retrieval trace -- is compacted before it gets in. The
  compaction lives next to whatever produces the thing (`summarise_crawl` in
  `run_executor`, `summarise_retrieval` in `agents.nodes`), because that is the only
  place that knows which losses are the cheap ones.
* **A reader of this state does not get to assume its own version wrote it.** Keys are
  added over time and nothing migrates a JSONB column, so every key added after the
  first checkpoint is `NotRequired` and :func:`from_checkpoint` supplies the default.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, NotRequired, TypedDict, cast
from uuid import UUID

from pydantic import BaseModel

from backend.app.engines.channel.specs import canonical_channel, has_spec

#: The channels a run renders posts for when nobody chose.
#:
#: It lives HERE rather than in `agents.nodes` where it used to, because the channel
#: set is now per-run state and `state.py` cannot import from `nodes` -- `nodes`
#: imports this module. `nodes` re-exports the name, so importers of
#: `agents.nodes.DEFAULT_CHANNELS` are unaffected.
DEFAULT_CHANNELS: tuple[str, ...] = ("linkedin", "facebook", "instagram")

# From docs/AGENT_RUNTIME.md section 8. Named constants rather than literals at the
# call sites, so the documented numbers and the enforced ones cannot drift.
DEFAULT_MAX_STEPS = 14
DEFAULT_MAX_USD = Decimal("0.50")
DEFAULT_MAX_VALIDATE_LOOPS = 2

#: The approver recorded when a run is approved through a surface that carries no user
#: identity.
#:
#: `Actuation.approved_by` accepts either a user id or ``"policy:<name>"``, and this is
#: the second case: as of today NOTHING in this system records WHO approved a run.
#: `RunService.await_approval` writes a run STATE (`awaiting_approval`) and no actor,
#: there is no approve route, and `POST /runs/{id}/resume` refuses a run in that state
#: outright -- so there is no user id to thread, and inventing one would put a name on
#: an authorisation nobody gave. A policy string says exactly what happened: a human
#: gate was passed and the surface that passed it did not identify the human. Whoever
#: builds that route should pass the real id to :func:`approve` instead.
POLICY_APPROVER = "policy:human-review-gate"

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
    #: The `runs` row this state belongs to, as a STRING -- the same reason
    #: `business_id` is one: this is checkpointed to a JSONB column and a `UUID` does
    #: not survive a JSON round trip.
    #:
    #: It is here so a side effect can be attributed to the run that caused it.
    #: `nodes._actuate` puts it on every `Actuation`, which is what fills
    #: `actions.run_id` and `content_pieces.run_id` -- without it a published page and
    #: the ledger row that authorised it are both orphans, and "what did this run
    #: publish, and how many leads did it earn" is not a question the database can
    #: answer.
    #:
    #: `NotRequired` because it postdates checkpoints already written, and a run that
    #: cannot resume because an ATTRIBUTION field is missing would lose work a customer
    #: already paid for. `from_checkpoint` supplies `None`; the executor then stamps the
    #: id it fetched the row by, so a pre-key checkpoint resumes fully attributed.
    run_id: NotRequired[str | None]
    #: The channels THIS run renders posts for, chosen by the caller.
    #:
    #: It is state rather than a `NodeDeps` field, and that is the point of the key:
    #: as a dependency it was rebuilt from the default on every resume, so a run
    #: started for LinkedIn alone came back from a checkpoint targeting all three.
    #: The channel set is a property of the run, the checkpoint is the run's single
    #: source of truth, so it belongs in the checkpoint.
    #:
    #: `NotRequired` for the same reason as `run_id`: it postdates checkpoints already
    #: written, and `from_checkpoint` supplies the default rather than letting a run
    #: fail to resume over a field a default answers correctly.
    channels: NotRequired[list[str]]

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
    #: What CONVERT wrote: where the CTAs point on the business's OWN site, and the
    #: ask per channel. `{"destination_url": str, "ctas": [{"channel", "text"}]}`.
    #:
    #: It replaced `landing_page` when the founder ruled that we host no page (recorded
    #: in `CLAUDE.md`, 2026-08-21): the business already has a website, so a run improves
    #: that site's SEO and drives traffic to it rather than standing up a competitor to
    #: it. Primitives, not a model, because this is checkpointed to a JSONB column and a
    #: state that cannot serialise cannot resume.
    #:
    #: `landing_page` and `landing_report` are GONE from new states. A checkpoint written
    #: before this change still holds them; nothing reads them, and nothing migrates a
    #: JSONB column, so they simply travel unused until that run is finished.
    distribution: NotRequired[dict[str, Any] | None]
    #: Channel -> the finished post for that channel, as
    #: ``{"body", "hashtags", "hashtags_removed", "hashtags_shortfall", "over_target"}``.
    #:
    #: A dict rather than the bare body string it used to be, because REPACK asks the
    #: model for hashtags and was throwing them away: `REPACK_TOOL` accepts
    #: `posts[].hashtags`, the node stored only the body, so they never reached the
    #: checkpoint and the review screen could not show them. The enforcement counters
    #: travel with the post for a second reason -- `removed` is evidence about the
    #: MODEL, and a screen that shows a clean post without saying nine hashtags were
    #: cut out of it is reporting the renderer's competence as the model's.
    #:
    #: Checkpoints written before this change hold a plain string here. Readers must
    #: tolerate both; nothing migrates a JSONB column for a display field.
    renderings: dict[str, dict[str, Any]]

    # Deterministic verdicts.
    seo_report: NotRequired[dict[str, Any] | None]
    #: The regulated-claim verdict from the `claims` engine, written by VALIDATE.
    #: `None` means VALIDATE has not run yet, which is NOT the same as "clean" --
    #: the graph treats an absent verdict as "nothing checked" and a present
    #: failing one as a publication block.
    claim_check: NotRequired[dict[str, Any] | None]
    #: Who approved publication, and the ONLY thing that lets EXPORT act.
    #:
    #: `None` means nobody has, and EXPORT then publishes nothing and says so. It is
    #: deliberately not derived from "REVIEW has run": the interrupt fires after REVIEW
    #: and the checkpoint is written before the run is parked, so a run whose process
    #: died in that window is resumable with REVIEW in `visited` and no human decision
    #: behind it. An approval is a fact somebody records (:func:`approve`), never one
    #: the graph infers from its own progress.
    approved_by: NotRequired[str | None]
    #: What EXPORT actually did, per target -- statuses, external refs, and whether
    #: anything was SIMULATED. JSON primitives only, like everything else here.
    #:
    #: `None` means EXPORT has not run. An empty `refs` list with a `note` means it ran
    #: and published nothing, which is a different fact and has to read as one.
    published: NotRequired[dict[str, Any] | None]
    #: What MEASURE measured, and -- at least as important -- what it could not.
    #: A metric nobody measured is ABSENT here, never zero.
    measurement: NotRequired[dict[str, Any] | None]
    #: The agentic-RAG evidence: what each retrieving node asked its own documents,
    #: how every returned chunk was graded, and what the loop then decided.
    #:
    #: BOUNDED and TEXT-FREE, and both halves matter. The checkpoint is a JSONB
    #: column rewritten on EVERY node, so anything carried here is paid for eleven
    #: times a run -- the same reason `run_executor.summarise_crawl` exists. So this
    #: holds chunk IDS, grades and decisions and never chunk bodies: an id plus a
    #: grade is what makes a claim auditable, while the body is already in
    #: `document_chunks` and would put the whole knowledge base in the checkpoint.
    #: `nodes.summarise_retrieval` does the dropping and `nodes.append_retrieval_trace`
    #: applies the count cap.
    #:
    #: Absent on every checkpoint written before this key existed, which is why it is
    #: `NotRequired` and why `from_checkpoint` supplies an empty list. A reader of a
    #: JSONB column does not get to assume its own version wrote it.
    retrieval_traces: NotRequired[list[dict[str, Any]]]
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
    run_id: str | UUID | None = None,
    channels: Sequence[str] | None = None,
) -> AgentState:
    """A fresh run.

    ``run_id`` is optional rather than required, and that is a deliberate compromise
    with the tests: the graph and its nodes are driven directly by hundreds of them,
    none of which has a `runs` row, and making the id mandatory would force every one
    of them to invent one. The executor -- the only caller that runs a real run --
    always passes it, and `_actuate` reports an absent one as an unattributed
    actuation rather than guessing.
    """
    return AgentState(
        business_id=str(business_id),
        run_id=str(run_id) if run_id is not None else None,
        goal=goal,
        surfaces=surfaces if surfaces is not None else ["google"],
        channels=normalise_channels(channels),
        dna=dna or {},
        remembered=remembered or [],
        facts={},
        fact_gaps=[],
        opportunity=None,
        outline=None,
        draft=None,
        distribution=None,
        renderings={},
        seo_report=None,
        claim_check=None,
        approved_by=None,
        published=None,
        measurement=None,
        retrieval_traces=[],
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


def parse_run_id(raw: object) -> UUID | None:
    """``raw`` as a run id, or ``None`` if it cannot be one.

    One parser, so the checkpoint reader and the node that builds an `Actuation` cannot
    disagree about what counts as a run id. Everything that is not a UUID -- a missing
    key, a null, a hand-typed word, an integer some UPDATE put in the column -- is
    `None`, because the alternatives are a crash on the publish path or a foreign key
    pointing at nothing.
    """
    if isinstance(raw, UUID):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw.strip())
    except ValueError:
        return None


def _valid_run_id(raw: object) -> str | None:
    """The checkpoint form of :func:`parse_run_id`: a canonical string, or ``None``."""
    parsed = parse_run_id(raw)
    return str(parsed) if parsed is not None else None


def normalise_channels(raw: object) -> list[str]:
    """``raw`` as a usable channel list, or the default set.

    One normaliser, so the API validator, `new_state` and the checkpoint reader cannot
    disagree about what counts as a channel. Three rules, and each has a reason:

    * **Unknown channels are dropped, not carried.** A channel with no entry in
      ``engines/channel/specs.py`` cannot be length- or link-checked, which is already
      why ``actuators/social.py`` refuses one. Carrying it would produce a post nothing
      could validate.
    * **Names are canonicalised**, so ``facebook_post`` and ``facebook`` are one
      channel rather than two renderings of the same post.
    * **Empty normalises to the default set.** A run targeting nothing would pass
      VALIDATE, reach REPACK and render zero posts -- a silently empty deliverable.
      The default is the honest answer to "the caller did not choose".

    Order is the caller's, deduplicated, because it is the order the review screen and
    the export pack list the posts in.

    Only a list or a tuple is accepted, which is narrower than "iterable" and
    deliberately so: this value arrives from a JSONB column, where an array is the only
    thing that legitimately deserialises here, and the two iterables that are NOT
    arrays both fail interestingly. A bare ``"linkedin"`` would iterate into the
    channels ``l``, ``i``, ``n``, ...; a ``{"linkedin": true}`` would be key-mined into
    a real channel and so would look correct while proving nothing about the column.
    Both are malformed, and malformed reads as "nobody chose".
    """
    if not isinstance(raw, list | tuple):
        return list(DEFAULT_CHANNELS)
    seen: dict[str, None] = {}
    for entry in raw:
        if not isinstance(entry, str):
            continue
        channel = canonical_channel(entry.strip())
        if channel and has_spec(channel):
            seen[channel] = None
    return list(seen) if seen else list(DEFAULT_CHANNELS)


def channels_of(state: AgentState) -> tuple[str, ...]:
    """This run's channels, defaulted. The one reader every node uses."""
    return tuple(normalise_channels(state.get("channels")))


def run_uuid(state: AgentState) -> UUID | None:
    """This run's id, for the one place that needs it as a `UUID`: an `Actuation`.

    A reader, not an assertion. A state with no run id is ordinary -- every test that
    drives a node directly has one, and `new_state` allows it -- so this answers
    "attributed or not" and leaves the caller to say so.
    """
    return parse_run_id(state.get("run_id"))


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


def approve(state: AgentState, approver: str = POLICY_APPROVER) -> AgentState:
    """Record who approved publication. The one thing that unlocks EXPORT.

    A function rather than a bare assignment because the string is load-bearing: it
    ends up in `Actuation.approved_by`, which is persisted on every `actions` row and
    is the answer to "on whose authority did this go out". A blank one is refused
    here rather than reaching the actuator, where the same refusal would arrive one
    layer too late to say anything useful about the caller.
    """
    named = approver.strip()
    if not named:
        raise ValueError(
            "an approval needs an approver: a user id, or POLICY_APPROVER when the "
            "surface that approved it carries no identity"
        )
    return {**state, "approved_by": named}


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
    # `retrieval_traces` postdates the first checkpoints ever written, so an older row
    # simply has no such key -- and a run that cannot resume because a DISPLAY field is
    # missing would lose work a customer already paid for. Normalised rather than merely
    # defaulted: this column can also hold whatever a hand-run UPDATE put there, and one
    # malformed entry must not travel into the graph as an entry.
    raw_traces = payload.get("retrieval_traces")
    restored["retrieval_traces"] = (
        [entry for entry in raw_traces if isinstance(entry, dict)]
        if isinstance(raw_traces, list)
        else []
    )
    # Same rule for `run_id`, and normalised rather than merely defaulted for a sharper
    # reason than the traces: this value ends up in `Actuation.run_id`, which is typed
    # `UUID | None` and lands in a foreign key. A checkpoint holding `7`, `"latest"` or
    # a truncated id would either crash the publish path or write an unresolvable
    # reference, so anything that is not a parseable UUID reads as "not attributed" --
    # which is exactly what a pre-key checkpoint means, and the executor overwrites it
    # with the id it fetched the row by anyway.
    restored["run_id"] = _valid_run_id(payload.get("run_id"))
    # Same rule again for `channels`: a pre-key checkpoint has none, and a stored list
    # can hold a channel whose spec has since been removed. Normalising on the way in
    # means a resumed run renders for channels that can actually be validated.
    restored["channels"] = normalise_channels(payload.get("channels"))
    # The payload came from JSON, so the static type is a promise about what
    # to_checkpoint wrote, not a fact the checker can verify. One cast, named.
    return cast("AgentState", restored)
