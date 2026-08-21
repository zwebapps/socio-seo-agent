"""The automation panel's two routes.

Hermetic: the fake session is a stub of ONE ROW and the real
`automation_settings_service` runs against it, so the upsert's semantics — recomputing
the slot, clearing it when switched off, refusing an unstorable schedule — are genuinely
exercised and only Postgres is absent. A test that mocked the service would pass on the
day its semantics changed, which is the half that matters to the owner. The SQL itself is
covered by `tests/db/test_automation_settings.py`.

What is under attack in a route like this, and therefore what is asserted:

  no session                    -> 401  (who are you?)
  a business id from the client  -> ignored; this route derives it from the session
  a `nextRunAt` from the client  -> ignored; the server computes it, always
  an unstorable schedule         -> 422, and nothing written
  switching off                  -> the slot is cleared as well as the mode

The load-bearing behavioural test is
`test_the_response_carries_the_slot_the_scheduler_will_compare_against`. A 204 would look
equivalent and would leave the screen rendering a schedule it computed itself — which is
the class of bug this whole feature exists to close: a control that reports success while
the worker does something else.

`current_user` is overridden rather than a real cookie minted, so these tests do not move
when the session-token format does. Authentication itself is tested in `test_auth_api.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from backend.app.api import automation as automation_api
from backend.app.api.auth import current_user
from backend.app.api.runs import current_business
from backend.app.db.models import AutomationMode, AutomationSetting, Role, User
from backend.app.main import create_app
from backend.app.services import automation_settings_service as svc

BUSINESS = UUID("11111111-1111-4111-8111-111111111111")

#: A Wednesday, 12:00 UTC. Fixed so an assertion about which Thursday a save lands on
#: does not depend on the day the suite runs.
WEDNESDAY = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

AUTOMATION = "/api/v1/automation"

#: A complete, valid body. PUT is a full replacement, so every test starts from a whole
#: instruction and changes one thing — which is also how the form behaves.
VALID: dict[str, Any] = {
    "enabled": True,
    "cadence": "weekly",
    "dayOfWeek": 3,
    "hour": 8,
    "timezone": "Europe/Berlin",
    "channels": ["linkedin"],
    "goalTemplate": "more local enquiries",
}


def _user() -> User:
    user = User(email="owner@example.test", password_hash="x", is_active=True, role=Role.OWNER)
    user.id = uuid4()
    return user


class _Result:
    """What `session.execute` returns, for either shape of statement the service issues."""

    def __init__(self, row: AutomationSetting | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> AutomationSetting | None:
        return self._row

    def scalar_one(self) -> AutomationSetting:
        assert self._row is not None
        return self._row


class _OneRowSession:
    """A session holding at most one automation row, in memory.

    The service issues exactly two shapes of statement — a SELECT by business id and an
    INSERT ... ON CONFLICT ... RETURNING — and this tells them apart by whether the
    statement has values to apply. Applying them by hand is what keeps the service's own
    decisions (which fields it writes, and with what) under test rather than mocked away.
    """

    def __init__(self, row: AutomationSetting | None = None) -> None:
        self.row = row
        self.flushes = 0
        self.writes: list[dict[str, Any]] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        values = self._values(statement)
        if values is None:
            return _Result(self.row)
        self.writes.append(values)
        row = self.row or AutomationSetting()
        for key, value in values.items():
            setattr(row, key, value)
        # Columns the INSERT does not name keep their existing value; a fresh row has
        # never run, which is what `last_run_at is None` means here.
        row.last_run_at = getattr(row, "last_run_at", None)
        self.row = row
        return _Result(row)

    @staticmethod
    def _values(statement: Any) -> dict[str, Any] | None:
        compiled = getattr(statement, "_values", None)
        if not compiled:
            return None
        return {
            key.name if hasattr(key, "name") else str(key): getattr(value, "value", value)
            for key, value in compiled.items()
        }

    async def flush(self) -> None:
        self.flushes += 1


class _NeverOpened:
    """A session that fails the test if a refused request reaches the database."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a refused request opened a query")

    async def flush(self) -> None:
        raise AssertionError("a refused request wrote to the database")


def _client(
    session: _OneRowSession | _NeverOpened,
    *,
    authenticated: bool = True,
    now: datetime = WEDNESDAY,
) -> httpx.AsyncClient:
    app = create_app()

    if authenticated:
        app.dependency_overrides[current_user] = _user
        app.dependency_overrides[current_business] = lambda: BUSINESS
    else:

        def _no_session() -> User:
            raise HTTPException(status_code=401, detail={"code": "not_authenticated"})

        app.dependency_overrides[current_user] = _no_session

    @asynccontextmanager
    async def _opener(_bid: UUID) -> AsyncIterator[Any]:
        yield session

    app.dependency_overrides[automation_api.get_business_session_opener] = lambda: _opener
    app.dependency_overrides[automation_api.get_clock] = lambda: lambda: now
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _stored(business_id: UUID = BUSINESS, **fields: Any) -> AutomationSetting:
    row = AutomationSetting()
    row.business_id = business_id
    row.mode = fields.pop("mode", AutomationMode.SCHEDULED_DRAFT.value)
    row.cadence = fields.pop("cadence", "weekly")
    row.day_of_week = fields.pop("day_of_week", 1)
    row.hour = fields.pop("hour", 9)
    row.timezone = fields.pop("timezone", "Europe/Berlin")
    row.channels = fields.pop("channels", [])
    row.goal_template = fields.pop("goal_template", None)
    row.next_run_at = fields.pop("next_run_at", None)
    row.last_run_at = fields.pop("last_run_at", None)
    row.paused_reason = fields.pop("paused_reason", None)
    assert not fields, f"unknown field(s): {sorted(fields)}"
    return row


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["get", "put"])
async def test_both_routes_require_a_session(method: str) -> None:
    """`get` takes no body and `put` does, so the call is built rather than dispatched."""
    async with _client(_NeverOpened(), authenticated=False) as client:
        response = await client.request(
            method.upper(), AUTOMATION, json=VALID if method == "put" else None
        )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


async def test_an_unconfigured_business_is_told_the_values_are_defaults() -> None:
    """`configured: false` is what stops the panel showing a schedule nobody chose."""
    async with _client(_OneRowSession(None)) as client:
        response = await client.get(AUTOMATION)

    body = response.json()
    assert response.status_code == 200
    assert body["configured"] is False
    assert body["enabled"] is False
    assert body["nextRunAt"] is None
    assert body["hour"] == svc.DEFAULT_HOUR


async def test_reading_writes_nothing() -> None:
    session = _OneRowSession(None)
    async with _client(session) as client:
        await client.get(AUTOMATION)

    assert session.writes == []
    assert session.flushes == 0


async def test_the_read_carries_the_vocabulary_the_form_may_send() -> None:
    """A channel picker offering something the server refuses is the drift this prevents."""
    async with _client(_OneRowSession(None)) as client:
        body = (await client.get(AUTOMATION)).json()

    assert "linkedin" in body["knownChannels"]
    assert body["knownCadences"] == ["weekly", "biweekly", "monthly"]
    assert body["maxGoalLength"] > 0
    assert body["pollIntervalSeconds"] > 0
    assert "enabled" in body["editableFields"]


async def test_a_system_pause_is_reported_as_not_enabled_with_its_reason() -> None:
    """`mode` alone would show "on" for an automation that has stopped itself."""
    row = _stored(paused_reason="budget exhausted", next_run_at=WEDNESDAY)
    async with _client(_OneRowSession(row)) as client:
        body = (await client.get(AUTOMATION)).json()

    assert body["mode"] == AutomationMode.SCHEDULED_DRAFT
    assert body["enabled"] is False
    assert body["pausedReason"] == "budget exhausted"


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


async def test_the_response_carries_the_slot_the_scheduler_will_compare_against() -> None:
    """The load-bearing one: the screen must not have to compute the slot itself.

    Thursday (`dayOfWeek: 3`) at 08:00 Europe/Berlin is 06:00 UTC, the day after
    WEDNESDAY. If the route returned 204, or returned the request back, a panel could
    only guess — and a guess that disagrees with the worker is exactly the failure this
    feature closes.
    """
    async with _client(_OneRowSession(None)) as client:
        response = await client.put(AUTOMATION, json=VALID)

    body = response.json()
    assert response.status_code == 200
    assert body["configured"] is True
    assert body["enabled"] is True
    assert body["nextRunAt"] == "2026-08-20T06:00:00Z"


async def test_switching_off_clears_the_slot_as_well_as_the_mode() -> None:
    """Two independent reasons the worker cannot pick the row up."""
    session = _OneRowSession(_stored(next_run_at=WEDNESDAY))
    async with _client(session) as client:
        body = (await client.put(AUTOMATION, json={**VALID, "enabled": False})).json()

    assert body["enabled"] is False
    assert body["mode"] == AutomationMode.OFF
    assert body["nextRunAt"] is None


async def test_a_client_supplied_next_run_at_is_ignored() -> None:
    """It is a cache of the arithmetic. Accepting one makes the client authoritative."""
    async with _client(_OneRowSession(None)) as client:
        body = (
            await client.put(AUTOMATION, json={**VALID, "nextRunAt": "2030-01-01T00:00:00Z"})
        ).json()

    assert body["nextRunAt"] == "2026-08-20T06:00:00Z"


async def test_a_client_supplied_business_id_is_ignored() -> None:
    """The business comes from the session; there is no cross-tenant path to defend."""
    session = _OneRowSession(None)
    async with _client(session) as client:
        body = (await client.put(AUTOMATION, json={**VALID, "businessId": str(uuid4())})).json()

    assert body["businessId"] == str(BUSINESS)
    assert session.writes[0]["business_id"] == BUSINESS


async def test_a_write_never_touches_last_run_at() -> None:
    """It is the worker's record, and biweekly parity is computed from it."""
    session = _OneRowSession(None)
    async with _client(session) as client:
        await client.put(AUTOMATION, json={**VALID, "cadence": "biweekly"})

    assert "last_run_at" not in session.writes[0]


async def test_channels_are_canonicalised_on_the_way_in() -> None:
    async with _client(_OneRowSession(None)) as client:
        body = (
            await client.put(AUTOMATION, json={**VALID, "channels": ["Facebook_post", "facebook"]})
        ).json()

    assert body["channels"] == ["facebook"]


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field,value",
    [("dayOfWeek", 7), ("hour", 24), ("dayOfWeek", -1)],
)
async def test_a_schema_bound_is_refused_before_the_database_is_touched(
    field: str, value: int
) -> None:
    """`dayOfWeek: 7` is the ISO-vs-Python off-by-one arriving as a request body."""
    async with _client(_NeverOpened()) as client:
        response = await client.put(AUTOMATION, json={**VALID, field: value})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [("timezone", "Europe/Nowhere"), ("cadence", "daily"), ("channels", ["threads"])],
)
async def test_a_service_refusal_is_422_with_the_sentence_that_names_the_bound(
    field: str, value: Any
) -> None:
    """The message is written for the person in the form, so it survives to the wire."""
    async with _client(_OneRowSession(None)) as client:
        response = await client.put(AUTOMATION, json={**VALID, field: value})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_schedule"
    assert str(value if isinstance(value, str) else value[0]) in detail["message"]


async def test_an_over_long_goal_is_refused() -> None:
    async with _client(_NeverOpened()) as client:
        response = await client.put(
            AUTOMATION,
            json={**VALID, "goalTemplate": "x" * 5000},
        )

    assert response.status_code == 422
