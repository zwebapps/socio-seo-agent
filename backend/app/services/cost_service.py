"""The cost dashboard's read model: what this business has spent on model calls.

Built on ``model_usage`` -- the table `docs/ARCHITECTURE.md` section 8 already names as
the cost ledger -- and on ``runs`` for the per-run ceiling to compare against. No new
table: everything the dashboard shows is already in the schema.

Four properties, each of which is easy to get wrong in a way that still renders:

* **``Decimal`` from the database to the wire, serialised as a string.** Every sum here
  comes back from ``Numeric(12, 8)`` as ``Decimal`` and is never passed through
  ``float``. The API renders it with ``str()``. A float in a money path is a bug even
  when the number looks right, because it stops looking right at the third decimal
  place once you sum a few thousand rows.
* **Every read goes through ``business_session``.** ``model_usage`` and ``runs`` are
  business-scoped and under row-level security, so a query on an unscoped session
  returns zero rows -- which looks exactly like "you have spent nothing". That failure
  mode is why the tenant GUC is set by the session helper and never by a caller.
* **An empty ledger is reported as empty, not as zero.** See
  :attr:`CostReport.ledger_wired`. ``$0.00`` and "nothing has been recorded" are
  different statements, and a dashboard that renders the second as the first is lying
  about spend -- the same mistake ``admin_models.available_models`` already refuses to
  make for unpriced models.
* **The window is bounded.** A dashboard that scans the whole ledger gets slower every
  day it is useful.

Aggregation is done in SQL rather than in Python on purpose: pulling every usage row
into the process to sum it would move the cost of this screen from the database's
indexes to the API's memory, and the numbers are the only thing we want back.

The same ledger also answers a question that is not a dashboard question: **may this
business start another run at all.** :func:`monthly_spend_usd` and
:func:`over_monthly_cap` are the read and the boundary behind the per-business ceiling of
`docs/ARCHITECTURE.md` section 7.4; the refusal itself lives in ``api/runs.py``, because
what to do about a breached ceiling is an API decision and reading a ledger is not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from backend.app.agents.state import DEFAULT_MAX_USD
from backend.app.db.models import ModelUsage, Run
from backend.app.db.session import business_session
from backend.app.llm.pricing import format_usd, is_priced

#: Default reporting window. Thirty days covers a monthly bill conversation, which is
#: the question this screen exists to answer.
DEFAULT_WINDOW_DAYS: Final = 30

#: Hard ceiling on the window, so a hand-written query string cannot turn this into a
#: full-table scan.
MAX_WINDOW_DAYS: Final = 365

#: How many rows each breakdown returns. A long tail of one-call models is noise on a
#: dashboard; the totals still account for it.
BREAKDOWN_LIMIT: Final = 20

#: The window the per-business ceiling is measured over, in days.
#:
#: The SAME window the dashboard reports, and that is the point of aliasing it rather
#: than writing 30 again: an owner who is refused a run reads the reason, opens the cost
#: screen, and must find the number they were just quoted. A calendar month would put a
#: different figure on each surface and make every refusal a support ticket -- and it
#: would also hand a business at its ceiling a full allowance again at midnight on the
#: 1st, which is a reset rather than a control.
MONTHLY_CAP_WINDOW_DAYS: Final = DEFAULT_WINDOW_DAYS


class _Wire(BaseModel):
    """camelCase on the wire, snake_case in Python -- as everywhere else in this API.

    Copied from ``review_service._Wire`` rather than left as a plain ``BaseModel``,
    because these models are returned NESTED inside an API response: the outer model's
    alias generator does not reach into them, so without this the whole report went out
    in snake_case while its wrapper went out in camelCase, and the screen read
    ``undefined`` for every field. Caught by calling the endpoint, not by any type.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class SpendRow(_Wire):
    """One line of a breakdown. ``usd`` is a string, deliberately."""

    key: str
    calls: int
    tokens_in: int
    tokens_out: int
    usd: str
    #: False when this row's model has no price-table entry, so its ``usd`` is
    #: structurally zero rather than genuinely zero. Only meaningful on the by-model
    #: breakdown; the other breakdowns mix models and leave it None.
    priced: bool | None = None


class DailySpend(_Wire):
    day: date
    calls: int
    usd: str


class RunSpend(_Wire):
    """One run's recorded spend against its own ceiling."""

    run_id: UUID
    usd: str
    cap_usd: str
    #: True when recorded spend has reached or passed the run's own ``budget_usd``.
    #: Worth surfacing separately from the number: a run at its cap did not fail, it
    #: stopped and returned what it had, which is a different event from an error.
    at_cap: bool


class CostReport(_Wire):
    """Everything the cost screen renders, for one business and one window."""

    window_days: int
    since: datetime

    calls: int
    tokens_in: int
    tokens_out: int
    total_usd: str

    by_model: list[SpendRow]
    by_node: list[SpendRow]
    by_prompt_version: list[SpendRow]
    by_day: list[DailySpend]

    #: The ceiling the runtime enforces per run, as a string. Read from the runs
    #: themselves where any exist, because a run stores the cap it was created with and
    #: that is the number it was actually held to; falls back to the code default.
    default_run_cap_usd: str
    runs_in_window: int
    runs_at_cap: int
    top_runs: list[RunSpend]

    #: False when the ledger has no rows at all for this business while runs DO exist.
    #: That combination is not "spent nothing" -- it means nothing is recording usage,
    #: and the screen has to say so instead of showing a confident $0.00. Derived rather
    #: than hardcoded so it stops warning by itself the day a recorder is wired in.
    ledger_wired: bool
    #: Plain-language account of what the numbers above are and are not.
    message: str


def _window(days: int) -> tuple[int, datetime]:
    """Clamp the requested window and return it with its start instant."""
    clamped = max(1, min(int(days), MAX_WINDOW_DAYS))
    return clamped, datetime.now(UTC) - timedelta(days=clamped)


def _usd(value: Decimal | None) -> str:
    """Render a possibly-absent SUM as a money string.

    The only thing this adds over :func:`~backend.app.llm.pricing.format_usd` is the
    NULL case: ``SUM`` over no rows is SQL NULL, not zero, and ``str(None)`` would put the
    literal text ``None`` on a dashboard. Quantisation and fixed-point notation come from
    the shared formatter, so this screen and the sampling screen cannot drift.
    """
    return format_usd(value if value is not None else Decimal("0"))


#: The dimension a breakdown groups by. Typed as the mapped attribute rather than
#: `object`, so passing something that is not a column of this table is a type error here
#: instead of a runtime one inside SQLAlchemy's overload resolution.
BreakdownColumn = InstrumentedAttribute[str] | InstrumentedAttribute[str | None]


def _breakdown(column: BreakdownColumn, since: datetime) -> Select[Any]:
    """A grouped spend query over one dimension of `model_usage`."""
    return (
        select(
            column,
            func.count().label("calls"),
            func.coalesce(func.sum(ModelUsage.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(ModelUsage.tokens_out), 0).label("tokens_out"),
            func.coalesce(func.sum(ModelUsage.usd), Decimal("0")).label("usd"),
        )
        .where(ModelUsage.created_at >= since)
        .group_by(column)
        .order_by(func.coalesce(func.sum(ModelUsage.usd), Decimal("0")).desc())
        .limit(BREAKDOWN_LIMIT)
    )


async def _rows(
    session: AsyncSession, column: BreakdownColumn, since: datetime, *, priced: bool
) -> list[SpendRow]:
    result = await session.execute(_breakdown(column, since))
    rows: list[SpendRow] = []
    for key, calls, tokens_in, tokens_out, usd in result.all():
        name = str(key) if key is not None else "(not recorded)"
        rows.append(
            SpendRow(
                key=name,
                calls=int(calls),
                tokens_in=int(tokens_in),
                tokens_out=int(tokens_out),
                usd=_usd(usd),
                priced=is_priced(name) if priced and key is not None else None,
            )
        )
    return rows


async def cost_report(business_id: UUID, *, window_days: int = DEFAULT_WINDOW_DAYS) -> CostReport:
    """Aggregate this business's model spend over the window.

    One session for every query, because they are one consistent picture: two sessions
    could straddle a write and report a total that does not match its own breakdown.
    """
    days, since = _window(window_days)

    async with business_session(business_id) as s:
        totals = (
            await s.execute(
                select(
                    func.count().label("calls"),
                    func.coalesce(func.sum(ModelUsage.tokens_in), 0),
                    func.coalesce(func.sum(ModelUsage.tokens_out), 0),
                    func.coalesce(func.sum(ModelUsage.usd), Decimal("0")),
                ).where(ModelUsage.created_at >= since)
            )
        ).one()

        by_model = await _rows(s, ModelUsage.model, since, priced=True)
        by_node = await _rows(s, ModelUsage.node, since, priced=False)
        by_prompt = await _rows(s, ModelUsage.prompt_version, since, priced=False)

        day_col = func.date_trunc("day", ModelUsage.created_at).label("day")
        daily = (
            await s.execute(
                select(
                    day_col,
                    func.count().label("calls"),
                    func.coalesce(func.sum(ModelUsage.usd), Decimal("0")),
                )
                .where(ModelUsage.created_at >= since)
                .group_by(day_col)
                .order_by(day_col)
            )
        ).all()

        # Per-run spend is a JOIN, not a read of `runs.used_usd`: the ledger is the
        # authority on what was actually charged, and the run row's own copy is a
        # denormalisation that nothing currently maintains. Joining also means a run
        # with no usage rows correctly does not appear as a zero-cost run.
        run_rows = (
            await s.execute(
                select(
                    Run.id,
                    Run.budget_usd,
                    func.coalesce(func.sum(ModelUsage.usd), Decimal("0")).label("usd"),
                )
                .join(ModelUsage, ModelUsage.run_id == Run.id)
                .where(Run.created_at >= since)
                .group_by(Run.id, Run.budget_usd)
                .order_by(func.coalesce(func.sum(ModelUsage.usd), Decimal("0")).desc())
                .limit(BREAKDOWN_LIMIT)
            )
        ).all()

        runs_in_window = (
            await s.execute(select(func.count()).select_from(Run).where(Run.created_at >= since))
        ).scalar_one()

        # The cap a run was actually held to is the one stored on it. Reported from the
        # most recent run rather than from the code constant, because an operator asking
        # "what is the ceiling" wants the number in force, not the default it came from.
        latest_cap = (
            await s.execute(
                select(Run.budget_usd)
                .where(Run.created_at >= since)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    calls = int(totals[0])
    top_runs = [
        RunSpend(
            run_id=run_id,
            usd=_usd(usd),
            cap_usd=_usd(cap),
            at_cap=(usd or Decimal("0")) >= cap,
        )
        for run_id, cap, usd in run_rows
    ]

    ledger_wired = calls > 0 or runs_in_window == 0
    if not ledger_wired:
        message = (
            f"{runs_in_window} run(s) in this window recorded NO model_usage rows, so "
            "every figure below is zero because nothing is writing the ledger -- not "
            "because nothing was spent. Treat these numbers as unavailable, not as "
            "$0.00."
        )
    elif calls == 0:
        message = "No model calls in this window."
    else:
        unpriced = [r.key for r in by_model if r.priced is False]
        message = f"{calls} model call(s) over {days} day(s), summed from the model_usage ledger."
        if unpriced:
            message += (
                " Spend is understated: "
                + ", ".join(unpriced)
                + " has no price-table entry, so its calls contribute $0.00."
            )

    return CostReport(
        window_days=days,
        since=since,
        calls=calls,
        tokens_in=int(totals[1]),
        tokens_out=int(totals[2]),
        total_usd=_usd(totals[3]),
        by_model=by_model,
        by_node=by_node,
        by_prompt_version=by_prompt,
        by_day=[
            DailySpend(day=day.date(), calls=int(count), usd=_usd(usd)) for day, count, usd in daily
        ],
        default_run_cap_usd=_usd(latest_cap if latest_cap is not None else DEFAULT_MAX_USD),
        runs_in_window=int(runs_in_window),
        runs_at_cap=sum(1 for r in top_runs if r.at_cap),
        top_runs=top_runs,
        ledger_wired=ledger_wired,
        message=message,
    )


async def monthly_spend_usd(
    business_id: UUID, *, window_days: int = MONTHLY_CAP_WINDOW_DAYS
) -> Decimal:
    """This business's model spend over the ceiling's window, as a ``Decimal``.

    The same number as :attr:`CostReport.total_usd` for the same window, from the same
    table through the same tenant-scoped session -- but ONE ``SUM`` rather than the
    report's eight queries. :func:`cost_report` exists to render a screen: four
    breakdowns, a per-day series and a join against ``runs``. This is a guard on the path
    of every run start, and it needs exactly one figure. ``test_cost_service`` asserts the
    two agree, so the cheap read cannot drift away from the number the dashboard shows.

    Returned as ``Decimal`` rather than the report's display string: the caller compares
    it against a ceiling, and a comparison is arithmetic. Formatting for a human happens
    at the surface that shows it, through :func:`~backend.app.llm.pricing.format_usd`, so
    the refusal and the dashboard quote the figure identically.

    An empty ledger reads as zero here, and that is the deliberate direction of failure.
    :attr:`CostReport.ledger_wired` exists because "$0.00" and "nothing was recorded" are
    different statements -- but a guard cannot act on that difference: refusing every run
    on a business whose ledger is simply empty would stop a brand-new account from ever
    running anything. So an unrecorded ledger means an unenforceable ceiling, the
    dashboard is where that alarm is raised, and this function fails open.
    """
    _, since = _window(window_days)

    async with business_session(business_id) as s:
        spent: Decimal | None = (
            await s.execute(
                select(func.coalesce(func.sum(ModelUsage.usd), Decimal("0"))).where(
                    ModelUsage.created_at >= since
                )
            )
        ).scalar_one()

    return spent if spent is not None else Decimal("0")


def over_monthly_cap(spent_usd: Decimal, cap_usd: Decimal) -> bool:
    """Whether ``spent_usd`` has used up the ceiling.

    **Exactly AT the ceiling counts as over.** Three reasons, all pointing the same way:
    ``BudgetState.can_afford`` already works this way for the per-run cap (an estimate
    only fits while ``remaining_usd`` is still positive, so a run with zero remaining
    affords nothing) and two cap levels that disagree at their boundary is a bug waiting
    to be reported; :attr:`RunSpend.at_cap` reports ``>=`` too, so a business the
    dashboard shows at its cap would otherwise still be started; and the next run's first
    call is a call whose cost is not yet known -- a ceiling you may begin a run at is a
    ceiling that gets crossed by definition. The run budget errs towards refusing a call
    it could have afforded, and this errs the same way one level up.

    Pure, so the boundary is settled in one place rather than at each call site.
    """
    return spent_usd >= cap_usd


__all__ = [
    "BREAKDOWN_LIMIT",
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "MONTHLY_CAP_WINDOW_DAYS",
    "CostReport",
    "DailySpend",
    "RunSpend",
    "SpendRow",
    "cost_report",
    "monthly_spend_usd",
    "over_monthly_cap",
]
