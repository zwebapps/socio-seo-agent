"""The actuator that runs when no credential is configured — and says so.

The same posture `CLAUDE.md`'s model rule states for providers: a missing credential
means the FAKE, plus a status that says so. Never a silent no-op (a customer would
believe their post is live), never a crash (an unconfigured integration must not end a
run that produced good content), and never synthetic output passed off as real.

`Outcome.fake` is what carries that upward, and every surface that renders an outcome is
expected to show it. A simulated publish that looks identical to a real one is the worst
thing this layer could produce.
"""

from __future__ import annotations

from backend.app.actuators.contract import Actuation, Actuator, Outcome, OutcomeStatus

__all__ = ["FakeActuator"]


class FakeActuator:
    """Records the intent, reaches nothing, and marks the outcome `fake`.

    Deterministic: the same actuation yields the same `external_ref`, so a replay test
    can tell "the store returned the prior result" from "the fake ran twice" — which are
    the same output and very different bugs.
    """

    def __init__(self, action_type: str) -> None:
        self._action_type = action_type

    @property
    def action_type(self) -> str:
        return self._action_type

    @property
    def fake(self) -> bool:
        return True

    async def perform(self, actuation: Actuation) -> Outcome:
        key = actuation.idempotency_key()
        return Outcome(
            status=OutcomeStatus.SUCCEEDED,
            action_type=actuation.action_type,
            target=actuation.target,
            # Obviously not a URL. A plausible-looking fake ref is how a simulated post
            # ends up in a report as evidence.
            external_ref=f"fake://{actuation.action_type}/{actuation.target}#{key[-8:]}",
            detail={
                "simulated": True,
                "reason": (
                    "no credential is configured for this integration, so nothing left this process"
                ),
            },
            fake=True,
        )


def _satisfies_protocol(actuator: FakeActuator) -> Actuator:
    """Compile-time proof that the fake satisfies the port. mypy checks this line."""
    return actuator
