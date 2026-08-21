"""LinkedIn's OAuth adapter.

Every test drives an injected `httpx.MockTransport`, so nothing here touches the
network. The cases are chosen for the failures that would otherwise look like a working
connection: a granted scope narrower than the one requested, a refresh on a credential
that was never renewable, a 5xx swallowed as a successful disconnect, and a secret
reaching an exception message.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final

import httpx
import pytest

from backend.app.core.token_cipher import Secret
from backend.app.services.platform_oauth import OAuthError, get_oauth_provider
from backend.app.services.platform_oauth_linkedin import (
    APP_ID_ENV,
    APP_SECRET_ENV,
    LinkedInCredentials,
    LinkedInOAuthProvider,
    build_linkedin_provider,
)

CLIENT_ID: Final = "77abc123linked"
CLIENT_SECRET: Final = "WPL_AP1.sup3r-s3cret-value.xyz"
ACCESS: Final = "AQV8xk-access-token-value"
REFRESH: Final = "AQW9yl-refresh-token-value"
REDIRECT: Final = "http://localhost:8100/api/v1/connections/linkedin/callback"


class Wire:
    """A scripted transport that keeps every request. Mirrors the Meta suite's harness.

    Running out of responses is an assertion failure rather than a default 200: a test
    expecting two calls and getting three would otherwise pass while the adapter made a
    request nobody reviewed.
    """

    def __init__(self, responses: Iterable[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            assert self._responses, (
                f"the adapter made an unscripted {request.method} request to {request.url.path}"
            )
            return self._responses.pop(0)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _provider(wire: Wire) -> LinkedInOAuthProvider:
    return LinkedInOAuthProvider(
        LinkedInCredentials(client_id=CLIENT_ID, client_secret=Secret(CLIENT_SECRET)),
        client=wire.client(),
    )


def _token(**over: object) -> httpx.Response:
    body: dict[str, object] = {
        "access_token": ACCESS,
        "expires_in": 5183999,
        "refresh_token": REFRESH,
        "refresh_token_expires_in": 31536000,
        "scope": "openid,profile,w_member_social",
    }
    body.update(over)
    return httpx.Response(200, json=body)


def _userinfo(**over: object) -> httpx.Response:
    body: dict[str, object] = {"sub": "abc123XYZ", "name": "Müller Sanitär GmbH"}
    body.update(over)
    return httpx.Response(200, json=body)


def _oauth_error(status: int, code: str = "invalid_grant") -> httpx.Response:
    """LinkedIn's RFC 6749 shape, used by the OAuth endpoints."""
    return httpx.Response(
        status,
        json={"error": code, "error_description": f"the {code} description text"},
    )


def _api_error(status: int, service_code: int = 65600) -> httpx.Response:
    """LinkedIn's OTHER shape, used by the API. A reader that knows only the first
    reports "unknown error" for half the real failures."""
    return httpx.Response(
        status,
        json={
            "status": status,
            "message": "Invalid access token",
            "serviceErrorCode": service_code,
        },
    )


# --------------------------------------------------------------------------- #
# authorization_url — pure, no request
# --------------------------------------------------------------------------- #


def test_the_authorization_url_carries_the_state_and_the_scopes() -> None:
    wire = Wire([])
    url = _provider(wire).authorization_url(
        redirect_uri=REDIRECT, state="st-42", scopes=["openid", "w_member_social"]
    )

    assert url.startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert "state=st-42" in url
    assert "scope=openid+w_member_social" in url
    assert f"client_id={CLIENT_ID}" in url
    assert wire.requests == [], "asking for the URL must make no request"


def test_the_authorization_url_never_carries_the_client_secret() -> None:
    """The consent URL goes in a browser's address bar and its history."""
    url = _provider(Wire([])).authorization_url(
        redirect_uri=REDIRECT, state="st", scopes=["openid"]
    )

    assert CLIENT_SECRET not in url


# --------------------------------------------------------------------------- #
# exchange_code
# --------------------------------------------------------------------------- #


async def test_an_exchange_yields_a_grant_with_the_identity_and_the_refresh_token() -> None:
    wire = Wire([_token(), _userinfo()])

    grant = await _provider(wire).exchange_code(code="the-code", redirect_uri=REDIRECT)

    assert grant.external_account_id == "abc123XYZ"
    assert grant.external_account_name == "Müller Sanitär GmbH"
    assert grant.access_token.reveal() == ACCESS
    assert grant.refresh_token is not None
    assert grant.refresh_token.reveal() == REFRESH
    assert grant.fake is False
    assert grant.expires_at is not None and grant.expires_at > datetime.now(UTC)


async def test_the_token_request_is_a_post_with_a_form_body() -> None:
    """httpx logs `HTTP Request: <method> <url>` at INFO, so a GET with query parameters
    writes the client secret — and on renewal a live token — into the application log of
    any default-configured deployment."""
    wire = Wire([_token(), _userinfo()])

    await _provider(wire).exchange_code(code="the-code", redirect_uri=REDIRECT)

    token_request = wire.requests[0]
    assert token_request.method == "POST"
    assert CLIENT_SECRET not in str(token_request.url)
    assert CLIENT_SECRET.encode() in token_request.content, "it belongs in the body"


async def test_the_granted_scopes_are_recorded_not_the_requested_ones() -> None:
    """A member can decline one. Recording what was asked for would make a publish
    attempt look authorised right up to the point the platform refuses it."""
    wire = Wire([_token(scope="openid,profile"), _userinfo()])

    grant = await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.scopes == ("openid", "profile")
    assert "w_member_social" not in grant.scopes


async def test_an_empty_code_is_refused_before_any_request() -> None:
    wire = Wire([])

    with pytest.raises(OAuthError):
        await _provider(wire).exchange_code(code="   ", redirect_uri=REDIRECT)

    assert wire.requests == []


async def test_a_token_response_with_no_access_token_is_a_failure_not_a_grant() -> None:
    wire = Wire([httpx.Response(200, json={"expires_in": 3600})])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT)

    assert raised.value.retryable is False


async def test_a_missing_expiry_reads_as_unknown_rather_than_expired() -> None:
    """`None` means "no expiry known", which `ConnectionView` treats as "cannot be
    expired by the clock". Inventing a lifetime would have the refresh sweep act on it."""
    wire = Wire([_token(expires_in=None), _userinfo()])

    grant = await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.expires_at is None


async def test_userinfo_without_a_subject_is_refused() -> None:
    """A grant with no account id is a connection no publish could ever use."""
    wire = Wire([_token(), _userinfo(sub="")])

    with pytest.raises(OAuthError):
        await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT)


async def test_the_api_error_shape_is_read_and_reported() -> None:
    """The identity call answers with `serviceErrorCode`, not `error`."""
    wire = Wire([_token(), _api_error(401)])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT)

    assert "65600" in str(raised.value), "the code is what LinkedIn's docs are indexed by"
    assert raised.value.retryable is False, "a rejected token is not a blip"


# --------------------------------------------------------------------------- #
# refresh — the failure Meta cannot have
# --------------------------------------------------------------------------- #


async def test_a_refresh_returns_a_new_grant() -> None:
    wire = Wire([_token(access_token="AQV8-rotated"), _userinfo()])

    grant = await _provider(wire).refresh(Secret(REFRESH))

    assert grant.access_token.reveal() == "AQV8-rotated"


async def test_a_refresh_with_no_token_names_the_approval_and_does_not_retry() -> None:
    """LinkedIn issues refresh tokens only to approved applications, so an unapproved
    app holds a credential that was never renewable. Retrying cannot conjure an
    approval, and the message has to say what to do instead — reconnect."""
    wire = Wire([])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).refresh(Secret(""))

    assert raised.value.retryable is False
    assert "reconnect" in str(raised.value).lower()
    assert wire.requests == [], "no point asking LinkedIn about a token we do not have"


async def test_a_rejected_refresh_is_not_retryable() -> None:
    wire = Wire([_oauth_error(400, "invalid_grant")])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).refresh(Secret(REFRESH))

    assert raised.value.retryable is False


async def test_a_429_is_retryable() -> None:
    wire = Wire([_oauth_error(429, "rate_limit")])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).refresh(Secret(REFRESH))

    assert raised.value.retryable is True


# --------------------------------------------------------------------------- #
# revoke — idempotent on a 4xx, never on a 5xx
# --------------------------------------------------------------------------- #


async def test_a_revoke_succeeds() -> None:
    wire = Wire([httpx.Response(200)])

    await _provider(wire).revoke(Secret(ACCESS))

    assert wire.requests[0].method == "POST"


async def test_a_revoke_of_an_already_forgotten_token_is_idempotent() -> None:
    """The state a revoke wants has been reached, and the protocol promises idempotence."""
    wire = Wire([_oauth_error(400, "invalid_request")])

    await _provider(wire).revoke(Secret(ACCESS))  # must not raise


async def test_a_revoke_that_hits_a_5xx_raises_rather_than_claiming_success() -> None:
    """The bug this mirrors, caught by review in the Meta adapter: a 5xx means the
    platform did not answer. Reporting success marks the row revoked and discards the
    only credential that could ever have revoked the grant — so a live authorisation is
    left with nothing able to remove it."""
    wire = Wire([_api_error(503)])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).revoke(Secret(ACCESS))

    assert raised.value.retryable is True


# --------------------------------------------------------------------------- #
# secrets must not leak
# --------------------------------------------------------------------------- #


async def test_no_failure_message_carries_the_secret_or_the_token() -> None:
    for response in (_oauth_error(400), _oauth_error(500), _api_error(401), _api_error(503)):
        wire = Wire([response])
        with pytest.raises(OAuthError) as raised:
            await _provider(wire).refresh(Secret(REFRESH))
        message = str(raised.value)
        assert CLIENT_SECRET not in message
        assert REFRESH not in message
        assert ACCESS not in message


def test_the_credentials_repr_is_masked() -> None:
    """A traceback prints this object. `Secret.__repr__` is what stops it printing the
    live secret."""
    rendered = repr(LinkedInCredentials(client_id=CLIENT_ID, client_secret=Secret(CLIENT_SECRET)))

    assert CLIENT_SECRET not in rendered


# --------------------------------------------------------------------------- #
# selection — which provider a platform actually gets
# --------------------------------------------------------------------------- #

_CONFIGURED: Final = {APP_ID_ENV: CLIENT_ID, APP_SECRET_ENV: CLIENT_SECRET}


def test_the_real_provider_is_selected_only_when_both_values_are_present() -> None:
    assert build_linkedin_provider("linkedin", _CONFIGURED) is not None
    # Half-configured counts as absent: an id with no secret cannot complete an exchange,
    # so it would swap a fake that works for a real one that fails at the callback —
    # AFTER the member has already granted consent.
    assert build_linkedin_provider("linkedin", {APP_ID_ENV: CLIENT_ID}) is None
    assert build_linkedin_provider("linkedin", {APP_SECRET_ENV: CLIENT_SECRET}) is None
    assert build_linkedin_provider("linkedin", {}) is None


def test_blank_values_count_as_absent() -> None:
    """`LINKEDIN_CLIENT_ID=` in an env file is not a configured app."""
    assert build_linkedin_provider("linkedin", {APP_ID_ENV: "  ", APP_SECRET_ENV: "  "}) is None


def test_the_linkedin_builder_answers_none_for_another_platform() -> None:
    """No two builders may claim the same platform, which is what lets
    `get_oauth_provider` chain them without a precedence rule."""
    assert build_linkedin_provider("facebook", _CONFIGURED) is None
    assert build_linkedin_provider("tiktok", _CONFIGURED) is None


def test_get_oauth_provider_returns_the_real_linkedin_provider_when_configured() -> None:
    provider = get_oauth_provider("linkedin", _CONFIGURED)

    assert provider.fake is False
    assert provider.platform == "linkedin"


def test_get_oauth_provider_still_fakes_the_platforms_with_no_adapter() -> None:
    for platform in ("tiktok", "youtube", "google_business"):
        assert get_oauth_provider(platform, _CONFIGURED).fake is True


def test_linkedin_credentials_do_not_select_meta() -> None:
    """The two adapters are chained, and a chain is where one silently shadowing another
    would hide."""
    assert get_oauth_provider("facebook", _CONFIGURED).fake is True
    assert get_oauth_provider("instagram", _CONFIGURED).fake is True
