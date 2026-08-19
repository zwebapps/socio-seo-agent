"""What prompt versions the RUNTIME actually has, and which of them can be switched.

This exists because the honest answer to "build a prompt-version selector" is that there
is nothing to select yet, and a dropdown with one entry pretending to be a choice is
worse than a plain statement of fact. Three constants carry a prompt version in the
running app:

* ``agents/nodes/prompts.PROMPT_VERSION`` -- ``nodes.v1``. ONE value shared by all eight
  graph nodes, not one per node: the module assembles every node's prompt from the same
  ``system()`` helper and stamps them all with the same label.
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


class PromptSurface(BaseModel):
    """One place the runtime stamps a prompt version."""

    model_config = ConfigDict(frozen=True)

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
        "Graph nodes (all eight)",
        "backend.app.agents.nodes.prompts",
        "PROMPT_VERSION",
        "One constant covers every node, because every node's prompt is assembled by the "
        "same system() helper. A per-node version would mean per-node constants first.",
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


__all__ = ["EVAL_HARNESS_NOTE", "PromptSurface", "prompt_surfaces"]
