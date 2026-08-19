"""The tool-toggle screen cannot widen the allowlist, and this is the proof.

`backend/app/agents/tools.NODE_TOOLS` is a prompt-injection barrier: docs/AGENT_RUNTIME.md
section 3 claims only EXPORT can reach an actuator, and
`backend/tests/agents/test_prompt_injection.py` asserts that a fully compliant malicious
router still cannot publish. Adding an operator-facing tool screen puts that claim at
risk, so the tests here are not "does the feature work" -- they are the load-bearing ones:

1. the effective set is a SUBSET of the code allowlist for every node and every stored
   value, including hostile ones (:func:`test_no_stored_value_can_widen_any_node`);
2. `publish` in particular can never reach a node that reads untrusted text;
3. the STORAGE cannot express a grant either, so the invariant does not rest on one
   function being called correctly.

Test 1 is exhaustive over nodes rather than sampled, and its `revoked` inputs deliberately
include names from other nodes, unknown names, and the entire known vocabulary. A property
test with random subsets would be weaker here: the interesting inputs are exactly the ones
an attacker would choose, and they are enumerable.
"""

from backend.app.agents.tools import (
    ACTUATOR_TOOLS,
    KNOWN_TOOLS,
    NODE_TOOLS,
    PUBLISH,
    WEB_SEARCH,
    NodeToolbox,
    allowed_tools,
)
from backend.app.db.models import NodeToolPolicy
from backend.app.services.tool_policy import (
    RUNTIME_ENFORCED,
    describe_node_tools,
    effective_tools,
    revocable_tools,
    unknown_tool_names,
)

# Every stored value worth trying: nothing, one legitimate revocation, a tool the node
# never held, an actuator, a name that is not a tool at all, and the whole vocabulary.
_HOSTILE_INPUTS: tuple[tuple[str, ...], ...] = (
    (),
    (WEB_SEARCH,),
    (PUBLISH,),
    ("notify", "publish"),
    ("record_page", "record_posts", "record_outline"),
    ("definitely_not_a_tool", "../../etc/passwd", "*"),
    tuple(sorted(KNOWN_TOOLS)),
)


def test_no_stored_value_can_widen_any_node() -> None:
    """The ceiling is NODE_TOOLS, for every node, whatever is stored.

    This is the whole security argument for exposing the screen at all: revoking is a set
    difference, and a set difference cannot add a member.
    """
    for node in NODE_TOOLS:
        ceiling = allowed_tools(node)
        for stored in _HOSTILE_INPUTS:
            effective = effective_tools(node, stored)
            assert effective <= ceiling, (
                f"node {node} with revoked={stored} produced {sorted(effective - ceiling)} "
                "which the code does not grant -- the allowlist is no longer a ceiling"
            )


def test_the_ceiling_is_not_vacuous() -> None:
    """Guard the guard. If `allowed_tools` ever returned the empty set for everything,
    the subset assertion above would pass while proving nothing at all."""
    assert sum(len(allowed_tools(node)) for node in NODE_TOOLS) > 10


def test_an_actuator_cannot_be_handed_to_a_node_that_reads_untrusted_text() -> None:
    """The specific attack the barrier exists for: HARVEST crawls competitor pages and
    GENERATE is handed their text, so neither may ever hold `publish` or `notify` --
    however the tool policy is configured."""
    for node in ("HARVEST", "GENERATE", "OPPORTUNITY", "PLAN", "CONVERT", "REPACK", "REVIEW"):
        for stored in _HOSTILE_INPUTS:
            assert not (effective_tools(node, stored) & ACTUATOR_TOOLS), (
                f"{node} reached an actuator with revoked={stored}"
            )


def test_revoking_an_actuator_from_export_is_the_kill_switch() -> None:
    """The useful direction: EXPORT is the only node that may publish, so revoking both
    of its tools stops every outward-facing side effect without a deploy."""
    assert effective_tools("EXPORT", ["publish", "notify"]) == frozenset()


def test_a_revocation_actually_removes_the_tool() -> None:
    """The other half of correctness: narrowing must narrow, or the screen is decorative."""
    before = effective_tools("GENERATE", [])
    after = effective_tools("GENERATE", [WEB_SEARCH])

    assert WEB_SEARCH in before
    assert WEB_SEARCH not in after
    assert after == before - {WEB_SEARCH}


def test_an_unknown_node_is_granted_nothing_rather_than_everything() -> None:
    """Inherited from `allowed_tools`, and asserted here too because this function is
    what the API and the graph would call."""
    assert effective_tools("TOTALLY_MADE_UP", []) == frozenset()
    assert revocable_tools("TOTALLY_MADE_UP") == frozenset()


def test_the_storage_model_has_no_column_that_could_express_a_grant() -> None:
    """The invariant must not depend on one function being called correctly.

    If a `granted` column ever appears on this table, widening becomes representable and
    the argument in `services/tool_policy.py` stops holding -- so the shape of the table
    is asserted, not just the behaviour of the resolver.
    """
    columns = set(NodeToolPolicy.__table__.c.keys())

    assert "revoked" in columns
    forbidden = {name for name in columns if "grant" in name or "allow" in name}
    assert not forbidden, (
        f"node_tool_policies grew {sorted(forbidden)}. A column that can express a GRANT "
        "makes the per-node allowlist widenable from a browser, which is exactly what "
        "backend/tests/agents/test_prompt_injection.py asserts is impossible. If this is "
        "genuinely wanted it needs its own decision, not a column."
    )


def test_a_typo_is_refused_rather_than_stored_as_an_inert_control() -> None:
    """Harmless to security -- set difference ignores it -- but the operator would believe
    they had switched something off."""
    assert unknown_tool_names(["publish"]) == []
    assert unknown_tool_names(["pubish", "publish"]) == ["pubish"]


def test_every_node_appears_on_the_screen_including_the_empty_and_unbuilt_ones() -> None:
    """A settings screen listing only the rows somebody already touched cannot be used to
    see the whole picture, which is the main reason to open it."""
    views = {v.node: v for v in describe_node_tools()}

    assert set(views) == set(NODE_TOOLS)
    assert views["REVIEW"].granted == [], "the human interrupt holds nothing"
    assert views["EXPORT"].actuators == ["notify", "publish"]


def test_the_screen_reports_the_effect_of_a_stored_revocation() -> None:
    views = {v.node: v for v in describe_node_tools({"GENERATE": [WEB_SEARCH]})}

    generate = views["GENERATE"]
    assert generate.revoked == [WEB_SEARCH]
    assert WEB_SEARCH not in generate.effective
    assert WEB_SEARCH in generate.granted, "the ceiling is still reported, read-only"


def test_a_stale_revocation_is_surfaced_rather_than_silently_dropped() -> None:
    """A row left behind by a rename names a tool the node no longer holds. Hiding it
    means nobody ever cleans it up, and the screen would show a control that does
    nothing."""
    views = {v.node: v for v in describe_node_tools({"GENERATE": [PUBLISH, WEB_SEARCH]})}

    generate = views["GENERATE"]
    assert generate.ignored == [PUBLISH]
    assert generate.revoked == [WEB_SEARCH], "only what the node actually holds counts"
    assert PUBLISH not in generate.effective


def test_the_screen_reports_enforcement_that_actually_happens() -> None:
    """REPLACES `test_the_screen_admits_the_revocation_is_not_yet_enforced_at_runtime`.

    That test asserted `RUNTIME_ENFORCED is False`, and its own docstring said to flip it
    when the wiring landed. The wiring has landed -- `agents/nodes._toolbox` passes the
    revocations into `NodeToolbox(revoked=...)` -- so the old assertion now pins an
    honest-but-unfinished state that no longer exists.

    The replacement is stronger in kind, not just in value. Rather than asserting the
    constant equals a literal, it asserts the constant AGREES WITH THE BEHAVIOUR, by
    revoking a tool and checking the toolbox actually refuses it. So the flag and the
    runtime cannot drift apart in either direction -- and a flag claiming enforcement
    that does not happen is the failure mode this whole surface was careful about.
    """
    box = NodeToolbox(node="EXPORT", revoked=frozenset({PUBLISH}))
    actually_enforced = not box.allows(PUBLISH)

    assert actually_enforced is True, "the runtime must honour a revocation"
    assert actually_enforced == RUNTIME_ENFORCED
    assert all(view.enforced is True for view in describe_node_tools())
