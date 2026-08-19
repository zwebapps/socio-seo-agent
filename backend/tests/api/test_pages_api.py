"""``GET /p/{piece_id}`` — the public landing page a tracked link points at.

Hermetic: the store is replaced through a dependency override, so there is no
database and no network.

What is being pinned down is not the rendering, which the engine's own tests cover.
It is the five rules this route has to obey:

* **it works with no JavaScript and sets no cookie.** Both are properties of the
  response, so both are asserted on the response;
* **every refusal is the same 404** — unknown id, malformed id, a draft, a content
  piece that is not a landing page, and a spec we cannot read. A 403 on the draft
  would confirm that unpublished work exists at that id;
* **nothing from the request is reflected.** Not the id in the 404, not a ``ref`` that
  could not be one of our codes, not a query parameter that is not a UTM tag;
* **the form posts to the endpoint that can actually receive it**, keyed on this page's
  own content piece -- which is what makes the resulting lead attributable;
* **the confirmation is a state of the same page**, and it is never cached.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from backend.app.api import pages as pages_api
from backend.app.db.adapters.content_store import LANDING_SURFACE, LandingPageTarget
from backend.app.main import create_app

BUSINESS_ID = UUID("11111111-1111-4111-8111-111111111111")
PIECE_ID = UUID("33333333-3333-4333-8333-333333333333")
OTHER_ID = UUID("44444444-4444-4444-8444-444444444444")

SPEC: dict[str, Any] = {
    "headline": "Notdienst-Checkliste für Hauseigentümer in Koblenz",
    "subhead": "Fünf Prüfungen, bevor Sie den Notdienst rufen.",
    "offer": "Eine zweiseitige Checkliste mit den fünf Prüfungen bei einem Wasserschaden.",
    "proof_points": [{"text": "Seit 1998 in Koblenz.", "source": "Leistungsübersicht 2026"}],
    "form_fields": [
        {"name": "name", "label": "Ihr Name", "required": False},
        {"name": "email", "label": "E-Mail-Adresse", "required": True},
    ],
    "primary_cta": "Checkliste anfordern",
    "consent_text": "Ich bin mit der Kontaktaufnahme einverstanden.",
    "ctas": [{"channel": "linkedin", "text": "Unsere Checkliste:"}],
}


class FakeStore:
    def __init__(
        self,
        *,
        target: LandingPageTarget | None = None,
    ) -> None:
        self._target = target
        self.asked: list[UUID] = []

    async def resolve_landing_page(self, piece_id: UUID) -> LandingPageTarget | None:
        self.asked.append(piece_id)
        if self._target is not None and self._target.content_piece_id == piece_id:
            return self._target
        return None


def _target(**over: Any) -> LandingPageTarget:
    base: dict[str, Any] = {
        "business_id": BUSINESS_ID,
        "content_piece_id": PIECE_ID,
        "status": "approved",
        "surface": LANDING_SURFACE,
        "title": "Notdienst-Checkliste",
        "slug": "notdienst-checkliste",
        "spec": SPEC,
        "business_name": "Müller Sanitär GmbH",
        "locale": "de",
    }
    base.update(over)
    return LandingPageTarget(**base)


@pytest.fixture
def client_for() -> Any:
    def build(store: FakeStore) -> httpx.AsyncClient:
        app = create_app()
        app.dependency_overrides[pages_api.get_store] = lambda: store
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    return build


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


async def test_the_page_is_served_as_html_with_no_cookie_and_no_script(
    client_for: Any,
) -> None:
    async with client_for(FakeStore(target=_target())) as client:
        response = await client.get(f"/p/{PIECE_ID}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "set-cookie" not in response.headers, (
        "a public campaign page must not need a consent banner of its own"
    )
    body = response.text
    assert "<script" not in body.lower()
    assert "Notdienst-Checkliste" in body


async def test_the_form_posts_to_the_endpoint_that_can_receive_it(client_for: Any) -> None:
    """Keyed on this page's own content piece, which is what makes the lead
    attributable without the form carrying a business id a visitor could change."""
    async with client_for(FakeStore(target=_target())) as client:
        body = (await client.get(f"/p/{PIECE_ID}")).text

    assert f'action="/public/forms/{PIECE_ID}"' in body
    assert '<form method="post"' in body


async def test_the_page_is_shared_cacheable_because_it_holds_nothing_private(
    client_for: Any,
) -> None:
    async with client_for(FakeStore(target=_target())) as client:
        response = await client.get(f"/p/{PIECE_ID}")

    assert response.headers["cache-control"] == "public, max-age=60"


async def test_a_published_page_is_served_as_well_as_an_approved_one(client_for: Any) -> None:
    async with client_for(FakeStore(target=_target(status="published"))) as client:
        assert (await client.get(f"/p/{PIECE_ID}")).status_code == 200


# --------------------------------------------------------------------------- #
# Every refusal is the same 404
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "target"),
    [
        (f"/p/{OTHER_ID}", _target()),
        ("/p/not-a-uuid", _target()),
        (f"/p/{PIECE_ID}", _target(status="draft")),
        (f"/p/{PIECE_ID}", _target(status="rejected")),
        (f"/p/{PIECE_ID}", _target(surface="article")),
        (f"/p/{PIECE_ID}", _target(spec={})),
        (f"/p/{PIECE_ID}", _target(spec={"headline": "x", "form_fields": []})),
    ],
)
async def test_every_reason_a_page_is_not_served_answers_identically(
    client_for: Any, path: str, target: LandingPageTarget
) -> None:
    """A 403 on the draft would confirm that unpublished work exists at that id, and
    the visitor can act on none of the distinctions."""
    async with client_for(FakeStore(target=target)) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "not available" in response.text.lower()
    assert response.headers["cache-control"].startswith("no-store")


async def test_the_refusal_does_not_echo_the_id_it_refused(client_for: Any) -> None:
    """Otherwise the endpoint is a mirror for anything an attacker wants on our
    domain."""
    async with client_for(FakeStore(target=_target())) as client:
        response = await client.get("/p/not-a-uuid-<script>alert(1)</script>")

    assert response.status_code == 404
    assert "script" not in response.text.lower()
    assert "not-a-uuid" not in response.text


async def test_a_malformed_stored_spec_is_a_404_and_not_a_stack_trace(client_for: Any) -> None:
    """Our bug, not the visitor's -- but a public marketing page is not where to
    report it. It is logged instead."""
    async with client_for(FakeStore(target=_target(spec={"headline": 7}))) as client:
        response = await client.get(f"/p/{PIECE_ID}")

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# What rides along, and what does not
# --------------------------------------------------------------------------- #


async def test_utm_parameters_are_carried_into_the_form(client_for: Any) -> None:
    """Otherwise the lead knows nothing about the campaign that produced it."""
    async with client_for(FakeStore(target=_target())) as client:
        body = (await client.get(f"/p/{PIECE_ID}?utm_source=linkedin&utm_campaign=notdienst")).text

    assert '<input type="hidden" name="utm_source" value="linkedin">' in body
    assert 'name="utm_campaign" value="notdienst"' in body


async def test_a_query_parameter_that_is_not_a_utm_tag_is_not_carried(client_for: Any) -> None:
    """The query string is caller-controlled: the form must not become a carrier for
    whatever somebody appended to a campaign link."""
    async with client_for(FakeStore(target=_target())) as client:
        body = (await client.get(f"/p/{PIECE_ID}?redirect=https://evil.example&x=1")).text

    assert "evil.example" not in body
    assert 'name="redirect"' not in body


async def test_a_reference_code_rides_along_and_a_malformed_one_is_dropped(
    client_for: Any,
) -> None:
    """`ref` is what lets the lead name the exact link that produced it. A value that
    could not be one of our codes is not reflected at all."""
    async with client_for(FakeStore(target=_target())) as client:
        good = (await client.get(f"/p/{PIECE_ID}?ref=Ab3xY7kp")).text
        bad = (await client.get(f'/p/{PIECE_ID}?ref="><script>alert(1)</script>')).text

    assert '<input type="hidden" name="ref" value="Ab3xY7kp">' in good
    assert 'name="ref"' not in bad
    assert "<script>" not in bad


# --------------------------------------------------------------------------- #
# The three states of one page
# --------------------------------------------------------------------------- #


async def test_the_confirmation_replaces_the_form_and_is_never_cached(client_for: Any) -> None:
    """No JavaScript means the form endpoint redirects back here with `?sent=1`, so a
    cached confirmation would tell a visitor they had submitted something they had
    not."""
    async with client_for(FakeStore(target=_target())) as client:
        response = await client.get(f"/p/{PIECE_ID}?sent=1")

    assert response.status_code == 200
    assert "<form" not in response.text
    assert 'role="status"' in response.text
    assert response.headers["cache-control"].startswith("no-store")


async def test_the_error_state_keeps_the_form_and_is_never_cached(client_for: Any) -> None:
    async with client_for(FakeStore(target=_target())) as client:
        response = await client.get(f"/p/{PIECE_ID}?error=1")

    assert "<form" in response.text
    assert 'role="alert"' in response.text
    assert response.headers["cache-control"].startswith("no-store")


async def test_the_state_is_decided_by_presence_not_by_value(client_for: Any) -> None:
    """`?sent=` with any value means sent. Comparing the value would show the form
    again to somebody who had just submitted it."""
    async with client_for(FakeStore(target=_target())) as client:
        assert "<form" not in (await client.get(f"/p/{PIECE_ID}?sent=yes")).text
        assert "<form" not in (await client.get(f"/p/{PIECE_ID}?sent")).text


async def test_the_page_renders_in_the_business_locale(client_for: Any) -> None:
    async with client_for(FakeStore(target=_target(locale="en"))) as client:
        body = (await client.get(f"/p/{PIECE_ID}?sent=1")).text

    assert '<html lang="en">' in body
    assert "Thank you" in body
