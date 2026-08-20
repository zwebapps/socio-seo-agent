"""The Ragas arm: real Ragas metrics, run in their own interpreter.

`docs/CRITERIA_MAP.md` asks for Ragas or DeepEval and this repo now has both, for
different reasons. DeepEval judges IN-PROCESS with the judge routed through our own
`ModelRouter`. Ragas cannot: `ragas` depends on `instructor`, which caps
`openai<3.0.0`, and this project pins `openai>=3.2.0` deliberately — v3 is built on
httpx2, which is why `httpx2` is a declared dev dependency and why respx cannot
intercept our provider calls. Downgrading the SDK to fit an eval library would change
the transport under the shipped provider adapter, which is not a trade an eval gets to
make.

So Ragas runs in `.venv-ragas` (create it with `make ragas-env`), driven as a
subprocess with a JSON payload in and a JSON result out. Four consequences, all of
them stated rather than discovered:

* **The judge does not go through `ModelRouter`.** The child calls an
  OpenAI-compatible endpoint directly. What is preserved: the model id is RESOLVED
  HERE from our routing table (so nothing is hardcoded at a call site and a tier
  change still moves the judge), and the child reports token usage back so the spend
  lands in the report's accounting instead of vanishing. What is not preserved: the
  per-call budget guard and the fallback chain. `MAX_SAMPLES` is the crude replacement
  — a hard ceiling on how much one invocation can be asked to judge.
* **It is BATCHED, not per-arm.** Ragas evaluates a dataset, so the arm collects every
  case-arm during the run and resolves once at the end. That is also why it is cheap
  enough to be worth having: one interpreter start, not eighty.
* **`answer_relevancy` needs an embeddings endpoint**, because Ragas measures it by
  embedding generated questions against the original. Without one it is reported as
  not measured, with that reason — the alternative is a number derived from nothing.
* **Every failure is "not measured", never `0.00`, and never an exception.** A missing
  venv, an unparseable reply, a dead endpoint: each comes back as a stated absence.
  An eval harness that dies because its optional arm is misconfigured has taken the
  deterministic scorers down with it, and those are the ones that gate drafts.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from backend.app.llm.contract import TaskClass
from backend.app.llm.openrouter_provider import OPENROUTER_BASE_URL
from backend.app.llm.pricing import compute_usd
from backend.app.llm.router import ModelRouter
from evals.judged import (
    ANSWER_RELEVANCY,
    FAITHFULNESS,
    JudgedArm,
    MetricOutcome,
    not_measured,
)

__all__ = [
    "MAX_SAMPLES",
    "RAGAS_PYTHON",
    "RagasArm",
    "RagasRunStatus",
    "ragas_arm_off_status",
]

#: The interpreter the child runs under. A path rather than a flag, because the whole
#: point is that it is NOT this process's interpreter.
RAGAS_PYTHON: Final = Path(".venv-ragas/bin/python")

#: The child script, resolved relative to this file so the harness works from any cwd.
RAGAS_RUNNER: Final = Path(__file__).with_name("ragas_runner.py")

#: How to build the environment, quoted verbatim into the report and into the
#: not-measured reason. A message that names the command is worth ten that describe it.
SETUP_HINT: Final = "make ragas-env"

#: Hard ceiling on samples per invocation.
#:
#: The budget guard is the thing this arm gives up by running out-of-process, so this
#: is what stands in for it: 40 samples is the full dataset's two arms, and a payload
#: larger than that is a bug in the caller rather than a bigger eval.
MAX_SAMPLES: Final = 40

#: How long the child gets. Ragas makes several judge calls per sample, so this scales
#: with the dataset; the floor covers interpreter start plus imports, which are slow.
TIMEOUT_BASE_S: Final = 90
TIMEOUT_PER_SAMPLE_S: Final = 45

#: Why an ungrounded arm's faithfulness is discarded even when Ragas returns a number.
#:
#: MEASURED, not assumed: driven against a stub judge, Ragas returned
#: `faithfulness = 1.00` for a sample whose `retrieved_contexts` was EMPTY. It has no
#: contradiction to find in an empty context, so every claim passes by default -- and
#: 1.00 on the arm that was given no documents would be the most flattering number in
#: the report and the most meaningless. The DeepEval arm refuses the same case for the
#: same reason; this one has to refuse it on the way back, because the refusal has to
#: survive whatever the library decides to compute.
NO_CONTEXT_REASON: Final = (
    "faithfulness needs something to check the claims against, and this arm was given "
    "no retrieval context -- an ungrounded output is unverifiable, not faithful. Ragas "
    "scores an empty context as 1.00 (verified), so the score is discarded rather than "
    "reported"
)

#: The task whose tier the judge borrows. REVIEW rather than GENERATE: judging is a
#: careful reading, not a piece of writing, and this keeps the judge off the strong
#: tier that the thing being judged was written on.
JUDGE_TASK: Final = TaskClass.REVIEW


@dataclass(frozen=True, slots=True)
class RagasRunStatus:
    """Run-level state of the arm, rendered verbatim into the report header.

    `requested` -- not `calls > 0` -- decides whether the report shows the columns, so
    "it ran and every metric failed" cannot be mistaken for "it was never asked".
    """

    requested: bool
    available: bool
    note: str
    model: str = ""
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    measured: int = 0
    attempted: int = 0


def ragas_arm_off_status() -> RagasRunStatus:
    """The status for a run that never asked for this arm."""
    return RagasRunStatus(
        requested=False,
        available=False,
        note="not run (pass `--ragas` to enable it)",
    )


@dataclass
class _Sample:
    """One case-arm awaiting judgement."""

    key: str
    question: str
    answer: str
    contexts: tuple[str, ...]


@dataclass
class RagasArm:
    """Collect during the run, judge once at the end.

    Built unconditionally and inert unless `requested`: with the flag off, `record`
    stores nothing, `resolve` spawns nothing, and no subprocess or Ragas import
    exists on the default path -- which is what keeps CI hermetic by construction
    rather than by a guard test.
    """

    router: ModelRouter
    requested: bool = False
    env: Mapping[str, str] | None = None
    python: Path = RAGAS_PYTHON
    runner: Path = RAGAS_RUNNER
    _samples: list[_Sample] = field(default_factory=list, init=False, repr=False)
    _errors: list[str] = field(default_factory=list, init=False, repr=False)
    _status: RagasRunStatus | None = field(default=None, init=False, repr=False)

    # ---------------------------------------------------------------- collect ---- #

    def record(
        self,
        *,
        case_id: str,
        arm: str,
        request: str,
        output: str,
        retrieval_context: Sequence[str],
    ) -> str | None:
        """Queue one arm for judgement. Returns its key, or None when the arm is off.

        An arm with NO retrieval context is queued anyway, and Ragas is told the truth
        about the empty context rather than handed a placeholder -- but its faithfulness
        score is DISCARDED on the way back. See :data:`NO_CONTEXT_REASON`: the library
        returns 1.00 for an empty context, because there is nothing there to contradict.
        """
        if not self.requested:
            return None
        if len(self._samples) >= MAX_SAMPLES:
            self._errors.append(
                f"more than {MAX_SAMPLES} samples were offered; {case_id}/{arm} was not judged"
            )
            return None

        key = f"{case_id}::{arm}"
        self._samples.append(
            _Sample(
                key=key,
                question=request,
                answer=output,
                contexts=tuple(retrieval_context),
            )
        )
        return key

    # ----------------------------------------------------------------- judge ---- #

    async def resolve(self) -> dict[str, JudgedArm]:
        """Run Ragas over everything recorded. Never raises.

        Every exit sets `_status`, so the report can always say what happened -- a
        header that silently omits the arm is indistinguishable from a run that did
        not ask for it.
        """
        if not self.requested:
            self._status = ragas_arm_off_status()
            return {}

        if not self._samples:
            self._status = RagasRunStatus(
                requested=True, available=True, note="no samples were offered to judge"
            )
            return {}

        missing = self._missing_prerequisite()
        if missing is not None:
            self._status = RagasRunStatus(requested=True, available=False, note=missing)
            return self._all_not_measured(missing)

        model = self._judge_model()
        payload = {
            "model": model,
            # Overridable so the child can be driven against a local stub endpoint in
            # a verification run without a real provider or a real spend. Unset in
            # normal use, which is what the default is for.
            "base_url": (self.env or {}).get("RAGAS_BASE_URL") or OPENROUTER_BASE_URL,
            "api_key": self._api_key(),
            "embeddings_model": (self.env or {}).get("RAGAS_EMBEDDINGS_MODEL", ""),
            "samples": [
                {
                    "key": sample.key,
                    "question": sample.question,
                    "answer": sample.answer,
                    "contexts": list(sample.contexts),
                }
                for sample in self._samples
            ],
        }

        outcome = await self._spawn(payload)
        if isinstance(outcome, str):
            self._status = RagasRunStatus(requested=True, available=True, note=outcome, model=model)
            self._errors.append(outcome)
            return self._all_not_measured(outcome)

        return self._map(outcome, model=model)

    # ------------------------------------------------------------- reporting ---- #

    def status(self) -> RagasRunStatus:
        """What to print in the header. Always available after `resolve`."""
        return self._status or ragas_arm_off_status()

    @property
    def errors(self) -> tuple[str, ...]:
        """Every failure, in the order they happened."""
        return tuple(self._errors)

    # --------------------------------------------------------------- internals -- #

    def _missing_prerequisite(self) -> str | None:
        """Why this arm cannot run, or None. Checked before anything is spawned."""
        if not self.python.exists():
            return (
                f"the isolated environment is missing: `{self.python}` does not exist. "
                f"Create it with `{SETUP_HINT}` -- Ragas cannot share this project's "
                "venv, because it caps `openai<3` and we pin `openai>=3.2`"
            )
        if not self.runner.exists():
            return f"the runner script is missing: `{self.runner}` does not exist"
        if not self._api_key():
            return (
                "no `OPENROUTER_API_KEY` is set, and an out-of-process judge has no "
                "fake provider to fall back to -- so nothing was judged rather than "
                "judged by nothing"
            )
        return None

    def _api_key(self) -> str:
        env = self.env
        if env is None:
            import os

            env = os.environ
        return str(env.get("OPENROUTER_API_KEY", "") or "")

    def _judge_model(self) -> str:
        """The model id, resolved from OUR routing table rather than hardcoded here.

        The one thing the out-of-process design does keep from `ModelRouter`: a tier
        change still moves the judge, and no model id is written at a call site.
        """
        route = self.router.resolve(JUDGE_TASK)
        return route.chain[0].model if route.chain else ""

    async def _spawn(self, payload: dict[str, Any]) -> dict[str, Any] | str:
        """Run the child. Returns its parsed result, or a reason as a string."""
        timeout = TIMEOUT_BASE_S + TIMEOUT_PER_SAMPLE_S * len(self._samples)
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.python),
                str(self.runner),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return f"could not start the Ragas interpreter: {exc}"

        try:
            raw_out, raw_err = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode()), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return f"the Ragas subprocess did not finish within {timeout}s and was killed"

        if not raw_out.strip():
            detail = raw_err.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            return f"the Ragas subprocess wrote nothing on stdout ({detail[0]})"

        try:
            parsed = json.loads(raw_out.decode())
        except json.JSONDecodeError as exc:
            # stdout is a contract: the child forces its chatter to stderr, so this
            # means something printed where it must not.
            return f"the Ragas subprocess wrote something that is not JSON: {exc}"

        if not isinstance(parsed, dict):
            return "the Ragas subprocess returned a payload that is not an object"
        if "error" in parsed:
            return f"Ragas failed: {parsed['error']}"
        return parsed

    def _map(self, outcome: Mapping[str, Any], *, model: str) -> dict[str, JudgedArm]:
        """Turn the child's JSON into `JudgedArm`s and record what it cost."""
        results = outcome.get("results")
        if not isinstance(results, Mapping):
            reason = "the Ragas subprocess returned no results block"
            self._status = RagasRunStatus(requested=True, available=True, note=reason, model=model)
            self._errors.append(reason)
            return self._all_not_measured(reason)

        judged: dict[str, JudgedArm] = {}
        measured = 0
        for sample in self._samples:
            row = results.get(sample.key)
            entry = row if isinstance(row, Mapping) else {}
            raw_errors = entry.get("errors")
            errors: Mapping[str, Any] = raw_errors if isinstance(raw_errors, Mapping) else {}
            # An empty context means faithfulness is not measurable, whatever came
            # back. See NO_CONTEXT_REASON: Ragas returns 1.00 there, and reporting it
            # would put the report's best number on its least grounded output.
            faithfulness = (
                not_measured(FAITHFULNESS, NO_CONTEXT_REASON)
                if not sample.contexts
                else self._outcome(FAITHFULNESS, entry, errors)
            )
            arm = JudgedArm(
                faithfulness=faithfulness,
                answer_relevancy=self._outcome(ANSWER_RELEVANCY, entry, errors),
            )
            judged[sample.key] = arm
            measured += sum(1 for item in arm.outcomes() if item.measured)
            for metric, detail in errors.items():
                self._errors.append(f"{sample.key} {metric}: {detail}")

        tokens_in = int(outcome.get("tokens_in", 0) or 0)
        tokens_out = int(outcome.get("tokens_out", 0) or 0)
        self._status = RagasRunStatus(
            requested=True,
            available=True,
            note=f"ran out-of-process against `{self.python}`",
            model=model,
            calls=int(outcome.get("calls", 0) or 0),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            # Priced with OUR table, so this arm's spend is comparable with every
            # other number in the report even though the call did not go through our
            # router. Zero when the child could not report usage, which is honest:
            # an unknown cost is not a free one, and the call count is beside it.
            cost_usd=compute_usd(model, tokens_in, tokens_out),
            measured=measured,
            attempted=len(self._samples) * 2,
        )
        return judged

    @staticmethod
    def _outcome(metric: str, entry: Mapping[str, Any], errors: Mapping[str, Any]) -> MetricOutcome:
        value = entry.get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return MetricOutcome(metric=metric, score=float(value))
        detail = errors.get(metric) or errors.get("evaluate") or "no score was returned"
        return not_measured(metric, str(detail))

    def _all_not_measured(self, reason: str) -> dict[str, JudgedArm]:
        """One not-measured pair per sample, so every cell has a stated reason."""
        return {
            sample.key: JudgedArm(
                faithfulness=not_measured(FAITHFULNESS, reason),
                answer_relevancy=not_measured(ANSWER_RELEVANCY, reason),
            )
            for sample in self._samples
        }


def main() -> int:  # pragma: no cover - a convenience for checking the environment
    """`python -m evals.ragas_arm` reports whether the isolated env is usable."""
    arm = RagasArm(ModelRouter(), requested=True)
    missing = arm._missing_prerequisite()
    if missing:
        print(f"ragas arm: NOT usable — {missing}")
        return 1
    print(f"ragas arm: usable — {arm.python} present, judge model {arm._judge_model()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
