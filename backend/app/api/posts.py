"""The post queue over HTTP: the calendar's read, and the publish button's write.

Follows `api/runs.py`'s conventions: `CamelModel` responses rendered by alias, the
business derived from the SESSION rather than accepted from the body, and a session
opener a test can override so the route can be exercised without Postgres.

**The publish route is the only place in the product where an owner causes an external
side effect directly.** Everything else that publishes runs inside the graph, behind
the REVIEW gate. So two things are true of it that are not true of the read routes:
the approver is recorded as the authenticated user rather than a policy string, and the
response distinguishes a SIMULATED send from a real one, because with no publisher
configured — every deployment today — nothing leaves the process and a screen that said
"sent" would be lying.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from backend.app.api.auth import CurrentUser
from backend.app.api.runs import current_business
from backend.app.db.session import business_session
from backend.app.services import social_post_service
from backend.app.services.social_post_service import (
    PostNotFoundError,
    PostNotPublishableError,
    PostRecord,
    PublishResult,
)

router = APIRouter(prefix="/api/v1/posts", tags=["posts"])

#: The most a calendar read returns. A month of a weekly cadence across four channels is
#: about 16 rows, so 200 is a wide margin — and the ceiling exists because `limit` is not
#: a query parameter here: an unbounded read is the one that gets slow silently.
MAX_POSTS: Final = 200


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PostOut(CamelModel):
    """One post on the calendar."""

    id: UUID
    content_piece_id: UUID
    platform: str
    body: str
    hashtags: list[str]
    status: str
    #: `None` for a `queued` post: rendered, not yet timed. The calendar shows those in
    #: their own column rather than dropping them, which is the whole reason the read
    #: does not filter them out.
    scheduled_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    piece_title: str | None


class PostListResponse(CamelModel):
    posts: list[PostOut]


class ScheduleRequest(CamelModel):
    """When it should go out. `null` un-schedules it back to the queue."""

    when: datetime | None = None


class PublishResponse(CamelModel):
    post: PostOut
    status: str
    #: True when nothing left this process. The screen MUST render this differently from
    #: a real send — see the module docstring.
    simulated: bool
    external_ref: str | None
    error: str | None


def get_business_session_opener() -> Any:
    """The tenant-scoped session opener. Overridden in tests."""
    return business_session


def get_actuator_resolver() -> Any:
    """How an action type becomes an actuator. Overridden in tests.

    The same resolver the graph's executor builds, imported rather than rebuilt: a second
    resolver would be a second answer to "is social publishing real on this deployment",
    and the honest reporting of `simulated` depends on there being one.
    """
    from backend.app.services.run_executor import _build_actuator_resolver

    return _build_actuator_resolver()


def get_action_store() -> Any:
    """The `actions` ledger the idempotency key is claimed in. Overridden in tests."""
    from backend.app.db.adapters.action_store import PostgresActionStore

    return PostgresActionStore()


def _out(record: PostRecord) -> PostOut:
    return PostOut(
        id=record.id,
        content_piece_id=record.content_piece_id,
        platform=record.platform,
        body=record.body,
        hashtags=list(record.hashtags),
        status=record.status,
        scheduled_at=record.scheduled_at,
        published_at=record.published_at,
        created_at=record.created_at,
        piece_title=record.piece_title,
    )


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.get(
    "",
    response_model=PostListResponse,
    response_model_by_alias=True,
    summary="The post queue, for the calendar",
)
async def list_posts(
    business_id: Annotated[UUID, Depends(current_business)],
    open_session: Annotated[Any, Depends(get_business_session_opener)],
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
) -> PostListResponse:
    """Every post in the window, plus every untimed one.

    The untimed ones are always included: a `queued` post has no `scheduled_at`, so a
    plain range filter would hide exactly the backlog the calendar exists to help place.
    """
    async with open_session(business_id) as session:
        records = await social_post_service.list_posts(
            business_id, session=session, since=since, until=until, limit=MAX_POSTS
        )
    return PostListResponse(posts=[_out(record) for record in records])


@router.post(
    "/{post_id}/schedule",
    response_model=PostOut,
    response_model_by_alias=True,
    summary="Give a post a time, or take it away",
)
async def schedule(
    post_id: UUID,
    payload: ScheduleRequest,
    business_id: Annotated[UUID, Depends(current_business)],
    open_session: Annotated[Any, Depends(get_business_session_opener)],
) -> PostOut:
    async with open_session(business_id) as session:
        try:
            record = await social_post_service.schedule_post(
                post_id, when=payload.when, session=session
            )
        except PostNotFoundError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=_error("post_not_found", "No such post.")
            ) from exc
        except PostNotPublishableError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=_error(
                    "post_already_published",
                    "A published post keeps the time it went out.",
                ),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_error("naive_datetime", str(exc)),
            ) from exc
    return _out(record)


@router.post(
    "/{post_id}/cancel",
    response_model=PostOut,
    response_model_by_alias=True,
    summary="Take a post off the calendar without deleting it",
)
async def cancel(
    post_id: UUID,
    business_id: Annotated[UUID, Depends(current_business)],
    open_session: Annotated[Any, Depends(get_business_session_opener)],
) -> PostOut:
    async with open_session(business_id) as session:
        try:
            record = await social_post_service.cancel_post(post_id, session=session)
        except PostNotFoundError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=_error("post_not_found", "No such post.")
            ) from exc
        except PostNotPublishableError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=_error("post_already_published", "A published post cannot be cancelled."),
            ) from exc
    return _out(record)


@router.post(
    "/{post_id}/publish",
    response_model=PublishResponse,
    response_model_by_alias=True,
    summary="Send one post now",
)
async def publish(
    post_id: UUID,
    user: CurrentUser,
    business_id: Annotated[UUID, Depends(current_business)],
    open_session: Annotated[Any, Depends(get_business_session_opener)],
    actuator_for: Annotated[Any, Depends(get_actuator_resolver)],
    store: Annotated[Any, Depends(get_action_store)],
) -> PublishResponse:
    """The publish button.

    **The approver is the authenticated user's id, not a policy string.** Inside the
    graph, `state.POLICY_APPROVER` records that a human gate was passed by a surface
    which did not identify the human; here the surface does, so the real id goes in the
    ledger. That is the difference between an audit row and a shrug.

    A 200 does NOT mean the post went out. `simulated` and `status` say what happened,
    and with no publisher configured the honest answer is a refusal that names App
    Review — which is why this route returns a body on the refusal path rather than an
    error status: nothing went wrong, and a 4xx would send the screen down its error
    branch for a working, correctly-refused publish.
    """
    async with open_session(business_id) as session:
        try:
            result: PublishResult = await social_post_service.publish_post(
                post_id,
                business_id=business_id,
                approved_by=str(user.id),
                actuator=actuator_for(social_post_service.SOCIAL_POST_ACTION),
                store=store,
                session=session,
            )
        except PostNotFoundError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=_error("post_not_found", "No such post.")
            ) from exc
        except PostNotPublishableError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=_error(
                    "post_not_publishable",
                    f"A post with status {exc.status!r} cannot be published.",
                ),
            ) from exc

    return PublishResponse(
        post=_out(result.post),
        status=result.status,
        simulated=result.simulated,
        external_ref=result.external_ref,
        error=result.error,
    )
