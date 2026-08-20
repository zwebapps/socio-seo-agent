"""``platform_connections``: the only place a credential is encrypted, and decrypted.

Every read on this class returns a :class:`~backend.app.services.connection_service
.ConnectionView`, which has no field that could hold a credential. The plaintext is
reachable only through :meth:`reveal_access` and :meth:`reveal_refresh`, named that way
on purpose: "which code can read a customer's access token" should be answerable with
``grep -rn reveal_`` rather than by reading every call site.

Encryption lives HERE rather than in the service above, so that the plaintext cannot
reach SQL even by mistake. The store takes a :class:`~backend.app.core.token_cipher
.Secret` and writes an envelope; there is no code path that accepts a bare string for
those columns. When no cipher is configured, ``encrypt`` raises and the write never
happens -- which is the intended outcome, not a bug to work around: with no key there is
nowhere safe to put a token.

Tenant-scoped through ``business_session``, like every other adapter here. Whose
connection a row is, is row-level security's question, not an ``if``'s -- and on this
table in particular, an ``if`` that got it wrong would hand one business's publishing
credential to another.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.token_cipher import (
    Secret,
    TokenCipher,
    credential_aad,
    mask_secret,
    select_token_cipher,
)
from backend.app.db.models import PlatformConnection
from backend.app.db.session import business_session
from backend.app.services.connection_service import ConnectionStatus, ConnectionView
from backend.app.services.platform_oauth import TokenGrant

__all__ = ["PostgresConnectionStore"]

#: A revoked row keeps its identity and loses its secret. Naming the wipe once means the
#: two callers cannot disagree about what "forget the credential" includes.
_FORGOTTEN: Final = {
    "credential_encrypted": None,
    "refresh_credential_encrypted": None,
    "credential_hint": "",
    "credential_scheme": "",
}


class PostgresConnectionStore:
    """The real :class:`ConnectionStore`. One instance is fine; each call opens a session.

    The cipher is injected so a test can pin it, and defaults to
    :func:`select_token_cipher` so the running application reads the environment once and
    behaves the same on every call.
    """

    def __init__(self, cipher: TokenCipher | None = None) -> None:
        self._cipher = cipher if cipher is not None else select_token_cipher()

    @property
    def cipher(self) -> TokenCipher:
        """The cipher in force, so a status surface can say what protection is real."""
        return self._cipher

    async def save_grant(
        self, *, business_id: UUID, platform: str, grant: TokenGrant
    ) -> ConnectionView:
        """Insert or update the connection for one platform account.

        The encryption happens BEFORE the session is opened. That ordering is deliberate:
        a cipher that refuses (no key configured) must fail without having opened a
        transaction, so there is no window in which a half-written row exists, and no
        temptation to "just store the plaintext" to satisfy a NOT NULL.

        Re-authorising the same account UPDATES in place rather than inserting a second
        row. "Which of these three LinkedIn tokens is live" is not a question a publish
        path should ever have to answer.
        """
        aad = credential_aad(business_id=business_id, platform=platform)
        access_envelope = self._cipher.encrypt(grant.access_token, aad=aad)
        refresh_envelope = (
            self._cipher.encrypt(grant.refresh_token, aad=aad)
            if grant.refresh_token is not None
            else None
        )

        async with business_session(business_id) as session:
            row = (
                await session.execute(
                    select(PlatformConnection).where(
                        PlatformConnection.platform == platform,
                        PlatformConnection.external_account_id == grant.external_account_id,
                    )
                )
            ).scalar_one_or_none()

            if row is None:
                row = PlatformConnection(
                    business_id=business_id,
                    platform=platform,
                    external_account_id=grant.external_account_id,
                )
                session.add(row)

            row.external_account_name = grant.external_account_name
            row.scopes = list(grant.scopes)
            row.status = ConnectionStatus.CONNECTED.value
            row.expires_at = grant.expires_at
            row.credential_scheme = self._cipher.scheme
            row.credential_encrypted = access_envelope
            row.refresh_credential_encrypted = refresh_envelope
            row.credential_hint = mask_secret(grant.access_token.reveal())
            row.fake = grant.fake

            await session.flush()
            return _to_view(row)

    async def view(self, *, business_id: UUID, platform: str) -> ConnectionView | None:
        """This business's connection for ``platform``, without its credential."""
        async with business_session(business_id) as session:
            row = await self._row(session, platform=platform)
            return None if row is None else _to_view(row)

    async def views(self, *, business_id: UUID) -> list[ConnectionView]:
        """Every connection this business has. For the settings screen."""
        async with business_session(business_id) as session:
            rows = (
                await session.execute(
                    select(PlatformConnection).order_by(PlatformConnection.platform)
                )
            ).scalars()
            return [_to_view(row) for row in rows]

    async def reveal_access(self, *, business_id: UUID, platform: str) -> Secret | None:
        """The access credential itself. Called by the publish path and nothing else.

        ``None`` for a row that holds no credential -- a revoked connection -- rather than
        an exception, because "there is nothing to publish with" is an ordinary answer the
        actuator turns into a refusal. A credential that exists and will not DECRYPT is
        different and does raise: that is a key or an integrity problem, and silently
        reading it as absent would hide it.
        """
        return await self._reveal(business_id=business_id, platform=platform, refresh=False)

    async def reveal_refresh(self, *, business_id: UUID, platform: str) -> Secret | None:
        """The refresh credential, where the platform issued one."""
        return await self._reveal(business_id=business_id, platform=platform, refresh=True)

    async def set_status(
        self,
        *,
        business_id: UUID,
        platform: str,
        status: ConnectionStatus,
        forget_credential: bool = False,
    ) -> ConnectionView | None:
        """Move the stored lifecycle state, optionally wiping the credential.

        ``forget_credential`` is what a revoke uses. Leaving a decryptable token in a row
        marked ``revoked`` would mean a disconnect the customer asked for did not actually
        remove our ability to act as them -- which is the whole content of the request.
        """
        async with business_session(business_id) as session:
            row = await self._row(session, platform=platform)
            if row is None:
                return None
            row.status = status.value
            if forget_credential:
                for field, value in _FORGOTTEN.items():
                    setattr(row, field, value)
            await session.flush()
            return _to_view(row)

    async def _reveal(self, *, business_id: UUID, platform: str, refresh: bool) -> Secret | None:
        async with business_session(business_id) as session:
            row = await self._row(session, platform=platform)
            if row is None:
                return None
            envelope = row.refresh_credential_encrypted if refresh else row.credential_encrypted
            if envelope is None:
                return None
            return self._cipher.decrypt(
                envelope, aad=credential_aad(business_id=business_id, platform=platform)
            )

    @staticmethod
    async def _row(session: AsyncSession, *, platform: str) -> PlatformConnection | None:
        """The one connection row for ``platform`` inside an already-scoped session.

        ``business_id`` is deliberately absent from the WHERE clause: the session is
        scoped, so row-level security applies it, and repeating it in Python would create
        a second place that could get tenancy wrong.
        """
        result = await session.execute(
            select(PlatformConnection).where(PlatformConnection.platform == platform)
        )
        return result.scalars().first()


def _to_view(row: PlatformConnection) -> ConnectionView:
    """Project a row onto the credential-free read model.

    The projection is the security boundary: there is no branch here that could copy an
    envelope into the returned object, because :class:`ConnectionView` has nowhere to put
    one.
    """
    return ConnectionView(
        business_id=row.business_id,
        platform=row.platform,
        external_account_id=row.external_account_id,
        external_account_name=row.external_account_name,
        scopes=tuple(row.scopes or ()),
        status=ConnectionStatus(row.status),
        expires_at=row.expires_at,
        credential_hint=row.credential_hint,
        credential_scheme=row.credential_scheme,
        has_credential=row.credential_encrypted is not None,
        fake=row.fake,
    )
