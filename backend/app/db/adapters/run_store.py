"""Postgres-backed run persistence.

Every method runs inside ``business_session`` so RLS applies — a run belongs to exactly
one business, and the timeline is customer data.

One thing worth naming: ``next_seq`` reads MAX(seq)+1 inside the same transaction as the
insert. That is safe here because a single worker owns a run for its lifetime, so there is
no concurrent appender. If runs ever become multi-worker, this needs a sequence or an
advisory lock — the unique constraint on (run_id, seq) will turn the race into a visible
error rather than a silently reordered timeline, which is the failure mode you want.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select

from backend.app.db.models import Run, RunEvent
from backend.app.db.session import business_session
from backend.app.services.run_service import RunEventRecord, RunRecord


class PostgresRunStore:
    """The real store behind ``RunService``."""

    async def create(self, run: RunRecord) -> RunRecord:
        async with business_session(run.business_id) as s:
            s.add(
                Run(
                    id=run.id,
                    business_id=run.business_id,
                    goal=run.goal,
                    state=run.state,
                    current_node=run.current_node,
                    checkpoint=run.checkpoint,
                )
            )
        return run

    async def get(self, run_id: UUID) -> RunRecord | None:
        business_id = await self._business_for(run_id)
        if business_id is None:
            return None
        async with business_session(business_id) as s:
            row = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if row is None:
            return None
        return RunRecord(
            id=row.id,
            business_id=row.business_id,
            goal=row.goal,
            state=row.state,  # type: ignore[arg-type]
            current_node=row.current_node,
            resumed_count=row.resumed_count,
            finished_reason=row.finished_reason,
            checkpoint=row.checkpoint,
        )

    async def update(self, run: RunRecord) -> None:
        async with business_session(run.business_id) as s, s.begin():
            row = (await s.execute(select(Run).where(Run.id == run.id))).scalar_one_or_none()
            if row is None:
                raise KeyError(run.id)
            row.state = run.state
            row.current_node = run.current_node
            row.resumed_count = run.resumed_count
            row.finished_reason = run.finished_reason
            row.checkpoint = run.checkpoint

    async def append_event(self, event: RunEventRecord) -> None:
        business_id = await self._business_for(event.run_id)
        if business_id is None:
            raise KeyError(event.run_id)
        async with business_session(business_id) as s, s.begin():
            s.add(
                RunEvent(
                    business_id=business_id,
                    run_id=event.run_id,
                    seq=event.seq,
                    node=event.node,
                    status=event.status,
                    payload=event.payload,
                )
            )

    async def next_seq(self, run_id: UUID) -> int:
        business_id = await self._business_for(run_id)
        if business_id is None:
            raise KeyError(run_id)
        async with business_session(business_id) as s:
            highest = (
                await s.execute(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
            ).scalar()
        return int(highest or 0) + 1

    async def list_events(self, run_id: UUID, *, after_seq: int = 0) -> Sequence[RunEventRecord]:
        business_id = await self._business_for(run_id)
        if business_id is None:
            return []
        async with business_session(business_id) as s:
            rows = (
                (
                    await s.execute(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
                        .order_by(RunEvent.seq)
                    )
                )
                .scalars()
                .all()
            )
        return [
            RunEventRecord(
                run_id=row.run_id,
                seq=row.seq,
                node=row.node,
                status=row.status,  # type: ignore[arg-type]
                payload=row.payload,
                at=row.created_at,
            )
            for row in rows
        ]

    async def _business_for(self, run_id: UUID) -> UUID | None:
        """Find which business owns a run, so the scoped session can be opened.

        A chicken-and-egg: RLS needs the business id, and the business id lives on the row
        RLS is protecting. Resolved with the OWNER session for this ONE column lookup —
        `runs.id` is a UUID nobody can guess, the query returns a single scalar, and every
        subsequent statement in the request runs scoped. Widening this shortcut to anything
        else would defeat the isolation it is carved out of.
        """
        from backend.app.db.session import session

        async with session() as s:
            return (
                await s.execute(select(Run.business_id).where(Run.id == run_id))
            ).scalar_one_or_none()


__all__ = ["PostgresRunStore"]
