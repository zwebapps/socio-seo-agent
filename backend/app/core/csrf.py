"""CSRF defence for cookie-authenticated writes, beyond ``SameSite=Lax``.

What ``SameSite=Lax`` already does, and what it does not
-------------------------------------------------------

The session cookie is ``SameSite=Lax``, so a browser will not attach it to a
cross-site ``POST`` -- ``Lax`` sends the cookie only on a top-level navigation with
a safe method. That covers the textbook attack (a hidden auto-submitting form on
another site) and it covers it well. Three gaps are left, and they are the reason
this module exists:

* **same-site subdomains are not cross-site.** ``evil.example.com`` and
  ``app.example.com`` share a registrable domain, so a request from one to the
  other is *same-site* and ``Lax`` attaches the cookie. Anything that can host
  content on a sibling subdomain -- a takeover of a stale DNS record, a
  user-content host, a vendor's CNAME -- gets a working CSRF vector that
  ``SameSite`` is not designed to stop;
* **older browsers.** ``SameSite`` has no effect where it is not implemented, and a
  cookie policy whose enforcement lives entirely in the client is a policy the
  client can lack;
* **it is one attribute.** ``Lax`` is the browser default now, but it is set on our
  side of the wire by one keyword argument, and this is a codebase that already
  writes tests for the absence of a cookie attribute for exactly that reason.

Origin/Referer validation, and why NOT a double-submit token
------------------------------------------------------------

The decision turns entirely on how this specific frontend authenticates, so it is
worth being concrete. ``frontend/app/lib/admin-api.ts``, ``frontend/app/login`` and
``frontend/app/runs`` all call the API with ``credentials: "include"`` from a
**different origin** -- Next.js on ``:3100``, FastAPI on ``:8100``. That single fact
decides it:

* **a double-submit cookie cannot be read by this frontend.** The pattern requires
  page script to read a CSRF cookie and echo it into a header, and ``document.cookie``
  is scoped to the reading document's origin. JavaScript served from ``:3100``
  cannot see a cookie the API set on ``:8100``, in any browser, by design. What gets
  built instead is a token handed over in a response body and kept in memory -- a
  synchronizer token wearing a double-submit costume, which needs a bootstrap
  endpoint, a place to keep the value, a refresh path when it expires, and an
  answer for "CSRF token invalid, please reload";
* **double-submit's own weakness is the first gap listed above.** Its security rests
  on the attacker being unable to set the cookie half, and a sibling subdomain CAN
  set a cookie on the parent domain -- so a subdomain attacker forges both halves
  and the check passes. Closing that means putting ``__Host-`` on the CSRF cookie
  too, and after all that work the subdomain attack is still what you are worried
  about. Origin validation refuses the sibling subdomain directly, because
  ``https://evil.example.com`` is not in the allowlist;
* **the allowlist already exists, and drifting from it would be the bug.** The set
  of origins permitted to make credentialed calls is ``settings.cors_origins``,
  which ``CORSMiddleware`` is already configured from. Deriving this check from the
  same tuple means adding a frontend origin is one edit in one place. The comment on
  the CORS block in ``main.py`` records what happens when a browser-only policy list
  falls out of step with reality: a missing ``PUT`` broke every admin save while
  every test passed;
* **``Origin`` cannot be forged by the attacker's page.** It is a forbidden header
  name, so ``fetch`` and ``XMLHttpRequest`` cannot set it; the browser does, from
  the document that made the request. And nothing has to change in the frontend --
  it is already sending the header on every one of these calls.

``Referer`` is only a fallback, for the case where a request carries no ``Origin``.
It is checked scheme-and-host only, and it is second because a referrer policy can
legitimately strip it -- which is a reason to distrust its absence, not its
contents.

The check applies to cookie-bearing writes only, and that is deliberate
----------------------------------------------------------------------

CSRF is the browser attaching **ambient credentials** to a request the user did not
intend. A request that carries no session cookie has no ambient credential in it, so
there is nothing to forge -- and refusing those would break something real: the
public lead form (``api.leads``) is an unauthenticated write whose entire purpose is
to be posted from a landing page on some other host (docs/CHANNELS.md section 5).
Gating on the cookie keeps that endpoint reachable while every authenticated write
is covered. The public form's own protections -- rate limit, size cap, closed
schema, honeypot -- are what stand in front of it, and they are the right shape for
an anonymous route.

Safe methods are not checked either. ``GET`` and ``HEAD`` must not change state; a
``GET`` that does is a bug to fix at the route, and blocking cross-origin reads is
CORS's job, in the browser, where the response is.

Residual risks, recorded rather than implied
--------------------------------------------

* **login CSRF is not closed.** A request to ``/auth/login`` carries no session
  cookie yet, so it is not checked, and an attacker can in principle try to log a
  victim into an account the attacker controls. ``SameSite=Lax`` blocks the
  cross-site ``POST`` that would do it in any browser that implements it, and the
  gain from a successful one is low here (there is nothing a victim would submit
  into an attacker's session that they would not submit into their own). Closing it
  properly needs a pre-session token, which is the synchronizer machinery this
  module exists to avoid.
* **a non-browser client that sends our session cookie will be refused**, because
  it sends no ``Origin`` and no ``Referer``. That is intended: the cookie is a
  browser credential, and the API has no documented machine-to-machine mode. A
  script that needs one should get a token, not replay a session.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

#: Methods that may change state and are therefore checked. ``GET``, ``HEAD`` and
#: ``OPTIONS`` are not: see the module docstring. ``OPTIONS`` also has to stay open
#: for the CORS preflight, which is answered by the middleware wrapped around this
#: one and never reaches here.
STATE_CHANGING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: One answer for every refusal. It deliberately does not echo the origin that was
#: refused: reflecting caller-supplied text into a response body is how an error
#: message becomes an injection vector, and the caller learns nothing actionable
#: from being told its own ``Origin``.
_REFUSED_BODY: Final = json.dumps(
    {
        "detail": {
            "code": "csrf_origin_refused",
            "message": (
                "That request did not come from an allowed origin, so it was refused. "
                "Please retry from the application."
            ),
        }
    }
).encode()


def normalise_origin(value: str) -> str:
    """An origin as ``scheme://host[:port]``, lowercased, with no trailing slash.

    Applied to both sides of every comparison so that ``https://App.Example.com/``
    and ``https://app.example.com`` cannot disagree.
    """
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


class OriginCsrfMiddleware:
    """Refuse a cookie-authenticated state-changing request from an unknown origin.

    Pure ASGI, so the refusal happens before the body is read and before any
    dependency runs -- a forged request must not get as far as a database session.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: Iterable[str],
        session_cookie_name: str,
    ) -> None:
        self._app = app
        self._allowed = frozenset(
            normalised for origin in allowed_origins if (normalised := normalise_origin(origin))
        )
        self._cookie_name = session_cookie_name

    @property
    def allowed_origins(self) -> frozenset[str]:
        """The normalised allowlist. Exposed so a test can assert it has not drifted."""
        return self._allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "") not in STATE_CHANGING_METHODS:
            return await self._app(scope, receive, send)

        headers = _header_map(scope)
        if not _carries_cookie(headers.get(b"cookie", b""), self._cookie_name):
            # No ambient credential, so no CSRF. See the module docstring -- this is
            # what keeps the public lead form usable from a landing page anywhere.
            return await self._app(scope, receive, send)

        if self._origin_is_allowed(headers):
            return await self._app(scope, receive, send)

        return await self._refuse(send)

    def _origin_is_allowed(self, headers: dict[bytes, bytes]) -> bool:
        """Whether this request proves it came from somewhere we trust.

        ``Origin`` first because the browser always sends it on a cross-origin
        write and page script cannot forge it. ``Referer`` only when ``Origin`` is
        absent. Neither present is a refusal, not a pass: a browser making a
        credentialed write sends one of them, so a request with neither is either
        not a browser or is too old to be relied on for ``SameSite`` either -- which
        is precisely the gap this middleware was added to cover.
        """
        raw_origin = headers.get(b"origin")
        if raw_origin is not None:
            return self._is_trusted(raw_origin.decode("latin-1"), headers)

        raw_referer = headers.get(b"referer")
        if raw_referer is not None:
            return self._is_trusted(raw_referer.decode("latin-1"), headers)

        return False

    def _is_trusted(self, value: str, headers: dict[bytes, bytes]) -> bool:
        origin = normalise_origin(value)
        if not origin:
            # An opaque origin ("null" from a sandboxed iframe or a file:// page)
            # or unparseable rubbish. Neither is somewhere we trust.
            return False
        if origin in self._allowed:
            return True
        return self._is_own_host(origin, headers)

    @staticmethod
    def _is_own_host(origin: str, headers: dict[bytes, bytes]) -> bool:
        """Allow a request whose origin IS the host it is addressed to.

        Two reasons this is safe and one reason it is necessary.

        Safe, because a page cannot lie about either side: the browser sets
        ``Origin`` from the document, and ``Host`` from the URL being fetched. An
        attacker at ``https://evil.example`` posting to ``https://api.ours``
        produces ``Origin: https://evil.example`` against ``Host: api.ours``, which
        does not match. There is no arrangement in which a cross-origin request
        satisfies this test.

        Necessary, because it makes a same-origin deployment -- frontend and API
        behind one hostname -- work without ``cors_origins`` having to name its own
        host, which is a configuration nobody would think to add and whose absence
        would refuse every write in production.

        Compared on host and port only. The app's view of the SCHEME depends on
        whether it is being run with ``--proxy-headers`` behind a TLS terminator; a
        mismatch there is a proxy misconfiguration, not an attack, and it must not
        present as every write failing with 403.
        """
        host = headers.get(b"host")
        if host is None:
            return False
        return urlsplit(origin).netloc == host.decode("latin-1").strip().lower()

    @staticmethod
    async def _refuse(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REFUSED_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REFUSED_BODY})


def _header_map(scope: Scope) -> dict[bytes, bytes]:
    """Last-wins map of the request headers, keyed by lowercase name.

    ASGI delivers headers as a list of pairs and guarantees lowercase names. Only
    single-valued headers are read here, so collapsing is safe -- and a request that
    sends two ``Origin`` headers is not one we intend to honour anyway: the last
    value wins and it has to be an allowed one.
    """
    return dict(scope.get("headers", ()))


def _carries_cookie(header: bytes, name: str) -> bool:
    """Whether the ``Cookie`` header contains a cookie called ``name``.

    Presence only -- the value is the session module's business, and this check must
    fire for an INVALID session cookie too. A forged write that happens to carry
    expired or garbage credentials still has to prove its origin; deferring to
    whether the token verifies would make the guard depend on the very thing an
    attacker controls.

    Written out rather than delegating to a cookie parser because the parsers
    silently drop pairs they consider malformed, and a session cookie the
    application will read but this function does not see is the one failure mode
    that must not exist.
    """
    for part in header.split(b";"):
        candidate, _, _ = part.partition(b"=")
        if candidate.strip().decode("latin-1") == name:
            return True
    return False
