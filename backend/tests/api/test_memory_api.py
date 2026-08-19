"""The business-memory panel's four routes.

Every test here is hermetic. The fake session is not a stub of the service -- it is a
stub of ONE ROW, and the real ``memory_service`` runs against it. So dedup, the ordering
guarantee, the ceiling, and in-place revision are all genuinely exercised; only Postgres
is absent. A test that mocked ``memory_service`` instead would pass on the day the
service's semantics changed, which is the half that actually matters to the owner.

What is under attack in an API like this, and therefore what is asserted:

  no session               -> 401  (who are you?)
  a business id from the client -> ignored; these routes derive it from the session
  a blank or over-long rule-> 422, and nothing written
  the 26th preference      -> 409  (the rule is fine; the list refuses it)
  an edit that would merge -> 409  (never silently lose a rule)
  a stale preference id    -> 404

The load-bearing behavioural test is
``test_editing_a_preference_keeps_its_position_in_the_list``. Delete-then-add would look
equivalent and would move the rule to the end -- and the list's order is the order the
owner confirmed things in, which prompt assembly emits verbatim into every model call.

``current_user`` is overridden rather than a real cookie minted, so these tests do not
move when the session-token format does. Authentication itself is tested in
``test_auth_api.py``; what is under test here is that these routes require it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from backend.app.api import memory as memory_api
from backend.app.api.auth import current_user
from backend.app.api.memory import preference_id
from backend.app.api.runs import current_business
from backend.app.db.models import Business, Role, User
from backend.app.main import create_app
from backend.app.services.memory_service import MAX_PREFERENCES, MAX_RULE_LENGTH

BUSINESS = UUID("11111111-1111-4111-8111-111111111111")
OTHER_BUSINESS = UUID("22222222-2222-4222-8222-222222222222")

NO_EXCLAIM = "Never use exclamation marks"
NO_SIE = "Address the reader as du, never Sie"


def _user() -> User:
    user = User(email="owner@example.test", password_hash="x", is_active=True, role=Role.OWNER)
    user.id = uuid4()
    return user


class _Result:
    """What ``session.execute`` returns for the one query the service makes."""

    def __init__(self, row: Business | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Business | None:
        return self._row


class _OneRowSession:
    """A session holding exactly one business row, in memory.

    ``execute`` ignores the statement because ``memory_service`` issues exactly one shape
    of query -- select the business by id, optionally FOR UPDATE. Ignoring it is honest
    here rather than lazy: what is being tested is the service's effect on ``dna``, and
    the SQL itself is covered by the database-backed tests in
    ``tests/services/test_memory_service.py``.
    """

    def __init__(self, row: Business | None) -> None:
        self.row = row
        self.flushes = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result(self.row)

    async def flush(self) -> None:
        self.flushes += 1


class _NeverOpened:
    """A session that fails the test if a refused request reaches the database."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a refused request opened a query")

    async def flush(self) -> None:
        raise AssertionError("a refused request wrote to the database")


def _business(preferences: list[str] | None = None, **dna: Any) -> Business:
    business = Business(id=BUSINESS, owner_id=uuid4(), name="Klempner Koblenz")
    business.dna = {"name": "Klempner Koblenz", **dna}
    if preferences is not None:
        business.dna["preferences"] = list(preferences)
    return business


def _client(
    session: _OneRowSession | _NeverOpened,
    *,
    authenticated: bool = True,
    business_id: UUID = BUSINESS,
) -> httpx.AsyncClient:
    app = create_app()

    if authenticated:
        app.dependency_overrides[current_user] = _user
        app.dependency_overrides[current_business] = lambda: business_id
    else:

        def _no_session() -> User:
            raise HTTPException(status_code=401, detail={"code": "not_authenticated"})

        app.dependency_overrides[current_user] = _no_session

    @asynccontextmanager
    async def _opener(_bid: UUID) -> AsyncIterator[Any]:
        yield session

    app.dependency_overrides[memory_api.get_business_session_opener] = lambda: _opener
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


#: The routes derive the business from the session, so there is no id in the path.
MEMORY = "/api/v1/memory"


# --------------------------------------------------------------------------- #
# The identity on the wire
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "variant",
    ["Never use exclamation marks", "never use exclamation marks", "Never  use exclamation  marks"],
)
def test_the_wire_id_is_the_same_for_rules_memory_treats_as_the_same(variant: str) -> None:
    """The id is derived from the dedup key, so it cannot address a rule dedup merged.

    If these differed, a panel could hold two ids for one stored rule and one of them
    would resolve to nothing.
    """
    assert preference_id(variant) == preference_id(NO_EXCLAIM)


def test_the_wire_id_separates_genuinely_different_rules() -> None:
    assert preference_id(NO_EXCLAIM) != preference_id(NO_SIE)


def test_the_wire_id_is_url_safe_and_short() -> None:
    """It goes in a path segment. A 200-character German sentence would not."""
    pref = preference_id(NO_SIE)
    assert len(pref) == 16
    assert pref.isalnum()


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


async def test_the_panel_gets_the_rules_the_ids_and_the_exact_prompt_lines() -> None:
    """``promptLines`` is the claim's evidence: it is what the next run is literally told.

    A panel listing rules without it would assert an effect it cannot demonstrate.
    """
    session = _OneRowSession(
        _business([NO_EXCLAIM, NO_SIE], tone="professional", audience="Hausbesitzer")
    )
    async with _client(session) as client:
        response = await client.get(MEMORY)

    assert response.status_code == 200
    body = response.json()
    assert [p["rule"] for p in body["preferences"]] == [NO_EXCLAIM, NO_SIE]
    assert [p["id"] for p in body["preferences"]] == [
        preference_id(NO_EXCLAIM),
        preference_id(NO_SIE),
    ]
    assert body["rememberedCount"] == 2
    assert body["tone"] == "professional"
    assert body["audience"] == "Hausbesitzer"
    assert body["promptLines"] == [
        "Write for this audience: Hausbesitzer.",
        NO_EXCLAIM,
        NO_SIE,
    ]


async def test_a_business_that_has_confirmed_nothing_is_empty_not_an_error() -> None:
    """An empty memory is a real state: it is what a new business contributes to a prompt."""
    async with _client(_OneRowSession(_business([]))) as client:
        body = (await client.get(MEMORY)).json()

    assert body["preferences"] == []
    assert body["rememberedCount"] == 0
    assert body["promptLines"] == []


async def test_the_response_carries_the_limits_so_the_ui_cannot_drift_from_them() -> None:
    async with _client(_OneRowSession(_business([]))) as client:
        body = (await client.get(MEMORY)).json()

    assert body["maxPreferences"] == MAX_PREFERENCES
    assert body["maxRuleLength"] == MAX_RULE_LENGTH


async def test_the_response_names_which_fields_this_api_can_change() -> None:
    """Tone and banned claims are shown but owned by onboarding. The panel's read-only
    markers come from the server so they cannot claim an edit the API would refuse."""
    async with _client(_OneRowSession(_business([], tone="friendly"))) as client:
        body = (await client.get(MEMORY)).json()

    assert body["editableFields"] == ["preferences"]


async def test_junk_in_dna_does_not_make_the_panel_unreadable() -> None:
    """``dna`` is JSONB and can hold whatever an older version or a hand-run statement
    put there. One malformed preference must not cost the owner the whole panel."""
    business = _business()
    business.dna["preferences"] = [NO_EXCLAIM, 42, None, "", "  ", NO_SIE]
    async with _client(_OneRowSession(business)) as client:
        response = await client.get(MEMORY)

    assert response.status_code == 200
    assert [p["rule"] for p in response.json()["preferences"]] == [NO_EXCLAIM, NO_SIE]


# --------------------------------------------------------------------------- #
# Adding
# --------------------------------------------------------------------------- #


async def test_adding_a_preference_returns_201_and_the_whole_memory() -> None:
    """The whole memory, so the panel repaints from the server's account rather than its
    own optimistic guess -- which is what keeps two open tabs in agreement."""
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        response = await client.post(f"{MEMORY}/preferences", json={"rule": NO_SIE})

    assert response.status_code == 201
    assert [p["rule"] for p in response.json()["preferences"]] == [NO_EXCLAIM, NO_SIE]
    assert session.row is not None
    assert session.row.dna["preferences"] == [NO_EXCLAIM, NO_SIE]


async def test_a_rule_is_normalised_before_it_is_stored() -> None:
    session = _OneRowSession(_business([]))
    async with _client(session) as client:
        body = (
            await client.post(f"{MEMORY}/preferences", json={"rule": "  Keep   it   short  "})
        ).json()

    assert [p["rule"] for p in body["preferences"]] == ["Keep it short"]


async def test_restating_a_rule_does_not_duplicate_it_in_every_future_prompt() -> None:
    """A duplicated instruction reads as emphasis, and emphasis on an arbitrary rule is a
    behaviour change nobody asked for."""
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        response = await client.post(
            f"{MEMORY}/preferences", json={"rule": "never use  EXCLAMATION marks"}
        )

    assert response.status_code == 201
    assert [p["rule"] for p in response.json()["preferences"]] == [NO_EXCLAIM]


@pytest.mark.parametrize("rule", ["   ", "\n\t "])
async def test_a_blank_rule_is_refused_and_nothing_is_written(rule: str) -> None:
    """An empty row in the panel is a rule the owner believes is in force and is not."""
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        response = await client.post(f"{MEMORY}/preferences", json={"rule": rule})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_rule"
    assert session.flushes == 0, "a refused request must not write"
    assert session.row is not None
    assert session.row.dna["preferences"] == [NO_EXCLAIM]


async def test_an_essay_is_refused_with_a_message_that_says_where_it_belongs() -> None:
    """Every preference is prepended to EVERY model call, so length is a per-call cost."""
    session = _OneRowSession(_business([]))
    async with _client(session) as client:
        response = await client.post(
            f"{MEMORY}/preferences", json={"rule": "x" * (MAX_RULE_LENGTH + 1)}
        )

    assert response.status_code == 422
    assert str(MAX_RULE_LENGTH) in response.json()["detail"]["message"]
    assert session.flushes == 0


async def test_the_ceiling_refuses_with_409_rather_than_silently_dropping() -> None:
    """409 not 422: the sentence is fine, the state of the list is what refuses it. A 422
    would tell the owner to fix their wording, which would not help.

    Silently dropping the newest would leave them believing a rule is in force when it
    is not -- the exact drift this whole module exists to prevent.
    """
    session = _OneRowSession(_business([f"Rule number {i}" for i in range(MAX_PREFERENCES)]))
    async with _client(session) as client:
        response = await client.post(f"{MEMORY}/preferences", json={"rule": "One rule too many"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "preference_limit"
    assert session.flushes == 0


# --------------------------------------------------------------------------- #
# Editing -- the reason this API had to be added
# --------------------------------------------------------------------------- #


async def test_editing_a_preference_keeps_its_position_in_the_list() -> None:
    """Delete-then-add would look equivalent and would move the rule to the END.

    The order is the order the owner confirmed things in, and `to_prompt_lines` emits it
    verbatim -- so fixing a typo must not reorder the instructions a model receives.
    """
    session = _OneRowSession(_business(["First rule", "Middle rule", "Last rule"]))
    async with _client(session) as client:
        response = await client.put(
            f"{MEMORY}/preferences/{preference_id('Middle rule')}",
            json={"rule": "Middle rule, reworded"},
        )

    assert response.status_code == 200
    assert [p["rule"] for p in response.json()["preferences"]] == [
        "First rule",
        "Middle rule, reworded",
        "Last rule",
    ]


async def test_an_edit_changes_the_prompt_the_next_run_receives() -> None:
    """The point of the panel, asserted at the only place it can be: the prompt lines."""
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        body = (
            await client.put(
                f"{MEMORY}/preferences/{preference_id(NO_EXCLAIM)}",
                json={"rule": "Never use exclamation marks or emoji"},
            )
        ).json()

    assert body["promptLines"] == ["Never use exclamation marks or emoji"]


async def test_editing_only_the_casing_of_a_rule_is_accepted_not_called_a_duplicate() -> None:
    """Dedup treats them as the same rule, but the owner is fixing what they SEE, and the
    stored text is what the panel shows them."""
    session = _OneRowSession(_business(["never use sie"]))
    async with _client(session) as client:
        response = await client.put(
            f"{MEMORY}/preferences/{preference_id('never use sie')}",
            json={"rule": "Never use Sie"},
        )

    assert response.status_code == 200
    assert [p["rule"] for p in response.json()["preferences"]] == ["Never use Sie"]


async def test_an_edit_that_would_merge_two_rules_is_refused_not_silently_applied() -> None:
    """Merging would remove a rule the owner never asked to remove, and the list would
    come back one line shorter than they just confirmed. That is data loss."""
    session = _OneRowSession(_business([NO_EXCLAIM, NO_SIE]))
    async with _client(session) as client:
        response = await client.put(
            f"{MEMORY}/preferences/{preference_id(NO_SIE)}",
            json={"rule": NO_EXCLAIM},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "duplicate_preference"
    assert session.row is not None
    assert session.row.dna["preferences"] == [NO_EXCLAIM, NO_SIE], "nothing was lost"


async def test_editing_a_rule_that_is_no_longer_there_is_404() -> None:
    """A panel holding an id from before another tab's delete must be told, not guess.

    An edit is not idempotent the way a delete is: silence would leave the owner unable
    to tell "saved" from "the rule was gone, so nothing was saved".
    """
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        response = await client.put(
            f"{MEMORY}/preferences/{preference_id('a rule that was deleted')}",
            json={"rule": "anything"},
        )

    assert response.status_code == 404
    assert session.flushes == 0


async def test_an_edit_to_a_blank_rule_is_refused_and_leaves_the_original() -> None:
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        response = await client.put(
            f"{MEMORY}/preferences/{preference_id(NO_EXCLAIM)}", json={"rule": "   "}
        )

    assert response.status_code == 422
    assert session.row is not None
    assert session.row.dna["preferences"] == [NO_EXCLAIM]


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #


async def test_deleting_removes_only_the_named_rule() -> None:
    session = _OneRowSession(_business([NO_EXCLAIM, NO_SIE, "Keep it short"]))
    async with _client(session) as client:
        response = await client.delete(f"{MEMORY}/preferences/{preference_id(NO_SIE)}")

    assert response.status_code == 200
    assert [p["rule"] for p in response.json()["preferences"]] == [NO_EXCLAIM, "Keep it short"]


async def test_deleting_returns_the_remaining_memory_rather_than_an_empty_body() -> None:
    """204 would make the client's optimistic removal the only account of what happened,
    and a panel could go on showing a rule that is no longer in force."""
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        body = (await client.delete(f"{MEMORY}/preferences/{preference_id(NO_EXCLAIM)}")).json()

    assert body["preferences"] == []
    assert body["promptLines"] == []
    assert body["rememberedCount"] == 0


async def test_deleting_something_that_is_already_gone_is_404_not_500() -> None:
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session) as client:
        response = await client.delete(f"{MEMORY}/preferences/{preference_id('gone')}")

    assert response.status_code == 404


async def test_the_business_removed_from_memory_leaves_the_rest_of_dna_alone() -> None:
    """A partial write must never drop a key this module does not own."""
    session = _OneRowSession(
        _business([NO_EXCLAIM], tone="professional", city="Koblenz", website="https://x.test")
    )
    async with _client(session) as client:
        await client.delete(f"{MEMORY}/preferences/{preference_id(NO_EXCLAIM)}")

    assert session.row is not None
    assert session.row.dna["city"] == "Koblenz"
    assert session.row.dna["website"] == "https://x.test"
    assert session.row.dna["tone"] == "professional"
    assert session.row.dna["name"] == "Klempner Koblenz"


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


async def test_no_session_is_401_on_every_route() -> None:
    async with _client(_NeverOpened(), authenticated=False) as client:
        assert (await client.get(MEMORY)).status_code == 401
        assert (
            await client.post(f"{MEMORY}/preferences", json={"rule": NO_SIE})
        ).status_code == 401
        assert (
            await client.put(f"{MEMORY}/preferences/abc", json={"rule": NO_SIE})
        ).status_code == 401
        assert (await client.delete(f"{MEMORY}/preferences/abc")).status_code == 401


async def test_no_business_id_is_accepted_from_the_client_on_any_route() -> None:
    """The cross-tenant attack surface is REMOVED here, not defended against.

    These routes derive the business from the session, so there is no id for a caller to
    tamper with. This pins that: an id offered in the query string, offered in the write
    body, or reached for at the id-taking path shape changes nothing. If someone later
    adds a ``business_id`` parameter to these routes, this test fails -- which is the
    point, because that parameter would then need the checked-against-session treatment
    the proposals route has.
    """
    session = _OneRowSession(_business([NO_EXCLAIM]))
    async with _client(session, business_id=BUSINESS) as client:
        read = await client.get(f"{MEMORY}?businessId={OTHER_BUSINESS}")
        assert read.status_code == 200
        assert read.json()["businessId"] == str(BUSINESS), "a query string must not re-scope"

        written = await client.post(
            f"{MEMORY}/preferences",
            json={"rule": NO_SIE, "businessId": str(OTHER_BUSINESS)},
        )
        assert written.status_code == 201
        assert written.json()["businessId"] == str(BUSINESS), "a body must not re-scope"

        # There is no id-taking variant of this resource to reach at all.
        assert (await client.get(f"/api/v1/businesses/{OTHER_BUSINESS}/memory")).status_code == 404


async def test_an_unknown_business_is_404_rather_than_an_empty_memory() -> None:
    """An empty memory is a real state; conflating it with "no such business" would let
    the panel report on a tenant that is not there."""
    async with _client(_OneRowSession(None)) as client:
        assert (await client.get(MEMORY)).status_code == 404


async def test_a_missing_rule_field_is_422_and_does_not_echo_anything_sensitive() -> None:
    async with _client(_OneRowSession(_business([]))) as client:
        response = await client.post(f"{MEMORY}/preferences", json={})

    assert response.status_code == 422
    # The app-wide handler strips the submitted value out of every validation error.
    assert "input" not in response.text
