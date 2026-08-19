"""Persist what each model call cost. The other half of the cost ledger.

`ModelUsage` has existed since the Phase 1 schema, with a docstring calling itself "the
cost ledger" and `contract.Usage` saying rows are "persisted as a `model_usage` row
(docs/ARCHITECTURE.md section 8), which is what makes 'what does a content piece cost
us?' a query rather than a guess."

Nothing wrote one. The table was structurally empty, `runs.used_usd` was never written
either, and the developer console's cost dashboard therefore had to report figures as
UNAVAILABLE rather than show a confident `$0.00` — which was the right call, and is the
gap this module closes.

**Why a buffer rather than a write per call.** The router's sink is synchronous: it is on
the hot path of every node, and awaiting an insert there would put node latency behind a
ledger write. So calls are appended to a list and flushed once per node, in the async
part of the run where a database round trip already belongs.

**Why losing a row must not fail a run.** The ledger is a record OF the work, not the
work. A model call that has already been paid for must not be discarded because its
accounting row would not insert — so `flush` logs and drops. The consequence is stated
plainly rather than hidden: the ledger can under-report, and it can never over-report,
which is the safe direction for a number an operator reads as spend.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from backend.app.db.models import ModelUsage
from backend.app.db.session import business_session
from backend.app.llm.contract import Usage

logger: Final = logging.getLogger(__name__)


class UsageRecorder:
    """Collects `Usage` from the router and writes `model_usage` rows.

    One per run. The run id comes from here rather than from the router's trace context
    because the router has no idea what a run is -- it is handed a task and a chain --
    whereas the executor that constructed this knows exactly which run it is driving.
    """

    def __init__(self, *, run_id: UUID, business_id: UUID) -> None:
        self._run_id = run_id
        self._business_id = business_id
        self._pending: list[tuple[Usage, Mapping[str, str]]] = []
        self.recorded = 0

    def sink(self, usage: Usage, context: Mapping[str, str]) -> None:
        """The router's `UsageSink`. Synchronous, so it only buffers."""
        self._pending.append((usage, dict(context)))

    @property
    def pending(self) -> int:
        """Buffered rows not yet written. Exists so a test can assert the flush happened."""
        return len(self._pending)

    async def flush(self) -> None:
        """Write and clear the buffer. Never raises.

        Cleared BEFORE the insert is attempted, so a failing flush cannot retry the same
        rows on the next node and multiply one call into several ledger entries. Losing a
        row is under-reporting; duplicating one is over-reporting spend, which is the
        error an operator would act on.
        """
        if not self._pending:
            return
        batch = self._pending
        self._pending = []

        try:
            async with business_session(self._business_id) as s:
                for usage, context in batch:
                    s.add(
                        ModelUsage(
                            id=uuid4(),
                            business_id=self._business_id,
                            run_id=self._run_id,
                            # The node the router was told about. `None` rather than a
                            # guess when absent: an unattributed row is honest, and a
                            # wrong attribution silently misreports which step is
                            # expensive.
                            node=context.get("node") or None,
                            provider=usage.provider,
                            model=usage.model,
                            prompt_version=context.get("prompt_version") or None,
                            tokens_in=usage.tokens_in,
                            tokens_out=usage.tokens_out,
                            usd=usage.usd,
                            latency_ms=usage.latency_ms,
                        )
                    )
            self.recorded += len(batch)
        except Exception:
            logger.exception(
                "could not write %d model_usage row(s) for run %s", len(batch), self._run_id
            )

    def total_usd(self) -> Decimal:
        """What this recorder has seen, including anything still buffered.

        Sums `Usage.usd` rather than reading the state's running total, so the number
        agrees with the rows about to be written rather than with a parallel count.
        """
        return sum((usage.usd for usage, _ in self._pending), Decimal("0"))


__all__ = ["UsageRecorder"]
