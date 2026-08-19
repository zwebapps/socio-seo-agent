"""``GET /l/{code}`` and ``GET /go/{slug}`` — the two public surfaces of the loop.

Written before the routes. Hermetic: the store is replaced with an in-memory fake
through a dependency override, so no database and no network are involved.

What is being pinned down here is not the redirect, which is trivial. It is the
four rules the redirect has to obey:

* **the visitor is never made to pay for our analytics.** A click write that fails
  still redirects. Losing one row of measurement is trivial; losing a lead is not.
* **no user agent and no IP reach the store.** The UA decides one boolean and is
  dropped, and there is no parameter on the write that could carry an address. The
  referrer is reduced to a host, because a referrer PATH can carry a search query
  or a token.
* **nothing is reflected.** An unknown code gets a 404 that does not echo it, so
  the endpoint is not a mirror for anything an attacker wants to put on our domain.
* **the hub is public, so it publishes nothing private.** In particular not click
  counts: how well a business's CTAs perform is the business's own data, and the
  bio-link page is readable by anyone who has the URL.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from backend.app.api import links as links_api
from backend.app.db.adapters.lead_store import BusinessHandle, HubCta, ShortLinkRecord
from backend.app.main import create_app

BUSINESS_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_BUSINESS_ID = UUID("22222222-2222-4222-8222-222222222222")
#: The readable hub address. A real slug shape, so a route that only ever accepted
#: UUIDs cannot pass these tests by accident.
BUSINESS_SLUG = "mueller-sanitaer-gmbh"
LINK_ID = UUID("33333333-3333-4333-8333-333333333333")

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PREVIEWER = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

TARGET = "https://mueller.example/lp?utm_source=instagram&utm_medium=social_organic"


class FakeStore:
    """Just enough store to serve a redirect and a hub, plus a record of the calls."""

    def __init__(
        self,
        *,
        link: ShortLinkRecord | None = None,
        ctas: list[HubCta] | None = None,
        name: str | None = "Müller Sanitär GmbH",
        click_raises: Exception | None = None,
    ) -> None:
        self._link = link
        self._ctas = ctas or []
        self._name = name
        self._click_raises = click_raises
        self.resolved: list[str] = []
        self.clicks: list[dict[str, Any]] = []

    async def resolve(self, code: str) -> ShortLinkRecord | None:
        self.resolved.append(code)
        if self._link is not None and self._link.code == code:
            return self._link
        return None

    async def record_click(
        self,
        link_id: UUID,
        business_id: UUID,
        *,
        referrer_host: str | None,
        is_bot: bool,
    ) -> None:
        self.clicks.append(
            {
                "link_id": link_id,
                "business_id": business_id,
                "referrer_host": referrer_host,
                "is_bot": is_bot,
            }
        )
        if self._click_raises is not None:
            raise self._click_raises

    async def list_hub_ctas(self, business_id: UUID) -> list[HubCta]:
        return list(self._ctas) if business_id == BUSINESS_ID else []

    async def business_name(self, business_id: UUID) -> str | None:
        return self._name if business_id == BUSINESS_ID else None

    async def business_by_handle(self, handle: str) -> BusinessHandle | None:
        """Resolve either address form, exactly as the real store does.

        Both are answered by the same fake so the route's own branch is what is under
        test, rather than the fake's opinion about which form is canonical.
        """
        if handle in {str(BUSINESS_ID), BUSINESS_SLUG} and self._name is not None:
            # A business with no name is how this fake spells "no such business", so
            # it must not resolve a handle either -- the real store returns None for
            # the same row.
            return BusinessHandle(id=BUSINESS_ID, name=self._name, slug=BUSINESS_SLUG)
        return None


def a_link(*, code: str = "abcd2345", target: str = TARGET) -> ShortLinkRecord:
    return ShortLinkRecord(
        id=LINK_ID,
        business_id=BUSINESS_ID,
        code=code,
        target_url=target,
        content_piece_id=None,
        channel="instagram",
        campaign="sommer",
        click_count=7,
    )


def _client(store: FakeStore) -> httpx.AsyncClient:
    app = create_app()
    app.include_router(links_api.router)
    app.dependency_overrides[links_api.get_store] = lambda: store
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# GET /l/{code}
# --------------------------------------------------------------------------- #


async def test_a_known_code_redirects_to_the_tagged_target() -> None:
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        response = await client.get("/l/abcd2345", headers={"user-agent": CHROME})

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


async def test_the_redirect_is_never_cached() -> None:
    """A cached 302 is a click that never reaches us.

    The whole product measures attribution through this hop, so an intermediary
    that answers it from cache silently deletes the measurement -- and on a bio
    link, which is fetched constantly, it would delete most of it.
    """
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        response = await client.get("/l/abcd2345", headers={"user-agent": CHROME})

    assert "no-store" in response.headers["cache-control"]


async def test_a_click_is_recorded_against_the_business_the_code_resolved_to() -> None:
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        await client.get(
            "/l/abcd2345",
            headers={"user-agent": CHROME, "referer": "https://l.instagram.com/"},
        )

    assert store.clicks == [
        {
            "link_id": LINK_ID,
            "business_id": BUSINESS_ID,
            "referrer_host": "l.instagram.com",
            "is_bot": False,
        }
    ]


async def test_only_the_referrer_host_is_kept() -> None:
    """A referrer PATH can carry a search query, a session id or a token.

    The host answers the only question we ask of it -- which channel sent this --
    and carries none of that.
    """
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        await client.get(
            "/l/abcd2345",
            headers={
                "user-agent": CHROME,
                "referer": "https://www.google.com/search?q=patient+name+kredit",
            },
        )

    assert store.clicks[0]["referrer_host"] == "www.google.com"


async def test_a_link_previewer_is_flagged_and_still_served() -> None:
    """Both halves. The flag keeps the click count honest; the redirect keeps
    working, because a previewer that gets a 404 makes the link look broken in
    every chat app that unfurls it."""
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        response = await client.get("/l/abcd2345", headers={"user-agent": PREVIEWER})

    assert response.status_code == 302
    assert store.clicks[0]["is_bot"] is True


async def test_no_user_agent_and_no_ip_are_passed_to_the_store() -> None:
    """Asserted on the call, which is where it can still be got wrong.

    The database cannot hold them (``tests/db/test_lead_store.py`` proves the
    columns do not exist), so the remaining risk is this route putting them in the
    referrer field or in a lead's ``fields`` blob. It passes four things and none of
    them is an identifier.
    """
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        await client.get(
            "/l/abcd2345",
            headers={"user-agent": PREVIEWER, "referer": "https://t.co/x", "x-real-ip": "1.2.3.4"},
        )

    recorded = store.clicks[0]
    assert set(recorded) == {"link_id", "business_id", "referrer_host", "is_bot"}
    flattened = " ".join(str(value) for value in recorded.values())
    assert "facebookexternalhit" not in flattened
    assert "1.2.3.4" not in flattened


async def test_a_failing_click_write_still_redirects_the_visitor() -> None:
    """The rule this endpoint exists to protect.

    Analytics is the reason for the hop, but it is not the reason the visitor is
    here. One lost row is nothing; a visitor who wanted the offer and got a 500 is
    the lead this whole loop was built to capture.
    """
    store = FakeStore(link=a_link(), click_raises=RuntimeError("postgres is down"))

    async with _client(store) as client:
        response = await client.get("/l/abcd2345", headers={"user-agent": CHROME})

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


async def test_an_unknown_code_is_a_404_that_does_not_echo_it() -> None:
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        response = await client.get("/l/zzzz2345")

    assert response.status_code == 404
    assert "zzzz2345" not in response.text


async def test_a_malformed_code_is_refused_without_touching_the_store() -> None:
    """Scanners try paths, not codes.

    The charset and length are known, so anything outside them is refused before it
    becomes a privileged database lookup -- which is the one query in the product
    that runs without a tenant scope.
    """
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        for path in ("/l/short", "/l/has-a-dash", "/l/' OR 1=1 --", "/l/" + "x" * 40):
            assert (await client.get(path)).status_code == 404

    assert store.resolved == []


async def test_a_code_using_the_excluded_characters_is_refused() -> None:
    """``0O1lI`` are not in the alphabet, so a code containing one is a typo."""
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        assert (await client.get("/l/abcd234O")).status_code == 404

    assert store.resolved == []


async def test_a_click_with_no_referrer_records_none_rather_than_a_placeholder() -> None:
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        await client.get("/l/abcd2345", headers={"user-agent": CHROME})

    assert store.clicks[0]["referrer_host"] is None


async def test_a_malformed_referrer_is_dropped_rather_than_stored_raw() -> None:
    store = FakeStore(link=a_link())

    async with _client(store) as client:
        await client.get(
            "/l/abcd2345", headers={"user-agent": CHROME, "referer": "not a url at all"}
        )

    assert store.clicks[0]["referrer_host"] is None


# --------------------------------------------------------------------------- #
# GET /go/{slug} -- the link hub
# --------------------------------------------------------------------------- #


def some_ctas() -> list[HubCta]:
    return [
        HubCta(
            code="abcd2345",
            label="Notdienst",
            channel="link_hub",
            campaign="notdienst",
            click_count=98765,
        ),
        HubCta(
            code="efgh6789",
            label="Jetzt anrufen",
            channel=None,
            campaign="anrufen",
            click_count=3,
        ),
    ]


async def test_the_hub_lists_every_active_cta_as_a_tracked_link() -> None:
    """The fix for the structural problem in docs/CHANNELS.md section 1.

    An Instagram feed caption and a TikTok caption carry no clickable link at all,
    so this page IS the conversion path for those two channels -- and every entry
    on it has to go through ``/l/{code}`` or the click is invisible again.
    """
    store = FakeStore(ctas=some_ctas())

    async with _client(store) as client:
        response = await client.get(f"/go/{BUSINESS_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["business"]["name"] == "Müller Sanitär GmbH"
    assert [cta["label"] for cta in body["ctas"]] == ["Notdienst", "Jetzt anrufen"]
    assert [cta["path"] for cta in body["ctas"]] == ["/l/abcd2345", "/l/efgh6789"]


async def test_the_hub_returns_relative_paths_rather_than_absolute_urls() -> None:
    """The ``Host`` header is caller-controlled.

    Building an absolute URL from it means a poisoned Host produces a hub whose
    every CTA points at somebody else's domain. The frontend knows its own origin;
    we do not need to guess it.
    """
    store = FakeStore(ctas=some_ctas())

    async with _client(store) as client:
        response = await client.get(f"/go/{BUSINESS_ID}", headers={"host": "evil.example"})

    for cta in response.json()["ctas"]:
        assert cta["path"].startswith("/l/")
        assert "evil.example" not in cta["path"]


async def test_the_hub_does_not_publish_click_counts() -> None:
    """How well a CTA performs is the business's own data.

    This page is public by design -- it is the bio link -- so anything on it is
    readable by a competitor who tries the URL.
    """
    store = FakeStore(ctas=some_ctas())

    async with _client(store) as client:
        response = await client.get(f"/go/{BUSINESS_ID}")

    assert "98765" not in response.text
    assert all("clickCount" not in cta for cta in response.json()["ctas"])


async def test_an_unknown_business_is_a_404() -> None:
    store = FakeStore(ctas=some_ctas())

    async with _client(store) as client:
        response = await client.get(f"/go/{OTHER_BUSINESS_ID}")

    assert response.status_code == 404


@pytest.mark.parametrize("handle", ["nobody-by-that-name", "../../etc/passwd", "0"])
async def test_an_unrecognised_hub_handle_is_a_404(handle: str) -> None:
    """REPLACES ``test_a_slug_that_is_not_an_identifier_is_a_404``.

    That test asserted every non-UUID handle was a 404, which was correct while
    ``businesses`` had no slug column -- and is now exactly the behaviour that must
    NOT hold: migration ``9a4f21c7de83`` added the column, and a readable slug is the
    address the hub hands out. Its original rationale ("guessing one from the business
    name would be ambiguous") was about deriving a slug at read time, which is not
    what a stored unique column does.

    The property worth keeping is the narrower one: an UNRECOGNISED handle is a 404,
    in whatever shape it arrives. Kept stronger than before by covering all three
    shapes of miss -- an unknown slug, a traversal attempt, and a bare digit -- and
    paired with the test below, which proves a KNOWN slug resolves. Either test alone
    is satisfiable by a route that is simply broken in one direction.
    """
    store = FakeStore(ctas=some_ctas())

    async with _client(store) as client:
        assert (await client.get(f"/go/{handle}")).status_code == 404


async def test_the_hub_resolves_a_readable_slug() -> None:
    """The reason the column exists: an address a person can say out loud."""
    store = FakeStore(ctas=some_ctas())

    async with _client(store) as client:
        response = await client.get(f"/go/{BUSINESS_SLUG}")

    assert response.status_code == 200
    assert response.json()["business"]["id"] == str(BUSINESS_ID)


async def test_the_hub_still_resolves_the_old_uuid_address() -> None:
    """The compatibility guarantee, and it is load-bearing rather than polite.

    ``/go/{uuid}`` may already be printed on a flyer or pasted into an Instagram bio,
    and for Instagram and TikTok -- which have no clickable link of their own -- this
    hub is the ENTIRE conversion path. Dropping the old form would kill live campaigns
    with no error anywhere.
    """
    store = FakeStore(ctas=some_ctas())

    async with _client(store) as client:
        response = await client.get(f"/go/{BUSINESS_ID}")

    assert response.status_code == 200
    assert response.json()["business"]["id"] == str(BUSINESS_ID)


async def test_a_business_with_no_ctas_still_renders() -> None:
    """An empty hub is a real state -- a business that has just signed up.

    A 404 here would make a freshly pasted bio link look broken.
    """
    store = FakeStore(ctas=[])

    async with _client(store) as client:
        response = await client.get(f"/go/{BUSINESS_ID}")

    assert response.status_code == 200
    assert response.json()["ctas"] == []
