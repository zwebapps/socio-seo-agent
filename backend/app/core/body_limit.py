"""A ceiling on the size of a request body, enforced before the application reads it.

Why this exists at all
----------------------

``POST /api/v1/auth/login`` and ``/signup`` run argon2id at 64 MiB (see
``core.security``). ``core.rate_limit`` bounds how MANY of those may run -- per IP,
per email, and how many at once -- but nothing bounded the request itself, so a
single caller could hand the process a multi-megabyte "password" and have it
copied through h11, Starlette's body buffer, pydantic and the hasher's input. The
same hole is a free write amplifier on every other route: FastAPI parses the whole
body into Python objects before a single field constraint is consulted.

Two enforcement points, because one is not a limit
--------------------------------------------------

**``Content-Length``, before the stream is touched.** This is the cheap path and
covers the ordinary oversized request: the refusal costs one header read and no
allocation.

**The stream itself, chunk by chunk.** ``Content-Length`` is caller-supplied. A
request may declare 40 bytes and send 20 MB, or use chunked transfer encoding and
declare nothing at all -- and a limit that only trusts the header is not a limit,
it is a request that attackers be honest. Every ``http.request`` message is
therefore counted as it passes through, and the first one that crosses the ceiling
ends the request. Peak memory is bounded by the ceiling plus one chunk, so the
oversized body is never assembled.

The chunked case is deliberately NOT answered with 411 Length Required. The public
lead form does that (``api.leads``) because an anonymous form submission has no
business streaming, and there refusing is free. Globally it would break every
legitimate client that streams, for no gain the byte counter does not already give.

Choosing the default
--------------------

:data:`DEFAULT_MAX_BODY_BYTES` is 64 KiB. The reasoning, rather than a round number
picked for looking sensible:

* every JSON body this API accepts is bounded by its own declared field limits,
  and the largest of them is small -- the lead form caps itself at 8 KiB, a run
  goal is 500 characters, a model-route chain is a handful of short strings, an
  onboarding request is one 2048-character URL. 64 KiB is eight times the largest
  and no legitimate caller is near it;
* it must be large enough not to become the binding limit on a route that already
  has a tighter one of its own. ``api.leads`` caps its body at 8 KiB and has tests
  proving *that* cap refuses; a global ceiling below ~20 KiB would quietly take
  over those refusals and the endpoint's own control would stop being exercised;
* it must be small enough that the ceiling is not itself the attack. At 64 KiB,
  the four concurrent hashes ``PASSWORD_HASH_CONCURRENCY`` permits can hold at
  most 256 KiB of body between them -- against the 256 MiB of argon2 arena those
  same four hashes allocate, that is a rounding error. A 10 MiB default would not
  have been.

It is a ceiling, not a per-route policy: a route that needs less says so in its own
schema, which is where a field limit belongs.

Raising it for one prefix
-------------------------

:class:`BodyLimit` overrides the default for a path prefix, longest match wins.
Nothing uses it today, and that is a finding rather than an omission: onboarding
takes a URL and crawls the site itself (``api.onboarding``), and while the
knowledge-base service can ingest a PDF or a DOCX (``services.kb_service``), no
HTTP route accepts an upload -- there is no ``UploadFile`` anywhere in
``backend/app``. When one lands, it adds one entry here rather than a second
mechanism:

    BodySizeLimitMiddleware(
        app,
        overrides=(BodyLimit("/api/v1/documents", 25 * 1024 * 1024),),
    )

An override on the upload prefix and nowhere else is the point: a document
endpoint's cap has nothing to do with the ceiling that protects the argon2 routes,
and a single global number generous enough for a scanned PDF would protect
neither.

What this does not replace
--------------------------

A byte cap at the application is the last line, not the only one. The process has
already accepted the connection and read some of the body by the time this runs, so
a proxy limit (``client_max_body_size`` in nginx, uvicorn behind it) still belongs
in front of it. This is the layer that is version-controlled with the code it
protects, which is why it is here as well.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: See the module docstring for the arithmetic behind the number.
DEFAULT_MAX_BODY_BYTES: Final = 64 * 1024

#: The one answer an oversized request gets. A fixed string: it names no field, no
#: byte count of the offending body, and nothing that was submitted. The declared
#: limit is not disclosed either -- it tells a caller nothing they need and tells an
#: attacker exactly how large a request may be while still being processed.
_TOO_LARGE_BODY: Final = json.dumps(
    {
        "detail": {
            "code": "payload_too_large",
            "message": "That request body is too large.",
        }
    }
).encode()


@dataclass(frozen=True, slots=True)
class BodyLimit:
    """A ceiling that applies to one path prefix instead of the global default."""

    prefix: str
    max_bytes: int

    def __post_init__(self) -> None:
        if not self.prefix.startswith("/"):
            raise ValueError("prefix must be an absolute path, starting with '/'")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")

    def matches(self, path: str) -> bool:
        """Whether ``path`` is this prefix or sits underneath it.

        The boundary check is not decoration. A plain ``startswith`` would make an
        override on ``/api/v1/doc`` silently apply to ``/api/v1/documents`` -- and
        in this direction the mistake RAISES a limit on a route nobody meant to
        cover, which is the direction that matters.
        """
        trimmed = self.prefix.rstrip("/")
        return path == trimmed or path.startswith(f"{trimmed}/")


class _BodyTooLargeError(Exception):
    """Raised out of the wrapped ``receive`` when the stream crosses the ceiling.

    Deliberately not an ``HTTPException``: this is thrown from inside whatever the
    application happened to be doing when it asked for the next chunk -- FastAPI's
    body parser, usually -- and an ``HTTPException`` would be caught by Starlette's
    exception middleware on the way out and turned into a response built by
    machinery that has no idea what happened.

    **It cannot be relied on to arrive, and that was measured, not assumed.**
    FastAPI's request handler wraps its body read in a bare
    ``except Exception: raise HTTPException(400, "There was an error parsing the
    body")``, so on every FastAPI route this exception is swallowed and the caller
    is told its JSON was malformed -- which is both the wrong status and a lie about
    what happened. A test app that reads ``await request.body()`` in the endpoint
    itself does NOT reproduce that, which is precisely how the mistake survives a
    green suite; it took a probe against the real application to see it.

    So the exception is the *fast stop* -- it ends the read immediately, which is
    what bounds memory -- and the ``send`` side owns the *answer*: if the limit was
    exceeded, whatever response the application produces about its truncated body is
    replaced with the 413. Two mechanisms, because only one of them is guaranteed to
    reach this middleware.
    """


class BodySizeLimitMiddleware:
    """Refuse a request whose body exceeds the ceiling for its path.

    Pure ASGI rather than ``BaseHTTPMiddleware``, and that is a requirement, not a
    style preference: ``BaseHTTPMiddleware`` gives a handler a ``Request`` whose
    body it can only inspect by reading it, so the "check before reading" half of
    the design is not expressible there.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_max_bytes: int = DEFAULT_MAX_BODY_BYTES,
        overrides: Iterable[BodyLimit] = (),
    ) -> None:
        if default_max_bytes < 1:
            raise ValueError("default_max_bytes must be at least 1")
        self._app = app
        self._default = default_max_bytes
        # Longest prefix first, so the most specific override wins and the order
        # they were declared in cannot change behaviour.
        self._overrides = tuple(sorted(overrides, key=lambda o: len(o.prefix), reverse=True))

    def limit_for(self, path: str) -> int:
        """The ceiling that applies to ``path``. Public so a test can assert it."""
        for override in self._overrides:
            if override.matches(path):
                return override.max_bytes
        return self._default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self._app(scope, receive, send)

        limit = self.limit_for(scope.get("path", ""))

        if self._declared_length(scope) > limit:
            return await self._refuse(send)

        started = False
        exceeded = False
        replaced = False
        received = 0

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Set BEFORE raising, because the raise may never be seen: see
                    # `_BodyTooLargeError`. This flag is what the send side acts on.
                    exceeded = True
                    raise _BodyTooLargeError
            return message

        async def guarding_send(message: Message) -> None:
            nonlocal started, replaced
            if replaced:
                # Our 413 is already on the wire. Anything the application still has
                # to say is about a body it never fully received.
                return
            if message["type"] == "http.response.start":
                if exceeded and not started:
                    replaced = True
                    started = True
                    return await self._refuse(send)
                started = True
            await send(message)

        try:
            await self._app(scope, counting_receive, guarding_send)
        except _BodyTooLargeError:
            # Reached only when the application let the exception through, which is
            # the case where nothing has been sent yet.
            if started:
                # The application began answering and only then kept reading. There
                # is no honest 413 to add to a response already on the wire, so let
                # the server abort the connection and log it rather than truncating
                # a reply that looks successful.
                raise
            await self._refuse(send)
        return None

    @staticmethod
    def _declared_length(scope: Scope) -> int:
        """``Content-Length`` as an int, or 0 when it is absent or unusable.

        0 rather than an error on a malformed value: rejecting it here would be
        answering a question the HTTP server has already answered better, and the
        byte counter covers a request that declares nothing anyway.
        """
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return 0
        return 0

    @staticmethod
    async def _refuse(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
                    # The rest of the body is never read, so the connection cannot
                    # be reused -- whatever is still in flight would be parsed as
                    # the start of the next request.
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})
