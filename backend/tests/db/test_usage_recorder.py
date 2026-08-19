"""``UsageRecorder`` against real Postgres, with RLS on.

`model_usage` called itself "the cost ledger" from the Phase 1 schema onward and
NOTHING wrote a row. The table was structurally empty, so the cost dashboard had to
report its figures as unavailable rather than show a confident `$0.00`.

Tested against the real database rather than a fake session for the reason the run-store
file records: an adapter whose job is to satisfy RLS cannot be tested against something
that has no RLS. A business-scoped insert that silently wrote nothing would pass every
in-memory test.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.llm.contract import Usage
from backend.app.services.usage_recorder import UsageRecorder

pytestmark = pytest.mark.db


@pytest.fixture
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


@pytest.fixture
async def business_a(two_businesses: tuple[UUID, UUID]) -> UUID:
    return two_businesses[0]


@pytest.fixture
async def business_b(two_businesses: tuple[UUID, UUID]) -> UUID:
    return two_businesses[1]


async def _a_run(engine: AsyncEngine, business_id: UUID) -> UUID:
    """A real `runs` row.

    `model_usage.run_id` is a foreign key, so a fabricated id is refused -- which is the
    right constraint (a cost row that belongs to no run is unattributable) and means these
    tests have to create one rather than invent a UUID.
    """
    run_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_business_id', :b, true)"),
            {"b": str(business_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, business_id, goal, state) "
                "VALUES (:id, :b, 'ledger fixture', 'running')"
            ),
            {"id": run_id, "b": business_id},
        )
    return run_id


def _usage(*, usd: str = "0.0125", model: str = "openai/gpt-4.1-mini") -> Usage:
    return Usage(
        provider="openrouter",
        model=model,
        tokens_in=1200,
        tokens_out=430,
        usd=Decimal(usd),
        latency_ms=812,
    )


async def _count(engine: AsyncEngine, business_id: UUID) -> int:
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_business_id', :b, true)"),
            {"b": str(business_id)},
        )
        result = await conn.execute(text("SELECT count(*) FROM model_usage"))
        return int(result.scalar() or 0)


async def test_a_flushed_call_becomes_a_ledger_row(
    scoped_sessions: None, business_a: UUID, app_engine: AsyncEngine
) -> None:
    """The gap this closes: `model_usage` had a schema, a docstring and no writer."""
    run_id = await _a_run(app_engine, business_a)
    recorder = UsageRecorder(run_id=run_id, business_id=business_a)

    recorder.sink(_usage(), {"node": "GENERATE", "prompt_version": "nodes.v1"})
    assert recorder.pending == 1, "the sink is synchronous, so it only buffers"

    await recorder.flush()

    assert recorder.pending == 0
    assert recorder.recorded == 1
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_business_id', :b, true)"),
            {"b": str(business_a)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT run_id, node, provider, model, prompt_version, tokens_in, "
                    "tokens_out, usd, latency_ms FROM model_usage"
                )
            )
        ).one()

    assert row.run_id == run_id
    assert row.node == "GENERATE"
    assert row.provider == "openrouter"
    assert row.model == "openai/gpt-4.1-mini"
    assert row.prompt_version == "nodes.v1"
    assert row.tokens_in == 1200
    assert row.tokens_out == 430
    assert row.usd == Decimal("0.0125"), "money is Decimal end to end, never a float"
    assert row.latency_ms == 812


async def test_the_row_is_written_under_the_businesss_own_scope(
    scoped_sessions: None, business_a: UUID, business_b: UUID, app_engine: AsyncEngine
) -> None:
    """`model_usage` is business-scoped, so a wrong scope is a cross-tenant cost leak.

    And an insert under NO scope would fail the RLS WITH CHECK rather than land
    somewhere harmless — which is why this is asserted against the real database.
    """
    recorder = UsageRecorder(run_id=await _a_run(app_engine, business_a), business_id=business_a)
    recorder.sink(_usage(), {"node": "PLAN"})
    await recorder.flush()

    assert await _count(app_engine, business_a) == 1
    assert await _count(app_engine, business_b) == 0, "B must not see A's spend"


async def test_a_missing_node_is_recorded_as_null_not_guessed(
    scoped_sessions: None, business_a: UUID, app_engine: AsyncEngine
) -> None:
    """An unattributed row is honest; a wrong attribution misreports which step is dear."""
    recorder = UsageRecorder(run_id=await _a_run(app_engine, business_a), business_id=business_a)
    recorder.sink(_usage(), {})
    await recorder.flush()

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_business_id', :b, true)"),
            {"b": str(business_a)},
        )
        node = (await conn.execute(text("SELECT node FROM model_usage"))).scalar()

    assert node is None


async def test_a_failed_flush_drops_the_batch_rather_than_retrying_it(
    scoped_sessions: None, business_a: UUID, app_engine: AsyncEngine
) -> None:
    """Losing a row under-reports; retrying one over-reports, and that is worse.

    An operator acts on spend that looks too high. So the buffer is cleared BEFORE the
    insert is attempted: a failing flush cannot multiply one model call into several
    ledger entries on the next node boundary.
    """
    recorder = UsageRecorder(run_id=uuid4(), business_id=uuid4())  # no such business
    recorder.sink(_usage(), {"node": "GENERATE"})

    await recorder.flush()  # must not raise: the ledger is a record OF the work

    assert recorder.pending == 0, "the batch is dropped, not left to be retried"
    assert recorder.recorded == 0, "and it must not claim to have recorded anything"

    # A later, valid flush still works -- the recorder is not poisoned.
    good = UsageRecorder(run_id=await _a_run(app_engine, business_a), business_id=business_a)
    good.sink(_usage(), {"node": "PLAN"})
    await good.flush()
    assert await _count(app_engine, business_a) == 1


async def test_an_empty_flush_is_a_no_op(scoped_sessions: None, business_a: UUID) -> None:
    """Called on every node boundary, and most nodes make no model call at all.

    No run row needed: an empty flush must not touch the database at all, which is also
    why it cannot fail on a foreign key.
    """
    recorder = UsageRecorder(run_id=uuid4(), business_id=business_a)

    await recorder.flush()

    assert recorder.recorded == 0
