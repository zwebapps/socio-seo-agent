"""The two public link surfaces: the tracked redirect, and the bio-link hub.

Both routes are **unauthenticated by design**, and that is the point rather than a
concession. docs/CHANNELS.md section 1: an Instagram feed caption and a TikTok
caption do not render a clickable link, so a UTM-tagged URL pasted into either is
not a broken link -- it is no link at all. Attribution therefore cannot be a
property of publishing. It has to be a property of a redirect we own, which works
whether we published the post, the owner pasted the caption by hand, or the traffic
came from a channel we cannot publish to at all.

Four rules shape this module.

**The visitor never pays for our measurement.** The click write runs as a
background task *after* the 302 has been handed to the visitor, and it is wrapped so
that no exception can escape it. Losing one analytics row is trivial; losing a lead
is the only thing this product is judged on.

**No user agent and no IP is ever passed onward.** The UA is read here, decides one
boolean through ``link_service.is_bot``, and is dropped. The referrer is reduced to
a host before it leaves this module, because a referrer *path* can carry a search
query, a session id or a token. See ``LinkClick`` in ``backend/app/db/models.py``.

**Nothing from the request is reflected.** A 404 does not echo the code it refused.
A malformed code is refused on shape alone, before it can become the one database
lookup in this product that runs without a tenant scope (see
``db/adapters/lead_store.py``).

**The hub is public, so it publishes nothing private.** Notably not click counts:
CTA performance is the business's own data, and this page is readable by anyone who
tries the URL. Links are returned as *relative paths* -- the ``Host`` header is
caller-controlled, so building absolute URLs from it would let a poisoned Host point
every CTA at somebody else's domain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Final, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from starlette.background import BackgroundTask

from backend.app.db.adapters.lead_store import HubCta, PostgresLeadStore, ShortLinkRecord
from backend.app.services.link_service import (
    CODE_ALPHABET,
    MAX_CODE_LENGTH,
    MIN_CODE_LENGTH,
    is_bot,
)

router = APIRouter(tags=["links"])

logger = logging.getLogger(__name__)

_CODE_CHARS: Final = frozenset(CODE_ALPHABET)

#: A ``Location`` on a 302 must not be cached, by a browser or by anything between
#: us and the browser. A cached redirect is a click that never reaches us, and on a
#: bio link -- which is fetched constantly -- it would silently delete most of the
#: measurement this endpoint exists to produce.
_NO_STORE: Final = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

#: The hub changes when a CTA is approved, which is minutes-scale at best. A short
#: shared cache absorbs the burst of previewer traffic a pasted bio link attracts
#: without making a newly approved CTA wait long to appear.
_HUB_CACHE: Final = {"Cache-Control": "public, max-age=60"}


class LinkStore(Protocol):
    """What these two routes need from persistence, and nothing more."""

    async def resolve(self, code: str) -> ShortLinkRecord | None: ...

    async def record_click(
        self,
        link_id: UUID,
        business_id: UUID,
        *,
        referrer_host: str | None,
        is_bot: bool,
    ) -> None: ...

    async def list_hub_ctas(self, business_id: UUID) -> list[HubCta]: ...

    async def business_name(self, business_id: UUID) -> str | None: ...


def get_store() -> LinkStore:
    """The persistence adapter. Overridden in tests, which is why it is a function."""
    return PostgresLeadStore()


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HubBusiness(CamelModel):
    id: UUID
    name: str


class HubEntry(CamelModel):
    """One CTA on the hub.

    ``path`` rather than ``url``: relative on purpose (see the module docstring),
    and named so a client cannot mistake it for something already absolute. There is
    deliberately no click count here.
    """

    label: str
    path: str
    channel: str | None
    campaign: str | None


class HubResponse(CamelModel):
    business: HubBusiness
    ctas: list[HubEntry]


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


#: One string for every reason a link is not served -- unknown code, malformed
#: code, withdrawn link. Distinguishing them would turn the endpoint into an oracle
#: for which codes exist, and the visitor can act on none of the distinctions.
_NOT_FOUND: Final = _error("not_found", "That link is not available.")


def _is_well_formed(code: str) -> bool:
    """Whether ``code`` could have been produced by ``link_service.new_code``.

    Checked before the store is touched. The resolve is the one lookup in this
    product that runs on a privileged connection without a tenant scope, so keeping
    obvious garbage away from it is worth six lines.
    """
    return MIN_CODE_LENGTH <= len(code) <= MAX_CODE_LENGTH and set(code) <= _CODE_CHARS


def _referrer_host(referrer: str | None) -> str | None:
    """The host of ``referrer``, or ``None``.

    Everything except the host is discarded here, at the boundary, so no other
    module ever holds the full referrer. A value we cannot parse becomes ``None``
    rather than being stored raw -- an unparseable referrer is attacker-supplied
    text, and it answers no question we ask.
    """
    if not referrer:
        return None
    try:
        host = urlsplit(referrer.strip()).hostname
    except ValueError:
        return None
    if not host or len(host) > 255:
        return None
    return host.lower()


async def _record_click_safely(
    store: LinkStore,
    link: ShortLinkRecord,
    *,
    referrer_host: str | None,
    bot: bool,
) -> None:
    """Write the click, and swallow anything that goes wrong.

    Runs as a background task, so the visitor already has the 302 by the time this
    executes -- but an exception escaping a background task is still logged as an
    unhandled error and, on some servers, aborts the connection after the response
    has begun. Catching it here is what makes "the redirect always wins" true rather
    than mostly true.

    ``Exception`` deliberately, not a narrow tuple: the whole contract of this
    function is that nothing it does can affect the visitor, and enumerating the
    ways a database call can fail would make that contract depend on the list being
    complete.
    """
    try:
        await store.record_click(link.id, link.business_id, referrer_host=referrer_host, is_bot=bot)
    except Exception:
        logger.warning(
            "click not recorded for short_link %s (business %s)",
            link.id,
            link.business_id,
            exc_info=True,
        )


@router.get(
    "/l/{code}",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Follow a tracked short link",
    responses={302: {"description": "Redirect to the campaign target"}},
)
async def follow(
    code: str,
    request: Request,
    store: Annotated[LinkStore, Depends(get_store)],
) -> RedirectResponse:
    """302 to the link's target, and record the click without the visitor waiting.

    302 rather than 301: a permanent redirect is cached by browsers indefinitely,
    which means the second click from that device is never seen by us and the target
    can never be repointed.
    """
    if not _is_well_formed(code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    link = await store.resolve(code)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    bot = is_bot(request.headers.get("user-agent"))
    referrer_host = _referrer_host(request.headers.get("referer"))

    return RedirectResponse(
        url=link.target_url,
        status_code=status.HTTP_302_FOUND,
        headers=_NO_STORE,
        background=BackgroundTask(
            _record_click_safely,
            store,
            link,
            referrer_host=referrer_host,
            bot=bot,
        ),
    )


@router.get(
    "/go/{slug}",
    response_model=HubResponse,
    response_model_by_alias=True,
    summary="The business's link hub — the Instagram and TikTok bio link",
)
async def hub(
    slug: str,
    response: Response,
    store: Annotated[LinkStore, Depends(get_store)],
) -> HubResponse:
    """The public list of a business's active CTAs, each as a tracked short link.

    ``slug`` is the business id today. ``businesses`` has no slug column, and adding
    one is a migration this module cannot make; deriving a readable slug from the
    business *name* was rejected instead of deferred, because it is ambiguous the
    first time two customers share a name and it would need a full-table scan on a
    public endpoint to resolve.

    An empty hub is a 200 with no entries, not a 404: a business that has just
    signed up has no CTAs yet, and a 404 would make a freshly pasted bio link look
    broken.
    """
    try:
        business_id = UUID(slug)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from exc

    name = await store.business_name(business_id)
    if name is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    ctas = await store.list_hub_ctas(business_id)
    response.headers.update(_HUB_CACHE)
    return HubResponse(
        business=HubBusiness(id=business_id, name=name),
        ctas=[
            HubEntry(
                label=cta.label or (cta.campaign or "Mehr erfahren"),
                path=f"/l/{cta.code}",
                channel=cta.channel,
                campaign=cta.campaign,
            )
            for cta in ctas
        ],
    )


if TYPE_CHECKING:  # pragma: no cover - a compile-time conformance check

    def _store_satisfies_port(store: PostgresLeadStore) -> LinkStore:
        """Fails type checking the moment the adapter drifts from what these routes need."""
        return store
