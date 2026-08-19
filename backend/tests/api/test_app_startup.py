"""The application refuses to start insecurely.

The auth work left `session_secret` defaulting to a known constant, with nothing
failing if it is left there. Anyone holding that string can mint a session for any
user id — so outside local development, booting with it must be impossible rather
than merely inadvisable. A comment is not a control.
"""

from typing import cast

import pytest

from backend.app.core.config import DEFAULT_SESSION_SECRET, Settings
from backend.app.main import InsecureConfigurationError, create_app


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "environment": "local",
        "session_secret": DEFAULT_SESSION_SECRET,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


def test_local_development_may_use_the_default_secret() -> None:
    """Requiring a real secret to run tests would be friction with no security gain."""
    assert create_app(settings=_settings(environment="local")) is not None


@pytest.mark.parametrize("environment", ["ci", "staging", "production"])
def test_any_non_local_environment_refuses_the_default_secret(environment: str) -> None:
    with pytest.raises(InsecureConfigurationError) as exc:
        create_app(settings=_settings(environment=environment))

    message = str(exc.value)
    assert "SESSION_SECRET" in message
    assert environment in message
    assert DEFAULT_SESSION_SECRET not in message, "the refusal must not print the secret itself"


def test_a_short_secret_is_refused_outside_local() -> None:
    """A 6-character secret is brute-forceable; the HMAC is only as good as its key."""
    with pytest.raises(InsecureConfigurationError):
        create_app(settings=_settings(environment="production", session_secret="short"))


def test_a_real_secret_boots_anywhere() -> None:
    secret = "x" * 48
    assert create_app(settings=_settings(environment="production", session_secret=secret))


def test_cors_allows_every_method_the_app_actually_serves() -> None:
    """A CORS allowlist that omits a method breaks that endpoint from a browser ONLY,
    and it breaks it at the preflight — the server logs nothing and the UI shows a
    generic network error. That happened here with PUT: every admin save failed while
    every test passed, because tests call the ASGI app directly and never preflight.

    Methods come from the OpenAPI schema rather than `app.routes`: this FastAPI version
    wraps included routers in objects that expose no `methods`, so walking app.routes
    finds only the docs endpoints and the check would pass vacuously.
    """
    app = create_app(settings=_settings())

    # openapi() is typed loosely enough that mypy cannot see into the nested dicts,
    # so the shape is asserted once, here, rather than ignored at each access.
    paths = cast("dict[str, dict[str, object]]", app.openapi()["paths"])
    served = {method.upper() for operations in paths.values() for method in operations}
    assert {"GET", "POST", "PUT", "DELETE"} <= served, (
        "the schema walk found almost nothing, so this test would prove nothing"
    )

    cors = next(m for m in app.user_middleware if "CORSMiddleware" in repr(m))
    # Middleware kwargs are `object` to the checker, so narrow once at the boundary.
    configured = cast("list[str]", cors.kwargs["allow_methods"])
    allowed = {method.upper() for method in configured}

    missing = served - allowed
    assert not missing, (
        f"these methods are served but blocked by CORS: {sorted(missing)}. "
        "A browser cannot call them; a test calling the ASGI app directly will not notice."
    )
