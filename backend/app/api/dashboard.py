"""The owner's dashboard: one GET, and the honesty rules that shape its body.

Three decisions, and the first two are about what is NOT here.

**No window parameter.** The cost screen takes one because spend is a rate; these are
lifetime counts of things that happened, and a dashboard that silently reported "clicks
in the last 30 days" as "clicks" would understate every business whose posts are older
than the default. When a trend is wanted it needs its own endpoint and its own series.

**`null` survives the wire, and it is not the same as `0`.**
``services/dashboard_service`` exists to keep "we have not measured this" separate from
"we measured zero", so every optional figure is typed ``| None`` here rather than being
defaulted on the way out. A response model that coerced them would undo the whole point
of the service, and the screen would print a measurement nobody took.

**Money leaves as a STRING.** ``spendUsd`` is rendered with
:func:`~backend.app.llm.pricing.format_usd`, which is what the cost screen uses, so the
two agree digit for digit. JSON has one number type and it is binary floating point:
serialising a ``Decimal`` as a JSON number is how ``0.30000000`` becomes
``0.30000000000000004`` in the one path that exists to talk about money accurately.

The read is tenant-scoped by the DATABASE, not by a ``WHERE`` clause: every statement in
the service is unqualified and ``business_session`` sets the row-level-security GUC, so
this endpoint can only ever see one business's rows. The business is derived from the
session and never accepted from the client, which is why ``current_business`` is imported
rather than reimplemented -- a codebase with two answers to "who is this caller acting
for" eventually has one that is wrong.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.runs import current_business
from backend.app.db.session import business_session
from backend.app.llm.pricing import format_usd
from backend.app.services.dashboard_service import DashboardSummary, read_dashboard

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

#: A callable that opens a transaction scoped to one business. A dependency rather than a
#: direct import so a test can supply one that never touches Postgres -- the route's
#: shape is then provable without a database, and the SQL is proved separately in
#: ``tests/db/test_dashboard_service.py`` where real row-level security applies.
BusinessSessionOpener = Callable[[UUID], AbstractAsyncContextManager[AsyncSession]]


def get_business_session_opener() -> BusinessSessionOpener:
    """The real, row-level-security-scoped session opener. Overridden in tests."""
    return business_session


BusinessId = Annotated[UUID, Depends(current_business)]
OpenSession = Annotated[BusinessSessionOpener, Depends(get_business_session_opener)]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelClicksOut(CamelModel):
    """Clicks earned by one channel. ``link_hub`` is the hub link, which has no channel."""

    channel: str
    clicks: int


class DashboardOut(CamelModel):
    """Every tile, plus the business it was read for and what could not be shown.

    ``businessId`` is echoed for the same reason the cost report echoes it: a figure with
    no statement of whose figure it is invites being read as a platform total.

    ``gaps`` is not decoration. It is the list of things this product cannot measure and
    why, in the owner's words, and the screen is expected to render it -- a dashboard that
    dropped it would present the tiles it happens to have as the whole picture.
    """

    business_id: UUID
    #: `null` when no tracked link has ever been minted. `0` means links exist and nobody
    #: has clicked, which is a measurement; `null` is not.
    clicks_total: int | None
    clicks_by_channel: list[ChannelClicksOut]
    #: Reported separately rather than folded into `clicksTotal`, which counts only human
    #: clicks -- see `db/adapters/lead_store.py`: a link previewer is not a person.
    clicks_from_bots: int
    runs_total: int
    runs_awaiting_approval: int
    runs_partial: int
    leads_total: int
    #: A decimal string, never a JSON number. `null` when no model call was ever
    #: recorded, which on a fake-provider deployment is the ordinary state and is not
    #: "$0.00".
    spend_usd: str | None
    seo_problems: int | None
    seo_pages_audited: int | None
    #: True when the audit hit its page cap, so "no duplicate titles" is a sample rather
    #: than a finding.
    seo_truncated: bool
    #: A SAMPLE of what a few models say, never a census, and `null` unless a run
    #: actually probed with a real provider.
    share_of_voice: float | None
    gaps: list[str]


def _out(business_id: UUID, summary: DashboardSummary) -> DashboardOut:
    """Project the service's dataclass onto the wire.

    Kept as a function so the ``None``-preserving mapping is written once: every
    optional field is passed straight through, and only ``spend_usd`` is transformed.
    """
    return DashboardOut(
        business_id=business_id,
        clicks_total=summary.clicks_total,
        clicks_by_channel=[
            ChannelClicksOut(channel=row.channel, clicks=row.clicks)
            for row in summary.clicks_by_channel
        ],
        clicks_from_bots=summary.clicks_from_bots,
        runs_total=summary.runs_total,
        runs_awaiting_approval=summary.runs_awaiting_approval,
        runs_partial=summary.runs_partial,
        leads_total=summary.leads_total,
        spend_usd=None if summary.spend_usd is None else format_usd(summary.spend_usd),
        seo_problems=summary.seo_problems,
        seo_pages_audited=summary.seo_pages_audited,
        seo_truncated=summary.seo_truncated,
        share_of_voice=summary.share_of_voice,
        gaps=list(summary.gaps),
    )


@router.get(
    "",
    response_model=DashboardOut,
    response_model_by_alias=True,
    summary="The dashboard figures for the caller's own business",
)
async def get_dashboard(business_id: BusinessId, open_session: OpenSession) -> DashboardOut:
    async with open_session(business_id) as session:
        summary = await read_dashboard(business_id, session=session)
    return _out(business_id, summary)


__all__ = ["BusinessSessionOpener", "get_business_session_opener", "router"]
