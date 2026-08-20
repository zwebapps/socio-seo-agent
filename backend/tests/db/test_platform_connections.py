"""``platform_connections`` against a real Postgres: isolation, and no plaintext anywhere.

Two claims, and both are about the most dangerous column in this schema — a credential
that lets us act as a customer on their own social account.

**Business B cannot reach business A's connection.** Not by a plain read, not by a
targeted ``WHERE id =``, not by an UPDATE, and not by forging A's ``business_id`` into an
insert. ``tests/db/test_tenant_isolation.py`` already derives its table list from the ORM,
so it asserts a policy EXISTS on this table automatically; what it cannot say is that the
policy is the right one for this table's columns, which is what the tests below do.

**Nothing readable is stored, and nothing readable is returned.** The row is inspected as
the owner role — a superuser locally, so it sees everything RLS would hide — and the
credential's plaintext must not appear in any column. Then the same check is made of what
the store's read path returns, because the two failures are independent: a perfectly
encrypted column is no help if ``view()`` hands back the token.

Every test connects the app-role engine, never the owner's. The owner is a superuser
locally and a superuser bypasses row-level security entirely, so an isolation test run as
one passes against a database with no policies at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.core.token_cipher import EphemeralVaultCipher, NotConfiguredCipher, Secret
from backend.app.db import session as session_module
from backend.app.db.adapters.connection_store import PostgresConnectionStore
from backend.app.services.connection_service import ConnectionStatus
from backend.app.services.platform_oauth import TokenGrant

pytestmark = pytest.mark.db

PLATFORM = "linkedin"
ACCESS_TOKEN = "AQV-real-looking-linkedin-access-token-0123456789"
REFRESH_TOKEN = "AQW-real-looking-linkedin-refresh-token-9876543210"


@pytest.fixture
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the session factory at this test's engine.

    Patching the factory rather than injecting sessions keeps the real RLS scoping under
    test instead of replacing it with a hand-rolled copy that could differ. Function
    scoped, because an asyncpg pool belongs to the loop that created it -- see this
    package's conftest.
    """
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


@pytest.fixture
def store(scoped_sessions: None) -> PostgresConnectionStore:
    """The real adapter, with the in-process vault standing in for AES-256-GCM.

    The cipher is pinned rather than read from the environment so this file tests
    STORAGE, not configuration -- `tests/core/test_token_cipher.py` owns the question of
    which cipher a given environment selects. What matters here is that whatever the
    cipher produces is what reaches the column.
    """
    return PostgresConnectionStore(cipher=EphemeralVaultCipher())


@pytest.fixture
async def business_a(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[0]


@pytest.fixture
async def business_b(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[1]


def a_grant(**over: Any) -> TokenGrant:
    base: dict[str, Any] = {
        "external_account_id": "urn:li:person:AbC123",
        "external_account_name": "Müller Sanitär",
        "access_token": Secret(ACCESS_TOKEN),
        "refresh_token": Secret(REFRESH_TOKEN),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "scopes": ("w_member_social",),
        "fake": True,
    }
    return TokenGrant(**{**base, **over})


async def _row_as_owner(session: AsyncSession, business_id: UUID) -> Any:
    """Every column of the connection row, read as the table owner.

    As the OWNER on purpose: this is the worst case for the encryption claim. If the
    plaintext is anywhere in the row, the role that bypasses RLS is the role that will
    find it -- and so would anyone holding a database dump.
    """
    result = await session.execute(
        text("SELECT * FROM platform_connections WHERE business_id = :b"), {"b": business_id}
    )
    return result.mappings().one()


async def test_the_stored_row_contains_no_credential_plaintext(
    store: PostgresConnectionStore, business_a: UUID, owner_session: AsyncSession
) -> None:
    view = await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())

    row = await _row_as_owner(owner_session, business_a)
    serialised = " ".join(str(value) for value in row.values())

    assert ACCESS_TOKEN not in serialised, "the access token was stored in the clear"
    assert REFRESH_TOKEN not in serialised, "the refresh token was stored in the clear"
    assert row["credential_encrypted"].startswith("v1.ephemeral:")
    assert row["refresh_credential_encrypted"] is not None
    assert row["credential_scheme"] == "v1.ephemeral"
    # The hint is the one credential-derived value that is stored, and it is not usable.
    assert row["credential_hint"] == "AQV-…6789"
    assert view.credential_hint == row["credential_hint"]


async def test_the_read_path_never_returns_the_credential(
    store: PostgresConnectionStore, business_a: UUID
) -> None:
    """The other half: an encrypted column is no help if `view()` hands the token back."""
    await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())

    view = await store.view(business_id=business_a, platform=PLATFORM)
    views = await store.views(business_id=business_a)

    assert view is not None
    assert ACCESS_TOKEN not in repr(view)
    assert REFRESH_TOKEN not in repr(view)
    assert ACCESS_TOKEN not in repr(views)
    assert view.status is ConnectionStatus.CONNECTED
    assert view.external_account_name == "Müller Sanitär"

    # And the deliberate path still works, or the credential would be unusable.
    revealed = await store.reveal_access(business_id=business_a, platform=PLATFORM)
    assert revealed is not None and revealed.reveal() == ACCESS_TOKEN


async def test_business_b_cannot_read_or_reveal_business_a_s_connection(
    store: PostgresConnectionStore, business_a: UUID, business_b: UUID
) -> None:
    """The whole point of the table having RLS: a publishing credential is not shareable.

    Asserted positively as well as negatively -- an unscoped or wrongly-scoped read
    returns zero rows SILENTLY here, so "B sees nothing" alone would also pass against a
    store that is simply broken.
    """
    await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())

    assert await store.view(business_id=business_a, platform=PLATFORM) is not None
    assert await store.view(business_id=business_b, platform=PLATFORM) is None
    assert await store.views(business_id=business_b) == []
    assert await store.reveal_access(business_id=business_b, platform=PLATFORM) is None
    assert await store.reveal_refresh(business_id=business_b, platform=PLATFORM) is None


async def test_business_b_cannot_target_read_update_or_delete_the_row(
    store: PostgresConnectionStore,
    app_engine: AsyncEngine,
    business_a: UUID,
    business_b: UUID,
    owner_session: AsyncSession,
) -> None:
    """SQL underneath the adapter, because the guarantee has to hold without it.

    A cross-tenant UPDATE or DELETE is a silent no-op rather than an error, which is
    exactly why the row counts are asserted -- and why the row is read back afterwards.
    """
    await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())
    row = await _row_as_owner(owner_session, business_a)
    connection_id = row["id"]

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        await s.execute(
            text("SELECT set_config('app.current_business_id', :bid, true)"),
            {"bid": str(business_b)},
        )

        targeted = (
            await s.execute(
                text("SELECT count(*) FROM platform_connections WHERE id = :id"),
                {"id": connection_id},
            )
        ).scalar_one()
        assert targeted == 0, "a targeted query bypassed the policy"

        updated = cast(
            "CursorResult[Any]",
            await s.execute(
                text(
                    "UPDATE platform_connections SET credential_encrypted = 'hijacked' "
                    "WHERE id = :id"
                ),
                {"id": connection_id},
            ),
        )
        assert updated.rowcount == 0, "a cross-tenant UPDATE matched a connection"

        deleted = cast(
            "CursorResult[Any]",
            await s.execute(
                text("DELETE FROM platform_connections WHERE id = :id"), {"id": connection_id}
            ),
        )
        assert deleted.rowcount == 0, "a cross-tenant DELETE matched a connection"

    after = await _row_as_owner(owner_session, business_a)
    assert after["credential_encrypted"] == row["credential_encrypted"]


async def test_business_b_cannot_forge_a_connection_into_business_a(
    app_engine: AsyncEngine, business_a: UUID, business_b: UUID
) -> None:
    """WITH CHECK, so B cannot plant a credential of its own choosing in A's tenancy."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    with pytest.raises(DBAPIError):
        async with factory() as s, s.begin():
            await s.execute(
                text("SELECT set_config('app.current_business_id', :bid, true)"),
                {"bid": str(business_b)},
            )
            await s.execute(
                text(
                    "INSERT INTO platform_connections "
                    "(id, business_id, platform, external_account_id, credential_encrypted) "
                    "VALUES (gen_random_uuid(), :b, 'linkedin', 'forged', 'v1.ephemeral:x')"
                ),
                {"b": business_a},
            )


async def test_an_envelope_moved_between_businesses_will_not_open(
    store: PostgresConnectionStore, business_a: UUID, business_b: UUID, owner_session: AsyncSession
) -> None:
    """Defence in depth for the day RLS is not the only thing standing there.

    The envelope is bound to its business and platform, so even a row copied by something
    holding the database — a bad migration, a restored backup merged wrongly, an operator
    with owner access — yields an unreadable credential rather than a working one.
    """
    await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())
    row = await _row_as_owner(owner_session, business_a)

    # Plant A's envelope under B, as only the owner role could.
    await owner_session.execute(
        text(
            "INSERT INTO platform_connections "
            "(id, business_id, platform, external_account_id, credential_encrypted, "
            "credential_scheme) VALUES (gen_random_uuid(), :b, :p, 'stolen', :env, :scheme)"
        ),
        {
            "b": business_b,
            "p": PLATFORM,
            "env": row["credential_encrypted"],
            "scheme": row["credential_scheme"],
        },
    )
    await owner_session.commit()

    from backend.app.core.token_cipher import CredentialUnreadableError

    with pytest.raises(CredentialUnreadableError):
        await store.reveal_access(business_id=business_b, platform=PLATFORM)


async def test_reconnecting_the_same_account_replaces_the_credential_in_place(
    store: PostgresConnectionStore, business_a: UUID, owner_session: AsyncSession
) -> None:
    """One row per account, or a publish path has to choose between three live tokens."""
    await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())
    await store.save_grant(
        business_id=business_a,
        platform=PLATFORM,
        grant=a_grant(access_token=Secret("AQV-second-authorisation-token-abcdefghij")),
    )

    count = (
        await owner_session.execute(
            text("SELECT count(*) FROM platform_connections WHERE business_id = :b"),
            {"b": business_a},
        )
    ).scalar_one()
    assert count == 1

    revealed = await store.reveal_access(business_id=business_a, platform=PLATFORM)
    assert revealed is not None
    assert revealed.reveal() == "AQV-second-authorisation-token-abcdefghij"


async def test_revoking_forgets_the_credential_rather_than_just_labelling_the_row(
    store: PostgresConnectionStore, business_a: UUID, owner_session: AsyncSession
) -> None:
    """A disconnect that left a decryptable token behind would not be a disconnect."""
    await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())

    view = await store.set_status(
        business_id=business_a,
        platform=PLATFORM,
        status=ConnectionStatus.REVOKED,
        forget_credential=True,
    )

    assert view is not None
    assert view.status is ConnectionStatus.REVOKED
    assert view.has_credential is False
    assert await store.reveal_access(business_id=business_a, platform=PLATFORM) is None

    row = await _row_as_owner(owner_session, business_a)
    assert row["credential_encrypted"] is None
    assert row["refresh_credential_encrypted"] is None
    assert row["credential_hint"] == ""


async def test_the_database_refuses_a_platform_it_could_never_publish_to(
    app_engine: AsyncEngine, business_a: UUID
) -> None:
    """The CHECK constraint. A typo'd platform is a connection nobody can ever use."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    with pytest.raises(DBAPIError):
        async with factory() as s, s.begin():
            await s.execute(
                text("SELECT set_config('app.current_business_id', :bid, true)"),
                {"bid": str(business_a)},
            )
            await s.execute(
                text(
                    "INSERT INTO platform_connections "
                    "(id, business_id, platform, external_account_id) "
                    "VALUES (gen_random_uuid(), :b, 'linkedn', 'typo')"
                ),
                {"b": business_a},
            )


async def test_no_row_is_written_when_there_is_nowhere_safe_to_put_the_credential(
    scoped_sessions: None, business_a: UUID, owner_session: AsyncSession
) -> None:
    """The unconfigured-cipher path, end to end: the write fails and the table stays empty.

    This is the behaviour that makes "we could not add the `cryptography` dependency"
    survivable rather than dangerous. The alternative -- store it in the clear for now --
    leaves every credential taken before the cleanup readable forever, in every backup
    made meanwhile.
    """
    from backend.app.core.token_cipher import CipherNotConfiguredError

    store = PostgresConnectionStore(cipher=NotConfiguredCipher("no key in this environment"))

    with pytest.raises(CipherNotConfiguredError):
        await store.save_grant(business_id=business_a, platform=PLATFORM, grant=a_grant())

    count = (
        await owner_session.execute(
            text("SELECT count(*) FROM platform_connections WHERE business_id = :b"),
            {"b": business_a},
        )
    ).scalar_one()
    assert count == 0, "a row was written without a protected credential"


async def test_the_isolation_suite_actually_covers_this_table() -> None:
    """Guard the guard.

    ``test_tenant_isolation.py`` derives its list of business-scoped tables from the ORM,
    which is what makes a new table without a policy a build failure rather than a quiet
    leak. That derivation is only protective if this table is in it, so that is asserted
    here instead of assumed.
    """
    from backend.tests.db.test_tenant_isolation import BUSINESS_SCOPED_TABLES

    assert "platform_connections" in BUSINESS_SCOPED_TABLES
