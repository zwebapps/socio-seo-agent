"""Authentication routes, and the ``current_user`` dependency every other router
will depend on.

Four rules shape this module.

**Cookie, not a bearer header.** The browser is the client, and a token in
JavaScript's reach is a token an XSS steals. ``httpOnly`` puts it out of reach;
``sameSite=lax`` stops a third-party site from replaying it on a state-changing
request while still surviving a normal top-level navigation back into the app.
``secure`` is on everywhere except local, where it would make the cookie
undeliverable over plain-HTTP localhost. **No ``domain`` attribute** -- see
:func:`_set_session_cookie`; that omission is the whole host-isolation guarantee,
and the ``__Host-`` prefix now has the browser enforce it rather than us. The name
is therefore environment-dependent and this module exports no name constant:
``core.cookies.session_cookie_name`` is the only way to obtain it, and
``core.csrf`` reads it through the same function.

``sameSite=lax`` is necessary and not sufficient -- it does nothing about a
same-site subdomain or a browser that does not implement it -- so cookie-bearing
writes are additionally checked against an origin allowlist. That check is
middleware, in ``core.csrf``, because a forged request must be refused before it
reaches a dependency, and the reasoning for choosing it over a double-submit token
is recorded there.

**One answer for every failed login.** Unknown address, wrong password, and
deactivated account all produce the same 401 and the same body. The service makes
them cost the same work too, which is the half a response body cannot fix.

**Requests carry almost no field constraints, on purpose.** FastAPI's stock 422
includes the offending ``input`` in the response, so a constraint on ``password``
would once have echoed passwords into browsers, proxies and logs. Every field
therefore defaults to the empty string and the service raises typed errors
instead -- which also keeps one error shape across the whole module.

The single exception is :data:`MAX_PASSWORD_FIELD_CHARS`, added once ``main.py``
grew an app-wide handler that strips the submitted value out of every validation
error. It is a hard transport ceiling, not the password policy: see the constant.

**The wire is camelCase**, because a TypeScript client should not have to translate
field names it did not choose.

Two controls added after the first cut, both of which the routes here are the only
place to apply:

**Session revocation.** ``current_user`` refuses a token issued before the user's
``sessions_valid_from`` watermark, and ``logout`` bumps that watermark. Before
this, logout only cleared the browser's copy and a stolen cookie stayed valid for
its full thirty days.

**Throttling on both credential routes.** Login and signup each run argon2id at
64 MiB, so either one unthrottled is a memory-amplification denial of service --
see ``core.rate_limit`` for the per-IP/per-email windows and the concurrency gate
that bounds the hashing itself.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import rate_limit
from backend.app.core.config import Settings, get_settings
from backend.app.core.cookies import cookie_secure, session_cookie_name
from backend.app.core.proxy_trust import warn_once_if_misconfigured
from backend.app.core.rate_limit import FixedWindowRateLimiter
from backend.app.core.security import (
    session_is_revoked,
    session_issued_at,
    sign_session,
    verify_session,
)
from backend.app.db.models import User
from backend.app.db.session import session
from backend.app.services import auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# There is deliberately no SESSION_COOKIE_NAME constant. The name depends on the
# environment now (``__Host-`` requires ``Secure``, which local development cannot
# have), so a constant would be a name that is right on a laptop and wrong in
# production -- the one failure shape a test suite cannot catch. Every reader,
# including ``core.csrf`` and the test suite, goes through
# ``core.cookies.session_cookie_name``.

#: Hard ceiling on the ``password`` field, in characters.
#:
#: NOT the password policy. ``auth_service.MAX_PASSWORD_LENGTH`` (256) is the policy,
#: and it answers a human with a sentence they can act on -- "please use at most 256
#: characters". This is the ceiling on what the *transport* will carry at all, and it
#: exists because ``login`` had no length bound of any kind: ``authenticate`` handed
#: whatever arrived to argon2, and argon2 hashes any length without complaint.
#:
#: 1024 rather than 256, so the two layers do not collide. At 256 the pydantic error
#: would fire first and a human who typed a slightly-too-long passphrase would get
#: the app-wide redacted "this field is not accepted here" instead of the service's
#: real explanation. Four times the policy limit leaves the friendly message as the
#: one a person actually meets, and reserves the blunt 422 for input that is not a
#: password at all -- a 100-word diceware passphrase is about 600 characters and a
#: password manager's output is well under 128, so nothing legitimate is near it.
#: ``test_the_transport_ceiling_sits_above_the_password_policy`` pins the ordering so
#: the two numbers cannot cross later.
MAX_PASSWORD_FIELD_CHARS: Final = 1024

# Thirty days. Long enough that a weekly user is not logged out between visits,
# short enough that a cookie copied off a shared machine expires within a
# billing cycle. Deactivation does not wait for it: `load_active_user` runs on
# every authenticated request, so switching an account off takes effect on the
# next one.
SESSION_MAX_AGE: Final = timedelta(days=30)


# --------------------------------------------------------------------------- #
# Dependencies -- functions, so tests can override them
# --------------------------------------------------------------------------- #


async def db_session() -> AsyncIterator[AsyncSession]:
    """A database session for the request.

    ``users`` and ``businesses`` are the only two tables that are not
    business-scoped, so the unscoped session is correct here. Anything carrying
    ``business_id`` must go through ``business_session`` instead, or row-level
    security will not be active for it.
    """
    async with session() as active:
        yield active


def get_auth_settings() -> Settings:
    """Settings, as a dependency so a test can vary the environment."""
    return get_settings()


def get_login_limiter() -> FixedWindowRateLimiter:
    """The login throttle.

    A dependency, not a direct call, for one reason that matters beyond testing:
    the limiter is process-wide state with a real memory of who has knocked
    recently. Tests must be able to swap in an isolated one, or the suite's own
    login attempts become each other's rate-limit budget and the failures land in
    whichever test happens to run thirty-first.
    """
    return rate_limit.login_limiter()


def get_signup_limiter() -> FixedWindowRateLimiter:
    """The signup throttle -- its own namespace and its own, tighter policy."""
    return rate_limit.signup_limiter()


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SignupRequest(CamelModel):
    # Defaults rather than constraints: see the module docstring. A missing field
    # must not become a FastAPI validation error, because that error would carry
    # the rest of the body -- including the password -- back to the caller.
    email: str = ""
    # The one constraint, and it is a transport ceiling rather than policy: see
    # MAX_PASSWORD_FIELD_CHARS. Safe to declare now only because `main.py` strips the
    # submitted value out of every validation error; `test_a_rejected_signup_never_
    # echoes_the_password` and its login twin hold that line.
    password: str = Field(default="", max_length=MAX_PASSWORD_FIELD_CHARS)
    business_name: str = ""


class LoginRequest(CamelModel):
    email: str = ""
    #: The reason this constant exists. Login is the route that had no bound at all:
    #: `authenticate` passes the field straight to argon2, so before this a caller
    #: could make the process hash a megabyte.
    password: str = Field(default="", max_length=MAX_PASSWORD_FIELD_CHARS)


class SignupResponse(CamelModel):
    user_id: UUID
    business_id: UUID
    email: str


class UserOut(CamelModel):
    """Deliberately narrow. ``password_hash`` is not in this model, so it cannot
    be leaked by adding a field somewhere else later."""

    id: UUID
    email: str
    is_active: bool
    #: The business this account acts for, or ``None`` for an account that has none.
    #:
    #: ``null`` rather than an error, because an account without a business is a
    #: legitimate state (mid-signup, or one whose business was removed) and it is the
    #: screen's job to say "finish onboarding", not this endpoint's to refuse.
    #:
    #: Exposed because every authenticated screen needs it and none of them had it: the
    #: memory routes derive the tenant from the session instead of taking a path id
    #: like their `proposals` sibling, precisely because the client could not know it.
    business_id: UUID | None = None
    #: Exposed so the UI can tell an operator from a customer and hide what the
    #: customer cannot use anyway. It is NOT the authorisation decision — the server
    #: re-checks the role on every admin call.
    role: str


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


# One string, one place. Anything that reworded it per-branch would immediately
# become an account-enumeration oracle.
_INVALID_CREDENTIALS: Final = _error(
    "invalid_credentials", "That email and password combination is not correct."
)
_NOT_AUTHENTICATED: Final = _error("not_authenticated", "Please sign in to continue.")

# One message for both dimensions and both routes. Saying "too many attempts for
# this account" rather than "from this address" would tell an attacker which limit
# it hit, and therefore which one to route around -- and on signup it would also
# confirm that the address is one somebody keeps trying.
_RATE_LIMITED: Final = _error(
    "rate_limited",
    "Too many attempts. Please wait a moment and try again.",
)


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #


def _client_ip(request: Request) -> str:
    """The peer address, and deliberately NOT ``X-Forwarded-For``.

    Trusting a client-supplied header on an internet-facing route would let an
    attacker put a fresh address in every request and erase the per-IP dimension
    completely -- a throttle that an attacker configures is not a throttle.

    Behind a reverse proxy the correct fix is at the server, not here. Note the
    detail, since it decides whether this works: uvicorn 0.52 already defaults
    ``--proxy-headers`` ON, so the missing piece is ``--forwarded-allow-ips`` (env
    ``FORWARDED_ALLOW_IPS``), which defaults to ``127.0.0.1``. That default is right
    only for a proxy sharing this container's network namespace -- with the proxy as
    its own service, ``X-Forwarded-For`` arrives and is silently discarded, and every
    caller collapses into one bucket.

    That over-limits rather than under-limits, so the failure direction is safe, but
    it is still a total outage of per-client limiting and it announces itself nowhere.
    Hence the warn-once check: ``core.proxy_trust`` compares the header against the
    peer and logs what to change the first time they disagree. It is called from HERE
    -- the code whose correctness depends on the answer -- rather than at startup,
    because the discarded-header case is only observable on a real request.
    """
    client = request.client
    host = client.host if client is not None else None
    warn_once_if_misconfigured(
        client_host=host,
        forwarded_for=request.headers.get("x-forwarded-for"),
    )
    return host if host is not None else "unknown"


async def _throttle(limiter: FixedWindowRateLimiter, request: Request, *, email: str) -> None:
    """Count this attempt and raise 429 if either window is full.

    Called before any argon2 work, which is the entire point: the cost being
    rationed is the 64 MiB hash, so a refusal that happened after it would ration
    nothing.

    ``Retry-After`` is a real header rather than prose in the body, so a client can
    back off correctly without parsing English.
    """
    decision = await limiter.check(
        {
            rate_limit.DIMENSION_IP: _client_ip(request),
            rate_limit.DIMENSION_EMAIL: email,
        }
    )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE_LIMITED,
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


# --------------------------------------------------------------------------- #
# Cookie handling
# --------------------------------------------------------------------------- #


def _set_session_cookie(
    response: Response, user_id: UUID, settings: Settings, *, issued_at: datetime | None = None
) -> None:
    """Attach a freshly signed session cookie.

    ``issued_at`` is how a caller avoids minting a token its own revocation
    watermark would refuse. Because the watermark rounds up to the next whole
    second, it can sit slightly in the future, and a bare ``now()`` inside that
    sliver produces a session that is dead on its next request. ``login`` passes
    :func:`~backend.app.core.security.session_issued_at` for exactly that reason --
    signing in immediately after signing out lands in the sliver -- and a future
    password-change endpoint must do the same with the value ``revoke_sessions``
    returns. Defaulting to ``now()`` keeps ``signup`` unchanged: a brand-new user
    has no watermark.

    **There is no ``domain`` argument, and there must never be one.** Omitting it
    makes the cookie host-only: the browser sends it back to exactly the host that
    set it. A ``domain=.example.com`` cookie would instead be attached to every
    subdomain, which in a multi-tenant product means the session travelling to
    customer-facing hosts -- a cross-tenant session leak written as one keyword
    argument. ``tests/api/test_auth_api.py`` asserts the attribute is absent.

    The name comes from :func:`~backend.app.core.cookies.session_cookie_name`, which
    prefixes it with ``__Host-`` wherever the cookie is ``Secure``. That prefix makes
    the paragraph above enforceable by the browser instead of by this docstring: a
    ``__Host-`` cookie carrying a ``Domain`` is not a weaker cookie, it is a cookie
    the browser refuses to store, and a subdomain cannot overwrite one either. The
    three attributes it requires -- ``Secure``, ``Path=/``, no ``Domain`` -- are all
    set below, and ``cookie_secure`` is the same predicate the name is chosen by, so
    they cannot drift apart into a cookie no browser accepts.
    """
    response.set_cookie(
        key=session_cookie_name(settings),
        value=sign_session(
            user_id,
            issued_at=issued_at if issued_at is not None else datetime.now(UTC),
            secret=settings.session_secret,
        ),
        max_age=int(SESSION_MAX_AGE.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """Expire the cookie, with the same attributes it was set with.

    A browser only replaces a cookie when name, path and domain all match, so the
    attributes here are not decoration -- get them wrong and logout appears to
    work while the session cookie survives.
    """
    response.delete_cookie(
        key=session_cookie_name(settings),
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
    )


# --------------------------------------------------------------------------- #
# The dependency other routers will use
# --------------------------------------------------------------------------- #


async def current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
) -> User:
    """Resolve the caller, or raise 401.

    Every failure -- no cookie, malformed cookie, forged signature, expired
    stamp, unknown user, deactivated user, **revoked session** -- is the same 401.
    A 403 for the deactivated case would be worse: it says "we know who you are,
    and no", which turns an account that is switched off into a permissions
    support ticket.

    The revocation check is what makes ``sessions_valid_from`` mean anything, and
    it necessarily comes last: the watermark lives on the user row, so the token
    has to be believed far enough to look the row up before it can be refused.
    Refusing here rather than in ``verify_session`` also keeps ``core.security``
    free of the database, which is what lets the crypto be tested on its own.
    """
    token = request.cookies.get(session_cookie_name(settings))
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)

    verified = verify_session(token, secret=settings.session_secret, max_age=SESSION_MAX_AGE)
    if verified is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)

    user = await auth_service.load_active_user(verified.user_id, session=db)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)

    if session_is_revoked(verified.issued_at, user.sessions_valid_from):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)

    return user


CurrentUser = Annotated[User, Depends(current_user)]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.post(
    "/signup",
    response_model=SignupResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its first business",
)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
    limiter: Annotated[FixedWindowRateLimiter, Depends(get_signup_limiter)],
) -> SignupResponse:
    # Throttled for the same reason login is, and it is easy to forget because
    # signup does not feel like an attack surface: it runs the same 64 MiB argon2
    # hash, it is equally unauthenticated, and it also writes two rows.
    await _throttle(limiter, request, email=payload.email)

    try:
        result = await auth_service.signup(
            payload.email, payload.password, payload.business_name, session=db
        )
    except auth_service.InvalidEmailError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_error("invalid_email", str(exc))
        ) from exc
    except auth_service.WeakPasswordError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_error("weak_password", exc.reason)
        ) from exc
    except auth_service.InvalidBusinessNameError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_error("invalid_business_name", str(exc))
        ) from exc
    except auth_service.EmailTakenError as exc:
        # 409, and deliberately vague. It cannot avoid revealing that the address
        # is in use -- there is no other honest answer at signup time -- but it
        # does not confirm whose it is. The service docstring records the tension
        # and what closing it properly would take.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=_error(
                "email_taken",
                "We could not create an account with those details. "
                "If you have signed up before, try signing in or resetting your password.",
            ),
        ) from exc

    # Signing up just proved who they are; making them log in again immediately
    # would be ceremony rather than security.
    _set_session_cookie(response, result.user_id, settings)
    return SignupResponse(
        user_id=result.user_id, business_id=result.business_id, email=result.email
    )


@router.post(
    "/login",
    response_model=UserOut,
    response_model_by_alias=True,
    summary="Exchange credentials for a session cookie",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
    limiter: Annotated[FixedWindowRateLimiter, Depends(get_login_limiter)],
) -> UserOut:
    # Before `authenticate`, because what is being rationed is the argon2 hash
    # inside it. Counting the attempt whether or not the password turns out to be
    # right is deliberate: the memory was spent either way.
    await _throttle(limiter, request, email=payload.email)

    user = await auth_service.authenticate(payload.email, payload.password, session=db)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS)

    # The password was right, so this attempt was the account owner. Refund the
    # per-email counter: the 10-per-15-minutes budget exists to ration guessing,
    # and without this a user who signs in from a few devices, or reconnects a few
    # times, spends it on themselves and is locked out of their own account.
    #
    # The per-IP counter is deliberately NOT refunded. It is the flood defence, and
    # refunding it would make one valid credential an unlimited enumeration budget:
    # a stuffing run that finds a single live account would top itself up on every
    # hit. Refunding the email dimension has no equivalent, because a success there
    # is by definition the owner of that address.
    #
    # What this does NOT fix, stated plainly so it is not mistaken for closed: an
    # attacker who knows the address can still burn its window with 10 wrong
    # passwords and lock the owner out for up to 15 minutes. Closing that needs the
    # per-email window to stop being a pre-hash block -- and it is a pre-hash block
    # on purpose, because the cost being rationed is the 64 MiB argon2 hash, which
    # a refusal issued after it would not ration at all. The residual is bounded,
    # self-clearing, and cheaper than the alternative.
    await limiter.give_back({rate_limit.DIMENSION_EMAIL: payload.email})

    # Stamped with `session_issued_at`, not a bare `now()`. A login that lands in
    # the same second as a revocation -- signing in right after signing out is the
    # ordinary way to do that -- would otherwise mint a token its own watermark
    # refuses, and the user would appear logged in until their next request.
    _set_session_cookie(
        response,
        user.id,
        settings,
        issued_at=session_issued_at(user.sessions_valid_from),
    )
    return UserOut(id=user.id, email=user.email, is_active=user.is_active, role=user.role)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the session cookie",
)
async def logout(
    request: Request,
    db: Annotated[AsyncSession, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
) -> Response:
    """Clear the cookie AND revoke every session issued up to now.

    The revocation is the point. Clearing the cookie only edits the caller's own
    browser, which is no help at all in the case logout exists for -- somebody
    else has a copy. Bumping ``sessions_valid_from`` is what makes the copy stop
    working, and it deliberately ends every session for the user rather than only
    this one: the token carries no device identity, so "log out everywhere" is the
    only revocation this design can offer, and it is the safe reading of the
    request when a user logs out from a machine they do not trust.

    Still idempotent, and still does not require a *usable* session: an expired or
    forged cookie is simply cleared and answered 204. Requiring a valid session
    would mean the one cookie you most want gone is the one you cannot clear, and a
    double-clicked logout would show an error page.

    The bump runs for any correctly SIGNED token, before checking whether the user
    exists or is active -- a signature we produced is proof enough to act on, and
    an unknown id updates no rows. Nothing here reveals which of those happened:
    the answer is 204 either way.
    """
    token = request.cookies.get(session_cookie_name(settings))
    if token:
        verified = verify_session(token, secret=settings.session_secret, max_age=SESSION_MAX_AGE)
        if verified is not None:
            await auth_service.revoke_sessions(verified.user_id, session=db)

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response, settings)
    return response


@router.get(
    "/me",
    response_model=UserOut,
    response_model_by_alias=True,
    summary="The signed-in user",
)
async def me(user: CurrentUser, db: Annotated[AsyncSession, Depends(db_session)]) -> UserOut:
    """Who the caller is, and which business they act for.

    The business is resolved with the SAME query `runs.current_business` uses, imported
    rather than repeated -- two lookups answering "whose business is this" is how a
    screen and an authorisation check start disagreeing.
    """
    from backend.app.api.runs import business_for_user

    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        role=user.role,
        business_id=await business_for_user(user.id, session=db),
    )
