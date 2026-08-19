"""``PostgresRunStore`` against real Postgres, with RLS actually on.

**This file exists because its absence hid a total outage.** The run store was only
ever exercised through ``InMemoryRunStore``, which is a dict and has no row-level
security to fail against. So every test passed while the real store returned ``None``
for every run that exists: it resolved the owning business from the run row on an
unscoped session, and under ``FORCE ROW LEVEL SECURITY`` the restricted application
role reads zero rows without a tenant GUC. The Phase 9 timeline screen had never
worked outside tests.

The lesson generalises, and it is the reason this file is structured around the
restricted role rather than around the store's methods: an adapter whose whole job is
to satisfy RLS cannot be tested against something that does not have RLS.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.db.adapters.run_store import PostgresRunStore
from backend.app.services.run_service import RunEventRecord, RunRecord

pytestmark = pytest.mark.db


@pytest.fixture
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the session factory at this test's engine -- the RESTRICTED role.

    Deliberately the restricted role and nothing else. Using the owner engine here
    would reproduce exactly the blindness that let the bug through.
    """
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


def _run(business_id: UUID, *, goal: str = "get more leads") -> RunRecord:
    return RunRecord(id=uuid4(), business_id=business_id, goal=goal, state="queued")


# --------------------------------------------------------------------------- #
# The bug
# --------------------------------------------------------------------------- #


async def test_a_run_reads_back_on_the_restricted_role(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The regression test for the 404-on-every-run bug.

    Before the fix this returned ``None``: the store asked an unscoped session which
    business owned the run, RLS answered "no rows", and the store concluded the run did
    not exist. Nothing errored -- which is why it looked like "nobody has started a run
    yet" rather than like a bug.
    """
    store = PostgresRunStore(business_a)
    created = await store.create(_run(business_a))

    found = await store.get(created.id)

    assert found is not None, "the run exists; a None here is the bug this file was written for"
    assert found.id == created.id
    assert found.business_id == business_a
    assert found.goal == "get more leads"


async def test_a_run_belonging_to_another_business_is_invisible(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """Isolation is now the DATABASE's answer, not an ``if`` in the route.

    The API also compares ``run.business_id`` to the caller's, and that check stays as
    defence in depth -- but this asserts the store cannot hand it a foreign row to
    compare in the first place.
    """
    a_store = PostgresRunStore(business_a)
    b_store = PostgresRunStore(business_b)
    a_run = await a_store.create(_run(business_a, goal="A's private goal"))

    assert await b_store.get(a_run.id) is None


async def test_events_are_written_and_read_under_the_callers_scope(
    scoped_sessions: None, business_a: UUID
) -> None:
    """`append_event` had the same unscoped lookup, so it raised KeyError for every run."""
    store = PostgresRunStore(business_a)
    run = await store.create(_run(business_a))

    seq = await store.next_seq(run.id)
    await store.append_event(
        RunEventRecord(
            run_id=run.id, seq=seq, node="INTAKE", status="done", payload={}, at=datetime.now(UTC)
        )
    )

    events = await store.list_events(run.id)
    assert [e.node for e in events] == ["INTAKE"]
    assert await store.next_seq(run.id) == seq + 1


async def test_another_business_cannot_read_the_events(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """Run events carry the payloads, so leaking them leaks the work itself."""
    a_store = PostgresRunStore(business_a)
    run = await a_store.create(_run(business_a))
    await a_store.append_event(
        RunEventRecord(
            run_id=run.id, seq=1, node="GENERATE", status="done", payload={}, at=datetime.now(UTC)
        )
    )

    assert list(await PostgresRunStore(business_b).list_events(run.id)) == []


async def test_updating_another_businesss_run_raises_rather_than_writing(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """`update` scopes to the REQUEST's tenant, not to the record's own business_id.

    Trusting the record would be the client choosing its own authorisation: a caller
    holding a constructed ``RunRecord`` could name another tenant and have RLS scoped to
    whatever it claimed. Under the request's scope the row is invisible, so the write
    finds nothing and says so.
    """
    a_run = await PostgresRunStore(business_a).create(_run(business_a))
    forged = RunRecord(
        id=a_run.id,
        business_id=business_a,  # the record claims A...
        goal="hijacked",
        state="done",
    )

    with pytest.raises(KeyError):
        await PostgresRunStore(business_b).update(forged)  # ...but B is asking

    still_a = await PostgresRunStore(business_a).get(a_run.id)
    assert still_a is not None
    assert still_a.goal == "get more leads", "A's run must be untouched"
    assert still_a.state == "queued"


async def test_a_checkpoint_survives_the_round_trip(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The checkpoint IS the resumability mechanism, so it has to come back intact."""
    store = PostgresRunStore(business_a)
    run = await store.create(_run(business_a))
    run.checkpoint = {"node": "GENERATE", "draft": {"title": "Rohrbruch in Koblenz"}}

    await store.update(run)
    restored = await store.get(run.id)

    assert restored is not None
    assert restored.checkpoint["draft"]["title"] == "Rohrbruch in Koblenz"


async def test_the_app_role_still_reads_nothing_unscoped(
    scoped_sessions: None, business_a: UUID, app_engine: AsyncEngine
) -> None:
    """The premise, proved rather than assumed.

    If this ever returns a row, every isolation assertion above is vacuous and the
    store's tenant scoping is decoration.
    """
    from sqlalchemy import text

    run = await PostgresRunStore(business_a).create(_run(business_a))

    async with app_engine.connect() as conn:
        blind = await conn.execute(text("SELECT count(*) FROM runs WHERE id = :i"), {"i": run.id})

    assert blind.scalar() == 0


async def test_a_reason_longer_than_the_column_still_records_the_terminal_state(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The bug, against the database that actually raises.

    `runs.finished_reason` is VARCHAR(255). A provider failure produced a reason of
    about 700 characters -- an `AllProvidersFailedError` naming two refused models with
    their 404 bodies -- so the UPDATE raised `StringDataRightTruncationError` and
    `finish` failed. The executor's handler then tried to record THAT exception as the
    reason, which was longer still and failed identically, leaving the run saying
    `running` forever: indistinguishable from one still working.

    This assertion cannot be made against `InMemoryRunStore`, which is a dict and
    accepts any length. It is the same lesson as the rest of this file: a guard about a
    database constraint has to be tested against the database.
    """
    from backend.app.services.run_service import MAX_FINISHED_REASON, RunService

    store = PostgresRunStore(business_a)
    service = RunService(store)
    run = await store.create(_run(business_a))

    long_reason = "Opportunity selection could not run. Cause: " + ("y" * 900)
    await service.finish(run.id, outcome="partial", reason=long_reason)

    finished = await store.get(run.id)
    assert finished is not None
    assert finished.state == "partial", "the terminal state must be written regardless"
    assert finished.finished_reason is not None
    assert len(finished.finished_reason) <= MAX_FINISHED_REASON
    assert finished.finished_reason.startswith("Opportunity selection could not run"), (
        "the human sentence is written first precisely so it survives truncation"
    )


# --------------------------------------------------------------------------- #
# Listing runs: RLS is the scoping, not a WHERE clause
# --------------------------------------------------------------------------- #


async def test_listing_returns_this_businesss_runs_newest_first(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The owner's way back to a run they started, so it has to work on the real role.

    Ordering is asserted rather than assumed: `created_at DESC` is what makes "the run I
    just started" the first row, and a list ordered the other way is unusable by the
    twenty-first run.
    """
    store = PostgresRunStore(business_a)
    older = await store.create(_run(business_a, goal="older"))
    newer = await store.create(_run(business_a, goal="newer"))

    listed = await store.list_runs()

    ids = [r.id for r in listed]
    assert older.id in ids and newer.id in ids
    assert ids.index(newer.id) < ids.index(older.id), "newest first"
    assert {r.business_id for r in listed} == {business_a}


async def test_listing_does_not_see_another_businesss_runs(
    scoped_sessions: None, business_a: UUID, business_b: UUID
) -> None:
    """The point of the whole adapter, on the surface most likely to get it wrong.

    `list_runs` carries NO `WHERE business_id = ...`: it opens `business_session` and lets
    row-level security answer. That is the same choice as `get`, and it is what the
    store's docstring argues for -- so this test is the proof that RLS really is doing it,
    because a broken scope here would return every tenant's goals rather than a 404 for
    one id.
    """
    a_store = PostgresRunStore(business_a)
    await a_store.create(_run(business_a, goal="A's private goal"))

    listed = await PostgresRunStore(business_b).list_runs()

    assert all(r.business_id == business_b for r in listed)
    assert not any(r.goal == "A's private goal" for r in listed)


async def test_a_listed_run_carries_its_terminal_state_and_reason(
    scoped_sessions: None, business_a: UUID
) -> None:
    """A `partial` run with no explanation reads as a broken product rather than as a
    credential that cannot reach the mid tier, so the reason travels with the state."""
    from backend.app.services.run_service import RunService

    store = PostgresRunStore(business_a)
    service = RunService(store)
    run = await store.create(_run(business_a, goal="needs an explanation"))
    await service.finish(run.id, outcome="partial", reason="No mid-tier credential.")

    listed = [r for r in await store.list_runs() if r.id == run.id]

    assert [(r.state, r.finished_reason) for r in listed] == [
        ("partial", "No mid-tier credential.")
    ]


async def test_listing_refuses_a_limit_outside_the_band(
    scoped_sessions: None, business_a: UUID
) -> None:
    """`limit` reaches the store from a query parameter. The route bounds it too, but a
    bound enforced in exactly one place stops existing the moment something else calls in.
    """
    from backend.app.services.run_service import MAX_RUN_LIST_LIMIT

    store = PostgresRunStore(business_a)

    with pytest.raises(ValueError, match="limit"):
        await store.list_runs(limit=MAX_RUN_LIST_LIMIT + 1)
    with pytest.raises(ValueError, match="limit"):
        await store.list_runs(limit=0)


async def test_listing_never_reads_the_checkpoint_column(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The list selects named columns, so the biggest thing a run produces is never even
    fetched -- not merely omitted from the response afterwards.

    Asserted through the record's own shape: `RunSummaryRecord` has no `checkpoint`
    field, so a draft cannot ride along however large the stored state grows.
    """
    from backend.app.services.run_service import RunSummaryRecord

    store = PostgresRunStore(business_a)
    run = await store.create(_run(business_a))
    run.checkpoint = {"draft": {"html": "<h1>the unpublished draft</h1>"}}
    await store.update(run)

    listed = [r for r in await store.list_runs() if r.id == run.id]

    assert len(listed) == 1
    assert "checkpoint" not in RunSummaryRecord.model_fields
    assert "unpublished draft" not in listed[0].model_dump_json()
