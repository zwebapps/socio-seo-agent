"""The post queue: what is scheduled, and what happens when somebody presses publish.

`social_posts` gave the schema a place to keep a channel rendering that has been
approved but not yet sent. This module is the behaviour around it — queue, schedule,
publish, cancel — and it exists because the review screen could show a post and offer
no way to act on it.

Three rules shape it.

**Publishing goes through `actuate()`, never around it.** That function owns the three
guarantees a publish needs: the `actions` row is written BEFORE the external call, its
unique `idempotency_key` is the lock, and every failure mode comes back as a recorded
`Outcome` rather than an exception. A second publish path that talked to the actuator
directly would have none of those, and would be the one place a double post could
happen. `_perform` in `agents/nodes` is the graph's caller; this is the owner's.

**The row's status is written from the outcome, never assumed.** `succeeded` →
`published`, `refused` → `refused`, anything else → `failed`, and the distinction
between the last two is the one `actions.status` already draws: a refusal is our own
policy saying no and a failure is the platform saying no, so a retry is correct for one
and a policy violation for the other. Nothing here writes `published` optimistically.

**A simulated publish is not a publish.** `Outcome.fake` is true whenever no real
`SocialPublisher` is configured — which is every deployment today, because posting on
behalf of other people is gated on per-platform App Review. The flag is returned to the
caller so a screen cannot render "sent" for something that never left the process, and
the row stays `queued` rather than moving to `published`: a fake send must not consume
the post.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.actuators.actuate import actuate
from backend.app.actuators.contract import Actuation, Actuator, ActuatorStore, OutcomeStatus
from backend.app.db.models import ContentPiece, SocialPost, SocialPostStatus
from backend.app.engines.channel.specs import canonical_channel, has_spec
from backend.app.services.publish_cap import PublishCounter, weekly_publish_state

__all__ = [
    "SOCIAL_POST_ACTION",
    "PostNotFoundError",
    "PostNotPublishableError",
    "PostRecord",
    "PublishResult",
    "cancel_post",
    "list_posts",
    "publish_post",
    "queue_posts",
    "schedule_post",
]

#: The dotted action type the social actuator performs. Imported from the node module
#: would be a cycle (`agents.nodes` imports half the service layer), and re-declaring the
#: string is how the two would drift — so it is asserted equal in the tests instead.
SOCIAL_POST_ACTION: Final = "social.post"

#: Statuses a publish may be attempted from. `published` is absent so pressing the button
#: twice cannot send twice; `cancelled` is absent because a cancelled post is a decision,
#: not a draft. `failed` and `refused` ARE here: the first deserves a retry, and the
#: second may have been refused by a policy that has since changed (a connection added,
#: a scope granted).
PUBLISHABLE_FROM: Final = frozenset(
    {
        SocialPostStatus.QUEUED.value,
        SocialPostStatus.SCHEDULED.value,
        SocialPostStatus.FAILED.value,
        SocialPostStatus.REFUSED.value,
    }
)


class PostNotFoundError(LookupError):
    """No such post for this business.

    Raised rather than returning `None` so a route cannot accidentally treat "not yours"
    as "nothing to do". Tenancy is enforced by RLS, so a post belonging to another
    business is genuinely not visible here — this is the same 404 a cross-business run id
    gets, for the same reason: whether a row exists is itself information.
    """


class PostNotPublishableError(RuntimeError):
    """The post is in a status a publish may not start from.

    Carries the status so the caller can say which, rather than making the owner guess
    why the button did nothing.
    """

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"a post with status {status!r} cannot be published")


@dataclass(frozen=True, slots=True)
class PostRecord:
    """One queued post, as a screen needs it."""

    id: UUID
    content_piece_id: UUID
    platform: str
    body: str
    hashtags: tuple[str, ...]
    status: str
    scheduled_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    #: The article this rendering came from, for the calendar's row label. `None` when the
    #: piece has no title, which the model permits.
    piece_title: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What pressing publish actually did."""

    post: PostRecord
    status: str
    #: True when no real publisher is configured, so nothing left this process. Carried
    #: rather than inferred, because a screen that cannot tell a simulated send from a
    #: real one will report both as "sent".
    simulated: bool
    #: The platform's reference for the post, or a `fake://` one. `None` on a failure.
    external_ref: str | None
    #: Why it was refused or why it failed, in the actuator's own words.
    error: str | None


def _record(post: SocialPost, piece_title: str | None = None) -> PostRecord:
    return PostRecord(
        id=post.id,
        content_piece_id=post.content_piece_id,
        platform=post.platform,
        body=post.body,
        hashtags=tuple(str(tag) for tag in (post.hashtags or [])),
        status=post.status,
        scheduled_at=post.scheduled_at,
        published_at=post.published_at,
        created_at=post.created_at,
        piece_title=piece_title,
    )


async def queue_posts(
    business_id: UUID,
    *,
    content_piece_id: UUID,
    renderings: Mapping[str, Mapping[str, Any]],
    session: AsyncSession,
    scheduled_at: datetime | None = None,
) -> list[PostRecord]:
    """Put a run's channel renderings into the queue.

    `renderings` is `AgentState["renderings"]` — channel to the finished post REPACK
    wrote after the channel engine enforced that platform's length and hashtag limits.
    The body is copied rather than referenced for the reason `SocialPost` records: it is
    not derivable from the article, and re-rendering at publish time would send text no
    human reviewed and no engine checked.

    **Idempotent per (piece, channel).** A second call for the same content piece skips
    the channels already queued rather than adding a duplicate — an owner pressing
    "add to calendar" twice must not get two posts, and the row carries no natural key a
    unique index could enforce without also forbidding a legitimate re-post later.

    A channel with no entry in `engines/channel/specs.py` is skipped, not queued: it
    cannot be length- or link-checked, which is already why `actuators/social.py` refuses
    one, so queueing it would only defer the refusal to the moment somebody presses
    publish.
    """
    existing = set(
        (
            await session.execute(
                select(SocialPost.platform).where(
                    SocialPost.content_piece_id == content_piece_id,
                    SocialPost.status != SocialPostStatus.CANCELLED.value,
                )
            )
        )
        .scalars()
        .all()
    )

    created: list[SocialPost] = []
    for raw_channel, rendering in renderings.items():
        channel = canonical_channel(str(raw_channel).strip())
        if not channel or not has_spec(channel) or channel in existing:
            continue
        body = str(rendering.get("body") or "").strip()
        if not body:
            # REPACK already recorded WHY a rendering is missing (a banned claim, a
            # malformed tool call). Queueing an empty post would report that loss twice
            # and put an unpublishable row on the calendar.
            continue
        post = SocialPost(
            id=uuid4(),
            business_id=business_id,
            content_piece_id=content_piece_id,
            platform=channel,
            body=body,
            hashtags=[str(tag) for tag in (rendering.get("hashtags") or [])],
            status=(
                SocialPostStatus.SCHEDULED.value
                if scheduled_at is not None
                else SocialPostStatus.QUEUED.value
            ),
            scheduled_at=scheduled_at,
        )
        session.add(post)
        created.append(post)
        existing.add(channel)

    await session.flush()
    return [_record(post) for post in created]


async def list_posts(
    business_id: UUID,
    *,
    session: AsyncSession,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[PostRecord]:
    """The queue, for the calendar.

    The window filters on `scheduled_at` but ALWAYS includes untimed posts, and that is
    the point rather than an oversight: a `queued` post has no date, so a plain
    `BETWEEN` would hide exactly the backlog the calendar exists to help place. The screen
    shows them in a "not scheduled" column.
    """
    statement = select(SocialPost, ContentPiece.title).join(
        ContentPiece, ContentPiece.id == SocialPost.content_piece_id, isouter=True
    )
    if since is not None:
        statement = statement.where(
            (SocialPost.scheduled_at.is_(None)) | (SocialPost.scheduled_at >= since)
        )
    if until is not None:
        statement = statement.where(
            (SocialPost.scheduled_at.is_(None)) | (SocialPost.scheduled_at <= until)
        )
    statement = statement.order_by(
        SocialPost.scheduled_at.is_(None).desc(),
        SocialPost.scheduled_at.asc(),
        SocialPost.created_at.asc(),
    ).limit(limit)

    rows = (await session.execute(statement)).all()
    return [_record(row[0], row[1]) for row in rows]


async def _load(post_id: UUID, *, session: AsyncSession) -> SocialPost:
    post = (
        await session.execute(select(SocialPost).where(SocialPost.id == post_id))
    ).scalar_one_or_none()
    if post is None:
        raise PostNotFoundError(str(post_id))
    return post


async def schedule_post(
    post_id: UUID, *, when: datetime | None, session: AsyncSession
) -> PostRecord:
    """Give a post a time, or take its time away.

    `when=None` moves it back to `queued`, which is how an owner un-schedules something
    without cancelling it. A published post is refused: its time is a fact now, and
    rewriting it would make the gap between `scheduled_at` and `published_at` — the only
    measure of whether the publisher keeps up — a fiction.
    """
    post = await _load(post_id, session=session)
    if post.status == SocialPostStatus.PUBLISHED.value:
        raise PostNotPublishableError(post.status)
    if when is not None and when.tzinfo is None:
        # The same refusal `automation_service` makes, for the same reason: a naive
        # instant read from somewhere that meant local time shifts every slot by the
        # host's offset, and the result still looks like a valid schedule.
        raise ValueError("`when` must be timezone-aware")

    post.scheduled_at = when
    post.status = (
        SocialPostStatus.SCHEDULED.value if when is not None else SocialPostStatus.QUEUED.value
    )
    await session.flush()
    return _record(post)


async def cancel_post(post_id: UUID, *, session: AsyncSession) -> PostRecord:
    """Take a post off the calendar without deleting it.

    Cancelled rather than removed, because "we decided not to post this" is worth being
    able to see later, and a deleted row cannot be told apart from one that was never
    created.
    """
    post = await _load(post_id, session=session)
    if post.status == SocialPostStatus.PUBLISHED.value:
        raise PostNotPublishableError(post.status)
    post.status = SocialPostStatus.CANCELLED.value
    post.scheduled_at = None
    await session.flush()
    return _record(post)


async def publish_post(
    post_id: UUID,
    *,
    business_id: UUID,
    approved_by: str,
    actuator: Actuator | None,
    store: ActuatorStore,
    session: AsyncSession,
    count_published: PublishCounter | None = None,
) -> PublishResult:
    """Send one post now, through `actuate()`.

    `actuator=None` means no publisher is wired for `social.post` at all, which is
    reported as a refusal naming that rather than as a failure: nothing was attempted, so
    nothing went wrong.

    **The status is written from the outcome.** A `fake` outcome leaves the row where it
    was — a simulated send must not consume the post, or an owner would find their
    calendar emptied by a publisher that does not exist. Every other status is recorded,
    including `refused`, because "our policy said no" and "the platform said no" need to
    stay distinguishable in SQL.

    **The weekly volume cap applies here too, and that is the point of `count_published`
    being a parameter rather than an import.** This is the second way a piece can be
    published — EXPORT is the first — and a cap enforced on one path is advisory. The
    per-business USD ceiling was exactly that for a while: guarded on the HTTP routes and
    invisible to the scheduler, which spent past it. So both publish paths consult the
    same `services/publish_cap` decision, and `tests/test_run_start_guard.py` fails the
    build if a third one appears without doing so. `None` means the cap is not enforced,
    which only a test chooses; the route wires it.
    """
    post = await _load(post_id, session=session)
    if post.status not in PUBLISHABLE_FROM:
        raise PostNotPublishableError(post.status)

    if count_published is not None:
        allowance = await weekly_publish_state(business_id, count=count_published)
        if allowance.exhausted:
            # The row keeps its status, exactly as it does for a simulated send and for
            # the same reason: a cap says "not this week", so consuming the post would
            # turn a delay into a loss. It stays on the calendar and the button works
            # again once the week rolls forward.
            return PublishResult(
                post=_record(post),
                status=post.status,
                simulated=False,
                external_ref=None,
                error=(
                    f"{allowance.sentence}, so this post stays queued. This is a "
                    "deliberate volume cap, not a failure — it can go out once the week "
                    "rolls forward."
                ),
            )

    if actuator is None:
        post.status = SocialPostStatus.REFUSED.value
        await session.flush()
        return PublishResult(
            post=_record(post),
            status=SocialPostStatus.REFUSED.value,
            simulated=True,
            external_ref=None,
            error=(
                "No publisher is configured for social.post, so nothing was attempted. "
                "Publishing to this platform is gated on its own App Review."
            ),
        )

    outcome = await actuate(
        Actuation(
            business_id=business_id,
            action_type=SOCIAL_POST_ACTION,
            target=post.platform,
            payload={"body": post.body, "hashtags": list(post.hashtags or [])},
            approved_by=approved_by,
            # Reused across attempts on purpose: the key is what makes a retry after a
            # timeout return the first result instead of posting twice. Derived from the
            # POST's id rather than its content, because the content is identical across
            # a retry and a deliberate re-post, and only the row can tell them apart.
            key=f"social_post:{post.id}",
        ),
        actuator=actuator,
        store=store,
    )

    simulated = bool(getattr(outcome, "fake", False))
    if outcome.status is OutcomeStatus.SUCCEEDED and not simulated:
        post.status = SocialPostStatus.PUBLISHED.value
        post.published_at = datetime.now(UTC)
    elif outcome.status is OutcomeStatus.REFUSED:
        post.status = SocialPostStatus.REFUSED.value
    elif outcome.status is OutcomeStatus.FAILED:
        post.status = SocialPostStatus.FAILED.value
    # A SUCCEEDED-but-fake outcome falls through all three: the row keeps its status, so
    # the post is still there to send for real once a publisher exists.

    await session.flush()
    return PublishResult(
        post=_record(post),
        status=post.status,
        simulated=simulated,
        external_ref=getattr(outcome, "external_ref", None),
        error=getattr(outcome, "error", None),
    )


def publishable_platforms(renderings: Mapping[str, Any]) -> Sequence[str]:
    """Which channels in `renderings` could be queued. For a caller that wants to say so
    before offering the button."""
    return [
        channel
        for channel in (canonical_channel(str(name).strip()) for name in renderings)
        if channel and has_spec(channel)
    ]
