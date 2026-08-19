"""The cost dashboard's endpoint.

One route, and two decisions in it that are worth stating because both look like
oversights until they are explained.

**It is behind the platform-admin gate, like the rest of `/developer`.** Spend is not
secret from the business that incurred it, but this screen lives in the developer
console next to model routing, and one gate for one console is easier to reason about
than two. The consequence -- a plain owner cannot see their own spend anywhere -- is a
real product gap and is recorded as such rather than papered over here.

**The data is the CALLER'S OWN business, and cannot be anything else.** ``model_usage``
is business-scoped and under row-level security, and the runtime connects as the
restricted role, so this endpoint reads through ``business_session`` and sees exactly one
tenant's rows. A cross-business "platform revenue" view is therefore NOT available from
here and must not be added by loosening the session: it would need a
``SECURITY DEFINER`` function written for that purpose, reviewed on its own merits. The
response says which business it is for, so the screen can say so too.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from backend.app.api.admin_models import require_admin
from backend.app.api.runs import current_business
from backend.app.db.models import User
from backend.app.services.cost_service import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    CostReport,
    cost_report,
)

router = APIRouter(prefix="/api/v1/admin/cost", tags=["admin"])


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CostOut(CamelModel):
    """The report plus the tenancy it was read under.

    ``business_id`` is echoed deliberately: a spend figure with no statement of whose
    spend it is invites being read as a platform total.
    """

    business_id: UUID
    report: CostReport


@router.get(
    "",
    response_model=CostOut,
    response_model_by_alias=True,
    summary="Model spend for the caller's own business",
)
async def get_cost(
    _: Annotated[User, Depends(require_admin)],
    business_id: Annotated[UUID, Depends(current_business)],
    window_days: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_WINDOW_DAYS,
            alias="windowDays",
            description="Reporting window in days.",
        ),
    ] = DEFAULT_WINDOW_DAYS,
) -> CostOut:
    report = await cost_report(business_id, window_days=window_days)
    return CostOut(business_id=business_id, report=report)
