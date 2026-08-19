"""The evaluation harness: dataset, deterministic rubric, and the runner.

    uv run python evals/run.py            # writes evals/report.md, no network
    uv run python evals/run.py --live     # uses real providers, SPENDS MONEY

This package holds only the harness. Its tests live under ``backend/tests/evals/``
because ``pyproject.toml`` pins ``testpaths = ["backend/tests"]``.

Nothing here is imported by the application: the harness may read the product, and
the product must never read the harness.
"""
