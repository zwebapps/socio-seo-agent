"""Every path that can begin a run consults the monthly ceiling. Enforced, not trusted.

This test exists because the failure it prevents already happened. The per-business USD
ceiling of `docs/ARCHITECTURE.md` section 7.4 was composed and enforced inside
`api/runs.py`, which was correct while HTTP was the only way a run could start. Then the
scheduler shipped, called `RunService.start` and `submit(...)` directly, and spent past a
ceiling a human pressing the same button was refused at. Nothing was broken in the guard;
a new caller simply did not know it existed — and no unit test could notice, because each
module was internally consistent.

So the rule is structural: **a module that hands a run to the executor must also reference
`cost_service.monthly_cap_state`.** It is asserted at MODULE level rather than per call
site, deliberately. `api/runs.py` has one deliberately unguarded submit — `approve`, which
publishes work already generated and already paid for — and encoding a per-function
exemption list would turn this test into a rubber stamp that passes whatever the code
does. Module level catches the thing that actually went wrong: a whole new entry point
(a second worker, a CLI, a webhook) that starts runs and never asks about money.

What this test does NOT claim: that the guard is called before the submit, or that its
answer is obeyed. Ordering and consequence are behavioural and are asserted where they
belong — `tests/api/test_runs_api.py` for the 409, `tests/db/test_scheduler.py` for the
pause, both with the guard removed to prove they fail.

**The same shape guards the weekly volume cap**, for the same reason and after the same
near-miss: `ARCHITECTURE.md` §7.4's third ceiling applies to published PIECES, there are
two ways a piece reaches a platform (EXPORT and the calendar's publish button), and a cap
enforced on one of them is advisory rather than real.
"""

import ast
from pathlib import Path


def _repo_root() -> Path:
    """Anchored by walking up to `pyproject.toml`, as `test_engine_boundary` does.

    Counting parents broke there the moment `tests/` moved, and it broke by pointing at a
    directory that does not exist — reported as "nothing to check" rather than as its own
    misconfiguration, which is the failure mode a guard test can least afford.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate the repo root (no pyproject.toml above this file)")


APP_DIR = _repo_root() / "backend" / "app"

#: The name of the executor's entry point. One string, because there is one way to start
#: a run in this codebase and this test is worth nothing if a second one is invented under
#: a different name — see `test_the_executor_still_calls_its_entry_point_submit`.
SUBMIT = "submit"

#: The shared decision. A module that submits must name this.
GUARD = "monthly_cap_state"

#: `actuate()` is how a side effect reaches the outside world, and the weekly volume cap's
#: shared decision. A module that publishes must name the second.
PUBLISH = "actuate"
PUBLISH_GUARD = "weekly_publish_state"

#: Modules that call `actuate()` for something that is not a published piece, or that are
#: the machinery rather than a caller. Each is listed with its reason, because an
#: unexplained exemption is how a guard test becomes a formality:
#:
#: * `actuators/actuate.py` IS `actuate`; it defines the function.
#: * `agents/nodes/__init__.py` reaches it through `_actuate`, and DOES consult the cap —
#:   it is here only because its call is indirect, and the assertion below pins that it
#:   names the guard anyway.
#: * `services/social_post_service.py` is the calendar path and consults the cap.
#: The set is therefore "known publish callers", and anything NOT in it that calls
#: `actuate` is a new path that has to answer for itself.
PUBLISH_CALLERS = {
    Path("agents") / "nodes" / "__init__.py",
    Path("services") / "social_post_service.py",
}

#: Where the guard itself lives, and the executor that owns `submit`. Neither is a caller:
#: `cost_service` IS the decision, and `run_executor.submit` is the thing being called.
EXEMPT = {
    Path("services") / "cost_service.py",
    Path("services") / "run_executor.py",
}


def _modules() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def _calls_submit(tree: ast.Module) -> bool:
    """Whether this module CALLS something named `submit`.

    A definition does not count — `run_executor` declares the method — so the check is on
    `ast.Call`, and it accepts both shapes in use: `executor.submit(...)` (an attribute on
    an injected object) and `submit(...)` (the callable the scheduler is handed).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == SUBMIT:
            return True
        if isinstance(func, ast.Name) and func.id == SUBMIT:
            return True
    return False


def _calls(tree: ast.Module, name: str) -> bool:
    """Whether this module CALLS something named `name`. A definition does not count."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
        if isinstance(func, ast.Name) and func.id == name:
            return True
    return False


def _mentions(tree: ast.Module, name: str) -> bool:
    """Whether `name` appears as an imported or referenced identifier anywhere."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
        if isinstance(node, ast.ImportFrom) and any(alias.name == name for alias in node.names):
            return True
    return False


def test_every_module_that_starts_a_run_consults_the_monthly_ceiling() -> None:
    offenders: list[str] = []
    submitters: list[str] = []

    for path in _modules():
        relative = path.relative_to(APP_DIR)
        if relative in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _calls_submit(tree):
            continue
        submitters.append(str(relative))
        if not _mentions(tree, GUARD):
            offenders.append(str(relative))

    assert not offenders, (
        f"these modules hand a run to the executor without consulting {GUARD}: "
        f"{offenders}. The per-business monthly ceiling is not the API's rule, it is the "
        "platform's — see docs/ARCHITECTURE.md section 7.4 and cost_service."
    )
    # A guard test that finds nothing to guard is a guard test that has stopped working.
    # Both known entry points must be present: if `submit` is renamed, or a route file is
    # split, this list shrinks and the assertion above starts passing vacuously.
    assert set(submitters) == {"api/runs.py", "worker/scheduler.py"}, (
        f"the set of run-starting modules changed: {sorted(submitters)}. If that is "
        "intended, the new module must consult the ceiling and this list must say so."
    )


def test_the_executor_still_calls_its_entry_point_submit() -> None:
    """The premise of the test above, asserted so it cannot rot silently.

    If `RunExecutor.submit` is renamed, `_calls_submit` finds nothing, every module looks
    innocent, and the guard evaporates without a single failure. This is the cheapest
    possible tripwire for that.
    """
    source = (APP_DIR / "services" / "run_executor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert SUBMIT in names, (
        f"RunExecutor no longer defines `{SUBMIT}`; test_run_start_guard is now blind. "
        "Point SUBMIT at the new entry point."
    )


def test_every_module_that_publishes_a_piece_consults_the_weekly_cap() -> None:
    """`ARCHITECTURE.md` §7.4's volume cap, guarded structurally like the USD one.

    A publish path that does not ask is not a small omission: the cap is the mitigation
    `ROADMAP.md` §10 offers against scaled content abuse, and one unguarded path makes the
    document's claim false rather than partially true.
    """
    offenders: list[str] = []
    callers: list[str] = []

    for path in _modules():
        relative = path.relative_to(APP_DIR)
        if relative == Path("actuators") / "actuate.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _calls(tree, PUBLISH):
            continue
        callers.append(str(relative))
        if not _mentions(tree, PUBLISH_GUARD):
            offenders.append(str(relative))

    assert not offenders, (
        f"these modules call {PUBLISH}() without consulting {PUBLISH_GUARD}: {offenders}. "
        "If the action is not a published piece, that is fine — but say so here, because "
        "a cap enforced on one of two publish paths is advisory."
    )
    assert {Path(caller) for caller in callers} == PUBLISH_CALLERS, (
        f"the set of publish callers changed: {sorted(callers)}. A new one must consult "
        "the weekly cap and be listed in PUBLISH_CALLERS with its reason."
    )


def test_the_executor_wires_the_weekly_cap_counter() -> None:
    """`NodeDeps.published_this_week=None` means the cap is not enforced, so the one place
    that builds real deps must set it.

    This is what makes "unwired" a test's choice rather than a deployment's — the claim
    the field's own docstring makes, asserted rather than trusted. Without it, deleting
    one keyword argument would silently disable the cap in production while every node
    test stayed green, because a node test that does not care about volume deliberately
    leaves the counter out.
    """
    source = (APP_DIR / "services" / "run_executor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wired = any(
        isinstance(node, ast.keyword) and node.arg == "published_this_week"
        for node in ast.walk(tree)
    )
    assert wired, (
        "services/run_executor.py no longer passes `published_this_week` to NodeDeps, so "
        "EXPORT publishes without the weekly volume cap of ARCHITECTURE.md 7.4."
    )
