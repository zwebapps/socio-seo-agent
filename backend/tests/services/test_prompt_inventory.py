"""The prompt-version inventory must describe the code, not a hope about the code.

This is an anti-drift test in the same spirit as the doc-table parser in
`backend/tests/agents/test_tool_allowlist.py`: the inventory declares module paths and
attribute names as DATA, so nothing stops it going stale except a test that reads the
constants directly and compares.

It also pins the honest claim the screen makes -- exactly one version per surface, none
switchable. If a second runtime version is ever introduced, this test fails and whoever
introduced it has to decide what the screen should now offer, which is the correct moment
for that decision.
"""

from unittest import mock

from backend.app.services.prompt_inventory import (
    EVAL_HARNESS_NOTE,
    graph_node_count,
    prompt_surfaces,
    task_class_count,
)


def test_every_declared_surface_resolves_to_a_real_constant() -> None:
    """Guard the guard: `_read` reports an unreadable surface rather than raising, so
    without this a wholly broken inventory would render as three tidy error rows."""
    surfaces = prompt_surfaces()

    assert surfaces, "the inventory is empty, so the screen would show nothing"
    broken = [(s.key, s.error) for s in surfaces if s.version is None]
    assert not broken, f"declared prompt surfaces that could not be read: {broken}"


def test_the_inventory_reports_the_same_values_a_direct_import_gives() -> None:
    """The declared module/attribute pairs are data. This is what stops them drifting."""
    from backend.app.agents.nodes import prompts
    from backend.app.services import kb_service, onboarding_service

    found = {s.key: s.version for s in prompt_surfaces()}

    assert found["nodes"] == prompts.PROMPT_VERSION
    assert found["kb_retrieval"] == kb_service.RETRIEVAL_PROMPT_VERSION
    assert found["onboarding"] == onboarding_service.PROMPT_VERSION


def test_the_runtime_has_exactly_one_version_per_surface_and_says_so() -> None:
    """The honest claim behind building an inventory instead of a dropdown.

    If this fails because a second version now exists, do NOT relax it -- that is the
    signal to build a real selector for the surface that grew one.
    """
    for surface in prompt_surfaces():
        assert surface.variants == 1, (
            f"{surface.key} now has {surface.variants} versions. The screen renders an "
            "inventory precisely because there was nothing to choose between; a real "
            "selector is now warranted for it."
        )
        assert surface.switchable is False, (
            f"{surface.key} claims to be switchable, but nothing in the runtime reads a "
            "stored prompt version -- the claim would be false on screen"
        )


def test_the_nodes_surface_covers_all_eight_nodes_with_one_constant() -> None:
    """Worth pinning because it is the fact most likely to be misread: `nodes.v1` is not
    "the INTAKE prompt", it is every node's prompt, since they are all assembled by the
    same `system()` helper."""
    nodes = next(s for s in prompt_surfaces() if s.key == "nodes")

    assert nodes.attribute == "PROMPT_VERSION"
    assert nodes.module.endswith("agents.nodes.prompts")
    assert "every node" in nodes.how_to_change or "each node" in nodes.how_to_change


def test_the_eval_harness_is_named_without_a_count_that_could_drift() -> None:
    """The harness genuinely does have several prompts and a `--prompt-version` flag, but
    quoting how many would be a claim that goes stale the moment somebody adds a builder --
    and the harness is not imported here, because the eval CLI is not part of the app."""
    assert "--prompt-version" in EVAL_HARNESS_NOTE
    assert "evals/run.py" in EVAL_HARNESS_NOTE
    assert not any(char.isdigit() for char in EVAL_HARNESS_NOTE), (
        "the note quotes a number; a count of eval prompt versions here is a claim nothing "
        "keeps true"
    )


# --------------------------------------------------------------------------- #
# The node count is DERIVED. A hardcoded one is what drifted.
# --------------------------------------------------------------------------- #


def test_the_node_label_carries_the_real_graph_size() -> None:
    """The label must equal `graph.ORDER`'s length, not a number somebody typed.

    This screen said "Graph nodes (all eight)" while `ORDER` held eleven — EXPORT and
    MEASURE landed with the publishing epic and nothing updated the caption. It was wrong
    in the other direction too: only five call sites use `_ask`, the helper that stamps
    the version at all. A false architectural claim on an admin screen is the kind of
    thing a reviewer finds and then distrusts everything else on the page for.
    """
    from backend.app.agents import graph

    label = next(s.label for s in prompt_surfaces() if s.key == "nodes")

    assert label == f"Graph nodes ({len(graph.ORDER)})"


def test_adding_a_node_changes_the_displayed_count() -> None:
    """The regression test that matters: the count TRACKS the graph.

    Asserting the label against `len(ORDER)` once would pass just as happily on a second
    hardcoded literal that happened to be right on the day it was written. This proves the
    dependency is live — extend the graph and the screen follows, with no second edit.
    """
    from backend.app.agents import graph

    before = next(s.label for s in prompt_surfaces() if s.key == "nodes")

    with mock.patch.object(graph, "ORDER", (*graph.ORDER, "NEW_NODE")):
        after = next(s.label for s in prompt_surfaces() if s.key == "nodes")

    assert before == f"Graph nodes ({len(graph.ORDER)})"
    assert after == f"Graph nodes ({len(graph.ORDER) + 1})"
    assert before != after, "the label is hardcoded again -- it did not follow the graph"


def test_the_two_counts_are_reported_separately() -> None:
    """Node count and task-class count are different concepts, and must stay two numbers.

    Conflating them is the whole defect this replaced: a graph node is a step in the run,
    a task class is what a model call is FOR, and two nodes doing the same kind of work
    share one class. So `EXTRACT` and `PRIORITISE` exist with no node of that name, and
    `EMBED` is a tier rather than a step in any graph. They are not meant to be equal, and
    a single count on the screen is what let a reader believe they were.
    """
    from backend.app.agents import graph
    from backend.app.llm.contract import TaskClass

    nodes, error = graph_node_count()

    assert error is None
    assert nodes == len(graph.ORDER)
    assert task_class_count() == len(TaskClass)
    assert nodes != task_class_count(), (
        "if these ever coincide the test still holds, but the assertion below is the "
        "point: they are read from different places and neither is derived from the other"
    )


def test_an_unreadable_graph_is_reported_rather_than_guessed() -> None:
    """A count that cannot be read becomes no count, never a plausible number.

    Same posture as the version constants: an inventory whose job is to tell the truth
    about drift has to survive the drift it is describing. The bare label is honest; a
    fallback of "8" or "11" would be the original bug with extra steps.
    """
    # Patched by dotted string rather than by attribute: `importlib` is an incidental
    # import inside that module, not part of its public surface, and mypy is right to say
    # so.
    with mock.patch(
        "backend.app.services.prompt_inventory.importlib.import_module",
        side_effect=ImportError("boom"),
    ):
        count, error = graph_node_count()
        label = next(s.label for s in prompt_surfaces() if s.key == "nodes")

    assert count is None
    assert error is not None and "could not be imported" in error
    assert label == "Graph nodes"
    assert "8" not in label and "11" not in label
