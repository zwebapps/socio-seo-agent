"""Sampling policy: the temperature and output ceiling a task class runs at.

This module owns three things and no side effects: the BOUNDS an operator may choose
between, the ARITHMETIC that justifies those bounds, and the set of models that refuse
a temperature at all. It imports no vendor SDK and touches no database, so the admin
screen and the router can both read it cheaply.

Why bounds at all, rather than "any number the provider accepts":

* **Temperature stops at 1.0** because that is the MINIMUM of the ranges our own two
  adapters accept. The Anthropic Messages API takes 0-1; the OpenAI-compatible surfaces
  (OpenRouter, Ollama) take 0-2. A control that can emit 1.7 is a control that produces
  a 400 as soon as a route falls back to Anthropic -- so the slider's ceiling is set by
  the strictest adapter, not the most permissive one. Nothing useful is lost: marketing
  copy degrades into non-sequiturs and invented specifics well below that, and this
  pipeline turns invented specifics into claim-gate refusals rather than flair.
* **`max_output_tokens` starts at 1024** because a lower ceiling silently truncates the
  one artefact the product exists to produce. See :func:`tokens_for_article` for the
  arithmetic; the short version is that a 2500-character German article is ~625 tokens
  of prose but ~1000 tokens once it is HTML inside a JSON tool-call argument, and a
  truncated JSON string does not parse, so the node gets NO structured output rather
  than a short one. Truncation here is a total loss, not a degradation.
* **`max_output_tokens` stops at 8192** because of the budget guard, not the model.
  `ModelRouter.estimate_usd` assumes the model emits its whole allowance, so this
  number IS the reservation the pre-call guard makes before every call, and raising it
  buys refusals rather than longer articles.

`temperature` is a `Decimal` in storage and on the wire it is a plain JSON number.
That is deliberate and it is NOT a violation of the money rule in CLAUDE.md: `Decimal`
is used here so a stored 0.7 reads back as `0.7` rather than `0.7000000000000001` on a
settings screen, and temperature is not currency, so it is not serialised as a string
the way `usd` is.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

from backend.app.llm.contract import TaskClass

# --------------------------------------------------------------------------- #
# Models that refuse the parameter outright
# --------------------------------------------------------------------------- #

#: Models that return HTTP 400 if `temperature` (or `top_p`/`top_k`) is sent at all.
#: The parameter was removed on these, and prompting is the supported way to steer
#: them.
#:
#: This lives here rather than in `anthropic_provider` -- which is where it used to
#: live, and which still re-exports it -- because two callers need it and only one of
#: them may import the vendor SDK: the adapter refuses such a request locally instead
#: of shipping a 400, and the admin API has to tell an operator *before* they save a
#: temperature that the route they are pointing it at cannot accept one. Reading that
#: fact should not cost an `import anthropic`.
MODELS_REJECTING_SAMPLING: Final[frozenset[str]] = frozenset(
    {
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-mythos-5",
    }
)


def rejects_sampling(model: str) -> bool:
    """Whether sending `temperature` to `model` is an error rather than a preference."""
    return model in MODELS_REJECTING_SAMPLING


# --------------------------------------------------------------------------- #
# The bounds, and the arithmetic behind them
# --------------------------------------------------------------------------- #

TEMPERATURE_MIN: Final = Decimal("0.00")
#: The strictest adapter's maximum, not the most permissive one. See the module note.
TEMPERATURE_MAX: Final = Decimal("1.00")
#: 21 stops. Fine enough that 0.20 and 0.25 are separately reachable -- which is a
#: perceptible difference in generated copy -- and coarse enough that a keyboard user
#: crosses the whole range in twenty arrow presses rather than a hundred.
TEMPERATURE_STEP: Final = Decimal("0.05")

MAX_TOKENS_MIN: Final = 1024
MAX_TOKENS_MAX: Final = 8192
MAX_TOKENS_STEP: Final = 256

#: Characters per token for German prose. The same divisor the knowledge-base engine
#: uses (`engines/kb/contract.CHARS_PER_TOKEN`), quoted rather than re-derived so the
#: two cannot drift: "the standard rule of thumb for English and German prose".
#:
#: Note this is NOT the divisor `pricing.conservative_token_estimate` uses. That one is
#: 3, deliberately pessimistic, because it feeds a guard whose job is to refuse. This
#: one feeds a floor, where UNDER-counting is the dangerous direction, so the smaller
#: (more tokens per character) figure is the safe one here too.
GERMAN_CHARS_PER_TOKEN: Final = 4

#: The length of a blog draft this product is expected to produce. The 2500-character
#: article is the worked example in the backlog and the reason the floor is not 512.
REFERENCE_ARTICLE_CHARS: Final = 2500

#: Markup characters the `record_page` schema explicitly demands around that prose:
#: one `h1` (9), roughly five `h2` (45), roughly eight `p` (56), and three links with
#: `href` attributes whose quotes are backslash-escaped inside the JSON argument
#: (~190). Counted from the schema's own instructions rather than rounded to taste.
MARKUP_CHARS: Final = 300

#: Markup tokenises far worse than prose -- `<h2>` is about three tokens for four
#: characters -- so it gets its own divisor instead of being folded into the prose one.
#: Using 4 for the whole output is the mistake that makes a ceiling look sufficient
#: and truncate anyway.
MARKUP_CHARS_PER_TOKEN: Final = Decimal("1.5")

#: Title (50-60 chars) plus meta description (140-160 chars) plus the tool-call
#: envelope's own keys, braces and quoting.
ENVELOPE_TOKENS: Final = 80


def tokens_for_article(chars: int = REFERENCE_ARTICLE_CHARS) -> int:
    """Output tokens a `record_page` call needs for an article of `chars` characters.

    Three terms, and leaving any of them out is how a ceiling ends up too low:

    1. the prose, at :data:`GERMAN_CHARS_PER_TOKEN`;
    2. :data:`MARKUP_CHARS` at :data:`MARKUP_CHARS_PER_TOKEN`, because GENERATE does not
       emit prose -- it emits HTML inside a JSON tool-call argument;
    3. :data:`ENVELOPE_TOKENS` for the title, meta description and surrounding object.

    At the reference length that is 625 + 200 + 80 = 905 tokens. :data:`MAX_TOKENS_MIN`
    is therefore 1024 -- the first step on the grid above it, with ~13% headroom -- and
    not the 590-625 figure a prose-only calculation produces.
    """
    prose = Decimal(chars) / Decimal(GERMAN_CHARS_PER_TOKEN)
    markup = Decimal(MARKUP_CHARS) / MARKUP_CHARS_PER_TOKEN
    return math.ceil(prose + markup) + ENVELOPE_TOKENS


class SamplingBoundsError(ValueError):
    """A sampling value outside the range this build offers.

    Carries the bound it broke, because "invalid temperature" cannot be acted on.
    """

    def __init__(self, field: str, value: object, low: object, high: object) -> None:
        self.field = field
        self.value = value
        super().__init__(
            f"{field}={value!r} is outside the supported range {low}..{high}. "
            "The bounds are set in backend/app/llm/sampling.py and each has a reason "
            "recorded there -- read it before widening one."
        )


def validate_temperature(value: Decimal) -> Decimal:
    """Return `value` quantised to :data:`TEMPERATURE_STEP`, or raise."""
    if value < TEMPERATURE_MIN or value > TEMPERATURE_MAX:
        raise SamplingBoundsError("temperature", value, TEMPERATURE_MIN, TEMPERATURE_MAX)
    # Quantise rather than reject an off-step value. A slider cannot produce one, but a
    # curl can, and 0.37 is a coherent wish -- snapping it is friendlier than a 422 and
    # keeps stored values on the same grid the UI can render.
    steps = (value / TEMPERATURE_STEP).quantize(Decimal("1"))
    return (steps * TEMPERATURE_STEP).quantize(Decimal("0.01"))


def validate_max_output_tokens(value: int) -> int:
    """Return `value` clamped onto the step grid, or raise if out of range."""
    if value < MAX_TOKENS_MIN or value > MAX_TOKENS_MAX:
        raise SamplingBoundsError("max_output_tokens", value, MAX_TOKENS_MIN, MAX_TOKENS_MAX)
    return round(value / MAX_TOKENS_STEP) * MAX_TOKENS_STEP


class SamplingRecord(BaseModel):
    """One stored sampling decision for one task class.

    Both fields are optional and `None` means "send nothing, take the provider
    default" -- which is exactly what every call site does today. That is what lets
    this ship without changing any behaviour until somebody moves a slider.
    """

    model_config = ConfigDict(frozen=True)

    task_class: TaskClass
    temperature: Decimal | None = None
    max_output_tokens: int | None = None
    note: str | None = None

    @property
    def is_empty(self) -> bool:
        """Nothing configured, so the record may as well not exist."""
        return self.temperature is None and self.max_output_tokens is None


__all__ = [
    "ENVELOPE_TOKENS",
    "GERMAN_CHARS_PER_TOKEN",
    "MARKUP_CHARS",
    "MARKUP_CHARS_PER_TOKEN",
    "MAX_TOKENS_MAX",
    "MAX_TOKENS_MIN",
    "MAX_TOKENS_STEP",
    "MODELS_REJECTING_SAMPLING",
    "REFERENCE_ARTICLE_CHARS",
    "TEMPERATURE_MAX",
    "TEMPERATURE_MIN",
    "TEMPERATURE_STEP",
    "SamplingBoundsError",
    "SamplingRecord",
    "rejects_sampling",
    "tokens_for_article",
    "validate_max_output_tokens",
    "validate_temperature",
]
