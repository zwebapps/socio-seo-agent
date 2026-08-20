"""An LLM-judged evaluation arm, built on DeepEval, beside the deterministic rubric.

The five scorers in `evals/rubric.py` are arithmetic: they count characters, match
hashtags, and check that the *figures* in a claim appear in a chunk the model said it
cited. That is deliberate ("if the answer is computable, compute it") and it leaves
exactly one hole, which `evals/run.py`'s limitations section has always named: a
sentence can cite a real chunk and then **misdescribe it in words**, and every
deterministic scorer passes it. Closing that hole needs a reader, so this module adds
one -- as a **second arm, never a replacement**. The rubric still runs, still reports,
and is still what the report leads with.

Two metrics, both DeepEval's own classes rather than reimplementations:

* **`FaithfulnessMetric`** -- decomposes the output into claims, the retrieved context
  into truths, and asks per claim whether the context supports it. This is the
  semantic half of `score_grounding`.
* **`AnswerRelevancyMetric`** -- decomposes the output into statements and asks how
  many of them actually address the request. Nothing deterministic here measures
  relevance at all; `score_coverage` only checks that required strings appear.

**Why DeepEval and not Ragas.** The module requirement names either. Ragas is not
installable in this project, and the reason is a genuine version wall rather than a
preference: `ragas>=0.4.3` depends on `instructor`, which caps `openai<3.0.0`, while
this project pins `openai>=3.2.0` on purpose -- openai v3 is built on httpx2, which is
why `httpx2` is a declared dev dependency (see the comment beside it in
`pyproject.toml`). Downgrading the SDK to fit an eval library would change the
transport under the shipped provider adapter, which is the wrong tail wagging the wrong
dog. The older `ragas==0.3.1` avoids that pin but imports
`langchain_community.chat_models.vertexai`, a module that no longer exists in
langchain-community 1.x, so it fails at import. DeepEval 4.1.8 installs and imports
cleanly against our pins. Recorded as a row in `docs/ARCHITECTURE.md` §14.

**The judge goes through our own `ModelRouter`.** DeepEval's default is its own OpenAI
client reading `OPENAI_API_KEY`, which would put a second, unrouted, unbudgeted,
unledgered path to a paid model into the repo -- past the routing table, past
`BudgetState`, past the "never a hardcoded model id at a call site" rule, and past the
"a missing credential means the FAKE provider" rule. So `RouterJudge` below does the
work and `build_deepeval_judge()` wraps it in DeepEval's `DeepEvalBaseLLM`. The judge
asks for `TaskClass.REVIEW`, exactly like any other caller: it names a task, never a
model.

**Hermetic by construction.** Nothing here is imported unless `--deepeval` is passed
(see `_deepeval_metrics`), and DeepEval phones home to PostHog on import unless told
not to -- so `harden_deepeval_env()` runs *before* the import and switches off both the
telemetry and DeepEval's own dotenv autoloading. The second one matters as much as the
first: `.env` in this repo holds a real `OPENROUTER_API_KEY`, and a library that
quietly loads it would defeat the whole reason `evals/run.py` refuses to call
`load_dotenv` at import time.

**A metric that cannot be measured is absent, never zero.** `MetricOutcome.score` is
`None` when the judge failed, when the budget ran out, or when there is no retrieval
context to check faithfulness against. That is the same rule the project applies to
`no_answer` in share-of-voice: a measurement that was never taken and a measurement
that came back bad are different facts, and rendering them identically turns the first
into a fabrication.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from backend.app.llm.contract import BudgetState, Message, Role, TaskClass
from backend.app.llm.router import TASK_TIERS, ModelRouter, config_status

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: The judge asks for a TASK, like every other caller in this codebase. `REVIEW` is
#: the right one and it is already mapped to the mid tier by `router.TASK_TIERS` --
#: judging is a reading job, and the deterministic rubric does the cheap checks.
JUDGE_TASK: Final = TaskClass.REVIEW

#: Per case-arm ceiling for the judge, matching the harness's own per-case budget.
#: Faithfulness costs four judge calls and answer relevancy three, so this bounds one
#: case-arm at seven mid-tier calls rather than bounding the whole run -- a single
#: pathological case then degrades to `not measured` instead of starving every case
#: after it.
DEFAULT_JUDGE_BUDGET_USD: Final = Decimal("0.50")

#: The score threshold DeepEval uses to call a metric passed. Reported, not acted on:
#: nothing in this harness gates on an LLM judgement.
DEFAULT_THRESHOLD: Final = 0.7

FAITHFULNESS: Final = "faithfulness"
ANSWER_RELEVANCY: Final = "answer_relevancy"

#: What the report prints for a cell that was never measured. Deliberately not `0.00`.
NOT_MEASURED_CELL: Final = "n/m"

#: Environment that must be in place BEFORE `deepeval` is imported.
#:
#: `DEEPEVAL_TELEMETRY_OPT_OUT` stops the PostHog capture in
#: `deepeval/telemetry/client.py`. `DEEPEVAL_DISABLE_DOTENV` stops DeepEval merging
#: `.env` into `os.environ` at import, which in this repo would inject a real provider
#: key into a process that took care not to have one.
#:
#: Both are FORCED rather than defaulted. The thing being switched off is outbound
#: network from a test suite that promises never to make any, so an ambient
#: `DEEPEVAL_TELEMETRY_OPT_OUT=0` in someone's shell must not win.
HERMETIC_DEEPEVAL_ENV: Final[Mapping[str, str]] = {
    "DEEPEVAL_TELEMETRY_OPT_OUT": "1",
    "DEEPEVAL_DISABLE_DOTENV": "1",
}

_JUDGE_SYSTEM: Final = (
    "You are an evaluation judge. Follow the instructions exactly and reply with "
    "nothing but the JSON object they ask for -- no prose, no code fence."
)


def harden_deepeval_env(env: MutableMapping[str, str] | None = None) -> None:
    """Force DeepEval's telemetry and dotenv autoloading off.

    Must run before the first `import deepeval`, because DeepEval reads its settings
    once, at import. Exposed (rather than inlined) so a test can assert the values
    without importing DeepEval.
    """
    target = os.environ if env is None else env
    for name, value in HERMETIC_DEEPEVAL_ENV.items():
        target[name] = value


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


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


@dataclass(frozen=True, slots=True)
class DeepEvalRunStatus:
    """Run-level state of the arm, rendered verbatim into the report header.

    `requested` -- not `calls > 0` -- is what decides whether the report shows the
    columns at all, so "the arm ran and every metric failed" cannot be mistaken for
    "the arm was never asked to run".
    """

    requested: bool
    judge_is_fake: bool
    note: str
    calls: int = 0
    cost_usd: Decimal = Decimal(0)
    models: tuple[str, ...] = ()
    measured: int = 0
    attempted: int = 0


#: The wording for an arm that was never asked to run. A module-level constant rather
#: than a string built at the call site, so the report says the same thing whether it
#: was handed a status by the runner or fell back to this default.
ARM_OFF_NOTE: Final = (
    "**not measured.** The LLM-judged arm is off by default, exactly like `--live`: "
    "pass `--deepeval` to run it. The columns below are absent, not zero — no "
    "faithfulness or relevancy value is estimated from the deterministic scores, "
    "because a judged number that is really a rubric average would be a fabrication."
)


def arm_off_status() -> DeepEvalRunStatus:
    """The status of an arm that was never requested.

    Exists so `render_report` has something honest to say when it is called without a
    status at all -- a report that simply omitted the row would leave a reader to
    assume the metrics were measured and merely uninteresting.
    """
    return DeepEvalRunStatus(requested=False, judge_is_fake=True, note=ARM_OFF_NOTE)


# --------------------------------------------------------------------------- #
# The judge: our router, wearing DeepEval's interface
# --------------------------------------------------------------------------- #


class JudgeUnavailableError(RuntimeError):
    """The judge was asked for something it cannot do in this harness."""


class RouterJudge:
    """The judge LLM. Deliberately knows nothing about DeepEval.

    Everything that matters -- routing by task class, the pre-call budget guard, the
    cost ledger -- lives here, where it can be tested without importing DeepEval at
    all. `build_deepeval_judge` wraps this in DeepEval's abstract base class, and that
    wrapper is the only DeepEval-aware code in the module.

    The split is not tidiness. DeepEval's `DeepEvalBaseLLM` declares its methods as
    `(*args, **kwargs)`, so a subclass is effectively untyped; keeping the real logic
    outside it means `mypy --strict` checks the part that can be wrong.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        task: TaskClass = JUDGE_TASK,
        budget_usd: Decimal = DEFAULT_JUDGE_BUDGET_USD,
    ) -> None:
        self._router = router
        self._task = task
        self._budget_usd = budget_usd
        self._budget = BudgetState(limit_usd=budget_usd)
        self._calls = 0
        self._cost_usd = Decimal(0)
        # A dict, not a set: the report lists the models that served, and a set would
        # reorder them between runs for no reason.
        self._models: dict[str, None] = {}

    @property
    def calls(self) -> int:
        """How many judge calls have been made across the whole run."""
        return self._calls

    @property
    def cost_usd(self) -> Decimal:
        """What the judging has cost, from the router's own usage figures."""
        return self._cost_usd

    @property
    def models(self) -> tuple[str, ...]:
        """Which models actually served, in first-seen order."""
        return tuple(self._models)

    def reset_budget(self) -> None:
        """Start a fresh per-case-arm budget. Totals are cumulative and survive."""
        self._budget = BudgetState(limit_usd=self._budget_usd)

    async def acomplete(self, prompt: str, schema: type[Any] | None = None) -> str:
        """One judge call. Returns the raw text; JSON parsing is DeepEval's job.

        `schema` is DeepEval's request for a particular JSON shape. Our provider
        contract has no structured-output parameter -- `Completion` is text plus tool
        calls -- so the schema is appended to the prompt as an instruction, which is
        what an unstructured judge can honestly offer. DeepEval then parses the text
        with its own lenient `trimAndLoadJson`, and a reply it cannot parse surfaces
        here as a *failed* metric rather than as a fabricated score.
        """
        messages = [
            Message(role=Role.SYSTEM, content=_JUDGE_SYSTEM),
            Message(role=Role.USER, content=_with_schema_instruction(prompt, schema)),
        ]
        completion = await self._router.complete(self._task, messages, budget=self._budget)
        usage = completion.usage
        self._calls += 1
        self._cost_usd += usage.usd
        self._models.setdefault(usage.model, None)
        return completion.text or ""

    def complete(self, prompt: str, schema: type[Any] | None = None) -> str:
        """Refused on purpose: this harness only ever uses the async path.

        `DeepEvalBaseLLM.generate` is abstract, so something has to exist here. Making
        it raise is safer than making it work: the router is async, and the sync
        bridges DeepEval offers for that (`get_or_create_event_loop`) would run a
        nested loop inside the harness's own `asyncio.run`. If this is ever raised, the
        caller used `metric.measure` where it should have used `metric.a_measure`.
        """
        raise JudgeUnavailableError(
            "RouterJudge is async-only: call `metric.a_measure(...)`, not "
            "`metric.measure(...)`. The model router is async and nesting an event "
            "loop inside the harness's own would deadlock."
        )


def _with_schema_instruction(prompt: str, schema: type[Any] | None) -> str:
    """Append the requested JSON shape to a judge prompt, when there is one.

    DeepEval's own templates already ask for JSON; this adds the exact field names, so
    a model that would otherwise invent a plausible-but-different key produces
    something `trimAndLoadJson` can use. It is best-effort by design -- a schema whose
    JSON schema cannot be rendered simply contributes nothing rather than failing the
    call.
    """
    if schema is None:
        return prompt
    dumper = getattr(schema, "model_json_schema", None)
    if dumper is None:
        return prompt
    try:
        rendered = json.dumps(dumper(), sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return prompt
    return f"{prompt}\n\nReturn ONLY a JSON object matching this schema:\n{rendered}"


def build_deepeval_judge(judge: RouterJudge) -> Any:
    """Wrap `RouterJudge` in DeepEval's `DeepEvalBaseLLM`.

    Imported lazily and defined inside the function, so that a run without
    `--deepeval` never imports DeepEval -- which is what keeps the default hermetic
    path free of DeepEval's import-time settings, dotenv loading and telemetry client.

    Returns `Any` rather than `DeepEvalBaseLLM` because naming that type in an
    annotation would require the import at module scope, defeating the point.
    """
    base = _deepeval_base_llm()

    class _RouterBackedJudge(base):  # type: ignore[misc,valid-type]
        # The two ignore codes are the whole cost of the lazy import: `base` is a
        # local variable rather than a static name, so mypy cannot treat it as a
        # class (`valid-type`) or as a valid base (`misc`). Narrowed to this one
        # line on purpose -- everything that can actually be wrong lives in
        # `RouterJudge`, which is checked normally.
        """DeepEval's interface, delegating every call to `RouterJudge`."""

        def __init__(self, inner: RouterJudge) -> None:
            self._inner = inner
            super().__init__(model=_judge_model_name(inner))

        def load_model(self, *args: Any, **kwargs: Any) -> Any:
            """DeepEval loads a client here. Ours is already built."""
            return self._inner

        def get_model_name(self, *args: Any, **kwargs: Any) -> str:
            return _judge_model_name(self._inner)

        def generate(self, *args: Any, **kwargs: Any) -> str:
            return self._inner.complete(*args, **kwargs)

        async def a_generate(self, *args: Any, **kwargs: Any) -> str:
            return await self._inner.acomplete(*args, **kwargs)

    return _RouterBackedJudge(judge)


def _judge_model_name(judge: RouterJudge) -> str:
    """A label for the report: the models that served, or the route if none yet.

    DeepEval stores this on the metric as `evaluation_model`, so it should say
    something true even before the first call -- hence the tier name as the fallback
    rather than an invented model id.
    """
    served = judge.models
    if served:
        return ", ".join(served)
    return f"router:{JUDGE_TASK.value}/{TASK_TIERS[JUDGE_TASK].value}"


# --------------------------------------------------------------------------- #
# Lazy DeepEval imports
# --------------------------------------------------------------------------- #


def _deepeval_base_llm() -> Any:
    """`DeepEvalBaseLLM`, after the environment has been hardened."""
    harden_deepeval_env()
    from deepeval.models.base_model import DeepEvalBaseLLM

    return DeepEvalBaseLLM


def _deepeval_metrics() -> tuple[Any, Any, Any]:
    """`(FaithfulnessMetric, AnswerRelevancyMetric, LLMTestCase)`, imported lazily."""
    harden_deepeval_env()
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    return FaithfulnessMetric, AnswerRelevancyMetric, LLMTestCase


# --------------------------------------------------------------------------- #
# The arm
# --------------------------------------------------------------------------- #


class DeepEvalArm:
    """The opt-in LLM-judged arm. Off unless `requested` is true.

    Mirrors `--live`'s posture: default off, and the report says which state produced
    its numbers. When it is off, `judge()` returns `None` and DeepEval is never
    imported -- so CI stays hermetic without a guard test having to prove it, because
    the code path does not exist.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        requested: bool,
        env: Mapping[str, str] | None = None,
        judge_budget_usd: Decimal = DEFAULT_JUDGE_BUDGET_USD,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._requested = requested
        self._threshold = threshold
        self._judge_is_fake = config_status(env=env).using_fake_provider
        self._judge = RouterJudge(router, budget_usd=judge_budget_usd) if requested else None
        self._model: Any | None = None
        self._measured = 0
        self._attempted = 0
        self._errors: list[str] = []

    @property
    def requested(self) -> bool:
        """Whether `--deepeval` was passed."""
        return self._requested

    @property
    def errors(self) -> tuple[str, ...]:
        """Every judge failure, in the order they happened."""
        return tuple(self._errors)

    async def judge(
        self,
        *,
        case_id: str,
        arm: str,
        request: str,
        output: str,
        retrieval_context: Sequence[str],
    ) -> JudgedArm | None:
        """Score one arm of one case. `None` when the arm is off.

        `retrieval_context` empty means faithfulness is **not measured** rather than
        failed: there is nothing to check the claims against, and a 0.00 would report
        the retrieval loop's silence as the model's dishonesty.
        """
        judge = self._judge
        if judge is None:
            return None

        judge.reset_budget()
        faithfulness_metric, relevancy_metric, test_case_cls = _deepeval_metrics()
        if self._model is None:
            self._model = build_deepeval_judge(judge)

        context = [text for text in retrieval_context if text.strip()]
        test_case = test_case_cls(
            input=request,
            actual_output=output,
            retrieval_context=context or None,
        )

        if context:
            faithfulness = await self._measure(
                FAITHFULNESS,
                faithfulness_metric(
                    model=self._model,
                    threshold=self._threshold,
                    include_reason=True,
                    async_mode=True,
                ),
                test_case,
                label=f"{case_id}/{arm}",
            )
        else:
            faithfulness = MetricOutcome(
                FAITHFULNESS,
                error=(
                    "no retrieval context: faithfulness has nothing to check the "
                    "output's claims against"
                ),
            )

        answer_relevancy = await self._measure(
            ANSWER_RELEVANCY,
            relevancy_metric(
                model=self._model,
                threshold=self._threshold,
                include_reason=True,
                async_mode=True,
            ),
            test_case,
            label=f"{case_id}/{arm}",
        )
        return JudgedArm(faithfulness=faithfulness, answer_relevancy=answer_relevancy)

    async def _measure(
        self,
        name: str,
        metric: Any,
        test_case: Any,
        *,
        label: str,
    ) -> MetricOutcome:
        """Run one metric, converting any failure into `not measured`.

        The broad `except` is the point of this method. A judge is a model, and a
        model can return prose where JSON was asked for, exhaust the budget, or be
        the fake provider returning a canned sentence -- and a harness that dies on
        any of those produces no report at all, which is the moment a report is most
        wanted. `BaseException` is deliberately NOT caught: a `KeyboardInterrupt`
        must still stop the run.
        """
        self._attempted += 1
        try:
            score = await metric.a_measure(test_case, _show_indicator=False)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self._errors.append(f"{label} / {name}: {detail}")
            return MetricOutcome(name, error=detail)

        if score is None:
            self._errors.append(f"{label} / {name}: metric returned no score")
            return MetricOutcome(name, error="metric returned no score")

        self._measured += 1
        reason = getattr(metric, "reason", None)
        return MetricOutcome(name, score=float(score), reason=reason if reason else None)

    def status(self) -> DeepEvalRunStatus:
        """Everything the report header needs to describe this arm honestly."""
        judge = self._judge
        return DeepEvalRunStatus(
            requested=self._requested,
            judge_is_fake=self._judge_is_fake,
            note=self._note(),
            calls=judge.calls if judge else 0,
            cost_usd=judge.cost_usd if judge else Decimal(0),
            models=judge.models if judge else (),
            measured=self._measured,
            attempted=self._attempted,
        )

    def _note(self) -> str:
        """One sentence for the report, and the most important string in this module.

        A table of LLM-judged numbers produced by `FakeProvider` looks exactly like
        one produced by a frontier model. Only this sentence can tell them apart.
        """
        if not self._requested:
            return ARM_OFF_NOTE
        if self._judge_is_fake:
            return (
                "**requested, but nothing was judged.** No model provider is "
                "configured, so the judge was served by `FakeProvider`, which returns "
                "a canned string chosen by hashing the prompt — not a judgement. Every "
                "cell therefore reads `n/m` (not measured), which is what proves the "
                "plumbing runs hermetically and refuses to invent a score. Add "
                "`--live` with `OPENROUTER_API_KEY` set to obtain real judgements; "
                "that spends real money on top of the generation calls."
            )
        judge = self._judge
        models = ", ".join(f"`{model}`" for model in judge.models) if judge else ""
        # "measured" vs "nothing was judged" is decided by the count, not by the
        # flag. A run with a real credential whose judge returned unparseable prose
        # every time has measured exactly as much as a fake one, and must not be
        # allowed to say otherwise just because a key was present.
        headline = (
            "**measured.**"
            if self._measured
            else "**requested, but nothing was judged** — every metric failed; see the list below."
        )
        return (
            f"{headline} The judge was routed through our own `ModelRouter` on the "
            f"`{JUDGE_TASK.value}` task class ({TASK_TIERS[JUDGE_TASK].value} tier), so "
            f"it obeyed the routing table, the pre-call budget guard and the cost "
            f"ledger rather than DeepEval's own OpenAI client. "
            + (f"Models that served: {models}. " if models else "")
            + f"{self._measured} of {self._attempted} metric measurements returned a "
            "score; the rest are reported as `n/m`, never as 0.00."
        )
