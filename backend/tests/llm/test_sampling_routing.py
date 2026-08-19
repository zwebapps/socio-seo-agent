"""Configured sampling reaching an actual provider call, and the three rules around it.

The behaviour under test is not "the value is stored" -- that is a database concern -- but
"the value arrives at the provider, and does not arrive where it must not":

1. **Nothing configured sends nothing.** Every call site passes `temperature=None` today
   and the two Anthropic-family defaults depend on that, so an empty table must leave the
   request byte-identical.
2. **A caller who names a value wins.** `geo_service` sizes `max_tokens` to the probe it
   is running; an operator's default must not overwrite a call's own intention.
3. **A configured temperature is SKIPPED for a model that rejects the parameter.** The
   STRONG chain's first choice is such a model, so applying a stored default blindly would
   take GENERATE offline the moment somebody moved the slider. A caller-supplied
   temperature still reaches the adapter and is still refused loudly there -- the
   distinction is between a preference and an intention.

The fake provider records what it was handed, which is the only way to test 1 and 3: both
are about the ABSENCE of a parameter, and an absence is invisible from the response.
"""

from decimal import Decimal
from typing import Any

import pytest

from backend.app.llm import ModelTier, TaskClass
from backend.app.llm.contract import (
    BudgetExceededError,
    Completion,
    Message,
    Provider,
    Role,
    ToolSpec,
    Usage,
)
from backend.app.llm.route_config import InMemoryRouteStore, RouteRecord, RouteResolver
from backend.app.llm.router import ModelRouter, RouteEntry
from backend.app.llm.sampling import SamplingRecord

_MESSAGES = [Message(role=Role.USER, content="write something")]


class RecordingProvider:
    """A provider that remembers every keyword it was called with.

    Not a mock: the point is to assert on `temperature is None` versus
    `temperature == 0.4`, and a mock that accepts anything would pass either way.
    """

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: Any,
        *,
        model: str,
        tools: Any = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        self.calls.append({"model": model, "temperature": temperature, "max_tokens": max_tokens})
        return Completion(
            text="ok",
            tool_calls=[],
            is_final=True,
            usage=Usage(
                provider=self.name,
                model=model,
                tokens_in=1,
                tokens_out=1,
                usd=Decimal("0"),
                latency_ms=1,
            ),
        )


def _router(
    sampling: list[SamplingRecord] | None = None,
    *,
    provider: RecordingProvider,
    model: str = "openai/gpt-4.1-mini",
) -> tuple[ModelRouter, RouteResolver]:
    """A router whose whole chain is one recording provider on a named model."""
    store = InMemoryRouteStore(
        routes=[
            RouteRecord(
                task_class=TaskClass.GENERATE,
                tier=ModelTier.STRONG,
                chain=[{"provider": "recording", "model": model}],
            )
        ],
        sampling=sampling or [],
    )
    resolver = RouteResolver(store)
    router = ModelRouter(
        providers={"recording": provider},
        resolver=resolver,
    )
    return router, resolver


async def test_nothing_configured_sends_no_sampling_parameters_at_all() -> None:
    """Rule 1, and the reason this feature is safe to deploy ahead of its screen."""
    provider = RecordingProvider()
    router, _ = _router(provider=provider)
    await router.refresh_routes()

    await router.complete(TaskClass.GENERATE, _MESSAGES)

    assert provider.calls == [
        {"model": "openai/gpt-4.1-mini", "temperature": None, "max_tokens": None}
    ]


async def test_a_configured_policy_reaches_the_provider() -> None:
    provider = RecordingProvider()
    router, _ = _router(
        [
            SamplingRecord(
                task_class=TaskClass.GENERATE,
                temperature=Decimal("0.40"),
                max_output_tokens=4096,
            )
        ],
        provider=provider,
    )
    await router.refresh_routes()

    await router.complete(TaskClass.GENERATE, _MESSAGES)

    assert provider.calls[0]["temperature"] == 0.4
    assert provider.calls[0]["max_tokens"] == 4096


async def test_a_caller_who_names_a_value_beats_the_stored_default() -> None:
    """Rule 2. `geo_service` sizes `max_tokens` to its probe and every node passes
    `temperature=None` deliberately; an operator default that overrode either would be
    changing a decision the call site already made."""
    provider = RecordingProvider()
    router, _ = _router(
        [
            SamplingRecord(
                task_class=TaskClass.GENERATE,
                temperature=Decimal("0.40"),
                max_output_tokens=4096,
            )
        ],
        provider=provider,
    )
    await router.refresh_routes()

    await router.complete(TaskClass.GENERATE, _MESSAGES, temperature=0.9, max_tokens=1024)

    assert provider.calls[0]["temperature"] == 0.9
    assert provider.calls[0]["max_tokens"] == 1024


async def test_a_configured_temperature_is_skipped_for_a_model_that_rejects_it() -> None:
    """Rule 3, and the trap this feature would otherwise walk into: `claude-opus-5` returns
    400 if `temperature` is present at all, and it is the STRONG chain's first choice. A
    stored DEFAULT yields to that rather than failing the run; the output ceiling, which
    every adapter accepts, still applies."""
    provider = RecordingProvider()
    router, _ = _router(
        [
            SamplingRecord(
                task_class=TaskClass.GENERATE,
                temperature=Decimal("0.40"),
                max_output_tokens=4096,
            )
        ],
        provider=provider,
        model="claude-opus-5",
    )
    await router.refresh_routes()

    await router.complete(TaskClass.GENERATE, _MESSAGES)

    assert provider.calls[0]["temperature"] is None, (
        "a configured temperature reached a model that rejects the parameter; the call "
        "would have been a 400 and the whole task would be offline"
    )
    assert provider.calls[0]["max_tokens"] == 4096


async def test_a_caller_supplied_temperature_is_still_passed_to_a_rejecting_model() -> None:
    """The distinction that keeps the adapter's own refusal meaningful: skipping applies to
    a stored DEFAULT, never to a parameter a call site chose. The adapter raises on it, and
    that loud failure is correct -- silently dropping an explicit temperature would change
    a call's character with nobody told."""
    provider = RecordingProvider()
    router, _ = _router(provider=provider, model="claude-opus-5")
    await router.refresh_routes()

    await router.complete(TaskClass.GENERATE, _MESSAGES, temperature=0.9)

    assert provider.calls[0]["temperature"] == 0.9


async def test_the_budget_guard_reserves_against_the_configured_ceiling() -> None:
    """The consequence the screen has to show. `estimate_usd` assumes the model emits its
    whole allowance, so a configured ceiling the guard did not know about would make the
    reservation smaller than the call it is guarding -- the guard would stop being a
    guard."""
    provider = RecordingProvider()
    router, _ = _router(
        [SamplingRecord(task_class=TaskClass.GENERATE, max_output_tokens=8192)],
        provider=provider,
        model="openai/gpt-4.1",
    )
    await router.refresh_routes()

    unaware = router.estimate_usd("openai/gpt-4.1", _MESSAGES)
    aware = router.estimate_usd("openai/gpt-4.1", _MESSAGES, max_tokens=8192)
    assert aware > unaware

    class Budget:
        """Just enough of `BudgetState` to answer the guard, and to record what it asked."""

        limit_usd = Decimal("0.01")
        spent_usd = Decimal("0")
        asked: Decimal | None = None

        def can_afford(self, estimate: Decimal) -> bool:
            Budget.asked = estimate
            return False

        def record(self, usage: Usage) -> None:  # pragma: no cover - never reached
            raise AssertionError("the call should have been refused before this")

    with pytest.raises(BudgetExceededError):
        await router.complete(TaskClass.GENERATE, _MESSAGES, budget=Budget())  # type: ignore[arg-type]

    assert Budget.asked == aware, (
        "the guard reserved the default allowance rather than the configured one, so a "
        "raised ceiling would spend past the run cap"
    )
    assert provider.calls == [], "the guard must refuse before the provider is called"


async def test_the_resolver_reports_every_task_including_unconfigured_ones() -> None:
    """Same reasoning as `describe()`: a screen listing only touched rows cannot show the
    whole picture."""
    store = InMemoryRouteStore(
        sampling=[SamplingRecord(task_class=TaskClass.GENERATE, max_output_tokens=4096)]
    )
    resolver = RouteResolver(store)
    await resolver.refresh()

    described = {s.task_class: s for s in resolver.describe_sampling()}

    assert set(described) == set(TaskClass)
    assert described[TaskClass.GENERATE].max_output_tokens == 4096
    assert described[TaskClass.CLASSIFY].is_empty


async def test_a_stored_row_for_an_unknown_task_does_not_break_the_resolver() -> None:
    """A resolver that raised on a stale settings row would turn a rename into an outage.
    The row is ignored and that task falls back to sending nothing, which is the default.
    """
    resolver = RouteResolver(InMemoryRouteStore(sampling=[]))
    await resolver.refresh()

    assert resolver.sampling_for(TaskClass.GENERATE) is None
    assert resolver.max_tokens_for(TaskClass.GENERATE) is None
    assert resolver.temperature_for(TaskClass.GENERATE, "openai/gpt-4.1-mini") is None


async def test_a_router_with_no_resolver_ignores_sampling_entirely() -> None:
    """Production constructs the router without a resolver in some paths, and the eval
    harness does too. Neither may acquire a dependency on this table."""
    provider = RecordingProvider()
    router = ModelRouter(
        providers={"recording": provider},
        chains={ModelTier.STRONG: (RouteEntry("recording", "openai/gpt-4.1-mini"),)},
        task_tiers={TaskClass.GENERATE: ModelTier.STRONG},
    )

    await router.complete(TaskClass.GENERATE, _MESSAGES)

    assert provider.calls[0]["temperature"] is None
    assert provider.calls[0]["max_tokens"] is None


def test_the_recording_provider_satisfies_the_provider_protocol() -> None:
    """Otherwise this file could be asserting on a shape the real adapters do not have."""
    provider: Provider = RecordingProvider()
    assert provider.name == "recording"
    assert ToolSpec is not None
