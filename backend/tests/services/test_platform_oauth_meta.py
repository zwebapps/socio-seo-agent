"""The real Meta adapter: what it sends, what it refuses, and what it must never print.

Hermetic by construction. HTTP is an injected ``httpx.MockTransport``, so no socket exists
for a request to escape through, and ``backend/tests/conftest.py`` strips ``META_APP_ID``
and ``META_APP_SECRET`` -- the configured cases pass ``env={...}`` or build the provider
with credentials of their own.

Each test here targets a failure that would look fine on a screen:

* **The three-call exchange sends the SHORT-lived token to the long-lived call.** Sending
  the code again, or the long-lived token, both return 200-shaped bodies in a fake and a
  credential that dies in two hours in production.
* **``refresh_token`` carries the access token.** Meta issues no refresh credential, and
  an empty field makes ``refresh_connection`` mark every Meta connection expired at the
  first sweep -- a dashboard full of "reconnect" prompts for connections that are fine.
* **``retryable`` is asserted per status.** It is the field ``refresh_connection``
  branches on: a 429 read as fatal writes ``expired`` on a healthy credential, and a
  refused grant read as retryable leaves a row claiming ``connected`` forever.
* **No credential in any URL, and none in any error message.** ``httpx`` logs request
  URLs at INFO and ``refresh_connection`` logs the exception text, so both are places a
  token leaks silently while every test passes.
* **A half-configured app is not configured.** One variable set would send a customer
  through a real consent dialog and then fail the exchange, leaving a live grant on Meta
  that we never recorded and can never revoke.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qs

import httpx
import pytest

from backend.app.core.token_cipher import Secret
from backend.app.services.platform_oauth import (
    CONNECTABLE_PLATFORMS,
    FakeOAuthProvider,
    OAuthError,
    get_oauth_provider,
    oauth_status,
    real_oauth_platforms,
)
from backend.app.services.platform_oauth_meta import (
    APP_ID_ENV,
    APP_SECRET_ENV,
    AUTHORIZE_ENDPOINT,
    GRAPH_BASE,
    META_PLATFORMS,
    TOKEN_ENDPOINT,
    MetaCredentials,
    MetaOAuthProvider,
    build_meta_provider,
)

APP_ID: Final = "1234567890"
#: Deliberately distinctive strings: every leak assertion below is a substring search, so
#: a value that could occur by accident in a URL or a status line would make the test
#: pass for the wrong reason.
APP_SECRET: Final = "meta-app-secret-zzz-do-not-log"
SHORT_LIVED: Final = "EAAshort-lived-token-aaa"
LONG_LIVED: Final = "EAAlong-lived-token-bbb"
REDIRECT_URI: Final = "https://app.example:8100/api/v1/connections/facebook/callback"
NOW: Final = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

SIXTY_DAYS_SECONDS: Final = 5_184_000


def _credentials() -> MetaCredentials:
    return MetaCredentials(APP_ID, Secret(APP_SECRET))


class Wire:
    """A scripted transport that keeps every request it was given.

    Responses are consumed in order, and running out is an assertion failure rather than a
    default 200: a test that expected three calls and got four would otherwise pass while
    the adapter made a request nobody reviewed.
    """

    def __init__(self, responses: Iterable[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def script(self, response: httpx.Response) -> None:
        """Append one more scripted response."""
        self._responses.append(response)

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            assert self._responses, (
                f"the adapter made an unscripted {request.method} request to {request.url.path}"
            )
            return self._responses.pop(0)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def form(self, index: int) -> dict[str, str]:
        """The form body of request ``index``, flattened."""
        parsed = parse_qs(self.requests[index].content.decode())
        return {key: values[0] for key, values in parsed.items()}


def _ok(**body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


def _error(
    status: int,
    *,
    message: str = "Something went wrong",
    type_: str = "OAuthException",
    code: int = 190,
    fbtrace_id: str = "AbCdEf123",
) -> httpx.Response:
    """Meta's error envelope, verbatim in shape."""
    return httpx.Response(
        status,
        json={
            "error": {
                "message": message,
                "type": type_,
                "code": code,
                "fbtrace_id": fbtrace_id,
            }
        },
    )


def _identity_body() -> dict[str, Any]:
    return {
        "id": "1000000000001",
        "name": "Nordwind Bakery",
        "permissions": {
            "data": [
                {"permission": "pages_manage_posts", "status": "granted"},
                # Declined in the consent dialog. Recording it would put a scope on the
                # connection row that the token does not carry.
                {"permission": "pages_read_engagement", "status": "declined"},
            ]
        },
    }


def _provider(wire: Wire, platform: str = "facebook") -> MetaOAuthProvider:
    return MetaOAuthProvider(platform, _credentials(), client=wire.client(), clock=lambda: NOW)


def _exchange_wire() -> Wire:
    return Wire(
        [
            _ok(access_token=SHORT_LIVED, token_type="bearer", expires_in=3600),
            _ok(access_token=LONG_LIVED, token_type="bearer", expires_in=SIXTY_DAYS_SECONDS),
            _ok(**_identity_body()),
        ]
    )


# --------------------------------------------------------------------------- #
# The happy path, in detail
# --------------------------------------------------------------------------- #


async def test_a_code_becomes_a_long_lived_credential_in_three_calls() -> None:
    """Exchange, upgrade, identify -- and the grant is built from the LAST token."""
    wire = _exchange_wire()

    grant = await _provider(wire).exchange_code(code="the-callback-code", redirect_uri=REDIRECT_URI)

    assert [str(request.url).split("?")[0] for request in wire.requests] == [
        TOKEN_ENDPOINT,
        TOKEN_ENDPOINT,
        f"{GRAPH_BASE}/me",
    ]
    assert grant.access_token.reveal() == LONG_LIVED, "the short-lived token was stored"
    # Meta issues no refresh credential; the long-lived token is what a renewal presents.
    # `None` here makes `refresh_connection` mark the connection expired at the first
    # sweep even though it is renewable.
    assert grant.refresh_token is not None
    assert grant.refresh_token.reveal() == LONG_LIVED
    assert grant.expires_at == NOW + timedelta(seconds=SIXTY_DAYS_SECONDS)
    assert grant.external_account_id == "1000000000001"
    assert grant.external_account_name == "Nordwind Bakery"
    # Only what Meta says was GRANTED. The declined permission is the reason a publish
    # fails long after the connection looked fine, so it must not be recorded as held.
    assert grant.scopes == ("pages_manage_posts",)
    assert grant.fake is False


async def test_the_long_lived_call_presents_the_short_lived_token() -> None:
    """The one ordering mistake a fake cannot catch: both calls hit the same endpoint and
    both answer 200, so sending the code (or the already-long token) a second time yields a
    grant that looks perfect and expires in an hour."""
    wire = _exchange_wire()

    await _provider(wire).exchange_code(code="the-callback-code", redirect_uri=REDIRECT_URI)

    first = wire.form(0)
    assert first["code"] == "the-callback-code"
    assert first["redirect_uri"] == REDIRECT_URI
    assert first["client_id"] == APP_ID
    assert first["client_secret"] == APP_SECRET
    assert "grant_type" not in first

    second = wire.form(1)
    assert second["grant_type"] == "fb_exchange_token"
    assert second["fb_exchange_token"] == SHORT_LIVED
    assert "code" not in second


async def test_the_identity_call_presents_the_token_as_a_bearer_header() -> None:
    """A header, not a query parameter, because httpx logs URLs and headers are not
    logged. Asserted rather than described, since `params=` is the obvious way to write it
    and would leak on every connect."""
    wire = _exchange_wire()

    await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT_URI)

    identity = wire.requests[2]
    assert identity.headers["authorization"] == f"Bearer {LONG_LIVED}"
    assert identity.url.params["fields"] == "id,name,permissions"


async def test_no_credential_ever_reaches_a_request_url() -> None:
    """The whole point of POSTing the token endpoint. `httpx._client` logs
    `HTTP Request: <method> <url>` at INFO, so a secret in a query string is written into
    the application log of every deployment whose logging is at the default level -- a leak
    that no assertion about our own log calls would ever notice."""
    wire = _exchange_wire()
    wire.script(httpx.Response(200, json={"success": True}))
    provider = _provider(wire)

    await provider.exchange_code(code="c", redirect_uri=REDIRECT_URI)
    await provider.revoke(Secret(LONG_LIVED))

    for request in wire.requests:
        url = str(request.url)
        assert APP_SECRET not in url
        assert SHORT_LIVED not in url
        assert LONG_LIVED not in url
        assert "client_secret" not in url
        assert "access_token=" not in url


async def test_the_authorization_url_is_metas_real_dialog() -> None:
    """Pure -- and the one place the real URL differs from the fake's in more than a
    hostname: Meta wants a COMMA-separated scope list."""
    url = _provider(Wire([])).authorization_url(
        redirect_uri=REDIRECT_URI,
        state="a-nonce",
        scopes=("pages_manage_posts", "pages_read_engagement"),
    )

    assert url.startswith(f"{AUTHORIZE_ENDPOINT}?")
    query = parse_qs(url.split("?", 1)[1])
    assert query["client_id"] == [APP_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["a-nonce"]
    assert query["scope"] == ["pages_manage_posts,pages_read_engagement"]
    assert APP_SECRET not in url, "the app secret has no business in a browser"


async def test_instagram_is_the_same_integration_and_not_a_second_one() -> None:
    """One Meta app authorises both, so an Instagram connect must use the same endpoints
    and the same app id -- a separate adapter is how two modules end up disagreeing about
    a 429."""
    wire = _exchange_wire()

    grant = await _provider(wire, platform="instagram").exchange_code(
        code="c", redirect_uri=REDIRECT_URI
    )

    assert [str(request.url).split("?")[0] for request in wire.requests] == [
        TOKEN_ENDPOINT,
        TOKEN_ENDPOINT,
        f"{GRAPH_BASE}/me",
    ]
    assert grant.access_token.reveal() == LONG_LIVED


def test_the_adapter_refuses_a_platform_it_does_not_speak_for() -> None:
    """`get_oauth_provider` routes by platform, and a Meta provider handed LinkedIn would
    send a LinkedIn customer to facebook.com."""
    with pytest.raises(ValueError, match="not a Meta platform"):
        MetaOAuthProvider("linkedin", _credentials())


def test_the_meta_platforms_are_platforms_the_schema_accepts() -> None:
    """`platform_connections.platform` has a CHECK constraint on
    `CONNECTABLE_PLATFORMS`; a typo here would be an insert failure after a real consent
    dialog."""
    assert set(META_PLATFORMS) <= set(CONNECTABLE_PLATFORMS)


# --------------------------------------------------------------------------- #
# Renewal
# --------------------------------------------------------------------------- #


async def test_a_refresh_re_exchanges_the_token_we_hold() -> None:
    """Meta has no refresh grant, so renewal is `fb_exchange_token` again with the
    current long-lived token."""
    renewed = "EAArenewed-token-ccc"
    wire = Wire(
        [
            _ok(access_token=renewed, expires_in=SIXTY_DAYS_SECONDS),
            _ok(**_identity_body()),
        ]
    )

    grant = await _provider(wire).refresh(Secret(LONG_LIVED))

    assert wire.form(0) == {
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "fb_exchange_token": LONG_LIVED,
    }
    assert grant.access_token.reveal() == renewed
    assert grant.refresh_token is not None and grant.refresh_token.reveal() == renewed


async def test_a_refused_refresh_is_not_retryable() -> None:
    """The failure this adapter exists to handle: the user removed the app, or sixty days
    passed. `refresh_connection` writes `expired` only on a NON-retryable error, so a
    retryable verdict here leaves the row claiming `connected` for a credential that will
    never work again."""
    wire = Wire([_error(400, type_="OAuthException", code=190)])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).refresh(Secret(LONG_LIVED))

    assert raised.value.retryable is False
    assert "code=190" in str(raised.value)
    assert "fbtrace=AbCdEf123" in str(raised.value), "the support handle is the useful half"


async def test_a_missing_expiry_is_not_an_invented_one() -> None:
    """Meta omits `expires_in` for tokens it treats as non-expiring. Defaulting to sixty
    days would have the sweep mark a live connection expired; `TokenGrant` documents
    `None` as "no expiry known", which cannot be expired by the clock."""
    wire = Wire([_ok(access_token=LONG_LIVED), _ok(**_identity_body())])

    grant = await _provider(wire).refresh(Secret(LONG_LIVED))

    assert grant.expires_at is None


async def test_a_two_hundred_with_no_token_is_a_failure() -> None:
    """Storing `Secret("")` would be discovered at the first publish, months later."""
    wire = Wire([_ok(token_type="bearer", expires_in=3600)])

    with pytest.raises(OAuthError, match="no access token"):
        await _provider(wire).refresh(Secret(LONG_LIVED))


# --------------------------------------------------------------------------- #
# Failure mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (429, True),
        (500, True),
        (503, True),
        (408, True),
        (400, False),
        (401, False),
        (403, False),
    ],
)
async def test_retryable_is_decided_by_status(status: int, retryable: bool) -> None:
    """`retryable` is the field callers branch on, so 429 and 401 the wrong way round
    means either giving up on a rate limit or hammering a permanent failure."""
    wire = Wire([_error(status)])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT_URI)

    assert raised.value.retryable is retryable
    assert f"HTTP {status}" in str(raised.value)


async def test_a_rate_limited_exchange_is_retryable_and_says_so() -> None:
    """Named separately from the parametrised sweep because this is the one status where a
    wrong answer destroys a healthy connection: Meta rate-limits per app, so a busy hour
    would otherwise expire every connection that happened to renew inside it."""
    wire = Wire([_error(429, type_="OAuthException", code=4, message="rate limit reached")])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT_URI)

    assert raised.value.retryable is True


async def test_an_error_envelope_cannot_leak_the_secret_or_the_token() -> None:
    """Meta's `message` quotes what it objected to, and this exception's text becomes a
    `logger.warning` in `refresh_connection`. So the message field is never repeated --
    only the machine-readable parts, which are what a human can act on."""
    wire = Wire(
        [
            _error(
                400,
                message=(
                    f"Invalid appsecret_proof or client_secret {APP_SECRET} "
                    f"provided for token {LONG_LIVED}"
                ),
            )
        ]
    )

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).refresh(Secret(LONG_LIVED))

    rendered = f"{raised.value}{raised.value!r}"
    assert APP_SECRET not in rendered
    assert LONG_LIVED not in rendered
    assert "Invalid appsecret_proof" not in rendered, "Meta's message must not be repeated"
    assert "type=OAuthException" in rendered


async def test_an_unparseable_error_body_is_still_a_mapped_failure() -> None:
    """Meta serving an HTML error page must be a retryable failure, not a `ValueError`
    from `.json()` escaping as a 500."""
    wire = Wire([httpx.Response(502, text="<html>bad gateway</html>")])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).exchange_code(code="c", redirect_uri=REDIRECT_URI)

    assert raised.value.retryable is True
    assert "type=unknown" in str(raised.value)


async def test_a_transport_failure_is_retryable_and_names_no_url() -> None:
    """A timeout says nothing about whether the grant is good. `str(exc)` on an httpx
    error carries the request URL, which is one refactor away from carrying a secret."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    provider = MetaOAuthProvider(
        "facebook",
        _credentials(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(OAuthError) as raised:
        await provider.exchange_code(code="c", redirect_uri=REDIRECT_URI)

    assert raised.value.retryable is True
    assert "ConnectTimeout" in str(raised.value)
    assert "graph.facebook.com" not in str(raised.value)


async def test_an_empty_code_is_refused_before_any_request() -> None:
    """A callback with no code is our bug or a probe, and either way there is nothing to
    exchange -- sending it would spend a round trip to be told so."""
    wire = Wire([])

    with pytest.raises(OAuthError, match="no authorization code"):
        await _provider(wire).exchange_code(code="   ", redirect_uri=REDIRECT_URI)

    assert wire.requests == []


# --------------------------------------------------------------------------- #
# Revoke
# --------------------------------------------------------------------------- #


async def test_revoke_deletes_every_permission_for_whoever_holds_the_token() -> None:
    """`/me/permissions`, not `/{stored-account-id}/permissions`: the protocol hands this
    method a credential and nothing else, and a stale stored id would de-authorise
    somebody else's grant."""
    wire = Wire([httpx.Response(200, json={"success": True})])

    await _provider(wire).revoke(Secret(LONG_LIVED))

    request = wire.requests[0]
    assert request.method == "DELETE"
    assert str(request.url) == f"{GRAPH_BASE}/me/permissions"
    assert request.headers["authorization"] == f"Bearer {LONG_LIVED}"


async def test_revoking_an_already_dead_token_succeeds() -> None:
    """The protocol says revoke is idempotent, and "Meta has already forgotten this
    credential" is the state a revoke is trying to reach. Raising would make
    `revoke_connection` log a refusal for a disconnect that is, in substance, complete."""
    wire = Wire([_error(400, type_="OAuthException", code=190)])

    await _provider(wire).revoke(Secret(LONG_LIVED))


async def test_a_revoke_meta_actually_refuses_still_raises() -> None:
    """A 500 is not "already gone". Swallowing it would report a disconnect while a live
    token stays authorised on the platform."""
    wire = Wire([_error(500, type_="GraphMethodException", code=100)])

    with pytest.raises(OAuthError) as raised:
        await _provider(wire).revoke(Secret(LONG_LIVED))

    assert raised.value.retryable is True


# --------------------------------------------------------------------------- #
# Selection: which provider a platform actually gets
# --------------------------------------------------------------------------- #

_CONFIGURED: Final = {APP_ID_ENV: APP_ID, APP_SECRET_ENV: APP_SECRET}


def test_a_configured_app_selects_the_real_provider_for_both_meta_platforms() -> None:
    for platform in META_PLATFORMS:
        provider = get_oauth_provider(platform, _CONFIGURED)
        assert isinstance(provider, MetaOAuthProvider)
        assert provider.fake is False
        assert provider.platform == platform


def test_every_other_platform_stays_fake_even_with_a_meta_app() -> None:
    """A Meta app says nothing about LinkedIn, and a provider chosen by anything other
    than its own credential is a provider that can be selected by accident."""
    for platform in CONNECTABLE_PLATFORMS:
        if platform in META_PLATFORMS:
            continue
        assert isinstance(get_oauth_provider(platform, _CONFIGURED), FakeOAuthProvider)


@pytest.mark.parametrize(
    "env",
    [
        {APP_ID_ENV: APP_ID},
        {APP_SECRET_ENV: APP_SECRET},
        {APP_ID_ENV: APP_ID, APP_SECRET_ENV: "   "},
        {APP_ID_ENV: "", APP_SECRET_ENV: APP_SECRET},
        {},
    ],
)
def test_a_half_configured_app_is_not_configured(env: dict[str, str]) -> None:
    """The dangerous middle state. A provider that looks real to every screen and fails on
    the exchange has already sent a customer through a consent dialog, so Meta holds a live
    grant that we never recorded and can therefore never revoke. Blank counts as absent:
    `META_APP_SECRET=` is how a `.env` file unsets a variable.
    """
    assert build_meta_provider("facebook", env) is None
    assert isinstance(get_oauth_provider("facebook", env), FakeOAuthProvider)
    assert real_oauth_platforms(env) == ()


def test_the_status_report_names_the_real_platforms_and_still_names_app_review() -> None:
    """The report is what the settings screen renders. Saying "real provider" without
    saying "publishing is still in Meta's queue" is the support ticket
    `docs/CHANNELS.md` §3 exists to prevent -- a real credential is not a permission to
    post."""
    status = oauth_status(_CONFIGURED)

    assert status.real_providers == ("facebook", "instagram")
    # Still true: linkedin, tiktok, youtube and google_business have no real adapter.
    assert status.using_fake_providers is True
    assert "facebook" in status.blocked_on_app_review
    assert "App Review" in status.message
    assert "linkedin" in status.message
    assert APP_SECRET not in status.message


def test_with_nothing_configured_the_report_is_the_one_it_always_was() -> None:
    """The routes call `oauth_status()` with no argument, and CI has no Meta app, so this
    is the sentence a fresh checkout serves."""
    status = oauth_status({})

    assert status.real_providers == ()
    assert status.using_fake_providers is True
    assert "FakeOAuthProvider" in status.message
    assert "App Review" in status.message


def test_credentials_cannot_print_the_app_secret() -> None:
    """An app secret signs requests for every connected account, so it is as bad in a
    traceback as a token is. `repr` is what a traceback renders."""
    rendered = repr(_credentials())

    assert APP_SECRET not in rendered
    assert APP_ID in rendered, "the id is not a secret and identifies which app is loaded"
