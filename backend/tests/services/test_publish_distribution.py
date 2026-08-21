"""Publishing a run that hosts no page of its own.

The founder's ruling: we do not host a landing page, because the business already has
a website. So the run mints tracked links pointing at THEIR site — and the moment the
CTA target stops being a page we control, its origin becomes the thing worth testing.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.engines.landing import ChannelCta
from backend.app.services.landing_service import (
    DestinationNotOwnedError,
    publish_distribution,
)

BUSINESS: UUID = UUID("11111111-1111-4111-8111-111111111111")
SITE = "https://mueller-sanitaer.de"


class _ContentStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_article_piece(
        self, business_id: UUID, *, title: str, body_md: str, run_id: UUID | None = None
    ) -> Any:
        self.created.append({"title": title, "body_md": body_md, "run_id": run_id})
        return type("Piece", (), {"id": uuid4(), "status": "approved"})()


class _Link:
    def __init__(self, code: str, target: str) -> None:
        self.id = uuid4()
        self.code = code
        self.target_url = target


class _LinkStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.retargeted: list[str] = []
        self._n = 0

    async def create_link(
        self,
        business_id: UUID,
        *,
        target_url: str,
        # Optional, matching the `LinkStore` protocol rather than only the call this
        # test makes: a narrower double satisfies the test and not the type, and then
        # `mypy --strict` is the thing that notices.
        content_piece_id: UUID | None = None,
        channel: str | None = None,
        campaign: str | None = None,
    ) -> Any:
        self._n += 1
        self.created.append({"target_url": target_url, "channel": channel, "campaign": campaign})
        return _Link(f"code{self._n}", target_url)

    async def retarget_link(self, business_id: UUID, link_id: UUID, *, target_url: str) -> None:
        self.retargeted.append(target_url)


async def _publish(destination: str, website: str = SITE) -> Any:
    return await publish_distribution(
        business_id=BUSINESS,
        destination_url=destination,
        website=website,
        ctas=[ChannelCta(channel="linkedin", text="Book a callout")],
        article_title="Notdienst in Koblenz",
        article_body_md="# hi",
        content_store=_ContentStore(),
        link_store=_LinkStore(),
        base_url="https://app.example",
    )


async def test_links_point_at_the_businesss_own_page() -> None:
    result = await _publish(f"{SITE}/notdienst")

    assert result.destination_url == f"{SITE}/notdienst"
    assert result.ctas[0].url == "https://app.example/l/code1"


async def test_an_off_site_destination_is_refused_before_anything_is_written() -> None:
    """The load-bearing guard of the whole change.

    Once we stop hosting the page, the CTA target comes from a MODEL reading a crawled
    website — attacker-influenceable text. A run that pointed a business's tracked links
    at somebody else's domain would send their audience away under their own name.
    """
    content, links = _ContentStore(), _LinkStore()

    with pytest.raises(DestinationNotOwnedError):
        await publish_distribution(
            business_id=BUSINESS,
            destination_url="https://competitor.example/offer",
            website=SITE,
            ctas=[ChannelCta(channel="linkedin", text="Book")],
            article_title="t",
            article_body_md="b",
            content_store=content,
            link_store=links,
        )

    assert content.created == [], "nothing is half-written"
    assert links.created == []


@pytest.mark.parametrize(
    "destination",
    [
        "https://www.mueller-sanitaer.de/notdienst",
        "http://mueller-sanitaer.de/notdienst",
        "https://MUELLER-SANITAER.DE/notdienst",
    ],
)
async def test_www_scheme_and_case_are_the_same_site(destination: str) -> None:
    """Refusing these would reject most real homepages: a profile saying `http://` while
    the links say `https://`, or a `www.` host, is the ordinary case rather than an
    attack."""
    result = await _publish(destination)

    assert result.destination_url == destination


async def test_a_lookalike_domain_is_not_the_same_site() -> None:
    """The case a `startswith` or an `in` check would wave through."""
    with pytest.raises(DestinationNotOwnedError):
        await _publish("https://mueller-sanitaer.de.evil.example/notdienst")


async def test_a_subdomain_is_not_the_same_site() -> None:
    """Deliberate. A subdomain can be somebody else entirely — a hosted status page, a
    shop on a platform — and the business named its site, not its zone."""
    with pytest.raises(DestinationNotOwnedError):
        await _publish("https://shop.mueller-sanitaer.de/x")


async def test_a_business_with_no_website_cannot_have_a_destination() -> None:
    """`_same_origin` returns False for an empty site rather than matching everything,
    which is what an empty-host comparison would otherwise do."""
    with pytest.raises(DestinationNotOwnedError):
        await _publish(f"{SITE}/x", website="")


async def test_the_article_piece_is_written_as_the_attribution_anchor() -> None:
    """Not vestigial: a click has to be attributable TO something, and without a piece
    "which content earned this" is unanswerable."""
    content, links = _ContentStore(), _LinkStore()
    run_id = uuid4()

    await publish_distribution(
        business_id=BUSINESS,
        destination_url=f"{SITE}/notdienst",
        website=SITE,
        ctas=[ChannelCta(channel="linkedin", text="Book")],
        article_title="Notdienst in Koblenz",
        article_body_md="# body",
        content_store=content,
        link_store=links,
        run_id=run_id,
    )

    assert content.created[0]["title"] == "Notdienst in Koblenz"
    assert content.created[0]["run_id"] == run_id


async def test_the_link_carries_its_own_ref_after_minting() -> None:
    """Two writes on purpose: `with_ref` needs the code, which only exists after the
    insert. Without the second write a click cannot be attributed to the LINK."""
    content, links = _ContentStore(), _LinkStore()

    await publish_distribution(
        business_id=BUSINESS,
        destination_url=f"{SITE}/notdienst",
        website=SITE,
        ctas=[ChannelCta(channel="linkedin", text="Book")],
        article_title="t",
        article_body_md="b",
        content_store=content,
        link_store=links,
    )

    assert links.retargeted and "code1" in links.retargeted[0]
