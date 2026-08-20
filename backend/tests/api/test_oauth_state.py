"""The OAuth ``state`` cookie: what it proves, and everything it refuses.

These are the CSRF control on the callback route, so they are tested as a unit rather than
only through the route. A signature check that is right in a happy-path integration test
and wrong in one of the branches below is a callback anybody can complete.

``core/csrf.py`` is deliberately untouched by any of this -- see ``api/oauth_state.py``'s
module docstring for why an ``Origin`` check cannot cover a redirect-borne ``GET``.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.app.api.oauth_state import (
    STATE_COOKIE_BASE_NAME,
    STATE_TTL,
    _signature,
    nonce_matches,
    sign_state,
    state_cookie_name,
    verify_state,
)
from backend.app.core.config import Settings
from backend.app.core.cookies import HOST_COOKIE_PREFIX

SECRET = "a-test-signing-key-of-entirely-adequate-length"
NONCE = "IhcQ7pW1lQd-VbYq0aOZ2xTn5mKrJ8sE4uF6gH9iZjk"
PLATFORM = "linkedin"
BUSINESS = UUID("11111111-1111-1111-1111-111111111111")


def _signed(
    *,
    nonce: str = NONCE,
    platform: str = PLATFORM,
    business_id: UUID = BUSINESS,
    issued_at: datetime | None = None,
    secret: str = SECRET,
) -> str:
    return sign_state(
        nonce=nonce,
        platform=platform,
        business_id=business_id,
        issued_at=issued_at if issued_at is not None else datetime.now(UTC),
        secret=secret,
    )


def test_a_signed_state_round_trips_with_every_bound_field() -> None:
    """All four fields come back, because all four are inside the signature. If any of
    them were merely carried alongside it, an attacker could edit that one."""
    verified = verify_state(_signed(), secret=SECRET)

    assert verified is not None
    assert verified.nonce == NONCE
    assert verified.platform == PLATFORM
    assert verified.business_id == BUSINESS


def test_a_state_signed_with_another_key_is_refused() -> None:
    """The whole point. Without this, anybody could mint a cookie that matches a nonce
    they chose and complete a connect into somebody else's business."""
    assert verify_state(_signed(secret="a-different-key-entirely"), secret=SECRET) is None


def test_editing_the_nonce_invalidates_the_state() -> None:
    """The nonce is authenticated, not just transported: swapping it for a value the
    attacker also put in the callback URL must not produce a match."""
    body = _signed()
    nonce, rest = body.split(".", 1)
    tampered = f"{nonce[:-1]}X.{rest}"

    assert verify_state(tampered, secret=SECRET) is None


def test_editing_the_platform_invalidates_the_state() -> None:
    """Otherwise a LinkedIn authorisation could be filed against the Facebook row."""
    parts = _signed().split(".")
    parts[1] = "facebook"

    assert verify_state(".".join(parts), secret=SECRET) is None


def test_editing_the_business_invalidates_the_state() -> None:
    """The tenancy binding is what stops a flow started by one business completing into
    another's connection row."""
    parts = _signed().split(".")
    parts[2] = str(uuid4())

    assert verify_state(".".join(parts), secret=SECRET) is None


def test_a_state_past_its_ttl_is_refused() -> None:
    """The expiry is inside the signed body, so it cannot be extended by editing the
    cookie's own ``Max-Age`` -- which is the only expiry a browser enforces."""
    stale = _signed(issued_at=datetime.now(UTC) - STATE_TTL - timedelta(seconds=1))

    assert verify_state(stale, secret=SECRET) is None


def test_a_state_inside_its_ttl_is_accepted() -> None:
    """The pair to the test above: a real consent screen takes minutes, and a TTL that
    refused a legitimate flow would be a test nobody could tell from a bug."""
    recent = _signed(issued_at=datetime.now(UTC) - STATE_TTL + timedelta(seconds=30))

    assert verify_state(recent, secret=SECRET) is not None


def test_a_state_stamped_far_in_the_future_is_refused() -> None:
    """A clock that jumped forward once must not mint cookies that outlive the TTL."""
    ahead = _signed(issued_at=datetime.now(UTC) + timedelta(hours=1))

    assert verify_state(ahead, secret=SECRET) is None


def test_rubbish_is_refused_rather_than_raising() -> None:
    """Every one of these arrives from a browser, so none of them may be a 500."""
    for value in ("", ".", "a.b.c.d.e", "not-a-cookie", f"{NONCE}.{PLATFORM}.not-a-uuid.0.ff"):
        assert verify_state(value, secret=SECRET) is None


def test_a_state_with_an_unparseable_timestamp_is_refused() -> None:
    """Signed by us, so the signature passes -- and the parse must still not raise."""
    body = f"{NONCE}.{PLATFORM}.{BUSINESS}.not-a-number"

    assert verify_state(f"{body}.{_signature(body, SECRET)}", secret=SECRET) is None


def test_the_nonce_comparison_answers_correctly_both_ways() -> None:
    assert nonce_matches(NONCE, NONCE)
    assert not nonce_matches(NONCE, NONCE[:-1] + "X")
    assert not nonce_matches("", NONCE)


def test_the_cookie_is_host_prefixed_wherever_it_can_be_secure() -> None:
    """``__Host-`` requires ``Secure``, so the prefix has to be keyed off exactly the
    predicate that decides ``Secure`` -- otherwise the browser silently drops the cookie
    and connecting a platform fails with nothing to read."""
    assert state_cookie_name(Settings(environment="production")) == (
        f"{HOST_COOKIE_PREFIX}{STATE_COOKIE_BASE_NAME}"
    )
    assert state_cookie_name(Settings(environment="staging")).startswith(HOST_COOKIE_PREFIX)


def test_the_cookie_is_unprefixed_in_local_development() -> None:
    """Local is plain HTTP on localhost, where a ``Secure`` cookie is never sent. A
    prefixed name there would not harden anything -- it would break the flow."""
    assert state_cookie_name(Settings(environment="local")) == STATE_COOKIE_BASE_NAME
