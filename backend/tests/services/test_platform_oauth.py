"""Where a fake provider sends a human, and why that address matters.

The rest of :mod:`backend.app.services.platform_oauth` is exercised through the lifecycle
(``tests/services/test_connection_service.py``) and the routes
(``tests/api/test_connections_api.py``). What only a unit test can pin down is the shape of
the authorization URL itself, which is the whole content of the A3-vi fix: it used to point
at ``fake-oauth.invalid`` -- reserved by RFC 2606, so no browser could ever reach the
callback -- and every route below it was therefore correct and un-completable by a person.

Hermetic by construction: building an authorization URL makes no request, which is the
first thing :class:`OAuthProvider` promises about it.
"""

from urllib.parse import parse_qs, urlsplit

from backend.app.services.platform_oauth import (
    FAKE_CONSENT_PATH_TEMPLATE,
    FakeOAuthProvider,
    fake_consent_path,
)

PLATFORM = "linkedin"
REDIRECT_URI = f"https://app.example:8100/api/v1/connections/{PLATFORM}/callback"


def test_the_consent_screen_is_on_the_same_origin_as_the_callback() -> None:
    """Not cosmetic, and not a free choice: the ``state`` cookie is host-only, so a consent
    screen on any other origin means the browser does not present it on the way back and
    the check it feeds could only ever refuse. The origin is taken from the ``redirect_uri``
    the caller computed from configuration -- never from a request header, which a poisoned
    ``Host`` could use to choose where a customer is sent.
    """
    url = FakeOAuthProvider(PLATFORM).authorization_url(
        redirect_uri=REDIRECT_URI, state="a-nonce", scopes=("w_member_social",)
    )

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == "https://app.example:8100"
    assert parts.path == fake_consent_path(PLATFORM)
    assert "invalid" not in parts.netloc, "an unreachable consent screen was the bug"


def test_the_url_carries_what_the_consent_screen_and_the_callback_both_need() -> None:
    """The real OAuth parameters, because the point of a fake is that the shape around it is
    the shape a real adapter slots into. ``state`` travelling in the URL is load-bearing:
    the consent screen echoes it from there, and the callback compares it against the signed
    cookie the browser holds."""
    url = FakeOAuthProvider(PLATFORM).authorization_url(
        redirect_uri=REDIRECT_URI, state="a-nonce", scopes=("w_member_social", "r_liteprofile")
    )

    query = parse_qs(urlsplit(url).query)
    assert query["state"] == ["a-nonce"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["w_member_social r_liteprofile"]
    assert query["client_id"] == [f"fake-{PLATFORM}-app"]


def test_a_relative_callback_falls_back_to_an_address_that_can_never_resolve() -> None:
    """No absolute callback means no origin to host a consent screen on, so there is nowhere
    real to send anybody. ``.invalid`` is reserved by RFC 2606, so the fallback cannot
    accidentally reach a service -- it is a fallback for a caller no route here has, and it
    is deliberately NOT the default any more, because an unreachable address is exactly what
    made a connect impossible to complete."""
    url = FakeOAuthProvider(PLATFORM).authorization_url(
        redirect_uri="/api/v1/connections/linkedin/callback",
        state="a-nonce",
        scopes=("w_member_social",),
    )

    assert urlsplit(url).netloc == "fake-oauth.invalid"


def test_the_consent_path_names_the_platform_as_a_path_segment() -> None:
    """A path segment and not a query parameter, so the route can resolve the provider for
    that platform and refuse to render at all unless it is the fake one -- which is the guard
    that keeps the page from being a back door around a real integration."""
    assert fake_consent_path("facebook") == FAKE_CONSENT_PATH_TEMPLATE.format(platform="facebook")
    assert fake_consent_path("facebook").endswith("/facebook/simulated-consent")
