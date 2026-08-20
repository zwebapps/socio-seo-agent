"""What prompt versions the RUNTIME actually has, and which of them can be switched.

This exists because the honest answer to "build a prompt-version selector" is that there
is nothing to select yet, and a dropdown with one entry pretending to be a choice is
worse than a plain statement of fact. Three constants carry a prompt version in the
running app:

* ``agents/nodes/prompts.PROMPT_VERSION`` -- ``nodes.v1``. ONE value shared by every node
  that calls the model, not one per node: the module assembles every prompt from the same
  ``system()`` helper and stamps them all with the same label.

  The label for this surface is DERIVED from ``graph.ORDER`` rather than written down. It
  used to read "all eight", which was false in both directions -- ``ORDER`` holds eleven
  nodes (EXPORT and MEASURE landed with the publishing epic and nothing updated the
  count), while only five call sites use ``_ask``, the helper that stamps the version at
  all. Two nodes make no model call by design and VALIDATE is deterministic scoring. A
  hardcoded count drifts on the next node added; a derived one cannot.
* ``services/kb_service.RETRIEVAL_PROMPT_VERSION`` -- the retrieval prompt.
* ``services/onboarding_service.PROMPT_VERSION`` -- website DNA extraction.

Each is a module-level string with no alternative defined anywhere, so **there is no
runtime prompt version to switch between.** The eval harness is different -- it holds
several generation prompts and picks one with ``--prompt-version`` on the command line
(``evals/run.py``, ``PROMPT_BUILDERS``) -- but those are the harness's prompts, not the
product's, and selecting one there changes an experiment rather than a run.

So this module builds an INVENTORY, not a selector: every surface, the constant it reads,
the value in force, and whether it can be changed without a deploy. The screen renders
exactly that, and says plainly that the way to introduce a second version is to add it in
code and record the choice here -- which is also the only way it could be reviewed.

**Why the constants are read by ``importlib`` rather than imported at the top.** Two
reasons, and the first is practical: ``backend.app.agents.nodes`` is NOT in the running
API's import graph today, and importing it here to read one string would add the whole
graph package -- nodes, tools, engines -- to every process that serves an HTTP request.
The second is that a surface which fails to import should be REPORTED as unreadable
rather than take the endpoint down with it; an inventory whose job is to tell the truth
about drift should survive the drift it is describing.
"""

from __future__ import annotations

import importlib
from typing import Final

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class PromptSurface(BaseModel):
    """One place the runtime stamps a prompt version.

    camelCase on the wire, like every other payload in this API. It is spelled out here
    rather than inherited because this model lives in a service module and the API's
    `CamelModel` lives in the route module — and the omission was not free: the route sets
    `response_model_by_alias=True`, but an alias generator on the OUTER model does not
    reach a nested one, so `how_to_change` shipped snake_case while the screen read
    `surface.howToChange`. That renders as `undefined`, which React shows as nothing, so
    the one sentence telling an operator how to change a prompt version was silently
    absent from the screen that exists to tell them.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    key: str
    label: str
    #: Dotted module path and attribute name, so the screen can point a reader at the
    #: line to edit instead of describing it.
    module: str
    attribute: str
    #: The value in force, or None when the constant could not be read.
    version: str | None
    #: How many alternatives exist to switch between. 1 means "no choice", which is the
    #: current state everywhere and the thing the screen must not disguise.
    variants: int
    #: True only if an operator could change this without a code change. All False today.
    switchable: bool
    #: What a reader should do if they want a different version here.
    how_to_change: str
    #: Set when :attr:`version` is None: why it could not be read.
    error: str | None = None


_DECLARED: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    (
        "nodes",
        # Empty on purpose: resolved by `_nodes_label()` from `graph.ORDER` at build time.
        # A literal here is what drifted, so there is no literal to drift.
        "",
        "backend.app.agents.nodes.prompts",
        "PROMPT_VERSION",
        "One constant covers every node that calls the model, because every prompt is "
        "assembled by the same system() helper. A per-node version would mean per-node "
        "constants first.",
    ),
    (
        "kb_retrieval",
        "Knowledge-base retrieval",
        "backend.app.services.kb_service",
        "RETRIEVAL_PROMPT_VERSION",
        "Edit the constant and the prompt it labels together, so a recorded version "
        "always names the text that produced a result.",
    ),
    (
        "onboarding",
        "Website DNA extraction",
        "backend.app.services.onboarding_service",
        "PROMPT_VERSION",
        "Same rule: the label and the prompt change in one commit.",
    ),
)

#: Where the harness's own prompt versions live. Named rather than imported: the eval CLI
#: is not part of the serving app, and a count quoted here would be a claim that drifts
#: the moment somebody adds a builder.
EVAL_HARNESS_NOTE: Final = (
    "The eval harness carries its own generation prompts and selects one with "
    "`--prompt-version` on the command line (evals/run.py, PROMPT_BUILDERS). Those are "
    "the harness's prompts, not the product's: choosing one changes an experiment, not "
    "a customer's run, and it is not settable from here."
)


def _read(module: str, attribute: str) -> tuple[str | None, str | None]:
    """Read one constant, returning either its value or the reason it is unreadable."""
    try:
        loaded = importlib.import_module(module)
    # Broad on purpose: a surface that cannot be imported is REPORTED, never swallowed and
    # never allowed to take the endpoint down. See the module docstring.
    except Exception as exc:
        return None, f"{module} could not be imported: {type(exc).__name__}: {exc}"
    value = getattr(loaded, attribute, None)
    if not isinstance(value, str):
        return None, (
            f"{module}.{attribute} is {type(value).__name__}, not a string -- the "
            "inventory below is stale and should be corrected."
        )
    return value, None


def _read_sequence(module: str, attribute: str) -> tuple[int | None, str | None]:
    """Count a tuple constant, returning either its length or why it is unreadable.

    Lazily imported for the same two reasons ``_read`` is, and the first one is what makes
    a module-level ``from backend.app.agents import graph`` the wrong fix here: importing
    it would pull the graph package -- nodes, tools, engines -- into every process that
    serves an HTTP request, which is precisely what this module was written to avoid.
    """
    try:
        loaded = importlib.import_module(module)
    # Broad on purpose. See `_read`.
    except Exception as exc:
        return None, f"{module} could not be imported: {type(exc).__name__}: {exc}"
    value = getattr(loaded, attribute, None)
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        return None, (
            f"{module}.{attribute} is not a tuple of strings, so the node count on this "
            "screen cannot be derived and is not being guessed."
        )
    return len(value), None


def graph_node_count() -> tuple[int | None, str | None]:
    """How many nodes the graph actually runs, read from `graph.ORDER` itself.

    The single source of truth for the number on the screen. `ORDER` *is* the execution
    order in both drivers, so a node added there changes the screen with no other edit --
    which is the whole point, and what the previous hardcoded label could not do.
    """
    return _read_sequence("backend.app.agents.graph", "ORDER")


def task_class_count() -> int:
    """How many model-routing task classes exist. NOT the same number as the nodes.

    Kept deliberately separate, because conflating the two is the error this whole change
    exists to correct. `TaskClass` is named after the WORK, not after a node, so two nodes
    doing the same kind of work share a route -- which is why `EXTRACT` and `PRIORITISE`
    are here and `HARVEST` is not, and why `EMBED` is a tier rather than a step in any
    graph. `llm.contract` is cheap to import: no nodes, no engines.
    """
    from backend.app.llm.contract import TaskClass

    return len(TaskClass)


def _nodes_label() -> str:
    """ "Graph nodes (11)" -- or an honest fallback if `ORDER` could not be read."""
    count, _ = graph_node_count()
    return "Graph nodes" if count is None else f"Graph nodes ({count})"


def prompt_surfaces() -> list[PromptSurface]:
    """Every runtime prompt-version constant, with its current value.

    ``variants`` is 1 for every surface and ``switchable`` is False for every surface.
    Both are fields rather than prose so the screen can render the state mechanically,
    and so a test can assert it -- if a second version is ever added without updating
    this inventory, that test is what notices.
    """
    surfaces: list[PromptSurface] = []
    for key, label, module, attribute, how in _DECLARED:
        version, error = _read(module, attribute)
        label = label or _nodes_label()
        surfaces.append(
            PromptSurface(
                key=key,
                label=label,
                module=module,
                attribute=attribute,
                version=version,
                variants=1,
                switchable=False,
                how_to_change=how,
                error=error,
            )
        )
    return surfaces


__all__ = [
    "EVAL_HARNESS_NOTE",
    "PromptSurface",
    "graph_node_count",
    "prompt_surfaces",
    "task_class_count",
]
