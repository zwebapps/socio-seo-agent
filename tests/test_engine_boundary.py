"""The engine boundary is enforced here, not by convention.

Engines are deterministic, read-only computation: they crawl, parse, score and
count. The moment an engine can reach an LLM, a database session, or an actuator,
the architecture in docs/ARCHITECTURE.md section 3 stops being true -- and every
guarantee built on top of it (testability, zero hallucination in scoring,
idempotent side effects, approval policy) quietly stops being true with it.

So this test walks the AST of every module under backend/app/engines/ and fails
the build on a forbidden import. With no engines written yet it passes vacuously,
which is the point: the rule is installed *before* the first engine exists, so it
can never need retrofitting.
"""

import ast
from pathlib import Path

ENGINES_DIR = Path(__file__).resolve().parents[1] / "backend" / "app" / "engines"

# An engine may not reach any of these. The reason differs per group, so they are
# grouped rather than flattened into one opaque list.
FORBIDDEN_PREFIXES: dict[str, str] = {
    # LLM clients -- an engine that can call a model is no longer deterministic.
    "openai": "LLM client",
    "anthropic": "LLM client",
    "langchain": "LLM framework",
    "langgraph": "agent runtime",
    "litellm": "LLM router",
    "ollama": "LLM client",
    # Persistence -- engines return data; the service layer decides what to store.
    "sqlalchemy": "database",
    "alembic": "database migrations",
    "asyncpg": "database driver",
    "psycopg": "database driver",
    "redis": "datastore",
    # Internal layers that sit above or beside engines.
    "backend.app.db": "persistence layer",
    "backend.app.agents": "agent layer",
    "backend.app.actuators": "side-effecting layer",
    "backend.app.tools": "tool registry",
    "backend.app.api": "transport layer",
}


def _engine_modules() -> list[Path]:
    return sorted(ENGINES_DIR.rglob("*.py"))


def _imported_modules(source: str) -> set[str]:
    """Collect every module name imported by a source file, absolute or relative.

    A ``SyntaxError`` is re-raised as a readable failure rather than surfacing as
    an ``ast.py`` traceback -- an unparseable engine is still a broken engine, but
    the report should name the file and line.
    """
    tree = ast.parse(source)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)

    return found


def _violation(module_name: str) -> str | None:
    for prefix, reason in FORBIDDEN_PREFIXES.items():
        if module_name == prefix or module_name.startswith(f"{prefix}."):
            return reason
    return None


def test_engines_directory_exists() -> None:
    """Guard the guard: a renamed directory must not silently disable this test."""
    assert ENGINES_DIR.is_dir(), (
        f"{ENGINES_DIR} is missing. If engines moved, update this test -- do not delete it."
    )


def test_engines_import_nothing_forbidden() -> None:
    violations: list[str] = []

    for path in _engine_modules():
        try:
            imported = _imported_modules(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            relative = path.relative_to(ENGINES_DIR.parents[3])
            violations.append(f"{relative} does not parse: line {exc.lineno}: {exc.msg}")
            continue

        for module_name in sorted(imported):
            reason = _violation(module_name)
            if reason is not None:
                relative = path.relative_to(ENGINES_DIR.parents[3])
                violations.append(f"{relative} imports {module_name} ({reason})")

    assert not violations, (
        "Engines must stay deterministic and side-effect free. Move this logic to a "
        "service, an agent, or an actuator:\n  " + "\n  ".join(violations)
    )
