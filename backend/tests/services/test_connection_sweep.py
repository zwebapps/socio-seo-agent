"""The reconciliation sweep: it makes the stored column agree, and nothing depends on it.

``status`` on ``platform_connections`` is a cache of a fact the clock changes without
anybody writing a row. Every surface folds that clock on read, through
:meth:`ConnectionView.unusable_reason` — so the interesting claims here are not "does the
sweep work" alone but the pair that makes it safe to ship with no scheduler behind it:

* the sweep writes ``expired`` exactly on the rows that disagree with the clock, once;
* **and every screen is already right on a database it has never touched.**

That second one has its own test at the bottom of this file, deliberately, because it is
the invariant that would be silently lost by somebody "optimising" a screen to read the
status column directly — at which point the product would start depending on a cron job
this project does not have.

The store is the in-memory double from ``test_connection_service.py`` rather than the real
adapter: this file tests the DECISION, and ``tests/db/test_connection_sweep.py`` tests
that the decision reaches Postgres through row-level security.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.app.core.token_cipher import Secret
from backend.app.services.connection_service import (
    ConnectionStatus,
    ConnectionView,
    sweep_expired_connections,
)
from backend.app.services.platform_oauth import TokenGrant

from .test_connection_service import (
    PLATFORM,
    InMemoryConnectionStore,
    MovableClock,
    _connect,
    _provider,
)


@dataclass
class _Write:
    """One ``set_status`` call, recorded so idempotency is provable rather than plausible."""

    business_id: UUID
    platform: str
    status: ConnectionStatus


class CountingStore(InMemoryConnectionStore):
    """The in-memory store, with every status write recorded.

    A subclass rather than a mock: "the second run wrote nothing" is a claim about calls,
    and the only way to assert it without inspecting calls is to assert on ``updated_at``
    — which an in-memory double does not have. The database test asserts that half; this
    one asserts that no write was even attempted, which is the stronger statement and the
    one that catches a sweep that writes the same value back.
    """

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[_Write] = []

    async def set_status(
        self,
        *,
        business_id: UUID,
        platform: str,
        status: ConnectionStatus,
        forget_credential: bool = False,
    ) -> ConnectionView | None:
        self.writes.append(_Write(business_id, platform, status))
        return await super().set_status(
            business_id=business_id,
            platform=platform,
            status=status,
            forget_credential=forget_credential,
        )


async def _connected_business(store: InMemoryConnectionStore, clock: MovableClock) -> UUID:
    """A business holding one live connection, made through the real connect path."""
    business_id = uuid4()
    await _connect(store, _provider(clock), business_id)
    return business_id


async def test_a_connection_the_clock_has_passed_is_written_expired() -> None:
    store = CountingStore()
    clock = MovableClock()
    business_id = await _connected_business(store, clock)

    # Past the fake provider's one-hour lifetime, so the stored `connected` is now a lie
    # that only the status column still tells.
    clock.advance(timedelta(hours=2))

    outcome = await sweep_expired_connections(store=store, business_ids=[business_id], now=clock())

    assert outcome.examined == 1
    assert len(outcome.expired) == 1
    written = outcome.expired[0]
    assert written.status is ConnectionStatus.EXPIRED
    assert written.platform == PLATFORM
    assert written.business_id == business_id

    stored = await store.view(business_id=business_id, platform=PLATFORM)
    assert stored is not None and stored.status is ConnectionStatus.EXPIRED
    assert store.writes == [_Write(business_id, PLATFORM, ConnectionStatus.EXPIRED)]


async def test_a_live_connection_is_examined_and_left_alone() -> None:
    """The row is fine, so touching it would be a lie in the other direction."""
    store = CountingStore()
    clock = MovableClock()
    business_id = await _connected_business(store, clock)

    clock.advance(timedelta(minutes=1))

    outcome = await sweep_expired_connections(store=store, business_ids=[business_id], now=clock())

    assert outcome.examined == 1
    assert outcome.expired == ()
    assert store.writes == [], "a live connection must not be written at all"
    stored = await store.view(business_id=business_id, platform=PLATFORM)
    assert stored is not None and stored.status is ConnectionStatus.CONNECTED


async def test_running_it_twice_writes_nothing_the_second_time() -> None:
    """Idempotent, and by not writing rather than by writing the same value.

    A sweep that re-wrote `expired` over `expired` would look correct and would churn
    `updated_at` on every run -- destroying the one column that says when a connection
    actually died.
    """
    store = CountingStore()
    clock = MovableClock()
    business_id = await _connected_business(store, clock)
    clock.advance(timedelta(hours=2))

    first = await sweep_expired_connections(store=store, business_ids=[business_id], now=clock())
    writes_after_first = list(store.writes)

    second = await sweep_expired_connections(store=store, business_ids=[business_id], now=clock())

    assert len(first.expired) == 1
    assert second.examined == 1, "the row is still read -- it is the write that stops"
    assert second.expired == ()
    assert store.writes == writes_after_first


async def test_a_revoked_connection_is_never_rewritten_as_expired() -> None:
    """Revoked is terminal and stronger. Downgrading it invites a pointless renewal."""
    store = CountingStore()
    clock = MovableClock()
    business_id = await _connected_business(store, clock)
    await store.set_status(
        business_id=business_id,
        platform=PLATFORM,
        status=ConnectionStatus.REVOKED,
        forget_credential=True,
    )
    store.writes.clear()
    clock.advance(timedelta(hours=2))

    outcome = await sweep_expired_connections(store=store, business_ids=[business_id], now=clock())

    assert outcome.expired == ()
    assert store.writes == []
    stored = await store.view(business_id=business_id, platform=PLATFORM)
    assert stored is not None and stored.status is ConnectionStatus.REVOKED


async def test_a_connection_with_no_stated_expiry_is_never_swept() -> None:
    """`expires_at is None` means the platform did not say, not "already dead".

    Only the platform rejecting such a credential can move it, and the sweep cannot ask a
    platform anything -- so writing `expired` here would be inventing a fact.
    """
    store = CountingStore()
    clock = MovableClock()
    business_id = uuid4()
    await store.save_grant(
        business_id=business_id,
        platform=PLATFORM,
        grant=TokenGrant(
            external_account_id="urn:li:person:fake",
            access_token=Secret("no-stated-expiry-token"),
            expires_at=None,
            scopes=("w_member_social",),
            fake=True,
        ),
    )
    clock.advance(timedelta(days=365))

    outcome = await sweep_expired_connections(store=store, business_ids=[business_id], now=clock())

    assert outcome.examined == 1
    assert outcome.expired == ()
    assert store.writes == []


async def test_only_the_businesses_handed_in_are_swept() -> None:
    """Tenancy in, tenancy out.

    The sweep's whole reach is the ids it was given -- there is no query here that could
    widen to another tenant. `tests/db/test_connection_sweep.py` proves the same thing
    against real row-level security, where a wrong answer would be a cross-tenant write.
    """
    store = CountingStore()
    clock = MovableClock()
    mine = await _connected_business(store, clock)
    theirs = await _connected_business(store, clock)
    clock.advance(timedelta(hours=2))

    outcome = await sweep_expired_connections(store=store, business_ids=[mine], now=clock())

    assert [view.business_id for view in outcome.expired] == [mine]
    assert outcome.examined == 1, "the other business's row was not even read"
    assert [write.business_id for write in store.writes] == [mine]

    untouched = await store.view(business_id=theirs, platform=PLATFORM)
    assert untouched is not None and untouched.status is ConnectionStatus.CONNECTED


async def test_a_business_with_no_connections_contributes_nothing() -> None:
    store = CountingStore()
    outcome = await sweep_expired_connections(
        store=store, business_ids=[uuid4(), uuid4()], now=datetime.now(UTC)
    )
    assert outcome == type(outcome)(examined=0, expired=())
    assert store.writes == []


async def test_every_surface_is_correct_on_a_database_the_sweep_has_never_run_against() -> None:
    """THE INVARIANT. The sweep is reconciliation, never a dependency.

    A connection past its expiry with the stored column still saying `connected` is exactly
    the state a database has when nothing schedules the sweep -- which is this project's
    actual deployment. In that state:

    * `unusable_reason` already refuses, which is the sentence the settings screen renders
      AND the one the social actuator reads before it declines to publish, so neither can
      be fooled by the stale column;
    * the stale column is genuinely stale, which is the *only* damage -- and it is damage
      to a SQL-level report, not to a customer;
    * the sweep changes nothing about what any of them see. It moves the column to agree
      with the sentence; the sentence was already right and is unchanged afterwards.

    If this test ever has to be relaxed, something started reading `status` directly and
    the product acquired a dependency on a cron job that does not exist.
    """
    store = CountingStore()
    clock = MovableClock()
    business_id = await _connected_business(store, clock)
    clock.advance(timedelta(hours=2))

    unswept = await store.view(business_id=business_id, platform=PLATFORM)
    assert unswept is not None

    # The row is wrong...
    assert unswept.status is ConnectionStatus.CONNECTED
    # ...and every surface is right anyway.
    reason_before = unswept.unusable_reason(now=clock())
    assert reason_before is not None and "expired" in reason_before
    assert unswept.is_expired(now=clock())
    assert unswept.needs_renewal(now=clock())

    await sweep_expired_connections(store=store, business_ids=[business_id], now=clock())

    swept = await store.view(business_id=business_id, platform=PLATFORM)
    assert swept is not None and swept.status is ConnectionStatus.EXPIRED
    # The refusal is identical before and after: the sweep added no information the
    # surfaces did not already have.
    assert swept.unusable_reason(now=clock()) == reason_before


def test_status_is_stale_is_the_single_write_decision() -> None:
    """The predicate itself, at the boundaries, with no store in the way.

    Asserted directly because both writers -- `mark_expired_if_stale` and the sweep --
    defer to it, so this is the one place a wrong comparison could hide.
    """
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    live = ConnectionView(
        business_id=uuid4(),
        platform=PLATFORM,
        external_account_id="urn:li:person:fake",
        external_account_name=None,
        scopes=("w_member_social",),
        status=ConnectionStatus.CONNECTED,
        expires_at=now + timedelta(minutes=30),
        credential_hint="AQV…6789",
        credential_scheme="ephemeral",
    )

    assert not live.status_is_stale(now=now)
    # Exactly at the expiry is expired: `is_expired` is `<=`, and a token whose lifetime
    # ends this instant is not one to publish with.
    assert replace(live, expires_at=now).status_is_stale(now=now)
    assert replace(live, expires_at=now - timedelta(seconds=1)).status_is_stale(now=now)
    assert not replace(live, expires_at=None).status_is_stale(now=now)

    dead = replace(live, expires_at=now - timedelta(hours=1))
    assert dead.status_is_stale(now=now)
    for already in (ConnectionStatus.EXPIRED, ConnectionStatus.REVOKED):
        assert not replace(dead, status=already).status_is_stale(now=now)
