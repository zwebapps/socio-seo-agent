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
    """The real store behind ``RunService``, scoped to one business.

    **The business id is a constructor argument, and that is the fix for a bug that
    made every run endpoint 404 in production.**

    This store used to resolve the owning business from the run row itself, calling
    that a chicken-and-egg: RLS needs the business id, and the id lives on the row RLS
    protects. Its docstring said the lookup ran on "the OWNER session" -- but it
    imported ``db.session.session``, which is the RESTRICTED role. Under FORCE row-level
    security with no tenant GUC set, that role reads ZERO rows, so the lookup returned
    ``None`` for every run in existence and every read 404'd. Verified against the live
    database: the owner role counts 1, the app role unscoped counts 0. The Phase 9
    timeline screen had therefore never worked outside tests, because the in-memory
    store has no RLS to fail against.

    There was never a chicken-and-egg. Every route that reaches this store already
    depends on ``current_business``, so the caller knows the tenant before it asks --
    the lookup was answering a question nobody needed to ask. Taking the id here means
    RLS does the isolation rather than a privileged read carved out of it: a run
    belonging to another tenant is simply not found, which is the same 404 the API
    already returns, arrived at by the database instead of by an ``if``.

    Deliberately NOT the SECURITY DEFINER pattern used for ``resolve_short_link``
    (migration ``7c1e4a90b2d5``). That exists because a public visitor genuinely has no
    tenant context and the lookup is what produces one. Here the context exists, so the
    right move is to use it, not to privilege a read past it.

    One instance per request, since it carries request state.
    """

    def __init__(self, business_id: UUID) -> None:
        self._business_id = business_id

    async def create(self, run: RunRecord) -> RunRecord:
        # Insert under this request's tenant. The record carries a business_id too, and
        # for a brand-new row they are the same -- but scoping to the request means a
        # record claiming a different tenant fails the RLS WITH CHECK rather than being
        # written where it asked to be.
        async with business_session(self._business_id) as s:
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
        async with business_session(self._business_id) as s:
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
        # This request's tenant, NOT the record's own `business_id`. Trusting the
        # record would let a caller that constructed one reach another tenant's row --
        # RLS would be scoped to whatever the argument claimed, which is the client
        # choosing its own authorisation. Under this scope a foreign row is invisible
        # and the KeyError below is what a mismatch produces.
        # No `s.begin()`: `business_session` has already opened the transaction --
        # it must, because `SET LOCAL` is transaction-scoped. A second begin raises
        # InvalidRequestError. This line used to carry one, and it never fired only
        # because the broken tenant lookup returned early before reaching it, so the
        # first bug was hiding the second.
        async with business_session(self._business_id) as s:
            row = (await s.execute(select(Run).where(Run.id == run.id))).scalar_one_or_none()
            if row is None:
                raise KeyError(run.id)
            row.state = run.state
            row.current_node = run.current_node
            row.resumed_count = run.resumed_count
            row.finished_reason = run.finished_reason
            row.checkpoint = run.checkpoint

    async def append_event(self, event: RunEventRecord) -> None:
        # No `s.begin()`: `business_session` has already opened the transaction --
        # it must, because `SET LOCAL` is transaction-scoped. A second begin raises
        # InvalidRequestError. This line used to carry one, and it never fired only
        # because the broken tenant lookup returned early before reaching it, so the
        # first bug was hiding the second.
        async with business_session(self._business_id) as s:
            s.add(
                RunEvent(
                    business_id=self._business_id,
                    run_id=event.run_id,
                    seq=event.seq,
                    node=event.node,
                    status=event.status,
                    payload=event.payload,
                )
            )

    async def next_seq(self, run_id: UUID) -> int:
        async with business_session(self._business_id) as s:
            highest = (
                await s.execute(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
            ).scalar()
        return int(highest or 0) + 1

    async def list_events(self, run_id: UUID, *, after_seq: int = 0) -> Sequence[RunEventRecord]:
        async with business_session(self._business_id) as s:
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


__all__ = ["PostgresRunStore"]
