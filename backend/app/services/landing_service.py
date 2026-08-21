"""Publishing a landing page: store the page, then mint one tracked link per CTA.

This is the seam between what a model wrote and what the world can reach. The
agent's `CONVERT` node produces a :class:`LandingPageSpec`; the `landing` engine
decides whether it can convert and what markup it becomes; this service is the
impure step in the middle -- it writes the row, mints the short links, and hands
back the CTA copy with the URL to paste after it.

Three decisions do the work here.

**A page with an error-severity finding is refused, not published.** The
deterministic audit already names each one ("there is no form", "the form asks for
no email address", "no channel CTA was written"), and every one of them means the
page cannot produce a lead. Publishing it anyway would put a URL into somebody's
Instagram bio that converts nothing while looking finished -- which is worse than
refusing, because the failure is invisible. This is the same refusal
``render_landing_page`` makes structurally; making it here as well means the caller
finds out before a visitor does.

**One short link per channel, and the link is retargeted once it exists.** The
short-link code is minted by the insert -- the unique index on ``short_links.code``
is the only real uniqueness guarantee -- so the page cannot know which link brought
a visitor until after the row exists. The publish path therefore creates the link
and then completes its target with ``?ref=<code>``, which is what lets a submitted
lead carry ``short_link_id`` rather than only its UTM parameters. The UTM tags
themselves are applied by ``lead_store.create_link``, so the destination's own
analytics sees the same channel ours does.

**The absolute URL comes from configuration, never from a request.** ``Host`` is
caller-controlled, so building a campaign's links from it would let a poisoned
header point every CTA at somebody else's domain -- the same reasoning
``api/links.py`` documents for returning relative paths.

The two stores are Protocols, so this module is testable with no database, and the
Postgres adapters are a separate, replaceable thing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from backend.app.core.config import get_settings
from backend.app.db.adapters.content_store import ContentPieceRecord
from backend.app.db.adapters.lead_store import ShortLinkRecord
from backend.app.engines.landing import (
    ChannelCta,
    LandingCheckRequest,
    LandingCheckResult,
    LandingPageSpec,
    check_landing_page,
    render_landing_markdown,
)
from backend.app.services.link_service import KNOWN_CHANNELS, with_ref
from backend.app.services.slugs import slugify_business_name

__all__ = [
    "ContentStore",
    "LandingPageNotPublishableError",
    "LinkStore",
    "PublishedCta",
    "PublishedLandingPage",
    "landing_page_path",
    "publish_landing_page",
]


#: The public path a landing page is served on. One function rather than an f-string
#: at three call sites, because the page URL is baked into every short link a campaign
#: ever mints -- if it moves, the links that are already in somebody's bio must keep
#: working, and that is only reviewable if there is one place to look.
def landing_page_path(content_piece_id: UUID) -> str:
    """The path ``GET /p/{piece_id}`` serves this page on."""
    return f"/p/{content_piece_id}"


class ContentStore(Protocol):
    """What publishing needs from content persistence, and nothing more."""

    async def create_landing_page(
        self,
        business_id: UUID,
        *,
        title: str,
        slug: str | None,
        body_md: str,
        spec: dict[str, Any],
        run_id: UUID | None = ...,
        opportunity_id: UUID | None = ...,
        status: str = ...,
    ) -> ContentPieceRecord: ...


class LinkStore(Protocol):
    """What publishing needs from the lead store, and nothing more."""

    async def create_link(
        self,
        business_id: UUID,
        *,
        target_url: str,
        content_piece_id: UUID | None = ...,
        channel: str | None = ...,
        campaign: str | None = ...,
    ) -> ShortLinkRecord: ...

    async def retarget_link(self, business_id: UUID, link_id: UUID, *, target_url: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PublishedCta:
    """One channel's ask, and the tracked link that goes after it.

    Both a ``path`` and a ``url`` are returned, and the distinction matters: the path
    is what our own UI renders (relative, so it cannot be poisoned), the absolute URL
    is what a human pastes into a post on a platform that has never heard of us.
    """

    channel: str
    text: str
    code: str
    path: str
    url: str


@dataclass(frozen=True, slots=True)
class PublishedLandingPage:
    """The result of publishing: the page, its verdict, and its CTAs."""

    content_piece_id: UUID
    business_id: UUID
    status: str
    path: str
    url: str
    report: LandingCheckResult
    ctas: tuple[PublishedCta, ...]


class LandingPageNotPublishableError(Exception):
    """The page cannot capture a lead, so it was not published.

    Carries the whole verdict rather than a message, because the caller's next move
    is to feed the fix hints back to the model -- and a caller given only a sentence
    would have to re-run the audit to get them.
    """

    def __init__(self, report: LandingCheckResult) -> None:
        self.report = report
        problems = "; ".join(f"{f.code}: {f.message}" for f in report.errors)
        super().__init__(
            f"the landing page scored {report.score}/100 and cannot be published: "
            f"{problems}. Publishing it would put a URL in somebody's bio that "
            "converts nothing while looking finished."
        )


@dataclass(frozen=True, slots=True)
class PublishedDistribution:
    """What a run leaves behind when it hosts no page of its own.

    The same shape of result as :class:`PublishedLandingPage` minus the page: an article
    content piece as the attribution anchor, and one tracked link per channel pointing at
    a page the BUSINESS owns.
    """

    content_piece_id: UUID
    business_id: UUID
    destination_url: str
    ctas: tuple[PublishedCta, ...]


class DestinationNotOwnedError(Exception):
    """The destination is not on the business's own site.

    Refused rather than corrected, and this is the load-bearing guard of the whole
    change. Once we stop hosting the page, the CTA target comes from a MODEL reading a
    crawled website — attacker-influenceable text — and a run that pointed a business's
    tracked links at somebody else's domain would be sending their audience away under
    their own name. So the destination must be same-origin with `dna["website"]`, and
    anything else is an error rather than a fallback: silently substituting the homepage
    would hide that the model proposed an off-site target at all.
    """


def _same_origin(candidate: str, website: str) -> bool:
    """Whether `candidate` is on the same site as `website`.

    Host compared without `www.` and case-folded, because `example.de` and
    `www.Example.de` are one site and refusing that pair would reject most real
    homepages. Scheme is NOT compared: a business whose profile says `http://` and whose
    links say `https://` is the ordinary case, and treating it as a different site would
    refuse every one of them.
    """
    from urllib.parse import urlsplit

    def host(value: str) -> str:
        return urlsplit(value.strip()).netloc.lower().removeprefix("www.")

    theirs = host(website)
    return bool(theirs) and host(candidate) == theirs


async def publish_distribution(
    *,
    business_id: UUID,
    destination_url: str,
    website: str,
    ctas: Sequence[ChannelCta],
    article_title: str,
    article_body_md: str,
    content_store: Any,
    link_store: LinkStore,
    campaign: str | None = None,
    run_id: UUID | None = None,
    base_url: str | None = None,
) -> PublishedDistribution:
    """Mint the tracked links for a run that hosts no page.

    The founder's ruling (recorded in `CLAUDE.md`, 2026-08-21): we do not host a landing
    page, because the business already has a website — we improve its SEO and promote the
    business on social. So this replaces `publish_landing_page` on the run path.

    **The article piece is still written, and it is not vestigial.** A tracked link and a
    queued social post both attribute to a `content_pieces` id; without a row there is
    nothing for a click to be attributed TO, and "which content earned this" becomes
    unanswerable. It is `surface='article'`, served nowhere.

    **What is lost, stated so nothing downstream implies otherwise:** the form was ours,
    so a submission was a `leads` row attributed to a piece. The form is now theirs and
    invisible to us, so attribution is CLICK-level. `link_clicks` still records channel,
    piece and campaign — the short link is still ours, which is why a channel we cannot
    post to is still measurable.

    Raises :class:`DestinationNotOwnedError` before writing anything.
    """
    if not _same_origin(destination_url, website):
        raise DestinationNotOwnedError(f"the destination {destination_url!r} is not on {website!r}")

    base = (base_url or get_settings().public_base_url).rstrip("/")
    title = (article_title or "Untitled").strip()[:512]
    campaign_slug = slugify_business_name(campaign or title) or "article"

    piece = await content_store.create_article_piece(
        business_id, title=title, body_md=article_body_md, run_id=run_id
    )

    published: list[PublishedCta] = []
    for cta in ctas:
        link = await link_store.create_link(
            business_id,
            target_url=destination_url,
            content_piece_id=piece.id,
            channel=cta.channel,
            campaign=campaign_slug,
        )
        # The code exists only now, so the target is completed with it — the same two-write
        # shape `publish_landing_page` uses, and for the same reason: `with_ref` needs the
        # code, and a failure between them leaves a link that still resolves to the
        # customer's page without per-link attribution.
        await link_store.retarget_link(
            business_id, link.id, target_url=with_ref(link.target_url, link.code)
        )
        published.append(
            PublishedCta(
                channel=cta.channel,
                text=cta.text,
                code=link.code,
                path=f"/l/{link.code}",
                url=f"{base}/l/{link.code}",
            )
        )

    return PublishedDistribution(
        content_piece_id=piece.id,
        business_id=business_id,
        destination_url=destination_url,
        ctas=tuple(published),
    )


async def publish_landing_page(
    *,
    business_id: UUID,
    spec: LandingPageSpec,
    content_store: ContentStore,
    link_store: LinkStore,
    title: str | None = None,
    campaign: str | None = None,
    run_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    status: str = "draft",
    base_url: str | None = None,
) -> PublishedLandingPage:
    """Store ``spec`` as a content piece and mint one tracked link per channel CTA.

    ``status`` defaults to ``draft``: a generated page has not been approved, and both
    the public page route and the public form endpoint refuse anything that is not
    ``approved`` or ``published``. The links are minted anyway, and that is correct --
    the link hub filters on the piece's status, so a draft's CTAs exist without being
    advertised, and approving the page lights them up without touching them.

    Raises :class:`LandingPageNotPublishableError` before writing anything if the
    deterministic audit finds an error. Nothing is half-written: the check runs first.
    """
    report = check_landing_page(
        LandingCheckRequest(spec=spec, known_channels=sorted(KNOWN_CHANNELS))
    )
    if report.errors:
        raise LandingPageNotPublishableError(report)

    base = (base_url or get_settings().public_base_url).rstrip("/")
    page_title = (title or spec.headline).strip()[:512]
    # `slugs.slugify_business_name`, not `link_service.slugify`: this is a public,
    # readable address for a German business, and the latter transliterates nothing --
    # it would turn "für Hauseigentümer" into "f-r-hauseigent-mer". That module's
    # 80-character cap applies, which is shorter than the column allows and is the
    # right answer anyway: a slug longer than that is not a better address.
    slug = slugify_business_name(page_title)
    campaign_slug = slugify_business_name(campaign or page_title) or "landing"

    piece = await content_store.create_landing_page(
        business_id,
        title=page_title,
        slug=slug,
        body_md=render_landing_markdown(spec),
        spec=spec.model_dump(mode="json"),
        run_id=run_id,
        opportunity_id=opportunity_id,
        status=status,
    )

    path = landing_page_path(piece.id)
    page_url = f"{base}{path}"

    published: list[PublishedCta] = []
    for cta in spec.ctas:
        link = await link_store.create_link(
            business_id,
            target_url=page_url,
            content_piece_id=piece.id,
            channel=cta.channel,
            campaign=campaign_slug,
        )
        # The code exists only now, so the target is completed with it. Second write,
        # same transaction boundary as any other: a failure here leaves a link that
        # still resolves to the page, just without per-link lead attribution.
        await link_store.retarget_link(
            business_id, link.id, target_url=with_ref(link.target_url, link.code)
        )
        published.append(
            PublishedCta(
                channel=cta.channel,
                text=cta.text,
                code=link.code,
                path=f"/l/{link.code}",
                url=f"{base}/l/{link.code}",
            )
        )

    return PublishedLandingPage(
        content_piece_id=piece.id,
        business_id=business_id,
        status=piece.status,
        path=path,
        url=page_url,
        report=report,
        ctas=tuple(published),
    )


if TYPE_CHECKING:  # pragma: no cover - a compile-time conformance check
    from backend.app.db.adapters.content_store import PostgresContentStore
    from backend.app.db.adapters.lead_store import PostgresLeadStore

    def _content_store_satisfies_port(store: PostgresContentStore) -> ContentStore:
        """Fails type checking the moment the adapter drifts from what this needs."""
        return store

    def _link_store_satisfies_port(store: PostgresLeadStore) -> LinkStore:
        """Same, for the lead store."""
        return store
