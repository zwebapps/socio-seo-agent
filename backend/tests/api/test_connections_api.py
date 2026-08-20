"""Connecting a platform over HTTP: the whole cycle, and every refusal on the way.

The store, the cipher, the OAuth seam and the lifecycle were all built and tested before
these routes existed -- and none of it was reachable, so no business could connect an
account even to the fake provider. These tests are about the four things only the HTTP
layer can get wrong:

* the ``state`` nonce is actually checked, so the callback cannot be completed by
  somebody who did not start the flow (``core/csrf.py`` cannot cover a redirect-borne
  ``GET``, so this comparison is the CSRF control -- see ``api/oauth_state.py``);
* a credential never leaves the process, in a response body or in a log line;
* the tenant comes from the session, so business B's list and business B's disconnect
  cannot reach business A's connection;
* disconnecting is a statement about the end state, so it is idempotent.

Hermetic. ``FakeOAuthProvider`` makes no network call by construction, and the store is an
in-memory double that encrypts through the REAL
:class:`~backend.app.core.token_cipher.EphemeralVaultCipher` -- so "the stored envelope
holds no plaintext" is exercised against the same code the database path uses. The
database half of tenancy is proved against real row-level security in
``tests/db/test_platform_connections.py``; what these assert is that the routes hand the
store the right tenant in the first place.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest

from backend.app.api import connections as connections_api
from backend.app.api import oauth_state
from backend.app.api.auth import current_user
from backend.app.api.runs import current_business
from backend.app.core.config import Settings
from backend.app.core.token_cipher import (
    EphemeralVaultCipher,
    NotConfiguredCipher,
    Secret,
    TokenCipher,
    credential_aad,
    mask_secret,
)
from backend.app.db.models import User
from backend.app.main import create_app
from backend.app.services.connection_service import ConnectionStatus, ConnectionView
from backend.app.services.platform_oauth import FakeOAuthProvider, OAuthProvider, TokenGrant

pytestmark = pytest.mark.anyio

PLATFORM = "linkedin"
BUSINESS = UUID("11111111-1111-1111-1111-111111111111")
OTHER_BUSINESS = UUID("22222222-2222-2222-2222-222222222222")
SECRET = "a-test-session-signing-key-of-adequate-length"

#: Local, so the state cookie is not ``Secure`` and httpx's jar will carry it over the
#: plain-HTTP test transport. The ``Secure``/``__Host-`` half is asserted on the raw
#: ``Set-Cookie`` header instead -- see the production test at the bottom.
LOCAL_SETTINGS = Settings(environment="local", session_secret=SECRET)


class FakeConnectionStore:
    """``ConnectionStoreWithCipher``, in memory, keyed by (business, platform).

    Deliberately not a mock. It stores ENVELOPES produced by the real ephemeral cipher and
    returns :class:`ConnectionView` objects, so a test that looked for a plaintext leak
    would find one; a mock would have let it pass. Keying on the business is what makes
    the cross-tenant tests meaningful: a route that forgot to pass the caller's tenant
    would read the wrong bucket, which is the mistake RLS is the second line of defence
    against.
    """

    def __init__(self, cipher: TokenCipher | None = None) -> None:
        self._cipher = cipher if cipher is not None else EphemeralVaultCipher()
        self._rows: dict[tuple[UUID, str], tuple[ConnectionView, str | None, str | None]] = {}
        #: Every access-token plaintext this store has ever been handed. The tests read it
        #: to assert the exact string never appears in a response or a log.
        self.plaintexts: list[str] = []
        self.revoked_at_provider: list[str] = []

    @property
    def cipher(self) -> TokenCipher:
        return self._cipher

    def simulate_restart(self) -> None:
        """Replace the ephemeral vault, leaving every stored envelope unreadable.

        Exactly what a deploy does under ``PLATFORM_CREDENTIAL_KEY=ephemeral``: the rows
        survive and the plaintexts do not. Documented behaviour rather than a fault, and
        the one state in which a disconnect has to work without being able to read what it
        is disconnecting.
        """
        self._cipher = EphemeralVaultCipher()

    async def save_grant(
        self, *, business_id: UUID, platform: str, grant: TokenGrant
    ) -> ConnectionView:
        aad = credential_aad(business_id=business_id, platform=platform)
        access = self._cipher.encrypt(grant.access_token, aad=aad)
        refresh = (
            self._cipher.encrypt(grant.refresh_token, aad=aad)
            if grant.refresh_token is not None
            else None
        )
        self.plaintexts.append(grant.access_token.reveal())
        view = ConnectionView(
            business_id=business_id,
            platform=platform,
            external_account_id=grant.external_account_id,
            external_account_name=grant.external_account_name,
            scopes=grant.scopes,
            status=ConnectionStatus.CONNECTED,
            expires_at=grant.expires_at,
            credential_hint=mask_secret(grant.access_token.reveal()),
            credential_scheme=self._cipher.scheme,
            has_credential=True,
            fake=grant.fake,
        )
        self._rows[(business_id, platform)] = (view, access, refresh)
        return view

    async def view(self, *, business_id: UUID, platform: str) -> ConnectionView | None:
        row = self._rows.get((business_id, platform))
        return None if row is None else row[0]

    async def views(self, *, business_id: UUID) -> list[ConnectionView]:
        return [view for (owner, _), (view, _, _) in self._rows.items() if owner == business_id]

    async def reveal_access(self, *, business_id: UUID, platform: str) -> Secret | None:
        return self._reveal(business_id=business_id, platform=platform, refresh=False)

    async def reveal_refresh(self, *, business_id: UUID, platform: str) -> Secret | None:
        return self._reveal(business_id=business_id, platform=platform, refresh=True)

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
        view, access, refresh = row
        updated = ConnectionView(
            business_id=view.business_id,
            platform=view.platform,
            external_account_id=view.external_account_id,
            external_account_name=view.external_account_name,
            scopes=view.scopes,
            status=status,
            expires_at=view.expires_at,
            credential_hint="" if forget_credential else view.credential_hint,
            credential_scheme="" if forget_credential else view.credential_scheme,
            has_credential=not forget_credential and view.has_credential,
            fake=view.fake,
        )
        self._rows[(business_id, platform)] = (
            updated,
            None if forget_credential else access,
            None if forget_credential else refresh,
        )
        return updated

    def _reveal(self, *, business_id: UUID, platform: str, refresh: bool) -> Secret | None:
        row = self._rows.get((business_id, platform))
        if row is None:
            return None
        envelope = row[2] if refresh else row[1]
        if envelope is None:
            return None
        return self._cipher.decrypt(
            envelope, aad=credential_aad(business_id=business_id, platform=platform)
        )


class RecordingProvider:
    """``FakeOAuthProvider`` with the revokes it was asked for written down.

    Wrapping rather than reimplementing: the lifecycle behaviours that matter (a refresh
    issues a DIFFERENT token, an unknown refresh token raises) are in the real fake and
    are tested in ``tests/services/test_connection_service.py``. What these tests add is
    "did the route actually tell the platform to forget the credential before wiping our
    copy", which needs a record and nothing else.
    """

    def __init__(self, platform: str = PLATFORM) -> None:
        self._inner = FakeOAuthProvider(platform)
        self.revoked: list[str] = []

    @property
    def platform(self) -> str:
        return self._inner.platform

    @property
    def fake(self) -> bool:
        return True

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: Any) -> str:
        return self._inner.authorization_url(redirect_uri=redirect_uri, state=state, scopes=scopes)

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        return await self._inner.exchange_code(code=code, redirect_uri=redirect_uri)

    async def refresh(self, refresh_token: Secret) -> TokenGrant:
        return await self._inner.refresh(refresh_token)

    async def revoke(self, credential: Secret) -> None:
        self.revoked.append(credential.reveal())
        await self._inner.revoke(credential)


def _user() -> User:
    return User(id=uuid4(), email="owner@example.com", is_active=True, role="user")


def _client(
    store: FakeConnectionStore,
    *,
    provider: OAuthProvider | None = None,
    business_id: UUID = BUSINESS,
    authenticated: bool = True,
    settings: Settings = LOCAL_SETTINGS,
) -> httpx.AsyncClient:
    app = create_app()
    if authenticated:
        app.dependency_overrides[current_user] = _user
        app.dependency_overrides[current_business] = lambda: business_id
    app.dependency_overrides[connections_api.get_connection_store] = lambda: store
    app.dependency_overrides[connections_api.get_connection_settings] = lambda: settings
    if provider is not None:
        app.dependency_overrides[connections_api.get_provider_factory] = lambda: (
            lambda _platform: provider
        )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _nonce_from(authorization_url: str) -> str:
    """The ``state`` the provider will echo back, read out of the URL the human is sent to.

    Exactly what a browser and a real provider do with it -- and the reason the route does
    not put ``state`` in its response body: a client that can read the nonce can be
    tricked into echoing it, which is the attack it exists to stop.
    """
    return parse_qs(urlsplit(authorization_url).query)["state"][0]


async def _start(client: httpx.AsyncClient, platform: str = PLATFORM) -> httpx.Response:
    return await client.post(f"/api/v1/connections/{platform}/connect")


# --------------------------------------------------------------------------- #
# The gap this closes
# --------------------------------------------------------------------------- #


async def test_a_business_can_connect_view_and_disconnect_an_account() -> None:
    """The full cycle against the fake provider, with no network anywhere.

    This is the whole point of the task: before these routes, every piece below them
    worked and none of it could be reached, so `actuators/social.py` could only ever
    refuse for want of a connection.
    """
    store = FakeConnectionStore()
    provider = RecordingProvider()

    async with _client(store, provider=provider) as client:
        started = await _start(client)
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["platform"] == PLATFORM
        assert body["fake"] is True
        assert body["scopes"] == ["w_member_social"]

        finished = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": _nonce_from(body["authorizationUrl"])},
        )
        assert finished.status_code == 200, finished.text
        connected = finished.json()
        assert connected["status"] == "connected"
        assert connected["usable"] is True
        assert connected["unusableReason"] is None
        assert connected["hasCredential"] is True
        assert connected["fake"] is True, "a fake connection must never look like a real one"
        assert connected["credentialScheme"] == "v1.ephemeral"

        listed = await client.get("/api/v1/connections")
        assert listed.status_code == 200
        assert [row["platform"] for row in listed.json()["connections"]] == [PLATFORM]
        assert listed.headers["cache-control"] == "no-store"

        removed = await client.delete(f"/api/v1/connections/{PLATFORM}")
        assert removed.status_code == 204

    # Revoked at the provider BEFORE our copy was wiped: the order is the whole content of
    # a disconnect. Wiping first would leave a live token we can no longer revoke.
    assert provider.revoked, "the platform must be told to forget the credential"
    after = await store.view(business_id=BUSINESS, platform=PLATFORM)
    assert after is not None
    assert after.status is ConnectionStatus.REVOKED
    assert after.has_credential is False
    assert await store.reveal_access(business_id=BUSINESS, platform=PLATFORM) is None


async def test_starting_a_connect_sets_a_signed_host_only_state_cookie() -> None:
    """The nonce is held in the cookie and NOT in the body, and the cookie is signed with
    the platform and the tenant inside it -- see ``api/oauth_state.py``."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        started = await _start(client)
        raw = client.cookies.get(oauth_state.STATE_COOKIE_BASE_NAME)

    assert "state" not in started.json()
    assert raw is not None
    verified = oauth_state.verify_state(raw, secret=SECRET)
    assert verified is not None
    assert verified.platform == PLATFORM
    assert verified.business_id == BUSINESS
    assert verified.nonce == _nonce_from(started.json()["authorizationUrl"])

    set_cookie = started.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie, (
        "the callback is a top-level redirect; SameSite=Strict would withhold the cookie "
        "on the one request that needs it"
    )
    assert "Domain=" not in set_cookie, "a host-only cookie, exactly like the session's"


async def test_the_authorization_url_and_the_exchange_use_the_configured_callback() -> None:
    """``redirect_uri`` is built from configuration, never from ``Host`` -- a poisoned
    header must not be able to send a customer's authorisation code elsewhere. OAuth also
    compares the value byte-for-byte between the two halves of the flow."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        started = await _start(client, "facebook")

    redirect = parse_qs(urlsplit(started.json()["authorizationUrl"]).query)["redirect_uri"][0]
    assert redirect == (f"{LOCAL_SETTINGS.public_base_url}/api/v1/connections/facebook/callback")


# --------------------------------------------------------------------------- #
# The state check IS the CSRF control on the callback
# --------------------------------------------------------------------------- #


async def test_a_callback_whose_state_does_not_match_the_cookie_is_refused() -> None:
    """The attack this closes: a third party who can make a victim's browser follow a
    callback URL of their choosing would otherwise connect an account THEY authorised into
    the victim's business."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        await _start(client)
        response = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": "a-state-nobody-issued"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_state_refused"
    assert not store.plaintexts, "nothing may be stored for a callback we refuse"


async def test_a_callback_with_no_state_cookie_at_all_is_refused() -> None:
    """No cookie means no flow was started in this browser. There is nothing to compare
    against, so the only safe answer is a refusal -- never "accept it anyway"."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        response = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": "anything"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_state_refused"
    assert not store.plaintexts


async def test_a_callback_with_no_state_parameter_at_all_is_refused() -> None:
    """An empty ``state`` must not compare equal to anything, and must not reach the
    exchange."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        await _start(client)
        response = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback", params={"code": "fake-code"}
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_state_refused"
    assert not store.plaintexts


async def test_a_state_issued_for_one_platform_cannot_be_redeemed_at_another() -> None:
    """The platform is inside the signature for this reason: without it, an Instagram
    authorisation could be filed against the Facebook connection."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        started = await _start(client, "facebook")
        response = await client.get(
            "/api/v1/connections/instagram/callback",
            params={"code": "fake-code", "state": _nonce_from(started.json()["authorizationUrl"])},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_state_refused"
    assert not store.plaintexts


async def test_a_state_issued_for_another_business_is_refused() -> None:
    """Belt and braces with the session: the session decides whose business this is, and
    binding it into the cookie means the two have to agree."""
    store = FakeConnectionStore()
    stale = oauth_state.sign_state(
        nonce="a-nonce-issued-to-somebody-else",
        platform=PLATFORM,
        business_id=OTHER_BUSINESS,
        issued_at=datetime.now(UTC),
        secret=SECRET,
    )

    async with _client(store) as client:
        client.cookies.set(oauth_state.STATE_COOKIE_BASE_NAME, stale)
        response = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": "a-nonce-issued-to-somebody-else"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_state_refused"
    assert not store.plaintexts


async def test_an_expired_state_is_refused() -> None:
    """A one-shot nonce with an unbounded life is a one-shot nonce in name only. The
    expiry is inside the signature, so the browser cannot extend it."""
    store = FakeConnectionStore()
    expired = oauth_state.sign_state(
        nonce="an-old-nonce",
        platform=PLATFORM,
        business_id=BUSINESS,
        issued_at=datetime.now(UTC) - oauth_state.STATE_TTL - timedelta(minutes=1),
        secret=SECRET,
    )

    async with _client(store) as client:
        client.cookies.set(oauth_state.STATE_COOKIE_BASE_NAME, expired)
        response = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": "an-old-nonce"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_state_refused"


async def test_the_state_cookie_is_consumed_by_the_callback() -> None:
    """One shot. Replaying a successful callback -- a refresh of the browser tab, or an
    attacker who captured the URL -- must not go through a second time."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        started = await _start(client)
        nonce = _nonce_from(started.json()["authorizationUrl"])
        first = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": nonce},
        )
        second = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": nonce},
        )

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["detail"]["code"] == "oauth_state_refused"
    assert len(store.plaintexts) == 1, "the replay must not have stored a second credential"


async def test_a_provider_refusal_is_reported_without_echoing_its_text() -> None:
    """A human clicking "cancel" is not an error in our system, and the provider's own
    string is never reflected into a response body -- the same rule ``core/csrf.py``'s
    single refusal body follows."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        started = await _start(client)
        response = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={
                "state": _nonce_from(started.json()["authorizationUrl"]),
                "error": "access_denied<script>alert(1)</script>",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "authorisation_declined"
    assert "script" not in response.text
    assert not store.plaintexts


# --------------------------------------------------------------------------- #
# A credential is write-only to the outside
# --------------------------------------------------------------------------- #


async def test_no_response_body_ever_carries_the_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one property that must hold across every route: the token is stored, used and
    never handed back. What a screen gets is ``mask_secret``'s four-and-four hint, which
    identifies the row without disclosing it -- and the same is true of every log line the
    cycle produces."""
    caplog.set_level(logging.DEBUG)
    store = FakeConnectionStore()
    bodies: list[str] = []

    async with _client(store) as client:
        started = await _start(client)
        bodies.append(started.text)
        finished = await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": _nonce_from(started.json()["authorizationUrl"])},
        )
        bodies.append(finished.text)
        listed = await client.get("/api/v1/connections")
        bodies.append(listed.text)

    assert store.plaintexts, "the test is meaningless if nothing was ever stored"
    secret = store.plaintexts[0]
    for body in bodies:
        assert secret not in body
        assert "v1.ephemeral:" not in body, "the ENVELOPE must not be returned either"

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logged

    connected = finished.json()
    assert connected["credentialHint"] == mask_secret(secret)
    assert secret not in connected["credentialHint"]


async def test_a_connection_the_clock_has_passed_is_reported_unusable() -> None:
    """``unusableReason`` comes from the same pure function the publish actuator asks, so
    the sentence on the settings screen is the sentence the refusal would carry."""
    store = FakeConnectionStore()
    await store.save_grant(
        business_id=BUSINESS,
        platform=PLATFORM,
        grant=TokenGrant(
            external_account_id="acct",
            access_token=Secret("an-access-token-that-is-long-enough"),
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
            scopes=("w_member_social",),
            fake=True,
        ),
    )

    async with _client(store) as client:
        listed = await client.get("/api/v1/connections")

    row = listed.json()["connections"][0]
    assert row["usable"] is False
    assert row["needsRenewal"] is True
    assert "expired" in (row["unusableReason"] or "")


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_business_b_cannot_see_business_a_s_connection() -> None:
    """The tenant comes from the session, so B's list is B's. Row-level security is the
    second line of defence and is proved against a real database in
    ``tests/db/test_platform_connections.py``; this asserts the route asks for the right
    tenant in the first place."""
    store = FakeConnectionStore()

    async with _client(store, business_id=BUSINESS) as client:
        started = await _start(client)
        await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": _nonce_from(started.json()["authorizationUrl"])},
        )

    async with _client(store, business_id=OTHER_BUSINESS) as other:
        listed = await other.get("/api/v1/connections")

    assert listed.status_code == 200
    assert listed.json()["connections"] == []


async def test_business_b_cannot_disconnect_business_a_s_connection() -> None:
    """A cross-tenant disconnect is not a 403 that confirms the row exists -- it is a
    no-op that leaves A connected."""
    store = FakeConnectionStore()
    provider = RecordingProvider()

    async with _client(store, provider=provider, business_id=BUSINESS) as client:
        started = await _start(client)
        await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": _nonce_from(started.json()["authorizationUrl"])},
        )

    async with _client(store, provider=provider, business_id=OTHER_BUSINESS) as other:
        removed = await other.delete(f"/api/v1/connections/{PLATFORM}")

    assert removed.status_code == 204
    assert not provider.revoked, "nothing of A's may be revoked at the platform"
    survivor = await store.view(business_id=BUSINESS, platform=PLATFORM)
    assert survivor is not None
    assert survivor.status is ConnectionStatus.CONNECTED
    assert survivor.has_credential is True


async def test_every_route_needs_a_session() -> None:
    """No cookie, no connections. All four, because one unguarded route is the whole
    guard."""
    store = FakeConnectionStore()

    async with _client(store, authenticated=False) as client:
        assert (await client.get("/api/v1/connections")).status_code == 401
        assert (await _start(client)).status_code == 401
        assert (await client.get(f"/api/v1/connections/{PLATFORM}/callback")).status_code == 401
        assert (await client.delete(f"/api/v1/connections/{PLATFORM}")).status_code == 401


# --------------------------------------------------------------------------- #
# Disconnecting, and the honest status of what connecting can do
# --------------------------------------------------------------------------- #


async def test_disconnecting_is_idempotent() -> None:
    """ "Disconnect this account" is a statement about the end state, not about a row, so a
    second call is a success. A customer clicking twice, or a client retrying after a
    dropped response, must not be told something is wrong."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        started = await _start(client)
        await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": _nonce_from(started.json()["authorizationUrl"])},
        )
        first = await client.delete(f"/api/v1/connections/{PLATFORM}")
        second = await client.delete(f"/api/v1/connections/{PLATFORM}")
        third = await client.delete("/api/v1/connections/facebook")

    assert first.status_code == 204
    assert second.status_code == 204
    assert third.status_code == 204, "nothing to disconnect is still the requested end state"


async def test_disconnecting_still_forgets_a_credential_that_will_not_decrypt() -> None:
    """The ephemeral vault does not survive a restart, which is documented behaviour, not
    a fault -- and a customer must still be able to disconnect. A credential we cannot
    decrypt is one we cannot use, so the honest end state is a revoked row with nothing in
    it."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        started = await _start(client)
        await client.get(
            f"/api/v1/connections/{PLATFORM}/callback",
            params={"code": "fake-code", "state": _nonce_from(started.json()["authorizationUrl"])},
        )
        # The process restarted: the vault is empty and every envelope is now unreadable.
        store.simulate_restart()
        removed = await client.delete(f"/api/v1/connections/{PLATFORM}")

    assert removed.status_code == 204
    after = await store.view(business_id=BUSINESS, platform=PLATFORM)
    assert after is not None
    assert after.status is ConnectionStatus.REVOKED
    assert after.has_credential is False


async def test_an_unknown_platform_is_a_404_that_names_the_known_ones() -> None:
    store = FakeConnectionStore()

    async with _client(store) as client:
        for response in (
            await _start(client, "myspace"),
            await client.get("/api/v1/connections/myspace/callback"),
            await client.delete("/api/v1/connections/myspace"),
        ):
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "unknown_platform"
            assert "linkedin" in response.json()["detail"]["message"]


async def test_the_list_says_that_every_provider_is_fake_and_why() -> None:
    """``oauth_status()`` verbatim. A screen offering "Connect Instagram" without saying
    publishing there waits on somebody else's approval queue is a support ticket, and
    ``docs/CRITERIA_MAP.md`` §7's claims discipline is binding on UI copy."""
    store = FakeConnectionStore()

    async with _client(store) as client:
        listed = await client.get("/api/v1/connections")

    reported = listed.json()["oauth"]
    assert reported["usingFakeProviders"] is True
    assert reported["realProviders"] == []
    assert set(reported["blockedOnAppReview"]) == {"facebook", "instagram", "linkedin", "tiktok"}
    assert "App Review" in reported["message"]
    assert "FakeOAuthProvider" in reported["message"]

    storage = listed.json()["credentialStorage"]
    assert storage["scheme"] == "v1.ephemeral"
    assert storage["canStoreCredentials"] is True


async def test_a_connect_is_refused_before_the_human_leaves_when_nothing_can_be_stored() -> None:
    """With no ``PLATFORM_CREDENTIAL_KEY`` the cipher refuses, and refusing is the point.
    Sending a customer through a consent screen first would waste their time AND leave a
    live grant on the platform that we never recorded and can therefore never revoke."""
    store = FakeConnectionStore(cipher=NotConfiguredCipher("PLATFORM_CREDENTIAL_KEY is not set"))

    async with _client(store) as client:
        started = await _start(client)
        listed = await client.get("/api/v1/connections")

    assert started.status_code == 503
    assert started.json()["detail"]["code"] == "credential_storage_unavailable"
    assert "PLATFORM_CREDENTIAL_KEY" in started.json()["detail"]["message"]
    assert "set-cookie" not in started.headers, "no pending authorisation may be issued"
    assert listed.json()["credentialStorage"]["canStoreCredentials"] is False


async def test_the_state_cookie_is_host_prefixed_and_secure_outside_local() -> None:
    """Asserted on the raw header, because a ``Secure`` cookie is not stored by a client
    talking plain HTTP -- which is also why the name is unprefixed in local development."""
    store = FakeConnectionStore()
    production = Settings(environment="production", session_secret=SECRET)

    async with _client(store, settings=production) as client:
        started = await _start(client)

    set_cookie = started.headers["set-cookie"]
    assert set_cookie.startswith("__Host-sma_oauth_state=")
    assert "Secure" in set_cookie
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie
