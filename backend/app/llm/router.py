"""The model router: task -> tier -> ordered fallback chain, with a budget guard.

This is the only module that knows a model id. A caller names a `TaskClass`;
everything downstream is data in the two tables below, so swapping a model is a
one-line change and never a code change (docs/ARCHITECTURE.md section 8).

Two decisions worth stating outright, because both are easy to get wrong in a
way that still looks like it works:

* **The budget guard runs before the provider call.** Checking spend after the
  tokens are gone is accounting, not control (docs/AGENT_RUNTIME.md section 8:
  "Caps are checked before the call"). The estimate is deliberately pessimistic
  -- full `max_tokens` of output, characters over three -- so the guard errs
  towards refusing a call it could have afforded.
* **Missing credentials mean the fake provider, never a paid call and never a
  crash.** A provider with no key is simply not in the chain. With no keys at
  all the router serves `FakeProvider`, and `config_status()` says so out loud
  so a UI can report "running on the fake provider" instead of leaving someone
  to wonder why the copy reads like a placeholder.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pydantic import BaseModel

from backend.app.llm.contract import (
    RETRYABLE_ERRORS,
    AllProvidersFailedError,
    BudgetExceededError,
    BudgetState,
    Completion,
    Message,
    ModelTier,
    Provider,
    ProviderFailure,
    ProviderUnavailableError,
    TaskClass,
    ToolSpec,
    Usage,
)
from backend.app.llm.fake_provider import FakeProvider
from backend.app.llm.pricing import compute_usd, conservative_token_estimate
from backend.app.obs import Tracer, get_tracer, llm_span_fields

OPENROUTER_KEY_ENV: Final = "OPENROUTER_API_KEY"
ANTHROPIC_KEY_ENV: Final = "ANTHROPIC_API_KEY"

OPENROUTER: Final = "openrouter"
ANTHROPIC: Final = "anthropic"
FAKE: Final = "fake"

#: Output-token assumption for the pre-call estimate when the caller sets no
#: ceiling. Matches the adapters' own default so the estimate and the request
#: cannot disagree.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 2048

#: Receives every completed call's usage, plus whatever trace context the caller
#: passed. Synchronous by design: the router is on the hot path of every node, and
#: awaiting a database write here would put request latency behind a ledger insert.
UsageSink = Callable[[Usage, Mapping[str, str]], None]


@dataclass(frozen=True, slots=True)
class RouteEntry:
    """One (provider, model) pair in a fallback chain."""

    provider: str
    model: str


# --------------------------------------------------------------------------- #
# The routing tables. This is the config; everything else is mechanism.
# --------------------------------------------------------------------------- #

#: Task -> tier. Mirrors the table in docs/ARCHITECTURE.md section 8.
#:
#: `REVIEW` sits on MID, not STRONG. Section 8 lists an optional final-review
#: pass as strong-tier, but the deterministic critic in VALIDATE does the actual
#: quality gating (docs/AGENT_RUNTIME.md section 7), and section 11's costed run
#: shows GENERATE is 86% of a piece's cost -- which is exactly why it is the one
#: task worth strong-tier money. Promoting REVIEW is a one-line edit here.
TASK_TIERS: Final[Mapping[TaskClass, ModelTier]] = {
    TaskClass.CLASSIFY: ModelTier.CHEAP,
    TaskClass.EXTRACT: ModelTier.CHEAP,
    TaskClass.REPACK: ModelTier.CHEAP,
    TaskClass.PLAN: ModelTier.MID,
    TaskClass.PRIORITISE: ModelTier.MID,
    TaskClass.REVIEW: ModelTier.MID,
    TaskClass.GENERATE: ModelTier.STRONG,
    TaskClass.EMBED: ModelTier.EMBED,
}

#: Tier -> ordered fallback chain. Order is the policy: cheapest-adequate first,
#: then a different *vendor* rather than a different model at the same vendor,
#: because the failures worth falling back over (429, 5xx) tend to be
#: vendor-wide.
TIER_CHAINS: Final[Mapping[ModelTier, tuple[RouteEntry, ...]]] = {
    ModelTier.CHEAP: (
        RouteEntry(OPENROUTER, "openai/gpt-4.1-mini"),
        RouteEntry(ANTHROPIC, "claude-haiku-4-5"),
        RouteEntry(OPENROUTER, "google/gemini-2.5-flash"),
    ),
    ModelTier.MID: (
        RouteEntry(OPENROUTER, "anthropic/claude-sonnet-4.5"),
        RouteEntry(ANTHROPIC, "claude-sonnet-5"),
        RouteEntry(OPENROUTER, "openai/gpt-4.1"),
    ),
    # Both of the original entries were Anthropic -- first party and via
    # OpenRouter -- which broke the vendor-diversity rule stated directly above,
    # and it was not theoretical: on 2026-08-19 an OpenRouter account whose data
    # policy refused every full-size model took out the ENTIRE strong tier, and
    # `--tier strong` could not run at all. Two Anthropic routes are two ways to
    # reach one vendor, not a fallback. A non-Anthropic third entry is what makes
    # this a chain; unreachable entries cost nothing, because a provider with no
    # credential is filtered out before the attempt and a 403/404 falls through.
    ModelTier.STRONG: (
        RouteEntry(ANTHROPIC, "claude-opus-5"),
        RouteEntry(OPENROUTER, "anthropic/claude-opus-4.8"),
        RouteEntry(OPENROUTER, "openai/gpt-5.1"),
    ),
    ModelTier.EMBED: (RouteEntry(OPENROUTER, "openai/text-embedding-3-small"),),
}

#: The chain used when no real provider is configured. Its models are priced at
#: zero in the table, which is the truth: nothing leaves the process.
FAKE_CHAINS: Final[Mapping[ModelTier, tuple[RouteEntry, ...]]] = {
    tier: (RouteEntry(FAKE, f"fake/{tier.value}"),) for tier in ModelTier
}

_UNMAPPED_TASKS = sorted(task.value for task in TaskClass if task not in TASK_TIERS)
if _UNMAPPED_TASKS:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "TaskClass values with no tier in TASK_TIERS: "
        f"{_UNMAPPED_TASKS}. Every task must route somewhere, or a caller gets "
        "a KeyError at run time instead of a failure at import."
    )


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """The chain a task will actually be attempted against."""

    task: TaskClass
    tier: ModelTier
    chain: tuple[RouteEntry, ...]
    using_fake: bool


# --------------------------------------------------------------------------- #
# Credentials and configuration reporting
# --------------------------------------------------------------------------- #


class ConfigStatus(BaseModel):
    """Which providers are actually reachable, and whether we are faking it.

    Surfaced through the API so a UI can say "running on the fake provider"
    rather than leaving a user to guess why output looks canned.
    """

    openrouter_configured: bool
    anthropic_configured: bool
    available_providers: tuple[str, ...]
    using_fake_provider: bool
    message: str


def _read_key(env: Mapping[str, str], name: str) -> str | None:
    """Return a usable key, treating blank and whitespace-only as absent.

    An empty `OPENROUTER_API_KEY=` in a `.env` file is a very common way to
    "unset" a variable; treating it as present would send an unauthenticated
    request and produce a 401 instead of the fake provider.
    """
    value = env.get(name, "").strip()
    return value or None


def config_status(env: Mapping[str, str] | None = None) -> ConfigStatus:
    """Report provider availability without constructing any client."""
    environ = env if env is not None else os.environ
    openrouter = _read_key(environ, OPENROUTER_KEY_ENV) is not None
    anthropic = _read_key(environ, ANTHROPIC_KEY_ENV) is not None

    available = [name for name, ok in ((OPENROUTER, openrouter), (ANTHROPIC, anthropic)) if ok]
    using_fake = not available

    if using_fake:
        message = (
            "No model provider is configured, so every model call is served by "
            f"FakeProvider (deterministic, no network). Set {OPENROUTER_KEY_ENV} "
            f"or {ANTHROPIC_KEY_ENV} to use a real model."
        )
    else:
        message = "Model providers configured: " + ", ".join(available) + "."

    return ConfigStatus(
        openrouter_configured=openrouter,
        anthropic_configured=anthropic,
        available_providers=(*available, FAKE),
        using_fake_provider=using_fake,
        message=message,
    )


def build_providers(env: Mapping[str, str] | None = None) -> dict[str, Provider]:
    """Construct one adapter per configured credential, plus the fake.

    Imports are local so that a process with no keys never imports a vendor SDK
    at all -- which keeps the no-credentials path both fast and impossible to
    break with an SDK-level import error.
    """
    environ = env if env is not None else os.environ
    providers: dict[str, Provider] = {FAKE: FakeProvider()}

    openrouter_key = _read_key(environ, OPENROUTER_KEY_ENV)
    if openrouter_key is not None:
        from backend.app.llm.openrouter_provider import OpenRouterProvider

        providers[OPENROUTER] = OpenRouterProvider(openrouter_key)

    anthropic_key = _read_key(environ, ANTHROPIC_KEY_ENV)
    if anthropic_key is not None:
        from backend.app.llm.anthropic_provider import AnthropicProvider

        providers[ANTHROPIC] = AnthropicProvider(anthropic_key)

    return providers


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #


class ModelRouter:
    """Resolves a task to a chain and runs it, guarding the budget first."""

    def __init__(
        self,
        *,
        providers: Mapping[str, Provider] | None = None,
        env: Mapping[str, str] | None = None,
        chains: Mapping[ModelTier, tuple[RouteEntry, ...]] | None = None,
        task_tiers: Mapping[TaskClass, ModelTier] | None = None,
        default_max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        resolver: object | None = None,
        tracer: Tracer | None = None,
        usage_sink: UsageSink | None = None,
    ) -> None:
        """Build a router.

        `providers` / `chains` / `task_tiers` exist so a test can inject stub
        providers and a two-entry chain without monkeypatching module state.
        Production passes none of them.

        `resolver` is an optional RouteResolver carrying admin-configured routing. It is
        typed loosely to keep this module free of an import cycle -- route_config
        imports TIER_CHAINS and TASK_TIERS from here. When absent, or when it has
        nothing to say about a task, the code defaults below apply unchanged: that is
        what lets the configuration feature ship without changing any behaviour until
        someone configures something.
        """
        resolved = dict(providers) if providers is not None else build_providers(env)
        # The fake is always reachable: it is the floor the router degrades to,
        # so it must never be possible to configure it away.
        resolved.setdefault(FAKE, FakeProvider())

        self._providers: dict[str, Provider] = resolved
        self._chains = chains if chains is not None else TIER_CHAINS
        self._task_tiers = task_tiers if task_tiers is not None else TASK_TIERS
        self._default_max_tokens = default_max_tokens
        self._resolver = resolver
        # No credentials means a genuinely free no-op tracer, same posture as every
        # other provider here. Redaction lives inside the tracer, so no call site can
        # leak prompt text by forgetting.
        self._tracer = tracer if tracer is not None else get_tracer(env)
        # Where each call's usage goes to be PERSISTED, as opposed to traced.
        #
        # A callback rather than a store, because this module must not reach the
        # database: `model_usage` is business-scoped and this router is a process-wide
        # object with no tenant, so it cannot write the row correctly even if it wanted
        # to. The caller that knows the run supplies the sink -- see
        # `services/run_executor`.
        self._usage_sink = usage_sink

    @property
    def providers(self) -> Mapping[str, Provider]:
        """The providers this router can reach, by name."""
        return self._providers

    async def refresh_routes(self) -> None:
        """Reload admin configuration, if this router has a resolver.

        Called at startup and after an admin edit. A no-op without a resolver, so
        callers need not know whether one is configured.
        """
        resolver = self._resolver
        if resolver is not None:
            await resolver.refresh()  # type: ignore[attr-defined]

    def _usable(self, provider: str) -> bool:
        """Whether this provider can serve a call right now.

        Three separate questions, which is why this is not just a dict lookup:
        the adapter exists (a credential was present at construction), an admin has not
        switched it off, and -- for a KEYLESS provider like Ollama -- configuration has
        given it an address. The environment cannot tell us anything about a local
        server, so only the configuration can.
        """
        resolver = self._resolver
        if resolver is not None and resolver.is_disabled(provider):  # type: ignore[attr-defined]
            return False
        if provider in self._providers:
            return True
        if resolver is not None:
            return provider in resolver.keyless_available()  # type: ignore[attr-defined]
        return False

    def resolve(self, task: TaskClass) -> ResolvedRoute:
        """Map a task to its tier and its *available* fallback chain.

        Entries whose provider cannot serve a call are filtered out rather than
        attempted and failed: an unreachable provider is not a fallback, it is latency.
        If nothing is left, the fake chain is used -- the floor, never an exception.
        """
        resolver = self._resolver
        if resolver is not None:
            tier = resolver.tier_for_task(task)  # type: ignore[attr-defined]
            candidates = resolver.chain_for_task(task)  # type: ignore[attr-defined]
        else:
            tier = self._task_tiers[task]
            candidates = self._chains.get(tier, ())

        chain = tuple(entry for entry in candidates if self._usable(entry.provider))
        if chain:
            return ResolvedRoute(task=task, tier=tier, chain=chain, using_fake=False)

        fake_chain = FAKE_CHAINS[tier]
        return ResolvedRoute(task=task, tier=tier, chain=fake_chain, using_fake=True)

    def _provider_for(self, name: str) -> Provider:
        """The adapter for one provider name, constructing a keyless one on demand.

        Ollama cannot be built at construction time the way a hosted provider is: there
        is no credential to detect, and its address comes from the admin configuration
        the resolver holds. So it is built here, lazily, and cached.

        The import is local for the same reason `build_providers`' are: a deployment with
        no local server and no API key must never load a vendor SDK just by importing
        this module.
        """
        existing = self._providers.get(name)
        if existing is not None:
            return existing

        resolver = self._resolver
        if name == "ollama" and resolver is not None:
            base_url = resolver.base_url(name)  # type: ignore[attr-defined]
            if base_url:
                from backend.app.llm.ollama_provider import OllamaProvider

                built: Provider = OllamaProvider(base_url=base_url)
                self._providers[name] = built
                return built

        # Should be unreachable: resolve() filters on _usable() first. A named error
        # beats a KeyError if the two ever disagree -- and it says which provider,
        # which a KeyError on a dict lookup does not make obvious in a traceback.
        raise ProviderUnavailableError(
            f"no adapter available for provider {name!r}: it is neither credentialled "
            "nor configured with an address"
        )

    def _configured_temperature(self, task: TaskClass, model: str) -> float | None:
        """The operator's temperature for this task and model, if any.

        Typed loosely against the resolver for the same reason `resolve` is: importing
        `RouteResolver` here would close an import cycle, because `route_config` imports
        the tables above.
        """
        resolver = self._resolver
        if resolver is None:
            return None
        value = resolver.temperature_for(task, model)  # type: ignore[attr-defined]
        return float(value) if value is not None else None

    def _configured_max_tokens(self, task: TaskClass) -> int | None:
        """The operator's output ceiling for this task, if any."""
        resolver = self._resolver
        if resolver is None:
            return None
        value = resolver.max_tokens_for(task)  # type: ignore[attr-defined]
        return int(value) if value is not None else None

    def estimate_usd(
        self,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> Decimal:
        """A deliberately pessimistic pre-call cost estimate.

        Counts the prompt at three characters per token (English prose runs
        nearer four), the full tool schemas, any replayed tool arguments, and
        assumes the model emits its entire output allowance. Over-estimating is
        the safe direction for a guard whose job is to refuse.
        """
        tokens_in = 0
        for message in messages:
            tokens_in += conservative_token_estimate(message.content)
            for call in message.tool_calls:
                tokens_in += conservative_token_estimate(json.dumps(call.arguments, sort_keys=True))
        for tool in tools or ():
            rendered = f"{tool.name}{tool.description}{json.dumps(tool.parameters, sort_keys=True)}"
            tokens_in += conservative_token_estimate(rendered)

        tokens_out = max_tokens if max_tokens is not None else self._default_max_tokens
        return compute_usd(model, tokens_in, tokens_out)

    async def complete(
        self,
        task: TaskClass,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        budget: BudgetState | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        trace: Mapping[str, str] | None = None,
    ) -> Completion:
        """Run `task` against its chain, falling back on retryable failures.

        Raises `BudgetExceededError` **before any network call** when the
        remaining budget cannot cover the estimate; `AllProvidersFailedError`
        carrying every cause when the chain is exhausted. A non-retryable
        failure (bad request, unparseable tool arguments) is raised straight
        through, because the next provider would reject it identically.

        `temperature` and `max_tokens` left as None fall back to the operator's stored
        sampling policy for this task, if a resolver carries one; a caller who names
        either wins. With nothing stored, both stay None and nothing is sent.
        """
        route = self.resolve(task)
        failures: list[ProviderFailure] = []

        # Configured sampling applies only where the CALLER named nothing. A call site
        # that passes `temperature=` or `max_tokens=` has an intention about this one
        # call, and an operator's stored default must not override it -- `geo_service`
        # sizes `max_tokens` to the probe it is running, and every node passes
        # `temperature=None` precisely so the provider default applies. With no
        # resolver, or nothing stored, both stay None and behaviour is unchanged.
        if max_tokens is not None:
            effective_max_tokens: int | None = max_tokens
        else:
            effective_max_tokens = self._configured_max_tokens(task)

        for entry in route.chain:
            provider = self._provider_for(entry.provider)

            # Resolved per ENTRY, not once: whether a temperature may be sent depends on
            # the model, and a chain can fall back from a model that refuses one to a
            # model that accepts it.
            effective_temperature = (
                temperature
                if temperature is not None
                else self._configured_temperature(task, entry.model)
            )

            # -- the guard, and it is here on purpose: before the await ------ #
            if budget is not None:
                # The EFFECTIVE ceiling, not the caller's. The estimate assumes the model
                # emits its whole allowance, so a configured ceiling the guard did not
                # know about would make the reservation smaller than the call it guards.
                estimate = self.estimate_usd(entry.model, messages, tools, effective_max_tokens)
                if not budget.can_afford(estimate):
                    raise BudgetExceededError(
                        model=entry.model,
                        limit_usd=budget.limit_usd,
                        spent_usd=budget.spent_usd,
                        estimated_usd=estimate,
                    )

            try:
                completion = await provider.complete(
                    messages,
                    model=entry.model,
                    tools=tools,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                )
            except RETRYABLE_ERRORS as exc:
                failures.append(
                    ProviderFailure(provider=entry.provider, model=entry.model, error=exc)
                )
                continue

            if budget is not None:
                budget.record(completion.usage)

            # Traced AFTER the cost is booked, so the span reports what was actually
            # charged rather than an estimate. `attempt` matters: a call that succeeded
            # on the second entry of the chain looks identical to a first-try success
            # without it, and "why is this slow" is usually a silent fallback.
            context = trace or {}
            with self._tracer.span(
                "llm.complete",
                **llm_span_fields(
                    run_id=context.get("run_id", ""),
                    business_id=context.get("business_id", ""),
                    node=context.get("node", task.value),
                    prompt_version=context.get("prompt_version", ""),
                    usage=completion.usage,
                    outcome="ok",
                    task=task.value,
                    tier=route.tier.value,
                    attempt=len(failures) + 1,
                ),
            ):
                pass

            if self._usage_sink is not None:
                # After the budget and the span, and deliberately not before: the sink
                # persists what was actually charged. A failure here must not lose a
                # completion that has already been paid for, so the sink owns its own
                # error handling and this call site does not guard it -- see the sink in
                # `run_executor`, which logs and drops.
                self._usage_sink(completion.usage, context)

            return completion

        raise AllProvidersFailedError(task.value, failures)
