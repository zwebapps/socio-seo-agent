"""The public contract every caller and every provider adapter agrees on.

This module is deliberately dependency-free apart from Pydantic: no LLM SDK, no
HTTP client, no settings. That is what lets an agent node import the types
without dragging a vendor SDK into its import graph, and what lets the two real
adapters (OpenRouter, Anthropic) be swapped without touching a call site.

Three ideas carry the whole design:

* **A caller asks for a TASK, never for a model.** `TaskClass` is the vocabulary
  of the agent graph; the mapping to a tier and then to an ordered list of
  concrete models lives in one table in `router.py` (docs/ARCHITECTURE.md
  section 8: "Never a hardcoded model name outside models/").
* **Money is `Decimal`.** `Usage.usd` is computed from an explicit price table
  using provider-reported token counts -- never a float, never a guess. A
  budget system built on approximations is a budget system that lies.
* **Errors are typed and carry their cause.** `AllProvidersFailedError` holds
  every underlying failure, so a caller can report *why* rather than
  "something failed" (docs/ARCHITECTURE.md section 7.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Task classes and tiers
# --------------------------------------------------------------------------- #


class TaskClass(StrEnum):
    """What the caller wants done. The only model-selection input a node gives.

    Mapped to a `ModelTier` by `router.TASK_TIERS`. Named after the work, not
    after a node, so two nodes doing the same kind of work share a route.
    """

    CLASSIFY = "classify"
    EXTRACT = "extract"
    REPACK = "repack"
    PLAN = "plan"
    PRIORITISE = "prioritise"
    GENERATE = "generate"
    REVIEW = "review"
    EMBED = "embed"


class ModelTier(StrEnum):
    """How much capability the task is worth paying for."""

    CHEAP = "cheap"
    MID = "mid"
    STRONG = "strong"
    EMBED = "embed"


class Role(StrEnum):
    """Conversation roles, in the neutral shape both adapters translate from.

    `TOOL` is the OpenAI-style tool-result turn. The Anthropic adapter converts
    it into a `tool_result` content block on a user turn, because that is how
    the Messages API expresses the same thing.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class ToolCall(BaseModel):
    """A model's request to run one tool.

    `arguments` is already-parsed JSON. Both adapters parse it, so a tool never
    sees a raw string and no tool contains defensive JSON handling
    (docs/AGENT_RUNTIME.md section 4). Malformed arguments raise
    `InvalidToolArgumentsError` at the adapter boundary instead.
    """

    name: str
    arguments: dict[str, Any]
    call_id: str


class ToolSpec(BaseModel):
    """A tool offered to the model: name, prose, and a JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


class Message(BaseModel):
    """One conversation turn.

    `tool_calls` is only meaningful on an `ASSISTANT` turn (it replays what the
    model asked for). `tool_call_id` is only meaningful on a `TOOL` turn (it
    says which request this result answers).
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class Usage(BaseModel):
    """What one model call actually cost.

    Persisted as a `model_usage` row (docs/ARCHITECTURE.md section 8), which is
    what makes "what does a content piece cost us?" a query rather than a guess.

    `estimated` is `False` for every well-behaved response. It is set only when
    a provider returned no usage block at all: rather than reporting a silent
    zero -- which would quietly corrupt the ledger -- the adapter substitutes a
    conservative estimate and marks it, so the row is visibly approximate.
    """

    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    usd: Decimal
    latency_ms: int
    estimated: bool = False


class Completion(BaseModel):
    """One model response, in the shape both adapters normalise onto.

    `is_final` is the node loop's exit condition: `True` means the model
    answered, `False` means it asked for tools and expects to be called again.
    """

    text: str | None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage
    is_final: bool


class BudgetState(BaseModel):
    """Remaining spend for a run, checked *before* each call.

    Mutable on purpose: the router records usage into it after a successful
    call, so one object threads through a whole run. Cost per run is capped at
    $0.50 by docs/AGENT_RUNTIME.md section 8; that number is the caller's to
    set, not this module's.
    """

    limit_usd: Decimal
    spent_usd: Decimal = Decimal(0)

    @property
    def remaining_usd(self) -> Decimal:
        """Budget left. May go negative if a call overshot its estimate."""
        return self.limit_usd - self.spent_usd

    def can_afford(self, estimated_usd: Decimal) -> bool:
        """Whether `estimated_usd` still fits inside the remaining budget."""
        return estimated_usd <= self.remaining_usd

    def record(self, usage: Usage) -> None:
        """Add a completed call's real cost to the running total."""
        self.spent_usd += usage.usd


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class LlmError(Exception):
    """Base class for every failure raised by `backend.app.llm`."""


class UnknownModelPriceError(LlmError):
    """A model was used that has no entry in the price table.

    Raised rather than defaulting to zero. A silent zero would make every
    budget ceiling in the system unenforceable while still *looking* enforced,
    which is strictly worse than a loud failure at the point of the mistake.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(
            f"No price-table entry for model {model!r}. Add it to "
            "backend/app/llm/pricing.py:PRICE_TABLE -- an unpriced model would "
            "cost zero in the ledger and silently defeat every budget cap."
        )


class BudgetExceededError(LlmError):
    """The remaining budget cannot cover a conservative estimate of the call.

    Raised *before* any network request. See `ModelRouter.complete`.
    """

    def __init__(
        self,
        *,
        model: str,
        limit_usd: Decimal,
        spent_usd: Decimal,
        estimated_usd: Decimal,
    ) -> None:
        self.model = model
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self.estimated_usd = estimated_usd
        self.remaining_usd = limit_usd - spent_usd
        super().__init__(
            f"Budget exceeded before calling {model!r}: estimated "
            f"${estimated_usd} needed, ${self.remaining_usd} remaining "
            f"(limit ${limit_usd}, spent ${spent_usd})."
        )


class ProviderError(LlmError):
    """Base for failures attributable to one provider call.

    Carries which provider and model failed so `AllProvidersFailedError` can
    report a chain rather than a pile of anonymous messages.
    """

    def __init__(self, provider: str, model: str, message: str) -> None:
        self.provider = provider
        self.model = model
        self.detail = message
        super().__init__(f"{provider}/{model}: {message}")


class ProviderRateLimitError(ProviderError):
    """Provider returned 429. Retryable, and a reason to fall back."""

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(provider, model, message)


class ProviderTimeoutError(ProviderError):
    """The request timed out. Retryable, and a reason to fall back."""


class ProviderServerError(ProviderError):
    """Provider 5xx, or a transport failure that never reached it.

    Not in the required error list, but 5xx and connection failures are
    explicitly fallback-triggering (docs/ARCHITECTURE.md section 7.2) and
    labelling them `ProviderTimeoutError` would be a lie in the trace.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(provider, model, message)


class ProviderRequestError(ProviderError):
    """Provider 4xx other than 429 -- our request was wrong.

    Deliberately *not* retryable: a malformed request, an unknown model id, or
    a rejected parameter will fail identically on the next provider, so falling
    back would just spend latency to produce the same error.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(provider, model, message)


class InvalidToolArgumentsError(ProviderError):
    """The model's tool arguments were not a JSON object.

    Raised at the adapter boundary so the tool never sees malformed input. The
    node loop answers this with exactly one repair turn carrying the error
    verbatim (docs/AGENT_RUNTIME.md section 4).
    """

    def __init__(self, provider: str, model: str, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        super().__init__(provider, model, f"tool {tool_name!r}: {message}")


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """One link in an exhausted fallback chain."""

    provider: str
    model: str
    error: LlmError


class AllProvidersFailedError(LlmError):
    """Every entry in the fallback chain failed.

    Carries all underlying errors, because "the chain failed" is not an
    actionable report and "rate-limited on A, 503 on B" is.
    """

    def __init__(self, task: str, failures: Sequence[ProviderFailure]) -> None:
        self.task = task
        self.failures = tuple(failures)
        chain = " ; ".join(
            f"{failure.provider}/{failure.model}: "
            f"{type(failure.error).__name__}: {failure.error.args[0]}"
            if failure.error.args
            else f"{failure.provider}/{failure.model}: {type(failure.error).__name__}"
            for failure in self.failures
        )
        super().__init__(f"All {len(self.failures)} provider(s) failed for task {task!r}: {chain}")


#: Failures that justify trying the next entry in the fallback chain. Anything
#: else (a bad request, unparseable tool arguments) would fail the same way on
#: the next provider, so it is raised straight through.
RETRYABLE_ERRORS: tuple[type[LlmError], ...] = (
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderServerError,
)


# --------------------------------------------------------------------------- #
# Provider protocol
# --------------------------------------------------------------------------- #


class Provider(Protocol):
    """What every adapter implements, real or fake.

    Structural, not inherited: an adapter satisfies this by shape, so a test
    stub is a plain class and needs no import from this module.

    Note the absence of `budget`. A provider's job is one call; budget control
    is the router's, and keeping it out of here means an adapter can never be
    the thing that forgot to check.
    """

    name: str

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Run one model call and normalise the response onto `Completion`."""
        ...
