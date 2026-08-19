"""Tests for the probe orchestration in `backend/app/services/geo_service.py`.

Hermetic: no network, no database, no real model. The two seams the service is
built around are exactly the two seams these tests replace -- a `Provider` stub
injected through `ModelRouter(providers=...)`, and an in-memory `ProbeStore`.
That is the point of both abstractions; if a test here needed a fixture, the
design would be wrong.

The cases are chosen around the ways a probe run can lie:

* a provider outage that reads as the brand being absent;
* a refusal counted in the denominator;
* one failed probe taking the whole run down, so the sample silently shrinks;
* a budget that can be exceeded 120 calls at a time;
* a "trend" computed against a run that asked different questions.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.engines.geo import (
    PROMPT_SET_VERSION,
    BrandIdentity,
    GeoPrompt,
    ProbeOutcome,
    ShareOfVoice,
    build_prompt_set,
    share_of_voice,
)
from backend.app.llm import (
    Completion,
    Message,
    ModelRouter,
    ModelTier,
    ProviderServerError,
    RouteEntry,
    ToolSpec,
    Usage,
)
from backend.app.services.geo_service import (
    PROBE_TASK,
    VisibilityReport,
    probe_models,
    probe_visibility,
)

BUSINESS = UUID("11111111-1111-1111-1111-111111111111")
BRAND = BrandIdentity(
    name="Müller Sanitär", aliases=["Sanitär Müller"], domains=["mueller-sanitaer.de"]
)
RIVAL = BrandIdentity(name="Sanitär Weber", domains=["sanitaer-weber.de"])

#: A priced model id served by a stub, so the budget guard does real arithmetic
#: instead of the `fake/*` zero-cost shortcut.
PRICED = "openai/gpt-4.1-mini"


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class StubProvider:
    """Satisfies `llm.Provider` structurally. Answers from a per-prompt script."""

    def __init__(
        self,
        *,
        answers: dict[str, str] | None = None,
        default: str = "Try Müller Sanitär in Koblenz.",
        fail_on: Sequence[str] = (),
        name: str = "stub",
        delay_s: float = 0.0,
    ) -> None:
        self.name = name
        self._answers = answers or {}
        self._default = default
        self._fail_on = tuple(fail_on)
        self._delay_s = delay_s
        self.calls: list[dict[str, object]] = []
        self.in_flight = 0
        self.max_in_flight = 0

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
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": tools,
                "messages": len(messages),
            }
        )
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            if any(marker in prompt for marker in self._fail_on):
                raise ProviderServerError(self.name, model, "503 upstream", status_code=503)
            text = self._answers.get(prompt, self._default)
            return Completion(
                text=text,
                tool_calls=[],
                usage=Usage(
                    provider=self.name,
                    model=model,
                    tokens_in=40,
                    tokens_out=60,
                    usd=Decimal("0.0001"),
                    latency_ms=7,
                ),
                is_final=True,
            )
        finally:
            self.in_flight -= 1


class FakeStore:
    """In-memory `ProbeStore`. Records call order, because ordering is a rule."""

    def __init__(self, previous: ShareOfVoice | None = None) -> None:
        self.previous = previous
        self.saved: list[ProbeOutcome] = []
        self.calls: list[str] = []
        self.saved_for: list[UUID] = []

    async def save_outcomes(self, business_id: UUID, outcomes: Sequence[ProbeOutcome]) -> int:
        self.calls.append("save_outcomes")
        self.saved_for.append(business_id)
        self.saved.extend(outcomes)
        return len(outcomes)

    async def latest_share_of_voice(self, business_id: UUID) -> ShareOfVoice | None:
        self.calls.append("latest_share_of_voice")
        return self.previous


def _router(provider: StubProvider, *, models: Sequence[str] = (PRICED,)) -> ModelRouter:
    """A router whose cheap tier is exactly `models`, all served by `provider`."""
    return ModelRouter(
        providers={provider.name: provider},
        chains={ModelTier.CHEAP: tuple(RouteEntry(provider.name, model) for model in models)},
    )


def _prompts(count: int = 3) -> list[GeoPrompt]:
    prompts = build_prompt_set(
        business_name=BRAND.name,
        city="Koblenz",
        services=("Badsanierung", "Heizungswartung", "Notdienst"),
        competitors=(RIVAL.name,),
        locale="de",
    )
    return prompts[:count]


async def _run(**overrides: object) -> tuple[VisibilityReport, StubProvider, FakeStore]:
    provider = overrides.pop("provider", None) or StubProvider()
    assert isinstance(provider, StubProvider)
    store = overrides.pop("store", None) or FakeStore()
    assert isinstance(store, FakeStore)
    kwargs: dict[str, object] = {
        "business_id": BUSINESS,
        "brand": BRAND,
        "prompts": _prompts(),
        "store": store,
        "router": _router(provider),
        "competitors": [RIVAL],
    }
    kwargs.update(overrides)
    report = await probe_visibility(**kwargs)  # type: ignore[arg-type]
    return report, provider, store


# --------------------------------------------------------------------------- #
# The shape of a run
# --------------------------------------------------------------------------- #


async def test_one_outcome_per_prompt_per_model() -> None:
    provider = StubProvider()
    report, _, store = await _run(
        provider=provider, router=_router(provider, models=(PRICED, "openai/gpt-4.1"))
    )

    assert report.probes_planned == 6  # 3 prompts x 2 models
    assert report.probes_run == 6
    assert len(report.outcomes) == 6
    assert len(store.saved) == 6
    assert report.saved_outcomes == 6
    assert report.share_of_voice.models_probed == 2


async def test_outcomes_are_ordered_prompt_then_model() -> None:
    """Deterministic order, so two runs of the same set render identically."""
    provider = StubProvider()
    report, _, _ = await _run(
        provider=provider, router=_router(provider, models=(PRICED, "openai/gpt-4.1"))
    )

    assert [(o.prompt_id, o.model) for o in report.outcomes] == [
        (prompt.prompt_id, model) for prompt in _prompts() for model in (PRICED, "openai/gpt-4.1")
    ]


async def test_the_probe_is_a_bare_user_question_with_no_temperature() -> None:
    """Two separate rules, both load-bearing.

    No system prompt: any instruction we add changes what the model would have
    told a real user, and then we are measuring our own prompt rather than the
    business's visibility. And `temperature` is never sent, because current Claude
    models reject the parameter outright.
    """
    _, provider, _ = await _run()

    for call in provider.calls:
        assert call["temperature"] is None
        assert call["tools"] is None
        assert call["messages"] == 1
        assert isinstance(call["max_tokens"], int)


async def test_mentions_and_competitors_are_both_detected() -> None:
    prompts = _prompts(2)
    provider = StubProvider(
        answers={
            prompts[0].text: "Ich empfehle Müller Sanitär (mueller-sanitaer.de).",
            prompts[1].text: "Sanitär Weber ist bekannt.",
        }
    )
    report, _, _ = await _run(provider=provider, prompts=prompts)

    sov = report.share_of_voice
    assert (sov.usable_answers, sov.mentions, sov.citations) == (2, 1, 1)
    assert [c.name for c in sov.competitors] == [RIVAL.name]


# --------------------------------------------------------------------------- #
# The rule the metric rests on
# --------------------------------------------------------------------------- #


async def test_a_refusal_is_excluded_from_the_denominator() -> None:
    """3 mentions of 8 probes with 2 refusals is 3/6, not 3/8."""
    prompts = build_prompt_set(
        business_name=BRAND.name,
        city="Koblenz",
        services=tuple(f"service {i}" for i in range(8)),
        competitors=(),
        locale="en",
    )[:8]
    answers = {
        **{prompts[i].text: "Müller Sanitär is a good option." for i in range(3)},
        **{prompts[i].text: "Consider a local Meisterbetrieb." for i in range(3, 6)},
        prompts[6].text: "I can't help with that.",
        prompts[7].text: "",
    }
    provider = StubProvider(answers=answers)

    report, _, store = await _run(provider=provider, prompts=prompts, competitors=[])

    sov = report.share_of_voice
    assert sov.probes_total == 8
    assert sov.no_answer_count == 2
    assert sov.usable_answers == 6
    assert sov.mentions == 3
    assert sov.mention_share_pct == 50.0
    assert sov.mention_share_pct != 37.5
    # A refusal is still data: "the model refused" is a fact worth storing.
    assert len(store.saved) == 8
    assert {o.error for o in store.saved if o.status == "no_answer"} == {"refusal_or_empty"}


async def test_a_failed_probe_becomes_no_answer_and_does_not_abort_the_batch() -> None:
    """A provider 503 on one question must not shrink the run to nothing, and must
    never be recorded as the brand being absent."""
    prompts = _prompts(3)
    provider = StubProvider(fail_on=(prompts[1].text,))

    report, _, _ = await _run(provider=provider, prompts=prompts)

    assert report.probes_run == 3
    failed = [o for o in report.outcomes if o.status == "no_answer"]
    assert len(failed) == 1
    assert failed[0].prompt_id == prompts[1].prompt_id
    assert failed[0].error is not None
    assert "503" in failed[0].error
    assert failed[0].mentioned is False
    assert report.share_of_voice.usable_answers == 2


async def test_an_unexpected_exception_is_also_contained() -> None:
    """One broken model must not cost the whole measurement."""

    class Exploding(StubProvider):
        async def complete(self, messages: Sequence[Message], **kwargs: object) -> Completion:
            raise RuntimeError("boom")

    provider = Exploding()
    report, _, _ = await _run(provider=provider)

    assert report.probes_run == 3
    assert report.share_of_voice.usable_answers == 0
    assert report.share_of_voice.mention_share_pct is None
    assert all(o.status == "no_answer" for o in report.outcomes)
    assert all("RuntimeError" in (o.error or "") for o in report.outcomes)


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


async def test_the_budget_stops_the_run_instead_of_raising() -> None:
    """40 prompts x 3 models is 120 calls. The ceiling must hold, and a hit
    ceiling is a truncated sample rather than a crashed run."""
    from backend.app.llm import BudgetState

    budget = BudgetState(limit_usd=Decimal("0.00000001"))
    report, provider, _ = await _run(budget=budget, concurrency=1)

    assert report.budget_exhausted is True
    assert report.probes_run == 0
    assert provider.calls == []
    assert report.share_of_voice.mention_share_pct is None
    assert any("budget" in caveat.lower() for caveat in report.caveats)


async def test_spend_is_recorded_against_the_budget() -> None:
    from backend.app.llm import BudgetState

    budget = BudgetState(limit_usd=Decimal("1.00"))
    report, _, _ = await _run(budget=budget)

    assert budget.spent_usd > Decimal(0)
    assert report.spent_usd == budget.spent_usd
    assert report.budget_exhausted is False


async def test_max_prompts_bounds_the_run_deterministically() -> None:
    prompts = _prompts(3)
    report, provider, _ = await _run(prompts=prompts, max_prompts=2)

    assert report.prompts_planned == 2
    assert report.probes_run == 2
    assert [call["prompt"] for call in provider.calls] == [p.text for p in prompts[:2]]


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


async def test_probes_run_concurrently_within_a_bounded_semaphore() -> None:
    prompts = _prompts(3)
    provider = StubProvider(delay_s=0.01)

    report, _, _ = await _run(provider=provider, prompts=prompts, concurrency=2)

    assert report.probes_run == 3
    assert provider.max_in_flight > 1  # genuinely concurrent
    assert provider.max_in_flight <= 2  # and genuinely bounded


# --------------------------------------------------------------------------- #
# The trend
# --------------------------------------------------------------------------- #


async def test_previous_score_is_read_before_the_new_one_is_written() -> None:
    """Otherwise "the latest run" is the run we just saved, and the trend compares
    a run against itself."""
    store = FakeStore(previous=None)
    await _run(store=store)

    assert store.calls == ["latest_share_of_voice", "save_outcomes"]


async def test_a_first_run_is_reported_as_a_baseline() -> None:
    report, _, _ = await _run(store=FakeStore(previous=None))

    assert report.delta.is_first_run is True
    assert report.delta.comparable is False
    assert any("baseline" in caveat.lower() for caveat in report.caveats)


async def test_a_matching_previous_run_produces_a_comparable_delta() -> None:
    prompts = _prompts(3)
    previous = share_of_voice(
        [
            ProbeOutcome(
                prompt_id=prompt.prompt_id,
                prompt_text=prompt.text,
                category=prompt.category,
                set_version=prompt.set_version,
                prompt_contains_brand=prompt.contains_brand,
                provider="stub",
                model=PRICED,
                status="answered",
                mentioned=False,
            )
            for prompt in prompts
        ]
    )
    report, _, _ = await _run(prompts=prompts, store=FakeStore(previous=previous))

    assert report.delta.comparable is True
    assert report.delta.mention_share_delta_pp == 100.0
    assert report.delta.direction == "up"


async def test_a_previous_run_on_different_questions_is_not_comparable() -> None:
    previous = share_of_voice(
        [
            ProbeOutcome(
                prompt_id="some-other-question",
                prompt_text="?",
                category="cost",
                set_version=PROMPT_SET_VERSION,
                prompt_contains_brand=False,
                provider="stub",
                model=PRICED,
                status="answered",
                mentioned=True,
            )
        ]
    )
    report, _, _ = await _run(store=FakeStore(previous=previous))

    assert report.delta.comparable is False
    assert report.delta.mention_share_delta_pp is None


# --------------------------------------------------------------------------- #
# Honesty of the report itself
# --------------------------------------------------------------------------- #


async def test_the_fake_provider_is_flagged_because_it_is_not_a_measurement() -> None:
    """With no credentials the router serves `FakeProvider`. Numbers from it are
    canned text, and a dashboard must not present them as visibility."""
    report, _, _ = await _run(router=ModelRouter(providers={}, env={}))

    assert report.using_fake_provider is True
    assert any("fake" in caveat.lower() for caveat in report.caveats)


async def test_the_report_always_carries_its_sample_size_in_words() -> None:
    report, _, _ = await _run()

    assert str(report.share_of_voice.usable_answers) in report.share_of_voice.headline
    assert any("sample" in caveat.lower() for caveat in report.caveats)


async def test_a_single_model_run_is_flagged() -> None:
    """One model is one vendor's opinion, not the AI answer landscape."""
    report, _, _ = await _run()

    assert report.share_of_voice.models_probed == 1
    assert any("one model" in caveat.lower() for caveat in report.caveats)


async def test_probe_models_come_from_the_router_not_from_a_literal() -> None:
    """No hardcoded model name outside the router's tables
    (docs/ARCHITECTURE.md section 8)."""
    provider = StubProvider()
    router = _router(provider, models=(PRICED, "openai/gpt-4.1", "google/gemini-2.5-flash"))

    assert [entry.model for entry in probe_models(router)] == [
        PRICED,
        "openai/gpt-4.1",
        "google/gemini-2.5-flash",
    ]
    assert [entry.model for entry in probe_models(router, limit=2)] == [PRICED, "openai/gpt-4.1"]
    assert PROBE_TASK.value in {"classify", "extract"}  # cheap tier only


async def test_empty_prompts_is_a_refusal_to_pretend() -> None:
    with pytest.raises(ValueError, match="at least one prompt"):
        await _run(prompts=[])


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #


def test_the_service_does_not_import_a_database() -> None:
    """Persistence goes through the `ProbeStore` port, so the real adapter can be
    swapped and these tests can stay hermetic. `tests/test_engine_boundary.py`
    guards engines; nothing else guards this file, so it is guarded here."""
    source = Path("backend/app/services/geo_service.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = ("sqlalchemy", "asyncpg", "psycopg", "alembic", "backend.app.db")
    assert not [
        name for name in imported if any(name == f or name.startswith(f"{f}.") for f in forbidden)
    ]
