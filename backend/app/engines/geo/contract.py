"""Typed contract for the `geo` engine: AI-answer visibility.

These shapes are a published contract. `ProbeOutcome` is what the service
persists as a `geo_results` row (docs/ROADMAP.md section 6), `ShareOfVoice` is
what the dashboard tile renders, and `SovDelta` is the two-run trend. Renaming a
field is a breaking change to three consumers.

**What this engine measures, stated plainly, because the whole module depends on
being honest about it.** There is no citation API for ChatGPT or Perplexity, so
visibility is measured by *probing*: a fixed set of questions is asked of N
models and the answers are searched for the brand. That makes every number here
a **sample, not a census** (docs/ARCHITECTURE.md section 15, limit 2). Three
design consequences follow, and each is enforced by a type rather than left to a
caller's good intentions:

* **`no_answer` is excluded from the denominator.** A refusal or an outage is
  the absence of a measurement, never the absence of the brand. `ShareOfVoice`
  therefore separates `usable_answers` from `no_answer_count`, and a
  `no_answer` outcome is forbidden from carrying a presence signal at all.
* **A percentage cannot be moved without its denominator.** The share fields are
  derived properties over stored counts, so anything that serialises a
  `ShareOfVoice` necessarily carries "9 of 41 answers across 3 models" with it.
* **Comparability is provable, not assumed.** A set version *and* a fingerprint
  over the question ids ride on every score, and the diff refuses to subtract
  two runs that asked different questions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------- #
# Prompt set
# --------------------------------------------------------------------------- #

#: Bumped whenever a template, a category, or the id scheme changes. Two runs on
#: different versions are not comparable and `diff_share_of_voice` says so
#: instead of subtracting them (docs/ARCHITECTURE.md section 15: comparability
#: requires a fixed prompt set).
PROMPT_SET_VERSION = "geo-v1"

#: The high-intent question shapes. Closed on purpose: the UI groups results by
#: category and the score reports a per-category breakdown, so a new shape is a
#: deliberate contract change plus a version bump.
type PromptCategory = Literal[
    "best_in_city",  # "best {service} in {city}"
    "near_city",  # "who offers {service} near {city}"
    "cost",  # "how much does {service} cost"
    "comparison",  # "{brand} vs {competitor}"
    "reputation",  # "is {brand} any good"
]

#: Categories whose text necessarily names the brand. A mention in one of these
#: is close to guaranteed and says nothing about discoverability, which is why
#: `ShareOfVoice` reports the unprompted rate separately.
BRAND_NAMING_CATEGORIES: frozenset[str] = frozenset({"comparison", "reputation"})


class GeoPrompt(BaseModel):
    """One question in the fixed prompt set.

    `prompt_id` is a content hash, not a sequence number: the id of a question
    is derived from the question, so a reworded prompt gets a new id and can
    never be silently compared against last week's answers to the old wording.
    """

    prompt_id: str
    text: str
    category: PromptCategory
    locale: str
    set_version: str
    contains_brand: bool
    #: The service or competitor this prompt was built from -- for grouping in the
    #: UI, and for answering "which service are we invisible for?".
    subject: str | None = None


class BrandIdentity(BaseModel):
    """How to recognise one business in a block of prose.

    Names and domains are deliberately separate inputs to separate outputs:
    matching a name proves a *mention*, matching a domain proves a *citation*,
    and those are different products of value to a customer.
    """

    name: str
    #: Other spellings a model might use: a trading name, a shortened form, a
    #: legal form. Matched with the same folding as `name`.
    aliases: list[str] = Field(default_factory=list)
    #: Registrable hosts, with or without scheme and `www.`. A subdomain of a
    #: listed host counts; a host that merely *contains* one does not.
    domains: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

#: `answered` = the model produced something we could read for presence.
#: `no_answer` = a refusal, an empty response, or a failed call. Excluded from
#: every denominator in this module.
type AnswerStatus = Literal["answered", "no_answer"]


class PresenceResult(BaseModel):
    """What one answer says about who is present.

    `mentioned` and `cited` are independent by design. A model that prints the
    URL without naming the business has cited it and not mentioned it; a model
    that praises it without linking has mentioned it and not cited it. Collapsing
    the two would destroy the distinction a customer is actually buying, so
    `present` is offered as a convenience and is never used as a denominator.
    """

    mentioned: bool
    cited: bool
    #: The alias that actually matched, so a false positive is debuggable from a
    #: stored row rather than by re-running the detector.
    matched_name: str | None = None
    matched_domains: list[str] = Field(default_factory=list)
    competitors_mentioned: list[str] = Field(default_factory=list)
    competitors_cited: list[str] = Field(default_factory=list)

    @property
    def present(self) -> bool:
        """Mentioned or cited. Reporting convenience only."""
        return self.mentioned or self.cited


class ProbeOutcome(BaseModel):
    """One (prompt x model) probe. Persisted as a `geo_results` row.

    No timestamp: the engine is pure and the service that writes the row owns
    `run_at`. Keeping the clock out of here is what makes every test in
    `tests/engines/test_geo.py` byte-deterministic.
    """

    prompt_id: str
    prompt_text: str
    category: PromptCategory
    set_version: str
    #: Whether the *question* named the brand. Carried on the row so a stored
    #: run can still be re-scored into prompted and unprompted halves.
    prompt_contains_brand: bool
    provider: str
    model: str
    status: AnswerStatus
    mentioned: bool = False
    cited: bool = False
    competitors_mentioned: list[str] = Field(default_factory=list)
    competitors_cited: list[str] = Field(default_factory=list)
    #: A short, human-readable slice of the answer. Evidence for the number: a
    #: user who cannot see why we scored an answer will not believe the score.
    answer_excerpt: str = ""
    #: Why the probe produced no answer -- a refusal, a 429, a timeout. Stored so
    #: "the model was down" is distinguishable from "the model refused".
    error: str | None = None
    usd: Decimal = Decimal(0)
    latency_ms: int = 0

    @model_validator(mode="after")
    def _no_answer_carries_no_presence(self) -> Self:
        """A `no_answer` may never claim a presence signal.

        This is the single most important invariant in the module, expressed
        where it cannot be forgotten. Without it, a refusal that happened to
        contain the brand name -- "I can't recommend, but Müller Sanitär
        exists" -- could be counted as a mention against a denominator it was
        excluded from, producing a share above 100%.
        """
        if self.status == "no_answer" and (
            self.mentioned or self.cited or self.competitors_mentioned or self.competitors_cited
        ):
            raise ValueError(
                "a no_answer outcome cannot carry mentions or citations: it is the "
                "absence of a measurement, not the absence of a brand"
            )
        return self


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


class ModelShare(BaseModel):
    """One model's slice of the sample.

    Present so a caller can never show a pooled percentage alone. Models
    disagree wildly on the same question, and a 22% that is really "60% on one
    model, 0% on two" is a different fact about the business.
    """

    provider: str
    model: str
    usable_answers: int
    no_answer_count: int
    mentions: int
    citations: int

    @property
    def mention_share_pct(self) -> float | None:
        """Percent of usable answers mentioning the brand, or `None` if none were usable."""
        return _share(self.mentions, self.usable_answers)

    @property
    def citation_share_pct(self) -> float | None:
        return _share(self.citations, self.usable_answers)


class CompetitorShare(BaseModel):
    """One competitor's slice, on exactly the same prompts and denominator."""

    name: str
    mentions: int
    citations: int
    usable_answers: int

    @property
    def mention_share_pct(self) -> float | None:
        return _share(self.mentions, self.usable_answers)

    @property
    def citation_share_pct(self) -> float | None:
        return _share(self.citations, self.usable_answers)


class CategoryShare(BaseModel):
    """One question shape's slice. "Invisible for cost questions" is actionable."""

    category: PromptCategory
    usable_answers: int
    mentions: int
    citations: int

    @property
    def mention_share_pct(self) -> float | None:
        return _share(self.mentions, self.usable_answers)


class ShareOfVoice(BaseModel):
    """The metric, with its own sample size attached.

    Every share is a derived property over stored integers, so a serialised
    `ShareOfVoice` physically cannot present "22%" without also presenting the 9,
    the 41, and the three models it came from.
    """

    set_version: str
    #: Hash over the ids actually probed. Two runs with equal fingerprints asked
    #: the same questions; unequal fingerprints are not comparable, whatever the
    #: version says.
    set_fingerprint: str
    #: Distinct prompts that produced at least one outcome.
    prompts_probed: int
    #: Every outcome recorded, usable or not.
    probes_total: int
    #: The denominator. `no_answer` is not in it.
    usable_answers: int
    no_answer_count: int
    mentions: int
    citations: int
    #: The same two counts restricted to prompts that did NOT name the brand.
    #: This is the number that actually says "we get found".
    unprompted_usable_answers: int
    unprompted_mentions: int
    unprompted_citations: int
    models: list[ModelShare] = Field(default_factory=list)
    competitors: list[CompetitorShare] = Field(default_factory=list)
    categories: list[CategoryShare] = Field(default_factory=list)

    @property
    def mention_share_pct(self) -> float | None:
        """Percent of usable answers mentioning the brand.

        `None` -- not `0.0` -- when nothing was usable. Zero would read as "the
        brand is never mentioned", which is a claim we did not measure.
        """
        return _share(self.mentions, self.usable_answers)

    @property
    def citation_share_pct(self) -> float | None:
        return _share(self.citations, self.usable_answers)

    @property
    def unprompted_mention_share_pct(self) -> float | None:
        """Mention rate over brand-free questions only. The honest headline."""
        return _share(self.unprompted_mentions, self.unprompted_usable_answers)

    @property
    def models_probed(self) -> int:
        return len(self.models)

    @property
    def headline(self) -> str:
        """A one-line rendering that cannot omit the denominator.

        Provided so the UI and the agent's summary share one wording, and so
        neither can accidentally print a bare percentage.
        """
        if self.usable_answers == 0:
            return (
                f"No usable answers ({self.no_answer_count} of {self.probes_total} probes "
                "returned nothing) — share of voice is unknown, not zero."
            )
        share = self.mention_share_pct
        assert share is not None  # usable_answers > 0
        excluded = f", {self.no_answer_count} excluded as no_answer" if self.no_answer_count else ""
        return (
            f"{share:.1f}% — mentioned in {self.mentions} of {self.usable_answers} answers "
            f"across {self.models_probed} model(s){excluded}"
        )


class SovDelta(BaseModel):
    """The run-over-run change, or an explanation of why there isn't one.

    `comparable` is the load-bearing field. A first run, a changed prompt set,
    or a previous run with no usable answers all produce `comparable=False` and a
    `note` saying which -- rather than a plausible-looking delta computed from
    incomparable samples.
    """

    current: ShareOfVoice
    previous: ShareOfVoice | None = None
    is_first_run: bool = False
    comparable: bool = False
    mention_share_delta_pp: float | None = None
    citation_share_delta_pp: float | None = None
    unprompted_mention_share_delta_pp: float | None = None
    mentions_delta: int | None = None
    citations_delta: int | None = None
    note: str = ""

    @property
    def direction(self) -> Literal["up", "down", "flat", "unknown"]:
        """Coarse direction for a UI arrow. `unknown` when not comparable."""
        if not self.comparable or self.mention_share_delta_pp is None:
            return "unknown"
        if self.mention_share_delta_pp > 0:
            return "up"
        if self.mention_share_delta_pp < 0:
            return "down"
        return "flat"


def _share(numerator: int, denominator: int) -> float | None:
    """Percent, or `None` when there is nothing to divide by.

    The one place division happens in this module. Returning `None` rather than
    `0.0` is deliberate: "we have no measurement" and "we measured zero" are
    different facts and a dashboard must be able to tell them apart.
    """
    if denominator <= 0:
        return None
    return round(numerator * 100 / denominator, 1)
