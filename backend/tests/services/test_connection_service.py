"""The connection lifecycle: connect, expire, refresh, revoke — hermetically.

This is the whole point of the OAuth seam. No platform will let us publish until its App
Review is approved (``docs/CHANNELS.md`` §2-3), but the lifecycle around the credential is
ours, and every one of its interesting states is reachable here with no network: a token
that has aged past its expiry, a refresh that succeeds, a refresh the platform refuses, a
transient failure that must NOT be mistaken for a dead connection, and a revoke.

The store is in-memory rather than the real adapter so that this file tests the DECISIONS
and ``tests/db/test_platform_connections.py`` tests the storage. It goes through the real
:class:`EphemeralVaultCipher`, though — so "the stored envelope contains no plaintext"
is asserted on the same code the database path uses.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from backend.app.core.token_cipher import (
    EphemeralVaultCipher,
    Secret,
    credential_aad,
    mask_secret,
)
from backend.app.services.connection_service import (
    ConnectionStatus,
    ConnectionView,
    begin_connect,
    complete_connect,
    mark_expired_if_stale,
    refresh_connection,
    revoke_connection,
)
from backend.app.services.platform_oauth import (
    FakeOAuthProvider,
    OAuthError,
    OAuthProvider,
    TokenGrant,
    oauth_status,
)

PLATFORM = "linkedin"
REDIRECT_URI = "http://localhost:8100/api/connections/linkedin/callback"


@dataclass
class _Row:
    """What this store would have written to a database.

    A dataclass rather than a dict so mypy checks the shape: an in-memory double that
    types every column as ``object`` needs casts to read back, and a cast is exactly how a
    test double drifts away from the schema it is standing in for.
    """

    view: ConnectionView
    credential: str | None
    refresh: str | None


class InMemoryConnectionStore:
    """A ``ConnectionStore`` with a dict behind it, encrypting through the real cipher.

    Deliberately not a mock: it stores envelopes and returns views, so a test that reads
    a credential out of it is exercising the same "encrypt on write, decrypt on an
    explicitly-named read" shape the Postgres adapter has. A mock would have let a
    plaintext leak pass.
    """

    def __init__(self) -> None:
        self._cipher = EphemeralVaultCipher()
        self._rows: dict[tuple[UUID, str], _Row] = {}

    async def save_grant(
        self, *, business_id: UUID, platform: str, grant: TokenGrant
    ) -> ConnectionView:
        aad = credential_aad(business_id=business_id, platform=platform)
        view = ConnectionView(
            business_id=business_id,
            platform=platform,
            external_account_id=grant.external_account_id,
            external_account_name=grant.external_account_name,
            scopes=tuple(grant.scopes),
            status=ConnectionStatus.CONNECTED,
            expires_at=grant.expires_at,
            credential_hint=mask_secret(grant.access_token.reveal()),
            credential_scheme=self._cipher.scheme,
            has_credential=True,
            fake=grant.fake,
        )
        self._rows[(business_id, platform)] = _Row(
            view=view,
            credential=self._cipher.encrypt(grant.access_token, aad=aad),
            refresh=(
                self._cipher.encrypt(grant.refresh_token, aad=aad)
                if grant.refresh_token is not None
                else None
            ),
        )
        return view

    async def view(self, *, business_id: UUID, platform: str) -> ConnectionView | None:
        row = self._rows.get((business_id, platform))
        return None if row is None else row.view

    async def views(self, *, business_id: UUID) -> list[ConnectionView]:
        return [row.view for (owner, _), row in self._rows.items() if owner == business_id]

    async def reveal_access(self, *, business_id: UUID, platform: str) -> Secret | None:
        row = self._rows.get((business_id, platform))
        return None if row is None else self._open(business_id, platform, row.credential)

    async def reveal_refresh(self, *, business_id: UUID, platform: str) -> Secret | None:
        row = self._rows.get((business_id, platform))
        return None if row is None else self._open(business_id, platform, row.refresh)

    async def set_status(
        self,
        *,
        business_id: UUID,
        platform: str,
        status: ConnectionStatus,
        forget_credential: bool = False,
    ) -> ConnectionView | None:
        row = self._rows.get((business_id, platform))
        if row is None:
            return None
        row.view = replace(
            row.view,
            status=status,
            has_credential=row.view.has_credential and not forget_credential,
            credential_hint="" if forget_credential else row.view.credential_hint,
        )
        if forget_credential:
            row.credential = None
            row.refresh = None
        return row.view

    def stored_columns(self, business_id: UUID, platform: str) -> str:
        """Everything this store holds for one connection, as one string.

        For the leak assertion: "does the plaintext appear anywhere in what was
        persisted" is one question, and it should be asked of the whole row rather than
        of the column somebody remembered to check.
        """
        return repr(self._rows.get((business_id, platform)))

    def _open(self, business_id: UUID, platform: str, envelope: str | None) -> Secret | None:
        if envelope is None:
            return None
        return self._cipher.decrypt(
            envelope, aad=credential_aad(business_id=business_id, platform=platform)
        )


class MovableClock:
    """A clock a test can push forward, because token expiry is the point.

    A test that had to sleep for the fake's one-hour lifetime is a test nobody runs, and
    one that used a lifetime short enough to sleep through would be timing-dependent in
    CI.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start if start is not None else datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class RefusingProvider:
    """A provider whose refresh is refused outright, the way a removed app behaves."""

    def __init__(self, *, retryable: bool) -> None:
        self._retryable = retryable
        self.revoked = False

    @property
    def platform(self) -> str:
        return PLATFORM

    @property
    def fake(self) -> bool:
        return True

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: object) -> str:
        return "https://fake-oauth.invalid/authorize"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        raise AssertionError("this provider is only used for the refresh path")

    async def refresh(self, refresh_token: Secret) -> TokenGrant:
        raise OAuthError("the user removed the app", retryable=self._retryable)

    async def revoke(self, credential: Secret) -> None:
        self.revoked = True


def _provider(clock: MovableClock) -> FakeOAuthProvider:
    return FakeOAuthProvider(PLATFORM, clock=clock, account_id="urn:li:person:fake")


async def _connect(
    store: InMemoryConnectionStore, provider: OAuthProvider, business_id: UUID
) -> ConnectionView:
    return await complete_connect(
        store=store,
        provider=provider,
        business_id=business_id,
        code="callback-code",
        redirect_uri=REDIRECT_URI,
    )


def test_the_authorization_url_carries_an_unguessable_state_and_the_publish_scopes() -> None:
    """`state` is the only thing separating a genuine callback from an attacker's."""
    request = begin_connect(FakeOAuthProvider(PLATFORM), redirect_uri=REDIRECT_URI)

    assert request.platform == PLATFORM
    assert "w_member_social" in request.scopes
    assert len(request.state) >= 32
    assert request.state in request.url
    assert begin_connect(FakeOAuthProvider(PLATFORM), redirect_uri=REDIRECT_URI).state != (
        request.state
    )


async def test_connect_stores_a_usable_connection_and_no_plaintext() -> None:
    store = InMemoryConnectionStore()
    clock = MovableClock()
    business_id = uuid4()

    view = await _connect(store, _provider(clock), business_id)

    assert view.status is ConnectionStatus.CONNECTED
    assert view.unusable_reason(now=clock()) is None
    assert view.external_account_id == "urn:li:person:fake"
    assert view.fake is True, "a connection made against the fake must say so"

    credential = await store.reveal_access(business_id=business_id, platform=PLATFORM)
    assert credential is not None

    plaintext = credential.reveal()
    assert plaintext not in store.stored_columns(business_id, PLATFORM), (
        "the credential's plaintext was persisted"
    )
    # The read model is the other half of that guarantee: there is nowhere in a view for
    # a credential to hide.
    assert plaintext not in repr(view)
    assert view.credential_hint and plaintext not in view.credential_hint


async def test_a_connection_expires_on_the_clock_before_anything_writes_a_row() -> None:
    """The pure function is the authority; the status column is a cache of it.

    This is the case that makes the distinction matter: nothing has run, nothing has been
    written, and the credential is nevertheless unusable because an hour has passed.
    """
    store = InMemoryConnectionStore()
    clock = MovableClock()
    business_id = uuid4()

    await _connect(store, _provider(clock), business_id)
    clock.advance(timedelta(hours=2))

    view = await store.view(business_id=business_id, platform=PLATFORM)
    assert view is not None
    assert view.status is ConnectionStatus.CONNECTED, "no sweep has run yet"
    assert view.is_expired(now=clock())
    reason = view.unusable_reason(now=clock())
    assert reason is not None and "expired" in reason

    # The sweep exists only so a SQL-level read agrees with the sentence above.
    swept = await mark_expired_if_stale(
        store=store, business_id=business_id, platform=PLATFORM, now=clock()
    )
    assert swept is not None and swept.status is ConnectionStatus.EXPIRED


async def test_refresh_replaces_the_credential_and_pushes_the_expiry_out() -> None:
    store = InMemoryConnectionStore()
    clock = MovableClock()
    business_id = uuid4()
    provider = _provider(clock)

    await _connect(store, provider, business_id)
    first = await store.reveal_access(business_id=business_id, platform=PLATFORM)
    assert first is not None
    clock.advance(timedelta(hours=2))

    refreshed = await refresh_connection(
        store=store, provider=provider, business_id=business_id, platform=PLATFORM
    )

    assert refreshed is not None
    assert refreshed.status is ConnectionStatus.CONNECTED
    assert refreshed.unusable_reason(now=clock()) is None
    second = await store.reveal_access(business_id=business_id, platform=PLATFORM)
    assert second is not None
    assert second.reveal() != first.reveal(), "the row still holds the old credential"


async def test_the_full_cycle_ends_with_a_connection_that_cannot_publish() -> None:
    """connect -> expire -> refresh -> expire -> revoke, in one pass.

    Written as one test on purpose: each state is only interesting because of the one
    before it, and a suite of four isolated assertions would not have caught a refresh
    that worked once and then left the row unrenewable.
    """
    store = InMemoryConnectionStore()
    clock = MovableClock()
    business_id = uuid4()
    provider = _provider(clock)

    await _connect(store, provider, business_id)

    for _ in range(3):
        clock.advance(timedelta(hours=2))
        view = await store.view(business_id=business_id, platform=PLATFORM)
        assert view is not None and view.is_expired(now=clock())
        renewed = await refresh_connection(
            store=store, provider=provider, business_id=business_id, platform=PLATFORM
        )
        assert renewed is not None and renewed.unusable_reason(now=clock()) is None

    revoked = await revoke_connection(
        store=store, provider=provider, business_id=business_id, platform=PLATFORM
    )
    assert revoked is not None
    assert revoked.status is ConnectionStatus.REVOKED
    assert revoked.has_credential is False
    assert revoked.unusable_reason(now=clock()) is not None
    assert await store.reveal_access(business_id=business_id, platform=PLATFORM) is None

    # And a revoked connection cannot be quietly renewed back to life: the platform has
    # forgotten the credential, so a refresh must fail rather than resurrect it.
    after = await refresh_connection(
        store=store, provider=provider, business_id=business_id, platform=PLATFORM
    )
    assert after is not None and after.status is not ConnectionStatus.CONNECTED


async def test_a_refused_refresh_records_expired_rather_than_raising() -> None:
    """Learning a connection is dead is useful; the useful form of it is a row saying so."""
    store = InMemoryConnectionStore()
    clock = MovableClock()
    business_id = uuid4()

    await _connect(store, _provider(clock), business_id)
    result = await refresh_connection(
        store=store,
        provider=RefusingProvider(retryable=False),
        business_id=business_id,
        platform=PLATFORM,
    )

    assert result is not None
    assert result.status is ConnectionStatus.EXPIRED


async def test_a_retryable_refresh_failure_leaves_the_connection_alone() -> None:
    """A rate limit says nothing about whether the credential is still good.

    Writing `expired` on a 429 would ask a business to reconnect an account that is
    perfectly fine -- and they would have to do it again after the next blip.
    """
    store = InMemoryConnectionStore()
    clock = MovableClock()
    business_id = uuid4()

    await _connect(store, _provider(clock), business_id)
    result = await refresh_connection(
        store=store,
        provider=RefusingProvider(retryable=True),
        business_id=business_id,
        platform=PLATFORM,
    )

    assert result is not None
    assert result.status is ConnectionStatus.CONNECTED


async def test_revoke_tells_the_platform_before_forgetting_the_credential() -> None:
    """Order matters: forgetting first leaves a live token we can no longer revoke."""
    store = InMemoryConnectionStore()
    clock = MovableClock()
    business_id = uuid4()
    provider = RefusingProvider(retryable=False)

    await _connect(store, _provider(clock), business_id)
    result = await revoke_connection(
        store=store, provider=provider, business_id=business_id, platform=PLATFORM
    )

    assert provider.revoked is True, "the platform was never told"
    assert result is not None and result.has_credential is False


async def test_refreshing_or_revoking_an_absent_connection_is_not_an_error() -> None:
    store = InMemoryConnectionStore()
    business_id = uuid4()
    provider = _provider(MovableClock())

    assert (
        await refresh_connection(
            store=store, provider=provider, business_id=business_id, platform=PLATFORM
        )
        is None
    )
    assert (
        await revoke_connection(
            store=store, provider=provider, business_id=business_id, platform=PLATFORM
        )
        is None
    )


def test_a_credential_with_no_stated_expiry_is_not_treated_as_expired() -> None:
    """`None` means "the platform did not say", which is not the same as "never expires".

    Reading it as expired would break every long-lived-token platform; reading it as
    immortal would be a claim we cannot make. It is unusable only once the platform
    rejects it, which arrives as a status change.
    """
    view = ConnectionView(
        business_id=uuid4(),
        platform=PLATFORM,
        external_account_id="acct",
        external_account_name=None,
        scopes=(),
        status=ConnectionStatus.CONNECTED,
        expires_at=None,
        credential_hint="abcd…wxyz",
        credential_scheme="v1.ephemeral",
    )

    assert view.is_expired() is False
    assert view.needs_renewal() is False
    assert view.unusable_reason() is None


def test_a_row_marked_expired_without_a_timestamp_still_refuses_readably() -> None:
    """The formatting edge: a platform can refuse a refresh having never stated an expiry."""
    view = ConnectionView(
        business_id=uuid4(),
        platform=PLATFORM,
        external_account_id="acct",
        external_account_name=None,
        scopes=(),
        status=ConnectionStatus.EXPIRED,
        expires_at=None,
        credential_hint="abcd…wxyz",
        credential_scheme="v1.ephemeral",
    )

    reason = view.unusable_reason()
    assert reason is not None and "expired" in reason


def test_the_status_report_names_app_review_rather_than_implying_we_are_slow() -> None:
    """A screen that offers "connect Instagram" has to say what that currently does."""
    status = oauth_status()

    assert status.using_fake_providers is True
    assert status.real_providers == ()
    assert "instagram" in status.blocked_on_app_review
    assert "App Review" in status.message


def test_a_provider_cannot_be_built_for_a_platform_the_schema_would_reject() -> None:
    """The CHECK constraint on `platform_connections.platform` and this list are one list."""
    with pytest.raises(ValueError, match="unknown platform"):
        FakeOAuthProvider("myspace")
