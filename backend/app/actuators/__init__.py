"""Actuators: the layer that owns every external side effect.

`actuate()` is the entry point, and it is the only one. An actuator's own `perform`
does the call and nothing else — no idempotency, no persistence, no approval check —
because those three are exactly the concerns `docs/ARCHITECTURE.md` §3 says this layer
exists to hold in ONE place. A new integration that had to remember them would be a new
integration that forgot one.

    outcome = await actuate(actuation, actuator=actuator, store=store)

Read `contract.py` for the four rules. The order in `actuate` is the load-bearing part:
claim the key, refuse without approval, call, settle. Reversing the first two would leak
an `in_flight` row for something policy was always going to refuse; reversing the first
and third is the double post.
"""

from backend.app.actuators.actuate import actuate
from backend.app.actuators.contract import (
    ACTION_STATUSES,
    Actuation,
    ActuationRefusedError,
    Actuator,
    ActuatorError,
    ActuatorStore,
    Outcome,
    OutcomeStatus,
    idempotency_key,
)
from backend.app.actuators.fake import FakeActuator

__all__ = [
    "ACTION_STATUSES",
    "Actuation",
    "ActuationRefusedError",
    "Actuator",
    "ActuatorError",
    "ActuatorStore",
    "FakeActuator",
    "Outcome",
    "OutcomeStatus",
    "actuate",
    "idempotency_key",
]
