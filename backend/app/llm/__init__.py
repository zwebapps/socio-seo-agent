"""Model access: one router, two real providers, one price table.

This is the only module an agent node imports to reach a language model. It is
not an engine -- engines may never import an LLM client (docs/ARCHITECTURE.md
section 3, enforced by tests/test_engine_boundary.py) -- and it is the reason
that rule can hold: everything vendor-specific is behind this boundary.

Usage from an agent node:

    from backend.app.llm import BudgetState, Message, ModelRouter, Role, TaskClass

    router = ModelRouter()
    completion = await router.complete(
        TaskClass.GENERATE,
        [Message(role=Role.USER, content=prompt)],
        budget=BudgetState(limit_usd=Decimal("0.50")),
    )

The node names a `TaskClass`; the model behind it is data in `router.py`.

The adapters are deliberately **not** re-exported here: importing this package
must not pull `openai` or `anthropic` into the process, so that a deployment
with no credentials never loads a vendor SDK. Import them from their own modules
when you need to construct one directly.
"""

from backend.app.llm.contract import (
    RETRYABLE_ERRORS,
    AllProvidersFailedError,
    BudgetExceededError,
    BudgetState,
    Completion,
    InvalidToolArgumentsError,
    LlmError,
    Message,
    ModelTier,
    Provider,
    ProviderError,
    ProviderFailure,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Role,
    TaskClass,
    ToolCall,
    ToolSpec,
    UnknownModelPriceError,
    Usage,
)
from backend.app.llm.fake_provider import FakeProvider
from backend.app.llm.pricing import (
    PRICE_TABLE,
    ModelPrice,
    compute_usd,
    conservative_token_estimate,
    is_priced,
    price_for,
)
from backend.app.llm.router import (
    TASK_TIERS,
    TIER_CHAINS,
    ConfigStatus,
    ModelRouter,
    ResolvedRoute,
    RouteEntry,
    build_providers,
    config_status,
)

__all__ = [
    "PRICE_TABLE",
    "RETRYABLE_ERRORS",
    "TASK_TIERS",
    "TIER_CHAINS",
    "AllProvidersFailedError",
    "BudgetExceededError",
    "BudgetState",
    "Completion",
    "ConfigStatus",
    "FakeProvider",
    "InvalidToolArgumentsError",
    "LlmError",
    "Message",
    "ModelPrice",
    "ModelRouter",
    "ModelTier",
    "Provider",
    "ProviderError",
    "ProviderFailure",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ResolvedRoute",
    "Role",
    "RouteEntry",
    "TaskClass",
    "ToolCall",
    "ToolSpec",
    "UnknownModelPriceError",
    "Usage",
    "build_providers",
    "compute_usd",
    "config_status",
    "conservative_token_estimate",
    "is_priced",
    "price_for",
]
