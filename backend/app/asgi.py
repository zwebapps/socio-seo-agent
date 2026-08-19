"""Process entry point. The ONLY place that touches the environment.

`uvicorn backend.app.asgi:app` starts here; the test suite imports
`backend.app.main` instead and therefore never loads a `.env`.

Why the split exists, because it cost a debugging round to find:

pydantic-settings reads `.env` into its own Settings object and stops there. Every
provider seam in this project reads `os.environ` — deliberately, so a test can pass
`env={...}` rather than monkeypatching a settings singleton. The consequence is that a
real `OPENROUTER_API_KEY` sitting in `.env` was invisible to the router, and the app
silently served every request from the fake provider while looking configured.

Loading `.env` inside `main.py` fixed that and immediately broke something else: the
test suite imports the app, so the keys landed back in `os.environ` after
`conftest.py` had stripped them, and `conftest`'s own guard failed the run. Which was
the guard doing its job — a suite holding a real credential makes paid network calls
that pass on a laptop and fail in CI.

So: importing a module must not mutate the environment. Bootstrap does that, once,
here.
"""

from dotenv import load_dotenv

# override=False so a real environment variable always beats the file, which is what a
# container expects: compose and Kubernetes inject env, and a stale .env baked into an
# image must never win over it.
load_dotenv(override=False)

from backend.app.main import app  # noqa: E402  -- must follow load_dotenv

__all__ = ["app"]
