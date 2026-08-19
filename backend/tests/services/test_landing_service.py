"""Publishing a landing page: the refusal, the links, and the attribution.

Hermetic -- both stores are fakes, so there is no database and no network here.

What is being pinned down:

* **a page that cannot capture a lead is refused before anything is written.** The
  interesting half is that nothing is half-created: a caller that gets the refusal has
  no orphan content piece and no orphan short links;
* **one tracked link per channel CTA, tagged and self-referencing.** The link carries
  the channel's UTM parameters AND ends up pointing at the page with ``?ref=<code>``,
  which is what lets a submitted lead name the exact link that produced it rather than
  only the channel;
* **the check and the renderer agree about what is unpublishable.** If the audit could
  pass a spec the renderer then refuses, every such page would be a 404 on a live
  campaign link. That relationship is asserted directly, over every combination of the
  three structural failures.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.db.adapters.content_store import LANDING_SURFACE, ContentPieceRecord
from backend.app.db.adapters.lead_store import ShortLinkRecord
from backend.app.engines.landing import (
    ChannelCta,
    FormField,
    LandingPageSpec,
    ProofPoint,
    RenderRefusedError,
    render_landing_page,
)
from backend.app.services.landing_service import (
    LandingPageNotPublishableError,
    landing_page_path,
    publish_landing_page,
)

BUSINESS = UUID("11111111-1111-4111-8111-111111111111")
BASE = "https://sma.example"


class FakeContentStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_landing_page(
        self,
        business_id: UUID,
        *,
        title: str,
        slug: str | None,
        body_md: str,
        spec: dict[str, Any],
        run_id: UUID | None = None,
        opportunity_id: UUID | None = None,
        status: str = "draft",
    ) -> ContentPieceRecord:
        self.created.append(
            {
                "business_id": business_id,
                "title": title,
                "slug": slug,
                "body_md": body_md,
                "spec": spec,
                "run_id": run_id,
                "status": status,
            }
        )
        return ContentPieceRecord(
            id=UUID("22222222-2222-4222-8222-222222222222"),
            business_id=business_id,
            surface=LANDING_SURFACE,
            title=title,
            slug=slug,
            status=status,
        )


class FakeLinkStore:
    """Enough of the lead store to mint and repoint links, plus a record of the calls.

    ``create_link`` applies the UTM tags the way the real adapter does, because the
    ``?ref=`` merge has to be shown NOT to break a target that already has a query
    string -- a fake that returned a bare URL would make that assertion vacuous.
    """

    def __init__(self) -> None:
        self.links: dict[UUID, ShortLinkRecord] = {}
        self.created: list[dict[str, Any]] = []
        self.retargeted: list[tuple[UUID, str]] = []
        self._codes = iter(["Aaa11111", "Bbb22222", "Ccc33333", "Ddd44444", "Eee55555"])

    async def create_link(
        self,
        business_id: UUID,
        *,
        target_url: str,
        content_piece_id: UUID | None = None,
        channel: str | None = None,
        campaign: str | None = None,
    ) -> ShortLinkRecord:
        from backend.app.services.link_service import apply_utm, build_utm

        tagged = target_url
        if channel and campaign:
            tagged = apply_utm(target_url, build_utm(channel=channel, campaign=campaign))
        record = ShortLinkRecord(
            id=uuid4(),
            business_id=business_id,
            code=next(self._codes),
            target_url=tagged,
            content_piece_id=content_piece_id,
            channel=channel,
            campaign=campaign,
            click_count=0,
        )
        self.links[record.id] = record
        self.created.append(
            {
                "channel": channel,
                "campaign": campaign,
                "target_url": tagged,
                "piece": content_piece_id,
            }
        )
        return record

    async def retarget_link(self, business_id: UUID, link_id: UUID, *, target_url: str) -> None:
        self.retargeted.append((link_id, target_url))
        old = self.links[link_id]
        self.links[link_id] = ShortLinkRecord(
            id=old.id,
            business_id=old.business_id,
            code=old.code,
            target_url=target_url,
            content_piece_id=old.content_piece_id,
            channel=old.channel,
            campaign=old.campaign,
            click_count=old.click_count,
        )


def _spec(**over: object) -> LandingPageSpec:
    base: dict[str, object] = {
        "headline": "Notdienst-Checkliste für Hauseigentümer in Koblenz",
        "subhead": "Fünf Prüfungen, bevor Sie den Notdienst rufen.",
        "offer": "Eine zweiseitige Checkliste mit den fünf Prüfungen bei einem Wasserschaden.",
        "proof_points": [
            ProofPoint(text="Seit 1998 in Koblenz.", source="Leistungsübersicht 2026"),
            ProofPoint(text="24-Stunden-Notdienst.", source="mueller-sanitaer.de/notdienst"),
        ],
        "form_fields": [FormField(name="email", label="E-Mail", required=True)],
        "primary_cta": "Checkliste anfordern",
        "consent_text": "Ich bin mit der Kontaktaufnahme einverstanden.",
        "ctas": [
            ChannelCta(channel="linkedin", text="Unsere Notdienst-Checkliste:"),
            ChannelCta(channel="link_hub", text="Notdienst-Checkliste, kostenlos"),
        ],
    }
    base.update(over)
    return LandingPageSpec.model_validate(base)


async def _publish(spec: LandingPageSpec | None = None, **over: Any) -> Any:
    content, links = FakeContentStore(), FakeLinkStore()
    kwargs: dict[str, Any] = {
        "business_id": BUSINESS,
        "spec": spec or _spec(),
        "content_store": content,
        "link_store": links,
        "base_url": BASE,
    }
    kwargs.update(over)
    published = await publish_landing_page(**kwargs)
    return published, content, links


# --------------------------------------------------------------------------- #
# The refusal
# --------------------------------------------------------------------------- #


async def test_a_page_with_no_form_is_refused_and_nothing_is_written() -> None:
    """A half-published page is worse than a refused one: the content piece would sit
    there with links pointing at a page that cannot convert."""
    content, links = FakeContentStore(), FakeLinkStore()

    with pytest.raises(LandingPageNotPublishableError) as exc:
        await publish_landing_page(
            business_id=BUSINESS,
            spec=_spec(form_fields=[]),
            content_store=content,
            link_store=links,
            base_url=BASE,
        )

    assert content.created == [], "the content piece must not exist"
    assert links.created == [], "no link may point at a page that cannot convert"
    assert [f.code for f in exc.value.report.errors]
    assert exc.value.report.fix_hints, "the caller's next move is to feed these back"


async def test_the_refusal_carries_the_whole_verdict_not_just_a_message() -> None:
    with pytest.raises(LandingPageNotPublishableError) as exc:
        await publish_landing_page(
            business_id=BUSINESS,
            spec=_spec(ctas=[]),
            content_store=FakeContentStore(),
            link_store=FakeLinkStore(),
            base_url=BASE,
        )

    assert exc.value.report.score < 100
    assert "channel_ctas" in {f.code for f in exc.value.report.errors}


@pytest.mark.parametrize(
    "override",
    [
        {"form_fields": []},
        {"primary_cta": ""},
        {"consent_text": ""},
        {"form_fields": [], "primary_cta": "", "consent_text": ""},
    ],
)
def test_the_audit_never_passes_a_spec_the_renderer_would_refuse(
    override: dict[str, object],
) -> None:
    """The relationship that keeps a published page from 404ing.

    ``render_landing_page`` refuses a spec that cannot capture a lead, and it runs at
    REQUEST time -- so if the audit could pass one, every visit to that live campaign
    link would be a logged 500-turned-404. Both refusals are derived from the same
    three facts, and this asserts they agree.
    """
    spec = _spec(**override)

    with pytest.raises(RenderRefusedError):
        render_landing_page(spec, business_name="X", form_action="/public/forms/x")

    from backend.app.engines.landing import LandingCheckRequest, check_landing_page

    verdict = check_landing_page(
        LandingCheckRequest(spec=spec, known_channels=["linkedin", "link_hub"])
    )
    assert verdict.passed is False, "the renderer would refuse this; the audit must too"


# --------------------------------------------------------------------------- #
# What gets written
# --------------------------------------------------------------------------- #


async def test_the_page_is_stored_as_a_draft_with_its_spec_and_a_readable_body() -> None:
    """A generated page has not been approved, and both public routes refuse anything
    that is not approved or published -- so a default of `published` would make the
    approval gate optional."""
    published, content, _ = await _publish()

    row = content.created[0]
    assert row["status"] == "draft"
    assert published.status == "draft"
    assert row["spec"]["headline"].startswith("Notdienst-Checkliste")
    assert row["spec"]["form_fields"][0]["name"] == "email"
    assert row["spec"]["proof_points"][0]["source"] == "Leistungsübersicht 2026", (
        "the stored spec must keep its sources: they are what makes a proof point checkable"
    )
    assert "# Notdienst-Checkliste" in row["body_md"], "the owner must be able to read it as text"
    # Transliterated, not stripped: `ü` becomes `ue`, because "f-r-hauseigent-mer" is
    # not an address a German business would put on anything.
    assert row["slug"] == "notdienst-checkliste-fuer-hauseigentuemer-in-koblenz"


async def test_the_page_url_is_built_from_configuration_and_not_from_a_request() -> None:
    """`Host` is caller-controlled: a poisoned header must not be able to point every
    CTA in a business's Instagram bio at somebody else's domain."""
    published, _, _ = await _publish()

    assert published.url == f"{BASE}{landing_page_path(published.content_piece_id)}"
    assert published.path.startswith("/p/")


async def test_one_tracked_link_is_minted_per_channel_cta_and_tagged() -> None:
    published, _, links = await _publish()

    assert [c["channel"] for c in links.created] == ["linkedin", "link_hub"]
    assert all(c["campaign"] for c in links.created), "an untagged link attributes nothing"
    assert all("utm_source=" in c["target_url"] for c in links.created)
    assert {cta.channel for cta in published.ctas} == {"linkedin", "link_hub"}


async def test_every_link_is_attached_to_the_content_piece_it_advertises() -> None:
    """This is what makes the link hub able to hide a draft page's CTAs, and what ties
    a click to the piece that earned it."""
    published, _, links = await _publish()

    assert {c["piece"] for c in links.created} == {published.content_piece_id}


async def test_each_link_is_completed_with_its_own_code_so_a_lead_names_the_link() -> None:
    """The code is minted by the insert, so the target cannot contain it until the row
    exists. Without this second write a lead carries its channel but not the link."""
    published, _, links = await _publish()

    assert len(links.retargeted) == 2
    for _link_id, target in links.retargeted:
        assert "ref=" in target
    for cta in published.ctas:
        stored = next(link for link in links.links.values() if link.code == cta.code)
        assert f"ref={cta.code}" in stored.target_url
        assert "utm_source=" in stored.target_url, "the ref merge must not drop the UTM tags"
        assert stored.target_url.count("?") == 1, "one query string, not two"


async def test_the_cta_carries_both_a_relative_path_and_an_absolute_url() -> None:
    """Our own UI renders the path -- relative, so it cannot be poisoned. The absolute
    URL is what a human pastes into a platform that has never heard of us."""
    published, _, _ = await _publish()

    cta = published.ctas[0]
    assert cta.path == f"/l/{cta.code}"
    assert cta.url == f"{BASE}/l/{cta.code}"
    assert cta.text, "a link with no copy in front of it is not a CTA"


async def test_the_campaign_defaults_to_the_page_so_clicks_group_by_content() -> None:
    _, _, links = await _publish()

    assert {c["campaign"] for c in links.created} == {
        "notdienst-checkliste-fuer-hauseigentuemer-in-koblenz"
    }


async def test_an_explicit_campaign_is_slugified_so_two_spellings_are_one_row() -> None:
    _, _, links = await _publish(campaign="Sommer Aktion 2026")

    assert {c["campaign"] for c in links.created} == {"sommer-aktion-2026"}


async def test_the_run_is_recorded_on_the_piece_so_the_page_traces_to_its_run() -> None:
    run_id = uuid4()
    _, content, _ = await _publish(run_id=run_id)

    assert content.created[0]["run_id"] == run_id
