"""The sampling bounds, and the arithmetic that has to hold for them to be defensible.

The floor on `max_output_tokens` is the interesting one. A ceiling below what a German
blog draft needs does not shorten the draft -- it truncates a JSON tool-call argument,
which then does not parse, so the node gets NO structured output at all. So the floor is
asserted against the computation rather than against a number somebody liked, and the
computation is asserted against its own inputs.
"""

from decimal import Decimal

import pytest

from backend.app.llm.contract import ModelTier
from backend.app.llm.router import DEFAULT_MAX_OUTPUT_TOKENS, TIER_CHAINS
from backend.app.llm.sampling import (
    GERMAN_CHARS_PER_TOKEN,
    MAX_TOKENS_MAX,
    MAX_TOKENS_MIN,
    MAX_TOKENS_STEP,
    MODELS_REJECTING_SAMPLING,
    REFERENCE_ARTICLE_CHARS,
    TEMPERATURE_MAX,
    TEMPERATURE_MIN,
    TEMPERATURE_STEP,
    SamplingBoundsError,
    SamplingRecord,
    rejects_sampling,
    tokens_for_article,
    validate_max_output_tokens,
    validate_temperature,
)

# --------------------------------------------------------------------------- #
# The max-tokens floor
# --------------------------------------------------------------------------- #


def test_the_floor_clears_what_a_2500_character_german_article_actually_needs() -> None:
    """The whole reason the floor is not 512 or 768."""
    needed = tokens_for_article(REFERENCE_ARTICLE_CHARS)

    assert needed <= MAX_TOKENS_MIN, (
        f"the floor {MAX_TOKENS_MIN} is below the {needed} tokens a "
        f"{REFERENCE_ARTICLE_CHARS}-character article needs, so every blog draft would be "
        "truncated -- and a truncated JSON tool-call argument does not parse, so the node "
        "gets nothing rather than something short"
    )


def test_the_floor_leaves_headroom_without_being_wasteful() -> None:
    """Both directions matter. Too tight truncates; far too generous inflates the pre-call
    budget reservation on every single call, because the guard assumes the full
    allowance is emitted."""
    needed = tokens_for_article()

    assert needed * 2 > MAX_TOKENS_MIN, (
        "the floor is more than twice what is needed; the budget guard reserves the full "
        "allowance per call, so that is spent headroom, not safety"
    )


def test_a_prose_only_calculation_would_have_produced_too_low_a_floor() -> None:
    """This is the mistake the two-divisor model exists to avoid: GENERATE emits HTML
    inside a JSON argument, not prose, and counting the whole output at four characters
    per token makes an insufficient ceiling look sufficient."""
    prose_only = REFERENCE_ARTICLE_CHARS // GERMAN_CHARS_PER_TOKEN

    assert prose_only < tokens_for_article(), (
        "the markup and envelope terms are not contributing, so the floor rests on a "
        "prose-only estimate again"
    )
    assert prose_only < 700, "sanity: the naive figure is the ~625 the backlog quotes"


def test_the_computation_scales_with_the_article() -> None:
    assert tokens_for_article(5000) > tokens_for_article(2500) > tokens_for_article(1000)


def test_the_bounds_sit_on_the_step_grid() -> None:
    """A slider whose min or max is off-step cannot actually reach its own limit."""
    assert MAX_TOKENS_MIN % MAX_TOKENS_STEP == 0
    assert MAX_TOKENS_MAX % MAX_TOKENS_STEP == 0


def test_the_default_output_ceiling_is_inside_the_offered_range() -> None:
    """The router's own default has to be a value the screen can express, or "revert to
    default" lands somewhere the slider cannot represent."""
    assert MAX_TOKENS_MIN <= DEFAULT_MAX_OUTPUT_TOKENS <= MAX_TOKENS_MAX


# --------------------------------------------------------------------------- #
# The temperature ceiling
# --------------------------------------------------------------------------- #


def test_the_temperature_ceiling_is_the_strictest_adapters_maximum() -> None:
    """The Anthropic Messages API accepts 0-1 and the OpenAI-compatible surfaces accept
    0-2, so a control that could emit 1.7 would 400 as soon as a chain fell back to
    Anthropic. 1.0 is also comfortably below the point where marketing copy stops being
    usable, so the strict bound costs nothing."""
    ceiling, floor = TEMPERATURE_MAX, TEMPERATURE_MIN

    assert ceiling == Decimal("1.00")
    assert floor == Decimal("0.00")
    assert ceiling < Decimal("1.2"), (
        "the ceiling is at or above the point where generated copy becomes unusable"
    )


def test_the_temperature_step_is_fine_enough_to_matter_and_coarse_enough_to_traverse() -> None:
    stops = (TEMPERATURE_MAX - TEMPERATURE_MIN) / TEMPERATURE_STEP

    assert 10 <= stops <= 40, (
        f"{stops} stops: below ten and a perceptible change is unreachable, above forty "
        "and a keyboard user cannot cross the range"
    )


@pytest.mark.parametrize("value", ["0.00", "0.35", "1.00"])
def test_an_in_range_temperature_is_accepted(value: str) -> None:
    assert validate_temperature(Decimal(value)) == Decimal(value)


@pytest.mark.parametrize("value", ["-0.01", "1.01", "2.00"])
def test_an_out_of_range_temperature_is_refused_with_its_bound(value: str) -> None:
    with pytest.raises(SamplingBoundsError) as exc:
        validate_temperature(Decimal(value))

    assert exc.value.field == "temperature"
    assert "0.00..1.00" in str(exc.value)


def test_an_off_step_temperature_is_snapped_rather_than_refused() -> None:
    """A slider cannot produce 0.37, but a curl can, and 0.37 is a coherent wish. Snapping
    keeps stored values on the grid the screen renders."""
    assert validate_temperature(Decimal("0.37")) == Decimal("0.35")
    assert validate_temperature(Decimal("0.38")) == Decimal("0.40")


@pytest.mark.parametrize("value", [1024, 2048, 8192])
def test_an_in_range_ceiling_is_accepted(value: int) -> None:
    assert validate_max_output_tokens(value) == value


@pytest.mark.parametrize("value", [0, 512, 1023, 8193, 100_000])
def test_an_out_of_range_ceiling_is_refused(value: int) -> None:
    with pytest.raises(SamplingBoundsError):
        validate_max_output_tokens(value)


def test_an_off_grid_ceiling_is_snapped() -> None:
    assert validate_max_output_tokens(2000) == 2048
    assert validate_max_output_tokens(2100) == 2048


# --------------------------------------------------------------------------- #
# Models that refuse the parameter
# --------------------------------------------------------------------------- #


def test_the_anthropic_adapter_reads_the_same_set_this_module_owns() -> None:
    """The set MOVED here from the adapter so config validation could read it without
    importing the vendor SDK. If the adapter ever grows its own copy, an operator would be
    warned about one list while the adapter enforced another."""
    from backend.app.llm import anthropic_provider

    assert anthropic_provider.MODELS_REJECTING_SAMPLING is MODELS_REJECTING_SAMPLING


def test_the_strong_tiers_first_choice_is_a_model_that_refuses_a_temperature() -> None:
    """This is why `temperature_for` has to take a model, and why the screen has to warn
    instead of just offering a slider: GENERATE is the task an operator most wants to tune
    and its first-choice model rejects the parameter outright."""
    first = TIER_CHAINS[ModelTier.STRONG][0]

    assert rejects_sampling(first.model), (
        "if this is no longer true the warning on the sampling screen is stale -- check "
        "MODELS_REJECTING_SAMPLING against the chain rather than deleting the test"
    )


def test_a_model_that_accepts_sampling_is_not_in_the_set() -> None:
    """Guard against the set becoming "every Anthropic model", which would silently
    disable the temperature control everywhere."""
    assert not rejects_sampling("claude-haiku-4-5")
    assert not rejects_sampling("openai/gpt-4.1-mini")


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


def test_an_unset_record_is_empty_so_nothing_is_sent() -> None:
    """The property that lets this ship without changing behaviour: no configuration means
    no parameter, which is what every call site does today."""
    from backend.app.llm.contract import TaskClass

    record = SamplingRecord(task_class=TaskClass.GENERATE)

    assert record.is_empty
    assert record.temperature is None
    assert record.max_output_tokens is None


def test_a_partly_configured_record_is_not_empty() -> None:
    from backend.app.llm.contract import TaskClass

    assert not SamplingRecord(task_class=TaskClass.GENERATE, max_output_tokens=4096).is_empty
