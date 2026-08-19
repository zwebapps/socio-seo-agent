"""The request-body ceiling.

The interesting assertions are not "an oversized body is refused". They are:

* **the endpoint never runs.** Every refusal test asserts a flag the endpoint would
  have set, because a 413 returned *after* the handler parsed a 20 MB body is not a
  limit, it is a report;
* **a lying ``Content-Length`` does not get past it.** The header is caller-supplied.
  A middleware that only reads it is asking attackers to declare their own size;
* **a chunked request with no ``Content-Length`` at all does not get past it either.**
  This is the case a header check cannot see, and it is the reason the byte counter
  exists;
* **the login route specifically.** That is the backlog item this closes: argon2 at
  64 MiB was reachable with an unbounded body, so there is an end-to-end test on the
  real application asserting the oversized login is refused before
  ``auth_service.authenticate`` is called at all.

No database and no network: a tiny app, an ASGI transport, and a flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from backend.app.api import auth as auth_api
from backend.app.core.body_limit import (
    DEFAULT_MAX_BODY_BYTES,
    BodyLimit,
    BodySizeLimitMiddleware,
)
from backend.app.main import create_app
from backend.app.services import auth_service

SMALL_CAP = 1024


class Reached:
    """Whether the endpoint ran, and with how much body.

    A mutable object rather than a nonlocal so the assertion reads the same in every
    test and cannot accidentally close over a stale value.
    """

    def __init__(self) -> None:
        self.called = False
        self.body_length: int | None = None


def _app(
    *,
    default_max_bytes: int = SMALL_CAP,
    overrides: tuple[BodyLimit, ...] = (),
) -> tuple[FastAPI, Reached]:
    app = FastAPI()
    reached = Reached()

    @app.post("/echo")
    @app.post("/api/v1/documents/upload")
    @app.post("/api/v1/doc")
    @app.post("/api/v1/documents")
    async def _sink(request: Request) -> dict[str, int]:
        body = await request.body()
        reached.called = True
        reached.body_length = len(body)
        return {"length": len(body)}

    @app.get("/echo")
    async def _read() -> dict[str, bool]:
        reached.called = True
        return {"ok": True}

    app.add_middleware(
        BodySizeLimitMiddleware,
        default_max_bytes=default_max_bytes,
        overrides=overrides,
    )
    return app, reached


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def _stream(payload: bytes, *, chunk: int = 4096) -> AsyncIterator[bytes]:
    """A body sent with ``Transfer-Encoding: chunked`` and no ``Content-Length``."""
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]


# --------------------------------------------------------------------------- #
# The two enforcement points
# --------------------------------------------------------------------------- #


async def test_a_body_inside_the_cap_is_delivered_untouched() -> None:
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/echo", content=b"x" * (SMALL_CAP - 1))

    assert response.status_code == 200
    assert reached.body_length == SMALL_CAP - 1


async def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read() -> None:
    """The cheap path: one header read, no allocation, and the handler never runs."""
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post("/echo", content=b"x" * (SMALL_CAP * 4))

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "payload_too_large"
    assert reached.called is False


async def test_a_declaration_alone_is_enough_to_be_refused() -> None:
    """Isolates the header check from the byte counter.

    The body here is empty, so the counter would happily let it through -- only the
    declared length is oversized. Without this, deleting the ``Content-Length`` check
    would break nothing in this file, because the counter quietly covers every case
    where the body actually arrives. That is a limit that costs a full read to
    enforce, which is the thing the cheap path exists to avoid.
    """
    app, reached = _app()
    async with _client(app) as client:
        response = await client.post(
            "/echo",
            content=b"",
            headers={"content-type": "application/octet-stream", "content-length": "999999"},
        )

    assert response.status_code == 413
    assert reached.called is False


async def test_a_lying_content_length_does_not_get_past_the_counter() -> None:
    """The header is a hint. This is why the stream is counted as well as declared."""
    app, reached = _app()
    payload = b"x" * (SMALL_CAP * 4)

    async with _client(app) as client:
        response = await client.post(
            "/echo",
            content=payload,
            headers={"content-type": "application/octet-stream", "content-length": "10"},
        )

    assert response.status_code == 413
    assert reached.called is False


async def test_a_chunked_body_over_the_cap_is_refused_although_it_declares_nothing() -> None:
    """The case a header check cannot see at all.

    ``Transfer-Encoding: chunked`` carries no ``Content-Length``, so the only way to
    bound it is to count what arrives and stop. Refusing every chunked request
    instead -- which is what the public lead form does -- would be a bound on
    legitimate clients rather than on attackers.
    """
    app, reached = _app()

    async with _client(app) as client:
        response = await client.post("/echo", content=_stream(b"x" * (SMALL_CAP * 4)))

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "payload_too_large"
    assert reached.called is False


async def test_a_chunked_body_inside_the_cap_still_works() -> None:
    """The counter must not turn "streamed" into "refused"."""
    app, reached = _app()

    async with _client(app) as client:
        response = await client.post("/echo", content=_stream(b"x" * (SMALL_CAP - 1)))

    assert response.status_code == 200
    assert reached.body_length == SMALL_CAP - 1


async def test_the_refusal_echoes_nothing_that_was_submitted() -> None:
    """A 413 is a place a body can leak back out. It must carry no part of it.

    The declared limit is withheld too: it tells a legitimate caller nothing they can
    act on, and tells an attacker exactly how large a request may be while still
    being parsed.
    """
    app, _ = _app()
    marker = "do-not-reflect-this-marker"

    async with _client(app) as client:
        response = await client.post("/echo", json={"secret": marker * 200})

    assert response.status_code == 413
    assert marker not in response.text
    assert str(SMALL_CAP) not in response.text


async def test_a_get_is_unaffected() -> None:
    app, reached = _app()
    async with _client(app) as client:
        response = await client.get("/echo")

    assert response.status_code == 200
    assert reached.called is True


# --------------------------------------------------------------------------- #
# Per-prefix overrides
# --------------------------------------------------------------------------- #


async def test_an_override_raises_the_cap_for_its_prefix_only() -> None:
    """What a future upload endpoint needs, and what it must not hand everything else."""
    app, reached = _app(overrides=(BodyLimit("/api/v1/documents", SMALL_CAP * 8),))
    payload = b"x" * (SMALL_CAP * 4)

    async with _client(app) as client:
        allowed = await client.post("/api/v1/documents/upload", content=payload)
        assert allowed.status_code == 200
        assert reached.body_length == len(payload)

        refused = await client.post("/echo", content=payload)

    assert refused.status_code == 413


def test_the_longest_matching_prefix_wins_whatever_order_they_are_declared_in() -> None:
    """Otherwise the cap for a path depends on the order of a tuple literal."""
    broad = BodyLimit("/api", 10)
    narrow = BodyLimit("/api/v1/documents", 5000)

    for overrides in ((broad, narrow), (narrow, broad)):
        middleware = BodySizeLimitMiddleware(FastAPI(), overrides=overrides)
        assert middleware.limit_for("/api/v1/documents/upload") == 5000
        assert middleware.limit_for("/api/v1/runs") == 10
        assert middleware.limit_for("/health") == DEFAULT_MAX_BODY_BYTES


async def test_an_override_stops_at_a_path_boundary() -> None:
    """``/api/v1/doc`` must not quietly raise the cap on ``/api/v1/documents``.

    A bare ``startswith`` gets this wrong in the dangerous direction: it LIFTS a
    limit on a route nobody meant to cover.
    """
    app, reached = _app(overrides=(BodyLimit("/api/v1/doc", SMALL_CAP * 8),))
    payload = b"x" * (SMALL_CAP * 4)

    async with _client(app) as client:
        sibling = await client.post("/api/v1/documents", content=payload)
        assert sibling.status_code == 413
        assert reached.called is False

        exact = await client.post("/api/v1/doc", content=payload)

    assert exact.status_code == 200


@pytest.mark.parametrize(
    ("prefix", "max_bytes"),
    [("api/v1", 100), ("", 100), ("/api", 0), ("/api", -1)],
)
def test_a_nonsensical_override_is_refused_at_construction(prefix: str, max_bytes: int) -> None:
    """A relative prefix would match nothing and a zero cap would refuse everything.

    Both are silent in production and loud here.
    """
    with pytest.raises(ValueError):
        BodyLimit(prefix, max_bytes)


def test_a_zero_default_is_refused() -> None:
    with pytest.raises(ValueError):
        BodySizeLimitMiddleware(FastAPI(), default_max_bytes=0)


# --------------------------------------------------------------------------- #
# The route the backlog item was actually about
# --------------------------------------------------------------------------- #


async def test_an_oversized_login_never_reaches_argon2(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backlog item, end to end on the real application.

    ``login`` runs argon2id at 64 MiB. Before the ceiling, the body carrying the
    password it hashes was unbounded, so one caller could hand the process megabytes
    to copy through h11, Starlette, pydantic and the hasher.

    ``authenticate`` is replaced with something that fails the test if it is called
    at all -- the same technique as ``test_a_rate_limited_login_never_reaches_argon2``,
    and for the same reason: "refused" and "refused before spending the memory" are
    different properties and only the second one is a defence.
    """

    async def _must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("authenticate was reached with an oversized body")

    monkeypatch.setattr(auth_service, "authenticate", _must_not_run)

    app = create_app()
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "someone@example.test", "password": "x" * (DEFAULT_MAX_BODY_BYTES + 1)},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "payload_too_large"


@pytest.mark.parametrize("declared", [None, "50"])
async def test_a_body_the_header_did_not_declare_is_413_on_a_real_route_not_a_parse_error(
    declared: str | None,
) -> None:
    """The bug this file did not catch until it was probed against the real app.

    A test endpoint that calls ``await request.body()`` itself lets an exception from
    the receive channel travel up to the middleware. A **FastAPI** route does not:
    its request handler wraps the body read in a bare ``except Exception`` and turns
    anything at all into ``400 There was an error parsing the body``. So the
    streaming half of the limit was enforcing correctly -- the read did stop, memory
    was bounded -- while telling every caller its JSON was malformed, which is the
    wrong status and an untrue explanation.

    Both cases the ``Content-Length`` check cannot cover are asserted here, against a
    route FastAPI parses: a chunked request that declares nothing, and one that
    declares a small size and sends a large body.
    """
    payload = b'{"email":"a@b.test","password":"' + b"x" * (DEFAULT_MAX_BODY_BYTES + 1) + b'"}'
    headers = {"content-type": "application/json"}
    if declared is not None:
        headers["content-length"] = declared

    async with _client(create_app()) as client:
        response = await client.post(
            "/api/v1/auth/login",
            content=payload if declared is not None else _stream(payload),
            headers=headers,
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "payload_too_large"


async def test_the_shipped_default_leaves_the_lead_forms_tighter_cap_in_charge() -> None:
    """A global ceiling must not take over a route that caps itself harder.

    ``api.leads`` refuses a form body over 8 KiB and has its own tests proving that
    refusal. If the global default dropped below the ~20 KiB those tests submit,
    they would still pass -- while asserting nothing about the endpoint's own
    control. Pinning the ordering here is what stops that happening silently.
    """
    from backend.app.api.leads import MAX_FORM_BODY_BYTES

    assert DEFAULT_MAX_BODY_BYTES > MAX_FORM_BODY_BYTES * 2


def middleware_classes(app: FastAPI) -> list[object]:
    """The middleware classes installed on ``app``, outermost first.

    Typed as ``object`` deliberately. Starlette types ``Middleware.cls`` as an
    internal ``_MiddlewareFactory`` protocol, and ``mypy --strict`` refuses both an
    identity check and a ``list.index`` against a concrete class on that type. That is
    a typing detail of the framework, not a reason to stop asserting which middleware
    is installed and in what order -- so it is widened here, in the test, rather than
    worked around with a ``cast`` at every call site.
    """
    classes: list[object] = [entry.cls for entry in app.user_middleware]
    return classes


def test_the_shipped_application_applies_the_default_everywhere() -> None:
    """No route is exempt today, and the middleware is actually installed.

    An override list nobody reads would be the easiest thing in this change to lose
    in a merge, so the presence of the middleware in the shipped stack is asserted
    rather than assumed.
    """
    app = create_app()
    classes = middleware_classes(app)

    assert classes.count(BodySizeLimitMiddleware) == 1
    entry = app.user_middleware[classes.index(BodySizeLimitMiddleware)]
    assert not entry.kwargs, "the shipped app takes the documented default"
    assert auth_api.MAX_PASSWORD_FIELD_CHARS < DEFAULT_MAX_BODY_BYTES
