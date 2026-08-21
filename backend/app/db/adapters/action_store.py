"""The `actions` ledger: the unique index that makes publishing idempotent.

`claim` is the whole point of this class, and its contract is unusual for a reason. It
either RESERVES the key by inserting an `in_flight` row (returning None) or hands back
the outcome the key already has. A store that answered "exists / does not exist" would
leave the caller to fetch the prior result in a second call, and the gap between those
two calls is exactly where a double post lives.

The insert is the lock. `actions.idempotency_key` is uniquely indexed, so two concurrent
attempts race at the database and one loses with an `IntegrityError` — which is then
read as "somebody else holds this", not as an error to surface. Doing the check with a
SELECT first would be a check-then-act with a window in it.

Tenant-scoped through `business_session`, like every other adapter here: whose action a
row is, is row-level security's question, not an `if`'s.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from backend.app.actuators.contract import Actuation, Outcome, OutcomeStatus
from backend.app.db.models import Action
from backend.app.db.session import business_session

__all__ = ["PostgresActionStore"]


class PostgresActionStore:
    """The real `ActuatorStore`. One instance is fine; each call opens its own session."""

    async def claim(self, actuation: Actuation) -> Outcome | None:
        """Reserve the key, or return the outcome it already holds.

        A row that is still `in_flight` is returned as a REFUSAL rather than reserved
        again: something is already doing this, or something died doing it, and either
        way a second call must not make the request. That is deliberately conservative —
        the failure mode it prevents is a double publish, and the one it accepts is a
        stuck action a human resolves.
        """
        key = actuation.idempotency_key()

        async with business_session(actuation.business_id) as session:
            try:
                session.add(
                    Action(
                        business_id=actuation.business_id,
                        run_id=actuation.run_id,
                        action_type=actuation.action_type,
                        idempotency_key=key,
                        status="in_flight",
                        payload=dict(actuation.payload),
                    )
                )
                await session.flush()
            except IntegrityError:
                # Somebody else holds the key. Roll back this transaction before
                # reading, or the session is unusable.
                await session.rollback()
            else:
                return None

        async with business_session(actuation.business_id) as session:
            row = (
                await session.execute(select(Action).where(Action.idempotency_key == key))
            ).scalar_one_or_none()

        if row is None:
            # The key exists for ANOTHER tenant: the index is global and RLS hides the
            # row. Refusing is the only safe answer -- we cannot see what happened, so
            # we must not assume it did not.
            return Outcome(
                status=OutcomeStatus.REFUSED,
                action_type=actuation.action_type,
                target=actuation.target,
                error="that idempotency key is already in use and not visible here",
            )

        if row.status == "in_flight":
            return Outcome(
                status=OutcomeStatus.REFUSED,
                action_type=actuation.action_type,
                target=actuation.target,
                error=(
                    "an attempt at this action is already in flight (or a process died "
                    "mid-call). Nothing was sent; resolve the existing action first."
                ),
            )

        result: dict[str, Any] = dict(row.result or {})
        return Outcome(
            status=OutcomeStatus(row.status),
            action_type=row.action_type,
            target=actuation.target,
            external_ref=row.external_ref,
            detail=result,
            error=row.error,
            fake=bool(result.get("simulated", False)),
        )

    async def settle(self, actuation: Actuation, outcome: Outcome) -> None:
        """Record what happened. Silent for an unknown key.

        Silent rather than raising, because `settle` runs in the tail of an actuation
        that may already have failed: turning "the row vanished" into a second exception
        would lose the outcome we were trying to record.
        """
        async with business_session(actuation.business_id) as session:
            await session.execute(
                update(Action)
                .where(Action.idempotency_key == actuation.idempotency_key())
                .values(
                    status=outcome.status.value,
                    external_ref=outcome.external_ref,
                    result=dict(outcome.detail),
                    error=outcome.error,
                )
            )

    async def published_since(
        self, business_id: UUID, *, action_types: Collection[str], since: datetime
    ) -> int:
        """How many publishes this business has committed to since `since`.

        Satisfies `services.publish_cap.PublishLedger`. Counted in SQL rather than by
        pulling rows and measuring the list, for the reason `cost_service` gives about
        its own sums: the number is the only thing we want back.

        **`in_flight` counts as well as `succeeded`, and that is the conservative
        direction on purpose.** A cap exists to prevent over-publishing, so where the
        count is uncertain it must err towards refusing: an attempt that is mid-call will
        almost certainly land, and counting only settled rows would let two concurrent
        publishes both find room for the last slot. The cost of the choice is bounded and
        self-healing — a row abandoned `in_flight` by a dead process holds one slot, and
        only until it ages out of the rolling window.

        `refused` and `failed` are NOT counted: nothing was published, so nothing was
        produced, and counting a refusal would let a rejected post consume the allowance
        that would have let its replacement out.
        """
        if not action_types:
            return 0

        async with business_session(business_id) as session:
            counted = (
                await session.execute(
                    select(func.count())
                    .select_from(Action)
                    .where(
                        Action.action_type.in_(list(action_types)),
                        Action.status.in_(("succeeded", "in_flight")),
                        Action.created_at >= since,
                    )
                )
            ).scalar_one()
        return int(counted or 0)

    async def recent(self, business_id: UUID, *, limit: int = 50) -> list[Action]:
        """This business's actions, newest first. For the audit view."""
        async with business_session(business_id) as session:
            rows = (
                await session.execute(
                    select(Action).order_by(Action.created_at.desc(), Action.id.desc()).limit(limit)
                )
            ).scalars()
            return list(rows)
