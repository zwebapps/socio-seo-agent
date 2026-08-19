"""The session cookie's name and its ``Secure`` flag.

Small tests for a small module, but the property they pin is the one that makes
``__Host-`` usable at all: a browser refuses a ``__Host-`` cookie that is not
``Secure``, so a name and a flag that disagree produce a cookie that is silently
never stored. Two functions decide those two things, one line apart, and the last
test here asserts they can never disagree for ANY environment rather than checking
the four we happen to have today.
"""

from typing import get_args

import pytest

from backend.app.core.config import Environment, Settings
from backend.app.core.cookies import (
    HOST_COOKIE_PREFIX,
    SESSION_COOKIE_BASE_NAME,
    cookie_secure,
    session_cookie_name,
)

#: Every value the Environment literal permits, read off the type rather than
#: copied. A new environment added to config.py joins these tests automatically.
ALL_ENVIRONMENTS = get_args(Environment)


def _settings(environment: str) -> Settings:
    return Settings(environment=environment)  # type: ignore[arg-type]


def test_local_gets_the_bare_name_because_it_cannot_be_secure() -> None:
    """Local is plain HTTP on localhost. A prefixed cookie there is a broken login."""
    settings = _settings("local")

    assert cookie_secure(settings) is False
    assert session_cookie_name(settings) == SESSION_COOKIE_BASE_NAME
    assert not session_cookie_name(settings).startswith(HOST_COOKIE_PREFIX)


@pytest.mark.parametrize("environment", [e for e in ALL_ENVIRONMENTS if e != "local"])
def test_every_tls_environment_gets_the_host_prefix(environment: str) -> None:
    settings = _settings(environment)

    assert cookie_secure(settings) is True
    assert session_cookie_name(settings) == f"{HOST_COOKIE_PREFIX}{SESSION_COOKIE_BASE_NAME}"


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
def test_the_prefix_is_used_exactly_when_the_cookie_is_secure(environment: str) -> None:
    """The load-bearing invariant, asserted for every environment that exists.

    ``__Host-`` requires ``Secure``. If these two ever diverge -- a prefix without
    the flag, or an environment that gains TLS without gaining the prefix -- the
    browser drops the ``Set-Cookie`` on the floor and login fails with no error
    anywhere. Comparing the two predicates to each other, rather than each to a
    hardcoded expectation, is what makes this hold for an environment nobody has
    added yet.
    """
    settings = _settings(environment)

    assert session_cookie_name(settings).startswith(HOST_COOKIE_PREFIX) == cookie_secure(settings)


def test_the_base_name_is_never_the_whole_name_where_it_would_be_overwritable() -> None:
    """Restating the point of the prefix as an assertion.

    The un-prefixed name is a cookie a sibling subdomain can overwrite. Outside
    local it must never be the name we set, or the fixation gap the prefix exists to
    close is still open under a name that looks fixed.
    """
    for environment in ALL_ENVIRONMENTS:
        settings = _settings(environment)
        if cookie_secure(settings):
            assert session_cookie_name(settings) != SESSION_COOKIE_BASE_NAME
