"""The public landing page: ``GET /p/{piece_id}``.

The third link of the lead chain (docs/FEATURES.md section 0) had no public surface
until this route existed. `REACH` and `RELEVANCE` are the blog and social
generation; `ATTRIBUTION` is ``/l/{code}``, ``/go/{slug}`` and the lead form. But a
tracked short link pointing at a page that does not exist earns nothing, and this is
the page it points at.

It is **unauthenticated by design**, in exactly the posture the other two public
routes document. A visitor arriving from an Instagram bio has no session and no
business context, so the id in the URL is the only thing that names the tenant --
which is why the lookup goes through the ``SECURITY DEFINER`` resolver added by
migration ``4d2b7f9c1e83`` rather than through a privileged connection.

Five rules shape this module.

**No JavaScript and no cookie.** The page is server-rendered HTML with a plain
``method="post"`` form, and nothing here writes a cookie -- so it works with
scripting off, it needs no consent banner of its own, and it can be cached by
anything between us and the visitor. The markup comes from the pure
``engines.landing.render_landing_page``, so the escaping is unit-tested rather than
reviewed by eye.

**Nothing from the request is reflected.** The id is not echoed by the 404. ``ref``
is validated by SHAPE and dropped if it could not be one of our codes. Query
parameters are passed into the form only if they are shaped like UTM parameters.
Everything that does reach the markup is HTML-escaped by the renderer.

**Every refusal is the same 404.** Unknown id, malformed id, a draft page, a content
piece that is not a landing page, and a stored spec we cannot read all answer
identically. A 403 on the draft would confirm that unpublished work exists at that
id, and the visitor can act on none of the distinctions.

**The refusal is HTML, not JSON.** Unlike ``/l/{code}``, this route is read by a
person in a browser, and handing them ``{"detail": ...}`` would be a worse answer to
the same question. The body is a fixed document with nothing in it from the request.

**The confirmation is a state of the same page, not a second page.** After a
submission the form endpoint redirects back here with ``?sent=1``, which renders the
same document with the form replaced by a confirmation -- so the no-JavaScript flow
needs no thank-you route and no client-side rendering. Those two states are
``no-store``, because a cached confirmation on the back button would tell a visitor
they had submitted something they had not.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Final, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from backend.app.db.adapters.content_store import (
    LANDING_SURFACE,
    LandingPageTarget,
    PostgresContentStore,
)
from backend.app.engines.landing import (
    LandingPageSpec,
    PageState,
    RenderRefusedError,
    render_landing_page,
)

router = APIRouter(tags=["pages"])

logger = logging.getLogger(__name__)

#: The statuses whose page may be served. A draft has not been approved, and its copy
#: may promise something the business would not -- the same list the public form
#: endpoint refuses on, for the same reason.
LIVE_STATUSES: Final = frozenset({"approved", "published"})

#: A landing page changes when it is edited and re-approved, which is minutes-scale at
#: best. A short shared cache absorbs the burst a pasted campaign link attracts without
#: making an approved edit wait long to appear.
_CACHE: Final = {"Cache-Control": "public, max-age=60"}

#: The confirmation and error states must never be cached: a visitor pressing back
#: onto a cached "thank you" would be told they had submitted something they had not.
_NO_STORE: Final = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

#: One body for every reason a page is not served. See the module docstring: nothing
#: from the request appears in it, and the reasons are indistinguishable.
_NOT_FOUND_HTML: Final = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Not available</title></head>"
    "<body><main><h1>This page is not available.</h1>"
    "<p>The link may have expired, or the page may not be published yet.</p>"
    "</main></body></html>"
)


class PageStore(Protocol):
    """What this route needs from persistence, and nothing more."""

    async def resolve_landing_page(self, piece_id: UUID) -> LandingPageTarget | None: ...


def get_store() -> PageStore:
    """The persistence adapter. Overridden in tests, which is why it is a function."""
    return PostgresContentStore()


def form_action(piece_id: UUID) -> str:
    """Where this page's form posts.

    The public lead endpoint, keyed on the content piece -- which is what makes the
    submitted lead attributable to this page without the form carrying a business id
    the visitor could change.
    """
    return f"/public/forms/{piece_id}"


def _not_found() -> HTMLResponse:
    return HTMLResponse(_NOT_FOUND_HTML, status_code=404, headers=_NO_STORE)


def _page_state(request: Request) -> PageState:
    """Which of the three states to render.

    Presence, not value: ``?sent=1`` and ``?sent=yes`` mean the same thing, and
    comparing the value would let a wrong one silently show the form again to somebody
    who had just submitted it.
    """
    if "sent" in request.query_params:
        return "sent"
    if "error" in request.query_params:
        return "error"
    return "form"


def _utm(request: Request) -> dict[str, str]:
    """The UTM parameters on this request, to ride along in the form.

    Filtered by prefix here and again by shape in the renderer. The values are not
    trusted, only carried: they end up on the lead as the campaign that produced it,
    and the endpoint that stores them keeps only the five real keys.
    """
    return {
        key: value
        for key, value in request.query_params.items()
        if key.startswith("utm_") and value
    }


@router.get(
    "/p/{piece_id}",
    response_class=HTMLResponse,
    summary="A generated landing page (public, unauthenticated, no JavaScript)",
    responses={404: {"description": "No such page, or it is not published"}},
)
async def landing_page(
    piece_id: str,
    request: Request,
    store: Annotated[PageStore, Depends(get_store)],
) -> HTMLResponse:
    """Render one landing page, or the fixed 404.

    ``piece_id`` is parsed here rather than declared as a ``UUID`` path parameter so
    that a malformed id produces the same 404 as an unknown one instead of a 422 that
    describes our schema to a stranger.
    """
    try:
        content_piece_id = UUID(piece_id)
    except ValueError:
        return _not_found()

    target = await store.resolve_landing_page(content_piece_id)
    if target is None or target.status not in LIVE_STATUSES:
        return _not_found()
    if target.surface != LANDING_SURFACE:
        return _not_found()

    try:
        spec = LandingPageSpec.model_validate(target.spec)
        html = render_landing_page(
            spec,
            business_name=target.business_name,
            form_action=form_action(content_piece_id),
            ref=request.query_params.get("ref", ""),
            utm=_utm(request),
            locale=target.locale,
            state=_page_state(request),
        )
    except (ValueError, RenderRefusedError):
        # Our bug, not the visitor's: a stored spec that cannot be read or cannot be
        # rendered. Logged at ERROR because it is a lost lead on a live campaign link,
        # and answered with the same 404 as everything else -- a stack trace on a
        # public marketing page would be a worse answer to the same question.
        logger.exception("landing page %s could not be rendered", content_piece_id)
        return _not_found()

    state = _page_state(request)
    return HTMLResponse(html, headers=dict(_NO_STORE if state != "form" else _CACHE))


if TYPE_CHECKING:  # pragma: no cover - a compile-time conformance check

    def _store_satisfies_port(store: PostgresContentStore) -> PageStore:
        """Fails type checking the moment the adapter drifts from what this route needs."""
        return store
