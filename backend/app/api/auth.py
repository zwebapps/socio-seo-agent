"""Authentication routes, and the ``current_user`` dependency every other router
will depend on.

Four rules shape this module.

**Cookie, not a bearer header.** The browser is the client, and a token in
JavaScript's reach is a token an XSS steals. ``httpOnly`` puts it out of reach;
``sameSite=lax`` stops a third-party site from replaying it on a state-changing
request while still surviving a normal top-level navigation back into the app.
``secure`` is on everywhere except local, where it would make the cookie
undeliverable over plain-HTTP localhost. **No ``domain`` attribute** -- see
:func:`_set_session_cookie`; that omission is the whole host-isolation guarantee.

**One answer for every failed login.** Unknown address, wrong password, and
deactivated account all produce the same 401 and the same body. The service makes
them cost the same work too, which is the half a response body cannot fix.

**Requests carry no field constraints, on purpose.** FastAPI's stock 422 includes
the offending ``input`` in the response, so a constraint on ``password`` would
echo passwords into browsers, proxies and logs. Every field therefore defaults to
the empty string and the service raises typed errors instead -- which also keeps
one error shape across the whole module.

**The wire is camelCase**, because a TypeScript client should not have to translate
field names it did not choose.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import Settings, get_settings
from backend.app.core.security import sign_session, verify_session
from backend.app.db.models import User
from backend.app.db.session import session
from backend.app.services import auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

SESSION_COOKIE_NAME: Final = "sma_session"

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
    password: str = ""
    business_name: str = ""


class LoginRequest(CamelModel):
    email: str = ""
    password: str = ""


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


# --------------------------------------------------------------------------- #
# Cookie handling
# --------------------------------------------------------------------------- #


def _cookie_secure(settings: Settings) -> bool:
    """``Secure`` everywhere except local development.

    Local is served over plain HTTP on localhost, where a ``Secure`` cookie is
    simply never sent -- so the flag would not harden anything, it would break
    login. Every other environment terminates TLS.
    """
    return settings.environment != "local"


def _set_session_cookie(response: Response, user_id: UUID, settings: Settings) -> None:
    """Attach a freshly signed session cookie.

    **There is no ``domain`` argument, and there must never be one.** Omitting it
    makes the cookie host-only: the browser sends it back to exactly the host that
    set it. A ``domain=.example.com`` cookie would instead be attached to every
    subdomain, which in a multi-tenant product means the session travelling to
    customer-facing hosts -- a cross-tenant session leak written as one keyword
    argument. ``tests/api/test_auth_api.py`` asserts the attribute is absent.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sign_session(user_id, issued_at=datetime.now(UTC), secret=settings.session_secret),
        max_age=int(SESSION_MAX_AGE.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(settings),
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """Expire the cookie, with the same attributes it was set with.

    A browser only replaces a cookie when name, path and domain all match, so the
    attributes here are not decoration -- get them wrong and logout appears to
    work while the session cookie survives.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(settings),
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
    stamp, unknown user, deactivated user -- is the same 401. A 403 for the
    deactivated case would be worse: it says "we know who you are, and no", which
    turns an account that is switched off into a permissions support ticket.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)

    user_id = verify_session(token, secret=settings.session_secret, max_age=SESSION_MAX_AGE)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)

    user = await auth_service.load_active_user(user_id, session=db)
    if user is None:
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
    response: Response,
    db: Annotated[AsyncSession, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
) -> SignupResponse:
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
    response: Response,
    db: Annotated[AsyncSession, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
) -> UserOut:
    user = await auth_service.authenticate(payload.email, payload.password, session=db)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS)

    _set_session_cookie(response, user.id, settings)
    return UserOut(id=user.id, email=user.email, is_active=user.is_active, role=user.role)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the session cookie",
)
async def logout(settings: Annotated[Settings, Depends(get_auth_settings)]) -> Response:
    """Idempotent, and it does not require a valid session.

    Requiring one would mean an expired cookie could not be cleared, and a
    double-clicked logout would show an error. There is also nothing to protect:
    the only effect is on the caller's own browser.
    """
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response, settings)
    return response


@router.get(
    "/me",
    response_model=UserOut,
    response_model_by_alias=True,
    summary="The signed-in user",
)
async def me(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, email=user.email, is_active=user.is_active, role=user.role)
