"""FastAPI application entry point.

Layering rule (docs/ARCHITECTURE.md section 4):

    api -> services -> {engines, actuators, agents} -> adapters

Routes stay thin. They never reach into an engine or an adapter directly.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api import (
    admin_models,
    auth,
    connections,
    cost,
    dashboard,
    documents,
    feedback,
    health,
    leads,
    links,
    onboarding,
    pages,
    posts,
    runs,
)
from backend.app.core.body_limit import BodyLimit, BodySizeLimitMiddleware
from backend.app.core.config import DEFAULT_SESSION_SECRET, Settings, get_settings
from backend.app.core.cookies import session_cookie_name
from backend.app.core.csrf import OriginCsrfMiddleware

#: Below this length an HMAC key is brute-forceable, and the signature is only
#: ever as good as the key behind it.
MIN_SESSION_SECRET_LENGTH = 32


#: Keys whose value must never appear in a response, however the request was shaped.
_SENSITIVE_HINTS = ("password", "key", "secret", "token", "credential")


def _redact_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Strip the submitted value out of every validation error.

    FastAPI's default 422 body includes `input` — the value that failed — and for a
    `missing` error that is the WHOLE parent object. So a single constraint on one field
    can put a password or an API key in the response body, and from there into any log
    or error tracker that captures responses.

    This was not hypothetical: the admin API refuses a request carrying an `apiKey`, and
    the refusal echoed the key straight back. The auth module worked around the same
    problem by declaring no field constraints at all; with this handler that workaround
    is no longer the only defence.

    `loc` and `msg` are kept, because a caller still needs to know WHICH field was wrong
    and why. Only the value goes.
    """
    redacted: list[dict[str, Any]] = []
    for error in errors:
        cleaned: dict[str, Any] = {
            k: v for k, v in error.items() if k not in ("input", "ctx", "url")
        }
        parts: Sequence[Any] = error.get("loc") or ()
        loc = ".".join(str(part) for part in parts)
        if any(hint in loc.lower() for hint in _SENSITIVE_HINTS):
            cleaned["msg"] = "This field is not accepted here."
        redacted.append(cleaned)
    return redacted


class InsecureConfigurationError(RuntimeError):
    """The application refuses to start with a configuration that is not safe.

    Failing at boot rather than serving is deliberate. A misconfigured deployment
    that starts is a deployment nobody notices, and the failure mode here -- a
    known signing key -- lets anyone mint a session for any user.
    """


def _assert_secure(settings: Settings) -> None:
    if settings.environment == "local":
        return

    if settings.session_secret == DEFAULT_SESSION_SECRET:
        raise InsecureConfigurationError(
            f"SESSION_SECRET is still the built-in development default while "
            f"ENVIRONMENT={settings.environment}. Anyone holding that value can sign "
            "a session cookie for any user. Set SESSION_SECRET to a random 32+ "
            "character value (openssl rand -hex 32) and restart."
        )

    if len(settings.session_secret) < MIN_SESSION_SECRET_LENGTH:
        raise InsecureConfigurationError(
            f"SESSION_SECRET is {len(settings.session_secret)} characters while "
            f"ENVIRONMENT={settings.environment}; at least "
            f"{MIN_SESSION_SECRET_LENGTH} are required. The HMAC is only as strong "
            "as its key."
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    A factory rather than a module-level singleton so tests can construct an
    isolated app without importing process-wide state.
    """
    settings = settings or get_settings()
    _assert_secure(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Growth agent for small businesses: SEO content, AI-answer "
            "visibility, social content, and lead capture."
        ),
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": _redact_validation_errors(exc.errors())},
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin_models.router)
    app.include_router(connections.router)
    app.include_router(cost.router)
    app.include_router(dashboard.router)
    app.include_router(posts.router)
    app.include_router(documents.router)
    app.include_router(onboarding.router)
    app.include_router(runs.router)
    app.include_router(feedback.router)

    # PUBLIC routes: no session, reachable by a stranger. Mounted separately and last so
    # that "which of these is public?" is answered here rather than by reading each
    # module. links serves the tracked redirect and the bio-link hub; pages serves the
    # generated landing page a tracked link points AT; leads.public_router is the form
    # endpoint that page posts to.
    app.include_router(links.router)
    app.include_router(pages.router)
    app.include_router(leads.public_router)
    app.include_router(leads.router)

    # Middleware order is the reverse of the order it is added in: Starlette puts each
    # new one on the OUTSIDE, so the last call here is the outermost layer. The stack
    # this produces, outermost first, is
    #
    #     CORS  ->  Origin CSRF  ->  body-size limit  ->  routes
    #
    # and each position is a decision.
    #
    # CORS outermost so that a refusal from either guard still gets its
    # `Access-Control-Allow-Origin` header and the browser can read the status
    # instead of reporting a generic network error -- and so a preflight OPTIONS is
    # answered by CORS and never reaches the guards at all.
    #
    # CSRF outside the size limit because a forged request should be refused on its
    # headers, before a single byte of its body is counted.
    #
    # Both outside the routes, which is the whole point: an oversized login body must
    # not reach argon2, and a forged write must not reach a database session.
    app.add_middleware(
        BodySizeLimitMiddleware,
        # One override, and it is the only route in the product that legitimately
        # receives a file. The default ceiling is 64 KiB -- enough for every JSON body
        # here and far too small for a service brochure -- so the limit is raised for
        # this prefix ALONE rather than globally: an oversized login body must still
        # not reach argon2, which is what the default is protecting.
        #
        # 25 MiB is a generous price list and a mean-spirited photo album. The file is
        # read into memory to be extracted, so this number is also a bound on what one
        # request can make this process allocate.
        overrides=(BodyLimit("/api/v1/documents", 25 * 1024 * 1024),),
    )
    app.add_middleware(
        OriginCsrfMiddleware,
        allowed_origins=settings.cors_origins,
        # Resolved once, here, from the same predicate that decides `Secure` -- the
        # cookie's name is environment-dependent now (see core.cookies), so a
        # hardcoded name in the guard would leave every authenticated write
        # unprotected in exactly the environments that need protecting.
        session_cookie_name=session_cookie_name(settings),
    )
    app.add_middleware(
        CORSMiddleware,
        # The same tuple the CSRF guard above is built from. One list: an origin
        # permitted to make a credentialed call and an origin permitted to make a
        # state-changing one are the same question, and two lists would answer it
        # differently within a release.
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        # Every method the API serves. A missing entry breaks that endpoint from a
        # BROWSER only, and it breaks it at the preflight -- so the server logs nothing
        # and the UI reports a generic network error. PUT was missing here, and every
        # admin save failed silently while every test passed.
        #
        # Deliberately not derived from app.routes: this FastAPI version wraps included
        # routers in _IncludedRouter objects that expose no `methods`, so the walk finds
        # only the docs endpoints. test_cors_allows_every_method_the_app_actually_serves
        # reads the OpenAPI schema instead and fails if this list falls behind.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    return app


app = create_app()
