"""Automation over HTTP: the switch that was missing.

Two routes, and the business is derived from the SESSION rather than taken from the path
— the convention `api/memory.py` states at length and every owner-facing route here
follows:

* ``GET /api/v1/automation``  -- what the scheduler will do, and when
* ``PUT /api/v1/automation``  -- set it, or switch it off

**Why this exists at all, stated plainly because it is the whole point.** The worker has
read `automation_settings` since `e410d9c` — cadence, channels, goal, `next_run_at` — and
nothing anywhere could write it. A row could only come into existence by hand in SQL, so
"the scheduler executes automations" was true and "a business can automate its marketing"
was not. This is the second half.

**PUT, not PATCH, and the difference is a safety property.** There is at most one row per
business and this is a small form, so a full replacement means the stored schedule is
always exactly what somebody last saw and pressed save on. A partial update over a
schedule invites the worst version of this bug: two fields arrive, the other three keep
values from an edit the owner has forgotten, and the automation publishes at a time that
appears nowhere on their screen.

**`nextRunAt` is read-only on the wire.** It is a cache of `automation_service`'s
arithmetic and is computed on every save; accepting one would make the client
authoritative for one write and the pure function authoritative for all the others.
`lastRunAt` and `pausedReason` are read-only for the same class of reason — they are the
worker's record of what happened, not the owner's instruction.

**A refused request writes nothing.** The write happens inside `business_session`, which
owns the transaction, so an `HTTPException` raised from inside the block rolls it back —
the shape `api/memory.py` and `api/feedback.py` already use.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.runs import current_business
from backend.app.db.models import Cadence
from backend.app.db.session import business_session
from backend.app.engines.channel.specs import CHANNEL_SPECS
from backend.app.services import automation_settings_service as automation
from backend.app.services.automation_service import MAX_DAY_OF_WEEK, MAX_HOUR
from backend.app.services.automation_settings_service import (
    MAX_GOAL_LENGTH,
    AutomationRecord,
    InvalidScheduleError,
)
from backend.app.worker.scheduler import DEFAULT_INTERVAL_S

router = APIRouter(prefix="/api/v1/automation", tags=["automation"])

BusinessSessionOpener = Callable[[UUID], AbstractAsyncContextManager[AsyncSession]]


def get_business_session_opener() -> BusinessSessionOpener:
    """The tenant-scoped session opener. Overridden in tests.

    Its own dependency rather than a reuse of another module's, so overriding one
    module's database access in a test cannot silently redirect this one's.
    """
    return business_session


def get_clock() -> Callable[[], datetime]:
    """Now, as a dependency, because the stored schedule is computed from it.

    Injected rather than called inline for the same reason `save_automation` takes `now`:
    a test asserting which Thursday a save lands on cannot depend on the day it runs.
    """
    return lambda: datetime.now(UTC)


BusinessId = Annotated[UUID, Depends(current_business)]
OpenSession = Annotated[BusinessSessionOpener, Depends(get_business_session_opener)]
Clock = Annotated[Callable[[], datetime], Depends(get_clock)]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AutomationOut(CamelModel):
    """What the automation panel renders.

    `configured` and `enabled` are both present and are NOT the same question.
    `configured` is whether a row exists at all — false means the values beside it are
    the defaults a first save would apply, not a schedule somebody chose. `enabled` is
    whether the scheduler will act, which is the owner's switch AND the absence of a
    system pause; a panel reporting only `mode` would show "on" for an automation that
    has stopped itself.

    `nextRunAt` is the honest centrepiece, in the same spirit as memory's `promptLines`:
    it is the exact instant the worker will compare against, so the screen states when
    the next run is due rather than asserting that automation "is working".
    """

    business_id: UUID
    configured: bool
    enabled: bool
    mode: str
    cadence: str
    day_of_week: int
    hour: int
    timezone: str
    channels: list[str]
    goal_template: str | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    #: Why the system stopped by itself, verbatim. Rendered to the owner, because a
    #: paused automation with no stated reason is indistinguishable from a broken one.
    paused_reason: str | None
    #: The vocabulary the client may send, so a channel picker cannot offer one the
    #: server refuses. Sent rather than hardcoded in the browser for the same reason
    #: memory sends `editableFields`.
    known_channels: list[str]
    known_cadences: list[str]
    max_goal_length: int
    #: Seconds between the worker's passes. What lets the screen say a due run starts
    #: within a minute of its slot instead of promising the second.
    poll_interval_seconds: float
    #: Which fields this route accepts. The three read-only ones above are derived by
    #: the server and would be silently ignored if posted, and silently ignored is the
    #: one behaviour a form must never have.
    editable_fields: list[str]


class AutomationRequest(CamelModel):
    """The whole instruction. Every field, every time — see the module docstring.

    Bounds are declared here AND enforced by the service, deliberately: the schema's 422
    is what stops an obviously malformed body reaching a database round-trip, and the
    service's refusal is what protects a caller that reaches it directly (the same
    doubling `api/leads.py` uses for its size cap). Where they disagree the service
    wins, because it is the one the scheduler shares.
    """

    enabled: bool
    cadence: str = automation.DEFAULT_CADENCE
    #: Monday=0 .. Sunday=6, matching `date.weekday()`. Named in the description because
    #: the two neighbouring conventions differ and picking the wrong one does not fail —
    #: it publishes on the wrong day until somebody checks a calendar.
    day_of_week: int = Field(default=automation.DEFAULT_DAY_OF_WEEK, ge=0, le=MAX_DAY_OF_WEEK)
    hour: int = Field(default=automation.DEFAULT_HOUR, ge=0, le=MAX_HOUR)
    timezone: str = Field(default=automation.DEFAULT_TIMEZONE, min_length=1, max_length=64)
    #: Empty means "nobody chose", which an automated run resolves to the default set —
    #: the same reading `runs.channels` has. It does NOT mean "no channels".
    channels: list[str] = Field(default_factory=list)
    goal_template: str | None = Field(default=None, max_length=MAX_GOAL_LENGTH)


def _out(record: AutomationRecord) -> AutomationOut:
    """One place that turns a record into the wire shape.

    Both routes return through here, so a GET and the PUT that just wrote it cannot
    describe the same automation differently.
    """
    return AutomationOut(
        business_id=record.business_id,
        configured=record.configured,
        enabled=record.enabled,
        mode=record.mode,
        cadence=record.cadence,
        day_of_week=record.day_of_week,
        hour=record.hour,
        timezone=record.timezone,
        channels=list(record.channels),
        goal_template=record.goal_template,
        next_run_at=record.next_run_at,
        last_run_at=record.last_run_at,
        paused_reason=record.paused_reason,
        known_channels=sorted(CHANNEL_SPECS),
        known_cadences=[cadence.value for cadence in Cadence],
        max_goal_length=MAX_GOAL_LENGTH,
        poll_interval_seconds=DEFAULT_INTERVAL_S,
        editable_fields=[
            "enabled",
            "cadence",
            "dayOfWeek",
            "hour",
            "timezone",
            "channels",
            "goalTemplate",
        ],
    )


@router.get(
    "",
    response_model=AutomationOut,
    response_model_by_alias=True,
    summary="What the scheduler will do for this business, and when",
)
async def get_automation(business_id: BusinessId, open_session: OpenSession) -> AutomationOut:
    """Reads, and creates nothing.

    A business that has never configured automation gets the defaults with
    `configured: false`. Materialising the row on read would put a business into the
    scheduler's work table for a decision nobody made.
    """
    async with open_session(business_id) as session:
        return _out(await automation.load_automation(business_id, session=session))


@router.put(
    "",
    response_model=AutomationOut,
    response_model_by_alias=True,
    summary="Set this business's automation, or switch it off",
)
async def put_automation(
    payload: AutomationRequest,
    business_id: BusinessId,
    open_session: OpenSession,
    clock: Clock,
) -> AutomationOut:
    """200 with the whole automation, including the recomputed `nextRunAt`.

    Not 204: the response is the server's own account of what the scheduler will now do,
    which is the only way the screen can show a slot it did not compute itself — and two
    open tabs then agree after a save rather than each rendering its own optimism.

    Idempotent. The same body twice stores the same row twice and moves nothing; the
    second `nextRunAt` may differ from the first only because time passed between them,
    which is the correct answer rather than a stale one.
    """
    async with open_session(business_id) as session:
        try:
            record = await automation.save_automation(
                business_id,
                session=session,
                enabled=payload.enabled,
                cadence=payload.cadence,
                day_of_week=payload.day_of_week,
                hour=payload.hour,
                timezone=payload.timezone,
                channels=payload.channels,
                goal_template=payload.goal_template,
                now=clock(),
            )
        except InvalidScheduleError as exc:
            # 422 with the service's own sentence: it names the bound it refused and the
            # reader is the person in the form. Replacing it with a generic string here
            # would throw away the useful half.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_schedule", "message": str(exc)},
            ) from exc
    return _out(record)
