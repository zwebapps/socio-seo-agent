"""The post queue, against real SQL.

Every test here targets an outcome that would look like a working publish. A simulated
send that moves the row to `published` empties an owner's calendar via a publisher that
does not exist. A status written optimistically reports "sent" for a platform that
refused. And a queue that is not idempotent gives an owner two posts for one click.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from backend.app.actuators.contract import Actuation, Outcome, OutcomeStatus
from backend.app.db.models import SocialPostStatus
from backend.app.db.session import business_session
from backend.app.services import social_post_service as svc

pytestmark = pytest.mark.db

RENDERINGS: dict[str, dict[str, Any]] = {
    "linkedin": {"body": "Wir sind da, rund um die Uhr.", "hashtags": ["#Notdienst"]},
    "facebook": {"body": "Rohrbruch? Wir kommen.", "hashtags": []},
}


async def _piece(business_id: UUID) -> UUID:
    """A published content piece for the posts to hang off.

    Written with raw SQL rather than the ORM because `content_pieces` has a NOT NULL
    `body_md` and a status CHECK, and the point here is a valid parent row, not a
    faithful piece.
    """
    piece_id = uuid4()
    async with business_session(business_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO content_pieces
                    (id, business_id, surface, title, body_md, status, created_at, updated_at)
                VALUES (:i, :b, 'article', 'Notdienst in Koblenz', '# hi', 'approved',
                        now(), now())
                """
            ),
            {"i": piece_id, "b": business_id},
        )
    return piece_id


class _Publisher:
    """A social actuator double.

    `action_type` matters: `actuate` refuses a mismatch outright, and that refusal would
    otherwise look like a policy refusal from the platform.
    """

    action_type = "social.post"
    #: Part of the `Actuator` protocol: whether this actuator can reach the outside
    #: world at all. False here because the OUTCOME carries the simulated flag in these
    #: tests — the distinction being exercised is what the service writes for a fake
    #: outcome, not whether the actuator advertises itself as one.
    fake = False

    def __init__(self, outcome: Outcome) -> None:
        self._outcome = outcome
        self.calls: list[Actuation] = []

    async def perform(self, actuation: Actuation) -> Outcome:
        self.calls.append(actuation)
        return self._outcome


def _outcome(status: OutcomeStatus, *, fake: bool = False, ref: str | None = None) -> Outcome:
    return Outcome(
        status=status,
        action_type="social.post",
        target="linkedin",
        external_ref=ref,
        fake=fake,
    )


class _Store:
    """An in-memory `actions` ledger, to `ActuatorStore`'s real shape.

    `claim` either reserves the key (returning None) or hands back the outcome that key
    already has — the unusual contract the protocol documents, and the reason a store
    cannot answer a plain "exists": the gap between "does it exist" and "fetch the prior
    result" is where a double post lives.
    """

    def __init__(self) -> None:
        self.claimed: list[str] = []
        self._settled: dict[str, Outcome] = {}

    async def claim(self, actuation: Actuation) -> Outcome | None:
        key = actuation.idempotency_key()
        self.claimed.append(key)
        return self._settled.get(key)

    async def settle(self, actuation: Actuation, outcome: Outcome) -> None:
        self._settled[actuation.idempotency_key()] = outcome


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #


async def test_queueing_a_runs_renderings_creates_one_post_per_channel(
    scoped_sessions: None, business_a: UUID
) -> None:
    piece = await _piece(business_a)

    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )

    assert {post.platform for post in created} == {"linkedin", "facebook"}
    assert all(post.status == SocialPostStatus.QUEUED.value for post in created)
    assert all(post.scheduled_at is None for post in created), "queued means untimed"


async def test_queueing_twice_does_not_duplicate_a_post(
    scoped_sessions: None, business_a: UUID
) -> None:
    """An owner pressing "add to calendar" twice must not get two posts. There is no
    natural key a unique index could enforce without also forbidding a legitimate
    re-post later, so the service checks."""
    piece = await _piece(business_a)

    async with business_session(business_a) as session:
        await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
    async with business_session(business_a) as session:
        second = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        listed = await svc.list_posts(business_a, session=session)

    assert second == []
    assert len(listed) == 2


async def test_a_channel_with_no_spec_is_skipped_rather_than_queued(
    scoped_sessions: None, business_a: UUID
) -> None:
    """`actuators/social.py` already refuses a channel it cannot length- or link-check,
    so queueing one only defers the refusal to the moment somebody presses publish."""
    piece = await _piece(business_a)

    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a,
            content_piece_id=piece,
            renderings={"threads": {"body": "hello", "hashtags": []}},
            session=session,
        )

    assert created == []


async def test_an_empty_body_is_not_queued(scoped_sessions: None, business_a: UUID) -> None:
    """REPACK already recorded WHY a rendering is missing — a banned claim, a malformed
    tool call. An empty post on the calendar reports that loss twice."""
    piece = await _piece(business_a)

    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a,
            content_piece_id=piece,
            renderings={"linkedin": {"body": "   ", "hashtags": []}},
            session=session,
        )

    assert created == []


# --------------------------------------------------------------------------- #
# the calendar read
# --------------------------------------------------------------------------- #


async def test_the_window_never_hides_an_untimed_post(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The whole reason the read does not filter on `scheduled_at` alone: a queued post
    has no date, so a plain BETWEEN would hide exactly the backlog the calendar exists
    to help place."""
    piece = await _piece(business_a)
    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        await svc.schedule_post(
            created[0].id, when=datetime(2026, 9, 1, 9, tzinfo=UTC), session=session
        )

    async with business_session(business_a) as session:
        # A window that contains neither post's date.
        listed = await svc.list_posts(
            business_a,
            session=session,
            since=datetime(2027, 1, 1, tzinfo=UTC),
            until=datetime(2027, 2, 1, tzinfo=UTC),
        )

    statuses = {post.status for post in listed}
    assert statuses == {SocialPostStatus.QUEUED.value}, (
        "the scheduled post is out of the window; the untimed one must still show"
    )


async def test_the_read_carries_the_article_title_for_the_calendar_label(
    scoped_sessions: None, business_a: UUID
) -> None:
    piece = await _piece(business_a)
    async with business_session(business_a) as session:
        await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        listed = await svc.list_posts(business_a, session=session)

    assert all(post.piece_title == "Notdienst in Koblenz" for post in listed)


async def test_another_business_sees_none_of_these_posts(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """RLS, asserted rather than assumed — the house rule for every business-scoped
    table."""
    piece = await _piece(business_a)
    async with business_session(business_a) as session:
        await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )

    async with business_session(business_b) as session:
        assert await svc.list_posts(business_b, session=session) == []


# --------------------------------------------------------------------------- #
# schedule and cancel
# --------------------------------------------------------------------------- #


async def test_scheduling_sets_the_time_and_the_status(
    scoped_sessions: None, business_a: UUID
) -> None:
    piece = await _piece(business_a)
    when = datetime.now(UTC) + timedelta(days=2)

    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        record = await svc.schedule_post(created[0].id, when=when, session=session)

    assert record.status == SocialPostStatus.SCHEDULED.value
    assert record.scheduled_at is not None


async def test_unscheduling_returns_it_to_the_queue(
    scoped_sessions: None, business_a: UUID
) -> None:
    """How an owner takes a date off without cancelling the post."""
    piece = await _piece(business_a)
    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        await svc.schedule_post(
            created[0].id, when=datetime.now(UTC) + timedelta(days=1), session=session
        )
        record = await svc.schedule_post(created[0].id, when=None, session=session)

    assert record.status == SocialPostStatus.QUEUED.value
    assert record.scheduled_at is None


async def test_a_naive_schedule_time_is_refused(scoped_sessions: None, business_a: UUID) -> None:
    """The same refusal `automation_service` makes: a naive instant shifts every slot by
    the host's offset and the result still looks like a valid schedule."""
    piece = await _piece(business_a)
    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            await svc.schedule_post(created[0].id, when=datetime(2026, 9, 1, 9, 0), session=session)


async def test_cancelling_keeps_the_row(scoped_sessions: None, business_a: UUID) -> None:
    """ "We decided not to post this" is worth seeing later, and a deleted row cannot be
    told apart from one that was never created."""
    piece = await _piece(business_a)
    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        record = await svc.cancel_post(created[0].id, session=session)

    assert record.status == SocialPostStatus.CANCELLED.value


async def test_a_cancelled_channel_can_be_queued_again(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The idempotency check excludes cancelled rows deliberately: changing your mind
    back must be possible."""
    piece = await _piece(business_a)
    async with business_session(business_a) as session:
        created = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )
        await svc.cancel_post(created[0].id, session=session)
        again = await svc.queue_posts(
            business_a, content_piece_id=piece, renderings=RENDERINGS, session=session
        )

    assert [post.platform for post in again] == [created[0].platform]


# --------------------------------------------------------------------------- #
# publish — the honesty tests
# --------------------------------------------------------------------------- #


async def _one_post(business_id: UUID) -> UUID:
    piece = await _piece(business_id)
    async with business_session(business_id) as session:
        created = await svc.queue_posts(
            business_id,
            content_piece_id=piece,
            renderings={"linkedin": RENDERINGS["linkedin"]},
            session=session,
        )
    return created[0].id


async def test_a_real_publish_marks_the_post_published(
    scoped_sessions: None, business_a: UUID
) -> None:
    post_id = await _one_post(business_a)
    publisher = _Publisher(_outcome(OutcomeStatus.SUCCEEDED, ref="urn:li:share:123"))

    async with business_session(business_a) as session:
        result = await svc.publish_post(
            post_id,
            business_id=business_a,
            approved_by="user-1",
            actuator=publisher,
            store=_Store(),
            session=session,
        )

    assert result.status == SocialPostStatus.PUBLISHED.value
    assert result.simulated is False
    assert result.post.published_at is not None
    assert result.external_ref == "urn:li:share:123"


async def test_a_simulated_publish_leaves_the_post_where_it_was(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The most important test here.

    With no real `SocialPublisher` configured — every deployment today, because posting
    for other people is gated on App Review — the actuator returns SUCCEEDED with
    `fake=True`. Writing `published` for that would empty an owner's calendar through a
    publisher that does not exist, and the posts would be gone with nothing sent.
    """
    post_id = await _one_post(business_a)
    publisher = _Publisher(_outcome(OutcomeStatus.SUCCEEDED, fake=True, ref="fake://linkedin/1"))

    async with business_session(business_a) as session:
        result = await svc.publish_post(
            post_id,
            business_id=business_a,
            approved_by="user-1",
            actuator=publisher,
            store=_Store(),
            session=session,
        )

    assert result.simulated is True
    assert result.status == SocialPostStatus.QUEUED.value, "still there to send for real"
    assert result.post.published_at is None


async def test_no_publisher_at_all_is_a_refusal_that_names_app_review(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Nothing was attempted, so nothing went wrong — and the owner needs to know why
    rather than seeing a button that did nothing."""
    post_id = await _one_post(business_a)

    async with business_session(business_a) as session:
        result = await svc.publish_post(
            post_id,
            business_id=business_a,
            approved_by="user-1",
            actuator=None,
            store=_Store(),
            session=session,
        )

    assert result.status == SocialPostStatus.REFUSED.value
    assert result.simulated is True
    assert result.error is not None and "App Review" in result.error


async def test_a_platform_refusal_and_a_platform_failure_are_recorded_apart(
    scoped_sessions: None, business_a: UUID
) -> None:
    """`actions.status` already draws this distinction and for the same reason: a retry
    is correct for a failure and a policy violation for a refusal."""
    refused_id = await _one_post(business_a)
    async with business_session(business_a) as session:
        refused = await svc.publish_post(
            refused_id,
            business_id=business_a,
            approved_by="u",
            actuator=_Publisher(_outcome(OutcomeStatus.REFUSED)),
            store=_Store(),
            session=session,
        )
    assert refused.status == SocialPostStatus.REFUSED.value

    failed_id = await _one_post(business_a)
    async with business_session(business_a) as session:
        failed = await svc.publish_post(
            failed_id,
            business_id=business_a,
            approved_by="u",
            actuator=_Publisher(_outcome(OutcomeStatus.FAILED)),
            store=_Store(),
            session=session,
        )
    assert failed.status == SocialPostStatus.FAILED.value


async def test_a_published_post_cannot_be_published_again(
    scoped_sessions: None, business_a: UUID
) -> None:
    """Pressing the button twice must not send twice. `actuate`'s idempotency key is the
    lower guarantee; this is the one that stops the attempt."""
    post_id = await _one_post(business_a)
    async with business_session(business_a) as session:
        await svc.publish_post(
            post_id,
            business_id=business_a,
            approved_by="u",
            actuator=_Publisher(_outcome(OutcomeStatus.SUCCEEDED, ref="r")),
            store=_Store(),
            session=session,
        )

    async with business_session(business_a) as session:
        with pytest.raises(svc.PostNotPublishableError):
            await svc.publish_post(
                post_id,
                business_id=business_a,
                approved_by="u",
                actuator=_Publisher(_outcome(OutcomeStatus.SUCCEEDED, ref="r2")),
                store=_Store(),
                session=session,
            )


async def test_the_idempotency_key_is_the_posts_id_not_its_content(
    scoped_sessions: None, business_a: UUID
) -> None:
    """A retry after a timeout must return the first result rather than posting twice,
    and the content is identical across a retry AND a deliberate re-post — only the row
    can tell them apart."""
    post_id = await _one_post(business_a)
    publisher = _Publisher(_outcome(OutcomeStatus.SUCCEEDED, ref="r"))

    async with business_session(business_a) as session:
        await svc.publish_post(
            post_id,
            business_id=business_a,
            approved_by="u",
            actuator=publisher,
            store=_Store(),
            session=session,
        )

    assert publisher.calls[0].key == f"social_post:{post_id}"


async def test_the_approver_reaches_the_actuation(scoped_sessions: None, business_a: UUID) -> None:
    """`Actuation.approved_by` is not optional: an audit row with no approver is a
    publish nobody authorised."""
    post_id = await _one_post(business_a)
    publisher = _Publisher(_outcome(OutcomeStatus.SUCCEEDED, ref="r"))

    async with business_session(business_a) as session:
        await svc.publish_post(
            post_id,
            business_id=business_a,
            approved_by="the-owner-id",
            actuator=publisher,
            store=_Store(),
            session=session,
        )

    assert publisher.calls[0].approved_by == "the-owner-id"


async def test_a_post_from_another_business_is_not_found(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """RLS makes it invisible, and the service reports that as not-found rather than
    letting a route treat "not yours" as "nothing to do"."""
    post_id = await _one_post(business_a)

    async with business_session(business_b) as session:
        with pytest.raises(svc.PostNotFoundError):
            await svc.publish_post(
                post_id,
                business_id=business_b,
                approved_by="u",
                actuator=_Publisher(_outcome(OutcomeStatus.SUCCEEDED, ref="r")),
                store=_Store(),
                session=session,
            )
