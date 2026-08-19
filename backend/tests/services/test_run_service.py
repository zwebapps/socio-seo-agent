"""Run persistence: a run must survive the process that started it.

Written before the service. The properties that matter are the ones that make a run
resumable and a timeline replayable — not that a row exists.

Events are persisted as well as streamed, deliberately. The SSE stream is a
convenience; the table is the truth. A browser that reloads mid-run must see the same
timeline it saw before, and a run whose worker died must be resumable from what was
actually recorded.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from backend.app.agents.state import NodeError, new_state
from backend.app.services.run_service import (
    InMemoryRunStore,
    RunRecord,
    RunService,
)


def _service() -> tuple[RunService, InMemoryRunStore]:
    store = InMemoryRunStore()
    return RunService(store), store


async def test_starting_a_run_records_it_as_queued() -> None:
    service, store = _service()
    business_id = uuid4()

    run = await service.start(business_id=business_id, goal="more local leads")

    assert isinstance(run, RunRecord)
    assert run.state == "queued"
    assert run.business_id == business_id
    assert (await store.get(run.id)) is not None


async def test_events_are_appended_in_order_with_a_monotonic_sequence() -> None:
    """The sequence is what lets a client resume a stream without replaying everything,
    and what makes ordering independent of wall-clock ties."""
    service, _ = _service()
    run = await service.start(business_id=uuid4(), goal="g")

    for node in ("INTAKE", "HARVEST", "PLAN"):
        await service.record_event(run.id, node=node, status="started")
        await service.record_event(run.id, node=node, status="done")

    events = await service.events(run.id)
    assert [e.seq for e in events] == list(range(1, 7))
    assert [(e.node, e.status) for e in events][:2] == [("INTAKE", "started"), ("INTAKE", "done")]


async def test_a_reload_replays_the_same_timeline_from_the_store() -> None:
    """The stream is a convenience; the table is the truth."""
    service, _ = _service()
    run = await service.start(business_id=uuid4(), goal="g")
    await service.record_event(run.id, node="INTAKE", status="done", payload={"cost_usd": "0.001"})

    first = await service.events(run.id)
    again = await service.events(run.id)

    assert [e.model_dump() for e in first] == [e.model_dump() for e in again]


async def test_events_after_a_sequence_number_returns_only_the_tail() -> None:
    service, _ = _service()
    run = await service.start(business_id=uuid4(), goal="g")
    for i in range(5):
        await service.record_event(run.id, node=f"N{i}", status="done")

    tail = await service.events(run.id, after_seq=3)
    assert [e.seq for e in tail] == [4, 5]


async def test_checkpointing_stores_state_and_restores_it_exactly() -> None:
    """A run that cannot round-trip its state cannot resume, and a run that cannot
    resume loses work the customer already paid for."""
    service, _ = _service()
    run = await service.start(business_id=uuid4(), goal="g")

    state = new_state(business_id=run.business_id, goal="g")
    state = {**state, "cost_usd": Decimal("0.0731"), "step_count": 4}
    state["errors"] = [NodeError(node="HARVEST", code="serp_quota", message="quota")]

    await service.checkpoint(run.id, state=state, current_node="PLAN")
    restored = await service.restore(run.id)

    assert restored is not None
    assert restored["step_count"] == 4
    assert restored["cost_usd"] == Decimal("0.0731"), "Decimal must not become a float"
    assert [e.code for e in restored["errors"]] == ["serp_quota"]


async def test_finishing_a_run_records_the_outcome_and_a_reason() -> None:
    service, store = _service()
    run = await service.start(business_id=uuid4(), goal="g")

    await service.finish(run.id, outcome="partial", reason="max_usd reached (limit 0.50)")

    stored = await store.get(run.id)
    assert stored is not None
    assert stored.state == "partial"
    assert "max_usd" in (stored.finished_reason or ""), "a stopped run must say why"


async def test_a_resumed_run_increments_its_resume_counter() -> None:
    """A rising count is the signal that the workers are unstable, so it has to be
    recorded rather than inferred."""
    service, store = _service()
    run = await service.start(business_id=uuid4(), goal="g")

    await service.mark_resumed(run.id)
    await service.mark_resumed(run.id)

    stored = await store.get(run.id)
    assert stored is not None and stored.resumed_count == 2


async def test_awaiting_approval_is_a_distinct_state_not_a_finished_one() -> None:
    """A run paused for a human is neither running nor done. Collapsing it into either
    would make the queue either re-run it or forget it."""
    service, store = _service()
    run = await service.start(business_id=uuid4(), goal="g")

    await service.await_approval(run.id)

    stored = await store.get(run.id)
    assert stored is not None and stored.state == "awaiting_approval"


async def test_recording_an_event_for_an_unknown_run_is_an_error_not_a_silent_drop() -> None:
    service, _ = _service()
    with pytest.raises(KeyError):
        await service.record_event(uuid4(), node="INTAKE", status="done")


async def test_an_event_payload_never_carries_the_draft_text() -> None:
    """The timeline is an operational record. Putting generated content in it would
    duplicate the content store and bloat every stream."""
    service, _ = _service()
    run = await service.start(business_id=uuid4(), goal="g")
    await service.record_event(
        run.id,
        node="GENERATE",
        status="done",
        payload={"cost_usd": "0.09", "html": "<h1>should be dropped</h1>"},
    )
    events = await service.events(run.id)
    assert "html" not in events[-1].payload
    assert events[-1].payload["cost_usd"] == "0.09"
