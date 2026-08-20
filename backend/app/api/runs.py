"""Runs: start one, read its state, stream its timeline.

Three decisions shape this module.

**Starting a run returns 202, not 200.** The work has not happened; 200 would imply it had.
The client gets an id and opens the stream.

**The stream is resumable by sequence number.** Without `?after=`, a client that dropped its
connection would have to replay the whole run — and on a long run that is both slow and
visually wrong, because the timeline would animate from the beginning again.

**The stream terminates.** A run that has finished or is waiting for a human is not going to
produce more events, so the response ends. An SSE endpoint that never closes leaks a
connection on every reload, and browsers hold only a handful per origin.

A cross-business run id returns 404 rather than 403: whether a run exists is itself
information, and the caller has no legitimate way to know that id.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth import CurrentUser
from backend.app.db.adapters.run_store import PostgresRunStore
from backend.app.services.review_service import RunReview, project_review
from backend.app.services.run_executor import RunExecutor
from backend.app.services.run_service import (
    DEFAULT_RUN_LIST_LIMIT,
    MAX_RUN_LIST_LIMIT,
    RunRecord,
    RunService,
    decode_cursor,
    encode_cursor,
)

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

#: How long the stream waits between polls of the store. Short enough that the timeline
#: feels live, long enough that an idle stream is not a busy loop.
POLL_INTERVAL_S = 0.4

#: A stream cannot wait forever for a run that will never move. Ended cleanly so the
#: client can decide to reconnect rather than hanging on a dead socket.
MAX_STREAM_SECONDS = 15 * 60

#: States that mean "no further events are coming".
TERMINAL = frozenset({"awaiting_approval", "done", "failed", "partial"})


async def business_for_user(user_id: UUID, *, session: AsyncSession) -> UUID | None:
    """The business this user owns, or ``None``.

    Its own function because there are now two callers with different correct
    responses to a missing business: this module's dependency raises 409 (a run cannot
    be started without one), while `/auth/me` reports ``null`` (an account mid-signup
    is a fact about the account, not an error). One query, two readings -- and one
    place to change when a user can own more than one business.

    Takes the session rather than opening one, so the caller decides. `/auth/me` passes
    the request's session, which is what a test can override; a function reaching for
    the module-level engine would make every auth test open a real connection to
    answer a question about a fixture.
    """
    from sqlalchemy import select

    from backend.app.db.models import Business

    found: UUID | None = (
        await session.execute(select(Business.id).where(Business.owner_id == user_id).limit(1))
    ).scalar_one_or_none()
    return found


async def current_business(user: CurrentUser) -> UUID:
    """The business this caller is acting for.

    One business per user today, so it is derived rather than passed: accepting a
    business_id from the client would be an authorisation decision made by the client.
    """
    from backend.app.db.session import session

    async with session() as db:
        business_id = await business_for_user(user.id, session=db)

    if business_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "no_business",
                "message": "This account has no business yet. Complete onboarding first.",
            },
        )
    return business_id


def get_executor(request: Request) -> RunExecutor:
    """The application's ONE executor, cached on `app.state`.

    One per application, not one per request: it owns the concurrency limit and the
    set of in-flight tasks, and a fresh instance per request would enforce neither --
    every caller would get its own allowance of four, which is no allowance at all,
    and each new instance would drop the previous one's task references and let live
    runs be garbage-collected.

    Built lazily here rather than in `create_app` so this module owns its own
    machinery: the executor needs nothing but a way to make a `RunService`, which is
    right here, and wiring it into application startup would put a runs-specific
    detail into the app factory for no gain. The cost is that a first request pays the
    construction, which is a semaphore and an empty set.

    Overridable in tests through the usual `dependency_overrides`.
    """
    executor = getattr(request.app.state, "run_executor", None)
    if isinstance(executor, RunExecutor):
        return executor

    executor = RunExecutor(
        service_factory=lambda business_id: RunService(PostgresRunStore(business_id))
    )
    request.app.state.run_executor = executor
    return executor


async def get_run_service(
    business_id: Annotated[UUID, Depends(current_business)],
) -> RunService:
    """The real service, scoped to this caller's business. Overridden in tests.

    The tenant is injected HERE rather than resolved inside the store, which is what
    fixes the 404-on-every-run bug: the store used to look the owning business up from
    the run row on an unscoped session, and under FORCE row-level security that read
    returns nothing. Every route already depends on `current_business`, so the answer
    was available all along -- see `PostgresRunStore` for the full account.
    """
    return RunService(PostgresRunStore(business_id))


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StartRunRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=500)
    surfaces: list[str] = Field(default_factory=lambda: ["google"])


class StartRunResponse(CamelModel):
    run_id: UUID
    state: str


class EventOut(CamelModel):
    seq: int
    node: str
    status: str
    payload: dict[str, object]
    at: str


class RunOut(CamelModel):
    run_id: UUID
    goal: str
    state: str
    current_node: str | None
    resumed_count: int
    finished_reason: str | None
    events: list[EventOut]


class RunSummaryOut(CamelModel):
    """One row of the runs list.

    ``RunOut`` minus the timeline and plus the time it started. The events are what make a
    single run's page worth loading and what make a LIST of runs expensive: twenty runs of
    forty events each is eight hundred rows fetched to render twenty lines of text.

    ``finished_reason`` is on it deliberately. A run here can legitimately end ``partial``
    -- the configured credential cannot reach the mid tier -- and a list that showed the
    state without the reason would report a terminal state the owner cannot account for,
    which reads as a broken product rather than as a missing credential.
    """

    run_id: UUID
    goal: str
    state: str
    current_node: str | None
    resumed_count: int
    finished_reason: str | None
    created_at: str


class RunListResponse(CamelModel):
    runs: list[RunSummaryOut]
    #: Pass back as ``?cursor=`` for the next page, or ``null`` when this is the last.
    #:
    #: Present rather than a total count, and that is the honest shape: counting every
    #: run a business has ever had costs a second query to render a number nobody acts
    #: on, while "there is more" is exactly what the button needs to know.
    next_cursor: str | None = None


async def _require_own_run(run_id: UUID, business_id: UUID, service: RunService) -> RunRecord:
    """The run, or 404.

    One definition rather than one per route: "may this caller see this run" is an
    authorisation decision, and three copies of it is three chances for one to drift.
    404 for both "absent" and "another business's", on purpose — whether a run exists is
    itself information, and a caller has no legitimate way to hold an id from elsewhere.
    """
    run = await service.get(run_id)
    if run is None or run.business_id != business_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "No such run."},
        )
    return run


@router.post("", response_model=StartRunResponse, response_model_by_alias=True, status_code=202)
async def start_run(
    payload: StartRunRequest,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
    executor: Annotated[RunExecutor, Depends(get_executor)],
) -> StartRunResponse:
    run = await service.start(business_id=business_id, goal=payload.goal)
    # 202 and then work in the background. Before this, the row was created and
    # nothing ever advanced it: `run_graph` was reachable only from tests, so a
    # started run stayed `queued` forever and the timeline had nothing to show.
    executor.submit(run.id, business_id, payload.goal)
    return StartRunResponse(run_id=run.id, state=run.state)


@router.get(
    "",
    response_model=RunListResponse,
    response_model_by_alias=True,
    summary="This business's runs, newest first",
)
async def list_runs(
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
    response: Response,
    limit: Annotated[int, Query(ge=1, le=MAX_RUN_LIST_LIMIT)] = DEFAULT_RUN_LIST_LIMIT,
    cursor: Annotated[str | None, Query(max_length=120)] = None,
) -> RunListResponse:
    """The list an owner reaches a run from.

    Its absence was a hole rather than a simplification: ``POST /api/v1/runs`` returned an
    id, and nothing else in the product could tell you what ids existed -- so a run whose
    page you navigated away from was gone, and the timeline screen was reachable only by
    pasting an id in by hand.

    **Newest first, and that is part of the contract.** A run takes minutes and its id is
    not memorable, so "the one I just started" has to be the top row.

    **Paginated by cursor, not by offset.** It used to be a cap: a business past the
    ceiling could not reach its older runs at all. A keyset cursor is what makes the
    boundary exact while runs are still being started -- an offset shifts by a row every
    time a new run appears above it, which silently skips one.

    **No timeline, no checkpoint.** The store selects named columns, so the draft is never
    even read -- see :meth:`PostgresRunStore.list_runs`. A single run's events are a
    separate request because that is the one worth paying for.

    ``no-store``, because the goals are the customer's own words about their business and
    this sits behind a session cookie. Same rule as the leads list.

    Runs are filtered against the caller's business here as well as by RLS in the store,
    for the same reason ``_require_own_run`` compares owners on the single-run routes: this
    is the one endpoint that returns many rows and therefore the one where a scoping
    mistake leaks a set rather than a single guessed id.
    """
    response.headers["Cache-Control"] = "no-store"

    before = None
    if cursor:
        try:
            before = decode_cursor(cursor)
        except ValueError as exc:
            # 422 rather than a silent first page: a client whose cursor is malformed
            # wants to be told, because quietly restarting the list looks exactly like
            # the "older runs" button not working.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "bad_cursor", "message": "That page cursor is not valid."},
            ) from exc

    # One more than asked for, which is how "is there another page" is answered without
    # a second query and without claiming a total.
    found = await service.recent(limit=min(limit + 1, MAX_RUN_LIST_LIMIT + 1), before=before)
    mine = [run for run in found if run.business_id == business_id]
    page = mine[:limit]
    return RunListResponse(
        runs=[
            RunSummaryOut(
                run_id=run.id,
                goal=run.goal,
                state=run.state,
                current_node=run.current_node,
                resumed_count=run.resumed_count,
                finished_reason=run.finished_reason,
                created_at=run.created_at.isoformat(),
            )
            for run in page
        ],
        next_cursor=(
            encode_cursor((page[-1].created_at, page[-1].id))
            if page and len(mine) > len(page)
            else None
        ),
    )


@router.get("/{run_id}", response_model=RunOut, response_model_by_alias=True)
async def get_run(
    run_id: UUID,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunOut:
    run = await _require_own_run(run_id, business_id, service)

    events = await service.events(run_id)
    return RunOut(
        run_id=run.id,
        goal=run.goal,
        state=run.state,
        current_node=run.current_node,
        resumed_count=run.resumed_count,
        finished_reason=run.finished_reason,
        events=[
            EventOut(
                seq=e.seq, node=e.node, status=e.status, payload=e.payload, at=e.at.isoformat()
            )
            for e in events
        ],
    )


@router.get(
    "/{run_id}/review",
    response_model=RunReview,
    response_model_by_alias=True,
    summary="One run's output, as the four review tabs render it",
)
async def get_review(
    run_id: UUID,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunReview:
    """The draft, the SEO findings, the social posts and the AI answer blocks.

    Separate from ``GET /{run_id}`` on purpose. The timeline is polled every couple of
    seconds while a run is live, and the draft HTML is the largest thing the run
    produces — putting it in the polled payload would re-send the whole page on every
    tick. The timeline answers "where is it up to"; this answers "what came out".

    A run with nothing to show still returns 200 with populated notes rather than 404:
    the run exists, and "GENERATE has not run yet" is the answer, not an error.
    """
    run = await _require_own_run(run_id, business_id, service)
    return project_review(run.checkpoint)


@router.post(
    "/{run_id}/resume",
    response_model=StartRunResponse,
    response_model_by_alias=True,
    status_code=202,
    summary="Continue a run that stopped without finishing",
)
async def resume_run(
    run_id: UUID,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
    executor: Annotated[RunExecutor, Depends(get_executor)],
) -> StartRunResponse:
    """Pick a run back up from its checkpoint.

    This exists because the executor runs IN THE API PROCESS. If that process is
    restarted or killed mid-run, the row is left saying `running` and nothing will
    advance it — a distributed worker would notice and retry, and there isn't one.
    Runs were made resumable for exactly this case, so the recovery path is real; what
    is missing is something that walks stalled runs automatically.

    Deliberately refuses a run that has already reached a terminal state. Re-running a
    finished run would spend money to overwrite work somebody may have approved, and
    "resume" is not a word anyone expects to mean that. A run awaiting approval is also
    refused: it is not stalled, it is waiting for a person, and resuming it would step
    past the review gate that exists to stop exactly that.
    """
    run = await _require_own_run(run_id, business_id, service)

    if run.state in {"done", "failed", "partial"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_finished",
                "message": f"This run already finished ({run.state}) and cannot be resumed.",
            },
        )
    if executor.is_running(run_id):
        # Found by driving the real API: a run that is ACTIVELY executing was accepted
        # for resume, which would put a second executor on the same run -- two writers
        # racing on `next_seq` and on the checkpoint, i.e. exactly the corruption the
        # ordered event drain exists to prevent, reintroduced one level up.
        #
        # Refusing the DB state `running` outright would be wrong: after a process
        # restart the row still says `running` and nothing is driving it, which is the
        # case resume exists for. Only the executor can tell those apart, and only for
        # its own process -- see `RunExecutor.is_running`.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_already_executing",
                "message": "This run is already executing.",
            },
        )
    if run.state == "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_awaiting_approval",
                "message": "This run is waiting for a human decision, not stalled.",
            },
        )

    executor.submit(run.id, business_id, run.goal, resume=True)
    return StartRunResponse(run_id=run.id, state="running")


@router.get("/{run_id}/events")
async def stream_events(
    run_id: UUID,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
    after: int = 0,
) -> StreamingResponse:
    """Server-sent events for one run, resumable from `after`."""
    # Checked before the stream opens: a 404 must be an HTTP status, not the first
    # frame of a 200 response body.
    await _require_own_run(run_id, business_id, service)

    async def generate() -> AsyncIterator[str]:
        seen = after
        waited = 0.0
        while True:
            for event in await service.events(run_id, after_seq=seen):
                seen = event.seq
                frame = {
                    "seq": event.seq,
                    "node": event.node,
                    "status": event.status,
                    "payload": event.payload,
                    "at": event.at.isoformat(),
                }
                yield f"id: {event.seq}\nevent: node\ndata: {json.dumps(frame)}\n\n"

            current = await service.get(run_id)
            state = current.state if current else "failed"

            if state in TERMINAL:
                closing = {"state": state, "reason": current.finished_reason if current else None}
                yield f"event: end\ndata: {json.dumps(closing)}\n\n"
                return

            if waited >= MAX_STREAM_SECONDS:
                # Ended cleanly rather than left hanging: the client can reconnect with
                # ?after= and lose nothing.
                yield 'event: end\ndata: {"state": "timeout"}\n\n'
                return

            await asyncio.sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Proxies that buffer will make a live timeline arrive in one lump.
            "X-Accel-Buffering": "no",
        },
    )
