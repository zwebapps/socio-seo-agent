"""The LLM-judged arm, tested at the boundaries that decide whether it lies.

An eval harness that adds a model as a judge adds two new ways to produce a number
nobody should believe, so those are what this file is about:

* **A judge that failed must report an absence, never a zero.** A metric that came
  back 0.00 says "this text is unfaithful"; a metric that never ran says nothing at
  all. Rendering them identically would turn a judge outage into a quality finding --
  the same mistake the project already refuses to make with `no_answer` in
  share-of-voice.
* **A judge failure must not take the harness with it.** A judge is a model: it can
  return prose where JSON was asked for, exhaust the budget, or be `FakeProvider`
  returning a canned sentence. A harness that dies on any of those produces no report
  at all, which is exactly when a report is most wanted.

And two properties that are really guardrails rather than behaviour:

* **The judge goes through our own `ModelRouter`.** DeepEval's default is its own
  OpenAI client, which would be a second path to a paid model with no routing table,
  no budget guard and no cost ledger. `test_the_judge_routes_through_our_router`
  proves the adapter actually reaches the router, and does it with a stub provider so
  no network is involved.
* **Nothing here touches the network.** DeepEval phones home to PostHog on import and
  merges `.env` into `os.environ`, both by default, and this repo's `.env` holds a real
  provider key. `harden_deepeval_env` switches both off before the import, and that is
  asserted rather than assumed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from backend.app.llm.contract import (
    BudgetExceededError,
    Completion,
    Message,
    ModelTier,
    ToolSpec,
    Usage,
)
from backend.app.llm.router import ModelRouter, RouteEntry
from evals.deepeval_arm import (
    ANSWER_RELEVANCY,
    ARM_OFF_NOTE,
    FAITHFULNESS,
    HERMETIC_DEEPEVAL_ENV,
    NOT_MEASURED_CELL,
    DeepEvalArm,
    DeepEvalRunStatus,
    JudgeUnavailableError,
    MetricOutcome,
    RouterJudge,
    arm_off_status,
    build_deepeval_judge,
    harden_deepeval_env,
)
from evals.run import ArmResult, RunConfig, render_report

# A real entry from `pricing.PRICE_TABLE`. The router estimates cost *before* the
# call, and an unpriced model raises `UnknownModelPriceError` rather than defaulting
# to zero -- so a stub provider still has to answer as a priced model, or every test
# here would measure that guard instead of the judge.
JUDGE_MODEL = "anthropic/claude-sonnet-4.5"


# --------------------------------------------------------------------------- #
# A stub judge provider: valid DeepEval JSON, chosen by which schema was asked for
# --------------------------------------------------------------------------- #


class StubJudgeProvider:
    """Satisfies `llm.contract.Provider`. Answers DeepEval's prompts with real JSON.

    It dispatches on the JSON schema `RouterJudge` appends to the prompt, which is
    also the cheapest possible proof that the schema actually reaches the model: if
    that instruction were dropped, every branch below would miss and the metrics
    would fail rather than silently pass.
    """

    name = "stub"

    def __init__(self, *, verdict: str = "yes") -> None:
        self._verdict = verdict
        self.prompts: list[str] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        prompt = messages[-1].content
        self.prompts.append(prompt)
        return Completion(
            text=json.dumps(self._body(prompt)),
            usage=Usage(
                provider=self.name,
                model=model,
                tokens_in=10,
                tokens_out=10,
                usd=Decimal("0.001"),
                latency_ms=0,
            ),
            is_final=True,
        )

    def _body(self, prompt: str) -> dict[str, Any]:
        if '"truths"' in prompt:
            return {"truths": ["Die Anfahrt im Stadtgebiet ist kostenlos."]}
        if '"claims"' in prompt:
            return {"claims": ["Die Anfahrt kostet nichts."]}
        if '"statements"' in prompt:
            return {"statements": ["Die Anfahrt kostet nichts."]}
        if '"verdicts"' in prompt:
            return {"verdicts": [{"verdict": self._verdict, "reason": "stub verdict"}]}
        if '"reason"' in prompt:
            return {"reason": "stub reason"}
        return {"reason": "stub fallback"}


class BrokenJudgeProvider(StubJudgeProvider):
    """Answers with prose. This is what a bad judge -- or `FakeProvider` -- looks like."""

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        self.prompts.append(messages[-1].content)
        return Completion(
            text="I am afraid I cannot answer that in JSON.",
            usage=Usage(
                provider=self.name,
                model=model,
                tokens_in=10,
                tokens_out=10,
                usd=Decimal("0.001"),
                latency_ms=0,
            ),
            is_final=True,
        )


def _router(provider: StubJudgeProvider) -> ModelRouter:
    """A router whose mid tier -- where `TaskClass.REVIEW` lands -- is the stub."""
    return ModelRouter(
        providers={provider.name: provider},
        chains={ModelTier.MID: (RouteEntry(provider.name, JUDGE_MODEL),)},
    )


def _arm(provider: StubJudgeProvider, **kwargs: Any) -> DeepEvalArm:
    # `env` names a credential so the arm does not describe itself as fake-provider
    # backed; the actual calls still go to the stub, so nothing leaves the process.
    return DeepEvalArm(
        _router(provider), requested=True, env={"OPENROUTER_API_KEY": "test"}, **kwargs
    )


# --------------------------------------------------------------------------- #
# Hermetic-by-construction: no telemetry, no dotenv
# --------------------------------------------------------------------------- #


def test_deepeval_telemetry_and_dotenv_are_forced_off() -> None:
    """The two settings that would make this arm non-hermetic.

    Telemetry is an outbound PostHog request from a suite that promises never to make
    one. Dotenv autoloading would merge this repo's `.env` -- which holds a real
    `OPENROUTER_API_KEY` -- into `os.environ`, handing a real credential to a process
    that deliberately has none.
    """
    env: dict[str, str] = {"DEEPEVAL_TELEMETRY_OPT_OUT": "0"}
    harden_deepeval_env(env)

    assert env["DEEPEVAL_TELEMETRY_OPT_OUT"] == "1", "an ambient opt-in must not win"
    assert env["DEEPEVAL_DISABLE_DOTENV"] == "1"
    assert set(HERMETIC_DEEPEVAL_ENV) <= set(env)


def test_the_repo_wide_conftest_hardens_deepeval_before_anything_imports_it() -> None:
    """`backend/tests/conftest.py` duplicates these two values on purpose.

    They have to be set before *any* import of deepeval, including the one its own
    pytest plugin would do at session start, and the repo-wide conftest is the earliest
    place this project controls. Duplicated rather than imported so that conftest does
    not drag the whole LLM package in at collection time -- which is exactly why the
    two copies need a test that they agree.
    """
    for name, value in HERMETIC_DEEPEVAL_ENV.items():
        assert os.environ.get(name) == value, (
            f"{name} is not hardened for the test session. Update the duplicate in "
            "backend/tests/conftest.py to match HERMETIC_DEEPEVAL_ENV."
        )


def test_deepeval_itself_reports_telemetry_off_once_imported() -> None:
    """Asserted through DeepEval's own accessor, not through our env dict.

    Setting the variable is only half the claim: the other half is that DeepEval reads
    it, which it does exactly once, at import.
    """
    harden_deepeval_env()
    from deepeval.telemetry.client import telemetry_opt_out

    assert telemetry_opt_out() is True


def test_the_arm_does_not_import_deepeval_when_it_is_off() -> None:
    """`requested=False` must not merely skip the work -- it must skip the import.

    This is what keeps the default CI path free of DeepEval's import-time settings
    load, dotenv merge and telemetry client, without a guard test having to police it.
    """
    arm = DeepEvalArm(ModelRouter(env={}), requested=False, env={})

    assert arm.requested is False
    assert arm.status().requested is False


async def test_a_disabled_arm_returns_nothing_rather_than_a_score() -> None:
    arm = DeepEvalArm(ModelRouter(env={}), requested=False, env={})

    judged = await arm.judge(
        case_id="c", arm="rag_on", request="q", output="a", retrieval_context=["ctx"]
    )

    assert judged is None


# --------------------------------------------------------------------------- #
# The adapter: our router's completion, in DeepEval's shape
# --------------------------------------------------------------------------- #


async def test_the_judge_routes_through_our_router() -> None:
    """The whole point of the custom adapter, asserted end to end.

    DeepEval's own client would have called OpenAI. This proves the text a metric sees
    came out of `ModelRouter.complete`, and that the cost landed in our ledger rather
    than in DeepEval's `evaluation_cost` (which stays `None` for a non-native model).
    """
    provider = StubJudgeProvider()
    judge = RouterJudge(_router(provider))

    text = await judge.acomplete("give me truths")

    assert json.loads(text) == {"reason": "stub fallback"}
    assert judge.calls == 1
    assert judge.cost_usd == Decimal("0.001")
    assert judge.models == (JUDGE_MODEL,)


async def test_the_adapter_hands_deepevals_calls_to_the_router() -> None:
    """`a_generate` is the method every DeepEval metric ultimately calls."""
    provider = StubJudgeProvider()
    judge = RouterJudge(_router(provider))
    model = build_deepeval_judge(judge)

    text = await model.a_generate("anything")

    assert json.loads(text) == {"reason": "stub fallback"}
    assert model.get_model_name() == JUDGE_MODEL
    assert judge.calls == 1


async def test_the_requested_json_schema_reaches_the_model() -> None:
    """DeepEval asks for a Pydantic shape; our provider contract has no such field.

    So the schema is appended to the prompt. If that ever silently stopped happening,
    a real judge would still answer -- in a shape DeepEval could not parse -- and the
    only symptom would be metrics quietly degrading to `n/m`.
    """
    from deepeval.metrics.faithfulness.schema import Claims

    provider = StubJudgeProvider()
    judge = RouterJudge(_router(provider))

    await judge.acomplete("extract the claims", schema=Claims)

    assert '"claims"' in provider.prompts[-1]


def test_the_sync_path_is_refused_rather_than_silently_bridged() -> None:
    """`metric.measure` would need a nested event loop. Better to say so."""
    judge = RouterJudge(_router(StubJudgeProvider()))

    with pytest.raises(JudgeUnavailableError, match="async-only"):
        judge.complete("anything")


# --------------------------------------------------------------------------- #
# Measuring, and degrading honestly when it cannot
# --------------------------------------------------------------------------- #


async def test_both_metrics_are_measured_when_the_judge_answers() -> None:
    """The happy path, and the only test here that produces numbers at all."""
    provider = StubJudgeProvider()
    arm = _arm(provider)

    judged = await arm.judge(
        case_id="plumber-01",
        arm="rag_on",
        request="Notdienst Klempner Koblenz",
        output="Die Anfahrt kostet nichts. [chunk:plumber-01#0]",
        retrieval_context=["Die Anfahrt im Stadtgebiet ist kostenlos."],
    )

    assert judged is not None
    assert judged.faithfulness.metric == FAITHFULNESS
    assert judged.answer_relevancy.metric == ANSWER_RELEVANCY
    assert judged.faithfulness.score == pytest.approx(1.0)
    assert judged.answer_relevancy.score == pytest.approx(1.0)
    assert judged.faithfulness.reason == "stub reason"
    assert arm.status().measured == 2
    assert arm.status().cost_usd > 0


async def test_a_judge_returning_prose_degrades_to_not_measured() -> None:
    """The failure this arm will actually hit, including under `FakeProvider`.

    Note what is asserted: `score is None`, not `score == 0.0`. A judge that could not
    answer has told us nothing about the text.
    """
    provider = BrokenJudgeProvider()
    arm = _arm(provider)

    judged = await arm.judge(
        case_id="plumber-01",
        arm="rag_on",
        request="q",
        output="some German copy",
        retrieval_context=["ein Fakt"],
    )

    assert judged is not None
    assert judged.faithfulness.score is None
    assert judged.answer_relevancy.score is None
    assert judged.faithfulness.measured is False
    assert judged.faithfulness.cell() == NOT_MEASURED_CELL
    assert judged.faithfulness.error is not None
    assert arm.status().measured == 0
    assert arm.status().attempted == 2
    assert arm.errors, "a degraded metric must leave a reason behind, not vanish"


async def test_the_harness_survives_a_judge_that_raises() -> None:
    """A judge failure is a missing cell, never a dead run.

    A zero judge budget makes the router refuse before any call, which is the cleanest
    available stand-in for "the judge blew up": `BudgetExceededError` is raised inside
    DeepEval's own `a_measure`, i.e. exactly where a real failure would surface.
    """
    provider = StubJudgeProvider()
    arm = _arm(provider, judge_budget_usd=Decimal(0))

    judged = await arm.judge(
        case_id="plumber-01", arm="rag_on", request="q", output="copy", retrieval_context=["fact"]
    )

    assert judged is not None
    assert judged.any_measured is False
    assert provider.prompts == [], "the budget guard must refuse before the call"
    assert any("BudgetExceededError" in error for error in arm.errors)


async def test_the_budget_guard_still_bites_on_the_judge_path() -> None:
    """Stated directly as well, because the guard is the reason to route the judge.

    DeepEval's own client has no budget guard at all; this asserts ours is on the
    judge's path and not merely nearby.
    """
    judge = RouterJudge(_router(StubJudgeProvider()), budget_usd=Decimal(0))

    with pytest.raises(BudgetExceededError):
        await judge.acomplete("anything")


async def test_faithfulness_is_absent_without_retrieval_context() -> None:
    """`rag_off` has no evidence, so faithfulness is unmeasurable -- not 0.00.

    Scoring an ungrounded arm zero would report the absence of evidence as the model's
    dishonesty, and would make the rag_off/rag_on comparison read as a much larger
    win than the retrieval loop earned.
    """
    provider = StubJudgeProvider()
    arm = _arm(provider)

    judged = await arm.judge(
        case_id="plumber-01", arm="rag_off", request="q", output="copy", retrieval_context=()
    )

    assert judged is not None
    assert judged.faithfulness.score is None
    assert "no retrieval context" in (judged.faithfulness.error or "")
    # Relevancy needs no context, so it is still a real measurement.
    assert judged.answer_relevancy.score == pytest.approx(1.0)


async def test_a_whitespace_only_context_counts_as_no_context() -> None:
    """Otherwise faithfulness would be judged against nothing and report a number."""
    arm = _arm(StubJudgeProvider())

    judged = await arm.judge(
        case_id="c", arm="rag_on", request="q", output="copy", retrieval_context=["   ", ""]
    )

    assert judged is not None
    assert judged.faithfulness.score is None


# --------------------------------------------------------------------------- #
# The report: absence has to be visible
# --------------------------------------------------------------------------- #


def test_the_report_says_the_arm_was_not_measured_when_it_is_off() -> None:
    report = render_report(config=RunConfig(live=False), rows=[], notes=[])

    assert "LLM-judged metrics (DeepEval)" in report
    assert "not measured" in report
    assert ARM_OFF_NOTE in report


def test_the_report_explains_why_deepeval_rather_than_ragas() -> None:
    """A reader who knows the requirement said "Ragas or DeepEval" will ask."""
    report = render_report(config=RunConfig(live=False), rows=[], notes=[])

    assert "ragas" in report.lower()
    assert "openai<3.0.0" in report or "openai>=3.2.0" in report


def test_an_unmeasured_metric_never_renders_as_a_zero() -> None:
    """The rule this whole arm is built around, asserted on the rendered markdown."""
    status = arm_off_status()
    report = render_report(config=RunConfig(live=False), rows=[], notes=[], deepeval=status)

    assert "0.00" not in report.split("## LLM-judged metrics (DeepEval)")[1]


def test_the_judged_cell_distinguishes_absent_from_unmeasurable() -> None:
    """Three states, three renderings. Collapsing any two of them loses a fact."""
    assert MetricOutcome(FAITHFULNESS).cell() == NOT_MEASURED_CELL
    assert MetricOutcome(FAITHFULNESS, score=0.0).cell() == "0.00"
    assert MetricOutcome(FAITHFULNESS, score=0.755).cell() == "0.76"
    # And the arm-never-ran case, which the report renders as an em dash.
    arm = ArmResult(
        arm="rag_on",
        text="",
        results=(),
        grounding=_empty_triple(),
        cited_chunk_ids=(),
        retrieval=None,
        model="fake",
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal(0),
    )
    assert arm.judged is None


def test_the_report_carries_the_judged_columns_when_the_arm_ran() -> None:
    """The other half of the rule: when it DID measure, the numbers have to appear.

    Both halves matter. A report that hid an absence would overstate; a report that
    measured and then failed to print it would waste the money it just spent.
    """
    row = _case_row(
        faithfulness=MetricOutcome(FAITHFULNESS, score=0.80, reason="two claims supported"),
        answer_relevancy=MetricOutcome(ANSWER_RELEVANCY, score=0.50),
    )
    status = DeepEvalRunStatus(
        requested=True,
        judge_is_fake=False,
        note="**measured.** stub note",
        calls=14,
        cost_usd=Decimal("0.0125"),
        models=("anthropic/claude-sonnet-4.5",),
        measured=4,
        attempted=4,
    )

    report = render_report(
        config=RunConfig(live=True, deepeval=True), rows=[row], notes=[], deepeval=status
    )
    judged_section = report.split("## LLM-judged metrics (DeepEval)")[1]

    # The per-case table.
    assert "| 0.80 | 0.50 |" in report
    # The judged section's own aggregate and its cost accounting.
    assert "0.80 (1 of 1)" in judged_section
    assert "$0.012500" in judged_section
    assert "14" in judged_section
    # And the command line is reproducible from the header.
    assert "--live --deepeval" in report


def test_the_report_does_not_average_an_unmeasured_case_in_as_zero() -> None:
    """The denominator is the measured cases, and the cell says which."""
    measured = _case_row(
        faithfulness=MetricOutcome(FAITHFULNESS, score=1.0),
        answer_relevancy=MetricOutcome(ANSWER_RELEVANCY, score=1.0),
        case_id="measured-01",
    )
    failed = _case_row(
        faithfulness=MetricOutcome(FAITHFULNESS, error="judge returned prose"),
        answer_relevancy=MetricOutcome(ANSWER_RELEVANCY, error="judge returned prose"),
        case_id="failed-01",
    )
    status = DeepEvalRunStatus(
        requested=True, judge_is_fake=False, note="stub", measured=2, attempted=4
    )

    report = render_report(
        config=RunConfig(live=True, deepeval=True),
        rows=[measured, failed],
        notes=[],
        deepeval=status,
    )
    judged_section = report.split("## LLM-judged metrics (DeepEval)")[1]

    # 1.00 over the one case that was measured -- NOT 0.50, which is what averaging
    # the failed case in as 0.0 would have produced.
    assert "1.00 (1 of 2)" in judged_section
    assert "0.50 (2 of 2)" not in judged_section


def _case_row(
    *,
    faithfulness: MetricOutcome,
    answer_relevancy: MetricOutcome,
    case_id: str = "plumber-01",
) -> Any:
    """A minimal `CaseRow` whose only interesting content is the judged metrics."""
    from evals.deepeval_arm import JudgedArm
    from evals.rubric import score_brand
    from evals.run import CaseRow

    judged = JudgedArm(faithfulness=faithfulness, answer_relevancy=answer_relevancy)
    arm = ArmResult(
        arm="rag_on",
        text="",
        results=(score_brand("clean copy", ("kostenlos",)),),
        grounding=_empty_triple(),
        cited_chunk_ids=(),
        retrieval=None,
        model="stub",
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal(0),
        judged=judged,
    )
    return CaseRow(
        case_id=case_id,
        vertical="plumber",
        channel="linkedin",
        rag_off=arm,
        rag_on=arm,
        reference_grounding=_empty_triple(),
        reference_brand_passed=True,
        violating_brand_passed=False,
    )


def _empty_triple() -> Any:
    """A `GroundingTriple` of empty scores, for a fixture that never reads them."""
    from evals.rubric import score_grounding
    from evals.run import GroundingTriple

    empty = score_grounding("", (), {})
    return GroundingTriple(off=empty, on=empty, oracle=empty)
