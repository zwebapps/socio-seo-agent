"""The price table, and the only arithmetic allowed to produce a USD figure.

Two rules, both load-bearing:

1. **`Decimal` end to end.** Prices are `Decimal` literals built from strings,
   never floats. `0.1 + 0.2 != 0.3` in binary floating point, and a ledger that
   drifts is a ledger nobody trusts by month three.
2. **An unknown model raises.** `price_for` refuses to invent a price. The
   alternative -- defaulting to zero -- makes every budget ceiling in
   docs/AGENT_RUNTIME.md section 8 unenforceable while still appearing to work.

Prices are per **million** tokens, in USD, as published by each provider. They
are data, not logic: changing a model's price, or adding a model, is a one-line
edit here and touches nothing else.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from backend.app.llm.contract import UnknownModelPriceError

#: Cost is rounded to this quantum. 1e-8 USD is far below a single cheap-tier
#: call (~$0.0001), so nothing observable is lost, and it keeps stored values
#: exact rather than carrying 30 significant digits into the database.
USD_QUANTUM = Decimal("0.00000001")

_TOKENS_PER_MILLION = Decimal(1_000_000)

#: Conservative characters-per-token divisor for pre-call estimation. English
#: prose averages ~4; 3 deliberately over-counts, because the budget guard must
#: err towards refusing a call it could have afforded rather than allowing one
#: it could not.
_CHARS_PER_TOKEN = 3


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Published price for one model id."""

    model: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


def _price(model: str, input_usd: str, output_usd: str) -> tuple[str, ModelPrice]:
    """Build a table entry, forcing `Decimal`-from-string construction."""
    return model, ModelPrice(
        model=model,
        input_usd_per_mtok=Decimal(input_usd),
        output_usd_per_mtok=Decimal(output_usd),
    )


#: Model id -> price. Keys are the exact strings sent to a provider, so the
#: OpenRouter and first-party Anthropic ids for the same underlying model are
#: separate rows: they are billed through different accounts and can diverge.
#:
#: Anthropic first-party prices and ids are the published rates (2026-06).
#: OpenRouter slugs carry a small routing margin in practice; verify against
#: openrouter.ai/models before enabling live keys.
PRICE_TABLE: Mapping[str, ModelPrice] = dict(
    (
        # -- OpenRouter (OpenAI-compatible surface) --------------------------- #
        _price("openai/gpt-4.1-mini", "0.40", "1.60"),
        _price("openai/gpt-4.1", "2.00", "8.00"),
        _price("google/gemini-2.5-flash", "0.30", "2.50"),
        _price("anthropic/claude-sonnet-4.5", "3.00", "15.00"),
        _price("anthropic/claude-opus-4.8", "5.00", "25.00"),
        # Verified against the OpenRouter models API on 2026-08-19, as the note
        # above instructs. Present so the STRONG chain has a non-Anthropic entry
        # (see `router.TIER_CHAINS`).
        _price("openai/gpt-5.1", "1.25", "10.00"),
        _price("google/gemini-2.5-pro", "1.25", "10.00"),
        # Embeddings: output tokens do not exist on an embedding call, so the
        # output rate is a true zero rather than an unknown.
        _price("openai/text-embedding-3-small", "0.02", "0.00"),
        # -- Anthropic first party ------------------------------------------- #
        _price("claude-haiku-4-5", "1.00", "5.00"),
        _price("claude-sonnet-5", "3.00", "15.00"),
        _price("claude-opus-5", "5.00", "25.00"),
        _price("claude-opus-4-8", "5.00", "25.00"),
        _price("claude-fable-5", "10.00", "50.00"),
        # -- FakeProvider ---------------------------------------------------- #
        # Priced at zero because zero is the truth: no request leaves the
        # process. These entries exist so the budget guard can estimate a fake
        # call without special-casing it.
        _price("fake/cheap", "0.00", "0.00"),
        _price("fake/mid", "0.00", "0.00"),
        _price("fake/strong", "0.00", "0.00"),
        _price("fake/embed", "0.00", "0.00"),
    )
)


def is_priced(model: str) -> bool:
    """Whether `model` has a price-table entry."""
    return model in PRICE_TABLE


def price_for(model: str) -> ModelPrice:
    """Return the price for `model`, or raise `UnknownModelPriceError`."""
    try:
        return PRICE_TABLE[model]
    except KeyError:
        raise UnknownModelPriceError(model) from None


def compute_usd(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Cost of a call, from the price table and provider-reported token counts.

    Raises `UnknownModelPriceError` for an unpriced model.
    """
    price = price_for(model)
    cost = (
        Decimal(tokens_in) * price.input_usd_per_mtok
        + Decimal(tokens_out) * price.output_usd_per_mtok
    ) / _TOKENS_PER_MILLION
    return cost.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)


def conservative_token_estimate(text: str) -> int:
    """Over-estimate the token count of `text`.

    Used only for the pre-call budget guard, where over-estimating is the safe
    direction. Never used to populate a `Usage` row -- those are always the
    provider's own numbers.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))
