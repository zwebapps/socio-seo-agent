"""The per-node tool allowlist, and the gate that enforces it.

`docs/AGENT_RUNTIME.md` section 3 tabulates which tools each node may call, and
until now that table was documentation: nothing in the runtime read it, so a node
could quietly acquire a capability the design says it must not have, and the doc
and the code could drift apart without any test noticing.

:data:`NODE_TOOLS` is now the single source of truth. Two tests hold it there:
one asserts it matches the doc table (parsed out of the markdown), the other
asserts every name in it is a tool the registry actually knows.

**Why an allowlist is a security control and not tidiness.** The runtime ingests
attacker-controllable text -- crawled competitor pages, retrieved document
chunks, SERP snippets -- and hands it to a model. Section 3 already claims that
"only EXPORT can reach an actuator ... which is the second of three independent
prompt-injection barriers". A claim like that is worth exactly as much as its
enforcement, so the barrier lives here, in code, where a page that talks the
model into asking for ``publish`` gets a refusal rather than a publication.

**Two mechanisms, because there are two different threats.**

* :meth:`NodeToolbox.call` is what *our code* goes through to reach an engine. A
  node asking for a tool outside its allowlist is a programming error -- a
  capability nobody granted -- so it raises :class:`ToolNotAllowedError` and the
  graph turns that into a recorded node failure. Failing loudly is right: the
  alternative is a node that silently grew a new reach.
* :meth:`NodeToolbox.accept` is what a *model's* tool call goes through. Here a
  refusal must NOT end the run: the request may have been induced by untrusted
  text, and the correct response is to ignore it, record it, and carry on with
  the legitimate output. So the call is dropped and a :class:`ToolRefusal` is
  recorded, which the node converts into a :class:`NodeError` on the run state.

Either way the refusal is observable -- logged with the node, the tool and the
allowlist, and surfaced in ``AgentState["errors"]`` where the timeline and the
review screen already render it. A silent refusal would be indistinguishable
from a tool that simply returned nothing.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.app.agents.state import NodeError
from backend.app.llm import ToolSpec

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The tool vocabulary
# --------------------------------------------------------------------------- #

# Engine and side-effect tools. Dotted, engine-first, so the name says which
# module owns the capability rather than what a node happens to call it.
CRAWL_SITE: Final = "crawl.site"
SERP_SEARCH: Final = "serp.search"
KB_SEARCH: Final = "kb.search"
KB_VERIFY: Final = "kb.verify"
GEO_PROBE: Final = "geo.probe"
NAP_AUDIT: Final = "nap.audit"
SEO_SCORE: Final = "seo.score"
CLAIMS_CHECK: Final = "claims.check"
LANDING_CHECK: Final = "landing.check"
CHANNEL_VALIDATE: Final = "channel.validate"
MEMORY_LOAD: Final = "memory.load"
WEB_SEARCH: Final = "web_search"
ANALYTICS_FETCH: Final = "analytics.fetch"
PUBLISH: Final = "publish"
NOTIFY: Final = "notify"

# Model-facing structured-output tools. They are tool calls in the API sense, so
# they belong in the same allowlist: a model asked for `record_page` in a node
# that does not write pages is as wrong as one asking for `publish`.
RECORD_OPPORTUNITIES: Final = "record_opportunities"
RECORD_OUTLINE: Final = "record_outline"
RECORD_PAGE: Final = "record_page"
RECORD_LANDING_PAGE: Final = "record_landing_page"
RECORD_POSTS: Final = "record_posts"

#: Every tool name the runtime recognises. An allowlist entry outside this set is
#: a typo, and a typo in an allowlist fails open unless something checks -- so a
#: test checks.
KNOWN_TOOLS: Final[frozenset[str]] = frozenset(
    {
        CRAWL_SITE,
        SERP_SEARCH,
        KB_SEARCH,
        KB_VERIFY,
        GEO_PROBE,
        NAP_AUDIT,
        SEO_SCORE,
        CLAIMS_CHECK,
        LANDING_CHECK,
        CHANNEL_VALIDATE,
        MEMORY_LOAD,
        WEB_SEARCH,
        ANALYTICS_FETCH,
        PUBLISH,
        NOTIFY,
        RECORD_OPPORTUNITIES,
        RECORD_OUTLINE,
        RECORD_PAGE,
        RECORD_LANDING_PAGE,
        RECORD_POSTS,
    }
)

#: Tools that reach the outside world through an actuator. No node except EXPORT
#: may hold one, and the injection test asserts exactly that.
ACTUATOR_TOOLS: Final[frozenset[str]] = frozenset({PUBLISH, NOTIFY})

#: The allowlist. Keys are every node in the documented machine, including the
#: two (EXPORT, MEASURE) that are specified but not yet built -- their grants are
#: recorded now so the barrier is already in place when they are wired, rather
#: than being invented by whoever writes them.
NODE_TOOLS: Final[Mapping[str, frozenset[str]]] = {
    # No model call and no evidence gathering. It reads business memory, which is
    # our own data, not harvested text.
    "INTAKE": frozenset({MEMORY_LOAD}),
    # The widest grant in the graph, and the node with no model in it. Every
    # source here returns attacker-influenced text, which is precisely why it
    # cannot reach an actuator or an output tool.
    "HARVEST": frozenset({CRAWL_SITE, SERP_SEARCH, KB_SEARCH, GEO_PROBE, NAP_AUDIT}),
    "OPPORTUNITY": frozenset({KB_SEARCH, RECORD_OPPORTUNITIES}),
    "PLAN": frozenset({KB_SEARCH, RECORD_OUTLINE}),
    "GENERATE": frozenset({KB_SEARCH, WEB_SEARCH, RECORD_PAGE}),
    # The conversion surface: a landing page and the per-channel ask that points at
    # it. `kb.search` because a proof point that is not in the business's own
    # material is an invented claim, so the node needs to read that material; no
    # `web_search`, because a proof point sourced from a page the business does not
    # control is not proof of anything about the business.
    "CONVERT": frozenset({KB_SEARCH, RECORD_LANDING_PAGE}),
    # Deterministic verdicts only. No model, so no output tool. `landing.check` is
    # the conversion audit -- "is there a form, and can it be answered" is set
    # membership, which is arithmetic, not judgement.
    "VALIDATE": frozenset({SEO_SCORE, CLAIMS_CHECK, LANDING_CHECK, KB_VERIFY}),
    # `claims.check` as well as the output tool: a social rendering is separate
    # content, so a forbidden claim can appear in a post even when the page it was
    # derived from is clean, and REPACK is the only node that sees the post.
    "REPACK": frozenset({CHANNEL_VALIDATE, CLAIMS_CHECK, RECORD_POSTS}),
    # The human interrupt holds nothing at all: the run is paused, and a paused
    # run must not be able to act.
    "REVIEW": frozenset(),
    "EXPORT": frozenset({PUBLISH, NOTIFY}),
    "MEASURE": frozenset({GEO_PROBE, ANALYTICS_FETCH}),
}


def allowed_tools(node: str) -> frozenset[str]:
    """What this node may call.

    An unknown node gets the empty set, not a KeyError and not a wildcard: a node
    nobody granted anything to should be able to do nothing, which is the only
    fail-safe default for an allowlist.
    """
    return NODE_TOOLS.get(node, frozenset())


class ToolNotAllowedError(Exception):
    """A node tried to use a tool outside its allowlist.

    Carries all three facts a reader needs -- which node, which tool, what it was
    actually granted -- because "tool not allowed" on its own cannot be acted on.
    """

    def __init__(self, node: str, tool: str, allowed: Iterable[str]) -> None:
        self.node = node
        self.tool = tool
        self.allowed = tuple(sorted(allowed))
        granted = ", ".join(self.allowed) or "nothing"
        super().__init__(
            f"node {node} may not call tool {tool!r}: its allowlist grants {granted}. "
            "The allowlist in backend/app/agents/tools.py is the single source of "
            "truth; widen it there, deliberately, or use a tool the node holds."
        )


@dataclass(frozen=True)
class ToolRefusal:
    """One refused tool call, in a form that can be rendered and stored."""

    node: str
    tool: str
    reason: str

    def as_node_error(self) -> NodeError:
        return NodeError(node=self.node, code="tool_not_allowed", message=self.reason)


ToolImpl = Callable[..., Awaitable[Any]] | Callable[..., Any]


@dataclass
class NodeToolbox:
    """Every tool a single node reaches passes through one of these.

    Constructed per node execution rather than per process: the refusal list is
    part of one node's result, and a shared instance would leak refusals from one
    node into another's errors.
    """

    node: str
    implementations: Mapping[str, ToolImpl] = field(default_factory=dict)
    #: Operator revocations for this node, from `node_tool_policies`.
    #:
    #: Subtracted from the code allowlist, never added to it. The direction is the
    #: security property, and it holds structurally: a set DIFFERENCE cannot produce a
    #: member its left operand did not have, so no stored value -- garbage, hostile, or
    #: naming another node's tools -- can widen what this node reaches. That is why the
    #: settings table has a `revoked` column and no `granted` one.
    revoked: frozenset[str] = frozenset()
    _refusals: list[ToolRefusal] = field(default_factory=list, init=False, repr=False)

    @property
    def allowed(self) -> frozenset[str]:
        """The code ceiling, minus whatever an operator has revoked."""
        return allowed_tools(self.node) - self.revoked

    @property
    def refusals(self) -> tuple[ToolRefusal, ...]:
        return tuple(self._refusals)

    def allows(self, tool: str) -> bool:
        """Is this tool granted to this node."""
        return tool in self.allowed

    def available(self, tool: str) -> bool:
        """Granted AND wired up.

        The distinction is deliberate, and its meaning has narrowed: an allowlisted
        tool with no implementation used to mean "the build has not reached this yet"
        (`kb.search` inside GENERATE, `geo.probe` and `nap.audit` in HARVEST,
        `web_search` in GENERATE -- all four now implemented). What remains is the
        stronger case: a tool deliberately left unwired because wiring it would be
        DISHONEST. `serp.search` and `web_search` are wired only when the real provider
        is configured, because a fake result that reaches a draft cannot be told apart
        from a real one afterwards; `kb.search` is wired only when the business has
        indexed something, because a retriever over an empty store answers "nothing
        relevant" and that reads as a business whose own material had nothing to say.

        So "not available" is now a fact about the DEPLOYMENT or the TENANT, and every
        caller turns it into a named `fact_gap` rather than silence.
        """
        return self.allows(tool) and self.implementations.get(tool) is not None

    def guard(self, tool: str) -> None:
        """Raise unless this node holds `tool`. The synchronous enforcement point.

        The refusal is recorded before the raise, so a node whose failure the graph
        converts into a degradation still leaves the specific tool name in the log
        rather than only a generic node failure.
        """
        if tool in self.allowed:
            return
        self._record(tool, "our own code requested it")
        raise ToolNotAllowedError(self.node, tool, self.allowed)

    async def call(self, tool: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a tool, enforcing the allowlist first.

        Raises :class:`ToolNotAllowedError` before the implementation is even
        looked up, so an ungranted tool is refused whether or not it happens to be
        wired. A missing implementation for a granted tool is a `KeyError` -- the
        caller is expected to ask :meth:`available` first, and a silent None would
        be indistinguishable from a tool that found nothing.
        """
        self.guard(tool)
        impl = self.implementations[tool]
        result = impl(*args, **kwargs)
        if isinstance(result, Awaitable):
            return await result
        return result

    def accept(self, tool: str) -> bool:
        """Is a MODEL's tool call permitted here. Records a refusal instead of raising.

        Returns False for a tool this node does not hold, because a model that
        asks for `publish` in GENERATE has very likely been asked to by the page
        it was given, and the run should continue without it rather than crash.
        """
        return self._permit(tool, "the model requested it")

    def _permit(self, tool: str, how: str) -> bool:
        if tool in self.allowed:
            return True
        self._record(tool, how)
        return False

    def accepted_calls(self, calls: Sequence[Any]) -> list[Any]:
        """Filter a completion's tool calls down to the ones this node holds."""
        return [call for call in calls if self.accept(str(getattr(call, "name", "")))]

    def offer(self, specs: Sequence[ToolSpec]) -> list[ToolSpec]:
        """Filter the specs offered to the model down to this node's allowlist.

        Section 4 of the runtime doc: a tool the node cannot have is REMOVED from
        the list rather than refused on call, so the model never plans around a
        capability it will not get. :meth:`accept` is the backstop for the case
        where it asks anyway.
        """
        return [
            spec
            for spec in specs
            if self._permit(spec.name, "our own code offered it to the model")
        ]

    def node_errors(self) -> list[NodeError]:
        """The refusals, as state errors the UI already knows how to render."""
        return [refusal.as_node_error() for refusal in self._refusals]

    def _record(self, tool: str, how: str) -> ToolRefusal:
        reason = (
            f"Refused tool {tool!r}: node {self.node} is not allowed to call it "
            f"({how}). Allowed here: {', '.join(sorted(self.allowed)) or 'nothing'}."
        )
        refusal = ToolRefusal(node=self.node, tool=tool, reason=reason)
        self._refusals.append(refusal)
        # WARNING, not DEBUG: a refusal is either a bug in our wiring or an
        # attempted injection, and both deserve to be in the log by default.
        logger.warning(
            "tool refused: node=%s tool=%s allowed=%s trigger=%s",
            self.node,
            tool,
            sorted(self.allowed),
            how,
        )
        return refusal


__all__ = [
    "ACTUATOR_TOOLS",
    "ANALYTICS_FETCH",
    "CHANNEL_VALIDATE",
    "CLAIMS_CHECK",
    "CRAWL_SITE",
    "GEO_PROBE",
    "KB_SEARCH",
    "KB_VERIFY",
    "KNOWN_TOOLS",
    "LANDING_CHECK",
    "MEMORY_LOAD",
    "NAP_AUDIT",
    "NODE_TOOLS",
    "NOTIFY",
    "PUBLISH",
    "RECORD_LANDING_PAGE",
    "RECORD_OPPORTUNITIES",
    "RECORD_OUTLINE",
    "RECORD_PAGE",
    "RECORD_POSTS",
    "SEO_SCORE",
    "SERP_SEARCH",
    "WEB_SEARCH",
    "NodeToolbox",
    "ToolImpl",
    "ToolNotAllowedError",
    "ToolRefusal",
    "allowed_tools",
]
