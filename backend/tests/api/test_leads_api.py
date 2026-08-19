"""``POST /public/forms/{id}`` and ``GET /api/v1/leads``.

Written before the routes. Hermetic: the store and the rate limiter are replaced
through dependency overrides, so no database, no Redis and no network.

One of these endpoints is an **unauthenticated write**, which makes it the most
exposed surface in the product. The tests are therefore mostly about refusal, and
each refusal encodes a decision:

* **a filled honeypot answers exactly like a success.** A 400 would tell the bot
  which field to leave alone next time, and the whole value of a honeypot is that
  the sender cannot tell it fired.
* **the body is bounded before it is read**, by ``Content-Length``, and a request
  that declines to declare a length is refused rather than streamed.
* **the field schema is closed.** An unexpected key is refused instead of being
  swallowed into the JSONB blob, because "we store whatever arrives" on a public
  endpoint is how a lead row becomes a place to keep somebody else's payload.
* **nothing is reflected.** Not in the 202, not in the 422 -- so a submitted value
  never comes back out of this endpoint, whatever shape the request was.
* **the owner's list is scoped by the session, never by the request.** A
  ``businessId`` query parameter cannot change whose leads are returned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from backend.app.api import leads as leads_api
from backend.app.api.auth import current_user
from backend.app.core.rate_limit import (
    DIMENSION_IP,
    FixedWindowRateLimiter,
    InMemoryWindowCounter,
    RateLimitRule,
)
from backend.app.db.adapters.lead_store import FormTarget, LeadRecord, ShortLinkRecord
from backend.app.db.models import Role, User

BUSINESS_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_BUSINESS_ID = UUID("22222222-2222-4222-8222-222222222222")
FORM_ID = UUID("44444444-4444-4444-8444-444444444444")
LINK_ID = UUID("55555555-5555-4555-8555-555555555555")
OWNER_ID = UUID("66666666-6666-4666-8666-666666666666")

VALID_BODY: dict[str, Any] = {
    "name": "Petra Klein",
    "email": "petra@example.test",
    "phone": "0261 12345",
    "message": "Heizung tropft, bitte um Rückruf.",
    "consent": True,
    "utm": {"utm_source": "instagram", "utm_campaign": "notdienst"},
}


class FakeStore:
    def __init__(
        self,
        *,
        form: FormTarget | None = None,
        link: ShortLinkRecord | None = None,
        leads: list[LeadRecord] | None = None,
        business: UUID | None = BUSINESS_ID,
    ) -> None:
        self._form = form
        self._link = link
        self._leads = leads or []
        self._business = business
        self.created: list[dict[str, Any]] = []
        self.listed: list[tuple[UUID, int]] = []

    async def resolve_form(self, content_piece_id: UUID) -> FormTarget | None:
        if self._form is not None and self._form.content_piece_id == content_piece_id:
            return self._form
        return None

    async def resolve(self, code: str) -> ShortLinkRecord | None:
        if self._link is not None and self._link.code == code:
            return self._link
        return None

    async def create_lead(
        self,
        business_id: UUID,
        *,
        fields: dict[str, Any],
        utm: dict[str, Any],
        short_link_id: UUID | None = None,
        content_piece_id: UUID | None = None,
        source: str = "form",
    ) -> LeadRecord:
        self.created.append(
            {
                "business_id": business_id,
                "fields": fields,
                "utm": utm,
                "short_link_id": short_link_id,
                "content_piece_id": content_piece_id,
                "source": source,
            }
        )
        return LeadRecord(
            id=uuid4(),
            business_id=business_id,
            content_piece_id=content_piece_id,
            short_link_id=short_link_id,
            source=source,
            utm=dict(utm),
            fields=dict(fields),
            status="new",
            created_at=datetime.now(UTC),
        )

    async def list_leads(self, business_id: UUID, *, limit: int = 100) -> list[LeadRecord]:
        self.listed.append((business_id, limit))
        return [lead for lead in self._leads if lead.business_id == business_id]

    async def business_for_owner(self, user_id: UUID) -> UUID | None:
        return self._business if user_id == OWNER_ID else None


def a_form(*, status: str = "published") -> FormTarget:
    return FormTarget(
        business_id=BUSINESS_ID,
        content_piece_id=FORM_ID,
        status=status,
        title="Notdienst Koblenz",
    )


def a_link(*, code: str = "abcd2345", business_id: UUID = BUSINESS_ID) -> ShortLinkRecord:
    return ShortLinkRecord(
        id=LINK_ID,
        business_id=business_id,
        code=code,
        target_url="https://mueller.example/lp",
        content_piece_id=FORM_ID,
        channel="instagram",
        campaign="notdienst",
        click_count=3,
    )


def a_lead(*, business_id: UUID = BUSINESS_ID, name: str = "Petra Klein") -> LeadRecord:
    return LeadRecord(
        id=uuid4(),
        business_id=business_id,
        content_piece_id=FORM_ID,
        short_link_id=LINK_ID,
        source="form",
        utm={"utm_source": "instagram"},
        fields={"name": name, "email": "petra@example.test"},
        status="new",
        created_at=datetime.now(UTC),
    )


def an_owner() -> User:
    return User(
        id=OWNER_ID,
        email="owner@example.test",
        password_hash="x",
        is_active=True,
        role=Role.OWNER,
    )


def a_limiter(*, limit: int = 5) -> FixedWindowRateLimiter:
    """A limiter with its own private counter.

    Never the shipped one: it is process-wide and cached, so the suite's own
    submissions would become each other's budget and the failure would land in
    whichever test happened to run sixth.
    """
    return FixedWindowRateLimiter(
        rules={DIMENSION_IP: RateLimitRule(limit=limit, window_seconds=600)},
        counter=InMemoryWindowCounter(),
        namespace="test:lead-form",
        secret="test-secret-not-a-real-one",
    )


def _client(
    store: FakeStore,
    *,
    authenticated: bool = False,
    limiter: FixedWindowRateLimiter | None = None,
) -> httpx.AsyncClient:
    from backend.app.main import create_app

    app = create_app()
    app.include_router(leads_api.public_router)
    app.include_router(leads_api.router)
    app.dependency_overrides[leads_api.get_store] = lambda: store
    app.dependency_overrides[leads_api.get_form_limiter] = lambda: limiter or a_limiter()
    if authenticated:
        app.dependency_overrides[current_user] = an_owner
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# POST /public/forms/{id} -- the happy path
# --------------------------------------------------------------------------- #


async def test_a_submission_becomes_a_lead_attributed_to_the_content_piece() -> None:
    """The whole point of the loop: the lead names the content that produced it."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)

    assert response.status_code == 202
    assert len(store.created) == 1
    created = store.created[0]
    assert created["business_id"] == BUSINESS_ID
    assert created["content_piece_id"] == FORM_ID
    assert created["fields"]["email"] == "petra@example.test"
    assert created["utm"]["utm_source"] == "instagram"


async def test_the_response_reflects_nothing_that_was_submitted() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)

    assert "Petra" not in response.text
    assert "petra@example.test" not in response.text
    assert str(BUSINESS_ID) not in response.text


async def test_a_ref_code_attributes_the_lead_to_the_short_link_that_earned_it() -> None:
    """The Instagram bio path end to end.

    The landing page carries the code it was reached by, so the click and the lead
    are the same story rather than two unconnected numbers.
    """
    store = FakeStore(form=a_form(), link=a_link())

    async with _client(store) as client:
        await client.post(f"/public/forms/{FORM_ID}", json={**VALID_BODY, "ref": "abcd2345"})

    assert store.created[0]["short_link_id"] == LINK_ID


async def test_a_ref_code_from_another_business_is_ignored_but_the_lead_is_kept() -> None:
    """Attribution is worth less than the lead.

    A mismatched code means the attribution cannot be trusted, so it is dropped --
    but refusing the submission would throw away a real enquiry from a real person
    because of a bad query parameter, and it would also let anyone plant leads in
    another business's report.
    """
    store = FakeStore(form=a_form(), link=a_link(business_id=OTHER_BUSINESS_ID))

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", json={**VALID_BODY, "ref": "abcd2345"}
        )

    assert response.status_code == 202
    assert store.created[0]["short_link_id"] is None


async def test_an_unknown_ref_code_is_ignored_but_the_lead_is_kept() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", json={**VALID_BODY, "ref": "zzzz2345"}
        )

    assert response.status_code == 202
    assert store.created[0]["short_link_id"] is None


async def test_no_ip_and_no_user_agent_are_stored_on_the_lead() -> None:
    """The same rule as the click, and this is the path most likely to break it.

    ``fields`` is JSONB, so there is nothing structural stopping a route from
    quietly adding the caller's address to it. The route builds the blob from the
    declared schema only.
    """
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        await client.post(
            f"/public/forms/{FORM_ID}",
            json=VALID_BODY,
            headers={"user-agent": "curl/8.7.1", "x-forwarded-for": "203.0.113.42"},
        )

    blob = " ".join(str(value) for value in store.created[0]["fields"].values())
    assert "203.0.113.42" not in blob
    assert "curl" not in blob
    forbidden = ("ip", "ip_address", "user_agent", "ua", "remote_addr")
    assert not [key for key in store.created[0]["fields"] if key in forbidden]


# --------------------------------------------------------------------------- #
# POST /public/forms/{id} -- refusals
# --------------------------------------------------------------------------- #


async def test_a_filled_honeypot_is_answered_exactly_like_a_success() -> None:
    """Indistinguishable on purpose.

    A bot that gets a 400 learns which field to leave empty next time; a bot that
    gets a 202 learns nothing and keeps filling it. The refusal is that nothing is
    stored.
    """
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        honest = await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)
        trapped = await client.post(
            f"/public/forms/{FORM_ID}", json={**VALID_BODY, "homepage2": "https://spam.example"}
        )

    assert trapped.status_code == honest.status_code
    assert trapped.text == honest.text
    assert len(store.created) == 1


async def test_a_body_larger_than_the_cap_is_refused_before_it_is_read() -> None:
    store = FakeStore(form=a_form())
    huge = {**VALID_BODY, "message": "x" * 20_000}

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{FORM_ID}", json=huge)

    assert response.status_code == 413
    assert store.created == []


async def test_a_body_with_no_declared_length_is_refused() -> None:
    """Chunked upload from an anonymous caller is a stream with no bound.

    The cap can only be enforced before reading if the length is declared, so a
    request that declines to declare one is refused rather than read hopefully.
    """
    store = FakeStore(form=a_form())

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"name":"x","email":"x@y.test","consent":true}'

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{FORM_ID}", content=chunks())

    assert response.status_code == 411
    assert store.created == []


async def test_a_lying_content_length_does_not_get_past_the_cap() -> None:
    """The header is caller-supplied, so it is a hint and not a fact."""
    store = FakeStore(form=a_form())
    payload = b'{"name":"x","email":"x@y.test","consent":true,"message":"' + b"x" * 20_000 + b'"}'

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            content=payload,
            headers={"content-type": "application/json", "content-length": "40"},
        )

    assert response.status_code == 413
    assert store.created == []


async def test_the_rate_limit_refuses_the_sixth_submission_from_one_address() -> None:
    store = FakeStore(form=a_form())
    limiter = a_limiter(limit=5)

    async with _client(store, limiter=limiter) as client:
        for _ in range(5):
            assert (
                await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)
            ).status_code == 202
        refused = await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)

    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    assert len(store.created) == 5


async def test_the_rate_limit_refusal_names_neither_the_dimension_nor_the_limit() -> None:
    """Telling a caller which limit it hit tells it what to vary.

    There is one dimension today, so the leak is small -- but the body is the place
    that habit forms, and ``core.rate_limit`` already treats the dimension as
    log-only.
    """
    store = FakeStore(form=a_form())
    limiter = a_limiter(limit=1)

    async with _client(store, limiter=limiter) as client:
        await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)
        refused = await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)

    assert refused.status_code == 429
    body = refused.text.lower()
    assert "ip" not in body.replace("description", "")
    assert "1" not in body


async def test_the_rate_limit_is_checked_before_the_form_is_looked_up() -> None:
    """Otherwise the throttle protects the write and not the lookup.

    The form lookup is a privileged, unscoped database read (see
    ``db/adapters/lead_store.py``), so it is exactly the thing a flood must not be
    able to run at will.
    """
    store = FakeStore(form=a_form())
    limiter = a_limiter(limit=1)

    async with _client(store, limiter=limiter) as client:
        await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)
        refused = await client.post(f"/public/forms/{uuid4()}", json=VALID_BODY)

    assert refused.status_code == 429


async def test_an_unknown_form_is_a_404() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{uuid4()}", json=VALID_BODY)

    assert response.status_code == 404
    assert store.created == []


async def test_a_form_id_that_is_not_an_identifier_is_a_404() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post("/public/forms/notauuid", json=VALID_BODY)

    assert response.status_code == 404
    assert "notauuid" not in response.text


async def test_a_draft_landing_page_does_not_accept_leads_and_does_not_admit_it() -> None:
    """404, not 403.

    A 403 would confirm that a draft page exists at that id, which is a preview of
    unpublished work -- and the submitter can do nothing with the distinction.
    """
    store = FakeStore(form=a_form(status="draft"))

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{FORM_ID}", json=VALID_BODY)

    assert response.status_code == 404
    assert "draft" not in response.text.lower()
    assert store.created == []


async def test_an_unexpected_field_is_refused_rather_than_stored() -> None:
    """``fields`` is JSONB, so an open schema means an anonymous caller chooses
    what we keep -- and a public write that stores arbitrary keys is a free
    key-value store with our name on the bill."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", json={**VALID_BODY, "is_admin": True}
        )

    assert response.status_code == 422
    assert store.created == []


async def test_a_validation_failure_does_not_echo_the_submitted_value() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            json={**VALID_BODY, "email": "definitely-not-an-email-address"},
        )

    assert response.status_code == 422
    assert "definitely-not-an-email-address" not in response.text
    assert "Petra" not in response.text


async def test_a_lead_with_no_way_to_reply_is_refused() -> None:
    """A lead nobody can answer is not a lead.

    The business gets an alert, calls nobody, and stops trusting the number.
    """
    store = FakeStore(form=a_form())
    unreachable = {"name": "Petra Klein", "consent": True}

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{FORM_ID}", json=unreachable)

    assert response.status_code == 422
    assert store.created == []


async def test_a_submission_without_consent_is_refused() -> None:
    """Storing contact details with no recorded consent is the compliance problem
    this product would otherwise hand to every customer at once."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", json={**VALID_BODY, "consent": False}
        )

    assert response.status_code == 422
    assert store.created == []


async def test_unknown_utm_keys_are_dropped_rather_than_stored() -> None:
    """``utm`` is a public, caller-controlled JSONB blob.

    Filtering to the five real parameters keeps it a measurement field instead of a
    place to put anything at all.
    """
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        await client.post(
            f"/public/forms/{FORM_ID}",
            json={
                **VALID_BODY,
                "utm": {"utm_source": "tiktok", "evil": "x" * 50, "utm_term": "notdienst"},
            },
        )

    stored = store.created[0]["utm"]
    assert stored == {"utm_source": "tiktok", "utm_term": "notdienst"}


async def test_a_malformed_json_body_is_a_422_and_not_a_500() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            content=b"{not json at all",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert store.created == []


# --------------------------------------------------------------------------- #
# GET /api/v1/leads
# --------------------------------------------------------------------------- #


async def test_listing_leads_requires_a_session() -> None:
    store = FakeStore(leads=[a_lead()])

    async with _client(store) as client:
        response = await client.get("/api/v1/leads")

    assert response.status_code == 401


async def test_an_owner_sees_its_own_leads() -> None:
    store = FakeStore(leads=[a_lead(name="Petra Klein")])

    async with _client(store, authenticated=True) as client:
        response = await client.get("/api/v1/leads")

    assert response.status_code == 200
    body = response.json()
    assert len(body["leads"]) == 1
    assert body["leads"][0]["fields"]["name"] == "Petra Klein"
    assert body["leads"][0]["contentPieceId"] == str(FORM_ID)


async def test_the_business_comes_from_the_session_and_not_from_the_query() -> None:
    """The one thing that must not be caller-controllable.

    A ``businessId`` parameter that worked would be a complete cross-tenant read,
    and FastAPI ignores unknown query parameters silently -- so the assertion is on
    which business the store was actually asked about.
    """
    store = FakeStore(leads=[a_lead()])

    async with _client(store, authenticated=True) as client:
        response = await client.get(f"/api/v1/leads?businessId={OTHER_BUSINESS_ID}")

    assert response.status_code == 200
    assert [business for business, _ in store.listed] == [BUSINESS_ID]


async def test_an_owner_with_no_business_gets_an_empty_list_rather_than_an_error() -> None:
    """Anomalous, not broken.

    Signup creates the business in the same transaction as the user, so this state
    means a platform-admin account or a removed membership -- neither of which is
    the caller's fault, and neither of which has any leads to show.
    """
    store = FakeStore(leads=[a_lead()], business=None)

    async with _client(store, authenticated=True) as client:
        response = await client.get("/api/v1/leads")

    assert response.status_code == 200
    assert response.json()["leads"] == []


@pytest.mark.parametrize("limit", [0, -1, 5000])
async def test_a_nonsense_limit_is_refused(limit: int) -> None:
    store = FakeStore(leads=[a_lead()])

    async with _client(store, authenticated=True) as client:
        response = await client.get(f"/api/v1/leads?limit={limit}")

    assert response.status_code == 422
    assert store.listed == []


async def test_the_limit_is_passed_through_to_the_store() -> None:
    store = FakeStore(leads=[a_lead()])

    async with _client(store, authenticated=True) as client:
        await client.get("/api/v1/leads?limit=25")

    assert store.listed == [(BUSINESS_ID, 25)]


def test_the_public_router_and_the_authed_router_are_separate() -> None:
    """They are mounted separately on purpose.

    One of them is an anonymous write and the other reads a tenant's customer
    records; keeping them as two objects means "which of these is public?" is
    answered by the mount site, not by reading every decorator.
    """
    public_paths = {route.path for route in _paths(leads_api.public_router.routes)}
    authed_paths = {route.path for route in _paths(leads_api.router.routes)}

    assert public_paths == {"/public/forms/{form_id}"}
    assert authed_paths == {"/api/v1/leads"}


def _paths(routes: Iterable[Any]) -> list[Any]:
    return [route for route in routes if hasattr(route, "path")]


# --------------------------------------------------------------------------- #
# The no-JavaScript path: a plain HTML form post
#
# The generated landing page carries no script (see api/pages.py), so a real
# visitor's lead arrives as `application/x-www-form-urlencoded`. A JSON-only
# endpoint would refuse every one of them AFTER the visitor had typed it in, which
# is the most expensive way to lose a lead. Every protection above still applies:
# the same schema, the same honeypot, the same size cap, the same rate limit.
# --------------------------------------------------------------------------- #


FORM_BODY: dict[str, str] = {
    "name": "Petra Klein",
    "email": "petra@example.test",
    # What a checked checkbox actually sends. An unchecked one sends nothing at all.
    "consent": "on",
    "utm_source": "instagram",
    "utm_campaign": "notdienst",
    "homepage2": "",
}


async def test_a_plain_html_form_post_becomes_a_lead() -> None:
    """The path a visitor with JavaScript off actually takes."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", data=FORM_BODY, follow_redirects=False
        )

    assert response.status_code == 303, "a form post is answered with a redirect, not JSON"
    assert len(store.created) == 1, "a form post must produce a lead, not a 422"
    created = store.created[0]
    assert created["fields"]["email"] == "petra@example.test"
    assert created["fields"]["consent"] is True
    assert created["content_piece_id"] == FORM_ID


async def test_the_flat_utm_inputs_are_folded_back_into_the_utm_map() -> None:
    """A form cannot send a nested object, so the page renders `utm_source` and
    `utm_campaign` as hidden inputs. Without folding them the campaign that produced
    the lead would be lost -- which is link four of the chain."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        await client.post(f"/public/forms/{FORM_ID}", data=FORM_BODY)

    assert store.created[0]["utm"] == {
        "utm_source": "instagram",
        "utm_campaign": "notdienst",
    }


async def test_a_form_post_is_answered_with_a_redirect_back_to_the_page() -> None:
    """A JSON body is not an answer a person in a browser can act on. 303 rather than
    302 because the request was a POST and the next thing to fetch is a GET."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", data=FORM_BODY, follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/p/{FORM_ID}?sent=1"
    assert response.headers["cache-control"] == "no-store"


async def test_the_redirect_target_is_built_from_the_form_and_never_from_the_request() -> None:
    """A redirect target taken from a parameter is an open redirect, and this endpoint
    is public."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            data={**FORM_BODY, "utm_next": "https://evil.example"},
            follow_redirects=False,
        )

    assert response.headers["location"] == f"/p/{FORM_ID}?sent=1"
    assert "evil.example" not in response.headers["location"]


async def test_a_form_post_with_no_consent_is_sent_back_with_an_error_flag() -> None:
    """An unchecked checkbox sends nothing at all, so this is the ordinary mistake --
    and the visitor has to be told, on the page, in their own language."""
    store = FakeStore(form=a_form())
    body = {key: value for key, value in FORM_BODY.items() if key != "consent"}

    async with _client(store) as client:
        response = await client.post(f"/public/forms/{FORM_ID}", data=body, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/p/{FORM_ID}?error=1"
    assert store.created == [], "nothing may be stored without evidence of consent"


async def test_a_form_post_with_no_way_to_reply_is_sent_back_with_an_error_flag() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            data={"name": "Petra", "consent": "on"},
            follow_redirects=False,
        )

    assert response.headers["location"] == f"/p/{FORM_ID}?error=1"
    assert store.created == []


async def test_a_form_post_error_reflects_nothing_that_was_submitted() -> None:
    """One bit -- `error=1` -- and the page's own copy says what is required."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            data={"email": "petra@example.test", "name": "Petra Klein"},
            follow_redirects=False,
        )

    assert "petra@example.test" not in response.text
    assert "petra@example.test" not in response.headers["location"]
    assert "Petra" not in response.text


async def test_a_filled_honeypot_on_a_form_post_is_answered_exactly_like_a_success() -> None:
    """Byte-identical to the success answer, including the redirect target: a bot must
    not be able to tell that the field it filled in is the one that gave it away."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        success = await client.post(
            f"/public/forms/{FORM_ID}", data=FORM_BODY, follow_redirects=False
        )
        trapped = await client.post(
            f"/public/forms/{FORM_ID}",
            data={**FORM_BODY, "homepage2": "https://spam.example"},
            follow_redirects=False,
        )

    assert trapped.status_code == success.status_code == 303
    assert trapped.headers["location"] == success.headers["location"]
    assert len(store.created) == 1, "the honeypot submission must not be stored"


async def test_an_unexpected_field_on_a_form_post_is_still_refused() -> None:
    """`extra="forbid"` is one schema, shared by both encodings: `leads.fields` is
    JSONB, and an open schema on a public endpoint is a free key-value store with our
    name on the bill."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            data={**FORM_BODY, "budget": "10000"},
            follow_redirects=False,
        )

    assert response.headers["location"] == f"/p/{FORM_ID}?error=1"
    assert store.created == []


async def test_a_form_post_to_a_draft_page_is_still_a_404() -> None:
    """The page route refuses to serve a draft, and this refuses to take its leads --
    and neither admits which of the two reasons applies."""
    store = FakeStore(form=a_form(status="draft"))

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", data=FORM_BODY, follow_redirects=False
        )

    assert response.status_code == 404
    assert store.created == []


async def test_a_form_post_over_the_size_cap_is_still_refused() -> None:
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            data={**FORM_BODY, "message": "x" * (leads_api.MAX_FORM_BODY_BYTES + 1)},
            follow_redirects=False,
        )

    assert response.status_code == 413
    assert store.created == []


async def test_the_rate_limit_applies_to_form_posts_too() -> None:
    store = FakeStore(form=a_form())
    limiter = a_limiter(limit=1)

    async with _client(store, limiter=limiter) as client:
        first = await client.post(
            f"/public/forms/{FORM_ID}", data=FORM_BODY, follow_redirects=False
        )
        second = await client.post(
            f"/public/forms/{FORM_ID}", data=FORM_BODY, follow_redirects=False
        )

    assert first.status_code == 303
    assert second.status_code == 429


async def test_a_charset_parameter_on_the_content_type_still_routes_as_a_form() -> None:
    """A browser sends `application/x-www-form-urlencoded; charset=UTF-8`, so an exact
    string comparison would send every real submission down the JSON path and refuse
    it."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}",
            content=b"email=petra%40example.test&consent=on",
            headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert len(store.created) == 1


async def test_a_json_caller_still_gets_the_constant_202_and_no_redirect() -> None:
    """The existing contract is unchanged: adding an encoding must not move the answer
    the API's own clients already depend on."""
    store = FakeStore(form=a_form())

    async with _client(store) as client:
        response = await client.post(
            f"/public/forms/{FORM_ID}", json=VALID_BODY, follow_redirects=False
        )

    assert response.status_code == 202
    assert response.json() == {"status": "received"}
    assert "location" not in response.headers
