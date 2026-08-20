"""The OAuth ``state`` nonce, carried in a signed ``__Host-`` cookie.

``connection_service.begin_connect`` mints a ``state`` and deliberately does not store
it: its docstring says verification "has to be held wherever the browser's session is",
and this module is that place.

Why a cookie and not a row or a Redis key
-----------------------------------------

``state`` is a **one-shot nonce with a ten-minute life**. A database table for it would
need a sweep, an index, a migration and a story about the rows nobody ever comes back to
collect -- for a value that is meaningless the moment the callback lands. Redis is not a
hard dependency of this API today (it is the queue's, and the queue is not deployed), so
requiring it here would make connecting an account fail on a machine where everything
else works. A signed cookie needs none of that: the browser holds the value, the
signature is what makes it un-forgeable, and the expiry is inside the signed body so an
expired one cannot be un-expired by editing the cookie's ``Max-Age``.

**This cookie IS the CSRF control on the callback route**, which is the standard OAuth
design and is why ``core/csrf.py`` is untouched by any of this. That middleware guards
*cookie-bearing unsafe methods* by checking ``Origin``; an OAuth callback is a ``GET``
arriving as a top-level redirect from a third party, so it carries no ``Origin`` and
never could. Adding it to the ``Origin`` machinery would mean either refusing every real
callback or punching a hole in the middleware's invariant. The nonce comparison answers
the same question the ``Origin`` check answers -- "did this request originate from a flow
this browser actually started?" -- and it answers it with a value the attacker cannot
read or set.

What the signature covers, and why each field is in it
------------------------------------------------------

The body is ``{nonce}.{platform}.{business_id}.{issued_at_epoch}``, and every part is
authenticated:

* **nonce** -- the value the provider echoes back. Comparing it is the whole point.
* **platform** -- so a ``state`` minted for LinkedIn cannot be redeemed at Facebook's
  callback. Without it, one authorisation could be filed against a different platform's
  row.
* **business_id** -- so a flow started by one tenant cannot complete into another's row
  if the session changes mid-flight. The session cookie already decides whose business
  this is; binding it here means the two have to agree.
* **issued_at** -- the TTL. Inside the signature, so it is ours and not the browser's.

``SameSite=Lax`` is required rather than incidental: the callback is a top-level
navigation, and ``Strict`` would withhold the cookie on exactly the one request that
needs it -- the flow would fail for every user, always. ``Lax`` still refuses to attach
it to a cross-site ``POST``, which is all this cookie needs.

The ``__Host-`` prefix and the local-development exception are resolved through
``core.cookies.cookie_secure``, the same predicate the session cookie's name is chosen
by. They must not drift: ``__Host-`` requires ``Secure``, so a prefixed name in an
environment that cannot set ``Secure`` produces a cookie the browser silently throws
away, and connecting a platform would fail on a developer's laptop only.

Not reusing ``core.security.sign_session``, and why
---------------------------------------------------

That function signs a ``UUID`` subject with a thirty-day session life, and its verifier
returns a ``VerifiedSession``. This is a random string with a ten-minute life and two
extra bound fields. Bending one into the other would mean either widening the session
signer's shape for a caller that is not a session, or encoding a nonce as a fake user id.
The primitive is copied instead of the function: ``hmac`` + SHA-256 over a dotted body,
compared with :func:`hmac.compare_digest`, opaque rather than a JWT -- the same three
decisions, for the same reasons, recorded in ``core/security.py``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from fastapi import Response

from backend.app.core.config import Settings
from backend.app.core.cookies import HOST_COOKIE_PREFIX, cookie_secure

__all__ = [
    "STATE_COOKIE_BASE_NAME",
    "STATE_TTL",
    "VerifiedState",
    "clear_state_cookie",
    "nonce_matches",
    "read_state_cookie",
    "set_state_cookie",
    "sign_state",
    "state_cookie_name",
    "verify_state",
]

#: The cookie name before any prefix. Never used bare as a cookie name --
#: :func:`state_cookie_name` decides whether it is prefixed, exactly as
#: ``core.cookies.session_cookie_name`` does for the session.
STATE_COOKIE_BASE_NAME: Final = "sma_oauth_state"

#: How long a pending authorisation stays redeemable. Long enough for a human to read a
#: consent screen and pick which Page to grant, short enough that an abandoned flow's
#: nonce is worthless by the time anybody could find it. A one-shot value with a long
#: life is a one-shot value in name only.
STATE_TTL: Final = timedelta(minutes=10)

#: Tolerated forward clock skew, matching ``core.security``'s. A cookie stamped further
#: ahead than this is refused rather than trusted: without the check, one clock jump
#: would mint state cookies that outlive their TTL.
_CLOCK_SKEW: Final = timedelta(minutes=5)


def state_cookie_name(settings: Settings) -> str:
    """The state cookie's name in this environment.

    ``__Host-``-prefixed wherever the cookie can be ``Secure``; bare in local
    development, where it cannot be and where a prefixed cookie would simply be dropped.
    Keyed off the same predicate as the session cookie so the two cannot disagree about
    what this environment supports.
    """
    if cookie_secure(settings):
        return f"{HOST_COOKIE_PREFIX}{STATE_COOKIE_BASE_NAME}"
    return STATE_COOKIE_BASE_NAME


def _signature(body: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_state(
    *,
    nonce: str,
    platform: str,
    business_id: UUID,
    issued_at: datetime,
    secret: str,
) -> str:
    """Return the signed cookie value for one pending authorisation.

    ``nonce`` is what ``begin_connect`` generated -- ``secrets.token_urlsafe``, so it
    contains no ``.`` and the dotted body cannot be made ambiguous by it. ``platform``
    comes from ``CONNECTABLE_PLATFORMS`` and a ``UUID`` renders without dots either, so
    the split in :func:`verify_state` is exact rather than best-effort.
    """
    stamped = issued_at if issued_at.tzinfo is not None else issued_at.replace(tzinfo=UTC)
    body = f"{nonce}.{platform}.{business_id}.{int(stamped.timestamp())}"
    return f"{body}.{_signature(body, secret)}"


@dataclass(frozen=True, slots=True)
class VerifiedState:
    """What a good state cookie proves: which flow, for which platform and tenant."""

    nonce: str
    platform: str
    business_id: UUID
    #: UTC, whole seconds -- the resolution the cookie carries.
    issued_at: datetime


def verify_state(value: str, *, secret: str, now: datetime | None = None) -> VerifiedState | None:
    """Return the flow this cookie describes, or ``None`` if it is not usable.

    ``None`` covers every failure -- malformed, forged, re-dated, expired. The caller
    answers all of them with one refusal, for the same reason ``api.auth`` answers every
    bad session with one 401: distinguishing them tells whoever is probing which half of
    a guess was right.

    The signature is checked BEFORE anything in the body is parsed or believed, with
    :func:`hmac.compare_digest` -- a ``==`` on a MAC short-circuits on the first wrong
    byte and leaks, one byte at a time, how much of a forgery was correct.
    """
    parts = value.split(".")
    if len(parts) != 5:
        return None

    nonce, platform, raw_business_id, raw_issued_at, signature = parts
    expected = _signature(f"{nonce}.{platform}.{raw_business_id}.{raw_issued_at}", secret)
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        return None

    # Reached only for a value this server signed, so the parsing below is defensive
    # rather than adversarial -- but it still must not raise.
    try:
        business_id = UUID(raw_business_id)
        issued_at = datetime.fromtimestamp(int(raw_issued_at), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None

    if not nonce or not platform:
        return None

    moment = now if now is not None else datetime.now(UTC)
    if issued_at - moment > _CLOCK_SKEW:
        return None
    if moment - issued_at > STATE_TTL:
        return None

    return VerifiedState(
        nonce=nonce, platform=platform, business_id=business_id, issued_at=issued_at
    )


def nonce_matches(presented: str, expected: str) -> bool:
    """Whether the provider echoed back the nonce this browser was issued.

    :func:`hmac.compare_digest` rather than ``==``. The nonce is not a MAC, but it is a
    secret being compared against a caller-supplied value, and a short-circuiting
    comparison on one of those is the same class of leak.
    """
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def set_state_cookie(response: Response, *, value: str, settings: Settings) -> None:
    """Attach the pending-authorisation cookie.

    ``samesite="lax"`` is load-bearing, not a default copied from the session cookie:
    the callback is a top-level navigation from the provider, and ``strict`` would
    withhold this cookie on precisely that request -- every connect would fail, for
    everyone, with no error to read.

    **There is no ``domain`` argument, and there must never be one.** Same rule as the
    session cookie: omitting it makes the cookie host-only, and the ``__Host-`` prefix
    has the browser enforce that rather than this docstring.
    """
    response.set_cookie(
        key=state_cookie_name(settings),
        value=value,
        max_age=int(STATE_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
        path="/",
    )


def clear_state_cookie(response: Response, settings: Settings) -> None:
    """Expire the cookie, with the same attributes it was set with.

    A browser only replaces a cookie when name, path and domain all match, so these are
    not decoration: get them wrong and a consumed nonce stays in the browser.
    """
    response.delete_cookie(
        key=state_cookie_name(settings),
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
    )


def read_state_cookie(cookies: dict[str, str], settings: Settings) -> str | None:
    """The raw cookie value, or ``None``. One place that knows the name."""
    return cookies.get(state_cookie_name(settings))
