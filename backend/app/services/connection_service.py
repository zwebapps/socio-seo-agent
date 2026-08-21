"""Connecting a platform: the lifecycle, and the read model that never carries a token.

This is the half of "connecting platforms" that does not depend on anybody's approval
queue. Publishing to Facebook, Instagram, LinkedIn or TikTok is gated on per-platform App
Review (``docs/CHANNELS.md`` §2-3, and see ``platform_oauth`` for why no real client is
written yet) — but *storing a credential safely*, *knowing whether it is still usable*,
and *renewing it before it dies* are ours to get right, and everything downstream waits
on them.

Three rules shape the module.

**A read never yields a credential.** :class:`ConnectionView` has no field that could
hold one: it carries a masked hint and the scheme that wrote the envelope, and that is
all a screen, an API response or a log line ever needs. Getting the plaintext is a
separate, differently-named call on the store (``reveal_access``), so "who can read a
token" is answerable with grep instead of with a review.

**The clock decides usability, not the status column.** ``status`` is a cache of a fact
whose truth changes without anybody writing a row: a token with ``expires_at`` five
minutes ago is expired whether or not a sweep has noticed. So
:meth:`ConnectionView.unusable_reason` is the authority every surface asks — the actuator
included — and :func:`mark_expired_if_stale` / :func:`sweep_expired_connections` exist
only so SQL-level reads agree with it. If those two ever disagree the pure function wins,
which is the same discipline the rest of this codebase applies to derived state.

The consequence is the invariant that makes the sweep safe to ship with no scheduler
behind it: **every screen, API response and publish refusal is already correct on a
database the sweep has never touched**, because each of them folds the clock on read. The
sweep is reconciliation for reports and for the operator's eye — it is a convenience, not
a dependency, and nothing may be written that makes a surface depend on it having run.
``tests/services/test_connection_sweep.py`` asserts exactly that.

**A refusal to store is the correct outcome when there is no cipher.** With no
``PLATFORM_CREDENTIAL_KEY`` the cipher refuses (``core/token_cipher``), so
:func:`complete_connect` fails and no row is written. The alternative — write the token
in the clear and tidy it up later — leaves every credential taken before the cleanup
readable forever, in every backup made meanwhile.

That refusal is about *storing*, and it does not generalise to reading. A credential we
cannot open is one we cannot use either, so it must never block the customer from
disconnecting: :func:`revoke_connection` handles the unreadable case itself and records
that the platform was not told, because the alternative is an account nobody can
disconnect for as long as the key stays rotated.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from backend.app.core.token_cipher import Secret, TokenCipherError
from backend.app.services.platform_oauth import (
    CONNECTABLE_PLATFORMS,
    PLATFORM_SCOPES,
    OAuthError,
    OAuthProvider,
    TokenGrant,
)

logger: Final = logging.getLogger(__name__)

__all__ = [
    "AuthorizationRequest",
    "ConnectionStatus",
    "ConnectionStore",
    "ConnectionView",
    "SweepOutcome",
    "begin_connect",
    "complete_connect",
    "mark_expired_if_stale",
    "refresh_connection",
    "revoke_connection",
    "sweep_expired_connections",
]

#: How long before the stated expiry a credential is treated as due for renewal. A token
#: that expires in forty seconds is not worth publishing with: the request would be built,
#: sent, and refused, and the post would be lost to a race we could have avoided by
#: renewing first.
RENEW_BEFORE: Final = 300  # seconds

#: Entropy for the OAuth ``state`` parameter. 32 bytes because this value is the only
#: thing standing between a genuine callback and an attacker's — a guessable state is a
#: connection made to somebody else's account.
_STATE_BYTES: Final = 32


class ConnectionStatus(StrEnum):
    """The stored lifecycle state of a connection.

    Mirrors the CHECK constraint on ``platform_connections.status`` deliberately: a value
    this code can produce and the database will reject is a write that fails at 3am, so
    the two lists are the same list.
    """

    CONNECTED = "connected"
    #: The credential passed its expiry and no refresh has succeeded. Recoverable — a
    #: refresh may still work, which is why it is not the same thing as revoked.
    EXPIRED = "expired"
    #: Withdrawn, here or at the platform. Terminal: the credential is forgotten and the
    #: business has to authorise again.
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class ConnectionView:
    """Everything about a connection except the credential.

    Safe to return from an API, render on a screen, and put in a log. There is no field
    here that could hold a token, which is the property that makes that true by
    construction rather than by care.
    """

    business_id: UUID
    platform: str
    external_account_id: str
    external_account_name: str | None
    scopes: tuple[str, ...]
    status: ConnectionStatus
    expires_at: datetime | None
    #: A prefix/suffix of the credential — enough for a human to match this row against a
    #: token they are holding, not enough to use.
    credential_hint: str
    #: Which cipher wrote the envelope. Queryable, so a key rotation can find the rows it
    #: still has to re-encrypt.
    credential_scheme: str
    #: Whether a credential is stored at all. False after a revoke, which wipes it.
    has_credential: bool = True
    #: True when the grant came from the fake OAuth provider. Carried all the way to the
    #: screen, because a connection that was never made to a real platform must not look
    #: like one that was.
    fake: bool = False

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """Whether the clock has passed the stated expiry.

        ``expires_at is None`` means "no expiry known", which is not the same as "never
        expires" — some platforms simply do not say. Such a credential cannot be expired
        by the clock; only the platform rejecting it can move it.
        """
        if self.expires_at is None:
            return False
        moment = now if now is not None else datetime.now(UTC)
        return _as_utc(self.expires_at) <= moment

    def needs_renewal(self, *, now: datetime | None = None) -> bool:
        """Whether it is expired or close enough that publishing with it is a race."""
        if self.expires_at is None:
            return False
        moment = now if now is not None else datetime.now(UTC)
        return (_as_utc(self.expires_at) - moment).total_seconds() <= RENEW_BEFORE

    def status_is_stale(self, *, now: datetime | None = None) -> bool:
        """Whether the stored ``status`` column now disagrees with the clock.

        The write decision, and the only one — :func:`mark_expired_if_stale` and
        :func:`sweep_expired_connections` both ask this, so a single connection swept by
        hand and a thousand swept by the script cannot reach different conclusions about
        the same row.

        Deliberately narrow in two directions, and both are what makes the sweep
        idempotent rather than merely repeatable:

        * only ``connected`` is a candidate. A row already saying ``expired`` is already
          in agreement and must not be rewritten — a sweep that touched it would churn
          ``updated_at`` on every run, and ``updated_at`` is what an operator reads to
          find out when a connection actually died.
        * ``revoked`` is never a candidate. It is terminal and stronger than expired:
          overwriting it would say a credential we have already forgotten is merely stale,
          and invite somebody to try renewing it.

        This is not a second authority on usability. :meth:`unusable_reason` remains the
        one every surface asks; this answers the narrower question of whether the cached
        column needs a write to catch up with it.
        """
        return self.status is ConnectionStatus.CONNECTED and self.is_expired(now=now)

    def unusable_reason(self, *, now: datetime | None = None) -> str | None:
        """Why nothing may be published on this connection, or ``None`` if it may.

        One function, every caller — the actuator's refusal message, the dashboard's
        warning badge and the refresh sweep all read the same sentence, so they cannot
        disagree about whether a business is connected.
        """
        if self.status is ConnectionStatus.REVOKED:
            return (
                f"the {self.platform} connection was revoked; the business has to "
                "authorise it again"
            )
        if not self.has_credential:
            return f"the {self.platform} connection holds no credential"
        if self.status is ConnectionStatus.EXPIRED or self.is_expired(now=now):
            # `expires_at` can legitimately be None on a row already marked expired -- a
            # platform that refused a refresh without ever stating an expiry. Formatting
            # None would turn a refusal into a TypeError, so the sentence adapts.
            when = (
                f" at {_as_utc(self.expires_at):%Y-%m-%d %H:%M} UTC"
                if self.expires_at is not None
                else ""
            )
            return f"the {self.platform} credential expired{when} and has not been renewed"
        return None


class ConnectionStore(Protocol):
    """Persistence for ``platform_connections``.

    A protocol, so the lifecycle below is testable without a database — and so the
    actuator can be handed something narrower than the real store.

    Note the split that matters: every read returns a :class:`ConnectionView`, and the
    plaintext is only available through the two ``reveal_*`` methods. That is not a
    convenience; it is the reason a credential cannot leak into a response by somebody
    returning the wrong object.
    """

    async def save_grant(
        self, *, business_id: UUID, platform: str, grant: TokenGrant
    ) -> ConnectionView: ...

    async def view(self, *, business_id: UUID, platform: str) -> ConnectionView | None: ...

    async def views(self, *, business_id: UUID) -> list[ConnectionView]: ...

    async def reveal_access(self, *, business_id: UUID, platform: str) -> Secret | None: ...

    async def reveal_refresh(self, *, business_id: UUID, platform: str) -> Secret | None: ...

    async def set_status(
        self,
        *,
        business_id: UUID,
        platform: str,
        status: ConnectionStatus,
        forget_credential: bool = False,
    ) -> ConnectionView | None: ...


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Where to send the human, and the ``state`` the callback must echo back.

    ``state`` is returned to the caller rather than stored here because verifying it is a
    session concern: the value has to be held wherever the browser's session is, and this
    module has neither a session nor a request. Two places deciding whether a callback is
    genuine is one place too many.
    """

    url: str
    state: str
    platform: str
    scopes: tuple[str, ...]


def begin_connect(
    provider: OAuthProvider,
    *,
    redirect_uri: str,
    scopes: Sequence[str] | None = None,
) -> AuthorizationRequest:
    """Build the authorization URL for one platform. Pure: nothing is called or stored.

    ``scopes`` defaults to :data:`platform_oauth.PLATFORM_SCOPES`, so a caller cannot
    accidentally request a narrower set than publishing needs and discover it weeks later
    as a permissions error on a post.
    """
    platform = provider.platform
    if platform not in CONNECTABLE_PLATFORMS:
        raise ValueError(f"{platform!r} is not a connectable platform")

    requested = tuple(scopes) if scopes is not None else PLATFORM_SCOPES[platform]
    state = secrets.token_urlsafe(_STATE_BYTES)
    return AuthorizationRequest(
        url=provider.authorization_url(redirect_uri=redirect_uri, state=state, scopes=requested),
        state=state,
        platform=platform,
        scopes=requested,
    )


async def complete_connect(
    *,
    store: ConnectionStore,
    provider: OAuthProvider,
    business_id: UUID,
    code: str,
    redirect_uri: str,
) -> ConnectionView:
    """Exchange the callback code and persist the connection.

    Raises :class:`platform_oauth.OAuthError` if the exchange fails and
    ``core.token_cipher.CipherNotConfiguredError`` if there is nowhere safe to put the
    result. Neither is caught here: both mean no connection exists, and inventing a row
    that says otherwise is how a dashboard ends up claiming a business is connected to an
    account it never authorised.
    """
    grant = await provider.exchange_code(code=code, redirect_uri=redirect_uri)
    view = await store.save_grant(business_id=business_id, platform=provider.platform, grant=grant)
    logger.info(
        "platform connected: business=%s platform=%s account=%s scopes=%s fake=%s",
        business_id,
        view.platform,
        view.external_account_id,
        ",".join(view.scopes),
        view.fake,
    )
    return view


async def refresh_connection(
    *,
    store: ConnectionStore,
    provider: OAuthProvider,
    business_id: UUID,
    platform: str,
) -> ConnectionView | None:
    """Renew a connection's credential. Returns the resulting view, or ``None`` if absent.

    Failure is recorded, not raised: a platform that refuses the refresh has told us the
    connection is dead, and the useful outcome of learning that is a row saying
    ``expired`` — which is what makes the dashboard able to ask the business to reconnect.
    Raising instead would leave the row claiming ``connected`` and the next publish
    discovering it the hard way.
    """
    current = await store.view(business_id=business_id, platform=platform)
    if current is None:
        return None

    refresh_token = await store.reveal_refresh(business_id=business_id, platform=platform)
    if refresh_token is None:
        logger.info(
            "cannot refresh: business=%s platform=%s has no refresh credential",
            business_id,
            platform,
        )
        return await store.set_status(
            business_id=business_id, platform=platform, status=ConnectionStatus.EXPIRED
        )

    try:
        grant = await provider.refresh(refresh_token)
    except OAuthError as exc:
        # WARNING, not exception(): a refused refresh is the platform working as designed
        # (the user removed our app), and a stack trace per dead connection is how a log
        # becomes unreadable.
        logger.warning(
            "refresh refused: business=%s platform=%s retryable=%s error=%s",
            business_id,
            platform,
            exc.retryable,
            exc,
        )
        if exc.retryable:
            # Leave the row alone: a rate limit or a 502 says nothing about whether the
            # credential is still good, and writing `expired` on one would ask the
            # business to reconnect an account that is fine.
            return current
        return await store.set_status(
            business_id=business_id, platform=platform, status=ConnectionStatus.EXPIRED
        )

    return await store.save_grant(business_id=business_id, platform=platform, grant=grant)


async def revoke_connection(
    *,
    store: ConnectionStore,
    provider: OAuthProvider,
    business_id: UUID,
    platform: str,
) -> ConnectionView | None:
    """Disconnect: tell the platform if we can, then forget the credential.

    In that order, and the order is the whole point. Wiping our copy first would leave a
    live token on the platform that we can no longer revoke — the credential outlives the
    disconnect the customer just asked for. Conversely nothing that goes wrong upstream
    may stop us forgetting: the customer asked to disconnect, and a token we cannot revoke
    is a token we should at least stop being able to use.

    There are two ways "tell the platform" does not happen, and neither is allowed to
    become a refusal to disconnect:

    * the provider rejects the revoke — the platform has our request and answered it;
    * **the credential cannot be read at all** — a rotated key, or the ephemeral vault
      after ANY restart, which is the documented local configuration. Then there is
      nothing to send, because the only thing we could send is a token we do not have.

    Both end in the same place — ``revoked``, credential wiped — and both are logged as a
    WARNING naming the platform as *not told*. That log line is the whole record: the
    status column deliberately does not distinguish (see :class:`ConnectionStatus`, "here
    or at the platform"), so a local-only revocation that logged nothing would be
    indistinguishable from one that reached the platform, and the difference is exactly
    what an operator chasing a live token needs. Raising instead — which is what reading
    the credential unguarded does — would make the one request whose entire content is
    "stop being able to act as me" a 500 the customer can never get past. The decision
    lives here rather than at each caller so that every caller gets it right; the
    ``TokenCipherError`` catch in ``api/connections.py`` folded into this.
    """
    try:
        credential = await store.reveal_access(business_id=business_id, platform=platform)
    except TokenCipherError as exc:
        # The cipher's own sentence is carried through rather than summarised: it names
        # which failure this is and what to do about it, and the two causes have different
        # fixes even though they have the same consequence here. Caught on the base class
        # for that reason -- `CipherNotConfiguredError` and `CredentialUnreadableError`
        # both mean "there is no token to send".
        logger.warning(
            "the stored credential could not be read, so the platform was NOT told and "
            "the connection is revoked locally only: business=%s platform=%s reason=%s",
            business_id,
            platform,
            exc,
        )
        credential = None

    if credential is not None:
        try:
            await provider.revoke(credential)
        except OAuthError as exc:
            # Same sentence shape as the unreadable-credential branch above, on purpose:
            # both record the same fact — the platform did not forget this credential —
            # and two phrasings for one fact is two things to grep for.
            logger.warning(
                "the platform refused the revoke, so the connection is revoked locally "
                "only: business=%s platform=%s error=%s",
                business_id,
                platform,
                exc,
            )

    return await store.set_status(
        business_id=business_id,
        platform=platform,
        status=ConnectionStatus.REVOKED,
        forget_credential=True,
    )


async def mark_expired_if_stale(
    *,
    store: ConnectionStore,
    business_id: UUID,
    platform: str,
    now: datetime | None = None,
) -> ConnectionView | None:
    """Write ``expired`` for a connection the clock has already passed.

    Purely so a SQL-level read agrees with :meth:`ConnectionView.unusable_reason`. Every
    surface is already correct without this — the pure function folds the clock — so this
    never has to run for the product to behave; it runs so a report does not disagree with
    a screen.

    One connection. :func:`sweep_expired_connections` is the same decision applied to
    every connection a set of businesses holds, and both defer to
    :meth:`ConnectionView.status_is_stale` so they cannot drift apart.
    """
    current = await store.view(business_id=business_id, platform=platform)
    if current is None:
        return None
    if not current.status_is_stale(now=now):
        return current
    return await store.set_status(
        business_id=business_id, platform=platform, status=ConnectionStatus.EXPIRED
    )


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """What one sweep looked at and what it changed.

    Carries :class:`ConnectionView` objects rather than ids because the caller wants to
    print which connection died and when — and a view is the one shape that can be logged
    without checking first whether it holds a credential.
    """

    #: Connections read, across every business swept. The denominator.
    examined: int
    #: The rows written, as they now read. Empty on an idempotent second run, which is the
    #: normal steady state and not a sign that anything failed.
    expired: tuple[ConnectionView, ...]


async def sweep_expired_connections(
    *,
    store: ConnectionStore,
    business_ids: Iterable[UUID],
    now: datetime | None = None,
) -> SweepOutcome:
    """Write ``expired`` on every connection the clock has already passed.

    A sweep rather than an endpoint, deliberately. The row has to converge whether or not
    anybody asks, and a route only converges the connections some client remembered to
    name — which is the same shape of bug as the status column itself: state that is right
    only when somebody thinks to look.

    **This must never become load-bearing.** Every surface folds the clock on read
    (:meth:`ConnectionView.unusable_reason`), so a database this has never run against
    still refuses the right publishes and still shows the right badges. What it fixes is
    narrower and real: a ``SELECT status FROM platform_connections`` — a report, a support
    query, a dashboard built in SQL rather than through the API — otherwise reads
    ``connected`` on a connection every line of application code already treats as dead.
    If a screen ever starts *needing* this to have run, the screen is the bug.

    **Tenancy is the store's, not this function's.** It iterates business ids and asks the
    store for each one's connections, so the real adapter opens one ``business_session``
    per business and row-level security decides what is visible — the same path the
    settings screen reads through. There is no privileged connection here and no
    cross-tenant query: ids come in, and the only rows touched are the ones a tenant-scoped
    read returned. A business id that has no connections, or that does not exist, simply
    contributes nothing.

    **No network.** Expiry is a clock comparison against the stored ``expires_at``; nothing
    here asks a platform anything. Renewing a credential is :func:`refresh_connection`,
    which is a different job with a different failure mode — a rate-limited refresh must
    leave the row alone, and folding that into a bulk sweep would either swallow the
    distinction or let one platform's outage stall the reconciliation of every other.
    """
    examined = 0
    expired: list[ConnectionView] = []

    for business_id in business_ids:
        for view in await store.views(business_id=business_id):
            examined += 1
            if not view.status_is_stale(now=now):
                continue
            written = await store.set_status(
                business_id=business_id,
                platform=view.platform,
                status=ConnectionStatus.EXPIRED,
            )
            if written is None:
                # The row was read a moment ago and is gone now -- a concurrent revoke
                # that deleted it, or a store that disagrees with its own read. Nothing to
                # report and nothing to fix: the next sweep sees whatever is there now.
                continue
            logger.info(
                "connection marked expired: business=%s platform=%s expires_at=%s",
                business_id,
                view.platform,
                view.expires_at.isoformat() if view.expires_at is not None else "unknown",
            )
            expired.append(written)

    return SweepOutcome(examined=examined, expired=tuple(expired))


def _as_utc(moment: datetime) -> datetime:
    """Read a naive timestamp as UTC, never as local time.

    ``expires_at`` is ``timestamptz`` so asyncpg returns an aware value, but a naive one
    can still arrive from a test or a fixture. Guessing local time would move every
    expiry by the server's offset — in one direction that publishes with a dead token, in
    the other it declares a live connection broken.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
