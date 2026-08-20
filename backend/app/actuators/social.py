"""``social.post``: the publish actuator, and the four things it refuses to do.

Read ``contract.py`` first — idempotency, approval and audit are ``actuate()``'s job, not
this file's, and everything below assumes that. ``perform`` does the call and the checks
that only a publisher can make, and nothing else.

**There is no real platform client behind this, and the boundary is deliberate.**
``docs/CHANNELS.md`` §2-3: Facebook Page and Instagram publishing need Meta App Review,
LinkedIn company pages need Marketing Developer Platform approval, and an unaudited
TikTok app can only post privately to itself. Those are two-to-six-week approval queues
belonging to somebody else, and a Meta client written today could be exercised by
nothing — not this suite, which never touches the network, and not by hand, because there
is no approved app to authenticate against. So the actuator is complete up to the seam
and the seam is empty: hand it a :class:`SocialPublisher` and it publishes, hand it
nothing and it produces a ``fake`` outcome that says nothing left the process.

What it refuses, and why each refusal is not a failure
-----------------------------------------------------

A refusal is the system working as designed (``ActuationRefusedError``, never retryable,
never alerted). Four of them:

1. **A channel we cannot publish to.** ``x`` sits behind a paid API tier and Google Ads
   spends money; ``docs/CHANNELS.md`` rules both export-only. Refusing names the export
   pack; silently succeeding would tell a customer their tweet is live.
2. **A link on a channel that cannot carry one.** ``CHANNEL_SPECS[...].link_in_body`` is
   False for Instagram and TikTok because a URL in a caption renders as plain text. That
   is ``docs/CHANNELS.md`` §1's headline correction: it is not a broken link, it is *no
   link*, so the lead never happens. Publishing it anyway would spend the post and lose
   the attribution — the link hub is the answer, and the refusal says so.
3. **A body the platform itself would reject**, or a channel whose limits we do not
   know. ``hard_max_chars`` is the platform's own ceiling, so sending past it buys a 400
   instead of a post — and a channel with no entry in ``engines/channel/specs.py`` cannot
   be length- or link-checked at all, so it refuses rather than publishing unchecked.
   That is why ``tiktok``, ``youtube``, ``instagram_story`` and ``google_business`` refuse
   today even though a business can connect them: their real path in
   ``docs/CHANNELS.md`` §2 is Tier-2 draft handoff, not direct publish, and adding a spec
   is what would promote them.
4. **No connection, or a dead one.** An expired or revoked credential is a real defect
   the business must fix by reconnecting, and it is the one case where a plausible
   success would be actively harmful: it would hide the broken integration until someone
   noticed weeks of posts that never happened.

Refusals are checked BEFORE the fake shortcut, and that ordering is the load-bearing
part of this file. A fake must never conceal a policy problem — if a caption carries a
link Instagram will not render, that is just as wrong in a simulation as it is live, and
finding it only on the day a real publisher lands is finding it in production.

The credential is read LAST, only once every check has passed, and it arrives as a
:class:`~backend.app.core.token_cipher.Secret` whose ``repr`` is masked — so the one
value in this system that must never reach a log cannot reach one through an f-string,
an exception message or a traceback local.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Final, Protocol, final
from uuid import UUID

from backend.app.actuators.contract import (
    Actuation,
    ActuationRefusedError,
    Actuator,
    Outcome,
    OutcomeStatus,
)
from backend.app.core.token_cipher import Secret, TokenCipherError
from backend.app.engines.channel.specs import CHANNEL_SPECS, canonical_channel
from backend.app.services.connection_service import ConnectionView

logger: Final = logging.getLogger(__name__)

__all__ = [
    "ACTION_TYPE",
    "CHANNEL_PLATFORMS",
    "ConnectionReader",
    "SocialPostActuator",
    "SocialPublisher",
]

#: The dotted name this actuator answers to. Target-first, like every other action type,
#: so a row in the `actions` table says which integration owns it without a join.
ACTION_TYPE: Final = "social.post"

#: Which platform authorises which channel. Not an identity mapping, and that is the
#: reason it exists: an Instagram story is published with the Instagram connection, so a
#: business connects an account once and gets both surfaces.
#:
#: A channel ABSENT from this table cannot be published to at all — ``x`` (paid API tier)
#: and ``google_ads`` (spends money) are absent on purpose, per ``docs/CHANNELS.md`` §2.
#: Names are the product's channel names, the ones already in
#: ``services/link_service._CHANNEL_TAGS`` and ``engines/channel/specs.py``.
CHANNEL_PLATFORMS: Final[Mapping[str, str]] = {
    "facebook": "facebook",
    "instagram": "instagram",
    "instagram_story": "instagram",
    "linkedin": "linkedin",
    "tiktok": "tiktok",
    "youtube": "youtube",
    "google_business": "google_business",
}


class ConnectionReader(Protocol):
    """The narrowest view of the connection store this actuator needs.

    Declared here rather than imported so the actuator depends on two method shapes
    instead of on a database adapter — the same reason ``contract.py`` declares
    ``ActuatorStore`` for itself. ``PostgresConnectionStore`` satisfies it structurally,
    with no import in either direction.
    """

    async def view(self, *, business_id: UUID, platform: str) -> ConnectionView | None: ...

    async def reveal_access(self, *, business_id: UUID, platform: str) -> Secret | None: ...


class SocialPublisher(Protocol):
    """The seam a real platform client will implement, once one can be exercised.

    One method, because one method is all the difference between the platforms amounts to
    from here: a body, an optional link, a credential, and an id or URL back. Rate limits,
    media upload and the two-step create-then-publish dance some platforms require are the
    adapter's business, not this actuator's.
    """

    async def publish(
        self, *, platform: str, credential: Secret, body: str, link: str | None
    ) -> str:
        """Post it and return the external reference. Raise ``ActuatorError`` on failure."""
        ...


@final
class SocialPostActuator:
    """Publishes one rendered post to one channel, or explains why it will not.

    ``publisher=None`` — today's only configuration, because no real client exists — makes
    this a fake actuator in the contract's sense: :attr:`fake` is True and every outcome
    it produces carries ``fake=True`` so no surface can render it as a live post.
    """

    def __init__(
        self,
        *,
        connections: ConnectionReader,
        publisher: SocialPublisher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connections = connections
        self._publisher = publisher
        self._now = clock if clock is not None else _utc_now

    @property
    def action_type(self) -> str:
        return ACTION_TYPE

    @property
    def fake(self) -> bool:
        return self._publisher is None

    async def perform(self, actuation: Actuation) -> Outcome:
        """Publish, or raise ``ActuationRefusedError`` saying which rule stopped it."""
        channel = canonical_channel(actuation.target)
        platform = CHANNEL_PLATFORMS.get(channel)
        if platform is None:
            raise ActuationRefusedError(
                f"{channel!r} cannot be published to by this system -- it has no platform "
                "connection path (see docs/CHANNELS.md section 2: X sits behind a paid API "
                "tier and Google Ads spends money, so both are export-only). The content "
                "is still delivered as an export pack."
            )

        body = _text(actuation.payload.get("body"))
        if not body:
            raise ActuationRefusedError("there is no body to publish")
        link = _text(actuation.payload.get("link")) or None

        self._check_channel_rules(channel=channel, body=body, link=link)

        view = await self._connections.view(business_id=actuation.business_id, platform=platform)
        if view is None:
            raise ActuationRefusedError(
                f"this business has no {platform} connection, so there is nothing to "
                f"publish {channel} with. Connect the account first."
            )

        unusable = view.unusable_reason(now=self._now())
        if unusable is not None:
            raise ActuationRefusedError(
                f"{unusable}. Nothing was sent -- reconnect the account and approve the post again."
            )

        if self._publisher is None:
            # Every check has passed, so this WOULD have gone out. Saying so honestly is
            # the whole job of the fake path: `Outcome.summary()` renders "(SIMULATED --
            # no credential configured)" and the ref is obviously not a URL.
            return self._simulated(actuation, channel=channel, platform=platform, view=view)

        credential = await self._resolve_credential(actuation, platform=platform)
        external_ref = await self._publisher.publish(
            platform=platform, credential=credential, body=body, link=link
        )
        logger.info(
            "published: business=%s channel=%s platform=%s account=%s ref=%s",
            actuation.business_id,
            channel,
            platform,
            view.external_account_id,
            external_ref,
        )
        return Outcome(
            status=OutcomeStatus.SUCCEEDED,
            action_type=actuation.action_type,
            target=actuation.target,
            external_ref=external_ref,
            detail={
                "channel": channel,
                "platform": platform,
                "account": view.external_account_id,
                "chars": len(body),
            },
        )

    def _check_channel_rules(self, *, channel: str, body: str, link: str | None) -> None:
        """The two deterministic refusals. Arithmetic and a table, never a model call."""
        spec = CHANNEL_SPECS.get(channel)
        if spec is None:
            # A channel with a platform but no spec is a table that has drifted. Refusing
            # is safer than guessing a limit: the alternative is a post rejected by the
            # platform for a length nobody chose.
            raise ActuationRefusedError(
                f"no channel spec for {channel!r}, so its length and link rules cannot be "
                "checked. Nothing is published unchecked."
            )

        if link is not None and not spec.link_in_body:
            raise ActuationRefusedError(
                f"a {channel} post renders a URL as plain text, so this link would not be "
                "clickable -- that is not a broken link, it is no link at all, and the "
                "lead never happens (docs/CHANNELS.md section 1). Publish the post "
                "without the link and put the link in the link hub."
            )

        if len(body) > spec.hard_max_chars:
            raise ActuationRefusedError(
                f"{len(body)} characters exceeds {channel}'s own limit of "
                f"{spec.hard_max_chars}, so the platform would reject the request. "
                "Re-pack the post to length first."
            )

    async def _resolve_credential(self, actuation: Actuation, *, platform: str) -> Secret:
        """Read the stored credential, converting both failure shapes into refusals.

        A credential that is absent, or present and unreadable, means the same thing to
        this actuator: it cannot act as the business, so nothing is sent. The difference
        matters to whoever fixes it, so the messages differ -- but neither is an
        ``ActuatorError``, because retrying an unreadable envelope will not make it open.
        """
        try:
            credential = await self._connections.reveal_access(
                business_id=actuation.business_id, platform=platform
            )
        except TokenCipherError as exc:
            raise ActuationRefusedError(
                f"the stored {platform} credential could not be read ({exc}). Nothing was "
                "sent; the account has to be reconnected."
            ) from exc

        if credential is None:
            raise ActuationRefusedError(
                f"the {platform} connection holds no credential, so nothing can be "
                "published with it"
            )
        return credential

    def _simulated(
        self, actuation: Actuation, *, channel: str, platform: str, view: ConnectionView
    ) -> Outcome:
        key = actuation.idempotency_key()
        return Outcome(
            status=OutcomeStatus.SUCCEEDED,
            action_type=actuation.action_type,
            target=actuation.target,
            # Obviously not a URL, on purpose: a plausible-looking ref is how a simulated
            # post ends up in a report as evidence.
            external_ref=f"fake://{actuation.action_type}/{channel}#{key[-8:]}",
            detail={
                "simulated": True,
                "channel": channel,
                "platform": platform,
                "account": view.external_account_id,
                "reason": (
                    "no publish client is configured for this platform, so nothing left "
                    "this process. Publishing to it is gated on that platform's App "
                    "Review -- see docs/CHANNELS.md sections 2-3."
                ),
            },
            fake=True,
        )


def _text(value: object) -> str:
    """A payload field as trimmed text.

    The payload is a ``Mapping[str, Any]`` from the graph, so it is treated as untrusted
    shape rather than assumed: a body that arrives as ``None`` or an ``int`` should be a
    refusal ("there is no body to publish"), never a ``TypeError`` inside a publish call.
    """
    return value.strip() if isinstance(value, str) else ""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _satisfies_protocol(actuator: SocialPostActuator) -> Actuator:
    """Compile-time proof that this satisfies the port. mypy checks this line."""
    return actuator
