"""Feedback routes: rate a piece, see what that produced, approve it.

Three endpoints, and the shape of the set is the point:

* ``POST /content/{id}/feedback``          -- rate one produced piece
* ``GET  /businesses/{id}/proposals``      -- the rules AWAITING approval
* ``POST /proposals/{id}/approve``         -- approving is what applies it

**The business is derived from the session, never accepted from the client.**
``current_business`` is imported rather than reimplemented: "which business is
this caller acting for" is an authorisation decision, and a codebase with two
answers to it eventually has one that is wrong. Where a business id appears in a
path it is CHECKED against the derived one and answered with 404 on a mismatch --
not 403. Whether a resource exists is itself information, and a caller who cannot
see it has no legitimate way to know its id.

**A refused request writes nothing.** Every write happens inside
``business_session``, which owns the transaction, so an ``HTTPException`` raised
from inside the block rolls the whole thing back. That is also why the services
flush rather than commit.

**Recording a rejection distils immediately, and says what it proposed.** The
alternative -- a nightly job -- means the owner rejects three pieces and sees
nothing until tomorrow, which is exactly long enough for them to conclude the
feedback box does nothing. Distilling is a query and at most one insert, and it is
deterministic, so doing it inline costs a few milliseconds and no model call. The
response carries the proposed rules so the UI can say "we noticed a pattern" at
the moment the pattern appeared.

**Nothing here applies a preference.** The proposals are ``status="proposed"``
rows; only the approve route reaches business memory. See
``services/feedback_service.py`` for why that boundary exists.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth import CurrentUser
from backend.app.api.runs import current_business
from backend.app.db.session import business_session
from backend.app.services import feedback_service
from backend.app.services.feedback_service import (
    AXES,
    ContentPieceNotFoundError,
    InvalidAxesError,
    InvalidVerdictError,
    ProposalNotFoundError,
)

router = APIRouter(prefix="/api/v1", tags=["feedback"])

#: ``{"onBrand": 4}`` from a TypeScript client means ``on_brand``. The rest of the
#: wire is camelCase (the field names are aliased), and a dict KEY is the one place
#: an alias generator does not reach -- so the mapping is explicit here rather than
#: leaving the client to guess which half of the payload changes case. Derived from
#: ``AXES`` so a new axis cannot be added to the rubric and forgotten here.
_AXIS_ALIASES: dict[str, str] = {to_camel(axis): axis for axis in AXES}

#: A callable that opens a transaction scoped to one business. A dependency rather
#: than a direct import so a test can supply one that never touches Postgres --
#: and so the 422 tests can prove a malformed rubric never opens a transaction at
#: all.
BusinessSessionOpener = Callable[[UUID], AbstractAsyncContextManager[AsyncSession]]


def get_business_session_opener() -> BusinessSessionOpener:
    """The real, row-level-security-scoped session opener. Overridden in tests."""
    return business_session


BusinessId = Annotated[UUID, Depends(current_business)]
OpenSession = Annotated[BusinessSessionOpener, Depends(get_business_session_opener)]


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class FeedbackRequest(CamelModel):
    """One rating.

    ``verdict`` is a ``Literal`` so the OpenAPI schema documents the two values and
    a generated client cannot invent a third. The service validates it again, and
    that is not redundant: the graph's ``REVIEW`` node calls the service directly,
    with no FastAPI in the path, and the rubric must mean the same thing there.

    ``axes`` is a partial rubric by design -- someone saying "the voice is wrong"
    should not have to invent an SEO score to say it. Ranges are enforced by the
    service, which refuses an out-of-range rating rather than clamping it.
    """

    verdict: Literal["approved", "rejected"]
    axes: dict[str, int] = Field(default_factory=dict)
    reject_reason: str | None = None

    @field_validator("axes", mode="before")
    @classmethod
    def _accept_camel_case_axis_names(cls, value: object) -> object:
        """Map ``onBrand`` to ``on_brand`` and leave everything else alone.

        Unknown keys are passed THROUGH rather than dropped, so the service refuses
        them by name. Silently discarding an axis nobody defined would let a client
        believe a rating was stored when it was not.
        """
        if not isinstance(value, dict):
            return value
        return {_AXIS_ALIASES.get(str(key), str(key)): item for key, item in value.items()}


class FeedbackResponse(CamelModel):
    """What the rating produced, if anything.

    ``proposedRules`` is almost always empty, and that is the honest normal: a rule
    is proposed only once a theme has recurred. The UI shows it when it is not
    empty and says nothing when it is.
    """

    proposed_rules: list[str]


class ProposalOut(CamelModel):
    id: UUID
    rule: str
    #: The reject reasons this rule was distilled from -- the evidence that makes
    #: the proposal reviewable instead of an assertion.
    derived_from: list[str]
    status: str
    created_at: datetime


class ApprovalResponse(CamelModel):
    rule: str
    #: Always true on a 200. Present so the client asserts on a field rather than
    #: on an empty body, and so "approved but not applied" can never be a state the
    #: API reports.
    applied: bool


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


_NOT_FOUND = _error(
    "not_found",
    "That item does not exist, or is not part of your business.",
)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.post(
    "/content/{content_piece_id}/feedback",
    response_model=FeedbackResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Rate one produced piece on the four-axis rubric",
)
async def submit_feedback(
    content_piece_id: UUID,
    payload: FeedbackRequest,
    user: CurrentUser,
    business_id: BusinessId,
    open_session: OpenSession,
) -> FeedbackResponse:
    async with open_session(business_id) as session:
        try:
            await feedback_service.record(
                content_piece_id,
                business_id,
                verdict=payload.verdict,
                axes=payload.axes,
                reject_reason=payload.reject_reason,
                # Recorded so a two-person business can see who said what. Optional
                # in the schema because a rating may also arrive from the graph.
                user_id=user.id,
                session=session,
            )
        except (InvalidAxesError, InvalidVerdictError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_error("invalid_rating", str(exc)),
            ) from exc
        except ContentPieceNotFoundError as exc:
            # 404 rather than 403: see the module docstring.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from exc

        # Only a rejection can carry a reason, so only a rejection can change what
        # the reasons add up to.
        proposed = (
            await feedback_service.distil(business_id, session=session)
            if payload.verdict == "rejected"
            else []
        )

    return FeedbackResponse(proposed_rules=proposed)


@router.get(
    "/businesses/{business_id}/proposals",
    response_model=list[ProposalOut],
    response_model_by_alias=True,
    summary="Brand rules distilled from feedback, awaiting approval",
)
async def list_proposals(
    business_id: UUID,
    scoped_business_id: BusinessId,
    open_session: OpenSession,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description=(
                "Which proposals to return. The default is the pending ones; pass "
                "an empty value for every status, which is what an audit view wants."
            ),
        ),
    ] = "proposed",
) -> list[ProposalOut]:
    if business_id != scoped_business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    async with open_session(business_id) as session:
        proposals = await feedback_service.list_proposals(
            business_id, session=session, status=status_filter
        )

    return [
        ProposalOut(
            id=proposal.id,
            rule=proposal.rule,
            derived_from=list(proposal.derived_from),
            status=proposal.status,
            created_at=proposal.created_at,
        )
        for proposal in proposals
    ]


@router.post(
    "/proposals/{proposal_id}/approve",
    response_model=ApprovalResponse,
    response_model_by_alias=True,
    summary="Approve a proposed rule, which is what puts it into business memory",
)
async def approve_proposal(
    proposal_id: UUID,
    business_id: BusinessId,
    open_session: OpenSession,
) -> ApprovalResponse:
    """Approval and application are one transaction.

    Two writes with a gap between them would allow a state where a rule is
    approved but not in force -- and the owner would have been told it was.
    """
    async with open_session(business_id) as session:
        try:
            rule = await feedback_service.approve_proposal(
                proposal_id, business_id, session=session
            )
        except ProposalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from exc

    return ApprovalResponse(rule=rule, applied=True)


__all__ = ["BusinessSessionOpener", "get_business_session_opener", "router"]
