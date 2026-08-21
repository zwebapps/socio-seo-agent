"""The sweep against a real Postgres: it reaches the column, and it cannot reach a tenant.

``tests/services/test_connection_sweep.py`` owns the decision — which rows are stale, and
that the second run writes nothing. What only a database can answer is the pair below, and
both are the reasons a sweep is the kind of code that usually reaches for a privileged
connection:

**It writes through row-level security, not around it.** The sweep is handed business ids
and asks the store for each one's connections, so every read and every write happens inside
a ``business_session`` as the restricted role. Sweeping business A therefore cannot touch
business B's row — not because an ``if`` says so, but because the policy does. A sweep that
had quietly acquired a bypass would pass every in-memory test in the suite and fail this
one.

**Not writing is observable.** ``updated_at`` is maintained by the database, so a second
run that re-wrote ``expired`` over ``expired`` would move it. The in-memory double has no
such column, which is why the idempotency claim is made twice: there it is "no call was
attempted", here it is "the timestamp did not move".

Every test drives the app-role engine. The owner is a superuser locally and a superuser
bypasses row-level security entirely, so an isolation claim asserted through it would pass
against a database with no policies at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.token_cipher import EphemeralVaultCipher, Secret
from backend.app.db.adapters.connection_store import PostgresConnectionStore
from backend.app.services.connection_service import (
    ConnectionStatus,
    sweep_expired_connections,
)
from backend.app.services.platform_oauth import TokenGrant

pytestmark = pytest.mark.db

PLATFORM = "linkedin"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(scoped_sessions: None) -> PostgresConnectionStore:
    """The real adapter, with the in-process vault standing in for AES-256-GCM.

    The cipher is pinned rather than read from the environment for the same reason
    ``test_platform_connections.py`` pins it: this file is about the status column and
    tenancy, not about which cipher a given environment selects.
    """
    return PostgresConnectionStore(cipher=EphemeralVaultCipher())


def _grant(*, expires_at: datetime | None, account: str = "urn:li:person:AbC123") -> TokenGrant:
    return TokenGrant(
        external_account_id=account,
        access_token=Secret("AQV-real-looking-linkedin-access-token-0123456789"),
        expires_at=expires_at,
        scopes=("w_member_social",),
        external_account_name="Müller Sanitär",
        fake=True,
    )


async def _row_as_owner(session: AsyncSession, business_id: UUID) -> Any:
    """The connection row read as the table owner, so RLS hides nothing from the assertion.

    A test that read the row through the app role could not tell "the sweep left it alone"
    apart from "the policy hid the change", and those are opposite outcomes.
    """
    await session.rollback()  # a fresh snapshot; the store committed on another connection
    result = await session.execute(
        text(
            "SELECT status, expires_at, updated_at FROM platform_connections WHERE business_id = :b"
        ),
        {"b": business_id},
    )
    return result.mappings().one()


async def test_a_stale_row_is_written_expired_through_the_restricted_role(
    store: PostgresConnectionStore, business_a: UUID, owner_session: AsyncSession
) -> None:
    await store.save_grant(
        business_id=business_a, platform=PLATFORM, grant=_grant(expires_at=NOW - timedelta(hours=1))
    )
    assert (await _row_as_owner(owner_session, business_a))["status"] == "connected"

    outcome = await sweep_expired_connections(store=store, business_ids=[business_a], now=NOW)

    assert len(outcome.expired) == 1
    assert (await _row_as_owner(owner_session, business_a))["status"] == "expired"


async def test_a_live_row_is_left_connected(
    store: PostgresConnectionStore, business_a: UUID, owner_session: AsyncSession
) -> None:
    await store.save_grant(
        business_id=business_a, platform=PLATFORM, grant=_grant(expires_at=NOW + timedelta(hours=1))
    )

    outcome = await sweep_expired_connections(store=store, business_ids=[business_a], now=NOW)

    assert outcome.examined == 1
    assert outcome.expired == ()
    assert (await _row_as_owner(owner_session, business_a))["status"] == "connected"


async def test_the_second_run_does_not_move_updated_at(
    store: PostgresConnectionStore, business_a: UUID, owner_session: AsyncSession
) -> None:
    """Idempotency where it is actually visible.

    `updated_at` is how an operator finds out when a connection died. A sweep that rewrote
    the same status every run would reset that to "just now", forever, and the column would
    become useless without ever being wrong in a way anybody could point at.
    """
    await store.save_grant(
        business_id=business_a, platform=PLATFORM, grant=_grant(expires_at=NOW - timedelta(hours=1))
    )

    await sweep_expired_connections(store=store, business_ids=[business_a], now=NOW)
    after_first = await _row_as_owner(owner_session, business_a)

    second = await sweep_expired_connections(store=store, business_ids=[business_a], now=NOW)
    after_second = await _row_as_owner(owner_session, business_a)

    assert second.expired == ()
    assert after_first["status"] == after_second["status"] == "expired"
    assert after_first["updated_at"] == after_second["updated_at"], (
        "the second sweep rewrote a row that already agreed with the clock"
    )


async def test_sweeping_one_business_cannot_touch_another_s_connection(
    store: PostgresConnectionStore,
    business_a: UUID,
    business_b: UUID,
    owner_session: AsyncSession,
) -> None:
    """The claim that matters. Both rows are stale; only the one asked for is written.

    If the sweep ever ran on a privileged connection -- or grew a cross-tenant query to
    "find all stale connections" in one pass -- B's row would move here. Nothing else in
    the suite would notice.
    """
    stale = _grant(expires_at=NOW - timedelta(hours=1))
    await store.save_grant(business_id=business_a, platform=PLATFORM, grant=stale)
    await store.save_grant(business_id=business_b, platform=PLATFORM, grant=stale)

    outcome = await sweep_expired_connections(store=store, business_ids=[business_a], now=NOW)

    assert [view.business_id for view in outcome.expired] == [business_a]
    assert outcome.examined == 1, "business B's row was not even read"
    assert (await _row_as_owner(owner_session, business_a))["status"] == "expired"
    assert (await _row_as_owner(owner_session, business_b))["status"] == "connected"


async def test_the_screens_are_correct_before_the_sweep_has_ever_run(
    store: PostgresConnectionStore, business_a: UUID, owner_session: AsyncSession
) -> None:
    """The invariant, asserted against real storage as well as in memory.

    This is the state of the database this project actually deploys: nothing schedules the
    sweep, so the column says `connected` on a dead credential. The read path -- the same
    `view()` the settings screen and the social actuator go through -- must already refuse.
    """
    await store.save_grant(
        business_id=business_a, platform=PLATFORM, grant=_grant(expires_at=NOW - timedelta(hours=1))
    )

    stored = await _row_as_owner(owner_session, business_a)
    view = await store.view(business_id=business_a, platform=PLATFORM)
    assert view is not None

    # The column is stale...
    assert stored["status"] == "connected"
    assert view.status is ConnectionStatus.CONNECTED
    # ...and the surface built on it is right anyway.
    reason = view.unusable_reason(now=NOW)
    assert reason is not None and "expired" in reason

    await sweep_expired_connections(store=store, business_ids=[business_a], now=NOW)

    swept = await store.view(business_id=business_a, platform=PLATFORM)
    assert swept is not None and swept.status is ConnectionStatus.EXPIRED
    assert swept.unusable_reason(now=NOW) == reason, (
        "the sweep changed what a surface sees; it is meant to change only the column"
    )
