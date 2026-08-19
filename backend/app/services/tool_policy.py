"""Operator-configurable NARROWING of the per-node tool allowlist.

`backend/app/agents/tools.NODE_TOOLS` is a security control, not a preference. It is
what `docs/AGENT_RUNTIME.md` section 3 calls "the second of three independent
prompt-injection barriers", and `backend/tests/agents/test_prompt_injection.py` asserts
that a fully compliant malicious router still cannot reach a publish actuator. So the
first question a tool-toggle screen has to answer is not "how do we store this" but
"what may a browser be allowed to change".

**The answer: revoking is exposed, granting is not, and the mechanism is set
difference so granting is not merely disallowed but inexpressible.**

Why revoking is safe. Every tool grant is a capability, and removing a capability can
only shrink what an injected page could talk a model into reaching. The worst outcome
of a bad revocation is a node that degrades -- `NodeToolbox` already records a refusal
as a `NodeError` the timeline renders -- and a degradation is a visible, recoverable,
non-security failure. There is no revocation that grants anything.

Why granting is not. A grant crosses the barrier the injection corpus exists to prove.
An HTTP-reachable grant would mean a session-fixation or CSRF bug, a stolen admin
cookie, or simply a mistaken click, is enough to hand `publish` to the node that reads
competitor HTML -- which is precisely the attack the allowlist is there to stop. The
allowlist is a code artefact that goes through review and a test suite for exactly that
reason, and moving it into a form would trade a reviewed control for an unreviewed one.

**The ceiling is `NODE_TOOLS` and it cannot be raised from here.** :func:`effective_tools`
is a set DIFFERENCE against `allowed_tools(node)`, so no value of `revoked` -- an
unknown name, a name granted to some other node, `publish` on a node that never had it,
the entire tool vocabulary -- can produce a tool the code did not already grant. The
storage shape matches: `node_tool_policies` has a `revoked` column and no `granted`
column, so there is no field in which a widening could even be written down. Both
properties are asserted in `backend/tests/services/test_tool_policy.py`.

**What this buys an operator, concretely.** Revoking `publish` and `notify` from EXPORT
is a kill switch for all outward-facing side effects that needs no deploy -- the single
most useful thing this screen can offer. Revoking `web_search` from GENERATE forces the
node onto the business's own material. Revoking `crawl.site` from HARVEST stops the
runtime fetching a site whose owner has asked us to stop.

**Enforcement.** The runtime reads its allowlist inside `agents/nodes._toolbox`,
which now passes the stored revocations into `NodeToolbox(revoked=...)`; the property
subtracts them from `allowed_tools(node)`. So :func:`describe_node_tools` reports
``enforced=True``, and that claim is backed by a test that drives a real node through
the real graph with a revocation in place rather than by this sentence.

Until that argument was wired, a revocation was stored, displayed and computed but not
honoured -- and the screen said so out loud, because a UI implying a live kill switch
that did nothing would be worse than no UI at all. Recorded here because the honest
intermediate state is the part that is easy to skip.
"""

from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from backend.app.agents.tools import (
    ACTUATOR_TOOLS,
    KNOWN_TOOLS,
    NODE_TOOLS,
    allowed_tools,
)

#: True: a stored revocation IS honoured by the running graph.
#:
#: `agents/nodes._toolbox` now passes the revocations into `NodeToolbox(revoked=...)`,
#: and `NodeToolbox.allowed` subtracts them from the code allowlist. One flag rather
#: than a per-node condition because the seam is one call site.
#:
#: This is a claim the UI repeats to an operator, so it is asserted rather than
#: believed: `test_a_revocation_is_honoured_by_the_running_graph` drives a real node
#: through the real graph with a revocation in place and proves the tool is refused.
RUNTIME_ENFORCED = True


class NodeToolPolicyRecord(BaseModel):
    """One stored narrowing. There is no `granted` field, deliberately."""

    model_config = ConfigDict(frozen=True)

    node: str
    revoked: list[str] = []
    note: str | None = None


def effective_tools(node: str, revoked: Iterable[str] = ()) -> frozenset[str]:
    """What `node` may call once operator revocations are applied.

    A set difference against the code allowlist. The direction is the whole point: the
    result is a subset of `allowed_tools(node)` for every possible `revoked`, so this
    function cannot widen the allowlist even if the stored value is garbage, hostile,
    or names tools from a different node.

    An unknown node yields the empty set, inherited from `allowed_tools` -- the same
    fail-safe default, for the same reason.
    """
    return allowed_tools(node) - frozenset(revoked)


def revocable_tools(node: str) -> frozenset[str]:
    """The only names it is meaningful to revoke on `node`: what it actually holds.

    Offering a name the node never had would put a control on screen that does nothing
    when toggled, which teaches an operator that the controls here are decorative.
    """
    return allowed_tools(node)


class NodeToolView(BaseModel):
    """One row of the tool-toggle screen."""

    model_config = ConfigDict(frozen=True)

    node: str
    #: The code allowlist: the ceiling, and read-only from every surface.
    granted: list[str]
    #: Names an operator has switched off. Always a subset of `granted` on the way out,
    #: even if a stale row names something else -- see :attr:`ignored`.
    revoked: list[str]
    #: `granted` minus `revoked`: what the node would hold once enforcement lands.
    effective: list[str]
    #: Stored revocations that name nothing this node holds. Surfaced rather than
    #: dropped: a row left behind by a rename is a stale control, and silently hiding
    #: it means nobody ever cleans it up.
    ignored: list[str]
    #: Tools here reach the outside world through an actuator. The screen marks these
    #: because revoking one is a kill switch, which is a different kind of decision
    #: from switching off a search tool.
    actuators: list[str]
    #: False while a revocation is stored but not yet read by the running graph.
    enforced: bool


def describe_node_tools(
    policies: Mapping[str, Sequence[str]] | None = None,
    *,
    enforced: bool = RUNTIME_ENFORCED,
) -> list[NodeToolView]:
    """Every node, its ceiling, its revocations and the effect.

    Every node in `NODE_TOOLS` appears, including the two that are specified and not
    yet built and the one (`REVIEW`) that holds nothing -- the same reasoning as
    `RouteResolver.describe`: a settings screen that lists only the rows somebody has
    already touched cannot be used to see the whole picture, which is the main thing an
    operator opens it for.
    """
    stored = policies or {}
    views: list[NodeToolView] = []
    for node in NODE_TOOLS:
        granted = allowed_tools(node)
        raw = frozenset(stored.get(node, ()))
        revoked = raw & granted
        views.append(
            NodeToolView(
                node=node,
                granted=sorted(granted),
                revoked=sorted(revoked),
                effective=sorted(effective_tools(node, revoked)),
                ignored=sorted(raw - granted),
                actuators=sorted(granted & ACTUATOR_TOOLS),
                enforced=enforced,
            )
        )
    return views


def unknown_tool_names(names: Iterable[str]) -> list[str]:
    """Names that are not tools this build recognises at all.

    Used to refuse a typo at the API boundary. A typo'd revocation is harmless to
    security -- set difference ignores it -- but it is a control the operator believes
    they have switched off and have not, so it is refused rather than stored.
    """
    return sorted(frozenset(names) - KNOWN_TOOLS)


__all__ = [
    "RUNTIME_ENFORCED",
    "NodeToolPolicyRecord",
    "NodeToolView",
    "describe_node_tools",
    "effective_tools",
    "revocable_tools",
    "unknown_tool_names",
]
