"""The real graph nodes, wired to the engines, the services and the model router.

Read alongside docs/AGENT_RUNTIME.md section 3, which tabulates every node with its
model tier, its tools and its failure mode.

Five invariants this package exists to hold:

* **HARVEST and VALIDATE call no model.** They are the trustworthy half of the
  pipeline precisely because they are deterministic — one gathers evidence, the other
  measures the result, and neither can hallucinate.
* **A failed fact source degrades the run and is NAMED.** `fact_gaps` is what lets the
  UI say "generated without live research" instead of quietly implying research
  happened.
* **Every tool call goes through the node's allowlist.** No node reaches an engine
  or accepts a model's tool call directly: both go through a
  :class:`~backend.app.agents.tools.NodeToolbox` built from `tools.NODE_TOOLS`, so
  the capability table in docs/AGENT_RUNTIME.md section 3 is enforced rather than
  merely described. A refusal is logged and lands in `state["errors"]`.
* **Retrieval evidence LEAVES the process.** Every node that asks the knowledge base
  goes through :func:`_retrieved`, which returns the trace as well as the passages and
  puts a bounded, text-free summary of it on `state["retrieval_traces"]`. Before that,
  the rewritten queries, the per-chunk grades and the fallback decision were computed
  and then dropped at the call site, so the one artifact that shows the retrieval was
  agentic — rather than a single vector search with a nice name — existed only in a log
  line nobody reads. A capability nothing can show is indistinguishable from one that
  is not there, which is the same rule the `fact_gaps` bullet above states for absence.
* **A banned claim stops publication, deterministically.** The claim list is in the
  system prompt too, but a prompt is a request: untrusted page text can argue a
  model out of it, and a model can simply forget. VALIDATE re-checks the finished
  draft with the `claims` engine, and the graph will not carry a failing verdict
  to REVIEW.

Every dependency is injected through :class:`NodeDeps`, so the whole set runs with no
network, no database and no model in tests. Nodes RECEIVE a router; they never build
one, which is why making the provider and model choice configurable elsewhere needs
no change here.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, NamedTuple
from uuid import UUID

from backend.app.actuators import (
    Actuation,
    Actuator,
    ActuatorStore,
    OutcomeStatus,
    actuate,
)
from backend.app.actuators import (
    Outcome as ActuationOutcome,
)
from backend.app.actuators.owner_notice import (
    ACTION_TYPE as OWNER_NOTICE_ACTION,
)
from backend.app.actuators.owner_notice import (
    OwnerNoticeIdentity,
    build_owner_notice_actuation,
)
from backend.app.agents.nodes.prompts import (
    DISTRIBUTION_TOOL,
    GENERATE_TOOL,
    OPPORTUNITY_TOOL,
    PLAN_TOOL,
    PROMPT_VERSION,
    REPACK_TOOL,
    WEB_SEARCH_TOOL,
    fence,
    system,
)
from backend.app.agents.state import (
    DEFAULT_CHANNELS,
    AgentState,
    NodeError,
    channels_of,
    run_uuid,
)
from backend.app.agents.tools import (
    CHANNEL_VALIDATE,
    CLAIMS_CHECK,
    CRAWL_SITE,
    GEO_PROBE,
    KB_SEARCH,
    MEMORY_LOAD,
    NAP_AUDIT,
    NOTIFY,
    PUBLISH,
    SEO_SCORE,
    SERP_SEARCH,
    WEB_SEARCH,
    NodeToolbox,
    ToolImpl,
    ToolNotAllowedError,
)
from backend.app.engines.channel import (
    CHANNEL_SPECS,
    ChannelSpec,
    canonical_channel,
    enforce_hashtags,
    hard_char_limits,
)
from backend.app.engines.claims import ClaimCheckRequest, check_claims
from backend.app.engines.nap import (
    CanonicalNap,
    DirectoryListing,
    RawNap,
    audit_nap,
    normalise_nap,
)
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
#:
#: DERIVED, not declared. This used to be its own table and it disagreed with the one
#: the eval harness graded against -- different channel names, and LinkedIn's 3,000
#: was a plain ceiling here and a *hard* max there. It is now the platform reject
#: thresholds out of `engines/channel/specs.py`, which the rubric reads too. Note what
#: that changed: Facebook's ceiling is its real 63,206 rather than the 2,000 that used
#: to live here, because 2,000 is an editorial target and truncating good copy at a
#: target is not enforcement. Being over the target is REPORTED instead (see `repack`).
CHANNEL_LIMITS: Mapping[str, int] = hard_char_limits()


#: The bio-link hub always gets its own CTA, whatever channels a run renders posts
#: for. docs/CHANNELS.md section 1: an Instagram feed caption and a TikTok caption
#: cannot carry a clickable link at all, so ``/go/{slug}`` is the ENTIRE conversion
#: path for those surfaces -- and a hub with no CTA on it is an empty page in
#: somebody's bio.
HUB_CHANNEL: str = "link_hub"

#: How many keyword seeds HARVEST searches. Each is a provider call, so this is the
#: main lever on harvest cost.
MAX_SEED_SEARCHES = 3

# --------------------------------------------------------------------------- #
# What of a RetrievalTrace reaches the checkpoint
#
# `state["retrieval_traces"]` is the agentic-RAG evidence, and it is carried in a
# JSONB column that is rewritten on EVERY node -- the same constraint that produced
# `run_executor.summarise_crawl`, and the same answer: compact it here, where the
# object is in hand and the cheap losses are known.
#
# Five retrieval sites exist (HARVEST, OPPORTUNITY, PLAN, GENERATE, CONVERT) and
# GENERATE and CONVERT can each re-run twice on the VALIDATE loop, so a run makes at
# most nine calls. `kb_service` bounds each trace at two attempts and each attempt at
# twelve graded chunks. The caps below sit at or just above those structural maxima:
# they are a guard against a `retrieve` dependency that does not respect its own
# contract, not a routine trim -- an ordinary run loses nothing to them.
# --------------------------------------------------------------------------- #

#: Retrieval calls kept per run. Above the structural maximum of nine, so the OLDEST
#: entry is only ever dropped by a trace source that has stopped honouring its bounds.
#: `seq` on each entry is what makes such a drop visible instead of silent.
MAX_RETRIEVAL_TRACES: Final = 12

#: Attempts kept per trace. `kb_service.MAX_RETRIEVAL_ATTEMPTS` is 2.
MAX_RETRIEVAL_ATTEMPTS_KEPT: Final = 3

#: Graded chunks kept per attempt. A widened second attempt asks for 12.
MAX_RETRIEVAL_GRADES_KEPT: Final = 12

#: Characters kept of a grader's justification for one chunk.
#:
#: The justification stays because a grade with no reason is unreviewable, which is
#: `ChunkGrade`'s own stated rule. It is CLAMPED because it is model prose: the
#: contract asks for one line, and nothing enforces that on the way out. At the caps
#: above this is the dominant term -- 12 grades x 3 attempts x 160 characters is under
#: 6 KB per entry, which is the budget this number was chosen to hold.
RETRIEVAL_REASON_CHARS: Final = 160

# --------------------------------------------------------------------------- #
# EXPORT's vocabulary
#
# Dotted, target-first, matching `actuators/contract.py`'s convention: the name says
# which integration owns the effect. The `action_type` is half of the idempotency key,
# so these strings are as load-bearing as a column name -- renaming one makes every
# prior action look like a different effect and re-publishes it.
# --------------------------------------------------------------------------- #

#: One channel rendering, posted to that channel.
SOCIAL_POST_ACTION: Final = "social.post"
#: The landing page the tracked links point at.
PAGE_PUBLISH_ACTION: Final = "publish.page"
#: The one message EXPORT sends the owner: what went live and what did not.
#:
#: `notify.owner`, NOT `notify.email`, and the distinction is the whole of A4. The email
#: actuator refused this message on purpose and every time -- no sender, no body, no
#: unsubscribe link, no consent basis -- so owner notification has never once worked. The
#: fix is a transactional action type (`actuators/owner_notice.py`), not a widened
#: `CONSENT_BASES`: an operational notice must not offer to unsubscribe you from "your run
#: published 3 of 4", and `existing_customer` is a soft-opt-in MARKETING basis, so
#: borrowing it would record a marketing claim about a message that is not marketing.
#:
#: Changing this string re-keys the action, which the header above warns about. That is
#: intended here rather than risky: no `notify.email` from this node has ever SUCCEEDED, so
#: there is no prior effect for a new key to duplicate.
NOTIFY_ACTION: Final = OWNER_NOTICE_ACTION

#: What `publish` may actuate, and what `notify` may. Two grants in the allowlist mean
#: two capabilities, so they cannot share one implementation: a node holding `notify`
#: and not `publish` must not be able to post, and the check has to live somewhere that
#: the grant name reaches.
#: What EXPORT may actuate. `PAGE_PUBLISH_ACTION` is deliberately NOT here any more:
#: the founder ruled we host no landing page, so no run publishes one. The action type
#: and its actuator remain in the codebase because pieces published before that ruling
#: still exist and `GET /p/{piece_id}` must keep serving them.
PUBLISHABLE_ACTIONS: Final[frozenset[str]] = frozenset({SOCIAL_POST_ACTION})
#: Deliberately just the one, and `notify.email` is deliberately NOT in it. No node sends
#: marketing email, so granting the capability here would widen what an induced tool call
#: could reach for nothing in return.
NOTIFIABLE_ACTIONS: Final[frozenset[str]] = frozenset({NOTIFY_ACTION})

#:
#: A constant rather than a slug because the page's public slug is minted by
#: `landing_service` when the piece is stored, and it is not in the agent state -- so
#: there is nothing truthful to put here per run. One landing page per run makes a
#: constant sufficient: `business_id` and the payload hash carry the rest of the
#: identity into the idempotency key, so an edited page is a new effect and an
#: unchanged one is a replay.
LANDING_TARGET: Final = "landing_page"

#: Why nothing was published, per reason. Stated in full on the run, because "EXPORT
#: ran and published nothing" has four causes and they mean entirely different things
#: to whoever reads the run.
NOT_APPROVED_NOTE: Final = (
    "Nothing was published: this run carries no approval. `approved_by` is empty, and "
    "the actuator layer requires one for every side effect -- so the content is here, "
    "reviewable, and unpublished. Approving the run records the approver and a resume "
    "publishes it."
)
NOTHING_TO_PUBLISH_NOTE: Final = (
    "Nothing was published: this run produced no channel rendering and no landing "
    "page, so there was nothing to send. Whatever was lost upstream is named in the "
    "run's errors."
)
PUBLISH_REVOKED_NOTE: Final = (
    "Nothing was published: an operator has REVOKED EXPORT's `publish` tool, so the "
    "kill switch is doing exactly what it is for. The approved content is stored and "
    "unpublished; restoring the grant and resuming publishes it. Deliberately distinct "
    "from 'no integration is configured' -- one of those is somebody's decision and the "
    "other is a deployment gap, and they are fixed by different people."
)
NO_ACTUATOR_NOTE: Final = (
    "Nothing was published: no publishing integration is configured on this "
    "deployment, so there is nowhere to send it. The approved content is stored and "
    "can be published by hand, or by wiring an actuator and resuming. This is NOT a "
    "claim that a post went out."
)

#: The analytics gap MEASURE names rather than fills.
#:
#: `analytics.fetch` is in MEASURE's allowlist and is deliberately implemented by
#: nothing: `CLAUDE.md` cuts GSC/GA4 from this build -- two OAuth flows for a metric
#: that cannot move inside the project's timeline -- so the grant records a capability
#: the design reserves and the deployment does not have. Named on every measurement,
#: because a report that silently omits search traffic reads as a report that measured
#: it and found nothing.
ANALYTICS_GAP: Final = (
    "Google Search Console / GA4 (cut from this build: attribution is proven by our "
    "own short links instead, so ranking and organic-traffic movement are NOT measured "
    "here)"
)

#: Why no lead count appears next to a freshly published piece.
#:
#: Leads arrive when a visitor clicks `/go/{code}` and submits the form, which is
#: minutes to weeks after EXPORT and is read by the leads surface, not by the run. So
#: MEASURE reports the attribution PATH it established and refuses to print a lead
#: count of zero for a piece that has not been live long enough to have one -- a zero
#: here is indistinguishable from a piece nobody has seen yet.
LEADS_NOT_YET_NOTE: Final = (
    "No leads are attributable yet: the tracked links were published moments ago, and "
    "a lead is counted when a visitor arrives through /go/{code} and submits the form. "
    "This is the attribution path, not a result -- zero would read as a measurement."
)


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
    #: AI-visibility probe over a fixed prompt set. None = no probe store or router
    #: configured for this run; HARVEST then records the gap rather than reporting a
    #: share of voice nobody measured.
    geo_probe: Callable[..., Awaitable[Any]] | None = None
    #: A live web search GENERATE may ask for mid-draft, to check a fact it is about
    #: to write. Wired only when the SERP provider is real, for the same reason
    #: `serp_search` is: a fake result that reads as research is worse than no
    #: research, because it cannot be told apart from the real thing afterwards.
    web_search: Callable[..., Awaitable[Any]] | None = None
    #: Resolves a dotted action type to the actuator that performs it.
    #:
    #: A resolver rather than a mapping so a deployment can decide per action type --
    #: a real WordPress publisher, a `FakeActuator` for the channels it has no
    #: credential for -- without the node knowing which is which. `None` (the default)
    #: means NO integration is configured at all, and EXPORT then reports that it
    #: published nothing and why, rather than skipping silently.
    actuator_for: Callable[[str], Actuator | None] | None = None
    #: The `actions` ledger the idempotency key is claimed in.
    #:
    #: Injected for the same reason `load_memory` is a callable: the nodes stay
    #: database-free, so every node test is hermetic. `actuate()` needs both this and
    #: an actuator, and EXPORT treats either one missing as "not wired".
    actuator_store: ActuatorStore | None = None
    #: Who EXPORT's owner notice goes to, and who it comes from.
    #:
    #: Injected for the same reason `actuator_store` is: resolving it needs a database read
    #: (`businesses.owner_id -> users.email`) and the nodes must stay database-free, so
    #: every node test stays hermetic.
    #:
    #: **It must never be derived from `state["dna"]`.** That address is extracted from a
    #: crawled homepage -- data we do not control -- and our own operational mail has to go
    #: to the AUTHENTICATED account, or a page we crawled could redirect it. The node reads
    #: this and nothing else; `actuators/owner_notice.py` refuses a payload that declares a
    #: crawled provenance, so the rule is enforced at the boundary as well as observed here.
    #:
    #: `None` means no owner notice is possible on this deployment -- no account address
    #: resolved, or no `OWNER_NOTICE_FROM` sender identity -- and EXPORT reports that in a
    #: named note rather than skipping silently.
    owner_notice: OwnerNoticeIdentity | None = None
    #: Mints the tracked links and writes the article piece, after approval.
    #:
    #: Injected like `actuator_store` and for the same reason: it needs a database and
    #: the nodes must stay database-free, so every node test stays hermetic. `None`
    #: means no link minting is wired — EXPORT then says so rather than posting content
    #: whose call to action points nowhere.
    #:
    #: NOT an actuator, deliberately. Minting a link and writing a row are internal
    #: effects; the landing actuator existed because SERVING a page is an external one.
    #: Routing an internal write through the actuator ledger would put an idempotency
    #: key on something the database already makes atomic.
    publish_distribution: Callable[..., Awaitable[Any]] | None = None
    #: node -> tools an operator has revoked, loaded from `node_tool_policies`.
    #:
    #: Injected like every other dependency rather than read here, because the nodes
    #: must stay database-free: a `_toolbox` that queried a table would put I/O behind
    #: a property and make every node test need Postgres.
    revoked_tools: Mapping[str, frozenset[str]] | None = None


def _implementations(deps: NodeDeps) -> dict[str, ToolImpl]:
    """Map tool NAMES to the callables that implement them.

    The indirection is what makes the allowlist enforceable: a node asks for
    `crawl.site` and cannot reach `deps.crawl_site` any other way, so there is
    exactly one place where "may this node do this" is decided. Unconfigured
    dependencies are omitted rather than mapped to None, so `available()` answers
    "granted and wired" in one call.
    """
    candidates: dict[str, ToolImpl | None] = {
        CRAWL_SITE: deps.crawl_site,
        SERP_SEARCH: deps.serp_search,
        KB_SEARCH: deps.retrieve,
        MEMORY_LOAD: deps.load_memory,
        SEO_SCORE: deps.score_page or score_page,
        # Deterministic and always available: the guard must not be able to be
        # "not configured", or a business could be published unchecked by omission.
        CLAIMS_CHECK: check_claims,
        # Same reasoning: a landing page whose conversion audit was skipped because
        # nobody wired it would look like a page that passed.
        # Granted to REPACK since the allowlist was written, and implemented by
        # nothing until now -- so `enforce_hashtags` had never once run inside a run,
        # and the engine's own measured finding (a prompt ending in the literal words
        # "Keine Hashtags" produced 21 of them) was going uncorrected in the product
        # while being corrected in the eval harness. Deterministic, so always
        # available for the same reason the two guards above are.
        CHANNEL_VALIDATE: enforce_hashtags,
        # Deterministic, so always available -- and now it has a SOURCE. The audit
        # shipped in Phase 4 and HARVEST held the grant with nothing behind it,
        # because nothing produced listings to diff. `engines/nap/extract` reads the
        # business's own `LocalBusiness` JSON-LD and its Impressum, which is a
        # narrower claim than a directory audit and one we can actually support.
        NAP_AUDIT: _audit_own_nap,
        GEO_PROBE: deps.geo_probe,
        WEB_SEARCH: deps.web_search,
        # The two actuator tools, wired together or not at all: `actuate()` needs an
        # actuator AND a ledger, and half of that is not a working publisher. Only
        # EXPORT holds them, so mapping them here cannot widen any other node -- the
        # allowlist decides who may reach them, and this dict decides whether they
        # exist at all.
        PUBLISH: _actuation_tool(deps, PUBLISHABLE_ACTIONS),
        NOTIFY: _actuation_tool(deps, NOTIFIABLE_ACTIONS),
        # `analytics.fetch` is ABSENT on purpose and must stay absent: GSC/GA4 is cut
        # from this build (`CLAUDE.md`), so MEASURE holds the grant, finds it unwired,
        # and names the gap. Wiring anything here -- least of all a fake -- would turn
        # a stated omission into a fabricated metric.
    }
    return {name: impl for name, impl in candidates.items() if impl is not None}


def _actuation_tool(deps: NodeDeps, actions: frozenset[str]) -> ToolImpl | None:
    """The implementation behind `publish` / `notify`: one actuation through `actuate`.

    Returns None when no integration is configured, which is what makes
    `NodeToolbox.available()` answer "granted but not wired" for EXPORT -- the state
    the node has to REPORT rather than skip.

    `actuate()` is called here and nowhere else in the graph. Everything it owns --
    claiming the idempotency key before the call, refusing without an approval,
    settling the row afterwards -- applies to every publish by construction rather
    than by each publisher remembering.
    """
    resolve, store = deps.actuator_for, deps.actuator_store
    if resolve is None or store is None:
        return None

    async def perform(actuation: Actuation) -> ActuationOutcome:
        if actuation.action_type not in actions:
            # A wiring mistake, refused rather than performed: `publish` and `notify`
            # are separate grants, and an implementation that honoured both would make
            # revoking one of them meaningless.
            return ActuationOutcome(
                status=OutcomeStatus.REFUSED,
                action_type=actuation.action_type,
                target=actuation.target,
                error=(
                    f"{actuation.action_type!r} is not one of the actions this tool "
                    f"performs ({', '.join(sorted(actions))})"
                ),
            )
        actuator = resolve(actuation.action_type)
        if actuator is None:
            # Configured integrations, but none for THIS action type. Distinct from
            # "nothing is configured", which is `available()` answering False.
            return ActuationOutcome(
                status=OutcomeStatus.REFUSED,
                action_type=actuation.action_type,
                target=actuation.target,
                error=(
                    f"no actuator is configured for {actuation.action_type!r}, so "
                    "nothing was sent to this destination"
                ),
            )
        return await actuate(actuation, actuator=actuator, store=store)

    return perform


def _toolbox(node: str, deps: NodeDeps) -> NodeToolbox:
    """The gate this node's tool calls pass through.

    This is the one call site the tool-policy service's docstring pointed at: until it
    passed `revoked`, an operator's revocation was stored, displayed and computed but
    NOT honoured by the running graph -- a kill switch that did nothing, which is worse
    than no kill switch because somebody would believe they had pulled it.
    """
    revoked = (deps.revoked_tools or {}).get(node, frozenset())
    return NodeToolbox(
        node=node, implementations=_implementations(deps), revoked=frozenset(revoked)
    )


def _with_refusals(state: AgentState, box: NodeToolbox, updates: dict[str, Any]) -> dict[str, Any]:
    """Surface any refused tool call on the run state.

    Appended to the existing list rather than assigned, because the graph MERGES a
    node's updates into the state: assigning `errors` would silently discard every
    degradation recorded by earlier nodes.
    """
    refused = box.node_errors()
    if refused:
        existing: list[NodeError] = list(updates.get("errors") or state["errors"])
        updates["errors"] = [*existing, *refused]
    return updates


def _tool_arguments(completion: Any, name: str, box: NodeToolbox) -> dict[str, Any] | None:
    """Pull one tool call's arguments, or None if the model answered in prose.

    Every returned call is run past the node's allowlist first. That is the
    backstop against an induced tool call: text inside the untrusted envelope can
    ask the model to call `publish`, and the model may comply, but a node that does
    not hold `publish` drops the call and records the refusal instead of executing
    it. Dropping rather than raising is deliberate — the run should continue with
    the legitimate output, and an attacker must not be able to end a run by
    smuggling one instruction into a competitor's page.

    None is returned rather than raised because each caller has a different correct
    response: OPPORTUNITY treats it as "nothing found", PLAN treats it as a failure.
    """
    for call in completion.tool_calls:
        if not isinstance(call, ToolCall):
            continue
        if not box.accept(call.name):
            continue
        if call.name == name:
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
    box: NodeToolbox,
    extra_tools: Sequence[ToolSpec] = (),
    max_tool_rounds: int = 2,
) -> tuple[dict[str, Any] | None, Decimal]:
    """One model exchange: assemble, call, and return the structured arguments and cost.

    The tool list handed to the model is filtered through the node's allowlist
    (docs/AGENT_RUNTIME.md section 4: a tool the node cannot have is REMOVED, not
    refused on call, so the model never plans around a capability it will not get).
    A node whose own output tool is not in its allowlist is a wiring bug, and it
    raises rather than silently calling the model with no tools at all.

    ``extra_tools`` turns this into a bounded LOOP rather than a single exchange. It
    exists for GENERATE's `web_search`: a model part-way through a draft can notice
    that a fact is missing and ask for it, which is the difference between research
    and a plausible guess. Four rules keep the loop from being an open-ended agent:

    * **the output tool is offered FIRST**, and that is load-bearing rather than
      tidy — the deterministic `FakeProvider` answers with `tools[0]`, so putting the
      record tool first is what keeps every hermetic test in this repo exercising the
      normal path instead of the search path;
    * **the loop is capped** at ``max_tool_rounds`` requests, after which the extra
      tools are withdrawn and the model is asked for its answer with what it has;
    * **an unwired or refused tool is answered honestly**, with a message saying the
      search did not run, so the model does not silently treat an empty result as
      "there is nothing to find";
    * **every result is fenced as untrusted**, because a search result is text from a
      page the business does not control — the same envelope harvested facts get.
    """
    # Extra tools are offered only when they are granted AND WIRED. `box.offer`
    # filters on the allowlist alone, which is right for an output tool -- a node
    # missing its own is a wiring bug worth raising over -- but wrong for a capability
    # the deployment simply does not have: docs/AGENT_RUNTIME.md §4's rule is that a
    # tool the node will not get is REMOVED, so the model never plans around it. The
    # "this search did not run" answer in `_tool_result` stays as the backstop for a
    # revocation that lands between the offer and the call.
    offered = box.offer([tool, *(spec for spec in extra_tools if box.available(spec.name))])
    if not any(spec.name == tool.name for spec in offered):
        raise ToolNotAllowedError(box.node, tool.name, box.allowed)

    messages: list[Message] = [
        system(role, state["dna"], state["remembered"]),
        Message(role=Role.USER, content=body),
    ]
    trace = {
        "business_id": str(state.get("business_id") or ""),
        "node": box.node,
        "prompt_version": PROMPT_VERSION,
    }
    spent = Decimal("0")

    for round_index in range(max_tool_rounds + 1):
        # The last round withdraws everything except the output tool: a model that has
        # already searched twice and is offered a third search will take it, and the
        # node needs a page, not a bibliography.
        tools = offered if round_index < max_tool_rounds else [tool]

        completion = await deps.router.complete(
            task,
            messages,
            tools=tools,
            # Current Claude models reject `temperature` outright, and every node here
            # wants the provider default anyway.
            temperature=None,
            # Nothing passed this before, so EVERY llm span was recorded with an empty
            # run_id, business_id and node -- `llm_span_fields` defaults them to "" and
            # the no-op tracer meant nobody saw it. It is also what a `model_usage` row
            # needs to be attributable to anything, so the ledger could not have been
            # written correctly even once a writer existed.
            trace=trace,
        )
        spent += Decimal(str(completion.usage.usd))

        args = _tool_arguments(completion, tool.name, box)
        if args is not None:
            return args, spent

        requests = _requested(completion, {spec.name for spec in tools} - {tool.name}, box)
        if not requests:
            # Prose, or a tool nobody granted. Either way there is nothing more this
            # exchange can produce, and each caller has its own correct response to
            # `None` -- OPPORTUNITY treats it as "nothing found", PLAN as a failure.
            return None, spent

        # An empty assistant turn carrying only the tool calls, which is what both
        # adapters expect to see replayed before the results.
        messages.append(Message(role=Role.ASSISTANT, content="", tool_calls=list(requests)))
        for call in requests:
            messages.append(
                Message(
                    role=Role.TOOL,
                    content=await _tool_result(deps, box, call),
                    tool_call_id=call.call_id,
                )
            )

    return None, spent


def _requested(completion: Any, names: set[str], box: NodeToolbox) -> list[ToolCall]:
    """The model's calls for tools OTHER than the output tool, allowlist-filtered.

    Filtered through :meth:`NodeToolbox.accept`, which is the backstop against an
    induced call: crawled page text can ask the model to reach for something, and the
    node drops what it does not hold and records the refusal rather than executing it.
    """
    return [
        call
        for call in completion.tool_calls
        if isinstance(call, ToolCall) and call.name in names and box.accept(call.name)
    ]


async def _tool_result(deps: NodeDeps, box: NodeToolbox, call: ToolCall) -> str:
    """Run one model-requested tool and render its result as an untrusted payload.

    Never raises. A search that fails, is unwired, or is refused comes back as a
    SENTENCE saying so — because the alternative is handing the model an empty result,
    which it will read as "there is nothing to find" and write around as though the
    absence were a fact.
    """
    if call.name != WEB_SEARCH:
        return f"{call.name} is not available in this step."

    query = str(call.arguments.get("query", "")).strip()
    if not query:
        return "No query was given, so no search ran."
    if not box.available(WEB_SEARCH):
        return (
            "Live web search is not configured on this deployment, so this search did "
            "NOT run. Do not treat that as evidence either way — write only what the "
            "supplied evidence supports, and leave the rest out."
        )

    try:
        page = await box.call(WEB_SEARCH, query)
    except ToolNotAllowedError:
        raise
    except Exception as exc:  # broad on purpose: a dead search must not end the run
        logger.warning("%s: web search failed: %s", box.node, exc)
        return (
            f"The search for {query!r} failed and returned nothing. Do not treat that "
            "as evidence either way."
        )

    results = getattr(page, "results", None) or []
    if not results:
        return f"The search for {query!r} returned no results."

    lines = [
        f"- {getattr(result, 'title', '') or 'untitled'} ({getattr(result, 'url', '')}): "
        f"{(getattr(result, 'snippet', '') or '')[:300]}"
        for result in results[:5]
    ]
    # Fenced, like every other thing a third party wrote. A search result is a page
    # the business does not control, so it is quoted evidence and never instruction.
    return f"Search results for {query!r}:\n" + fence("\n".join(lines))


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
        box = _toolbox("INTAKE", deps)
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
        if box.available(MEMORY_LOAD):
            try:
                updates["remembered"] = await box.call(MEMORY_LOAD)
            except ToolNotAllowedError:
                # Never swallowed into a fact gap: an allowlist refusal is a wiring
                # fault or an attack, and either way it must be loud.
                raise
            except Exception as exc:  # broad on purpose: memory is an enhancement
                logger.warning("intake: could not load memory: %s", exc)
                updates["fact_gaps"] = [*state["fact_gaps"], "remembered preferences"]

        return _with_refusals(state, box, updates)

    async def harvest(state: AgentState) -> dict[str, Any]:
        """Gather evidence from the engines. Engines only — no model, ever.

        Each source is independent and failure is per-source: one dead provider must
        not cost the run the evidence the others returned.
        """
        box = _toolbox("HARVEST", deps)
        dna = state["dna"]
        facts: dict[str, Any] = {}
        gaps: list[str] = []

        if box.available(CRAWL_SITE) and dna.get("website"):
            try:
                facts["site"] = await box.call(CRAWL_SITE, str(dna["website"]))
            except ToolNotAllowedError:
                # A refusal is NOT a dead source. Degrading it into a fact gap would
                # hide the one failure mode this allowlist exists to make visible.
                raise
            except Exception as exc:  # broad on purpose: one dead source must not end the run
                logger.warning("harvest: site crawl failed: %s", exc)
                gaps.append("website crawl")
        elif not dna.get("website"):
            gaps.append("website (none on record)")

        if box.available(SERP_SEARCH):
            seeds = _seed_queries(dna)
            pages: list[SerpPage] = []
            try:
                for seed in seeds[:MAX_SEED_SEARCHES]:
                    pages.append(
                        await box.call(SERP_SEARCH, seed, locale=dna.get("locale", "de"), limit=10)
                    )
            except ToolNotAllowedError:
                raise
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

        grounding = _no_grounding()
        if box.available(KB_SEARCH):
            grounding = await _retrieved(box, state["goal"], "HARVEST", state)
            if grounding.failed:
                gaps.append("uploaded documents")
            elif grounding.trace is not None:
                facts["knowledge"] = {
                    "outcome": getattr(grounding.trace, "outcome", None),
                    "chunks": len(getattr(grounding.trace, "chunks", []) or []),
                }
        else:
            gaps.append("uploaded documents")

        # The NAP audit needs no provider and no model: it diffs the business's own
        # published address against itself. Its listings come from the crawl that just
        # ran, so it is skipped -- and NAMED as skipped -- when the crawl produced
        # nothing, rather than reporting a clean audit of zero listings.
        if box.available(NAP_AUDIT) and facts.get("site"):
            try:
                audit = await box.call(NAP_AUDIT, dna, facts["site"])
            except ToolNotAllowedError:
                raise
            except Exception as exc:  # broad on purpose, per-source degradation
                logger.warning("harvest: nap audit failed: %s", exc)
                gaps.append("address consistency")
            else:
                if audit is None:
                    # Nothing published a NAP we could read. Not a finding, and not a
                    # pass either: an audit of no listings would score 100 and tell a
                    # business its address is consistent everywhere it is not listed.
                    gaps.append("address consistency (no address published on the site)")
                else:
                    facts["nap"] = audit

        if box.available(GEO_PROBE):
            try:
                facts["visibility"] = await box.call(GEO_PROBE, dna)
            except ToolNotAllowedError:
                raise
            except Exception as exc:  # broad on purpose, per-source degradation
                logger.warning("harvest: visibility probe failed: %s", exc)
                gaps.append("AI answer-engine visibility")
        else:
            gaps.append("AI answer-engine visibility (no probe configured)")

        return _with_refusals(
            state,
            box,
            {
                "facts": facts,
                "fact_gaps": [*state["fact_gaps"], *gaps],
                **grounding.updates,
            },
        )

    async def opportunity(state: AgentState) -> dict[str, Any]:
        """Rank the evidence into opportunities and take the best.

        An empty list is a legitimate answer, and the graph ends the run honestly on
        it. Inventing a topic to fill the slot is the failure mode here.
        """
        box = _toolbox("OPPORTUNITY", deps)
        # The business's own material, against the run's goal. `docs/AGENT_RUNTIME.md`
        # granted this node `kb.search` and nothing called it, so the ranking was made
        # from the crawl and the SERP alone -- which is exactly the evidence a
        # competitor also has. What a business can uniquely write about is in its own
        # documents, so a topic chosen without reading them is a topic anyone could
        # have chosen.
        grounding = await _retrieved(box, state["goal"], "OPPORTUNITY", state)
        passages = grounding.passages
        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.PRIORITISE,
            role=(
                "You choose what this business should publish next. You rank only what "
                "the evidence supports, and you return an empty list rather than a "
                "weak idea."
            ),
            state=state,
            body=(
                f"Goal: {state['goal']}\n\nEvidence:\n{_evidence(state)}\n"
                f"{passages}\n"
                "Call record_opportunities. Score each 0-100 on likely lead impact "
                "against effort."
            ),
            tool=OPPORTUNITY_TOOL,
        )
        if not args:
            # The trace rides out even here. A retrieval that ran and a model that then
            # answered with nothing is exactly the pair a reviewer needs to see: without
            # it, "the documents had nothing to say" and "the model said nothing about
            # the documents" look identical on the screen.
            return _with_refusals(
                state, box, {"opportunity": None, "_cost": cost, **grounding.updates}
            )

        ranked = sorted(
            (o for o in args.get("opportunities", []) if o.get("title")),
            key=lambda o: -int(o.get("score", 0)),
        )
        return _with_refusals(
            state,
            box,
            {
                "opportunity": ranked[0] if ranked else None,
                "_cost": cost,
                **grounding.updates,
            },
        )

    async def plan(state: AgentState) -> dict[str, Any]:
        """Outline the page. A target keyword is mandatory."""
        box = _toolbox("PLAN", deps)
        opp = state.get("opportunity") or {}
        # Retrieved against the CHOSEN opportunity, not the run goal: the goal is "more
        # local leads" and retrieving against it returns whatever is most on-topic for
        # the business in general. An outline needs the material about the thing being
        # written, which is what makes a section heading answerable from evidence.
        question = str(opp.get("title") or "").strip() or state["goal"]
        grounding = await _retrieved(box, question, "PLAN", state)
        passages = grounding.passages
        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.PLAN,
            role=(
                "You outline one page. Every section must serve the search intent "
                "behind the target keyword."
            ),
            state=state,
            body=(
                f"Opportunity: {json.dumps(opp, ensure_ascii=False, default=str)}\n\n"
                f"Evidence:\n{_evidence(state)}\n{passages}\nCall record_outline."
            ),
            tool=PLAN_TOOL,
        )
        if not args or not str(args.get("target_keyword", "")).strip():
            raise ValueError(
                "the outline has no target keyword; a page without one cannot be "
                "scored, and generating it would waste the run's budget"
            )
        return _with_refusals(state, box, {"outline": args, "_cost": cost, **grounding.updates})

    async def generate(state: AgentState) -> dict[str, Any]:
        """Write the page, grounded in the evidence.

        On a retry the SEO findings are passed through VERBATIM. Without that the
        validation loop is theatre: the model would be asked to try again with no idea
        what failed.
        """
        box = _toolbox("GENERATE", deps)
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

        # A failed claim check is passed through verbatim for the same reason the SEO
        # hints are: the model is told exactly which phrase is forbidden, so the retry
        # is a correction rather than a guess. It is appended AFTER the SEO block and
        # phrased as non-negotiable, because a low score is a quality problem and a
        # banned claim is a legal one.
        claims = state.get("claim_check") or {}
        if claims and not claims.get("passed", True) and claims.get("fix_hint"):
            retry += "\n\n" + str(claims["fix_hint"])

        # The business's own material, against the keyword the page is being written
        # for. This is the node where a missing passage becomes an invented sentence,
        # so it is the node where the grant mattered most and had nothing behind it.
        keyword = str(outline.get("target_keyword") or "").strip() or state["goal"]
        grounding = await _retrieved(box, keyword, "GENERATE", state)
        passages = grounding.passages

        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.GENERATE,
            role=(
                "You write the page. Every factual claim comes from the evidence or "
                "from the business's own documents; if the evidence does not support a "
                "claim, you leave it out."
            ),
            state=state,
            body=(
                f"Outline: {json.dumps(outline, ensure_ascii=False, default=str)}\n\n"
                f"Evidence:\n{_evidence(state)}\n{passages}{retry}\n\n"
                "Call record_page. If a fact you need is genuinely missing, you may "
                "call web_search ONCE for it rather than guessing."
            ),
            tool=GENERATE_TOOL,
            extra_tools=(WEB_SEARCH_TOOL,),
        )
        if not args:
            raise ValueError("the model did not return a page; expected a record_page tool call")
        return _with_refusals(state, box, {"draft": args, "_cost": cost, **grounding.updates})

    async def convert(state: AgentState) -> dict[str, Any]:
        """Choose where the clicks land on the business's OWN site, and write the ask.

        This is CONVERSION -- link three of the lead chain in docs/FEATURES.md section 0
        -- but it no longer produces the page. The founder ruled that we host none
        (`CLAUDE.md`, 2026-08-21): the business already has a website, so the job is to
        pick the page there that already answers what the post is about and to write the
        ask that earns the click.

        It still sits BETWEEN GENERATE and VALIDATE, and for the same reason as before:
        VALIDATE is where the deterministic verdicts are taken, and the regulated-claim
        gate must see the CTA copy before REVIEW. Copy produced after VALIDATE would be
        copy nothing had checked.

        **The destination is proposed here and enforced later.** This node reads crawled
        pages -- text we do not control -- so the URL it returns is untrusted. It is
        checked against the business's own domain at publish time by
        `landing_service.publish_distribution`, which refuses anything off-site. Checking
        it in the node instead would put the guard where a retry could talk past it.
        """
        box = _toolbox("CONVERT", deps)
        draft = state.get("draft") or {}
        outline = state.get("outline") or {}
        channels = _cta_channels(channels_of(state))
        grounding = await _retrieved(box, _proof_question(state), "CONVERT", state)
        passages = _passages(grounding.trace)

        # The claim verdict covers the article AND this copy as one check, so a retry
        # arrives here with the whole verdict rather than a per-artifact one.
        claims = state.get("claim_check") or {}
        retry = ""
        if claims and not claims.get("passed", True) and claims.get("fix_hint"):
            retry += "\n\n" + str(claims["fix_hint"])

        # The pages the crawl actually found, so the model chooses from what exists
        # rather than inventing a path. A URL it made up would be refused at publish
        # time, which is safe but wastes the run.
        site = (state.get("facts") or {}).get("site") or {}
        # Every access is type-guarded, because this is a checkpoint read and a reader
        # does not get to assume its own version wrote it. `pages` has held an integer
        # count in an older summary shape, and iterating that raised inside the node —
        # which the graph correctly reported as CONVERT failing, for a reason that had
        # nothing to do with conversion.
        raw_pages = site.get("pages") if isinstance(site, Mapping) else None
        crawled = [
            str(page["url"])
            for page in (raw_pages if isinstance(raw_pages, list) else [])
            if isinstance(page, Mapping) and page.get("url")
        ][:25]
        homepage = str((state["dna"] or {}).get("website") or "")

        args, cost = await _ask(
            deps,
            box=box,
            task=TaskClass.REPACK,
            role=(
                "You choose which page on the business's own website each post should "
                "send people to, and you write the ask that earns the click. You never "
                "invent a URL: you pick one of the pages listed, or the homepage."
            ),
            state=state,
            body=(
                f"Article title: {draft.get('title')}\n"
                f"Target keyword: {outline.get('target_keyword')}\n"
                f"Outline CTA: {outline.get('cta')}\n\n"
                f"Their homepage: {homepage}\n"
                f"Pages we crawled on their site:\n"
                + ("\n".join(f"- {url}" for url in crawled) or "- (only the homepage)")
                + f"\n\nChannels needing an ask: {', '.join(channels)}\n\n"
                f"Business evidence:\n{_evidence(state)}\n"
                f"{passages}{retry}\n\nCall record_distribution."
            ),
            tool=DISTRIBUTION_TOOL,
        )
        if not args:
            raise ValueError(
                "the model did not return a distribution plan; expected a "
                "record_distribution tool call. Without one the tracked links have "
                "nowhere to point, so the run has reach and no conversion path."
            )
        return _with_refusals(
            state,
            box,
            {
                "distribution": _distribution(args, homepage=homepage),
                "_cost": cost,
                **grounding.updates,
            },
        )

    async def validate(state: AgentState) -> dict[str, Any]:
        """Score the draft and check it for forbidden claims. No model, ever.

        Two deterministic verdicts, and they are different KINDS of verdict. The SEO
        score is a quality measure with a threshold: a weak page is publishable after
        a retry. The claim check is a compliance gate: a draft carrying a claim the
        business is not allowed to make cannot be published at any score, so the
        graph refuses to carry it to REVIEW (see `graph.run_graph`).

        An absent draft fails both rather than raising: the graph's retry path is the
        right response, and a crash here would lose the run.
        """
        box = _toolbox("VALIDATE", deps)
        draft = state.get("draft") or {}
        outline = state.get("outline") or {}
        html = str(draft.get("html") or "")
        title = str(draft.get("title") or "")
        meta = str(draft.get("meta_description") or "")

        # CONVERT writes; this node judges. Keeping both verdicts in one place is what
        # makes "the deterministic half of the pipeline" a place rather than a habit.
        # Title and meta are checked alongside the body. They are NOT folded into the
        # assembled HTML document for this: the meta description lives in an
        # attribute, and stripping markup would drop it, so a forbidden claim in the
        # meta description would have gone unnoticed on the one line Google shows.
        #
        # The per-channel ask joins the same check rather than getting its own verdict,
        # because "may this run be published" has one answer. The ask is the most
        # claim-dangerous line in the run -- it is the sentence somebody acts on -- and
        # the graph refuses to carry a failing verdict to REVIEW, so a banned claim in
        # the conversion copy cannot be approved either.
        claim_result = await box.call(
            CLAIMS_CHECK,
            ClaimCheckRequest(
                content="\n".join([title, meta, html, _cta_text(state)]),
                banned_claims=[str(c) for c in (state["dna"].get("banned_claims") or [])],
                contains_markup=True,
            ),
        )
        claim_check = claim_result.model_dump(mode="json")

        if not html:
            return _with_refusals(
                state,
                box,
                {
                    "claim_check": claim_check,
                    "seo_report": {
                        "score": 0,
                        "passed": False,
                        "findings": [],
                        "note": "No draft HTML was produced, so there was nothing to score.",
                    },
                },
            )

        # The engine scores a document, and GENERATE returns title and meta separately.
        document = (
            f"<html><head><title>{title}</title>"
            f'<meta name="description" content="{meta}">'
            f"</head><body>{html}</body></html>"
        )
        result = await box.call(
            SEO_SCORE,
            SeoScoreRequest(
                html=document,
                target_keyword=str(outline.get("target_keyword") or ""),
                secondary_keywords=list(outline.get("secondary_keywords") or []),
                locale=str(state["dna"].get("locale") or "de"),
            ),
        )
        return _with_refusals(
            state,
            box,
            {
                "seo_report": result.model_dump(mode="json"),
                "claim_check": claim_check,
            },
        )

    async def repack(state: AgentState) -> dict[str, Any]:
        """Render the approved message per channel, then enforce the limits in code."""
        box = _toolbox("REPACK", deps)
        draft = state.get("draft") or {}
        outline = state.get("outline") or {}
        wanted = ", ".join(channels_of(state))

        args, cost = await _ask(
            deps,
            box=box,
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
        banned = [str(c) for c in (state["dna"].get("banned_claims") or [])]
        renderings: dict[str, dict[str, Any]] = {}
        blocked: list[NodeError] = []
        for post in (args or {}).get("posts", []):
            channel = str(post.get("channel", "")).lower().strip()
            body = str(post.get("body", "")).strip()
            if not channel or not body:
                continue
            spec = CHANNEL_SPECS.get(canonical_channel(channel))
            limit = CHANNEL_LIMITS.get(canonical_channel(channel))
            if limit and len(body) > limit:
                # Trim on a word boundary. The platform would reject the post
                # outright, so shipping it over-length is not an option.
                body = body[: limit - 1].rsplit(" ", 1)[0] + "…"

            # Hashtags: the model's declared list AND whatever it wrote inline, both
            # brought inside the channel's range in code. The engine's docstring
            # explains why this is not the model's job -- a count is arithmetic, and a
            # negative count instruction is the one kind a model reliably disobeys.
            body, tags, removed, shortfall = await _bring_hashtags_in_range(
                box, spec, body, post.get("hashtags")
            )

            # Checked AFTER trimming, because the trimmed text is what would be
            # published. A rendering is separate content from the page: the page can
            # pass VALIDATE and a post derived from it can still carry a forbidden
            # claim, and there is no per-channel retry in the graph -- so the post is
            # DROPPED and the loss is named, rather than published or silently lost.
            verdict = await box.call(
                CLAIMS_CHECK,
                ClaimCheckRequest(content=body, banned_claims=banned, contains_markup=False),
            )
            if not verdict.passed:
                blocked.append(
                    NodeError(
                        node="REPACK",
                        code="banned_claim",
                        message=(
                            f"The {channel} post was withheld: it makes the forbidden "
                            f"claim(s) {', '.join(verdict.claims_found)}. "
                            "Nothing was published for that channel."
                        ),
                    )
                )
                continue
            renderings[channel] = {
                "body": body,
                "hashtags": list(tags),
                "hashtags_removed": removed,
                "hashtags_shortfall": shortfall,
                # Over the EDITORIAL target, which is not the same as over the
                # platform's limit and must not be treated as one: a 1,900-character
                # LinkedIn post publishes fine and is simply longer than it should be.
                # Reported so the review screen can say so; never truncated to it.
                "over_target": bool(spec and len(body) > spec.max_chars),
            }

        updates: dict[str, Any] = {"renderings": renderings, "_cost": cost}
        if blocked:
            updates["errors"] = [*state["errors"], *blocked]
        return _with_refusals(state, box, updates)

    async def review(state: AgentState) -> dict[str, Any]:
        """The interrupt point. Nothing to do: the graph pauses here for a human."""
        return {}

    async def export(state: AgentState) -> dict[str, Any]:
        """Publish what was approved — or record, precisely, why nothing went out.

        The ONLY node that may reach an actuator, which docs/AGENT_RUNTIME.md section 3
        calls "the second of three independent prompt-injection barriers". The barrier
        is the allowlist in `tools.py`: HARVEST and GENERATE handle attacker-controlled
        text and hold no actuator tool, so a crawled page that talks a model into
        asking for `publish` gets a recorded refusal in the node that read it, and the
        node that CAN publish never reads untrusted text at all.

        Two rules decide whether anything happens, and both fail CLOSED:

        * **No approval, no publication.** `state["approved_by"]` is the whole
          authority, and it is deliberately not inferred from "REVIEW is in `visited`":
          the checkpoint of an interrupted run is written BEFORE the run is parked, so
          a process that dies in that window leaves a resumable run with REVIEW behind
          it and no human decision anywhere. An approval is a fact somebody records.
          Nothing is even constructed without one -- an `Actuation` carrying a blank
          approver would be a request nobody made, and `actuate`'s own refusal path is
          the lower barrier for an approval revoked between the gate and the call, not
          a substitute for this one.
        * **A missing integration is REPORTED, never skipped.** With no actuator wired
          -- which is every deployment today -- this node says so, on the run, in the
          words a customer needs: the content exists, nothing was sent, and this is not
          a claim that a post went live.

        Per destination failure is per destination, exactly like HARVEST's per-source
        degradation: one dead platform costs that channel and is NAMED, and the other
        three still go out. A run that published three of four says which one it did
        not.
        """
        box = _toolbox("EXPORT", deps)
        approver = str(state.get("approved_by") or "").strip()
        pieces = _publishable(state)

        note = _export_refusal(approver=approver, pieces=pieces, box=box)
        if note is not None:
            code, message = note
            return _with_refusals(
                state,
                box,
                {
                    "published": {
                        "approved_by": approver or None,
                        "attempted": 0,
                        "refs": [],
                        "not_published": [target for _, target, _ in pieces],
                        "simulated": False,
                        "note": message,
                    },
                    # An error, not a `fact_gap`: `fact_gaps` says what the content was
                    # written WITHOUT, and this is about what happened to the finished
                    # content. The review screen already renders errors.
                    "errors": [
                        *state["errors"],
                        NodeError(node="EXPORT", code=code, message=message),
                    ],
                },
            )

        business = UUID(state["business_id"])
        # Read once, here, rather than inside `_actuate`: every action this node takes
        # belongs to the same run, and a helper that re-derived it per call is a helper
        # that can be given a state whose id disagrees with the page it just wrote.
        run = run_uuid(state)
        refs: list[dict[str, Any]] = []
        not_published: list[str] = []
        errors: list[NodeError] = []

        # The links first, and this ordering IS functional even though the page is gone:
        # every post carries the ask, and `link_service` completes each link's target
        # with its own code AFTER minting it. A post published before its link exists
        # carries no trackable URL, so the clicks it earns are attributable to nothing.
        distribution: dict[str, Any] = {}
        plan = state.get("distribution")
        if deps.publish_distribution is None:
            errors.append(
                NodeError(
                    node="EXPORT",
                    code="links_not_wired",
                    message=(
                        "No link minting is configured, so the posts below carry no "
                        "tracked link and their clicks cannot be attributed."
                    ),
                )
            )
        elif isinstance(plan, Mapping) and str(plan.get("destination_url") or "").strip():
            try:
                distribution = await deps.publish_distribution(
                    business_id=business,
                    run_id=run,
                    plan=dict(plan),
                    draft=dict(state.get("draft") or {}),
                    website=str((state["dna"] or {}).get("website") or ""),
                )
            except Exception as exc:
                # Broad on purpose, and the run continues: an off-site destination or a
                # dead database costs the ATTRIBUTION, not the content. The posts are
                # still worth publishing, and the owner is told which half failed.
                logger.warning("export: link minting failed: %s", exc)
                errors.append(
                    NodeError(
                        node="EXPORT",
                        code="links_failed",
                        message=(
                            f"The tracked links could not be created ({exc}). The posts "
                            "below went out without them, so their clicks are not "
                            "attributable."
                        ),
                    )
                )

        for action_type, target, payload in pieces:
            outcome = await _actuate(
                box,
                PUBLISH,
                business=business,
                run=run,
                approver=approver,
                action_type=action_type,
                target=target,
                payload=payload,
            )
            refs.append(_outcome_row(outcome))
            if outcome.succeeded:
                continue
            not_published.append(target)
            errors.append(
                NodeError(
                    node="EXPORT",
                    code="publish_refused"
                    if outcome.status is OutcomeStatus.REFUSED
                    else "publish_failed",
                    message=(
                        f"Nothing was published to {target}: {outcome.error or 'no reason given'}. "
                        "The other destinations were unaffected."
                    ),
                )
            )

        published = [row["target"] for row in refs if row["status"] == "succeeded"]
        # Carried all the way up, and into the one-line note as well as the flag: a
        # surface that renders only the sentence would otherwise say "Published 3 of 3"
        # about three posts that never left this process, and a report that cannot tell
        # a real post from a simulated one is worse than no report.
        simulated = any(row["fake"] for row in refs)
        report: dict[str, Any] = {
            "approved_by": approver,
            "attempted": len(pieces),
            "refs": refs,
            "not_published": not_published,
            "simulated": simulated,
            "note": (
                f"Published {len(published)} of {len(pieces)}"
                + (
                    f"; nothing was published to {', '.join(not_published)}"
                    if not_published
                    else ""
                )
                + (
                    " -- SIMULATED: at least one destination has no credential "
                    "configured, so nothing left this process for it"
                    if simulated
                    else ""
                )
            ),
        }
        report.update(
            await _notify_owner(
                box,
                business=business,
                run=run,
                approver=approver,
                identity=deps.owner_notice,
                published=published,
                not_published=not_published,
                note=str(report["note"]),
            )
        )

        # The links travel on the report, so the review screen can show the owner the
        # tracked URL per channel — the thing they paste. Absent rather than empty when
        # minting failed: an empty list reads as "no CTAs", and "we could not create
        # them" is a different fact the error above already states.
        if distribution:
            report["distribution"] = distribution

        updates: dict[str, Any] = {"published": report}
        if errors:
            updates["errors"] = [*state["errors"], *errors]
        return _with_refusals(state, box, updates)

    async def measure(state: AgentState) -> dict[str, Any]:
        """Report what the published work is doing, and NAME whatever nobody measured.

        Three honesty rules do all the work here, and each one exists because the
        obvious implementation would report a fabrication as a measurement:

        * **`no_answer` stays out of the share-of-voice denominator.** A model that
          would not answer is not a brand that was absent, so a probe whose answers
          were all unusable reports "no share to report" and not zero.
        * **A metric nobody measured is ABSENT, not zero.** There is no `movement: 0`
          for a piece published a minute ago and no `leads: 0` for a link nobody has
          clicked yet -- both would read as findings.
        * **`analytics.fetch` is granted and deliberately unwired**, so the GSC/GA4
          omission is named on every measurement rather than inferred from its absence.

        The share of voice is not re-probed here when HARVEST already measured it. A
        second probe minutes after publishing asks the same models the same prompts and
        would produce a "movement" number generated by sampling noise; the baseline is
        carried forward instead, and movement belongs to the next cycle. A probe DOES
        run when there is no baseline at all, because then it is establishing the
        series rather than pretending to compare against it.
        """
        box = _toolbox("MEASURE", deps)
        published = state.get("published") or {}
        refs = [
            row
            for row in (published.get("refs") or [])
            if isinstance(row, Mapping) and row.get("status") == "succeeded"
        ]
        channels = sorted({str(row.get("target")) for row in refs})

        # Named first, so it is present even on the path where nothing was published.
        gaps: list[str] = [ANALYTICS_GAP]
        report: dict[str, Any] = {
            "published_refs": len(refs),
            "channels": channels,
            # A simulated publish has no audience, so nothing downstream of it can be
            # measured -- and a metric collected against it would be measuring us.
            "simulated": bool(published.get("simulated")),
            "attribution": {
                "channels": channels,
                "leads_measured": False,
                "note": LEADS_NOT_YET_NOTE,
            },
        }
        if not refs:
            report["note"] = (
                "Nothing is live from this run, so there is nothing to measure. "
                + str(published.get("note") or "EXPORT has not published anything.")
            )

        share, gap = await _share_of_voice(box, state)
        if share is not None:
            report["share_of_voice"] = share
        if gap is not None:
            gaps.append(gap)

        report["gaps"] = gaps
        return _with_refusals(state, box, {"measurement": report})

    return {
        "INTAKE": intake,
        "HARVEST": harvest,
        "OPPORTUNITY": opportunity,
        "PLAN": plan,
        "GENERATE": generate,
        "CONVERT": convert,
        "VALIDATE": validate,
        "REPACK": repack,
        "REVIEW": review,
        "EXPORT": export,
        "MEASURE": measure,
    }


# --------------------------------------------------------------------------- #
# EXPORT
# --------------------------------------------------------------------------- #


def _publishable(state: AgentState) -> list[tuple[str, str, dict[str, Any]]]:
    """What REVIEW approved, as one actuation request per destination.

    **Ordering stopped mattering when we stopped hosting the page.** It used to: every
    post carried an ask pointing at a page we published in the same pass, so posting
    first put clicks on a page that was not there yet. The destination is now a page the
    business already serves, so there is nothing to publish before the posts.

    Both shapes of `renderings` are accepted: the current mapping with `body` and
    `hashtags`, and the plain string that checkpoints written before that change hold.
    A reader of a JSONB column does not get to assume its own version wrote it, and
    nothing migrates a display field.
    """
    pieces: list[tuple[str, str, dict[str, Any]]] = []

    for channel, rendering in (state.get("renderings") or {}).items():
        body, hashtags = _rendered_post(rendering)
        if not body:
            # Nothing to post. REPACK has already recorded WHY it is missing (a banned
            # claim, a malformed tool call), so an empty body here needs no second
            # error -- inventing one would report the same loss twice.
            continue
        pieces.append(
            (
                SOCIAL_POST_ACTION,
                canonical_channel(str(channel)),
                {"body": body, "hashtags": hashtags},
            )
        )
    return pieces


def _rendered_post(rendering: Any) -> tuple[str, list[str]]:
    """One channel's post as ``(body, hashtags)``, from either stored shape."""
    if isinstance(rendering, str):
        return rendering.strip(), []
    if isinstance(rendering, Mapping):
        tags = [str(tag) for tag in (rendering.get("hashtags") or []) if str(tag).strip()]
        return str(rendering.get("body") or "").strip(), tags
    return "", []


def _export_refusal(
    *,
    approver: str,
    pieces: Sequence[tuple[str, str, dict[str, Any]]],
    box: NodeToolbox,
) -> tuple[str, str] | None:
    """Why EXPORT will publish nothing, as ``(code, message)``, or None to proceed.

    Ordered deliberately: **approval is checked before capability.** A run nobody
    approved must read as unapproved even on a deployment that also has no publisher,
    because those two get fixed by completely different people.

    The last two are the same distinction one layer down. `allows` is the grant minus
    what an operator revoked and `available` adds "and it is wired", so a revoked
    publisher and an absent one are told apart rather than both reported as "not
    configured" -- which would have made the kill switch indistinguishable from a
    deployment that never had a publisher.
    """
    if not approver:
        return "not_approved", NOT_APPROVED_NOTE
    if not pieces:
        return "nothing_to_publish", NOTHING_TO_PUBLISH_NOTE
    if not box.allows(PUBLISH):
        return "publish_revoked", PUBLISH_REVOKED_NOTE
    if not box.available(PUBLISH):
        return "actuator_unwired", NO_ACTUATOR_NOTE
    return None


async def _actuate(
    box: NodeToolbox,
    tool: str,
    *,
    business: UUID,
    run: UUID | None,
    approver: str,
    action_type: str,
    target: str,
    payload: Mapping[str, Any],
) -> ActuationOutcome:
    """One side effect, through the node's allowlist. Never raises except a refusal.

    `actuate()` already returns an `Outcome` for every failure mode it knows about, so
    what this catches is the layer below it: an injected publisher whose store cannot
    reach the database, or an actuator that raised something `ActuatorError` does not
    cover. One dead destination has to cost that destination and nothing else, which is
    the same rule HARVEST applies to a dead fact source.

    An allowlist refusal is re-raised, exactly as everywhere else in this module: that
    is a wiring fault or an attack, and it must be loud rather than degraded into a
    channel that quietly did not publish.

    `run` is what attributes the effect to its cause. It reaches `actions.run_id`
    through the ledger and `content_pieces.run_id` through the landing actuator, and
    without it a published page cannot be joined back to the run that made it -- so a
    lead count per run is unanswerable however good the click tracking is. It is
    explicitly `UUID | None` rather than defaulted: a caller that forgot it would
    silently go back to writing NULLs, which is the state this parameter exists to
    leave, and a node driven by a test genuinely has no run.
    """
    return await _perform(
        box,
        tool,
        Actuation(
            business_id=business,
            run_id=run,
            action_type=action_type,
            target=target,
            payload=dict(payload),
            approved_by=approver,
        ),
    )


async def _perform(box: NodeToolbox, tool: str, actuation: Actuation) -> ActuationOutcome:
    """The half of `_actuate` that does not build the actuation.

    Split out for one caller that must NOT build its own: an owner notice is built by
    `build_owner_notice_actuation`, because that helper is what derives the target handle
    and stamps the recipient provenance. A node assembling that `Actuation` by hand is
    exactly how an address ended up in `target` in the first place.
    """
    action_type, target = actuation.action_type, actuation.target
    try:
        outcome = await box.call(tool, actuation)
    except ToolNotAllowedError:
        raise
    except Exception as exc:  # broad on purpose: one dead destination, not the run
        logger.warning("export: %s to %s raised: %s", action_type, target, exc)
        return ActuationOutcome(
            status=OutcomeStatus.FAILED,
            action_type=action_type,
            target=target,
            error=f"{type(exc).__name__}: {exc}",
        )

    if isinstance(outcome, ActuationOutcome):
        return outcome
    # A publisher that answered with something other than an outcome. Reported as a
    # failure rather than assumed successful: "we do not know what happened" and "it
    # went out" must never collapse into the same row.
    return ActuationOutcome(
        status=OutcomeStatus.FAILED,
        action_type=action_type,
        target=target,
        error=f"the publisher returned {type(outcome).__name__}, not an Outcome",
    )


def _outcome_row(outcome: ActuationOutcome) -> dict[str, Any]:
    """One outcome as JSON primitives, for the checkpoint and the review screen.

    `at` becomes a string because the checkpoint is a JSONB column and a `datetime`
    cannot serialise into one -- a state that cannot serialise cannot resume.
    """
    return {
        "action_type": outcome.action_type,
        "target": outcome.target,
        "status": str(outcome.status),
        "external_ref": outcome.external_ref,
        "replayed": outcome.replayed,
        "fake": outcome.fake,
        "error": outcome.error,
        "summary": outcome.summary(),
        "at": outcome.at.isoformat(),
        # The actuator's own structured result. Carried rather than dropped because it is
        # the ONLY path by which a real published address reaches a surface: the export
        # pack is a pure projection of this checkpoint, so a tracked short link the
        # landing actuator actually minted is either in here or it is nowhere. Already
        # JSON primitives by the `Actuation.payload` rule -- an audit row that cannot
        # serialise is an audit row that ends the run.
        "detail": dict(outcome.detail),
    }


#: Why nobody was told, per reason. Two causes, and they are fixed by different people:
#: one is a deployment that has not been given a sending identity, the other is an account
#: whose owner cannot be resolved.
NO_NOTICE_IDENTITY_NOTE: Final = (
    "Nobody was told: this deployment cannot send an owner notice. It needs both the "
    "account holder's address, resolved from the authenticated account, and a sending "
    "identity in OWNER_NOTICE_FROM. The published work is unaffected -- this is the notice "
    "about it, not the work. Deliberately NOT read from the business profile's contact "
    "address: that one comes from a crawled homepage, and a page we crawled must never be "
    "able to redirect our own operational mail."
)
NO_NOTIFIER_NOTE: Final = (
    "Nobody was told: no notification integration is configured on this deployment."
)

#: Why an owner notice carries no unsubscribe link, said in the message itself.
#:
#: In the body rather than only in a docstring because the recipient is the person who
#: would otherwise wonder. `notify.owner` is transactional -- it reports what their own run
#: did -- so an unsubscribe link would offer to switch off the product, and the actuator
#: refuses one by name.
NOTICE_FOOTER: Final = (
    "You are getting this because this run belongs to your account. It is an operational "
    "notice about work you asked for, not marketing, so there is nothing to unsubscribe "
    "from -- switching it off would mean a run publishing and nobody being told."
)


async def _notify_owner(
    box: NodeToolbox,
    *,
    business: UUID,
    run: UUID | None,
    approver: str,
    identity: OwnerNoticeIdentity | None,
    published: Sequence[str],
    not_published: Sequence[str],
    note: str,
) -> dict[str, Any]:
    """Tell the owner what went live and what did not. One message, never per channel.

    Sent even when everything failed -- especially then. "Nothing was published, and here
    is which platform refused it" is the message with the most value in it, and a notifier
    that only fires on success is how a silent failure stays silent.

    Three things about the message are load-bearing rather than incidental:

    * **The recipient is the AUTHENTICATED account holder**, injected as `identity`. It
      used to be `state["dna"]["email"]` -- a contact address scraped off the business's own
      homepage -- so a crawled page could have redirected our operational mail. The node
      never sees the database, and it never sees `dna` here either.
    * **`build_owner_notice_actuation` builds the actuation**, so `target` is a derived
      handle and not the address. The address used to travel in `target`, which
      `actuate()` logs, `Outcome.summary()` renders and `_outcome_row` copies into
      `runs.checkpoint` -- three routes to the Delivery tab and a log file at once.
    * **`note` opens the body**, and it is the same sentence the run reports. It already
      carries the SIMULATED caveat, so the owner cannot be told a post went live when
      nothing left the process -- and there is one sentence to get right instead of two.
    """
    if identity is None:
        return {"notified": False, "notify_note": NO_NOTICE_IDENTITY_NOTE}
    if not box.available(NOTIFY):
        return {"notified": False, "notify_note": NO_NOTIFIER_NOTE}

    total = len(published) + len(not_published)
    outcome = await _perform(
        box,
        NOTIFY,
        build_owner_notice_actuation(
            business_id=business,
            run_id=run,
            identity=identity,
            subject=f"Published {len(published)} of {total}",
            approved_by=approver,
            text=_notice_body(note, published, not_published),
        ),
    )
    return {"notified": outcome.succeeded, "notify": _outcome_row(outcome)}


def _notice_body(note: str, published: Sequence[str], not_published: Sequence[str]) -> str:
    """The notice, in plain text.

    Plain text and no HTML: this is a four-line operational message, and a second body part
    is a second place for the truth to drift. Built entirely from the run's own outcome --
    no crawled site text reaches it, so nothing a page we fetched said can end up in a
    message our sending domain is answering for.
    """
    lines = [note, ""]
    if published:
        lines += ["Live:", *(f"  - {target}" for target in published), ""]
    if not_published:
        lines += ["Not published:", *(f"  - {target}" for target in not_published), ""]
    lines.append(NOTICE_FOOTER)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# MEASURE
# --------------------------------------------------------------------------- #


async def _share_of_voice(
    box: NodeToolbox, state: AgentState
) -> tuple[dict[str, Any] | None, str | None]:
    """The AI-visibility figure to report, and the gap to name — never both.

    Three cases, and the first is the common one:

    * HARVEST measured it: carry the baseline forward and say plainly that movement is
      the next cycle's to report. Re-probing here would ask the same models the same
      prompts minutes later and call the difference "movement".
    * HARVEST could not: probe now, because establishing the baseline is real work and
      the grant exists for it.
    * No probe configured, or the probe fails: name the gap and report NO figure. The
      series is left alone -- "provider down → skip the cycle, never corrupt the
      series" (docs/AGENT_RUNTIME.md section 3).
    """
    baseline = (state.get("facts") or {}).get("visibility")
    if isinstance(baseline, Mapping):
        return {
            "source": "harvest",
            "baseline": _sov_view(baseline),
            "movement_note": (
                "Movement is not reported for this run: the baseline above was measured "
                "before publishing, and a second probe taken minutes later would "
                "measure sampling noise. The comparison belongs to the next cycle."
            ),
        }, None

    if not box.available(GEO_PROBE):
        return None, (
            "AI answer-engine visibility (no probe configured, so there is no baseline "
            "to measure movement against)"
        )

    try:
        probe = await box.call(GEO_PROBE, state["dna"])
    except ToolNotAllowedError:
        raise
    except Exception as exc:  # broad on purpose: a dead provider skips the cycle
        logger.warning("measure: visibility probe failed: %s", exc)
        return None, (
            "AI answer-engine visibility (the probe failed, so this cycle is skipped "
            f"rather than recorded as an absence: {exc})"
        )

    if not isinstance(probe, Mapping):
        return None, "AI answer-engine visibility (the probe returned nothing usable)"
    return {
        "source": "measure",
        "baseline": _sov_view(probe),
        "movement_note": (
            "This run took the FIRST measurement for this business, so there is nothing "
            "to compare it against yet."
        ),
    }, None


def _sov_view(probe: Mapping[str, Any]) -> dict[str, Any]:
    """A probe summary as a share of voice, or an explicit "no share to report".

    `usable_answers` is the denominator the geo service already computed with
    `no_answer` excluded, and this reads it rather than dividing anything itself. When
    it is zero there is no share -- reporting 0% would record a model outage as the
    brand being absent from answers nobody got, which is the difference between a
    measurement and a fabrication.
    """
    usable = int(probe.get("usable_answers") or 0)
    view: dict[str, Any] = {
        "usable_answers": usable,
        "no_answer_count": int(probe.get("no_answer_count") or 0),
        # Travels with every number, because a share measured against a deterministic
        # fake provider is not a measurement of anything.
        "using_fake_provider": bool(probe.get("using_fake_provider")),
        "caveats": [str(caveat) for caveat in (probe.get("caveats") or [])],
    }
    if usable <= 0:
        view["measured"] = False
        view["note"] = (
            "No share of voice is reported: every probe came back no_answer, and "
            "no_answer is excluded from the denominator. A model outage recorded as "
            "brand absence would be a fabrication, not a measurement."
        )
        return view

    view["measured"] = True
    # The engine's own rendering, which physically cannot print a share without its
    # denominator -- so nothing downstream can quote "22%" off nine samples.
    view["headline"] = str(probe.get("headline") or "")
    view["mention_share_pct"] = probe.get("mention_share_pct")
    view["unprompted_mention_share_pct"] = probe.get("unprompted_mention_share_pct")
    return view


async def _bring_hashtags_in_range(
    box: NodeToolbox,
    spec: ChannelSpec | None,
    body: str,
    declared: Any,
) -> tuple[str, list[str], int, int]:
    """Enforce the channel's hashtag range, and return the tags that survived.

    Two sources have to be reconciled, because the model uses both: hashtags written
    INLINE in the post, and the separate ``hashtags`` array `REPACK_TOOL` asks for.
    The body is the published artifact, so `channel.validate` runs on that; the
    declared array then tops the list up to the cap, in the order the model gave, and
    only with tags the body does not already carry.

    A shortfall is reported and never filled. Inventing ``#Zahnarzt`` here would be a
    node writing marketing copy from a counter, and the engine refuses to do it for
    exactly that reason -- so a channel wanting three tags and given one says so.

    A channel with no spec (a run configured for something the spec table does not
    cover) is left alone rather than defaulted to zero: silently stripping every
    hashtag off a post because we have no numbers for it would be enforcement by
    accident.
    """
    if spec is None:
        return body, _clean_tags(declared), 0, 0

    if box.available(CHANNEL_VALIDATE):
        result = await box.call(
            CHANNEL_VALIDATE, body, minimum=spec.hashtags_min, maximum=spec.hashtags_max
        )
        body = result.text
        inline = list(result.kept)
        removed = result.removed
    else:
        # Refused or unwired: the counters must then say nothing was enforced rather
        # than reporting a clean zero that looks like a compliant post.
        inline = []
        removed = 0

    tags = list(inline)
    for tag in _clean_tags(declared):
        if len(tags) >= spec.hashtags_max:
            break
        if tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)

    return body, tags, removed, max(0, spec.hashtags_min - len(tags))


def _clean_tags(declared: Any) -> list[str]:
    """The model's ``hashtags`` array, de-duplicated and prefixed, order preserved."""
    if not isinstance(declared, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in declared:
        text = str(item).strip().lstrip("#").strip()
        if not text:
            continue
        tag = f"#{text}"
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out


class _Grounding(NamedTuple):
    """What one retrieval gave a node: prompt text, checkpoint evidence, and status.

    Three values rather than one because they have three different audiences and only
    one of them used to leave the process. `passages` is for the model and carries the
    chunk BODIES; `updates` is for the checkpoint and carries none of them; `trace` is
    the raw object, for the one caller (HARVEST) that summarises it differently.
    `failed` is separate from ``trace is None`` because "there is no knowledge base"
    and "the knowledge base could not be read" are opposite facts and only the second
    one belongs in `fact_gaps`.
    """

    passages: str
    #: ``{}`` or ``{"retrieval_traces": [...]}``. Spread into the node's own updates,
    #: so it merges identically under both graph drivers -- each replaces the channel
    #: with the value the node returned, and the value is the whole appended list.
    updates: dict[str, Any]
    trace: Any | None
    failed: bool


def _no_grounding(*, failed: bool = False) -> _Grounding:
    return _Grounding(passages="", updates={}, trace=None, failed=failed)


async def _retrieved(box: NodeToolbox, question: str, node: str, state: AgentState) -> _Grounding:
    """Passages from the business's own documents, fenced and labelled — or nothing.

    The same shape CONVERT has used since it shipped, lifted out so OPPORTUNITY, PLAN
    and GENERATE can use it too. Those three held `kb.search` in their allowlist and
    never called it, which is the difference between a documented capability and a
    working one.

    A missing knowledge base is a NORMAL state and returns empty grounding: the caller
    then writes from the DNA and the harvested facts, which is what a business with no
    uploaded documents legitimately has. A retrieval that raises is logged and also
    returns empty — grounding is an enhancement to a prompt, and losing it must not
    cost the run the work every other source produced. An allowlist refusal is
    re-raised, because that is a wiring fault or an attack and must be loud.

    **It also carries the trace out of the process**, which is the whole reason this
    returns a record rather than a string. The rewritten queries, the per-chunk grades
    and the fallback decision were being computed and then dropped at this call site,
    so the one artifact that shows the retrieval was agentic existed only in a log
    line. It goes into the checkpoint now, compacted by :func:`summarise_retrieval`.
    """
    if not box.available(KB_SEARCH):
        return _no_grounding()
    try:
        trace = await box.call(KB_SEARCH, question)
    except ToolNotAllowedError:
        raise
    except Exception as exc:  # broad on purpose: grounding is an enhancement
        logger.warning("%s: retrieval failed: %s", node.lower(), exc)
        return _no_grounding(failed=True)
    return _Grounding(
        passages=_passages(trace),
        updates={
            "retrieval_traces": append_retrieval_trace(state, summarise_retrieval(trace, node=node))
        },
        trace=trace,
        failed=False,
    )


def append_retrieval_trace(state: AgentState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The run's traces with ``entry`` on the end, capped, and numbered.

    Returns the LIST rather than a new state, because a node returns update dicts and
    both drivers merge a returned channel by REPLACEMENT. So the node has to hand back
    the whole list; handing back only the new entry would mean each node's evidence
    erased the last one's, which is precisely the invisible version of the bug this
    key exists to fix.

    `seq` is a 1-based ordinal over the run's retrieval calls and it survives the cap.
    A panel that starts at ``seq: 4`` has said, without a flag or a note, that three
    earlier calls were dropped — and a cap that trims evidence silently is the kind of
    honesty failure this repo keeps paying to avoid.
    """
    existing = [entry for entry in (state.get("retrieval_traces") or []) if isinstance(entry, dict)]
    last = existing[-1].get("seq") if existing else 0
    previous = last if isinstance(last, int) and not isinstance(last, bool) and last > 0 else 0
    return [*existing, {**entry, "seq": max(previous, len(existing)) + 1}][-MAX_RETRIEVAL_TRACES:]


def summarise_retrieval(trace: Any, *, node: str) -> dict[str, Any]:
    """Compact a `RetrievalTrace` into something safe to carry in state.

    Lossy ON PURPOSE, and — as in `run_executor.summarise_crawl`, whose precedent this
    follows — the losses are chosen rather than incidental.

    **What survives is the argument that the retrieval was agentic**: the rewritten
    query and the rationale for it, every chunk id with its grade and the grader's
    reason, the per-attempt decision, and the final outcome with its reason. Those are
    what let a reviewer answer "is this claim grounded, and how do we know" from the
    checkpoint alone, which is what the trace was always for.

    **What is dropped is every chunk BODY**: `ChunkGrade.excerpt` and
    `GroundingChunk.content` are never read here. A chunk id plus a grade is auditable
    — the text is one indexed lookup away in `document_chunks`, and it is the same text
    the model was actually shown. Copying it into a JSONB column that is rewritten on
    every node would put the business's knowledge base in the checkpoint eleven times a
    run, and then send it to a model.

    Duck-typed over ``getattr`` for the same reason `summarise_crawl` is: `retrieve` is
    an injected dependency, the tests pass doubles, and a summariser that only works
    against the one real class cannot be exercised without the real one.
    """
    attempts_raw = list(getattr(trace, "attempts", []) or [])
    attempts: list[dict[str, Any]] = []
    for attempt in attempts_raw[:MAX_RETRIEVAL_ATTEMPTS_KEPT]:
        grades_raw = list(getattr(attempt, "grades", []) or [])
        attempts.append(
            {
                "attempt": _as_int(getattr(attempt, "attempt", 0)),
                # The rewrite, not the node's own words. This is the field the whole
                # "agentic" claim rests on, and it was the first one being discarded.
                "query": str(getattr(attempt, "query", "") or ""),
                "query_rationale": str(getattr(attempt, "query_rationale", "") or ""),
                "decision": str(getattr(attempt, "decision", "") or ""),
                "decision_reason": str(getattr(attempt, "decision_reason", "") or ""),
                "relevant": _as_int(getattr(attempt, "relevant", 0)),
                "partial": _as_int(getattr(attempt, "partial", 0)),
                "irrelevant": _as_int(getattr(attempt, "irrelevant", 0)),
                "grades": [
                    {
                        "chunk_id": str(getattr(grade, "chunk_id", "") or ""),
                        "document_id": str(getattr(grade, "document_id", "") or ""),
                        "ordinal": _as_int(getattr(grade, "ordinal", 0)),
                        "grade": str(getattr(grade, "grade", "") or ""),
                        "reason": str(getattr(grade, "reason", "") or "")[:RETRIEVAL_REASON_CHARS],
                        "distance": _as_float(getattr(grade, "distance", None)),
                        # `excerpt` is deliberately absent. See the docstring.
                    }
                    for grade in grades_raw[:MAX_RETRIEVAL_GRADES_KEPT]
                ],
                # So a reader of the checkpoint can tell "six chunks were graded" from
                # "six is all we kept" -- the same reason `summarise_crawl` carries
                # `excerpt_truncated` beside its excerpt.
                "grades_total": len(grades_raw),
                "notes": [str(note) for note in (getattr(attempt, "notes", []) or [])],
            }
        )

    return {
        # The graph node that asked, so the panel can be read per node. The trace does
        # not know this -- `kb_service` is called the same way from five places.
        "node": node,
        "question": str(getattr(trace, "question", "") or ""),
        "needed": bool(getattr(trace, "needed", False)),
        "need_reason": str(getattr(trace, "need_reason", "") or ""),
        # The fallback decision, which is the other half of the evidence: `sufficient`,
        # `fallback_to_web` or `not_needed`, with the reason the loop gave for it.
        "outcome": str(getattr(trace, "outcome", "") or ""),
        "outcome_reason": str(getattr(trace, "outcome_reason", "") or ""),
        "prompt_version": str(getattr(trace, "prompt_version", "") or ""),
        "attempts": attempts,
        "attempts_total": len(attempts_raw),
        "grounding_chunk_ids": [
            str(chunk_id) for chunk_id in (getattr(trace, "grounding_chunk_ids", []) or [])
        ],
        "chunk_count": len(list(getattr(trace, "chunks", []) or [])),
        "model_calls": _as_int(getattr(trace, "model_calls", 0)),
        # A string, like every other money value that crosses a JSON boundary here.
        "cost_usd": str(getattr(trace, "cost_usd", "0") or "0"),
        "notes": [str(note) for note in (getattr(trace, "notes", []) or [])],
    }


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _audit_own_nap(dna: Mapping[str, Any], site: Any) -> dict[str, Any] | None:
    """Diff the business's own published NAP against the profile it confirmed.

    The `nap.audit` implementation, and the reason HARVEST's grant is no longer empty.
    Returns ``None`` when the site published no readable address, because an audit of
    zero listings scores 100 -- which would tell a business its address is consistent
    everywhere it is not listed.

    The canonical record comes from the Business DNA the owner confirmed at
    onboarding, which is the right authority: it is the one version of the address a
    human has looked at and approved. Everything else -- the JSON-LD, the Impressum --
    is a copy that may have drifted from it.
    """
    raw = site.get("nap_sources") if isinstance(site, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return None
    # Extracted by `run_executor.summarise_crawl`, which is the last place the full
    # page facts exist: the summary carried in state drops the JSON-LD blocks and keeps
    # only an excerpt of each body, so extracting here would find nothing. The node
    # only ever DIFFS -- which is the engine boundary working as intended.
    listings = [DirectoryListing.model_validate(entry) for entry in raw]

    canonical = _canonical_nap(dna)
    result = audit_nap(canonical, listings)
    return {
        "consistency_score": result.consistency_score,
        "sources_checked": result.sources_checked,
        "sources": sorted({listing.source for listing in listings}),
        "findings": [finding.model_dump(mode="json") for finding in result.findings],
        # Stated in the payload, not only in a docstring, because this number will be
        # read as "your address is consistent online" if nothing says otherwise.
        "scope": (
            "Compares the address published on your own website (structured data and "
            "Impressum) against your confirmed profile. It does not check external "
            "directories."
        ),
    }


def _canonical_nap(dna: Mapping[str, Any]) -> CanonicalNap:
    """The confirmed profile as the one true NAP record.

    `normalise_nap` produces both the display and the comparison forms, so the audit
    can be brutal about equivalence while the advice stays correct German -- which is
    the distinction the `nap` engine's contract exists to preserve.
    """
    return normalise_nap(
        RawNap(
            legal_name=_optional_str(dna.get("legal_name")) or _optional_str(dna.get("name")),
            trading_name=_optional_str(dna.get("name")),
            street=_optional_str(dna.get("street")),
            house_number=_optional_str(dna.get("house_number")),
            postcode=_optional_str(dna.get("postcode")),
            city=_optional_str(dna.get("city")),
            phone=_optional_str(dna.get("phone")),
            email=_optional_str(dna.get("email")),
        )
    )


def _optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _distribution(args: Mapping[str, Any], *, homepage: str) -> dict[str, Any]:
    """CONVERT's tool call as the state key, normalised.

    Kept to primitives because this is checkpointed to a JSONB column. The destination
    falls back to the homepage when the model returned nothing usable — a run with a
    plan and no target has reach and no conversion path, and the homepage is the one
    page every business has. It is NOT validated here: the origin check belongs at
    publish time (`landing_service.publish_distribution`), where a retry cannot talk
    past it.
    """
    raw_ctas = args.get("ctas")
    ctas: list[dict[str, str]] = []
    for entry in raw_ctas if isinstance(raw_ctas, list) else []:
        if not isinstance(entry, Mapping):
            continue
        channel = canonical_channel(str(entry.get("channel") or "").strip())
        text = str(entry.get("text") or "").strip()
        if channel and text:
            ctas.append({"channel": channel, "text": text})
    destination = str(args.get("destination_url") or "").strip() or homepage
    return {"destination_url": destination, "ctas": ctas}


def _cta_text(state: AgentState) -> str:
    """Every ask, as one block for the claim gate.

    Joined rather than checked per-CTA because "may this run be published" has one
    answer: a forbidden claim in the LinkedIn ask blocks the run, not just LinkedIn.
    """
    distribution = state.get("distribution") or {}
    ctas = distribution.get("ctas") if isinstance(distribution, Mapping) else None
    return "\n".join(
        str(cta.get("text") or "")
        for cta in (ctas if isinstance(ctas, list) else [])
        if isinstance(cta, Mapping)
    )


def _cta_channels(channels: tuple[str, ...]) -> tuple[str, ...]:
    """The channels CONVERT writes an ask for: the run's own, plus the bio-link hub.

    Order-preserving de-duplication, so a run already rendering for ``link_hub`` does
    not get two CTAs for it -- which the deterministic check would (correctly) refuse
    as splitting one channel's clicks across two links.
    """
    return tuple(dict.fromkeys([*channels, HUB_CHANNEL]))


def _proof_question(state: AgentState) -> str:
    """What to ask the knowledge base for, when looking for proof rather than prose.

    Deliberately not the run's goal: the goal is "more local leads", and retrieving
    against it returns whatever is most on-topic. Proof points need evidence about the
    business's track record on the thing being offered, so the question is built from
    the offer's own subject.
    """
    outline = state.get("outline") or {}
    keyword = str(outline.get("target_keyword") or "").strip()
    name = str(state["dna"].get("name") or "").strip()
    subject = keyword or str((state.get("draft") or {}).get("title") or "").strip()
    return f"Belege, Referenzen und Zahlen zu {subject} bei {name}".strip()


def _passages(trace: Any) -> str:
    """Retrieved passages, fenced as untrusted, each labelled so it can be cited.

    The label is the document id and the passage ordinal, which is what the retrieval
    trace actually knows. The model is asked for a human-readable source, so the label
    is a fallback rather than the expected output -- and the engine can only enforce
    that a source is NAMED, never that it is true. That limit is stated in the
    landing contract and is not papered over here.
    """
    chunks = list(getattr(trace, "chunks", []) or [])
    if not chunks:
        return ""
    lines = [
        f"[{getattr(chunk, 'document_id', '?')}#{getattr(chunk, 'ordinal', 0)}] "
        f"{str(getattr(chunk, 'content', ''))[:600]}"
        for chunk in chunks[:6]
    ]
    return "\nOwn documents (cite these as sources):\n" + fence("\n\n".join(lines))


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


__all__ = [
    "ANALYTICS_GAP",
    "CHANNEL_LIMITS",
    "DEFAULT_CHANNELS",
    "NOTHING_TO_PUBLISH_NOTE",
    "NOTICE_FOOTER",
    "NOTIFY_ACTION",
    "NOT_APPROVED_NOTE",
    "NO_ACTUATOR_NOTE",
    "NO_NOTICE_IDENTITY_NOTE",
    "NO_NOTIFIER_NOTE",
    "PAGE_PUBLISH_ACTION",
    "PUBLISH_REVOKED_NOTE",
    "SOCIAL_POST_ACTION",
    "Node",
    "NodeDeps",
    "OwnerNoticeIdentity",
    "build_nodes",
]
