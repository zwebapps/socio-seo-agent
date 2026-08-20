"""The shapes an LLM-judged metric comes back in, shared by both judged arms.

Two arms now produce these: `evals/deepeval_arm.py` (in-process, judge routed through
our own `ModelRouter`) and `evals/ragas_arm.py` (out-of-process, because Ragas cannot
share a virtualenv with this codebase — see that module and `ARCHITECTURE.md` §14).
They agree on the RESULT shape and on nothing else, which is the point: the report
renders one set of cells whichever arm filled them, and the header says which did.

They live here rather than in either arm because the alternative was one arm importing
the other's module for a dataclass — and this repo has just spent a commit deleting two
copies of a channel-limit table that drifted. A shared shape belongs in a shared place
the first time there are two consumers, not the third.

Pure: no I/O, no model, no subprocess. Importing this pulls in neither DeepEval nor
Ragas.
"""

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ANSWER_RELEVANCY",
    "ARM_OFF_CELL",
    "FAITHFULNESS",
    "NOT_MEASURED_CELL",
    "JudgedArm",
    "MetricOutcome",
]

#: The two metrics. Named constants because they are column headers, dictionary keys
#: and subprocess payload keys at once, and a typo in any one of those is a silently
#: empty column.
FAITHFULNESS: Final = "faithfulness"
ANSWER_RELEVANCY: Final = "answer_relevancy"

#: What a cell says when the arm RAN and the judge produced nothing usable.
#:
#: Distinct from :data:`ARM_OFF_CELL` on purpose, and neither is ever `0.00`. "The
#: judge could not answer" tells us nothing about the text; "0.00" says the text is
#: entirely unfaithful. Conflating them is how an outage reads as a quality collapse.
NOT_MEASURED_CELL: Final = "n/m"

#: What a cell says when the arm was never asked to run.
ARM_OFF_CELL: Final = "—"


@dataclass(frozen=True, slots=True)
class MetricOutcome:
    """One LLM-judged metric on one generated output.

    `score is None` means **not measured**, and the reason is in `error`. It is never
    coerced to 0.0: a judge that failed to answer has told us nothing about the text,
    whereas 0.0 would say the text is entirely unfaithful.
    """

    metric: str
    score: float | None = None
    reason: str | None = None
    error: str | None = None

    @property
    def measured(self) -> bool:
        """Whether a real judgement was obtained."""
        return self.score is not None

    def cell(self) -> str:
        """This metric as one markdown table cell."""
        return f"{self.score:.2f}" if self.score is not None else NOT_MEASURED_CELL


@dataclass(frozen=True, slots=True)
class JudgedArm:
    """Both LLM-judged metrics for one arm of one case."""

    faithfulness: MetricOutcome
    answer_relevancy: MetricOutcome

    @property
    def any_measured(self) -> bool:
        """Whether at least one metric produced a number."""
        return self.faithfulness.measured or self.answer_relevancy.measured

    def outcomes(self) -> tuple[MetricOutcome, ...]:
        """Both outcomes, in report order."""
        return (self.faithfulness, self.answer_relevancy)


def not_measured(metric: str, error: str) -> MetricOutcome:
    """A metric that was attempted and produced nothing, with the reason kept.

    A helper rather than a constructor call at forty sites, because the one thing that
    must never happen here is a `score=0.0` standing in for a failure.
    """
    return MetricOutcome(metric=metric, error=error)
