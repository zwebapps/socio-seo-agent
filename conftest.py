"""Repo-wide pytest configuration.

Its whole job is to guarantee the suite is hermetic.

`backend/app/main.py` loads `.env` into the process environment at import, because
the provider seams read `os.environ` and pydantic-settings only populates its own
Settings object -- without that load, a real key in `.env` is invisible to the running
app. That fix creates a hazard for tests: a developer's real credential would become
visible to any test that builds a real router, and the suite would start making paid
network calls that pass locally and fail in CI.

So the keys are stripped here, before anything is imported. A test that wants a
configured provider passes `env={...}` explicitly, which is how every provider seam in
this project is written.
"""

import os

import pytest

#: Every credential that would cause a real outbound call if it leaked into a test.
_CREDENTIALS = (
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "RESEND_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)

for _name in _CREDENTIALS:
    os.environ.pop(_name, None)

# Settings reads `.env` by default. Point it at a file that does not exist so a real
# key cannot arrive that way either.
os.environ["ENV_FILE"] = ".env.testing-does-not-exist"


@pytest.fixture(autouse=True)
def _no_credentials_leaked() -> None:
    """Fail loudly if a test puts a real credential back into the environment.

    Without this the strip above is a one-time cleanup that any fixture could undo.
    """
    for name in _CREDENTIALS:
        assert name not in os.environ, (
            f"{name} is set during the test suite. Tests must never hold a real "
            "credential: pass env={...} to the provider seam instead."
        )
