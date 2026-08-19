"""The lead store, against a real Postgres with row-level security switched on.

Five things are being proved here, and only the first is ordinary.

1. **Round trip.** A link is created, resolved by code, clicked, and a lead lands
   attributed to the content piece that earned it.

2. **The unscoped resolve, and why it has to exist.** ``/l/{code}`` is public: the
   visitor has no session and no business context, so the lookup cannot run inside
   ``business_session``. The first test in that section proves the thing that makes
   the design necessary rather than convenient -- the restricted application role
   reads **zero rows** from ``short_links`` with no tenant GUC set, silently. So
   there is no "just query it" option, and ``resolve`` is the one call in this
   module that runs on a privileged connection.

3. **Tenancy, asserted positively.** An unscoped query returns zero rows silently,
   so a test that only asserted "business B sees nothing" would pass against a
   store that is simply broken. Every isolation test below also reads a known row
   back as the owning business.

4. **No user agent and no IP reach the database.** Asserted twice: no value in the
   row contains the string, and no column exists that could hold one.

5. **Cross-tenant attribution is refused.** A foreign key does not respect RLS, so
   nothing in the schema stops business A's lead from pointing at business B's
   short link. The store checks it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.db.adapters import lead_store as lead_store_module
from backend.app.db.adapters.lead_store import (
    CodeExhaustionError,
    PostgresLeadStore,
    UnknownContentPieceError,
    UnknownShortLinkError,
)
from backend.app.db.session import business_session

pytestmark = [pytest.mark.db]

BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
VISITOR_IP = "203.0.113.42"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the session factory at this test's engine.

    ONE factory, and that is the point of migration ``7c1e4a90b2d5``. This fixture
    used to patch two, because the store had two connection paths: writes on the
    restricted role under ``business_session``, and the cross-tenant ``resolve`` on
    a privileged one. The privileged path is gone -- both unscoped lookups now go
    through SECURITY DEFINER functions on this same restricted role -- so a second
    factory would be a fiction, and worse, patching one would have let a test pass
    on privilege the deployment does not have.

    Patching the factory rather than injecting sessions keeps the real RLS scoping
    under test instead of replacing it with a hand-rolled copy that could differ.

    Function-scoped, because an asyncpg pool belongs to the event loop that created
    it -- see this package's conftest.
    """
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


@pytest.fixture
def store(scoped_sessions: None) -> PostgresLeadStore:
    return PostgresLeadStore()


@pytest.fixture
async def business_a(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[0]


@pytest.fixture
async def business_b(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[1]


async def make_content_piece(
    business_id: UUID, *, title: str = "Notdienst Koblenz", status: str = "published"
) -> UUID:
    """Insert a ``content_pieces`` row as the business itself, and return its id."""
    piece_id = uuid4()
    async with business_session(business_id) as db:
        await db.execute(
            text(
                "INSERT INTO content_pieces "
                "(id, business_id, surface, title, slug, body_md, status) "
                "VALUES (:id, :b, 'landing_page', :t, 'notdienst-koblenz', '# x', :s)"
            ),
            {"id": piece_id, "b": business_id, "t": title, "s": status},
        )
    return piece_id


async def read_clicks(business_id: UUID) -> list[dict[str, Any]]:
    async with business_session(business_id) as db:
        result = await db.execute(text("SELECT * FROM link_clicks"))
        return [dict(row) for row in result.mappings().all()]


# --------------------------------------------------------------------------- #
# create_link
# --------------------------------------------------------------------------- #


async def test_create_link_stores_a_resolvable_code(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    link = await store.create_link(business_a, target_url="https://mueller.example/notdienst")

    found = await store.resolve(link.code)
    assert found is not None
    assert found.id == link.id
    assert found.business_id == business_a
    assert found.target_url.startswith("https://mueller.example/notdienst")
    assert found.click_count == 0


async def test_create_link_tags_the_target_with_the_channel_utm(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """The stored target is the FINAL, tagged URL.

    The 302 sends the visitor straight to ``target_url``, so if the tags were only
    built and not applied, the destination's own analytics would see an untagged
    referral and the channel comparison would exist only inside our database.
    """
    link = await store.create_link(
        business_a,
        target_url="https://mueller.example/lp",
        channel="instagram",
        campaign="Sommer Aktion",
    )

    assert "utm_source=instagram" in link.target_url
    assert "utm_medium=social_organic" in link.target_url
    assert "utm_campaign=sommer-aktion" in link.target_url


async def test_create_link_leaves_the_target_alone_without_a_campaign(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """A channel with no campaign cannot be tagged honestly, so it is not tagged.

    Inventing a campaign name would put real clicks under a label nobody chose.
    """
    link = await store.create_link(
        business_a, target_url="https://mueller.example/lp", channel="instagram"
    )

    assert link.target_url == "https://mueller.example/lp"
    assert link.channel == "instagram"


async def test_create_link_refuses_a_target_that_is_not_a_web_address(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """``target_url`` becomes a ``Location`` header on a public redirect."""
    with pytest.raises(ValueError, match="url"):
        await store.create_link(business_a, target_url="javascript:alert(1)")


async def test_create_link_retries_when_a_code_is_already_taken(
    store: PostgresLeadStore, business_a: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unique index is the guarantee; the generator is only probabilistic.

    Forced here by handing out one code twice, which is the collision that would
    otherwise surface to a user as a 500 on "create my CTA".
    """
    codes = iter(["dupdupdu", "dupdupdu", "freshone"])
    monkeypatch.setattr(lead_store_module, "new_code", lambda **_: next(codes))

    first = await store.create_link(business_a, target_url="https://x.example/1")
    second = await store.create_link(business_a, target_url="https://x.example/2")

    assert first.code == "dupdupdu"
    assert second.code == "freshone"


async def test_create_link_gives_up_loudly_when_every_attempt_collides(
    store: PostgresLeadStore, business_a: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanently colliding generator is a bug, not a busy keyspace.

    Retrying forever would hang the request; a typed error names the cause.
    """
    monkeypatch.setattr(lead_store_module, "new_code", lambda **_: "stuckcod")
    await store.create_link(business_a, target_url="https://x.example/1")

    with pytest.raises(CodeExhaustionError):
        await store.create_link(business_a, target_url="https://x.example/2")


# --------------------------------------------------------------------------- #
# resolve -- cross-tenant by necessity
# --------------------------------------------------------------------------- #


async def test_the_restricted_role_reads_nothing_from_short_links_unscoped(
    store: PostgresLeadStore, business_a: UUID, app_engine: AsyncEngine
) -> None:
    """The premise of the whole design, proved rather than asserted in a comment.

    ``short_links`` has FORCE row-level security and a policy keyed on a
    transaction-local GUC. With no GUC set the restricted application role sees
    ZERO rows and no error -- so a public ``/l/{code}`` cannot be served by "just
    querying without the tenant scope". That is why ``resolve`` goes through the
    ``resolve_short_link`` SECURITY DEFINER function (migration ``7c1e4a90b2d5``)
    rather than a bare SELECT.

    The two assertions are the whole design in two lines: RLS still blinds this
    role completely, AND the narrow function still answers. Read together with
    ``test_the_app_role_has_no_rls_bypass`` below, which is what stops the second
    assertion from passing for the wrong reason.
    """
    link = await store.create_link(business_a, target_url="https://mueller.example/lp")

    async with app_engine.connect() as conn:
        blind = await conn.execute(
            text("SELECT count(*) FROM short_links WHERE code = :c"), {"c": link.code}
        )

    assert blind.scalar() == 0
    assert await store.resolve(link.code) is not None


async def test_resolve_returns_the_owning_business_without_being_told_it(
    store: PostgresLeadStore, business_b: UUID
) -> None:
    """The resolve answers "whose link is this?", which is what makes the rest scoped.

    Every write that follows a click runs under the business id this returns, so a
    wrong answer here would be a cross-tenant write.
    """
    link = await store.create_link(business_b, target_url="https://b.example/lp")

    found = await store.resolve(link.code)

    assert found is not None
    assert found.business_id == business_b


async def test_resolve_returns_none_for_an_unknown_code(store: PostgresLeadStore) -> None:
    assert await store.resolve("zzzzzzzz") is None


async def test_resolve_returns_none_for_a_code_shaped_like_an_injection(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """The parameter is bound, so a quote is a character and not syntax.

    Worth a test because this is the one query in the codebase that runs unscoped
    on a privileged connection, and the input comes straight off a public URL.
    """
    await store.create_link(business_a, target_url="https://mueller.example/lp")

    assert await store.resolve("' OR 1=1 --") is None


async def test_the_app_role_has_no_rls_bypass(app_engine: AsyncEngine) -> None:
    """The load-bearing precondition for every RLS test in this file.

    This REPLACES an earlier test that asserted the *privileged* role could see
    past RLS. That test was correct about the old design and is now meaningless:
    the privileged connection is gone (migration ``7c1e4a90b2d5``), so there is no
    longer a role whose bypass we depend on.

    What matters instead is the opposite property, and it is strictly stronger.
    Every "RLS blocks this" assertion in this file would pass vacuously if the
    application role were a superuser or carried ``BYPASSRLS`` -- and so would
    ``resolve`` succeeding, for entirely the wrong reason. Asserting the absence of
    a bypass is what makes the rest of the file mean anything.
    """
    async with app_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
        is_super, can_bypass = row.one()

    assert is_super is False, "the app role must not be a superuser, or RLS is decorative"
    assert can_bypass is False, "the app role must not carry BYPASSRLS"


async def test_the_resolver_function_is_not_executable_by_public(
    app_engine: AsyncEngine,
) -> None:
    """A SECURITY DEFINER function is executable by PUBLIC unless revoked.

    So the ``REVOKE`` in the migration is the control, and the ``GRANT`` after it
    is what keeps the application working. If a future migration recreated the
    function without the revoke, it would silently become callable by every role
    on the database -- an unscoped read of every tenant's links, available to
    anyone who can connect.
    """
    async with app_engine.connect() as conn:
        for signature in ("resolve_short_link(varchar)", "resolve_form_target(uuid)"):
            granted = await conn.execute(
                text(f"SELECT has_function_privilege('public', '{signature}', 'EXECUTE')")
            )
            assert granted.scalar() is False, f"{signature} is callable by PUBLIC"


# --------------------------------------------------------------------------- #
# record_click -- and what is deliberately not stored
# --------------------------------------------------------------------------- #


async def test_record_click_stores_the_click_and_counts_it(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    link = await store.create_link(business_a, target_url="https://mueller.example/lp")

    await store.record_click(link.id, business_a, referrer_host="l.instagram.com", is_bot=False)

    clicks = await read_clicks(business_a)
    assert len(clicks) == 1
    assert clicks[0]["short_link_id"] == link.id
    assert clicks[0]["referrer_host"] == "l.instagram.com"
    assert clicks[0]["is_bot"] is False

    refreshed = await store.resolve(link.code)
    assert refreshed is not None
    assert refreshed.click_count == 1


async def test_a_bot_click_is_recorded_but_not_counted(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """Both halves matter.

    The row is kept, because "how much of this is robots?" is a real question about
    a bio link. The counter is not incremented, because ``click_count`` is what the
    owner reads as "people who clicked", and a link previewer is not a person.
    """
    link = await store.create_link(business_a, target_url="https://mueller.example/lp")

    await store.record_click(link.id, business_a, referrer_host=None, is_bot=True)

    clicks = await read_clicks(business_a)
    assert len(clicks) == 1
    assert clicks[0]["is_bot"] is True

    refreshed = await store.resolve(link.code)
    assert refreshed is not None
    assert refreshed.click_count == 0


async def test_no_user_agent_and_no_ip_reach_the_database(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """Asserted on the stored values AND on the shape of the table.

    A hashed user agent is still a weak fingerprint and an IP is personal data
    under GDPR; neither is needed to attribute a lead to a piece of content. The
    UA is read by ``is_bot`` in memory and dropped, so what lands is one boolean.

    The second half -- that no column could hold one -- is what stops this becoming
    true again later by accident.
    """
    link = await store.create_link(business_a, target_url="https://mueller.example/lp")
    await store.record_click(link.id, business_a, referrer_host="t.co", is_bot=True)

    clicks = await read_clicks(business_a)
    flattened = " ".join(str(value) for value in clicks[0].values())
    assert BOT_UA not in flattened
    assert "facebookexternalhit" not in flattened
    assert VISITOR_IP not in flattened

    async with business_session(business_a) as db:
        result = await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('link_clicks', 'leads')"
            )
        )
        columns = {str(row[0]) for row in result.all()}

    forbidden = ("ip", "addr", "agent", "ua_", "_ua", "fingerprint")
    assert not [c for c in columns if any(hint in c for hint in forbidden)]


async def test_a_click_cannot_be_recorded_against_another_business(
    store: PostgresLeadStore, business_a: UUID, business_b: UUID
) -> None:
    """Belt and braces on top of RLS.

    The click write is scoped to the business the resolve returned, so a mismatch
    can only come from a bug -- and RLS turns it into zero rows rather than an
    error, which is exactly the failure that hides.
    """
    link = await store.create_link(business_a, target_url="https://mueller.example/lp")

    with pytest.raises(UnknownShortLinkError):
        await store.record_click(link.id, business_b, referrer_host=None, is_bot=False)

    assert await read_clicks(business_b) == []
    assert await read_clicks(business_a) == []


# --------------------------------------------------------------------------- #
# create_lead
# --------------------------------------------------------------------------- #


async def test_create_lead_is_attributed_to_the_content_piece_that_earned_it(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """The point of the entire loop: a lead names the content that produced it."""
    piece_id = await make_content_piece(business_a)
    link = await store.create_link(
        business_a,
        target_url="https://mueller.example/lp",
        content_piece_id=piece_id,
        channel="instagram",
        campaign="notdienst",
    )

    lead = await store.create_lead(
        business_a,
        fields={"name": "Petra Klein", "email": "petra@example.test"},
        utm={"utm_source": "instagram", "utm_campaign": "notdienst"},
        short_link_id=link.id,
        content_piece_id=piece_id,
    )

    assert lead.content_piece_id == piece_id
    assert lead.short_link_id == link.id
    assert lead.utm["utm_source"] == "instagram"
    assert lead.fields["email"] == "petra@example.test"
    assert lead.status == "new"
    assert lead.source == "form"


async def test_create_lead_refuses_a_short_link_belonging_to_another_business(
    store: PostgresLeadStore, business_a: UUID, business_b: UUID
) -> None:
    """A foreign key does not respect row-level security.

    Nothing in the schema stops business A's lead from referencing business B's
    short link, so the check has to be here -- and it is done by reading the link
    under A's own scope, which means RLS enforces the boundary rather than a WHERE
    clause somebody could forget.
    """
    foreign = await store.create_link(business_b, target_url="https://b.example/lp")

    with pytest.raises(UnknownShortLinkError):
        await store.create_lead(business_a, fields={"name": "x"}, utm={}, short_link_id=foreign.id)


async def test_create_lead_refuses_a_content_piece_belonging_to_another_business(
    store: PostgresLeadStore, business_a: UUID, business_b: UUID
) -> None:
    foreign_piece = await make_content_piece(business_b)

    with pytest.raises(UnknownContentPieceError):
        await store.create_lead(
            business_a, fields={"name": "x"}, utm={}, content_piece_id=foreign_piece
        )


# --------------------------------------------------------------------------- #
# list_leads -- tenancy, asserted in both directions
# --------------------------------------------------------------------------- #


async def test_each_business_sees_only_its_own_leads(
    store: PostgresLeadStore, business_a: UUID, business_b: UUID
) -> None:
    """Both directions in one test, on purpose.

    "B sees nothing" alone would pass against a store whose query is simply
    broken, because an unscoped read returns zero rows silently. So B's own row is
    read back positively in the same breath.
    """
    a_lead = await store.create_lead(business_a, fields={"name": "A caller"}, utm={})
    b_lead = await store.create_lead(business_b, fields={"name": "B caller"}, utm={})

    a_visible = await store.list_leads(business_a)
    b_visible = await store.list_leads(business_b)

    assert [lead.id for lead in a_visible] == [a_lead.id]
    assert [lead.id for lead in b_visible] == [b_lead.id]
    assert a_lead.id not in {lead.id for lead in b_visible}
    assert b_visible[0].fields["name"] == "B caller"


async def test_list_leads_returns_the_newest_first_and_honours_the_limit(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    for index in range(3):
        await store.create_lead(business_a, fields={"name": f"caller-{index}"}, utm={})

    newest = await store.list_leads(business_a, limit=2)

    assert len(newest) == 2
    assert newest[0].fields["name"] == "caller-2"


async def test_list_leads_refuses_a_nonsense_limit(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    with pytest.raises(ValueError, match="limit"):
        await store.list_leads(business_a, limit=0)


# --------------------------------------------------------------------------- #
# The public form's target, and the link hub
# --------------------------------------------------------------------------- #


async def test_resolve_form_names_the_business_and_the_status(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """``POST /public/forms/{id}`` is unauthenticated, so the id has to carry the tenant.

    Same necessity as ``resolve``, same single privileged single-row read: without
    it there is no way to know which business a stranger's submission belongs to.
    The status comes back rather than being judged here, because "a draft page
    must not take leads" is the route's rule, not the store's.
    """
    piece_id = await make_content_piece(business_a, status="draft")

    form = await store.resolve_form(piece_id)

    assert form is not None
    assert form.business_id == business_a
    assert form.content_piece_id == piece_id
    assert form.status == "draft"


async def test_resolve_form_returns_none_for_an_unknown_id(store: PostgresLeadStore) -> None:
    assert await store.resolve_form(uuid4()) is None


async def test_business_for_owner_finds_the_signed_in_users_business(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    async with business_session(business_a) as db:
        result = await db.execute(
            text("SELECT owner_id FROM businesses WHERE id = :b"), {"b": business_a}
        )
        owner_id = result.scalar_one()

    assert await store.business_for_owner(UUID(str(owner_id))) == business_a


async def test_business_for_owner_returns_none_for_a_user_without_one(
    store: PostgresLeadStore,
) -> None:
    assert await store.business_for_owner(uuid4()) is None


async def test_business_name_is_readable_for_the_public_hub(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """The hub is public and needs the name to render a heading.

    ``businesses`` carries no RLS policy -- it is the tenant table -- so this one
    read is legitimately unscoped rather than privileged, unlike ``resolve``.
    """
    assert await store.business_name(business_a) == "business-a"
    assert await store.business_name(uuid4()) is None


async def test_hub_lists_published_ctas_and_hides_drafts(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """The bio link is a public page, so an unapproved landing page must not appear.

    Instagram feed and TikTok have no clickable link at all, so this page is their
    entire conversion path -- which also makes it the one page most likely to be
    seen before anyone meant it to be.
    """
    live = await make_content_piece(business_a, title="Notdienst", status="published")
    draft = await make_content_piece(business_a, title="Badsanierung", status="draft")

    live_link = await store.create_link(
        business_a, target_url="https://x.example/1", content_piece_id=live, channel="link_hub"
    )
    await store.create_link(
        business_a, target_url="https://x.example/2", content_piece_id=draft, channel="link_hub"
    )
    standing = await store.create_link(
        business_a, target_url="https://x.example/anrufen", campaign="Jetzt anrufen"
    )

    ctas = await store.list_hub_ctas(business_a)

    codes = [cta.code for cta in ctas]
    assert live_link.code in codes
    assert standing.code in codes
    assert len(codes) == 2
    assert next(cta.label for cta in ctas if cta.code == live_link.code) == "Notdienst"


async def test_the_hub_never_shows_another_business_ctas(
    store: PostgresLeadStore, business_a: UUID, business_b: UUID
) -> None:
    a_link = await store.create_link(business_a, target_url="https://a.example/x", campaign="a-cta")
    b_link = await store.create_link(business_b, target_url="https://b.example/x", campaign="b-cta")

    a_codes = {cta.code for cta in await store.list_hub_ctas(business_a)}
    b_codes = {cta.code for cta in await store.list_hub_ctas(business_b)}

    assert a_codes == {a_link.code}
    assert b_codes == {b_link.code}


# --------------------------------------------------------------------------- #
# The hub address -- slug and UUID, both forever
# --------------------------------------------------------------------------- #


async def test_the_hub_resolves_a_business_by_its_slug(
    store: PostgresLeadStore, business_a: UUID, app_engine: AsyncEngine
) -> None:
    """The readable address, which is the reason the column exists.

    The slug is made unique per run rather than hard-coded: ``slug`` is UNIQUE across
    the platform and this database is not truncated between runs, so a fixed literal
    fails on the second invocation -- against a row left by the first.
    """
    slug = f"mueller-sanitaer-{uuid4().hex[:8]}"
    async with app_engine.begin() as conn:
        await conn.execute(
            text("UPDATE businesses SET slug = :s WHERE id = :i"),
            {"s": slug, "i": business_a},
        )

    found = await store.business_by_handle(slug)

    assert found is not None
    assert found.id == business_a
    assert found.slug == slug


async def test_the_hub_still_resolves_the_old_uuid_address(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """The backward-compatibility guarantee, and it is not a nicety.

    ``/go/{uuid}`` is what the hub took before the slug column existed, so that
    string may already be printed on a flyer or pasted into an Instagram bio. For
    Instagram and TikTok -- which have no clickable link of their own -- this hub is
    the ENTIRE conversion path, so breaking the old form would silently kill live
    campaigns with no error anywhere.
    """
    found = await store.business_by_handle(str(business_a))

    assert found is not None
    assert found.id == business_a
    # The canonical readable address comes back too, so a caller can redirect
    # rather than serve one page at two URLs forever.
    assert found.slug


async def test_an_unknown_handle_resolves_to_none(store: PostgresLeadStore) -> None:
    """Both shapes of miss: a slug nobody has, and a well-formed but unused UUID."""
    assert await store.business_by_handle("no-such-business") is None
    assert await store.business_by_handle(str(uuid4())) is None


async def test_a_handle_that_is_not_a_uuid_does_not_error(store: PostgresLeadStore) -> None:
    """``'not-a-uuid'::uuid`` RAISES in Postgres rather than matching nothing.

    So the UUID branch has to be guarded in Python before the statement runs. Without
    that, every slug lookup -- the normal case -- would 500 instead of resolving.
    """
    assert await store.business_by_handle("definitely-not-a-uuid") is None


async def test_a_handle_shaped_like_an_injection_resolves_to_none(
    store: PostgresLeadStore, business_a: UUID
) -> None:
    """The handle is a URL segment, so it is bound, never interpolated."""
    assert await store.business_by_handle("' OR 1=1 --") is None
