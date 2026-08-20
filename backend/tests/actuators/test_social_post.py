"""``social.post``: what it refuses, in what order, and what it never claims.

The actuator has no real platform client behind it and cannot have one yet — publishing
to Facebook, Instagram, LinkedIn or TikTok is gated on each platform's App Review
(``docs/CHANNELS.md`` §2-3). So the behaviour worth testing is not "did it post": it is
that every path either refuses with a reason a customer can act on, or produces an outcome
that says plainly nothing left the process.

Two orderings are asserted deliberately, because both are silent failures if they invert:

* **refusals come before the fake shortcut.** A caption carrying a link Instagram will not
  render is just as wrong simulated as live, and a fake success would hide it until the day
  a real publisher lands — i.e. until production.
* **the credential is read last.** Nothing may touch a customer's access token until every
  check has passed, so a refused actuation never decrypts anything at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from backend.app.actuators.actuate import actuate
from backend.app.actuators.contract import (
    Actuation,
    ActuationRefusedError,
    ActuatorError,
    Outcome,
    OutcomeStatus,
)
from backend.app.actuators.social import ACTION_TYPE, SocialPostActuator
from backend.app.core.token_cipher import CredentialUnreadableError, Secret
from backend.app.services.connection_service import ConnectionStatus, ConnectionView

BUSINESS = uuid4()
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
TOKEN = "AQV-a-real-looking-linkedin-access-token-0123456789"


def a_view(
    *,
    platform: str = "linkedin",
    status: ConnectionStatus = ConnectionStatus.CONNECTED,
    expires_at: datetime | None = None,
    has_credential: bool = True,
) -> ConnectionView:
    return ConnectionView(
        business_id=BUSINESS,
        platform=platform,
        external_account_id="urn:li:person:AbC123",
        external_account_name="Müller Sanitär",
        scopes=("w_member_social",),
        status=status,
        expires_at=expires_at if expires_at is not None else NOW + timedelta(hours=1),
        credential_hint="AQV-…6789",
        credential_scheme="v1.ephemeral",
        has_credential=has_credential,
    )


class StubConnections:
    """A ``ConnectionReader`` that counts reveals.

    The count is the point: "the credential was never read" is the assertion that proves
    the ordering, and it cannot be made against a reader that does not record being
    asked.
    """

    def __init__(
        self, view: ConnectionView | None, *, reveal_error: Exception | None = None
    ) -> None:
        self._view = view
        self._reveal_error = reveal_error
        self.reveals = 0

    async def view(self, *, business_id: UUID, platform: str) -> ConnectionView | None:
        if self._view is None or self._view.platform != platform:
            return None
        return self._view

    async def reveal_access(self, *, business_id: UUID, platform: str) -> Secret | None:
        self.reveals += 1
        if self._reveal_error is not None:
            raise self._reveal_error
        if self._view is None or not self._view.has_credential:
            return None
        return Secret(TOKEN)


class RecordingPublisher:
    """Stands in for the platform client that cannot be written yet."""

    def __init__(self, *, fail: ActuatorError | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.credentials: list[str] = []
        self._fail = fail

    async def publish(
        self, *, platform: str, credential: Secret, body: str, link: str | None
    ) -> str:
        if self._fail is not None:
            raise self._fail
        self.calls.append((platform, body, link))
        self.credentials.append(credential.reveal())
        return f"https://www.linkedin.com/feed/update/urn:li:share:{len(self.calls)}"


class InMemoryActionStore:
    """The ``actions`` ledger, enough of it to run ``actuate()`` end to end."""

    def __init__(self) -> None:
        self.settled: list[Outcome] = []
        self._claimed: dict[str, Outcome] = {}

    async def claim(self, actuation: Actuation) -> Outcome | None:
        return self._claimed.get(actuation.idempotency_key())

    async def settle(self, actuation: Actuation, outcome: Outcome) -> None:
        self.settled.append(outcome)
        if outcome.status is OutcomeStatus.SUCCEEDED:
            self._claimed[actuation.idempotency_key()] = outcome


def an_actuation(
    *,
    target: str = "linkedin",
    body: str = "Fünf Prüfungen bei einem Wasserschaden.",
    **payload: object,
) -> Actuation:
    return Actuation(
        business_id=BUSINESS,
        action_type=ACTION_TYPE,
        target=target,
        payload={"body": body, **payload},
        approved_by="user:owner",
    )


def an_actuator(
    connections: StubConnections, publisher: RecordingPublisher | None = None
) -> SocialPostActuator:
    return SocialPostActuator(connections=connections, publisher=publisher, clock=lambda: NOW)


async def test_a_channel_we_cannot_publish_to_is_refused_not_faked() -> None:
    """X sits behind a paid API tier; the honest answer names the export pack."""
    actuator = an_actuator(StubConnections(a_view()))

    with pytest.raises(ActuationRefusedError, match="export pack"):
        await actuator.perform(an_actuation(target="x"))


async def test_a_link_in_an_instagram_caption_is_refused() -> None:
    """docs/CHANNELS.md §1: a caption URL is not a broken link, it is no link at all.

    Publishing it anyway would spend the post AND lose the attribution, which is the
    worst of the three available outcomes.
    """
    connections = StubConnections(a_view(platform="instagram"))
    actuator = an_actuator(connections)

    with pytest.raises(ActuationRefusedError, match="link hub"):
        await actuator.perform(
            an_actuation(target="instagram", link="https://sma.example/go/abc123")
        )

    assert connections.reveals == 0, "a refused actuation must not decrypt a credential"


async def test_the_same_instagram_post_without_a_link_is_allowed_through() -> None:
    """Guard the guard: the refusal above must be about the LINK, not about Instagram."""
    publisher = RecordingPublisher()
    actuator = an_actuator(StubConnections(a_view(platform="instagram")), publisher)

    outcome = await actuator.perform(an_actuation(target="instagram"))

    assert outcome.succeeded
    assert publisher.calls[0][2] is None


async def test_a_body_the_platform_would_reject_is_refused_before_the_call() -> None:
    """LinkedIn's own ceiling is 3,000 characters; sending past it buys a 400."""
    publisher = RecordingPublisher()
    actuator = an_actuator(StubConnections(a_view()), publisher)

    with pytest.raises(ActuationRefusedError, match="3000"):
        await actuator.perform(an_actuation(body="x" * 3001))

    assert publisher.calls == []


async def test_a_channel_with_no_spec_refuses_rather_than_publishing_unchecked() -> None:
    """TikTok is connectable but has no length/link spec, so nothing is sent blind.

    Its real path in docs/CHANNELS.md §2 is Tier-2 draft handoff rather than direct
    publish, so refusing here is the correct product behaviour as well as the safe one.
    """
    actuator = an_actuator(StubConnections(a_view(platform="tiktok")))

    with pytest.raises(ActuationRefusedError, match="no channel spec"):
        await actuator.perform(an_actuation(target="tiktok"))


async def test_an_empty_body_is_refused_rather_than_posted() -> None:
    actuator = an_actuator(StubConnections(a_view()))

    with pytest.raises(ActuationRefusedError, match="no body"):
        await actuator.perform(an_actuation(body="   "))


async def test_a_body_that_is_not_text_is_refused_rather_than_raising() -> None:
    """The payload comes from the graph, so its shape is checked, not assumed."""
    actuator = an_actuator(StubConnections(a_view()))
    actuation = Actuation(
        business_id=BUSINESS,
        action_type=ACTION_TYPE,
        target="linkedin",
        payload={"body": None},
        approved_by="user:owner",
    )

    with pytest.raises(ActuationRefusedError, match="no body"):
        await actuator.perform(actuation)


async def test_no_connection_is_refused_with_the_action_the_owner_has_to_take() -> None:
    connections = StubConnections(None)
    actuator = an_actuator(connections)

    with pytest.raises(ActuationRefusedError, match="Connect the account"):
        await actuator.perform(an_actuation())

    assert connections.reveals == 0


async def test_an_expired_connection_refuses_and_publishes_nothing() -> None:
    """The case a plausible success would be actively harmful.

    A simulated "posted" on a dead credential hides a broken integration until somebody
    notices weeks of posts that never happened -- so the expiry has to surface as a
    refusal naming the fix.
    """
    connections = StubConnections(a_view(expires_at=NOW - timedelta(minutes=1)))
    publisher = RecordingPublisher()
    actuator = an_actuator(connections, publisher)

    with pytest.raises(ActuationRefusedError, match="expired"):
        await actuator.perform(an_actuation())

    assert publisher.calls == []
    assert connections.reveals == 0


async def test_a_revoked_connection_refuses_even_though_the_clock_is_fine() -> None:
    connections = StubConnections(a_view(status=ConnectionStatus.REVOKED, has_credential=False))
    actuator = an_actuator(connections)

    with pytest.raises(ActuationRefusedError, match="revoked"):
        await actuator.perform(an_actuation())


async def test_an_unreadable_credential_refuses_rather_than_crashing_the_run() -> None:
    """A wrong key or a moved envelope is not retryable, so it is a refusal, not a failure."""
    connections = StubConnections(
        a_view(), reveal_error=CredentialUnreadableError("bound to a different business")
    )
    actuator = an_actuator(connections, RecordingPublisher())

    with pytest.raises(ActuationRefusedError, match="could not be read"):
        await actuator.perform(an_actuation())


async def test_with_no_publisher_the_outcome_says_it_was_simulated() -> None:
    """The contract's rule 4: a missing credential means a fake that SAYS SO.

    Three separate things are asserted because a report can be wrong in three separate
    ways: the flag a UI branches on, the reference a human would click, and the sentence
    a timeline renders.
    """
    connections = StubConnections(a_view())
    actuator = an_actuator(connections)

    outcome = await actuator.perform(an_actuation())

    assert actuator.fake is True
    assert outcome.status is OutcomeStatus.SUCCEEDED
    assert outcome.fake is True
    assert outcome.detail["simulated"] is True
    assert "App Review" in str(outcome.detail["reason"])
    assert outcome.external_ref is not None
    assert outcome.external_ref.startswith("fake://"), (
        "a plausible-looking ref is how a simulated post ends up in a report as evidence"
    )
    assert "SIMULATED" in outcome.summary()
    assert connections.reveals == 0, "the fake path must never touch the credential"


async def test_a_refusal_still_refuses_when_there_is_no_publisher() -> None:
    """The ordering that matters: a fake must not conceal a policy problem.

    Same actuation as the Instagram-link refusal, with the configuration that exists in
    production today (no publisher). If the fake shortcut ran first this would report a
    simulated success and the dead CTA would only be discovered by the first real post.
    """
    actuator = an_actuator(StubConnections(a_view(platform="instagram")))

    with pytest.raises(ActuationRefusedError, match="link hub"):
        await actuator.perform(
            an_actuation(target="instagram", link="https://sma.example/go/abc123")
        )


async def test_with_a_publisher_it_delegates_once_and_reports_the_real_reference() -> None:
    publisher = RecordingPublisher()
    connections = StubConnections(a_view())
    actuator = an_actuator(connections, publisher)

    outcome = await actuator.perform(
        an_actuation(link="https://sma.example/go/abc123", body="Wasserschaden in Koblenz?")
    )

    assert actuator.fake is False
    assert outcome.fake is False
    assert outcome.succeeded
    assert outcome.external_ref is not None and outcome.external_ref.startswith("https://")
    assert publisher.calls == [
        ("linkedin", "Wasserschaden in Koblenz?", "https://sma.example/go/abc123")
    ]
    assert publisher.credentials == [TOKEN], "the publisher was handed the real credential"
    assert connections.reveals == 1, "the credential was read more than once"


async def test_an_instagram_story_resolves_to_the_instagram_connection() -> None:
    """The channel-to-platform mapping is not an identity, and that is the reason it exists.

    A business connects one Instagram account and gets both surfaces. (The story channel
    has no spec yet, so this asserts the mapping resolves rather than that it posts.)
    """
    connections = StubConnections(a_view(platform="instagram"))
    actuator = an_actuator(connections)

    with pytest.raises(ActuationRefusedError, match="no channel spec"):
        await actuator.perform(an_actuation(target="instagram_story"))


async def test_a_provider_failure_is_a_failure_and_not_a_refusal() -> None:
    """Retryable-vs-not is the field callers branch on, so the two must not be conflated."""
    publisher = RecordingPublisher(fail=ActuatorError("rate limited", retryable=True))
    actuator = an_actuator(StubConnections(a_view()), publisher)

    with pytest.raises(ActuatorError) as caught:
        await actuator.perform(an_actuation())

    assert not isinstance(caught.value, ActuationRefusedError)
    assert caught.value.retryable is True


async def test_through_actuate_a_refusal_is_recorded_rather_than_raised() -> None:
    """The whole point of the layer: a failed publish must not take the run down with it.

    `actuate()` owns idempotency, approval and audit, so this asserts the seam rather
    than re-testing them: an `ActuationRefusedError` from `perform` becomes a REFUSED
    outcome, and it is settled -- an unrecorded refusal is a decision nobody can review.
    """
    store = InMemoryActionStore()
    actuator = an_actuator(StubConnections(None))

    outcome = await actuate(an_actuation(), actuator=actuator, store=store)

    assert outcome.status is OutcomeStatus.REFUSED
    assert outcome.error is not None and "Connect the account" in outcome.error
    assert [settled.status for settled in store.settled] == [OutcomeStatus.REFUSED]


async def test_through_actuate_an_unapproved_post_never_reaches_the_actuator() -> None:
    """Belt and braces on the gate the REVIEW interrupt exists to produce."""
    store = InMemoryActionStore()
    publisher = RecordingPublisher()
    actuator = an_actuator(StubConnections(a_view()), publisher)
    unapproved = Actuation(
        business_id=BUSINESS,
        action_type=ACTION_TYPE,
        target="linkedin",
        payload={"body": "Ready to go."},
        approved_by="",
    )

    outcome = await actuate(unapproved, actuator=actuator, store=store)

    assert outcome.status is OutcomeStatus.REFUSED
    assert publisher.calls == []
