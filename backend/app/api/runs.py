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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from backend.app.api.auth import CurrentUser
from backend.app.db.adapters.run_store import PostgresRunStore
from backend.app.services.review_service import RunReview, project_review
from backend.app.services.run_executor import RunExecutor
from backend.app.services.run_service import RunRecord, RunService

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

#: How long the stream waits between polls of the store. Short enough that the timeline
#: feels live, long enough that an idle stream is not a busy loop.
POLL_INTERVAL_S = 0.4

#: A stream cannot wait forever for a run that will never move. Ended cleanly so the
#: client can decide to reconnect rather than hanging on a dead socket.
MAX_STREAM_SECONDS = 15 * 60

#: States that mean "no further events are coming".
TERMINAL = frozenset({"awaiting_approval", "done", "failed", "partial"})


async def current_business(user: CurrentUser) -> UUID:
    """The business this caller is acting for.

    One business per user today, so it is derived rather than passed: accepting a
    business_id from the client would be an authorisation decision made by the client.
    """
    from sqlalchemy import select

    from backend.app.db.models import Business
    from backend.app.db.session import session

    async with session() as s:
        business_id = (
            await s.execute(select(Business.id).where(Business.owner_id == user.id).limit(1))
        ).scalar_one_or_none()

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
