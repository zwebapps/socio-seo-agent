"""``GET /api/v1/dashboard``: the wire contract, with no database in the path.

The SQL is proved in ``tests/db/test_dashboard_service.py`` against real rows and real
row-level security. What is proved HERE is everything that can go wrong between the
service's dataclass and the JSON a browser parses -- and each of these has a failure that
would look completely fine on screen:

* **a `null` coerced to `0`.** The service exists to keep "never measured" apart from
  "measured zero"; a response model that defaulted the optionals would print a
  measurement nobody took, and the tile would look right;
* **money as a JSON number.** `0.3` is not representable in binary floating point, so a
  `Decimal` serialised as a number leaves as `0.30000000000000004` -- correct-looking
  until someone reads the ledger;
* **snake_case on the wire.** The frontend reads `clicksTotal`; `clicks_total` arrives as
  `undefined`, which renders as an empty tile rather than an error;
* **an unscoped session.** The route must read through the opener it was given. A route
  that reached for `db.session()` itself would see every tenant's rows in production and
  pass every test that only checked the numbers.

``current_user`` is overridden rather than a real cookie being minted, so these tests do
not move when the session-token format does; authentication itself is tested in
``test_auth_api.py``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from backend.app.api import dashboard as dashboard_api
from backend.app.api import runs as runs_api
from backend.app.api.auth import current_user
from backend.app.db.models import Role, User
from backend.app.main import create_app
from backend.app.services.dashboard_service import ChannelClicks, DashboardSummary

BUSINESS_ID = UUID("11111111-1111-1111-1111-111111111111")


def _user() -> User:
    return User(email="owner@example.test", password_hash="x", is_active=True, role=Role.OWNER)


class _ScopedSession:
    """The object the opener yields. Identity is the whole point: the test asserts the
    route handed THIS to the service rather than opening one of its own."""


@asynccontextmanager
async def _opener(business_id: UUID) -> AsyncIterator[Any]:
    del business_id
    yield _ScopedSession()


def _app(
    summary: DashboardSummary | None = None,
    *,
    user: User | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
    seen: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    """``user=None`` means no session, which is the only case that needs no summary."""
    app = create_app()

    if user is None:

        def _no_session() -> User:
            raise HTTPException(status_code=401, detail={"code": "not_authenticated"})

        app.dependency_overrides[current_user] = _no_session
    else:
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[runs_api.current_business] = lambda: BUSINESS_ID
        app.dependency_overrides[dashboard_api.get_business_session_opener] = lambda: _opener

    if summary is not None:
        assert monkeypatch is not None

        async def _read(business_id: UUID, *, session: Any) -> DashboardSummary:
            if seen is not None:
                seen["business_id"] = business_id
                seen["session"] = session
            return summary

        monkeypatch.setattr(dashboard_api, "read_dashboard", _read)

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_no_session_is_401_and_never_reaches_the_service() -> None:
    """The service is not patched here, so a route that got past the gate would try to
    open a real connection and the test would fail loudly rather than 200."""
    async with _app(user=None) as client:
        response = await client.get("/api/v1/dashboard")
    assert response.status_code == 401


async def test_unmeasured_figures_stay_null_and_are_not_defaulted_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh-account shape: no links, no spend, no audit, no probe.

    Every one of these is `null` in the service and must be `null` on the wire. A `0`
    here is the product claiming a measurement it never took.
    """
    summary = DashboardSummary(clicks_total=None, gaps=("nothing measured yet",))

    async with _app(summary, user=_user(), monkeypatch=monkeypatch) as client:
        response = await client.get("/api/v1/dashboard")

    assert response.status_code == 200
    body = response.json()
    for field in ("clicksTotal", "spendUsd", "seoProblems", "seoPagesAudited", "shareOfVoice"):
        assert body[field] is None, f"{field} came back {body[field]!r}, not null"
    assert body["gaps"] == ["nothing measured yet"]


async def test_zero_clicks_is_reported_as_zero_and_not_as_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same distinction: links exist, nobody clicked. That IS a
    measurement, and `null` would hide a working link that nothing has reached."""
    summary = DashboardSummary(
        clicks_total=0,
        clicks_by_channel=(ChannelClicks(channel="link_hub", clicks=0),),
    )

    async with _app(summary, user=_user(), monkeypatch=monkeypatch) as client:
        body = (await client.get("/api/v1/dashboard")).json()

    assert body["clicksTotal"] == 0
    assert body["clicksByChannel"] == [{"channel": "link_hub", "clicks": 0}]


async def test_money_is_a_string_and_not_a_json_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """`0.1 + 0.2` is exactly the sum a float gets wrong, and it is the sum a real ledger
    of two calls produces. The raw text is asserted, not the parsed value, because
    `json.loads` would hide a number by turning it into a Python float."""
    summary = DashboardSummary(clicks_total=None, spend_usd=Decimal("0.1") + Decimal("0.2"))

    async with _app(summary, user=_user(), monkeypatch=monkeypatch) as client:
        response = await client.get("/api/v1/dashboard")

    assert '"spendUsd":"0.30000000"' in response.text.replace(" ", "")
    assert isinstance(response.json()["spendUsd"], str)


async def test_every_field_is_camel_case_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """`response_model_by_alias=True` is load-bearing: without it the aliases exist in the
    schema and the body ships snake_case, so a typed client reads `undefined` everywhere
    and the screen renders empty tiles with no error anywhere."""
    summary = DashboardSummary(
        clicks_total=7,
        clicks_from_bots=2,
        runs_total=3,
        runs_awaiting_approval=1,
        runs_partial=1,
        leads_total=4,
        seo_problems=9,
        seo_pages_audited=12,
        seo_truncated=True,
        share_of_voice=22.5,
    )

    async with _app(summary, user=_user(), monkeypatch=monkeypatch) as client:
        body = (await client.get("/api/v1/dashboard")).json()

    assert set(body) == {
        "businessId",
        "clicksTotal",
        "clicksByChannel",
        "clicksFromBots",
        "runsTotal",
        "runsAwaitingApproval",
        "runsPartial",
        "leadsTotal",
        "spendUsd",
        "seoProblems",
        "seoPagesAudited",
        "seoTruncated",
        "shareOfVoice",
        "gaps",
    }
    assert body["businessId"] == str(BUSINESS_ID)
    assert body["seoTruncated"] is True
    assert body["shareOfVoice"] == 22.5


async def test_the_read_uses_the_scoped_session_the_opener_gave_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row-level security is set by the opener's transaction, so a service call on any
    other session would be an unscoped, cross-tenant read that still returned plausible
    numbers. The business id must likewise be the DERIVED one, never a client's."""
    seen: dict[str, Any] = {}
    summary = DashboardSummary(clicks_total=None)

    async with _app(summary, user=_user(), monkeypatch=monkeypatch, seen=seen) as client:
        assert (await client.get("/api/v1/dashboard")).status_code == 200

    assert isinstance(seen["session"], _ScopedSession)
    assert seen["business_id"] == BUSINESS_ID


async def test_the_client_cannot_choose_the_business(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query parameter naming someone else's business is ignored, not honoured: which
    business a caller acts for is an authorisation decision, and one made by the client is
    not a decision at all."""
    seen: dict[str, Any] = {}
    summary = DashboardSummary(clicks_total=None)
    theirs = uuid4()

    async with _app(summary, user=_user(), monkeypatch=monkeypatch, seen=seen) as client:
        body = (await client.get(f"/api/v1/dashboard?businessId={theirs}")).json()

    assert seen["business_id"] == BUSINESS_ID
    assert body["businessId"] == str(BUSINESS_ID)
