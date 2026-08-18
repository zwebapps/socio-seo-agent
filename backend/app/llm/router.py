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
from collections.abc import Mapping, Sequence
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
    TaskClass,
    ToolSpec,
)
from backend.app.llm.fake_provider import FakeProvider
from backend.app.llm.pricing import compute_usd, conservative_token_estimate

OPENROUTER_KEY_ENV: Final = "OPENROUTER_API_KEY"
ANTHROPIC_KEY_ENV: Final = "ANTHROPIC_API_KEY"

OPENROUTER: Final = "openrouter"
ANTHROPIC: Final = "anthropic"
FAKE: Final = "fake"

#: Output-token assumption for the pre-call estimate when the caller sets no
#: ceiling. Matches the adapters' own default so the estimate and the request
#: cannot disagree.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 2048


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
    ModelTier.STRONG: (
        RouteEntry(ANTHROPIC, "claude-opus-5"),
        RouteEntry(OPENROUTER, "anthropic/claude-opus-4.8"),
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
    ) -> None:
        """Build a router.

        `providers` / `chains` / `task_tiers` exist so a test can inject stub
        providers and a two-entry chain without monkeypatching module state.
        Production passes none of them.
        """
        resolved = dict(providers) if providers is not None else build_providers(env)
        # The fake is always reachable: it is the floor the router degrades to,
        # so it must never be possible to configure it away.
        resolved.setdefault(FAKE, FakeProvider())

        self._providers: dict[str, Provider] = resolved
        self._chains = chains if chains is not None else TIER_CHAINS
        self._task_tiers = task_tiers if task_tiers is not None else TASK_TIERS
        self._default_max_tokens = default_max_tokens

    @property
    def providers(self) -> Mapping[str, Provider]:
        """The providers this router can reach, by name."""
        return self._providers

    def resolve(self, task: TaskClass) -> ResolvedRoute:
        """Map a task to its tier and its *available* fallback chain.

        Entries whose provider has no credential are filtered out rather than
        attempted and failed: an unreachable provider is not a fallback, it is
        latency. If nothing is left, the fake chain is used.
        """
        tier = self._task_tiers[task]
        chain = tuple(
            entry for entry in self._chains.get(tier, ()) if entry.provider in self._providers
        )
        if chain:
            return ResolvedRoute(task=task, tier=tier, chain=chain, using_fake=False)

        fake_chain = FAKE_CHAINS[tier]
        return ResolvedRoute(task=task, tier=tier, chain=fake_chain, using_fake=True)

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
    ) -> Completion:
        """Run `task` against its chain, falling back on retryable failures.

        Raises `BudgetExceededError` **before any network call** when the
        remaining budget cannot cover the estimate; `AllProvidersFailedError`
        carrying every cause when the chain is exhausted. A non-retryable
        failure (bad request, unparseable tool arguments) is raised straight
        through, because the next provider would reject it identically.
        """
        route = self.resolve(task)
        failures: list[ProviderFailure] = []

        for entry in route.chain:
            provider = self._providers[entry.provider]

            # -- the guard, and it is here on purpose: before the await ------ #
            if budget is not None:
                estimate = self.estimate_usd(entry.model, messages, tools, max_tokens)
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
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except RETRYABLE_ERRORS as exc:
                failures.append(
                    ProviderFailure(provider=entry.provider, model=entry.model, error=exc)
                )
                continue

            if budget is not None:
                budget.record(completion.usage)
            return completion

        raise AllProvidersFailedError(task.value, failures)
