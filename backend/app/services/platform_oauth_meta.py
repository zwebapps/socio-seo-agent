"""Meta's OAuth, for real: Facebook Pages and Instagram, one adapter and two platform ids.

The first real implementation of the :class:`~backend.app.services.platform_oauth.
OAuthProvider` seam. Read that module's docstring first — it explains why no real client
existed and what shape one has to slot into. This is that shape filled in for Meta, and
nothing more:

**This is the CONNECTION lifecycle. It does not make publishing work, and must not be read
as claiming so.** ``docs/CHANNELS.md`` §2-3 is unchanged: posting to a Facebook Page or an
Instagram Business account needs Meta App Review — screencast, privacy policy, business
verification, two to six weeks, refusable by form letter. What a customer can do once this
adapter is configured is *authorise us*, and what we can do is hold a renewable credential
and give it back when they disconnect. ``oauth_status()`` still names App Review, because a
real provider does not change whose queue publishing waits in.

Why one adapter for two platforms
---------------------------------
Instagram publishing goes through the Meta Graph API against an Instagram Business account
**linked to a Facebook Page**, authorised by a Facebook login. So there is one app, one
consent dialog, one token endpoint and one revoke endpoint; what differs between
``facebook`` and ``instagram`` is the scope set (``PLATFORM_SCOPES``) and, later, which
node a publish call addresses. Writing two adapters would duplicate every failure mapping
below so that two modules could disagree about a 429.

The two Meta-specific facts that shape the code
-----------------------------------------------
**1. Meta issues no refresh token.** The code exchange returns a SHORT-lived user token
(~1-2 hours); the long-lived one (~60 days) is obtained by a second call with
``grant_type=fb_exchange_token``. Renewal is the same call again, with the current
long-lived token as the input. There is therefore no separate refresh credential, and the
adapter carries the long-lived access token in BOTH :attr:`TokenGrant.access_token` and
:attr:`TokenGrant.refresh_token`. That looks redundant and is load-bearing:
``connection_service.refresh_connection`` reads ``store.reveal_refresh`` and marks a
connection ``expired`` when it is ``None``, so leaving the field empty would have the
sweep declare every Meta connection dead while it was in fact renewable. The field means
"the credential you present in order to renew", and for Meta that is the access token.

**2. Meta expires the credential rather than rejecting a refresh token.** A user who
removes the app, changes their password, or lets 60 days pass produces an
``OAuthException`` (``code`` 190) on the exchange. That is a refusal and never worth
retrying — the business has to reconnect — so it maps to
``OAuthError(retryable=False)``, and only a 429 or a 5xx maps to ``retryable=True``.

Why no credential is ever in a URL
----------------------------------
``httpx`` logs every request at INFO as ``HTTP Request: GET <full url>``. Meta documents
its token endpoint as a GET with ``client_secret`` in the query string, and following that
literally would write the app secret — and, on the renewal call, a live 60-day access
token — into the application log of anyone whose logging is at INFO, which is the default
here. So:

* the token calls are **POSTs with a form-encoded body**. Same endpoint, same parameter
  names, same documented semantics; the parameters simply travel where a logger cannot
  read them.
* the Graph calls that present a token (identity, revoke) use the
  ``Authorization: Bearer`` header, which Meta accepts for any access token.
* :func:`_failure` never interpolates the response body's ``message`` field or the request
  URL. Meta's ``message`` routinely quotes what it objected to, and the same reasoning
  ``actuators/email.py`` records applies: this string becomes a ``logger.warning`` in
  ``connection_service.refresh_connection``.

``test_platform_oauth_meta.py`` asserts the URL property against every request the adapter
makes, rather than trusting this paragraph.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, final
from urllib.parse import urlencode

import httpx

from backend.app.core.token_cipher import Secret
from backend.app.services.platform_oauth import OAuthError, TokenGrant

__all__ = [
    "APP_ID_ENV",
    "APP_SECRET_ENV",
    "GRAPH_VERSION",
    "META_PLATFORMS",
    "MetaCredentials",
    "MetaOAuthProvider",
    "build_meta_provider",
    "meta_credentials",
]

#: The platform ids this one adapter speaks for. A subset of
#: ``platform_oauth.CONNECTABLE_PLATFORMS``, and asserted to be one by the tests: a typo
#: here would silently hand a Meta provider to LinkedIn.
META_PLATFORMS: Final[tuple[str, ...]] = ("facebook", "instagram")

#: Pinned, never "latest". A Graph version is supported for about two years and then
#: starts answering differently; an unpinned client changes behaviour on Meta's schedule
#: rather than on a deploy of ours. Bumping this is a deliberate edit with a read of the
#: changelog behind it.
GRAPH_VERSION: Final = "v21.0"

#: Where the human goes. ``www.facebook.com`` and not ``graph.facebook.com`` — the dialog
#: is a page a person looks at, and the Graph host answers API calls.
AUTHORIZE_ENDPOINT: Final = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"

#: Both token operations: code-for-token and ``fb_exchange_token``. One endpoint, two
#: parameter sets.
TOKEN_ENDPOINT: Final = f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token"

#: Everything else (identity, revoke) hangs off here.
GRAPH_BASE: Final = f"https://graph.facebook.com/{GRAPH_VERSION}"

APP_ID_ENV: Final = "META_APP_ID"
APP_SECRET_ENV: Final = "META_APP_SECRET"  # noqa: S105 -- the NAME of a variable, not one

#: Statuses where the identical request may succeed later. 5xx is handled separately
#: because it is a range; mirrors ``actuators/email.py`` so the two cannot drift into
#: disagreeing about what a 408 means.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 429})

#: Meta's ``code`` for "this token is no longer valid" — expired, revoked by the user, or
#: invalidated by a password change. Named because :meth:`MetaOAuthProvider.revoke` treats
#: it as success: a credential the platform has already forgotten satisfies the request to
#: forget it, and the ``OAuthProvider`` protocol says revoke is idempotent.
OAUTH_EXCEPTION_CODE: Final = 190

DEFAULT_TIMEOUT_S: Final = 10.0

#: What the identity call asks for. ``permissions`` is an edge requested as a field, so
#: one round trip yields both who authorised us and what they actually granted — which is
#: the comparison ``PLATFORM_SCOPES`` exists to make possible (a token issued with a
#: subset is the usual reason a publish fails long after the connection looked fine).
IDENTITY_FIELDS: Final = "id,name,permissions"


class MetaCredentials:
    """The app's own identity at Meta. Not a user credential.

    ``secret`` is a :class:`Secret` rather than a ``str`` so that this object cannot print
    it — an app secret in a traceback is as bad as a token in one, because it signs
    requests on behalf of every connected account.
    """

    __slots__ = ("app_id", "secret")

    def __init__(self, app_id: str, secret: Secret) -> None:
        self.app_id = app_id
        self.secret = secret

    def __repr__(self) -> str:
        return f"MetaCredentials(app_id={self.app_id!r}, secret={self.secret!r})"


def _read(env: Mapping[str, str], name: str) -> str | None:
    """A usable value, treating blank and whitespace-only as absent.

    ``META_APP_SECRET=`` in a ``.env`` file is a common way to "unset" a variable, and
    reading it as present would send an unauthenticated exchange and get a 400 where the
    fake provider was wanted.
    """
    value = env.get(name, "").strip()
    return value or None


def meta_credentials(env: Mapping[str, str] | None = None) -> MetaCredentials | None:
    """The configured Meta app, or ``None`` if this process has no complete one.

    **Both halves or neither.** A configuration with only an id, or only a secret, is a
    misconfiguration, and the safe reading of a misconfiguration is "not configured": the
    alternative is a provider that looks real to every screen and fails on the exchange,
    after a customer has been sent to a consent dialog and granted permissions we then
    cannot collect. Returning ``None`` keeps the fake, which says out loud what it is.
    """
    environ = env if env is not None else os.environ
    app_id = _read(environ, APP_ID_ENV)
    secret = _read(environ, APP_SECRET_ENV)
    if app_id is None or secret is None:
        return None
    return MetaCredentials(app_id, Secret(secret))


@final
class MetaOAuthProvider:
    """Facebook / Instagram OAuth over the Graph API.

    ``client`` is injectable for the same reason it is in ``actuators/email.py`` and
    ``llm/ollama_provider.py``: the tests drive an ``httpx.MockTransport``, so there is no
    socket for a request to escape through and the suite stays hermetic. ``clock`` is
    injected because ``expires_at`` is computed from ``expires_in``, and a test that cannot
    fix "now" can only assert that the expiry is roughly somewhere.
    """

    def __init__(
        self,
        platform: str,
        credentials: MetaCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if platform not in META_PLATFORMS:
            raise ValueError(
                f"{platform!r} is not a Meta platform. This adapter speaks for: "
                f"{', '.join(META_PLATFORMS)}."
            )
        self._platform = platform
        self._credentials = credentials
        self._client = client
        self._timeout_s = timeout_s
        self._now = clock if clock is not None else _utc_now

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def fake(self) -> bool:
        return False

    # ----------------------------------------------------------------- authorize

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: Sequence[str]) -> str:
        """Meta's consent dialog. Pure — asking for this makes no request.

        ``scope`` is COMMA-separated here where ``FakeOAuthProvider`` uses spaces, because
        that is what Meta documents and accepts. It is the one place the real and the fake
        URL differ in more than hostname, so it is stated rather than left to be noticed.
        """
        query = urlencode(
            {
                "client_id": self._credentials.app_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": ",".join(scopes),
                "state": state,
            }
        )
        return f"{AUTHORIZE_ENDPOINT}?{query}"

    # ------------------------------------------------------------------ exchange

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Callback code → a long-lived credential, in three calls.

        Short-lived exchange, then ``fb_exchange_token`` for the ~60-day token, then one
        identity read. All three or none: a failure part-way raises, and no partial row is
        written, because ``complete_connect`` persists only what this returns.

        The honest cost of that: Meta has issued a grant by the time the identity call
        runs, so a failure there leaves a live authorisation we never recorded and can
        therefore never revoke. Recording a connection with an invented account id would be
        worse — it is the row a customer's dashboard then claims to be connected by — so
        the trade is deliberate, and the customer's retry re-authorises.
        """
        if not code.strip():
            raise OAuthError("the callback carried no authorization code")

        short_lived = await self._post_token(
            {
                "client_id": self._credentials.app_id,
                "client_secret": self._credentials.secret.reveal(),
                "redirect_uri": redirect_uri,
                "code": code,
            },
            operation="code exchange",
        )
        return await self._long_lived_grant(short_lived.token, operation="long-lived exchange")

    async def refresh(self, refresh_token: Secret) -> TokenGrant:
        """Renew by re-exchanging the current long-lived token.

        Meta has no refresh grant, so "refresh" is ``fb_exchange_token`` again with the
        credential we hold. A refusal here is the platform saying the connection is dead
        (the user removed the app, or 60 days passed with no use), which is
        ``retryable=False`` — see :func:`_failure`. ``refresh_connection`` turns that into
        an ``expired`` row and a "reconnect" prompt, which is the only useful outcome.
        """
        return await self._long_lived_grant(refresh_token.reveal(), operation="refresh")

    # -------------------------------------------------------------------- revoke

    async def revoke(self, credential: Secret) -> None:
        """``DELETE /me/permissions`` — de-authorise the app for this user.

        ``/me`` rather than ``/{user-id}/permissions`` because the protocol hands this
        method a credential and nothing else, and ``me`` is exactly "whoever this token
        belongs to". It also removes the failure mode where a stale stored account id
        revokes the wrong person's grant.

        Deletes every permission, which is the full disconnect a customer pressing
        "disconnect" is asking for; a scope-by-scope revoke would leave the app installed.
        """
        response = await self._request(
            "DELETE",
            f"{GRAPH_BASE}/me/permissions",
            headers=self._bearer(credential.reveal()),
            operation="revoke",
        )
        if response.status_code >= 400:
            envelope = _envelope(response)
            if envelope.code == OAUTH_EXCEPTION_CODE:
                # Already gone. The protocol promises idempotence, and "the platform has
                # forgotten this credential" is the state a revoke is trying to reach --
                # raising here would make `revoke_connection` log a refusal for a
                # disconnect that is, in substance, complete.
                return
            raise _failure(response, envelope, operation="revoke", platform=self._platform)

    # ------------------------------------------------------------------ internals

    async def _long_lived_grant(self, token: str, *, operation: str) -> TokenGrant:
        """``fb_exchange_token`` plus the identity read, as one grant."""
        exchanged = await self._post_token(
            {
                "grant_type": "fb_exchange_token",
                "client_id": self._credentials.app_id,
                "client_secret": self._credentials.secret.reveal(),
                "fb_exchange_token": token,
            },
            operation=operation,
        )
        identity = await self._identity(exchanged.token, operation=operation)
        return TokenGrant(
            external_account_id=identity.account_id,
            access_token=Secret(exchanged.token),
            # The same token, deliberately: Meta has no refresh credential and this is
            # what a renewal presents. See the module docstring.
            refresh_token=Secret(exchanged.token),
            expires_at=self._expiry(exchanged.expires_in),
            scopes=identity.granted_scopes,
            external_account_name=identity.account_name,
            fake=False,
        )

    def _expiry(self, expires_in: int | None) -> datetime | None:
        """``expires_in`` seconds → an absolute instant, or ``None``.

        ``None`` is not "never expires" and ``TokenGrant`` documents it as "no expiry
        known". Meta omits ``expires_in`` for tokens it treats as non-expiring (a Page
        token, or an app whose user never signs out), and inventing a 60-day expiry for one
        of those would have the sweep mark a live connection expired.
        """
        if expires_in is None or expires_in <= 0:
            return None
        return self._now() + timedelta(seconds=expires_in)

    async def _post_token(self, form: dict[str, str], *, operation: str) -> _TokenResponse:
        """POST the token endpoint and read ``access_token``/``expires_in``.

        A body, not a query string: the form carries the app secret and, on a renewal, a
        live access token, and ``httpx`` logs request URLs at INFO. See the module
        docstring.
        """
        response = await self._request("POST", TOKEN_ENDPOINT, data=form, operation=operation)
        if response.status_code >= 400:
            raise _failure(
                response, _envelope(response), operation=operation, platform=self._platform
            )

        body = _json_object(response, operation=operation, platform=self._platform)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            # A 200 with no token is a failure, not an empty success: the alternative is
            # storing `Secret("")` and discovering it at the first publish.
            raise OAuthError(
                f"Meta's {operation} for {self._platform} returned no access token",
                retryable=False,
            )
        return _TokenResponse(token=token, expires_in=_as_int(body.get("expires_in")))

    async def _identity(self, token: str, *, operation: str) -> _Identity:
        """Who authorised us, and what they actually granted."""
        response = await self._request(
            "GET",
            f"{GRAPH_BASE}/me",
            params={"fields": IDENTITY_FIELDS},
            headers=self._bearer(token),
            operation=operation,
        )
        if response.status_code >= 400:
            raise _failure(
                response, _envelope(response), operation=operation, platform=self._platform
            )

        body = _json_object(response, operation=operation, platform=self._platform)
        account_id = body.get("id")
        if not isinstance(account_id, str) or not account_id:
            raise OAuthError(
                f"Meta identified no account for the {self._platform} connection",
                retryable=False,
            )
        name = body.get("name")
        return _Identity(
            account_id=account_id,
            account_name=name if isinstance(name, str) and name else None,
            granted_scopes=_granted_scopes(body.get("permissions")),
        )

    @staticmethod
    def _bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        operation: str,
    ) -> httpx.Response:
        """One HTTP call, with transport failures mapped and nothing sensitive in the error.

        ``type(exc).__name__`` and not ``str(exc)``: an httpx message carries the request
        URL, and while this adapter keeps credentials out of URLs, an error string that
        quotes a URL is one refactor away from quoting a secret one. The class name
        distinguishes a timeout from a DNS failure, which is all a caller can act on.
        """
        try:
            if self._client is not None:
                return await self._client.request(
                    method, url, params=params, data=data, headers=headers
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.request(method, url, params=params, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthError(
                f"Meta was unreachable during the {self._platform} {operation} "
                f"({type(exc).__name__})",
                retryable=True,
            ) from exc


class _TokenResponse:
    """What the token endpoint said. Never printed — it holds a live token."""

    __slots__ = ("expires_in", "token")

    def __init__(self, token: str, expires_in: int | None) -> None:
        self.token = token
        self.expires_in = expires_in

    def __repr__(self) -> str:
        return f"_TokenResponse(token={Secret(self.token)!r}, expires_in={self.expires_in!r})"


class _Identity:
    """Who the credential belongs to, and what it may do."""

    __slots__ = ("account_id", "account_name", "granted_scopes")

    def __init__(
        self, account_id: str, account_name: str | None, granted_scopes: tuple[str, ...]
    ) -> None:
        self.account_id = account_id
        self.account_name = account_name
        self.granted_scopes = granted_scopes


class _ErrorEnvelope:
    """Meta's ``{"error": {...}}``, minus the one field that must not be repeated.

    ``message`` is deliberately absent from this object rather than merely unused: Meta's
    message quotes what it objected to, this text reaches a log line through
    ``refresh_connection``, and a field that exists is a field a later edit will format.
    """

    __slots__ = ("code", "fbtrace_id", "subcode", "type")

    def __init__(
        self,
        type_: str | None = None,
        code: int | None = None,
        subcode: int | None = None,
        fbtrace_id: str | None = None,
    ) -> None:
        self.type = type_
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id

    def describe(self) -> str:
        """The parts that are safe to log and useful in a support ticket."""
        parts = [
            f"type={self.type or 'unknown'}",
            f"code={self.code if self.code is not None else 'none'}",
        ]
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        if self.fbtrace_id:
            # Meta's own support handle for the failed call. Not a credential and not
            # derived from one, and it is the only thing that makes a Meta ticket
            # answerable.
            parts.append(f"fbtrace={self.fbtrace_id}")
        return " ".join(parts)


def _envelope(response: httpx.Response) -> _ErrorEnvelope:
    """Parse Meta's error envelope, degrading to an empty one.

    An unparseable or unexpected body is not an exception here: the status code already
    carries the decision (:func:`_failure`), and letting a malformed error body raise
    would turn "Meta had a bad minute and served HTML" into a traceback instead of a
    retryable failure.
    """
    try:
        body = response.json()
    except ValueError:
        return _ErrorEnvelope()
    if not isinstance(body, dict):
        return _ErrorEnvelope()
    error = body.get("error")
    if not isinstance(error, dict):
        return _ErrorEnvelope()
    type_ = error.get("type")
    fbtrace = error.get("fbtrace_id")
    return _ErrorEnvelope(
        type_=type_ if isinstance(type_, str) else None,
        code=_as_int(error.get("code")),
        subcode=_as_int(error.get("error_subcode")),
        fbtrace_id=fbtrace if isinstance(fbtrace, str) else None,
    )


def _failure(
    response: httpx.Response,
    envelope: _ErrorEnvelope,
    *,
    operation: str,
    platform: str,
) -> OAuthError:
    """Map an HTTP status onto ``retryable``, which is the field callers branch on.

    The question is "would the identical request succeed later?":

    * **429 — yes.** Meta rate-limits per app and per user, and the window passes.
    * **408, 5xx — yes.** Meta is having a bad minute; the grant is untouched.
    * **400 / 401 / 403 and every other 4xx — no.** A rejected grant: a used or expired
      code, a token the user has revoked, a redirect_uri that does not match the app
      config, a bad app secret. Retrying hammers a permanent failure, and
      ``refresh_connection`` needs the non-retryable answer to write ``expired`` and ask
      the business to reconnect — a retryable verdict there leaves the row claiming
      ``connected`` for a credential that will never work again.
    """
    status = response.status_code
    retryable = status >= 500 or status in RETRYABLE_STATUSES
    return OAuthError(
        f"Meta refused the {platform} {operation}: HTTP {status} ({envelope.describe()})",
        retryable=retryable,
    )


def _json_object(
    response: httpx.Response, *, operation: str, platform: str
) -> Mapping[str, object]:
    """A 2xx body as an object, or a non-retryable failure.

    A success we cannot parse is treated as a failure rather than as empty defaults: the
    defaults would become a connection row with no account and no expiry, which reads on a
    dashboard exactly like a working one.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise OAuthError(
            f"Meta's {operation} for {platform} returned an unreadable body",
            retryable=False,
        ) from exc
    if not isinstance(body, dict):
        raise OAuthError(
            f"Meta's {operation} for {platform} returned an unexpected body shape",
            retryable=False,
        )
    return body


def _granted_scopes(permissions: object) -> tuple[str, ...]:
    """The permissions Meta says are GRANTED, from ``/me?fields=permissions``.

    Only ``status == "granted"`` counts. A user can decline one permission on the consent
    dialog and Meta then reports it as ``declined`` in the same list; recording it as
    granted would put a scope on the connection row that the token does not carry, which
    is the "connection looked fine, publish failed on permissions" case the scope list
    exists to make visible.

    An absent or unexpected shape yields ``()`` — nothing is assumed to have been granted,
    because the requested scopes are not evidence of the granted ones.
    """
    if not isinstance(permissions, dict):
        return ()
    data = permissions.get("data")
    if not isinstance(data, list):
        return ()
    granted: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("permission")
        if isinstance(name, str) and name and entry.get("status") == "granted":
            granted.append(name)
    return tuple(granted)


def _as_int(value: object) -> int | None:
    """An int, or ``None``. Meta sends ``expires_in`` and ``code`` as numbers, but a
    string is accepted because a JSON API that changes a number to a numeric string is a
    common and harmless drift, and crashing on it would be worse than tolerating it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_meta_provider(
    platform: str,
    env: Mapping[str, str] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> MetaOAuthProvider | None:
    """A real Meta provider for ``platform``, or ``None`` if this process cannot make one.

    ``None`` rather than an exception, because "no app configured" is the ordinary state of
    a development machine and the caller's answer to it is the fake provider, not a 500.
    ``platform_oauth.get_oauth_provider`` is the one caller.
    """
    if platform not in META_PLATFORMS:
        return None
    credentials = meta_credentials(env)
    if credentials is None:
        return None
    return MetaOAuthProvider(platform, credentials, client=client, timeout_s=timeout_s)
