"""The application refuses to start insecurely.

The auth work left `session_secret` defaulting to a known constant, with nothing
failing if it is left there. Anyone holding that string can mint a session for any
user id — so outside local development, booting with it must be impossible rather
than merely inadvisable. A comment is not a control.
"""

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
