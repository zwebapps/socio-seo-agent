"""The landing page against a real Postgres, and the whole conversion loop end to end.

Four things are proved here, and the last one is the reason the file exists.

1. **The unscoped resolve has to exist.** ``GET /p/{piece_id}`` is public: the visitor
   has no session and no business context, so the lookup cannot run inside
   ``business_session``. The first test proves the fact that makes the design necessary
   rather than convenient -- the restricted application role reads **zero rows** from
   ``content_pieces`` with no tenant GUC set, silently. So there is no "just query it"
   option, and ``resolve_landing_page`` (migration ``4d2b7f9c1e83``) is a narrow
   ``SECURITY DEFINER`` function rather than a privileged connection.

2. **Tenancy, asserted positively.** An unscoped query returns zero rows silently, so a
   test that only asserted "business B sees nothing" would pass against a store that is
   simply broken. Every isolation test below also reads the same row back as the owning
   business.

3. **The second write cannot cross a tenant, and cannot smuggle a scheme.**
   ``retarget_link`` exists so a link can point at a page with its own code in the
   query string; it re-validates the URL, because a guard that only runs on the create
   path is a guard with a second door next to it.

4. **The lead chain closes.** REACH → RELEVANCE → CONVERSION → ATTRIBUTION, in one
   test, over real SQL and real row-level security: publish a page, approve it, serve
   it with no JavaScript, submit its form as a browser would, and find a lead in the
   database attributed to the content piece AND to the exact short link that produced
   it -- with the click on that link counted. Before this, the last two links of the
   chain were only ever asserted separately.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from backend.app.api import leads as leads_api
from backend.app.core.rate_limit import (
    DIMENSION_IP,
    FixedWindowRateLimiter,
    InMemoryWindowCounter,
    RateLimitRule,
)
from backend.app.db import session as session_module
from backend.app.db.adapters.content_store import (
    LANDING_SURFACE,
    PostgresContentStore,
    UnknownContentPieceError,
)
from backend.app.db.adapters.lead_store import PostgresLeadStore, UnknownShortLinkError
from backend.app.db.session import business_session
from backend.app.engines.landing import ChannelCta, FormField, LandingPageSpec, ProofPoint
from backend.app.main import create_app
from backend.app.services.landing_service import publish_landing_page

pytestmark = [pytest.mark.db]

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BASE = "https://sma.example"


def a_spec(**over: Any) -> LandingPageSpec:
    base: dict[str, Any] = {
        "headline": "Notdienst-Checkliste für Hauseigentümer in Koblenz",
        "subhead": "Fünf Prüfungen, bevor Sie den Notdienst rufen.",
        "offer": "Eine zweiseitige Checkliste mit den fünf Prüfungen bei einem Wasserschaden.",
        "proof_points": [
            ProofPoint(text="Seit 1998 in Koblenz.", source="Leistungsübersicht 2026"),
            ProofPoint(text="24-Stunden-Notdienst.", source="mueller-sanitaer.de/notdienst"),
        ],
        "form_fields": [
            FormField(name="name", label="Ihr Name", required=False),
            FormField(name="email", label="E-Mail-Adresse", required=True),
        ],
        "primary_cta": "Checkliste anfordern",
        "consent_text": "Ich bin mit der Kontaktaufnahme einverstanden.",
        "ctas": [ChannelCta(channel="linkedin", text="Unsere Notdienst-Checkliste:")],
    }
    base.update(over)
    return LandingPageSpec.model_validate(base)


async def read_pieces(business_id: UUID) -> list[dict[str, Any]]:
    async with business_session(business_id) as db:
        rows = (await db.execute(text("SELECT * FROM content_pieces"))).mappings().all()
    return [dict(row) for row in rows]


async def read_leads(business_id: UUID) -> list[dict[str, Any]]:
    async with business_session(business_id) as db:
        rows = (await db.execute(text("SELECT * FROM leads"))).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Why the resolver exists
# --------------------------------------------------------------------------- #


async def test_the_restricted_role_reads_nothing_from_content_pieces_unscoped(
    content_store: PostgresContentStore, business_a: UUID
) -> None:
    """The fact the whole design rests on. ``content_pieces`` has FORCE RLS, so with no
    tenant GUC the application role reads zero rows and RAISES NOTHING -- a public page
    route that "just queried it" would 404 every page while the rows sat in the table."""
    piece = await content_store.create_landing_page(
        business_a, title="X", slug="x", body_md="# x", spec=a_spec().model_dump(mode="json")
    )

    async with session_module.session() as db:
        rows = (
            await db.execute(text("SELECT id FROM content_pieces WHERE id = :id"), {"id": piece.id})
        ).all()

    assert rows == [], "if this ever returns a row, RLS is off and every test here is vacuous"
    assert len(await read_pieces(business_a)) == 1, "the row does exist for its own business"


async def test_the_resolver_returns_the_page_and_its_business_with_no_tenant_scope(
    content_store: PostgresContentStore, business_a: UUID
) -> None:
    piece = await content_store.create_landing_page(
        business_a,
        title="Notdienst-Checkliste",
        slug="notdienst-checkliste",
        body_md="# Notdienst",
        spec=a_spec().model_dump(mode="json"),
        status="approved",
    )

    target = await content_store.resolve_landing_page(piece.id)

    assert target is not None
    assert target.business_id == business_a
    assert target.status == "approved"
    assert target.surface == LANDING_SURFACE
    assert target.spec["headline"].startswith("Notdienst-Checkliste")
    assert target.business_name.startswith("business-")
    assert target.locale == "de", "the page renders in the business's own language"


async def test_the_resolver_returns_a_draft_rather_than_hiding_it(
    content_store: PostgresContentStore, business_a: UUID
) -> None:
    """It deliberately does not filter on status: the route needs to tell "no such page"
    from "not published yet" in order to answer both identically."""
    piece = await content_store.create_landing_page(
        business_a, title="X", slug="x", body_md="# x", spec=a_spec().model_dump(mode="json")
    )

    target = await content_store.resolve_landing_page(piece.id)

    assert target is not None
    assert target.status == "draft"


async def test_the_resolver_returns_none_for_an_unknown_id(
    content_store: PostgresContentStore, business_a: UUID
) -> None:
    assert await content_store.resolve_landing_page(uuid4()) is None


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_a_page_is_invisible_to_another_business_and_visible_to_its_own(
    content_store: PostgresContentStore, business_a: UUID, business_b: UUID
) -> None:
    await content_store.create_landing_page(
        business_a, title="A", slug="a", body_md="# a", spec=a_spec().model_dump(mode="json")
    )

    assert [row["title"] for row in await read_pieces(business_a)] == ["A"]
    assert await read_pieces(business_b) == []


async def test_set_status_refuses_another_businesss_page(
    content_store: PostgresContentStore, business_a: UUID, business_b: UUID
) -> None:
    """An RLS-blocked update is zero rows rather than an error, so without the
    visibility check this would report success having approved nothing -- or, read the
    other way, would be the call that publishes another tenant's draft."""
    piece = await content_store.create_landing_page(
        business_a, title="A", slug="a", body_md="# a", spec=a_spec().model_dump(mode="json")
    )

    with pytest.raises(UnknownContentPieceError):
        await content_store.set_status(business_b, piece.id, "published")

    assert (await read_pieces(business_a))[0]["status"] == "draft"


async def test_retarget_link_refuses_another_businesss_link(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
    business_b: UUID,
) -> None:
    piece = await content_store.create_landing_page(
        business_a, title="A", slug="a", body_md="# a", spec=a_spec().model_dump(mode="json")
    )
    link = await link_store.create_link(
        business_a, target_url=f"{BASE}/p/{piece.id}", content_piece_id=piece.id
    )

    with pytest.raises(UnknownShortLinkError):
        await link_store.retarget_link(business_b, link.id, target_url="https://evil.example")

    resolved = await link_store.resolve(link.code)
    assert resolved is not None
    assert "evil.example" not in resolved.target_url


async def test_retarget_link_refuses_a_target_that_is_not_a_web_address(
    content_store: PostgresContentStore, link_store: PostgresLeadStore, business_a: UUID
) -> None:
    """The target becomes the ``Location`` of a public 302, so ``javascript:`` there is
    stored XSS served from our own domain. Validated on BOTH write paths."""
    piece = await content_store.create_landing_page(
        business_a, title="A", slug="a", body_md="# a", spec=a_spec().model_dump(mode="json")
    )
    link = await link_store.create_link(
        business_a, target_url=f"{BASE}/p/{piece.id}", content_piece_id=piece.id
    )

    for bad in ("javascript:alert(1)", "//evil.example/x", "https://ok.example\nX-Injected: 1"):
        with pytest.raises(ValueError, match="url must"):
            await link_store.retarget_link(business_a, link.id, target_url=bad)

    resolved = await link_store.resolve(link.code)
    assert resolved is not None
    assert resolved.target_url == f"{BASE}/p/{piece.id}"


# --------------------------------------------------------------------------- #
# The whole chain
# --------------------------------------------------------------------------- #


def _isolated_limiter() -> FixedWindowRateLimiter:
    """The shipped limiter would share a Redis window with the rest of the suite.

    The rate limit itself is tested in ``test_leads_api``; here it must simply not be
    the thing that fails.
    """
    return FixedWindowRateLimiter(
        rules={DIMENSION_IP: RateLimitRule(limit=50, window_seconds=60)},
        counter=InMemoryWindowCounter(),
        namespace=f"sma:test:{uuid4()}",
        secret="test-secret",
    )


async def test_the_conversion_loop_closes_over_real_sql(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """Publish, approve, serve with no JavaScript, submit as a browser, be a lead.

    The stores are REAL and row-level security is on. Only the rate limiter is
    substituted, and only so that this test does not share a counter window with the
    rest of the suite.
    """
    published = await publish_landing_page(
        business_id=business_a,
        spec=a_spec(),
        content_store=content_store,
        link_store=link_store,
        base_url=BASE,
    )
    # The owner approves it. Until then both public routes refuse it, which is the
    # approval gate rather than a detail of this test.
    await content_store.set_status(business_a, published.content_piece_id, "approved")

    app = create_app()
    app.dependency_overrides[leads_api.get_form_limiter] = _isolated_limiter
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # ATTRIBUTION: the CTA link is what a visitor actually clicks.
        cta = published.ctas[0]
        redirect = await client.get(
            cta.path, headers={"user-agent": CHROME}, follow_redirects=False
        )
        assert redirect.status_code == 302
        location = redirect.headers["location"]
        assert location.startswith(f"{BASE}{published.path}")
        assert f"ref={cta.code}" in location, "the page must be told which link brought them"
        assert "utm_source=linkedin" in location

        # CONVERSION: the page the click lands on, served with no JavaScript.
        page = await client.get(
            f"{published.path}?ref={cta.code}&utm_source=linkedin&utm_campaign=x"
        )
        assert page.status_code == 200
        assert "<script" not in page.text.lower()
        assert "set-cookie" not in page.headers
        assert f'action="/public/forms/{published.content_piece_id}"' in page.text

        # The submission a browser makes from that form.
        submitted = await client.post(
            f"/public/forms/{published.content_piece_id}",
            data={
                "name": "Petra Klein",
                "email": "petra@example.test",
                "consent": "on",
                "ref": cta.code,
                "utm_source": "linkedin",
                "utm_campaign": "x",
                "homepage2": "",
            },
            follow_redirects=False,
        )
        assert submitted.status_code == 303
        assert submitted.headers["location"] == f"{published.path}?sent=1"

        confirmation = await client.get(f"{published.path}?sent=1")
        assert "<form" not in confirmation.text

    leads = await read_leads(business_a)
    assert len(leads) == 1, "the lead has to be in the database, not only in a response"
    lead = leads[0]
    assert lead["content_piece_id"] == published.content_piece_id
    assert lead["short_link_id"] is not None, (
        "the lead must name the exact link that produced it, not only the channel"
    )
    assert lead["fields"]["email"] == "petra@example.test"
    assert lead["fields"]["consent"] is True
    assert lead["utm"]["utm_source"] == "linkedin"

    clicked = await link_store.resolve(published.ctas[0].code)
    assert clicked is not None
    assert clicked.click_count == 1, "a human click counts once"


async def test_an_unapproved_page_is_not_served_and_takes_no_leads(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """The approval gate, at both public surfaces, over real SQL. A generated page is a
    draft, and a draft's copy has not been read by the person whose name is on it."""
    published = await publish_landing_page(
        business_id=business_a,
        spec=a_spec(),
        content_store=content_store,
        link_store=link_store,
        base_url=BASE,
    )

    app = create_app()
    app.dependency_overrides[leads_api.get_form_limiter] = _isolated_limiter
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        page = await client.get(published.path)
        submitted = await client.post(
            f"/public/forms/{published.content_piece_id}",
            data={"email": "petra@example.test", "consent": "on"},
            follow_redirects=False,
        )

    assert page.status_code == 404
    assert submitted.status_code == 404
    assert await read_leads(business_a) == []


async def test_a_drafts_ctas_are_minted_but_not_advertised_on_the_public_hub(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """The links exist from the moment the page is generated -- approving the page must
    not have to touch them -- but the bio-link hub filters on the piece's status, so a
    draft is never advertised in somebody's Instagram bio."""
    published = await publish_landing_page(
        business_id=business_a,
        spec=a_spec(),
        content_store=content_store,
        link_store=link_store,
        base_url=BASE,
    )

    assert await link_store.list_hub_ctas(business_a) == []

    await content_store.set_status(business_a, published.content_piece_id, "approved")
    hub = await link_store.list_hub_ctas(business_a)

    assert [cta.code for cta in hub] == [published.ctas[0].code]
    assert hub[0].label.startswith("Notdienst-Checkliste")
