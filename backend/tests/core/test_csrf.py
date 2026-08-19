"""CSRF: origin validation on cookie-authenticated writes.

The tests are organised around the three things the check has to get right, because
each of them is a way this control is commonly built wrong:

* **it must refuse a same-site sibling subdomain.** That is the gap ``SameSite=Lax``
  leaves and the whole reason the middleware exists, so it gets its own test with a
  hostname that shares a registrable domain with the allowlisted one;
* **it must NOT refuse an anonymous cross-origin write.** The public lead form is an
  unauthenticated ``POST`` whose purpose is to be submitted from a landing page on
  some other host. A CSRF check that blocks it would have broken the lead loop, and
  it would have looked like a security improvement while doing it;
* **it must fire on cookie PRESENCE, not on a valid session.** A forged write
  carrying an expired or garbage cookie still has to prove its origin. Keying off
  whether the token verifies would make the guard depend on the attacker's own input.

Everything is header-level: no database, no network, and a flag that says whether the
endpoint ran.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core.body_limit import BodySizeLimitMiddleware
from backend.app.core.config import get_settings
from backend.app.core.cookies import session_cookie_name
from backend.app.core.csrf import (
    STATE_CHANGING_METHODS,
    OriginCsrfMiddleware,
    normalise_origin,
)
from backend.app.main import create_app
from backend.tests.core.test_body_limit import middleware_classes

ALLOWED = "https://app.example.com"
#: Shares the registrable domain with ALLOWED, so a browser treats a request from
#: here as SAME-SITE and attaches the Lax cookie. This is the attacker.
SIBLING_SUBDOMAIN = "https://evil.app.example.com"
FOREIGN = "https://evil.example"

COOKIE = session_cookie_name(get_settings())


class Reached:
    def __init__(self) -> None:
        self.called = False


def _app(*, allowed: tuple[str, ...] = (ALLOWED,)) -> tuple[FastAPI, Reached]:
    app = FastAPI()
    reached = Reached()

    @app.api_route("/write", methods=sorted(STATE_CHANGING_METHODS))
    @app.get("/write")
    async def _sink() -> dict[str, bool]:
        reached.called = True
        return {"ok": True}

    app.add_middleware(
        OriginCsrfMiddleware,
        allowed_origins=allowed,
        session_cookie_name=COOKIE,
    )
    return app, reached


def _client(app: FastAPI, *, with_cookie: bool = True) -> httpx.AsyncClient:
    """A client for one origin's worth of requests.

    ``base_url`` is ``https://api.example.com`` so the request's own ``Host`` is
    something other than the allowlisted frontend origin -- otherwise the
    same-origin allowance below would quietly satisfy every test and none of them
    would be testing the allowlist.
    """
    cookies = {COOKIE: "a-token-value"} if with_cookie else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.example.com",
        cookies=cookies,
    )


# --------------------------------------------------------------------------- #
# The gap SameSite=Lax leaves
# --------------------------------------------------------------------------- #


async def test_a_write_from_a_sibling_subdomain_is_refused() -> None:
    """The reason this middleware exists.

    ``evil.app.example.com`` and ``app.example.com`` are the same SITE, so
    ``SameSite=Lax`` attaches the session cookie to this request and does not help.
    The origin allowlist refuses it, because a hostname we did not name is not one we
    trust however closely it is related to one we did.
    """
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/write", headers={"origin": SIBLING_SUBDOMAIN})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "csrf_origin_refused"
    assert reached.called is False


async def test_a_cookie_bearing_write_with_no_origin_and_no_referer_is_refused() -> None:
    """The older-browser gap, and the non-browser-client case, in one rule.

    A browser making a credentialed cross-origin write always sends ``Origin``. A
    request that carries our session cookie and neither header is either not a
    browser -- in which case it should not be replaying a browser credential -- or is
    one old enough that ``SameSite`` is doing nothing for it either.
    """
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/write")

    assert response.status_code == 403
    assert reached.called is False


@pytest.mark.parametrize("method", sorted(STATE_CHANGING_METHODS))
async def test_every_state_changing_method_is_covered(method: str) -> None:
    """``POST`` is the famous one. A ``PUT`` that saves an admin route is the same risk.

    Parametrised off the middleware's own set so a method added there cannot be added
    without a test.
    """
    app, reached = _app()
    async with _client(app) as client:
        response = await client.request(method, "/write", headers={"origin": FOREIGN})

    assert response.status_code == 403
    assert reached.called is False


# --------------------------------------------------------------------------- #
# What must keep working
# --------------------------------------------------------------------------- #


async def test_a_write_from_the_allowlisted_frontend_passes() -> None:
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/write", headers={"origin": ALLOWED})

    assert response.status_code == 200
    assert reached.called is True


async def test_an_anonymous_cross_origin_write_is_not_touched() -> None:
    """The public lead form. A CSRF check that blocked this would break the lead loop.

    There is no session cookie on the request, so there is no ambient credential to
    forge -- which is what CSRF is. The protections that belong on an anonymous write
    are the ones ``api.leads`` already has: a rate limit, a size cap, a closed schema
    and a honeypot.
    """
    app, reached = _app()
    async with _client(app, with_cookie=False) as client:
        response = await client.post("/write", headers={"origin": FOREIGN})

    assert response.status_code == 200
    assert reached.called is True


async def test_a_safe_method_is_not_checked_even_with_a_foreign_origin() -> None:
    """``GET`` must not change state, and what a cross-origin page may READ is CORS's
    job, decided in the browser where the response is."""
    app, reached = _app()
    async with _client(app) as client:
        response = await client.get("/write", headers={"origin": FOREIGN})

    assert response.status_code == 200
    assert reached.called is True


async def test_a_same_origin_write_passes_without_being_named_in_the_allowlist() -> None:
    """A frontend and API behind one hostname must work with no extra configuration.

    Nobody would think to add their own host to ``cors_origins``, and its absence
    would present as every write failing with 403 in production only. The request's
    own ``Host`` is set by the browser from the URL being fetched, so a cross-origin
    request can never satisfy this.
    """
    app, reached = _app(allowed=())
    async with _client(app) as client:
        response = await client.post("/write", headers={"origin": "https://api.example.com"})

    assert response.status_code == 200
    assert reached.called is True


async def test_the_scheme_of_a_same_host_origin_is_not_compared() -> None:
    """Behind a TLS terminator the app may see ``http`` while the browser says ``https``.

    That is a proxy configuration detail, not an attack, and it must not present as
    every write being refused. The host is what identifies our own origin.
    """
    app, reached = _app(allowed=())
    async with _client(app) as client:
        response = await client.post("/write", headers={"origin": "http://api.example.com"})

    assert response.status_code == 200
    assert reached.called is True


# --------------------------------------------------------------------------- #
# Referer fallback, and parsing
# --------------------------------------------------------------------------- #


async def test_an_allowlisted_referer_is_accepted_when_there_is_no_origin() -> None:
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/write", headers={"referer": f"{ALLOWED}/admin/models"})

    assert response.status_code == 200
    assert reached.called is True


async def test_a_foreign_referer_is_refused() -> None:
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/write", headers={"referer": f"{FOREIGN}/attack.html"})

    assert response.status_code == 403
    assert reached.called is False


async def test_origin_wins_over_referer() -> None:
    """A forged write cannot smuggle itself through by adding a friendly ``Referer``.

    ``Origin`` is set by the browser and cannot be set by page script; ``Referer`` is
    the weaker signal and is only consulted in its absence.
    """
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post(
            "/write",
            headers={"origin": FOREIGN, "referer": f"{ALLOWED}/admin"},
        )

    assert response.status_code == 403
    assert reached.called is False


@pytest.mark.parametrize("value", ["null", "", "not a url", "https://", "javascript:alert(1)"])
async def test_an_opaque_or_unparseable_origin_is_refused(value: str) -> None:
    """``Origin: null`` is what a sandboxed iframe or a ``file://`` page sends."""
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/write", headers={"origin": value})

    assert response.status_code == 403
    assert reached.called is False


async def test_the_refusal_does_not_reflect_the_origin_it_refused() -> None:
    """Caller-supplied text echoed into a body is how an error message becomes a
    vector, and the caller learns nothing from being told its own ``Origin``."""
    app, _ = _app()
    async with _client(app) as client:
        response = await client.post("/write", headers={"origin": SIBLING_SUBDOMAIN})

    assert "evil" not in response.text


@pytest.mark.parametrize(
    "configured",
    ["https://APP.example.com", "https://app.example.com/", "  https://app.example.com  "],
)
async def test_the_allowlist_is_normalised_so_formatting_cannot_lock_everyone_out(
    configured: str,
) -> None:
    """A trailing slash in an environment variable must not refuse every write."""
    app, reached = _app(allowed=(configured,))
    async with _client(app) as client:
        response = await client.post("/write", headers={"origin": ALLOWED})

    assert response.status_code == 200
    assert reached.called is True


def test_normalise_origin_drops_path_query_and_case() -> None:
    assert normalise_origin("HTTPS://App.Example.com:8443/some/path?x=1") == (
        "https://app.example.com:8443"
    )
    assert normalise_origin("relative/path") == ""


# --------------------------------------------------------------------------- #
# Presence, not validity
# --------------------------------------------------------------------------- #


async def test_a_garbage_session_cookie_still_triggers_the_check() -> None:
    """The guard runs before anything verifies the token, and it must.

    If it only fired for a *valid* session, an attacker could skip it by sending a
    forged write with a corrupted cookie -- and the interesting forged writes are the
    ones aimed at a route that would then 401 loudly rather than silently.
    """
    app, reached = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.example.com",
        cookies={COOKIE: "not-even-close"},
    ) as client:
        response = await client.post("/write", headers={"origin": FOREIGN})

    assert response.status_code == 403
    assert reached.called is False


async def test_an_unrelated_cookie_does_not_trigger_the_check() -> None:
    """Only the session cookie is an ambient credential. Analytics cookies are not."""
    app, reached = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.example.com",
        cookies={"theme": "dark", f"not_{COOKIE}": "x"},
    ) as client:
        response = await client.post("/write", headers={"origin": FOREIGN})

    assert response.status_code == 200
    assert reached.called is True


# --------------------------------------------------------------------------- #
# The shipped application
# --------------------------------------------------------------------------- #


def test_the_shipped_allowlist_is_exactly_the_cors_allowlist() -> None:
    """One list. Two would answer the same question differently within a release.

    ``main.py``'s CORS block records what a stale browser-policy list costs: a
    missing ``PUT`` broke every admin save while every test passed.
    """
    settings = get_settings()
    app = create_app()
    classes = middleware_classes(app)

    assert classes.count(OriginCsrfMiddleware) == 1
    entry = app.user_middleware[classes.index(OriginCsrfMiddleware)]
    assert entry.kwargs["allowed_origins"] == settings.cors_origins
    assert entry.kwargs["session_cookie_name"] == session_cookie_name(settings)


def test_the_csrf_guard_is_outside_the_body_limit_and_inside_cors() -> None:
    """Order is a decision, so it is a test.

    Outermost first: CORS has to wrap both guards or a browser sees a generic network
    error instead of the refusal, and the CSRF check has to wrap the size limit so a
    forged request is refused on its headers before its body is counted.
    """
    order = middleware_classes(create_app())

    assert order.index(CORSMiddleware) < order.index(OriginCsrfMiddleware)
    assert order.index(OriginCsrfMiddleware) < order.index(BodySizeLimitMiddleware)


async def test_a_forged_logout_against_the_real_app_is_refused_before_any_route_runs() -> None:
    """End to end on the shipped application, on a real cookie-authenticated route.

    ``logout`` is deliberately tolerant -- it answers 204 to a garbage cookie, and it
    revokes every session for a correctly signed one. That tolerance is exactly why it
    must not be reachable cross-site: a forged logout is a denial of service against
    every device a user owns.
    """
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.example.com",
        cookies={COOKIE: "some-token"},
    ) as client:
        forged = await client.post("/api/v1/auth/logout", headers={"origin": FOREIGN})

    assert forged.status_code == 403
    assert forged.json()["detail"]["code"] == "csrf_origin_refused"
