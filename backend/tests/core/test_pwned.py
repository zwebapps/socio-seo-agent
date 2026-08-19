"""``core/pwned``: ask whether a password is breached without disclosing it."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from backend.app.core.pwned import (
    BREACH_THRESHOLD,
    ENV_FLAG,
    PREFIX_LENGTH,
    HttpPwnedPasswordsChecker,
    OfflinePwnedChecker,
    get_checker,
    parse_range_response,
    password_digest,
)

PASSWORD = "correct horse battery staple"


def _digest(password: str) -> tuple[str, str]:
    full = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
    return full[:PREFIX_LENGTH], full[PREFIX_LENGTH:]


# --------------------------------------------------------------------------- #
# k-anonymity: what is allowed to leave this process
# --------------------------------------------------------------------------- #


def test_only_the_first_five_hex_characters_identify_the_query() -> None:
    """The whole privacy claim in one assertion."""
    prefix, suffix = password_digest(PASSWORD)

    assert len(prefix) == PREFIX_LENGTH
    assert (
        prefix + suffix
        == hashlib.sha1(PASSWORD.encode(), usedforsecurity=False).hexdigest().upper()
    )


async def test_the_request_url_contains_the_prefix_and_nothing_else() -> None:
    """Asserted against the actual request, not against the helper.

    A test that only checks `password_digest` would still pass if `breach_count` sent
    the full digest -- which is the one mistake that would silently destroy the
    k-anonymity this module exists to provide.
    """
    prefix, suffix = _digest(PASSWORD)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=f"{suffix}:7")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await HttpPwnedPasswordsChecker(client=client).breach_count(PASSWORD)

    assert len(seen) == 1
    url = seen[0]
    assert url.endswith(f"/{prefix}")
    assert suffix not in url, "the suffix must never be transmitted"
    assert PASSWORD not in url


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_a_matching_suffix_returns_its_count() -> None:
    body = "ABCDE:3\nFFFFF:12\n11111:9"

    assert parse_range_response(body, "FFFFF") == 12


def test_a_missing_suffix_is_zero() -> None:
    """Zero means "not found in the corpus", which is the pass case."""
    assert parse_range_response("ABCDE:3\nFFFFF:12", "99999") == 0


def test_the_comparison_is_case_insensitive() -> None:
    """The API returns uppercase; a caller must not have to know that."""
    assert parse_range_response("abcde:4", "ABCDE") == 4


def test_a_malformed_count_on_our_own_suffix_still_reports_a_breach() -> None:
    """Present is the part that matters; discarding a real hit would be worse."""
    assert parse_range_response("FFFFF:not-a-number", "FFFFF") == BREACH_THRESHOLD


def test_one_malformed_line_does_not_discard_the_rest() -> None:
    """Otherwise a single bad row turns a good lookup into a fail-open no-check."""
    assert parse_range_response("garbage\n\nFFFFF:5", "FFFFF") == 5


# --------------------------------------------------------------------------- #
# Failing open, and doing it deliberately
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("too slow"),
        httpx.HTTPStatusError(
            "500", request=httpx.Request("GET", "https://x"), response=httpx.Response(500)
        ),
    ],
)
async def test_a_lookup_failure_allows_the_password(failure: Exception) -> None:
    """A third party's outage must not stop new customers signing up.

    The offline denylist, the length rule and the distinct-character rule still apply
    as a floor, so failing open loses a bonus check rather than removing the policy.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await HttpPwnedPasswordsChecker(client=client).breach_count(PASSWORD) == 0


async def test_a_non_2xx_response_allows_the_password() -> None:
    """Same rule, arriving as a status rather than an exception."""
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="down"))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await HttpPwnedPasswordsChecker(client=client).breach_count(PASSWORD) == 0


async def test_a_breached_password_is_reported() -> None:
    """The happy path, such as it is."""
    _, suffix = _digest(PASSWORD)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=f"{suffix}:24230577"))

    async with httpx.AsyncClient(transport=transport) as client:
        assert await HttpPwnedPasswordsChecker(client=client).breach_count(PASSWORD) == 24230577


# --------------------------------------------------------------------------- #
# The network is off unless somebody turned it on
# --------------------------------------------------------------------------- #


def test_the_default_checker_touches_no_network() -> None:
    """The rule this whole codebase follows: nothing calls out unless configured to.

    Without this, importing the module would make the test suite and every local run
    depend on a third party being reachable.
    """
    assert isinstance(get_checker(env={}), OfflinePwnedChecker)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_flag_enables_the_real_checker(value: str) -> None:
    assert isinstance(get_checker(env={ENV_FLAG: value}), HttpPwnedPasswordsChecker)


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "maybe"])
def test_anything_else_leaves_it_offline(value: str) -> None:
    """Including a value that merely LOOKS set -- "maybe" must not enable it."""
    assert isinstance(get_checker(env={ENV_FLAG: value}), OfflinePwnedChecker)


async def test_the_offline_checker_reports_nothing_found() -> None:
    """Inconclusive-but-allowed, not "verified safe"."""
    assert await OfflinePwnedChecker().breach_count("hunter2") == 0
