"""The cost ledger. If this file is wrong, every budget in the system is a lie."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.app.llm.contract import UnknownModelPriceError
from backend.app.llm.pricing import (
    PRICE_TABLE,
    compute_usd,
    conservative_token_estimate,
    is_priced,
    price_for,
)
from backend.app.llm.router import FAKE_CHAINS, TIER_CHAINS


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's real key reach the suite."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_compute_usd_for_a_known_model() -> None:
    """Cost comes from the table and provider-reported tokens, not a guess."""
    # openai/gpt-4.1-mini is $0.40 / $1.60 per million tokens.
    # 1,000 in  -> 1000 * 0.40 / 1e6 = 0.0004
    # 2,000 out -> 2000 * 1.60 / 1e6 = 0.0032
    assert compute_usd("openai/gpt-4.1-mini", 1_000, 2_000) == Decimal("0.0036")


def test_compute_usd_matches_the_costed_run_in_the_docs() -> None:
    """A GENERATE-sized call on the strong tier lands where section 11 says.

    docs/AGENT_RUNTIME.md section 11 budgets GENERATE at ~$0.09 of a ~$0.14
    piece. claude-opus-5 is $5/$25 per Mtok, so 6k in + 2.5k out is $0.0925 --
    which is what makes the $0.50 per-run cap and the <$0.15 per-piece SLO
    (docs/ARCHITECTURE.md section 7.5) arithmetically reachable rather than
    aspirational.
    """
    assert compute_usd("claude-opus-5", 6_000, 2_500) == Decimal("0.0925")


def test_unknown_model_raises_rather_than_costing_zero() -> None:
    """A silent zero would make every budget cap unenforceable but look enforced."""
    with pytest.raises(UnknownModelPriceError) as caught:
        compute_usd("openai/definitely-not-a-real-model", 100, 100)

    assert caught.value.model == "openai/definitely-not-a-real-model"
    # The message must tell the next engineer where to fix it.
    assert "PRICE_TABLE" in str(caught.value)


def test_price_for_raises_for_unknown_and_is_priced_agrees() -> None:
    assert is_priced("claude-opus-5") is True
    assert is_priced("no-such-model") is False
    with pytest.raises(UnknownModelPriceError):
        price_for("no-such-model")


def test_zero_tokens_costs_exactly_zero() -> None:
    assert compute_usd("claude-opus-5", 0, 0) == Decimal("0")


def test_the_same_computation_drifts_in_float_but_not_in_decimal() -> None:
    """This is not a theoretical concern -- it is *this* price calculation.

    1,000 in + 2,000 out on openai/gpt-4.1-mini in binary floating point gives
    0.0036000000000000003. Decimal gives 0.0036. Multiply that error across
    thousands of runs a month and the ledger stops reconciling with the invoice.
    """
    float_result = 1_000 * 0.40 / 1e6 + 2_000 * 1.60 / 1e6
    assert float_result != 0.0036  # the drift is real, on real inputs

    decimal_result = compute_usd("openai/gpt-4.1-mini", 1_000, 2_000)
    assert decimal_result == Decimal("0.0036")
    assert isinstance(decimal_result, Decimal)


def test_accumulated_cost_stays_exact() -> None:
    """A run makes many calls; the running total must not accrue error."""
    usd = compute_usd("openai/gpt-4.1-mini", 100, 100)
    assert usd == Decimal("0.0002")

    total = sum((usd for _ in range(30)), Decimal(0))
    assert total == Decimal("0.006")
    assert isinstance(total, Decimal)


def test_prices_are_decimal_not_float() -> None:
    """A float anywhere in the table would silently re-introduce drift."""
    for price in PRICE_TABLE.values():
        assert isinstance(price.input_usd_per_mtok, Decimal), price.model
        assert isinstance(price.output_usd_per_mtok, Decimal), price.model


def test_every_routable_model_has_a_price() -> None:
    """A model in a chain but not in the table would raise at run time.

    This is the guard that makes the "unknown model raises" rule safe to have:
    without it, adding a model to a chain and forgetting its price would surface
    as an exception mid-run instead of as a red test.
    """
    routable = {
        entry.model
        for chains in (TIER_CHAINS, FAKE_CHAINS)
        for chain in chains.values()
        for entry in chain
    }
    unpriced = sorted(model for model in routable if not is_priced(model))
    assert not unpriced, f"routable but unpriced: {unpriced}"


def test_fake_models_are_priced_at_zero() -> None:
    """Zero here is the truth, not a default: nothing leaves the process."""
    for tier_chain in FAKE_CHAINS.values():
        for entry in tier_chain:
            assert compute_usd(entry.model, 10_000, 10_000) == Decimal("0")


def test_token_estimate_over_counts_on_purpose() -> None:
    """The guard must err towards refusing, so the estimate must not under-count."""
    text = "a" * 120
    assert conservative_token_estimate(text) == 40  # 120 / 3
    assert conservative_token_estimate(text) > len(text) // 4  # vs the ~4 average
    assert conservative_token_estimate("") == 0
    assert conservative_token_estimate("a") == 1  # never rounds down to zero
