"""The per-node tool allowlist, and the proof that the doc cannot drift from it.

`docs/AGENT_RUNTIME.md` section 3 has always tabulated which tools each node may
call. Documentation is not a control, so three things are asserted here:

1. the allowlist is COMPLETE and well-formed (every node, every name real);
2. the doc table and `NODE_TOOLS` agree, checked by parsing the markdown — so a
   change to either without the other fails the build;
3. the enforcement actually bites, in both directions: our own code asking for an
   ungranted tool raises, and a model asking for one is refused and RECORDED.

The second one is the anti-drift mechanism, and it is worth stating why it is a test
rather than a review habit: the table is prose in a file nobody imports, and the two
places would have diverged the first time a node grew a capability in a hurry.
"""

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from backend.app.agents.graph import ORDER
from backend.app.agents.tools import (
    ACTUATOR_TOOLS,
    CRAWL_SITE,
    KNOWN_TOOLS,
    NODE_TOOLS,
    NOTIFY,
    PUBLISH,
    RECORD_PAGE,
    SERP_SEARCH,
    NodeToolbox,
    ToolNotAllowedError,
    allowed_tools,
)
from backend.app.llm import ToolSpec


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate the repo root (no pyproject.toml above this file)")


DOC = _repo_root() / "docs" / "AGENT_RUNTIME.md"


# --------------------------------------------------------------------------- #
# The allowlist itself
# --------------------------------------------------------------------------- #


def test_every_node_the_graph_runs_has_an_entry() -> None:
    """A node with no entry gets the empty set, which is the right fail-safe -- but it
    would mean the node silently cannot work, so absence must be deliberate."""
    missing = [name for name in ORDER if name not in NODE_TOOLS]
    assert not missing, f"nodes with no allowlist entry: {missing}"


def test_the_two_documented_but_unbuilt_nodes_already_have_their_grants() -> None:
    """EXPORT and MEASURE are specified in the doc and not yet written. Recording
    their grants now means the barrier is in place before the code is, rather than
    being invented by whoever writes them under deadline."""
    assert NODE_TOOLS["EXPORT"] == {PUBLISH, NOTIFY}
    assert "MEASURE" in NODE_TOOLS


def test_no_allowlist_names_a_tool_the_runtime_does_not_know() -> None:
    """A typo in an allowlist entry fails OPEN in the sense that matters: the grant it
    was meant to express is missing, and nothing says so."""
    unknown = {
        f"{node}:{tool}"
        for node, tools in NODE_TOOLS.items()
        for tool in tools
        if tool not in KNOWN_TOOLS
    }
    assert not unknown, f"unknown tool names in the allowlist: {sorted(unknown)}"


def test_only_export_may_reach_an_actuator() -> None:
    """docs/AGENT_RUNTIME.md section 3 calls this "the second of three independent
    prompt-injection barriers". This is the line that makes that claim true: HARVEST
    and GENERATE handle attacker-controllable text and cannot publish or notify."""
    offenders = {
        node: sorted(tools & ACTUATOR_TOOLS)
        for node, tools in NODE_TOOLS.items()
        if node != "EXPORT" and tools & ACTUATOR_TOOLS
    }
    assert not offenders, f"nodes other than EXPORT holding an actuator tool: {offenders}"


def test_the_paused_run_can_do_nothing() -> None:
    """REVIEW is a human interrupt. A paused run that could still act would make the
    interrupt cosmetic."""
    assert allowed_tools("REVIEW") == frozenset()


def test_an_unknown_node_is_granted_nothing_rather_than_everything() -> None:
    assert allowed_tools("TOTALLY_MADE_UP") == frozenset()


def test_no_node_holds_another_nodes_output_tool() -> None:
    """The structured-output tools are in the same allowlist as the engine tools on
    purpose: a model asked to `record_page` inside PLAN is as wrong as one asked to
    `publish`, and only one node writes each artefact.

    The set is derived from `KNOWN_TOOLS` rather than listed, so a new output tool is
    covered by this rule the moment it exists. It was a hand-written list of four, and
    `record_landing_page` was added without it noticing -- which is exactly the drift
    the file's own docstring is about.
    """
    outputs = {tool for tool in KNOWN_TOOLS if tool.startswith("record_")}
    assert len(outputs) >= 5, (
        "the derivation found fewer output tools than exist; if the naming convention "
        f"changed, fix this derivation rather than deleting the rule (found {outputs})"
    )
    holders: dict[str, list[str]] = {tool: [] for tool in outputs}
    for node, tools in NODE_TOOLS.items():
        for tool in tools & outputs:
            holders[tool].append(node)
    assert all(len(nodes) == 1 for nodes in holders.values()), holders


# --------------------------------------------------------------------------- #
# The doc and the code cannot drift
# --------------------------------------------------------------------------- #


def _doc_table() -> dict[str, frozenset[str]]:
    """Parse the "Tools allowed" column out of section 3 of the runtime doc.

    The column is located by its HEADER rather than by index, so inserting a column
    into the table does not silently make this test read the wrong one -- which would
    turn the anti-drift guard into a test that passes for the wrong reason.
    """
    rows = [line for line in DOC.read_text(encoding="utf-8").splitlines() if line.startswith("| ")]
    header = next((row for row in rows if "Tools allowed" in row), None)
    assert header is not None, (
        f"{DOC} no longer has a 'Tools allowed' column. If the table moved, fix this "
        "parser -- do not delete it: it is the only thing keeping the doc and the "
        "runtime allowlist in step."
    )
    columns = [cell.strip() for cell in header.split("|")]
    index = columns.index("Tools allowed")

    table: dict[str, frozenset[str]] = {}
    for row in rows:
        cells = [cell.strip() for cell in row.split("|")]
        if len(cells) != len(columns) or cells[1].startswith("---"):
            continue
        node = cells[1].strip("`")
        if node not in NODE_TOOLS:
            continue
        table[node] = frozenset(re.findall(r"`([^`]+)`", cells[index]))
    return table


def test_the_doc_table_is_parseable_and_covers_every_node() -> None:
    """Guard the guard: a parser that silently matches nothing would make the drift
    test below pass on an empty comparison."""
    parsed = _doc_table()
    assert set(parsed) == set(NODE_TOOLS), (
        "the doc table and the allowlist do not even describe the same node set: "
        f"doc={sorted(parsed)} code={sorted(NODE_TOOLS)}"
    )


def test_the_doc_and_the_runtime_allowlist_agree_exactly() -> None:
    parsed = _doc_table()
    differences = {
        node: {"doc": sorted(parsed[node]), "code": sorted(NODE_TOOLS[node])}
        for node in NODE_TOOLS
        if parsed[node] != NODE_TOOLS[node]
    }
    assert not differences, (
        "docs/AGENT_RUNTIME.md section 3 and NODE_TOOLS disagree. NODE_TOOLS is the "
        "source of truth -- update the doc table to match it:\n"
        f"{differences}"
    )


# --------------------------------------------------------------------------- #
# Enforcement: our own code
# --------------------------------------------------------------------------- #


async def test_a_granted_tool_runs() -> None:
    async def crawl(url: str) -> dict[str, int]:
        return {"pages": 3}

    box = NodeToolbox(node="HARVEST", implementations={CRAWL_SITE: crawl})

    assert await box.call(CRAWL_SITE, "https://x.de") == {"pages": 3}
    assert box.refusals == ()


async def test_a_synchronous_implementation_is_supported_without_a_wrapper() -> None:
    """The deterministic engines (`seo.score`, `claims.check`) are plain functions."""
    box = NodeToolbox(node="HARVEST", implementations={CRAWL_SITE: lambda url: {"pages": 1}})
    assert await box.call(CRAWL_SITE, "https://x.de") == {"pages": 1}


async def test_an_ungranted_tool_raises_before_the_implementation_is_even_looked_up() -> None:
    """The order matters: refusing only when the tool happens to be unwired would mean
    the allowlist is enforced by accident, and stops being enforced the moment
    somebody injects the dependency."""
    calls: list[str] = []

    async def publish(ref: str) -> str:
        calls.append(ref)
        return "published"

    box = NodeToolbox(node="GENERATE", implementations={PUBLISH: publish})

    with pytest.raises(ToolNotAllowedError) as exc:
        await box.call(PUBLISH, "https://example.com/post")

    assert calls == [], "the implementation must not run"
    assert exc.value.node == "GENERATE"
    assert exc.value.tool == PUBLISH
    assert "may not call tool" in str(exc.value)
    assert "record_page" in str(exc.value), "the message must name what IS granted"


async def test_a_refusal_of_our_own_code_is_still_recorded_before_it_raises() -> None:
    """A node failure reaches the run state as a generic `node_failed`, so without
    this the specific tool name would exist only in a traceback."""
    box = NodeToolbox(node="GENERATE", implementations={})
    with pytest.raises(ToolNotAllowedError):
        await box.call(PUBLISH)

    assert [r.tool for r in box.refusals] == [PUBLISH]
    assert box.node_errors()[0].code == "tool_not_allowed"


def test_available_distinguishes_ungranted_from_unwired() -> None:
    """Two different states that must not be conflated. `kb.search` is granted to
    GENERATE and not implemented there yet, which is a build state; `publish` is not
    granted at all, which is a design decision."""
    box = NodeToolbox(node="HARVEST", implementations={CRAWL_SITE: lambda url: None})

    assert box.allows(CRAWL_SITE) and box.available(CRAWL_SITE)
    assert box.allows(SERP_SEARCH) and not box.available(SERP_SEARCH)
    assert not box.allows(PUBLISH) and not box.available(PUBLISH)


# --------------------------------------------------------------------------- #
# Enforcement: the model's tool calls
# --------------------------------------------------------------------------- #


class _Call:
    def __init__(self, name: str) -> None:
        self.name = name


def test_a_models_ungranted_tool_call_is_dropped_and_recorded_rather_than_raising() -> None:
    """A model asking for `publish` inside GENERATE has very likely been asked to by
    the page it was handed. Raising would let one sentence in a competitor's HTML end
    somebody's run, so the call is dropped and the run continues."""
    box = NodeToolbox(node="GENERATE", implementations={})

    accepted = box.accepted_calls([_Call(PUBLISH), _Call(RECORD_PAGE), _Call("notify")])

    assert [c.name for c in accepted] == [RECORD_PAGE]
    assert sorted(r.tool for r in box.refusals) == ["notify", "publish"]


def test_a_refused_model_call_is_visible_on_the_run_state() -> None:
    """Observability is the requirement, not just the refusal: a silent drop is
    indistinguishable from a tool that returned nothing."""
    box = NodeToolbox(node="HARVEST", implementations={})
    box.accept(PUBLISH)

    error = box.node_errors()[0]
    assert error.node == "HARVEST"
    assert error.code == "tool_not_allowed"
    assert "publish" in error.message
    assert "not allowed" in error.message


def test_the_refusal_is_logged_at_warning_because_it_is_never_routine(
    caplog: Any,
) -> None:
    box = NodeToolbox(node="GENERATE", implementations={})
    with caplog.at_level("WARNING", logger="backend.app.agents.tools"):
        box.accept(PUBLISH)

    assert any("tool refused" in record.message for record in caplog.records)
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_offer_removes_an_ungranted_spec_rather_than_waiting_to_refuse_it() -> None:
    """docs/AGENT_RUNTIME.md section 4: a tool the node cannot have is REMOVED from
    the list, so the model never plans around a capability it will not get."""
    box = NodeToolbox(node="GENERATE", implementations={})
    specs = [
        ToolSpec(name=RECORD_PAGE, description="d", parameters={"type": "object"}),
        ToolSpec(name=PUBLISH, description="d", parameters={"type": "object"}),
    ]

    offered = box.offer(specs)

    assert [spec.name for spec in offered] == [RECORD_PAGE]
    assert [r.tool for r in box.refusals] == [PUBLISH]


def test_a_toolbox_is_per_node_so_refusals_do_not_leak_between_nodes() -> None:
    harvest = NodeToolbox(node="HARVEST", implementations={})
    generate = NodeToolbox(node="GENERATE", implementations={})
    harvest.accept(PUBLISH)

    assert generate.refusals == (), "one node's refusal must not appear in another's errors"


def test_the_conversion_node_cannot_reach_the_open_web() -> None:
    """CONVERT writes the page a public URL will serve, and its proof points are claims
    about the BUSINESS. `web_search` would let it source one from a page the business
    does not control, which is not proof of anything about the business -- and it is a
    second untrusted-text intake on the node whose output carries a form."""
    tools = NODE_TOOLS["CONVERT"]

    assert "web_search" not in tools
    assert tools & ACTUATOR_TOOLS == frozenset()
    assert "kb.search" in tools, "proof has to come from the business's own material"


def test_the_landing_audit_belongs_to_the_node_with_no_model_in_it() -> None:
    """ "Is there a form, and can it be answered" is set membership. Granting
    `landing.check` to CONVERT instead would let the node that WROTE the page be the one
    that grades it."""
    assert "landing.check" in NODE_TOOLS["VALIDATE"]
    assert "landing.check" not in NODE_TOOLS["CONVERT"]


# --------------------------------------------------------------------------- #
# Revocations are honoured by the RUNNING graph, not merely stored
# --------------------------------------------------------------------------- #


def test_a_revocation_narrows_what_a_node_may_call() -> None:
    """The wiring `services/tool_policy` pointed at, now done.

    Until `_toolbox` passed `revoked`, an operator's revocation was stored, displayed
    and computed but NOT honoured by the running graph -- a kill switch that did
    nothing, which is worse than no kill switch because somebody would believe they
    had pulled it.
    """
    ceiling = allowed_tools("EXPORT")
    assert "publish" in ceiling, "the fixture depends on EXPORT holding publish"

    box = NodeToolbox(node="EXPORT", revoked=frozenset({"publish"}))

    assert box.allows("publish") is False
    assert box.allowed == ceiling - {"publish"}


def test_a_revocation_cannot_widen_the_allowlist() -> None:
    """The security property, and it holds STRUCTURALLY rather than by validation.

    `allowed` is a set DIFFERENCE, so it cannot produce a member its left operand did
    not have. Garbage, another node's tools, or the entire vocabulary all narrow or do
    nothing -- which is why the settings table has a `revoked` column and no `granted`
    one, and why no input sanitising is needed here.
    """
    ceiling = allowed_tools("GENERATE")

    for hostile in (
        frozenset({"publish"}),  # an actuator this node must never hold
        frozenset(KNOWN_TOOLS),  # the whole vocabulary
        frozenset({"../../etc/passwd", ""}),  # nonsense
    ):
        assert NodeToolbox(node="GENERATE", revoked=hostile).allowed <= ceiling


async def test_a_revocation_is_honoured_by_the_running_graph() -> None:
    """End to end through `build_nodes`, because the claim is about the RUNTIME.

    `describe_node_tools` reports `enforced=True` to an operator, and a constant
    asserting that would prove nothing. This drives a real node with a real toolbox and
    shows the tool is refused -- and, critically, that the node still produces its
    output, because an attacker must not be able to end a run by causing a refusal.
    """
    from backend.app.agents.nodes import NodeDeps, build_nodes
    from backend.app.agents.state import new_state

    crawled: list[str] = []

    async def crawl(url: str) -> dict[str, object]:
        crawled.append(url)
        return {"pages": []}

    state = new_state(business_id=uuid4(), goal="more leads")
    state["dna"] = {"website": "https://mueller.example", "locale": "de"}

    # Same deps twice, once with `crawl.site` revoked from HARVEST.
    permitted = build_nodes(NodeDeps(router=object(), crawl_site=crawl))
    revoked = build_nodes(
        NodeDeps(
            router=object(),
            crawl_site=crawl,
            revoked_tools={"HARVEST": frozenset({"crawl.site"})},
        )
    )

    await permitted["HARVEST"](state)
    assert crawled == ["https://mueller.example"], "the control: it crawls when permitted"

    crawled.clear()
    result = await revoked["HARVEST"](state)

    assert crawled == [], "revoked means the implementation is never reached"
    assert result is not None, "the node must still return; a refusal is not a run-ender"


def test_the_enforcement_flag_matches_reality() -> None:
    """REPLACES the assertion that `RUNTIME_ENFORCED is False`.

    That test was correct while the wiring was absent and is now exactly wrong -- it
    would pin the honest-but-unfinished state forever. It is replaced rather than
    deleted, and by something stronger: instead of asserting the constant's value, this
    asserts the constant AGREES with the behaviour, so the two cannot drift. A flag
    claiming enforcement that does not happen is the failure mode worth guarding.
    """
    from backend.app.services.tool_policy import RUNTIME_ENFORCED

    box = NodeToolbox(node="EXPORT", revoked=frozenset({"publish"}))
    actually_enforced = not box.allows("publish")

    assert actually_enforced == RUNTIME_ENFORCED
