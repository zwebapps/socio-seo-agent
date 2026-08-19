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

from backend.app.services.prompt_inventory import EVAL_HARNESS_NOTE, prompt_surfaces


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
