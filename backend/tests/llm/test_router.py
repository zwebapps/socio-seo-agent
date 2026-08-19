"""Routing, fallback, the budget guard, and the no-credentials degradation."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import pytest

from backend.app.llm.contract import (
    AllProvidersFailedError,
    BudgetExceededError,
    BudgetState,
    Completion,
    LlmError,
    Message,
    ModelTier,
    ModelUnavailableError,
    Provider,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    Role,
    TaskClass,
    ToolSpec,
    Usage,
)
from backend.app.llm.fake_provider import FakeProvider
from backend.app.llm.pricing import compute_usd
from backend.app.llm.router import (
    TASK_TIERS,
    TIER_CHAINS,
    ModelRouter,
    RouteEntry,
    build_providers,
    config_status,
)

# The OpenRouter adapter is driven over an injected httpx2 mock transport rather
# than respx, because the `openai` SDK rides httpx2 and respx cannot see it. The
# reasoning, and the live request that proved it, are in test_providers.py.
from backend.tests.llm.test_providers import openrouter_body, openrouter_stub, usage_block

PROMPT: Final = [Message(role=Role.USER, content="Write the Notdienst page.")]


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip real credentials so the suite can never make a paid call."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


class StubProvider:
    """A `Provider` that records its calls and optionally fails.

    Structural typing means this needs no base class: satisfying the protocol by
    shape is exactly what lets the router be tested without any SDK.
    """

    def __init__(
        self,
        name: str,
        *,
        error: LlmError | None = None,
        text: str = "stub answer",
    ) -> None:
        self.name = name
        self.error = error
        self.text = text
        self.calls: list[str] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        self.calls.append(model)
        if self.error is not None:
            raise self.error
        return Completion(
            text=self.text,
            tool_calls=[],
            usage=Usage(
                provider=self.name,
                model=model,
                tokens_in=100,
                tokens_out=200,
                usd=compute_usd(model, 100, 200),
                latency_ms=7,
            ),
            is_final=True,
        )


def _two_provider_router(
    first: StubProvider, second: StubProvider
) -> tuple[ModelRouter, RouteEntry, RouteEntry]:
    """A router whose STRONG tier is exactly `first` then `second`."""
    entry_a = RouteEntry(first.name, "claude-opus-5")
    entry_b = RouteEntry(second.name, "anthropic/claude-opus-4.8")
    router = ModelRouter(
        providers={first.name: first, second.name: second},
        chains={ModelTier.STRONG: (entry_a, entry_b)},
    )
    return router, entry_a, entry_b


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("task", "expected_tier"),
    [
        (TaskClass.CLASSIFY, ModelTier.CHEAP),
        (TaskClass.EXTRACT, ModelTier.CHEAP),
        (TaskClass.REPACK, ModelTier.CHEAP),
        (TaskClass.PLAN, ModelTier.MID),
        (TaskClass.PRIORITISE, ModelTier.MID),
        (TaskClass.REVIEW, ModelTier.MID),
        (TaskClass.GENERATE, ModelTier.STRONG),
        (TaskClass.EMBED, ModelTier.EMBED),
    ],
)
def test_every_task_resolves_to_its_expected_tier(
    task: TaskClass, expected_tier: ModelTier
) -> None:
    """The task -> tier table is the contract; pin all eight rows."""
    router = ModelRouter(providers={"openrouter": StubProvider("openrouter")})
    assert router.resolve(task).tier is expected_tier


def test_generate_is_the_only_task_on_the_strong_tier() -> None:
    """Strong-tier spend is 86% of a piece's cost, so exactly one task gets it.

    docs/AGENT_RUNTIME.md section 11. If a second task drifts onto STRONG, cost
    per piece moves off the <$0.15 SLO and this test is the alarm.
    """
    strong = {task for task, tier in TASK_TIERS.items() if tier is ModelTier.STRONG}
    assert strong == {TaskClass.GENERATE}


def test_every_task_class_has_a_tier() -> None:
    """A task with no tier would be a KeyError at run time, mid-run."""
    assert set(TASK_TIERS) == set(TaskClass)


def test_resolve_returns_the_configured_chain_in_order() -> None:
    """Chain order is policy: it must survive resolution unchanged."""
    router = ModelRouter(
        providers={
            "openrouter": StubProvider("openrouter"),
            "anthropic": StubProvider("anthropic"),
        }
    )
    route = router.resolve(TaskClass.GENERATE)
    assert route.chain == TIER_CHAINS[ModelTier.STRONG]
    assert route.using_fake is False


def test_resolve_drops_entries_whose_provider_is_unavailable() -> None:
    """An unreachable provider is not a fallback -- it is latency."""
    router = ModelRouter(providers={"anthropic": StubProvider("anthropic")})
    route = router.resolve(TaskClass.CLASSIFY)
    assert [entry.provider for entry in route.chain] == ["anthropic"]


# --------------------------------------------------------------------------- #
# Fallback
# --------------------------------------------------------------------------- #


async def test_rate_limit_on_the_first_provider_falls_through_to_the_second() -> None:
    first = StubProvider("alpha", error=ProviderRateLimitError("alpha", "m", "429"))
    second = StubProvider("beta", text="second answer")
    router, entry_a, entry_b = _two_provider_router(first, second)

    completion = await router.complete(TaskClass.GENERATE, PROMPT)

    assert completion.text == "second answer"
    assert completion.usage.provider == "beta"
    assert first.calls == [entry_a.model]
    assert second.calls == [entry_b.model]


@pytest.mark.parametrize(
    "error",
    [
        ProviderRateLimitError("alpha", "m", "429"),
        ProviderTimeoutError("alpha", "m", "timed out"),
        ProviderServerError("alpha", "m", "503", status_code=503),
    ],
)
async def test_every_retryable_failure_triggers_the_fallback(error: LlmError) -> None:
    """429, timeout and 5xx are the three documented fallback triggers."""
    first = StubProvider("alpha", error=error)
    second = StubProvider("beta")
    router, _, _ = _two_provider_router(first, second)

    await router.complete(TaskClass.GENERATE, PROMPT)

    assert second.calls  # the chain moved on


async def test_exhausted_chain_raises_carrying_every_cause() -> None:
    """ "Something failed" is not a report. Both causes must survive."""
    first = StubProvider("alpha", error=ProviderRateLimitError("alpha", "m", "429"))
    second = StubProvider(
        "beta", error=ProviderServerError("beta", "m", "503 overloaded", status_code=503)
    )
    router, entry_a, entry_b = _two_provider_router(first, second)

    with pytest.raises(AllProvidersFailedError) as caught:
        await router.complete(TaskClass.GENERATE, PROMPT)

    failures = caught.value.failures
    assert [failure.provider for failure in failures] == ["alpha", "beta"]
    assert [failure.model for failure in failures] == [entry_a.model, entry_b.model]
    assert isinstance(failures[0].error, ProviderRateLimitError)
    assert isinstance(failures[1].error, ProviderServerError)

    # And the summary a UI would show names both, so a human can act on it.
    message = str(caught.value)
    assert "alpha" in message
    assert "beta" in message
    assert "503 overloaded" in message
    assert caught.value.task == TaskClass.GENERATE.value


async def test_an_unavailable_model_falls_through_to_the_next_chain_entry() -> None:
    """A 403/404 is about one model, so the chain must absorb it.

    This is the failure that motivated `ModelUnavailableError`: an OpenRouter
    account whose data policy refused `anthropic/claude-opus-4.8` with a 404
    while happily serving `anthropic/claude-haiku-4.5`. The chain existed
    precisely for that, and read it as a total outage instead.
    """
    first = StubProvider(
        "alpha",
        error=ModelUnavailableError("alpha", "m", "404 no endpoints", status_code=404),
    )
    second = StubProvider("beta")
    router, _, _ = _two_provider_router(first, second)

    completion = await router.complete(TaskClass.GENERATE, PROMPT)

    assert completion.text == "stub answer"
    assert second.calls != [], "the second entry names a different model and must be tried"


async def test_an_unavailable_model_is_still_a_request_error_by_type() -> None:
    """Narrowing, not replacing: existing `except ProviderRequestError` still catches it."""
    error = ModelUnavailableError("alpha", "m", "404", status_code=404)
    assert isinstance(error, ProviderRequestError)
    assert error.status_code == 404


async def test_a_bad_credential_is_not_retried_on_the_next_provider() -> None:
    """401/402 are account-scoped: every entry fails, so fail fast and say why.

    Falling through here would bury "your key is bad" under N identical
    failures, which is the opposite of useful.
    """
    first = StubProvider(
        "alpha", error=ProviderRequestError("alpha", "m", "401 bad key", status_code=401)
    )
    second = StubProvider("beta")
    router, _, _ = _two_provider_router(first, second)

    with pytest.raises(ProviderRequestError):
        await router.complete(TaskClass.GENERATE, PROMPT)

    assert second.calls == []


async def test_a_bad_request_is_not_retried_on_the_next_provider() -> None:
    """A malformed request fails identically downstream; falling back is waste."""
    first = StubProvider("alpha", error=ProviderRequestError("alpha", "m", "400 bad tool schema"))
    second = StubProvider("beta")
    router, _, _ = _two_provider_router(first, second)

    with pytest.raises(ProviderRequestError):
        await router.complete(TaskClass.GENERATE, PROMPT)

    assert second.calls == []


# --------------------------------------------------------------------------- #
# The budget guard -- before the call, not after
# --------------------------------------------------------------------------- #


async def test_budget_guard_refuses_before_any_http_request() -> None:
    """The load-bearing assertion: no HTTP request was ever attempted.

    Raising after the tokens are spent is accounting. This proves control: a
    *real* adapter is wired up over a mock transport that records every attempt,
    and the guard stops the call before the transport is ever reached.
    """
    stub = openrouter_stub(body={"should": "never be reached"})
    router = ModelRouter(
        providers={"openrouter": stub.provider},
        chains={ModelTier.STRONG: (RouteEntry("openrouter", "openai/gpt-4.1"),)},
    )
    budget = BudgetState(limit_usd=Decimal("0.000001"))

    with pytest.raises(BudgetExceededError) as caught:
        await router.complete(TaskClass.GENERATE, PROMPT, budget=budget)

    assert stub.requests == []  # nothing was sent, not even an attempt
    assert caught.value.model == "openai/gpt-4.1"
    assert caught.value.estimated_usd > budget.remaining_usd
    assert budget.spent_usd == Decimal(0)


async def test_the_same_call_goes_through_when_the_budget_allows_it() -> None:
    """Positive control: without it, the assertion above could pass vacuously."""
    stub = openrouter_stub(body=openrouter_body(content="ok", usage=usage_block(10, 20)))
    router = ModelRouter(
        providers={"openrouter": stub.provider},
        chains={ModelTier.STRONG: (RouteEntry("openrouter", "openai/gpt-4.1"),)},
    )
    budget = BudgetState(limit_usd=Decimal("0.50"))

    completion = await router.complete(TaskClass.GENERATE, PROMPT, budget=budget)

    assert len(stub.requests) == 1
    assert completion.text == "ok"
    assert budget.spent_usd == compute_usd("openai/gpt-4.1", 10, 20)


async def test_budget_records_real_cost_not_the_estimate() -> None:
    """The ledger books what was spent; the estimate only ever gates."""
    provider = StubProvider("alpha")
    router = ModelRouter(
        providers={"alpha": provider},
        chains={ModelTier.STRONG: (RouteEntry("alpha", "claude-opus-5"),)},
    )
    budget = BudgetState(limit_usd=Decimal("0.50"))

    await router.complete(TaskClass.GENERATE, PROMPT, budget=budget)

    assert budget.spent_usd == compute_usd("claude-opus-5", 100, 200)


async def test_the_guard_re_checks_before_each_entry_in_the_chain() -> None:
    """Falling back must not smuggle a call past the cap.

    The first provider rate-limits, and the fallback is priced high enough that
    the remaining budget cannot cover it -- so the chain stops with a budget
    error rather than spending money the run did not have.
    """
    first = StubProvider("alpha", error=ProviderRateLimitError("alpha", "m", "429"))
    second = StubProvider("beta")
    router = ModelRouter(
        providers={"alpha": first, "beta": second},
        chains={
            ModelTier.STRONG: (
                RouteEntry("alpha", "fake/strong"),  # priced at zero, so affordable
                RouteEntry("beta", "claude-opus-5"),  # priced, so unaffordable
            )
        },
    )
    budget = BudgetState(limit_usd=Decimal("0.000001"))

    with pytest.raises(BudgetExceededError) as caught:
        await router.complete(TaskClass.GENERATE, PROMPT, budget=budget)

    assert first.calls == ["fake/strong"]
    assert second.calls == []
    assert caught.value.model == "claude-opus-5"


async def test_no_budget_means_no_guard() -> None:
    """A caller that passes no budget is opting out; do not invent a ceiling."""
    provider = StubProvider("alpha")
    router = ModelRouter(
        providers={"alpha": provider},
        chains={ModelTier.STRONG: (RouteEntry("alpha", "claude-opus-5"),)},
    )
    completion = await router.complete(TaskClass.GENERATE, PROMPT)
    assert completion.is_final is True


def test_estimate_is_pessimistic_about_tools_and_output() -> None:
    """Tool schemas are prompt tokens too, and the model may fill max_tokens."""
    router = ModelRouter(providers={"alpha": StubProvider("alpha")})
    tool = ToolSpec(
        name="kb_search",
        description="Search the customer's indexed documents.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )

    bare = router.estimate_usd("claude-opus-5", PROMPT, None, 100)
    with_tool = router.estimate_usd("claude-opus-5", PROMPT, [tool], 100)
    assert with_tool > bare

    assert router.estimate_usd("claude-opus-5", PROMPT, None, 4_000) > router.estimate_usd(
        "claude-opus-5", PROMPT, None, 100
    )


# --------------------------------------------------------------------------- #
# No credentials -> the fake provider, loudly
# --------------------------------------------------------------------------- #


def test_config_status_reports_fake_mode_when_no_keys_are_set() -> None:
    """A UI must be able to say "running on the fake provider"."""
    status = config_status(env={})

    assert status.using_fake_provider is True
    assert status.openrouter_configured is False
    assert status.anthropic_configured is False
    assert status.available_providers == ("fake",)
    assert "FakeProvider" in status.message
    assert "OPENROUTER_API_KEY" in status.message


def test_config_status_reports_configured_providers() -> None:
    status = config_status(env={"OPENROUTER_API_KEY": "sk-or-x", "ANTHROPIC_API_KEY": "sk-ant-x"})

    assert status.using_fake_provider is False
    assert status.openrouter_configured is True
    assert status.anthropic_configured is True
    assert status.available_providers == ("openrouter", "anthropic", "fake")


def test_a_blank_key_counts_as_absent() -> None:
    """`OPENROUTER_API_KEY=` in a .env file is how people unset things.

    Treating it as present would send an unauthenticated request and produce a
    401 instead of quietly working on the fake provider.
    """
    status = config_status(env={"OPENROUTER_API_KEY": "   "})
    assert status.openrouter_configured is False
    assert status.using_fake_provider is True


def test_build_providers_with_no_keys_yields_only_the_fake() -> None:
    """No key must mean no paid call -- and no crash either."""
    providers = build_providers(env={})
    assert set(providers) == {"fake"}
    assert isinstance(providers["fake"], FakeProvider)


def test_build_providers_constructs_an_adapter_per_key() -> None:
    providers = build_providers(env={"OPENROUTER_API_KEY": "sk-or-x"})
    assert set(providers) == {"fake", "openrouter"}
    assert providers["openrouter"].name == "openrouter"


@pytest.mark.parametrize("task", list(TaskClass))
async def test_with_no_credentials_every_task_is_served_by_the_fake(
    task: TaskClass,
) -> None:
    """Degrade to the fake, never to a crash and never to a silent paid call."""
    router = ModelRouter(env={})
    route = router.resolve(task)

    assert route.using_fake is True
    assert [entry.provider for entry in route.chain] == ["fake"]

    completion = await router.complete(task, PROMPT)
    assert completion.usage.provider == "fake"
    assert completion.usage.usd == Decimal("0")


def test_the_fake_cannot_be_configured_away() -> None:
    """It is the floor the router degrades to, so it must always be reachable."""
    router = ModelRouter(providers={})
    assert "fake" in router.providers


def test_a_provider_protocol_stub_satisfies_the_protocol() -> None:
    """Structural typing check, so the stubs above stay honest."""
    provider: Provider = StubProvider("alpha")
    assert provider.name == "alpha"


async def test_every_tier_chain_spans_more_than_one_vendor() -> None:
    """The chain comment's own rule, enforced instead of merely stated.

    STRONG shipped with two entries that were both Anthropic -- first party and
    via OpenRouter -- so a single vendor-wide refusal took out the whole tier.
    That happened for real on 2026-08-19: an OpenRouter data policy that refused
    every full-size model left `--tier strong` with nothing to call. EMBED is
    exempt: one embedding vendor is a deliberate choice, because mixing vector
    spaces across providers would silently corrupt every stored embedding.
    """
    for tier, chain in TIER_CHAINS.items():
        if tier is ModelTier.EMBED:
            continue
        vendors = {_vendor_of(entry.model) for entry in chain}
        assert len(vendors) > 1, (
            f"{tier.value} chain reaches only {vendors}: a second route to one "
            "vendor is not a fallback, because the failures worth falling back "
            "over tend to be vendor-wide"
        )


def _vendor_of(model: str) -> str:
    """The vendor behind a model id, however it is routed.

    `anthropic/claude-opus-4.8` (OpenRouter) and `claude-opus-5` (first party)
    are the same vendor by two roads, which is exactly the case this has to see
    through -- so a bare `claude-*` id counts as Anthropic.
    """
    if "/" in model:
        return model.split("/", 1)[0]
    return "anthropic" if model.startswith("claude") else model
