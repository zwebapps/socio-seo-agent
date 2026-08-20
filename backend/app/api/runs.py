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
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth import CurrentUser
from backend.app.core.config import get_settings
from backend.app.db.adapters.run_store import PostgresRunStore
from backend.app.services.review_service import (
    ExportPack,
    RunReview,
    project_export_pack,
    project_review,
    render_export_markdown,
)
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
#:
#: `rejected` belongs here for the plainest reason: nothing will ever move a rejected run,
#: so without it the stream would sit out the full `MAX_STREAM_SECONDS` waiting for events
#: that cannot come -- fifteen minutes of held connection per reload of a finished screen.
TERMINAL = frozenset({"awaiting_approval", "done", "failed", "partial", "rejected"})

#: How long a rejection reason may be. The floor exists because a reasonless rejection is
#: the one input this product can do nothing with, and the reviewer is the only person who
#: will ever know why; ten characters costs a real sentence nothing and stops `no`/`bad`.
#:
#: The ceiling is 240 and NOT 255, which is the width of `runs.finished_reason`, because
#: `clamp_reason` truncates SILENTLY. Shortening a provider stack trace is cosmetic;
#: shortening a person's stated reason is not. So the 422 here is the only length refusal a
#: human can ever meet, and the clamp stays a backstop for machine-authored reasons.
REJECT_REASON_MIN = 10
REJECT_REASON_MAX = 240


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


class RejectRunRequest(BaseModel):
    """A reviewer's refusal. The reason is REQUIRED, and bounded at both ends.

    Whitespace is collapsed BEFORE the bounds are applied, so 400 spaces is not a reason
    and neither is `"          "`. A `mode="before"` validator runs ahead of the string
    constraints, which is what makes the collapse count rather than merely tidy up
    something already accepted.
    """

    reason: str = Field(min_length=REJECT_REASON_MIN, max_length=REJECT_REASON_MAX)

    @field_validator("reason", mode="before")
    @classmethod
    def _collapse_whitespace(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class RunDecisionResponse(CamelModel):
    """What a completed human decision looks like coming back.

    Its own model rather than `StartRunResponse`, and `finished_reason` is the point: the
    response echoes the STORED reason, so the screen renders what was persisted rather than
    what it happened to send.
    """

    run_id: UUID
    state: str
    finished_reason: str | None


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


def _hub_url(business_id: UUID) -> str:
    """The business's bio-link hub, built from CONFIGURATION and never from the request.

    `Host` is caller-controlled, so building this from the request would let a poisoned
    header point every CTA in a downloaded file at somebody else's domain — the same
    reasoning `landing_service` and `api/links.py` both record.

    The UUID form of the handle rather than the readable slug, deliberately:
    `GET /go/{slug}` accepts either and resolves both permanently (see `api/links.py`),
    and the id is already in hand — reading the slug would add a query to a route whose
    whole job is a projection.
    """
    return f"{get_settings().public_base_url.rstrip('/')}/go/{business_id}"


#: Named so the two branches below cannot disagree about the filename, and so the JSON
#: branch's absence of a disposition is visibly deliberate.
_EXPORT_FILENAME = "export-pack-{run_id}.md"


@router.get(
    "/{run_id}/export",
    response_model=ExportPack,
    response_model_by_alias=True,
    summary="One run's export pack: paste-ready copy per channel, as JSON or Markdown",
    responses={
        200: {
            "description": "The pack. JSON by default; Markdown with `?format=markdown`.",
            "content": {"application/json": {}, "text/markdown": {}},
        }
    },
)
async def get_export(
    run_id: UUID,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
    response: Response,
    fmt: Annotated[Literal["json", "markdown"], Query(alias="format")] = "json",
) -> ExportPack | Response:
    """Everything needed to publish this run BY HAND, and every gap in it named.

    This is Tier 3 in docs/CHANNELS.md section 2 — the tier that "works on day one, on
    every platform, forever" — and it is the only publishing path this build actually
    has. So it is a first-class payload rather than a copy button: per-channel copy that
    is ready to paste, the character count against that channel's editorial target AND
    its platform ceiling, the hashtag count against the cap, whether a link in the body
    is clickable there at all, the landing page, the answer blocks, and the bio-link hub.

    **Two renderings of one payload.** JSON for the screen, Markdown for a person —
    `?format=markdown` returns the same pack as a downloadable file, because Tier 3's
    value is that somebody pastes it somewhere and nobody can paste an escaped JSON
    string. An unrecognised `format` is a 422 rather than a silent fallback to JSON: a
    client asking for something else has a bug, and answering the question it did not ask
    hides it. The Markdown branch returns a `Response` directly, which FastAPI passes
    through untouched while `response_model` keeps the JSON shape in the schema.

    **`Content-Disposition: attachment`, with an ASCII filename.** The pack is the
    customer's unpublished content and a browser must not render it as a page — and the
    filename carries the run id so two downloads do not overwrite each other. The id is a
    UUID, so the filename needs no RFC 5987 escaping; nothing caller-controlled reaches
    the header.

    **`no-store` on both branches.** This is the customer's own draft copy behind a
    session cookie, so it must not land in a shared cache. Same rule as the runs list and
    the leads list.

    A run with nothing rendered yet is 200 with populated notes, not 404 and not an empty
    pack: the run exists, and "REPACK has not completed" is the answer.
    """
    run = await _require_own_run(run_id, business_id, service)
    pack = project_export_pack(run.checkpoint, hub_url=_hub_url(business_id))

    if fmt == "markdown":
        return Response(
            content=render_export_markdown(pack),
            # charset stated: the copy is German more often than not, and a file served
            # as text/markdown with no charset is decoded by guesswork.
            media_type="text/markdown; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{_EXPORT_FILENAME.format(run_id=run_id)}"'
                ),
            },
        )

    response.headers["Cache-Control"] = "no-store"
    return pack


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

    # `rejected` is in this set because leaving it out is not a cosmetic omission: a
    # rejected run is not `awaiting_approval` any more either, so it would fall past BOTH
    # refusals and reach `executor.submit(..., resume=True)` -- which continues the graph
    # from the checkpoint straight through EXPORT and PUBLISHES the very draft a human just
    # refused. The review gate would be bypassable by pressing the button next to it.
    if run.state in {"done", "failed", "partial", "rejected"}:
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


@router.post(
    "/{run_id}/approve",
    response_model=StartRunResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Approve a parked run and let it publish",
)
async def approve_run(
    run_id: UUID,
    user: CurrentUser,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
    executor: Annotated[RunExecutor, Depends(get_executor)],
) -> StartRunResponse:
    """The human decision the whole machine is built around, finally reachable.

    `REVIEW` is an interrupt: the graph stops there, and EXPORT and MEASURE sit AFTER it
    in `ORDER` and are unreachable without passing through. Until this route existed,
    nothing in the product could pass through — `await_approval` wrote a state and no
    actor, and `resume` deliberately refuses a parked run ("waiting for a human decision,
    not stalled"). So a run could be reviewed and never published, and EXPORT's refusal
    for a missing approver fired on every real run. Correctly, and uselessly.

    **The approver is the AUTHENTICATED USER, and that is the point of doing it here.**
    It lands in `approved_by`, which reaches `Actuation.approved_by` and is persisted on
    every `actions` row — so "who authorised this post" is answerable from the audit
    ledger months later. A client-supplied approver would be the client making an
    authorisation decision, which is the same mistake `current_business` exists to avoid.

    202, not 200: approving starts work that takes minutes. The state returned is
    `running`, not `done`.

    Deliberately NOT idempotent-by-silence: a second approval of an already-running run
    is a 409 rather than a no-op, because the caller believes they are approving something
    and the honest answer is that it is already going.
    """
    run = await _require_own_run(run_id, business_id, service)

    if run.state != "awaiting_approval":
        # Every other state is a different sentence, and lumping them together would
        # send somebody to check the wrong thing.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_not_awaiting_approval",
                "message": (
                    f"This run is {run.state}, not waiting for approval. "
                    "Only a run parked at the review gate can be approved."
                ),
            },
        )

    if not await service.record_approval(run_id, approver=f"user:{user.id}"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "no_checkpoint",
                "message": (
                    "This run has no checkpoint, so there is nothing to approve. It was "
                    "parked before it produced anything."
                ),
            },
        )

    executor.submit(run.id, business_id, run.goal, resume=True)
    return StartRunResponse(run_id=run.id, state="running")


@router.post(
    "/{run_id}/reject",
    response_model=RunDecisionResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
    summary="Refuse a parked run's output, terminally",
)
async def reject_run(
    run_id: UUID,
    payload: RejectRunRequest,
    business_id: Annotated[UUID, Depends(current_business)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunDecisionResponse:
    """The other half of the review gate: a reviewer says no, and it sticks.

    **200, not 202.** Approve is 202 because it starts minutes of work. Rejecting starts
    none — one UPDATE and the run is over — so the work IS complete when this returns, and
    202 would be a claim that something is still happening.

    **Terminal and not reversible.** The recovery from a rejection is a NEW run, which
    re-derives from current documents instead of republishing what was refused. So a second
    reject, and an approve-after-reject, are both the existing 409
    `run_not_awaiting_approval` — no new vocabulary, and never a silent no-op: the caller
    believes they are deciding something, and the honest answer is that it is already
    decided.

    **The rejecter is deliberately NOT recorded, and this route takes no `CurrentUser` for
    exactly that reason.** `approved_by` exists because an approval authorises an outward
    publish and lands on every `actions` row; a rejection authorises nothing and sends
    nothing. There is one user per business today, so a `rejected_by` column would store
    what `business_id` already implies. A session is still required — `current_business`
    resolves through the authenticated user, so an anonymous caller is 401 here as
    everywhere else.

    **Nothing is written to `feedback`.** `Feedback.content_piece_id` is NOT NULL, and the
    only thing that ever creates a `content_pieces` row is the `publish.page` actuator at
    EXPORT — which is AFTER this gate. So no run has a content piece at review, approved or
    rejected, and hanging a rejection on one would mean inventing the row to hang it on.
    The rejection is recorded on the run itself: `state` plus `finished_reason`, and that is
    the whole record.

    **`runs.checkpoint` is left INTACT.** The review tabs are projected from it, and a
    refused draft is still evidence of work the owner paid for — clearing it would make the
    screen unable to show what was refused. This route also never touches the executor:
    there is nothing to start, and nothing to stop.

    **No `no_checkpoint` refusal**, the one deliberate divergence from approve. Approve
    needs a checkpoint because the approval is written INTO it; a rejection writes nothing
    there, and a run parked having produced nothing is precisely what a reviewer should be
    able to dismiss. A reviewer must always be able to say no.

    **No `run_already_executing` guard either.** After `await_approval` the executor's task
    returns and makes no further write, so the only window in which a parked run is still
    live is one where nothing can overwrite the rejection. Guarding it would refuse a
    legitimately parked run for a race that cannot happen.
    """
    run = await _require_own_run(run_id, business_id, service)

    if run.state != "awaiting_approval":
        # The SAME code as approve, because it is the same condition -- which lets one
        # client handler serve both buttons. Only the sentence differs.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_not_awaiting_approval",
                "message": (
                    f"This run is {run.state}, not waiting for approval. "
                    "Only a run parked at the review gate can be rejected."
                ),
            },
        )

    await service.finish(run_id, outcome="rejected", reason=payload.reason)

    # Read back rather than echo the request: the response's job is to report what was
    # PERSISTED, so the screen renders the stored reason and not the one it typed.
    stored = await _require_own_run(run_id, business_id, service)
    return RunDecisionResponse(
        run_id=stored.id, state=stored.state, finished_reason=stored.finished_reason
    )


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
