"""Confirming a drafted Business DNA, against real Postgres.

`/preview` drafted a DNA and handed it back, and NOTHING could accept it -- so the draft
was shown to the owner and thrown away, and `businesses.dna` stayed `{}` for every
business ever created. Two consequences, neither cosmetic:

* the regulated-claim guard reads `banned_claims` from there, so a dentist's forbidden
  phrases were never enforced for a real tenant;
* HARVEST reads `dna["website"]`, so no run could crawl the site the owner had pasted in.

Tested against the real database rather than a fake session, for the reason the rest of
`tests/db` records: this writes JSONB under row-level security, and both of those are
things an in-memory double cannot get wrong on your behalf.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.db.session import business_session
from backend.app.services.onboarding_service import (
    BusinessDnaDraft,
    BusinessNotFoundError,
    save_confirmed_dna,
)

pytestmark = pytest.mark.db


@pytest.fixture
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


@pytest.fixture
async def business_a(two_businesses: tuple[UUID, UUID]) -> UUID:
    return two_businesses[0]


def _draft() -> BusinessDnaDraft:
    return BusinessDnaDraft(
        name="Müller Sanitär GmbH",
        industry="Sanitär",
        city="Koblenz",
        locale="de",
        services=["Rohrreinigung", "Notdienst"],
        audience=["Hausbesitzer in Koblenz"],
        usps=["24-Stunden-Notdienst"],
        tone="professional",
        banned_claims=["garantiert schmerzfrei", "günstigster Anbieter"],
    )


async def _dna(business_id: UUID) -> dict[str, object]:
    async with business_session(business_id) as s:
        row = (
            await s.execute(
                text("SELECT dna, website FROM businesses WHERE id = :i"), {"i": business_id}
            )
        ).one()
    return {"dna": row.dna, "website": row.website}


async def test_a_confirmed_draft_is_stored(scoped_sessions: None, business_a: UUID) -> None:
    """The gap: nothing could accept a draft, so every business had `dna = {}`."""
    async with business_session(business_a) as s:
        await save_confirmed_dna(
            business_a, dna=_draft(), source_url="https://mueller.example", session=s
        )

    stored = await _dna(business_a)
    dna = stored["dna"]
    assert isinstance(dna, dict)
    assert dna["services"] == ["Rohrreinigung", "Notdienst"]
    assert dna["city"] == "Koblenz"
    assert dna["tone"] == "professional"


async def test_the_website_is_stored_where_harvest_reads_it(
    scoped_sessions: None, business_a: UUID
) -> None:
    """THE reason the URL is worth pasting.

    `agents/nodes` checks `dna.get("website")` and, when it is absent, records the fact
    gap "website (none on record)" and crawls nothing. `BusinessDnaDraft` has no
    `website` field, so saving the draft alone would produce a business whose runs can
    never look at its own site -- the exact thing the owner asked for.
    """
    async with business_session(business_a) as s:
        await save_confirmed_dna(
            business_a, dna=_draft(), source_url="https://mueller.example", session=s
        )

    stored = await _dna(business_a)
    dna = stored["dna"]
    assert isinstance(dna, dict)
    assert dna["website"] == "https://mueller.example", "HARVEST reads this key, not the column"
    assert stored["website"] == "https://mueller.example", "and the column, so it is queryable"


async def test_the_banned_claims_reach_the_place_the_guard_reads(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The claim gate is only as real as the claims configured for the tenant.

    With `dna = {}` the regulated-claim engine had nothing to match, so a dentist's
    forbidden phrases were unenforced for every real business while passing every test.
    """
    async with business_session(business_a) as s:
        await save_confirmed_dna(
            business_a, dna=_draft(), source_url="https://mueller.example", session=s
        )

    dna = (await _dna(business_a))["dna"]
    assert isinstance(dna, dict)
    assert "garantiert schmerzfrei" in dna["banned_claims"]


async def test_confirming_does_not_delete_what_the_agent_was_taught(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The data loss this design avoids, and it would have been silent.

    `memory_service` writes remembered preferences into `dna["preferences"]`. Replacing
    the whole dict on confirm would delete every rule the owner had taught the agent --
    no error, no warning, just an agent that quietly forgets. So confirm MERGES.
    """
    from backend.app.services.memory_service import remember

    async with business_session(business_a) as s:
        await remember(business_a, rule="Never mention competitors by name.", session=s)

    async with business_session(business_a) as s:
        await save_confirmed_dna(
            business_a, dna=_draft(), source_url="https://mueller.example", session=s
        )

    dna = (await _dna(business_a))["dna"]
    assert isinstance(dna, dict)
    assert dna.get("preferences") == ["Never mention competitors by name."], (
        "a confirm must not wipe business memory"
    )
    assert dna["services"] == ["Rohrreinigung", "Notdienst"], "and it must still store the draft"


async def test_confirming_twice_replaces_the_draft_rather_than_accumulating(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Re-onboarding is a correction, not an append. A merged services list would be wrong."""
    async with business_session(business_a) as s:
        await save_confirmed_dna(
            business_a, dna=_draft(), source_url="https://old.example", session=s
        )

    revised = _draft().model_copy(update={"services": ["Badsanierung"]})
    async with business_session(business_a) as s:
        await save_confirmed_dna(
            business_a, dna=revised, source_url="https://new.example", session=s
        )

    dna = (await _dna(business_a))["dna"]
    assert isinstance(dna, dict)
    assert dna["services"] == ["Badsanierung"]
    assert dna["website"] == "https://new.example"


async def test_an_unknown_business_raises_rather_than_creating_one(
    scoped_sessions: None,
) -> None:
    """Silently creating a tenant from a write is how a bug becomes a billing question."""
    ghost = uuid4()
    with pytest.raises(BusinessNotFoundError):
        async with business_session(ghost) as s:
            await save_confirmed_dna(ghost, dna=_draft(), source_url="https://x.example", session=s)
