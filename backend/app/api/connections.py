"""Connect a platform account, and disconnect it: the HTTP layer the store never had.

Everything below this route module already existed and was tested -- the encrypted store
(``db/adapters/connection_store.py``), the cipher (``core/token_cipher.py``), the OAuth
seam and its fake (``services/platform_oauth.py``) and the lifecycle
(``services/connection_service.py``). None of it was reachable, so no business could
connect an account even to the fake provider, and ``actuators/social.py`` -- whose
refusals are written entirely around a connection's state -- could only ever refuse.
This module is the missing four routes and nothing more.

Five rules shape it.

**The ``state`` nonce lives in a signed cookie, and comparing it IS the CSRF control on
the callback.** An OAuth callback is a ``GET`` arriving as a top-level redirect from a
third party, so it carries no ``Origin`` and ``core/csrf.py`` -- which guards
cookie-bearing *unsafe methods* by origin -- neither covers it nor should be made to. The
reasoning, the signed body and the ``__Host-`` prefix are in ``api/oauth_state.py``.

**A credential is write-only to the outside.** :class:`ConnectionOut` carries
``credentialHint`` -- ``mask_secret``'s four-and-four form -- and there is no field on it
that could hold a token, because it is projected from ``ConnectionView``, which has none
either. That is a property of the types rather than of the care taken here.

**Nothing is sent to a provider we cannot store the answer to.** If the cipher refuses
(no ``PLATFORM_CREDENTIAL_KEY``), starting a connect is a 503 that quotes the cipher's own
reason, *before* a human is sent off to authorise an account whose token we would then
have to throw away. The callback re-checks, because the key can be removed between the
two halves of a flow.

**The fake provider is stated, never hidden.** ``platform_oauth.oauth_status()`` is
surfaced verbatim on the list response, and every connection carries ``fake``. A
connection made against ``FakeOAuthProvider`` must not be able to look like one made
against Meta -- the same rule the model router follows for a missing API key.

**The tenant comes from the session.** Same as every other authenticated router here: a
``business_id`` in a path or a body would be an authorisation decision made by the client,
and row-level security is what actually keeps one business's publishing credential away
from another (``tests/db/test_platform_connections.py``).

There is deliberately no route for ``refresh_connection`` yet: a business whose credential
died reconnects, which is what the connect route already does, and an unusable connection
is reported as unusable rather than silently renewed behind a screen nobody has built.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Final, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from backend.app.api import oauth_state
from backend.app.api.runs import current_business
from backend.app.core.config import Settings, get_settings
from backend.app.core.token_cipher import (
    CipherStatus,
    TokenCipher,
    TokenCipherError,
    cipher_status,
)
from backend.app.db.adapters.connection_store import PostgresConnectionStore
from backend.app.services.connection_service import (
    ConnectionStore,
    ConnectionView,
    begin_connect,
    complete_connect,
    revoke_connection,
)
from backend.app.services.platform_oauth import (
    CONNECTABLE_PLATFORMS,
    OAuthError,
    OAuthProvider,
    get_oauth_provider,
    oauth_status,
)

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])

#: The callback path, relative to this router's prefix. Named once because the value has
#: to be byte-identical in the authorization URL and in the token exchange -- OAuth
#: compares ``redirect_uri`` exactly, and a trailing-slash difference between the two
#: halves of a flow is a failure whose message names neither.
CALLBACK_PATH: Final = "/callback"


# --------------------------------------------------------------------------- #
# Dependencies -- functions, so tests can override them
# --------------------------------------------------------------------------- #


class ConnectionStoreWithCipher(ConnectionStore, Protocol):
    """The store, plus the ability to say what protection is actually in force.

    ``ConnectionStore`` is the lifecycle's view of persistence; this adds the one thing an
    HTTP surface needs on top of it, which is being able to tell an operator that a
    credential cannot be stored at all rather than letting them discover it as a failed
    connect.
    """

    @property
    def cipher(self) -> TokenCipher: ...


def get_connection_store(request: Request) -> ConnectionStoreWithCipher:
    """The application's ONE connection store, cached on ``app.state``.

    Cached rather than built per request, and this is not a performance tweak -- it is a
    correctness requirement under the documented local configuration. With
    ``PLATFORM_CREDENTIAL_KEY=ephemeral`` the plaintext lives in the
    :class:`~backend.app.core.token_cipher.EphemeralVaultCipher` *instance*, and the
    database column holds only a handle into it. A store constructed per request would
    therefore build a fresh, empty vault every time: the connect would report success, and
    the very next read would find a handle pointing at nothing. One instance per process
    is what makes the ephemeral cipher behave as documented (gone on restart, present
    until then) instead of gone immediately.

    Built lazily here rather than in ``create_app`` for the reason ``runs.get_executor``
    records: the module that needs the machinery owns it, and the app factory stays free
    of per-feature detail.
    """
    store = getattr(request.app.state, "connection_store", None)
    if isinstance(store, PostgresConnectionStore):
        return store

    store = PostgresConnectionStore()
    request.app.state.connection_store = store
    return store


#: How the route obtains a provider for one platform. A factory rather than a dependency
#: because the platform is a path parameter, and a dependency cannot see one without
#: making every caller pass it twice.
ProviderFactory = Callable[[str], OAuthProvider]


def get_provider_factory() -> ProviderFactory:
    """The real factory. Returns ``FakeOAuthProvider`` for every platform today -- see
    ``platform_oauth``'s module docstring for why no real client is written."""
    return get_oauth_provider


def get_connection_settings() -> Settings:
    """Settings, as a dependency so a test can vary the environment (the cookie's name
    and its ``Secure`` flag both depend on it)."""
    return get_settings()


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConnectionOut(CamelModel):
    """One connection, as a screen shows it. No field here can hold a credential.

    ``usable``/``unusableReason`` are derived from
    :meth:`ConnectionView.unusable_reason`, the same function the publish actuator asks --
    so the sentence a customer reads on the settings screen is the sentence the refusal
    would carry, and the two cannot drift into disagreeing about whether an account is
    connected.
    """

    platform: str
    external_account_id: str
    external_account_name: str | None
    scopes: list[str]
    status: str
    expires_at: str | None
    #: Four leading and four trailing characters of the credential (``mask_secret``).
    #: Enough for a human to match this row against a token they hold, and useless to
    #: anyone else. The credential itself is never returned by any route.
    credential_hint: str
    #: Which cipher wrote the envelope, so an operator can see that ``v1.ephemeral`` is
    #: not ``v1.aesgcm``.
    credential_scheme: str
    has_credential: bool
    #: True when the grant came from ``FakeOAuthProvider``. Carried to the screen because
    #: a connection that was never made to a real platform must not look like one that
    #: was.
    fake: bool
    usable: bool
    unusable_reason: str | None
    needs_renewal: bool


class OAuthStatusOut(CamelModel):
    """``platform_oauth.oauth_status()``, verbatim.

    Surfaced rather than summarised: a screen that offers "Connect Instagram" without
    saying that publishing there waits on somebody else's approval queue generates a
    support ticket, and the message is written to be read by a human as-is.
    """

    platforms: list[str]
    real_providers: list[str]
    using_fake_providers: bool
    blocked_on_app_review: list[str]
    message: str


class CredentialStorageOut(CamelModel):
    """What protection a stored credential would actually get, in this process."""

    scheme: str
    protects_at_rest: bool
    can_store_credentials: bool
    message: str


class ConnectionListResponse(CamelModel):
    connections: list[ConnectionOut]
    oauth: OAuthStatusOut
    credential_storage: CredentialStorageOut


class ConnectStartResponse(CamelModel):
    """Where to send the human, and what was asked for.

    ``state`` is deliberately NOT in this body. It is in the signed cookie set alongside
    it, and a client that could read it could also be tricked into echoing it -- which is
    the entire attack the nonce exists to stop.
    """

    platform: str
    authorization_url: str
    scopes: list[str]
    #: True when the URL points at ``fake-oauth.invalid`` rather than a real platform.
    fake: bool


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


#: One refusal for every way a callback can fail its state check -- absent cookie, forged
#: or expired cookie, mismatched nonce, wrong platform, wrong tenant. Distinguishing them
#: in the response would tell whoever is probing which half of a guess was right, and no
#: legitimate caller can act on the difference: the fix is always "start the connect
#: again". The specific reason is logged server-side instead.
_STATE_REFUSED: Final = _error(
    "oauth_state_refused",
    "That authorisation could not be matched to a connection you started, so it was "
    "refused. Please start connecting the account again.",
)


def _out(view: ConnectionView, *, now: datetime | None = None) -> ConnectionOut:
    moment = now if now is not None else datetime.now(UTC)
    unusable = view.unusable_reason(now=moment)
    return ConnectionOut(
        platform=view.platform,
        external_account_id=view.external_account_id,
        external_account_name=view.external_account_name,
        scopes=list(view.scopes),
        status=view.status.value,
        expires_at=view.expires_at.isoformat() if view.expires_at is not None else None,
        credential_hint=view.credential_hint,
        credential_scheme=view.credential_scheme,
        has_credential=view.has_credential,
        fake=view.fake,
        usable=unusable is None,
        unusable_reason=unusable,
        needs_renewal=view.needs_renewal(now=moment),
    )


def _status_out() -> OAuthStatusOut:
    reported = oauth_status()
    return OAuthStatusOut(
        platforms=list(reported.platforms),
        real_providers=list(reported.real_providers),
        using_fake_providers=reported.using_fake_providers,
        blocked_on_app_review=list(reported.blocked_on_app_review),
        message=reported.message,
    )


def _storage_out(reported: CipherStatus) -> CredentialStorageOut:
    return CredentialStorageOut(
        scheme=reported.scheme,
        protects_at_rest=reported.protects_at_rest,
        can_store_credentials=reported.can_store_credentials,
        message=reported.message,
    )


def _known_platform(platform: str) -> str:
    """``platform``, or 404.

    404 rather than 422 because the path names a resource that does not exist. The known
    list is ours, not the caller's, so echoing it is safe and saves a round trip to the
    docs.
    """
    if platform not in CONNECTABLE_PLATFORMS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_error(
                "unknown_platform",
                "We cannot connect that platform. Connectable platforms are: "
                + ", ".join(CONNECTABLE_PLATFORMS)
                + ".",
            ),
        )
    return platform


def _redirect_uri(settings: Settings, platform: str) -> str:
    """The absolute callback URL, built from configuration and never from the request.

    ``Host`` is caller-controlled, so deriving this from the request would let a poisoned
    header send a customer's authorisation code to somebody else's server --
    ``api/links.py`` records the same reasoning for a tracked link's absolute URL. The
    value must also be byte-identical in the authorize step and the exchange, which is
    the second reason it is computed in one place.
    """
    base = settings.public_base_url.rstrip("/")
    return f"{base}{router.prefix}/{platform}{CALLBACK_PATH}"


def _refuse_state(reason: str, *, platform: str, business_id: UUID) -> HTTPException:
    """Log why, answer with the one refusal. See :data:`_STATE_REFUSED`."""
    logger.warning(
        "oauth callback refused: business=%s platform=%s reason=%s",
        business_id,
        platform,
        reason,
    )
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_STATE_REFUSED)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=ConnectionListResponse,
    response_model_by_alias=True,
    summary="Platform accounts this business has connected, and what connecting can do",
)
async def list_connections(
    store: Annotated[ConnectionStoreWithCipher, Depends(get_connection_store)],
    business_id: Annotated[UUID, Depends(current_business)],
    response: Response,
) -> ConnectionListResponse:
    """``no-store``: an account name and a credential hint are customer data behind a
    session cookie. Same rule as the leads list, the runs list and the documents list."""
    response.headers["Cache-Control"] = "no-store"
    views = await store.views(business_id=business_id)
    return ConnectionListResponse(
        connections=[_out(view) for view in views],
        oauth=_status_out(),
        credential_storage=_storage_out(cipher_status(store.cipher)),
    )


@router.post(
    "/{platform}/connect",
    response_model=ConnectStartResponse,
    response_model_by_alias=True,
    summary="Start connecting one platform: returns where to send the human",
)
async def start_connect(
    platform: str,
    store: Annotated[ConnectionStoreWithCipher, Depends(get_connection_store)],
    business_id: Annotated[UUID, Depends(current_business)],
    settings: Annotated[Settings, Depends(get_connection_settings)],
    factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
    response: Response,
) -> ConnectStartResponse:
    """Mint the authorization URL and the ``state`` cookie that will validate its callback.

    ``POST`` rather than ``GET``, for two reasons. It has an effect -- it writes a cookie
    that authorises the next callback -- and being a state-changing method puts it under
    ``core/csrf.py``'s origin check, so a third-party page cannot make a victim's browser
    mint a pending authorisation. The callback itself cannot be protected that way (no
    ``Origin`` on a redirect), which is exactly why the nonce this route issues is the
    control there.

    The cipher is checked FIRST. Sending a customer through a consent screen and then
    discovering there is nowhere safe to put the result wastes their time and leaves a
    live grant on the platform that we never recorded and therefore can never revoke.
    """
    _known_platform(platform)

    storage = cipher_status(store.cipher)
    if not storage.can_store_credentials:
        # OURS, not the caller's, and the status says so. The cipher's own sentence names
        # the environment variable and what to set it to.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_error("credential_storage_unavailable", storage.message),
        )

    provider = factory(platform)
    try:
        request_ = begin_connect(provider, redirect_uri=_redirect_uri(settings, platform))
    except ValueError as exc:  # pragma: no cover - _known_platform already refused these
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_error("unknown_platform", str(exc)),
        ) from exc

    oauth_state.set_state_cookie(
        response,
        value=oauth_state.sign_state(
            nonce=request_.state,
            platform=platform,
            business_id=business_id,
            issued_at=datetime.now(UTC),
            secret=settings.session_secret,
        ),
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    logger.info(
        "platform connect started: business=%s platform=%s fake=%s scopes=%s",
        business_id,
        platform,
        provider.fake,
        ",".join(request_.scopes),
    )
    return ConnectStartResponse(
        platform=platform,
        authorization_url=request_.url,
        scopes=list(request_.scopes),
        fake=provider.fake,
    )


@router.get(
    "/{platform}/callback",
    response_model=ConnectionOut,
    response_model_by_alias=True,
    summary="Finish connecting one platform: exchanges the code for a stored credential",
)
async def finish_connect(
    platform: str,
    store: Annotated[ConnectionStoreWithCipher, Depends(get_connection_store)],
    business_id: Annotated[UUID, Depends(current_business)],
    settings: Annotated[Settings, Depends(get_connection_settings)],
    factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
    request: Request,
    response: Response,
    code: Annotated[str, Query(max_length=2048)] = "",
    state: Annotated[str, Query(max_length=512)] = "",
    error: Annotated[str, Query(max_length=256)] = "",
) -> ConnectionOut:
    """Verify the nonce, exchange the code, store the credential.

    The cookie is cleared on EVERY path, success or refusal: ``state`` is a one-shot
    nonce, and a refused callback means the flow has to be restarted rather than retried
    with the same value. That does let a third party who can trigger this route in a
    victim's browser discard an in-flight authorisation -- an inconvenience costing one
    click, taken deliberately over leaving a nonce redeemable after it has been probed.

    ``error`` is the provider's own refusal (a human clicking "cancel", or a scope the
    account cannot grant). Its text is not echoed back: reflecting caller-supplied
    strings into a response body is how an error message becomes an injection vector,
    which is the same rule ``core/csrf.py``'s single refusal body follows.

    ``code`` and ``state`` default to empty rather than being required, so a callback
    missing either is answered by this function's own refusal instead of by a stock 422
    that would say more about our validation than about what went wrong.
    """
    _known_platform(platform)
    oauth_state.clear_state_cookie(response, settings)
    response.headers["Cache-Control"] = "no-store"

    raw_cookie = oauth_state.read_state_cookie(dict(request.cookies), settings)
    if raw_cookie is None:
        raise _refuse_state("no state cookie", platform=platform, business_id=business_id)

    verified = oauth_state.verify_state(raw_cookie, secret=settings.session_secret)
    if verified is None:
        raise _refuse_state(
            "state cookie forged, malformed or expired",
            platform=platform,
            business_id=business_id,
        )
    if verified.platform != platform:
        raise _refuse_state(
            f"state was issued for {verified.platform}",
            platform=platform,
            business_id=business_id,
        )
    if verified.business_id != business_id:
        raise _refuse_state(
            "state was issued for a different business",
            platform=platform,
            business_id=business_id,
        )
    if not state or not oauth_state.nonce_matches(state, verified.nonce):
        raise _refuse_state(
            "the callback did not echo the issued state",
            platform=platform,
            business_id=business_id,
        )

    if error:
        # The nonce checked out, so this IS our flow -- the platform or the human refused
        # it. A 400 with a fixed sentence; the provider's own words are logged, truncated,
        # and never returned.
        logger.info(
            "platform authorisation refused at the provider: business=%s platform=%s error=%.64s",
            business_id,
            platform,
            error,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_error(
                "authorisation_declined",
                f"The {platform} authorisation was not completed, so nothing was "
                "connected. You can start again whenever you are ready.",
            ),
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_error(
                "missing_code",
                "That callback carried no authorisation code, so nothing was connected. "
                "Please start connecting the account again.",
            ),
        )

    try:
        view = await complete_connect(
            store=store,
            provider=factory(platform),
            business_id=business_id,
            code=code,
            redirect_uri=_redirect_uri(settings, platform),
        )
    except OAuthError as exc:
        # Upstream's answer, not our defect: 502, the same reading `api/documents.py`
        # applies to an embeddings provider that did not respond.
        logger.warning(
            "platform token exchange failed: business=%s platform=%s retryable=%s error=%s",
            business_id,
            platform,
            exc.retryable,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_error(
                "exchange_failed",
                f"{platform} did not complete the authorisation, so nothing was "
                "connected. Please try again in a moment.",
            ),
        ) from exc
    except TokenCipherError as exc:
        # The key was removed, replaced or is unusable between starting the flow and
        # finishing it. No row was written -- which is the intended outcome, not a bug to
        # work around: with no key there is nowhere safe to put a token.
        logger.error(
            "platform credential could not be stored: business=%s platform=%s",
            business_id,
            platform,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_error(
                "credential_storage_unavailable",
                cipher_status(store.cipher).message,
            ),
        ) from exc

    return _out(view)


@router.delete(
    "/{platform}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect one platform: revoke at the provider, then forget the credential",
)
async def disconnect(
    platform: str,
    store: Annotated[ConnectionStoreWithCipher, Depends(get_connection_store)],
    business_id: Annotated[UUID, Depends(current_business)],
    factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> Response:
    """Idempotent, and 204 whether or not there was anything to disconnect.

    "Disconnect this account" is a statement about the end state, not about a row, so a
    second call is a success rather than a 404 -- and a customer clicking twice, or
    retrying after a dropped response, must not be told something is wrong. A caller that
    wants to know what exists asks ``GET /api/v1/connections``.

    Another business's connection is simply not there: row-level security scopes the read,
    so the revoke finds nothing and this returns 204 without having touched their row
    (``tests/db/test_platform_connections.py`` proves the database half).

    A credential that will not decrypt -- a rotated key, or the ephemeral vault after a
    restart -- is handled inside ``revoke_connection`` and does not surface here: it
    revokes locally, logs that the platform was not told, and returns normally. This route
    used to catch ``TokenCipherError`` and reach that end state itself, which put the
    decision in the one place that happened to notice rather than in the one place every
    caller goes through.
    """
    _known_platform(platform)

    await revoke_connection(
        store=store,
        provider=factory(platform),
        business_id=business_id,
        platform=platform,
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
