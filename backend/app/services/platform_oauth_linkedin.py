"""LinkedIn's OAuth adapter: the second real provider behind the seam.

Deliberately a sibling of :mod:`platform_oauth_meta` rather than a generalisation of it.
The two platforms agree on the four operations and on almost nothing else, and the
differences are the interesting part:

* **LinkedIn issues a refresh token; Meta does not.** Meta hands back a long-lived token
  that you renew by exchanging it for another one (``fb_exchange_token``); LinkedIn hands
  back a real ``refresh_token`` alongside the access token — but ONLY to applications
  approved for it. An unapproved app gets an access token with no refresh, and
  :meth:`LinkedInOAuthProvider.refresh` therefore has a failure mode Meta's does not:
  "this credential was never renewable in the first place". It is reported as exactly
  that, non-retryable, because retrying cannot conjure an approval.
* **Two error shapes, not one.** The OAuth endpoints answer with
  ``{"error", "error_description"}`` (RFC 6749) while the API answers with
  ``{"status", "message", "serviceErrorCode"}``. Both are read, because a caller that
  understands one and not the other reports "unknown error" for half the real failures.
* **Identity comes from OpenID Connect.** ``GET /v2/userinfo`` returns ``sub`` and
  ``name`` given the ``openid``/``profile`` scopes. It is a separate call rather than a
  field on the token response, so the same three-call shape (and the same caveat about a
  grant existing before the row does) applies as in the Meta adapter.

**Publishing is still gated and nothing here changes that.** ``w_member_social`` posts as
the authenticated member; posting as a company page needs ``w_organization_social``, which
needs Marketing Developer Platform approval — weeks of LinkedIn's queue, refusable. This
module implements the connection lifecycle only, and ``docs/CHANNELS.md`` §2-3 remains the
statement of what may be published.

No network in tests: the ``httpx.AsyncClient`` is injected, exactly as
``actuators/email.ResendSender`` and the Meta adapter take theirs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, final
from urllib.parse import urlencode

import httpx

from backend.app.core.token_cipher import Secret
from backend.app.services.platform_oauth import OAuthError, TokenGrant

__all__ = [
    "APP_ID_ENV",
    "APP_SECRET_ENV",
    "LINKEDIN_PLATFORM",
    "LinkedInCredentials",
    "LinkedInOAuthProvider",
    "build_linkedin_provider",
    "linkedin_credentials",
]

#: The one platform this adapter speaks for. A tuple-shaped constant like Meta's would
#: suggest a second is coming; LinkedIn is one platform with one connection.
LINKEDIN_PLATFORM: Final = "linkedin"

AUTHORIZE_URL: Final = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL: Final = "https://www.linkedin.com/oauth/v2/accessToken"  # noqa: S105 -- a URL
REVOKE_URL: Final = "https://www.linkedin.com/oauth/v2/revoke"
USERINFO_URL: Final = "https://api.linkedin.com/v2/userinfo"

APP_ID_ENV: Final = "LINKEDIN_CLIENT_ID"
APP_SECRET_ENV: Final = "LINKEDIN_CLIENT_SECRET"  # noqa: S105 -- the NAME, not a secret

DEFAULT_TIMEOUT_S: Final = 10.0

#: Statuses worth another attempt. 401/403 are not here: a rejected credential is not a
#: blip, and retrying one is how a revoked token becomes a hot loop.
_RETRYABLE_STATUSES: Final = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class LinkedInCredentials:
    """The application's own identity with LinkedIn.

    ``client_secret`` is :class:`Secret`, so this dataclass's generated ``repr`` — what a
    traceback or a log line prints — carries a masked value rather than the live secret.
    """

    client_id: str
    client_secret: Secret


def _read(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def linkedin_credentials(env: Mapping[str, str] | None = None) -> LinkedInCredentials | None:
    """The configured app, or ``None``.

    **Half-configured counts as absent.** An id with no secret cannot complete an
    exchange, so treating it as "configured" would swap the fake provider — which works —
    for a real one that fails at the callback, after the human has already granted
    consent. The same rule the Meta adapter follows.
    """
    import os

    environ = env if env is not None else os.environ
    client_id = _read(environ, APP_ID_ENV)
    secret = _read(environ, APP_SECRET_ENV)
    if client_id is None or secret is None:
        return None
    return LinkedInCredentials(client_id=client_id, client_secret=Secret(secret))


@dataclass(frozen=True, slots=True)
class _ErrorEnvelope:
    """Whatever LinkedIn said went wrong, from either of its two shapes."""

    code: str | None
    #: LinkedIn's own prose is NOT carried into the exception message: it can echo request
    #: parameters, and this adapter's messages end up in logs and on screens. The code and
    #: the status are enough to act on, and the code is what LinkedIn's docs are indexed by.
    service_error_code: int | None


@final
class LinkedInOAuthProvider:
    """LinkedIn's four operations. Satisfies ``platform_oauth.OAuthProvider``."""

    def __init__(
        self,
        credentials: LinkedInCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._credentials = credentials
        self._client = client
        self._timeout_s = timeout_s

    @property
    def platform(self) -> str:
        return LINKEDIN_PLATFORM

    @property
    def fake(self) -> bool:
        return False

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: object) -> str:
        """Where to send the human. Pure — no request is made.

        ``scopes`` is typed loosely to match the Protocol, which declares a
        ``Sequence[str]``; anything non-string in it would be a caller bug, and joining it
        blindly would put ``None`` in a URL LinkedIn then rejects with an opaque error.
        """
        wanted = [str(s) for s in scopes] if isinstance(scopes, (list, tuple)) else []
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._credentials.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": " ".join(wanted),
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade the callback code for a credential, then read who it belongs to."""
        if not code.strip():
            raise OAuthError("the callback carried no authorization code")
        payload = await self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret.reveal(),
            },
            operation="exchange",
        )
        return await self._grant(payload, operation="exchange")

    async def refresh(self, refresh_token: Secret) -> TokenGrant:
        """Renew with a real refresh token.

        The interesting failure is the one Meta cannot have: LinkedIn only issues refresh
        tokens to applications approved for them, so an unapproved app holds a credential
        that was never renewable. That is reported as a non-retryable refusal naming the
        approval, because no number of retries produces one.
        """
        token = refresh_token.reveal()
        if not token.strip():
            raise OAuthError(
                "this LinkedIn connection has no refresh token. LinkedIn issues them "
                "only to applications approved for refresh; reconnect the account "
                "instead.",
                retryable=False,
            )
        payload = await self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret.reveal(),
            },
            operation="refresh",
        )
        return await self._grant(payload, operation="refresh")

    async def revoke(self, credential: Secret) -> None:
        """Tell LinkedIn to forget this token. Idempotent on a 4xx.

        The status is checked as well as the body, for the reason the Meta adapter records
        in the same method: a 5xx means LinkedIn did not answer the question, and treating
        that as success marks the row revoked and discards the only credential that could
        ever have revoked the grant.
        """
        response = await self._request(
            "POST",
            REVOKE_URL,
            data={
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret.reveal(),
                "token": credential.reveal(),
            },
            operation="revoke",
        )
        if response.status_code >= 500:
            raise self._failure(response, operation="revoke")
        # Any 4xx here means LinkedIn will not act on this token — most often because it
        # is already gone, which is the state a revoke is trying to reach. The protocol
        # promises idempotence, so that is a success.

    # ------------------------------------------------------------------ internals

    async def _grant(self, payload: Mapping[str, Any], *, operation: str) -> TokenGrant:
        access = str(payload.get("access_token") or "").strip()
        if not access:
            raise OAuthError(
                f"LinkedIn's {operation} response carried no access token", retryable=False
            )

        refresh_raw = str(payload.get("refresh_token") or "").strip()
        expires_at = _expiry(payload.get("expires_in"))
        # The scope string LinkedIn actually granted, which can be NARROWER than what was
        # requested — a member may decline one. Recording the requested set instead would
        # make a publish attempt look authorised right up to the point it is refused.
        granted = tuple(str(payload.get("scope") or "").replace(",", " ").split())

        identity = await self._userinfo(access)
        return TokenGrant(
            external_account_id=identity[0],
            access_token=Secret(access),
            refresh_token=Secret(refresh_raw) if refresh_raw else None,
            expires_at=expires_at,
            scopes=granted,
            external_account_name=identity[1],
            fake=False,
        )

    async def _userinfo(self, access_token: str) -> tuple[str, str | None]:
        """``sub`` and ``name`` from OpenID Connect.

        A failure here raises, so no row is written. That is the deliberate choice the
        Meta adapter documents and it has the same cost: LinkedIn has already issued a
        grant by now, so a failure leaves an authorisation we never recorded and cannot
        revoke. The alternative — storing a row with an invented account id — is a
        connection the dashboard claims is live and no publish can ever use.
        """
        response = await self._request(
            "GET",
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            operation="identity",
        )
        if response.status_code >= 400:
            raise self._failure(response, operation="identity")
        body = _json_object(response, operation="identity")
        sub = str(body.get("sub") or "").strip()
        if not sub:
            raise OAuthError("LinkedIn's userinfo response carried no subject id", retryable=False)
        name = str(body.get("name") or "").strip() or None
        return sub, name

    async def _post_token(self, form: dict[str, str], *, operation: str) -> Mapping[str, Any]:
        """The token endpoint, as a POST with a form body.

        A POST rather than a GET with query parameters, and for a specific reason: httpx
        logs ``HTTP Request: <method> <url>`` at INFO, so putting the client secret and a
        live token in the query string writes both into the application log of any
        default-configured deployment. LinkedIn documents and accepts the form body.
        """
        response = await self._request("POST", TOKEN_URL, data=form, operation=operation)
        if response.status_code >= 400:
            raise self._failure(response, operation=operation)
        return _json_object(response, operation=operation)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        operation: str,
    ) -> httpx.Response:
        """One HTTP call, with every transport failure turned into an ``OAuthError``.

        Never lets an httpx exception escape, so the caller's error handling is against
        this module's contract rather than against a transport library's — the same rule
        ``engines/crawl/fetch.py`` states for itself.
        """
        try:
            if self._client is not None:
                return await self._client.request(
                    method, url, data=data, headers=headers, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.request(method, url, data=data, headers=headers)
        except httpx.TimeoutException as exc:
            raise OAuthError(
                f"LinkedIn {operation} timed out after {self._timeout_s:g}s", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            # The exception TYPE, never its message: httpx puts the full URL in it, and
            # the token endpoint's URL is harmless but the identity call's bearer header
            # has been in the same object. The type is what distinguishes the cases.
            raise OAuthError(
                f"LinkedIn {operation} failed: {type(exc).__name__}", retryable=True
            ) from exc

    def _failure(self, response: httpx.Response, *, operation: str) -> OAuthError:
        envelope = _envelope(response)
        retryable = response.status_code in _RETRYABLE_STATUSES
        detail = envelope.code or "no error code"
        if envelope.service_error_code is not None:
            detail = f"{detail} (serviceErrorCode {envelope.service_error_code})"
        return OAuthError(
            f"LinkedIn refused the {operation}: HTTP {response.status_code}, {detail}",
            retryable=retryable,
        )


def _envelope(response: httpx.Response) -> _ErrorEnvelope:
    """Read either of LinkedIn's two error shapes.

    OAuth endpoints answer ``{"error", "error_description"}``; the API answers
    ``{"status", "message", "serviceErrorCode"}``. A reader that knows one shape reports
    "unknown error" for half the real failures, which is why both are parsed here.
    Neither prose field is carried out: see :class:`_ErrorEnvelope`.
    """
    try:
        body = response.json()
    except ValueError:
        return _ErrorEnvelope(code=None, service_error_code=None)
    if not isinstance(body, dict):
        return _ErrorEnvelope(code=None, service_error_code=None)

    code = body.get("error")
    raw_service = body.get("serviceErrorCode")
    return _ErrorEnvelope(
        code=str(code).strip() if isinstance(code, str) and code.strip() else None,
        service_error_code=raw_service if isinstance(raw_service, int) else None,
    )


def _json_object(response: httpx.Response, *, operation: str) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise OAuthError(f"LinkedIn's {operation} response was not JSON", retryable=False) from exc
    if not isinstance(body, dict):
        raise OAuthError(f"LinkedIn's {operation} response was not a JSON object", retryable=False)
    return body


def _expiry(raw: object) -> datetime | None:
    """``expires_in`` seconds as an instant, or ``None`` when it is unusable.

    ``None`` means "no expiry known", which ``ConnectionView`` treats as "cannot be
    expired by the clock, only by the platform rejecting it" — the honest reading when
    LinkedIn does not say, and better than inventing a lifetime the refresh sweep would
    then act on.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    seconds = int(raw)
    if seconds <= 0:
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds)


def build_linkedin_provider(
    platform: str,
    env: Mapping[str, str] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> LinkedInOAuthProvider | None:
    """A real LinkedIn provider, or ``None`` if this process cannot make one.

    ``None`` rather than an exception: "no app configured" is the ordinary state of a
    development machine, and the caller's answer is the fake provider, not a 500.
    """
    if platform != LINKEDIN_PLATFORM:
        return None
    credentials = linkedin_credentials(env)
    if credentials is None:
        return None
    return LinkedInOAuthProvider(credentials, client=client, timeout_s=timeout_s)
