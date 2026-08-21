"""The cost dashboard's aggregation, against real rows and real row-level security.

Four things are proved here, and only the first is arithmetic:

1. **the sums are right, and they are `Decimal`.** A float in a money path is a bug even
   when the rendered number happens to look correct;
2. **the report is scoped to one tenant by the DATABASE, not by a WHERE clause.** Another
   business's usage rows are seeded and must not appear -- and because RLS is what excludes
   them, this is also a test that the read goes through `business_session`;
3. **an unrecorded ledger is reported as unrecorded, not as `$0.00`.** `model_usage` is
   currently written by nothing at all, so this distinction is the difference between a
   dashboard and a lie;
4. **spend is compared against the run's OWN cap**, joined from `runs`, rather than
   against a constant -- because a run stores the ceiling it was actually held to.

The suite is marked `db` and skipped when Postgres is unreachable, per the conftest note.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.services.cost_service import (
    MONTHLY_CAP_WINDOW_DAYS,
    cost_report,
    monthly_spend_usd,
)

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the app's session factory at THIS test's engine.

    Copied from ``test_lead_store.scoped_sessions`` and autouse here because every test in
    this file calls ``cost_report``, which reaches the database through the process-wide
    factory. Without it the second test in the file fails with "attached to a different
    loop": an asyncpg pool belongs to the event loop that created it, pytest-asyncio gives
    each test a fresh loop, and the module-level factory outlives both.

    Patching the factory rather than injecting a session keeps the real row-level-security
    scoping under test instead of replacing it with a hand-rolled copy -- which matters
    here, because tenant isolation is one of the things being asserted.
    """
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


async def _seed_run(session: AsyncSession, business_id: UUID, *, budget: str = "0.50") -> UUID:
    run_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO runs (id, business_id, goal, state, budget_usd, used_usd) "
            "VALUES (:id, :b, 'fixture goal', 'done', :budget, 0)"
        ),
        {"id": run_id, "b": business_id, "budget": Decimal(budget)},
    )
    return run_id


async def _seed_usage(
    session: AsyncSession,
    business_id: UUID,
    *,
    run_id: UUID | None,
    model: str,
    node: str,
    usd: str,
    tokens_in: int = 100,
    tokens_out: int = 200,
    prompt_version: str = "nodes.v1",
    created_at: datetime | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO model_usage (id, business_id, run_id, node, provider, model, "
            "prompt_version, tokens_in, tokens_out, usd, latency_ms, created_at) VALUES "
            "(:id, :b, :r, :node, 'openrouter', :model, :pv, :ti, :to, :usd, 10, "
            ":created_at)"
        ),
        {
            "id": uuid4(),
            "b": business_id,
            "r": run_id,
            "node": node,
            "model": model,
            "pv": prompt_version,
            "ti": tokens_in,
            "to": tokens_out,
            "usd": Decimal(usd),
            "created_at": created_at or datetime.now(UTC),
        },
    )


async def test_spend_is_summed_by_model_node_day_and_prompt_version(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    mine, theirs = two_businesses
    run = await _seed_run(owner_session, mine)
    await _seed_usage(
        owner_session, mine, run_id=run, model="openai/gpt-4.1", node="GENERATE", usd="0.02000000"
    )
    await _seed_usage(
        owner_session,
        mine,
        run_id=run,
        model="openai/gpt-4.1-mini",
        node="CLASSIFY",
        usd="0.00100000",
    )
    # Another tenant's spend, which row-level security must exclude.
    await _seed_usage(
        owner_session,
        theirs,
        run_id=None,
        model="openai/gpt-4.1",
        node="GENERATE",
        usd="9.99000000",
    )
    await owner_session.commit()

    report = await cost_report(mine)

    assert report.calls == 2
    assert Decimal(report.total_usd) == Decimal("0.02100000")
    assert Decimal(report.total_usd) < Decimal("9"), (
        "another business's spend is in this total, so the read is not tenant-scoped"
    )

    by_model = {row.key: row for row in report.by_model}
    assert Decimal(by_model["openai/gpt-4.1"].usd) == Decimal("0.02000000")
    assert by_model["openai/gpt-4.1"].priced is True

    by_node = {row.key: Decimal(row.usd) for row in report.by_node}
    assert by_node["GENERATE"] == Decimal("0.02000000")
    assert by_node["CLASSIFY"] == Decimal("0.00100000")

    assert {row.key for row in report.by_prompt_version} == {"nodes.v1"}
    assert len(report.by_day) == 1
    assert Decimal(report.by_day[0].usd) == Decimal("0.02100000")


async def test_totals_are_decimal_and_never_a_float(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """Three values that a float would round differently. 0.1 + 0.2 != 0.3 in binary
    floating point, and this is exactly that sum."""
    mine, _ = two_businesses
    for usd in ("0.10000000", "0.20000000"):
        await _seed_usage(
            owner_session, mine, run_id=None, model="openai/gpt-4.1", node="GENERATE", usd=usd
        )
    await owner_session.commit()

    report = await cost_report(mine)

    assert report.total_usd == "0.30000000", (
        f"total came back as {report.total_usd!r}; a float in the path would give "
        "0.30000000000000004"
    )
    assert Decimal(report.total_usd) == Decimal("0.3")


async def test_a_model_with_no_price_entry_is_flagged_rather_than_shown_as_free(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """The same honesty `admin_models.available_models` already applies: an unpriced model
    contributes $0.00 to the total, and saying so is the difference between a report and a
    wrong number."""
    mine, _ = two_businesses
    await _seed_usage(
        owner_session, mine, run_id=None, model="ollama/llama3.1", node="GENERATE", usd="0"
    )
    await owner_session.commit()

    report = await cost_report(mine)

    unpriced = [row for row in report.by_model if row.priced is False]
    assert [row.key for row in unpriced] == ["ollama/llama3.1"]
    assert "understated" in report.message
    assert "ollama/llama3.1" in report.message


async def test_an_unwritten_ledger_is_reported_as_unrecorded_not_as_zero(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """The state the product is actually in: `model_usage` is written by nothing, so a run
    exists with no usage rows behind it. `$0.00` would be a false statement about spend."""
    mine, _ = two_businesses
    await _seed_run(owner_session, mine)
    await owner_session.commit()

    report = await cost_report(mine)

    assert report.runs_in_window == 1
    assert report.calls == 0
    assert report.ledger_wired is False
    assert "not" in report.message and "$0.00" in report.message


async def test_no_runs_and_no_usage_is_simply_empty_rather_than_a_warning(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """A brand-new business has spent nothing, and that IS zero. The warning above must not
    fire for it, or it would cry wolf on every fresh account."""
    mine, _ = two_businesses

    report = await cost_report(mine)

    assert report.ledger_wired is True
    assert report.calls == 0
    assert Decimal(report.total_usd) == 0


async def test_run_spend_is_compared_against_the_run_s_own_cap(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """The cap comes from the run row, not a constant: a run stores the ceiling it was
    actually held to, and that is the number "did it hit the cap" has to mean."""
    mine, _ = two_businesses
    cheap = await _seed_run(owner_session, mine, budget="0.50")
    tight = await _seed_run(owner_session, mine, budget="0.01")
    await _seed_usage(
        owner_session, mine, run_id=cheap, model="openai/gpt-4.1", node="GENERATE", usd="0.05"
    )
    await _seed_usage(
        owner_session, mine, run_id=tight, model="openai/gpt-4.1", node="GENERATE", usd="0.02"
    )
    await owner_session.commit()

    report = await cost_report(mine)

    by_run = {row.run_id: row for row in report.top_runs}
    assert by_run[cheap].at_cap is False
    assert by_run[tight].at_cap is True, "0.02 spent against a 0.01 cap is over it"
    assert report.runs_at_cap == 1
    assert Decimal(by_run[tight].cap_usd) == Decimal("0.01")


async def test_the_window_excludes_older_rows(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    mine, _ = two_businesses
    await _seed_usage(
        owner_session,
        mine,
        run_id=None,
        model="openai/gpt-4.1",
        node="GENERATE",
        usd="1.00",
        created_at=datetime.now(UTC) - timedelta(days=45),
    )
    await _seed_usage(
        owner_session, mine, run_id=None, model="openai/gpt-4.1", node="GENERATE", usd="0.02"
    )
    await owner_session.commit()

    thirty = await cost_report(mine, window_days=30)
    ninety = await cost_report(mine, window_days=90)

    assert Decimal(thirty.total_usd) == Decimal("0.02")
    assert Decimal(ninety.total_usd) == Decimal("1.02")


async def test_an_absurd_window_is_clamped_rather_than_scanning_everything(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """A dashboard that can be asked for a million days is a full-table scan waiting for a
    hand-written query string."""
    mine, _ = two_businesses

    report = await cost_report(mine, window_days=10_000)

    assert report.window_days == 365


# --------------------------------------------------------------------------- #
# The per-business ceiling's read (`ARCHITECTURE.md` 7.4)
#
# `monthly_spend_usd` is a second, narrower query over the same ledger, and the risk of
# a second query is that it drifts from the first: the dashboard would show one number
# while the guard refused runs on another. So the agreement is asserted, not assumed.
# --------------------------------------------------------------------------- #


async def test_monthly_spend_is_the_same_number_the_dashboard_reports(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """The drift pin. One SUM instead of eight queries, and the same figure."""
    mine, _ = two_businesses
    for usd in ("0.10000000", "0.20000000"):
        await _seed_usage(
            owner_session, mine, run_id=None, model="openai/gpt-4.1", node="GENERATE", usd=usd
        )
    await owner_session.commit()

    spent = await monthly_spend_usd(mine)
    report = await cost_report(mine, window_days=MONTHLY_CAP_WINDOW_DAYS)

    assert spent == Decimal("0.3"), (
        f"came back {spent!r}; a float in the path would give 0.30000000000000004"
    )
    assert isinstance(spent, Decimal), "money is Decimal from the database to the comparison"
    assert spent == Decimal(report.total_usd), (
        "the guard's figure and the cost screen's figure must be the same number, or an "
        "owner is refused on one number and shown another"
    )


async def test_monthly_spend_is_scoped_to_one_tenant_by_the_database(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """Another business's spend must not consume this one's allowance.

    Row-level security is what excludes it -- the query has no `business_id` predicate --
    so this is also a test that the read goes through `business_session`.
    """
    mine, theirs = two_businesses
    await _seed_usage(
        owner_session, mine, run_id=None, model="openai/gpt-4.1", node="GENERATE", usd="0.02"
    )
    await _seed_usage(
        owner_session, theirs, run_id=None, model="openai/gpt-4.1", node="GENERATE", usd="99.00"
    )
    await owner_session.commit()

    assert await monthly_spend_usd(mine) == Decimal("0.02")
    assert await monthly_spend_usd(theirs) == Decimal("99.00")


async def test_monthly_spend_only_counts_the_window(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """A rolling window, so a ceiling reached today is not forgiven by a calendar page."""
    mine, _ = two_businesses
    await _seed_usage(
        owner_session,
        mine,
        run_id=None,
        model="openai/gpt-4.1",
        node="GENERATE",
        usd="9.00",
        created_at=datetime.now(UTC) - timedelta(days=MONTHLY_CAP_WINDOW_DAYS + 5),
    )
    await _seed_usage(
        owner_session, mine, run_id=None, model="openai/gpt-4.1", node="GENERATE", usd="0.02"
    )
    await owner_session.commit()

    assert await monthly_spend_usd(mine) == Decimal("0.02")
    assert await monthly_spend_usd(mine, window_days=MONTHLY_CAP_WINDOW_DAYS + 30) == Decimal(
        "9.02"
    )


async def test_an_empty_ledger_reads_as_zero_rather_than_none(
    owner_session: AsyncSession, two_businesses: tuple[UUID, UUID]
) -> None:
    """`SUM` over no rows is SQL NULL, and a guard comparing NULL to a ceiling would
    raise on the run-start path of every brand-new business.

    Failing OPEN here is deliberate and is documented on the function: a guard cannot tell
    "spent nothing" from "nothing was recorded", and refusing every fresh account would be
    the worse of the two mistakes. `CostReport.ledger_wired` is where the unrecorded case
    is called out.
    """
    mine, _ = two_businesses

    assert await monthly_spend_usd(mine) == Decimal("0")
