"""`actuate()`: idempotency, approval and audit, written once for every integration.

The order of the four steps below is the whole design, and each one is reversible-looking
and is not:

1. **Claim the key.** If it is already held by a succeeded action, return THAT outcome
   and do not call. The unique index on `actions.idempotency_key` is the lock, so the
   claim is also the write that makes a concurrent second attempt lose.
2. **Check approval.** After the claim, because a refusal is an auditable event and
   should leave a row saying so — but before the call, obviously.
3. **Call.** The only step that can touch the outside world, and the only one an
   integration writes.
4. **Settle.** Record what happened, including a failure. An `in_flight` row that is
   never settled is the signal a reconciler looks for; one that is settled as `failed`
   is a decision somebody made.
"""

from __future__ import annotations

import logging
from typing import Final

from backend.app.actuators.contract import (
    Actuation,
    ActuationRefusedError,
    Actuator,
    ActuatorError,
    ActuatorStore,
    Outcome,
    OutcomeStatus,
)

logger: Final = logging.getLogger(__name__)

__all__ = ["actuate"]


async def actuate(
    actuation: Actuation,
    *,
    actuator: Actuator,
    store: ActuatorStore,
) -> Outcome:
    """Perform one side effect, exactly once, and record it. Never raises.

    Returning an `Outcome` for every path rather than raising is deliberate: the caller
    is a graph node, and a node that dies on a failed publish takes the rest of the
    run's output with it. Every failure mode here — no approval, provider down, a
    mismatched actuator — comes back as a recorded outcome the timeline can render.
    """
    if actuator.action_type != actuation.action_type:
        # A wiring mistake, and it must not be allowed to look like a provider failure:
        # publishing a LinkedIn post through the email actuator would "succeed".
        return Outcome(
            status=OutcomeStatus.REFUSED,
            action_type=actuation.action_type,
            target=actuation.target,
            error=(
                f"wrong actuator: {actuation.action_type!r} was routed to one that "
                f"performs {actuator.action_type!r}"
            ),
        )

    existing = await store.claim(actuation)
    if existing is not None:
        # The effect has already happened. Reporting it as new would tell a customer
        # they have two posts; reporting an error would tell them they have none.
        logger.info(
            "actuation replayed: type=%s target=%s key=%s",
            actuation.action_type,
            actuation.target,
            actuation.idempotency_key(),
        )
        return Outcome(
            status=existing.status,
            action_type=existing.action_type,
            target=existing.target,
            external_ref=existing.external_ref,
            detail=existing.detail,
            error=existing.error,
            replayed=True,
            fake=existing.fake,
        )

    if not actuation.approved_by.strip():
        outcome = Outcome(
            status=OutcomeStatus.REFUSED,
            action_type=actuation.action_type,
            target=actuation.target,
            error=(
                "no approval on record. Nothing publishes without one -- the REVIEW "
                "interrupt is what produces it, and an actuator that could be called "
                "without it would make that gate decorative."
            ),
        )
        await store.settle(actuation, outcome)
        return outcome

    try:
        outcome = await actuator.perform(actuation)
    except ActuationRefusedError as exc:
        outcome = Outcome(
            status=OutcomeStatus.REFUSED,
            action_type=actuation.action_type,
            target=actuation.target,
            error=str(exc),
            fake=actuator.fake,
        )
    except ActuatorError as exc:
        # WARNING rather than exception(): a retryable provider blip is not a defect in
        # this code, and a stack trace per rate limit is how a log becomes unreadable.
        logger.warning(
            "actuation failed: type=%s target=%s retryable=%s error=%s",
            actuation.action_type,
            actuation.target,
            exc.retryable,
            exc,
        )
        outcome = Outcome(
            status=OutcomeStatus.FAILED,
            action_type=actuation.action_type,
            target=actuation.target,
            error=str(exc),
            detail={"retryable": exc.retryable},
            fake=actuator.fake,
        )
    except Exception as exc:
        # Logged with a traceback, unlike the two above: this one IS unexpected, and the
        # `in_flight` row it would otherwise leave is the thing a reconciler chases.
        logger.exception("actuation raised unexpectedly: type=%s", actuation.action_type)
        outcome = Outcome(
            status=OutcomeStatus.FAILED,
            action_type=actuation.action_type,
            target=actuation.target,
            error=f"{type(exc).__name__}: {exc}",
            fake=actuator.fake,
        )

    await store.settle(actuation, outcome)
    return outcome
