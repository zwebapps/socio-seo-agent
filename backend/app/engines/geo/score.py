"""Share-of-voice arithmetic and the run-over-run diff.

Two functions, and between them one idea: **the denominator is the product.**

`no_answer` outcomes -- refusals, timeouts, rate limits, an unparseable response
-- are counted, reported, and then excluded from every rate. Three of eight
probes mentioning the brand with two refusals is 3/6, not 3/8. Including the
refusals would let a provider outage read as the brand being absent, which is the
one failure that turns this metric from a measurement into a fabrication
(docs/ROADMAP.md section 9: "record as `no_answer`, exclude from the SoV
denominator, never count as absence").

Everything else here follows from refusing to hide a sample size:

* rates are `None`, never `0.0`, when nothing was usable -- "we did not measure"
  and "we measured zero" are different facts;
* the pooled number always ships with its per-model and per-category splits,
  because models disagree violently on the same question;
* prompts that name the brand are scored separately from prompts that do not,
  because a mention in "is {brand} any good?" is nearly free and says nothing
  about being found;
* the diff refuses to subtract two runs that asked different questions.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .contract import (
    CategoryShare,
    CompetitorShare,
    ModelShare,
    ProbeOutcome,
    PromptCategory,
    ShareOfVoice,
    SovDelta,
)
from .prompts import prompt_set_fingerprint

__all__ = ["diff_share_of_voice", "share_of_voice"]


def _usable(results: Iterable[ProbeOutcome]) -> list[ProbeOutcome]:
    """The outcomes that constitute a measurement. The denominator, once."""
    return [result for result in results if result.status == "answered"]


def share_of_voice(results: Sequence[ProbeOutcome]) -> ShareOfVoice:
    """Aggregate one run's probes into the metric, sample size attached.

    Raises `ValueError` if the outcomes span more than one `set_version`: two
    prompt-set versions are two different instruments, and their average
    describes neither. Pooling them is exactly how a "trend" gets manufactured
    out of a template edit.

    Ordering of every breakdown is first-seen order, not sorted and not set
    order, so the same run always renders the same way.
    """
    versions = {result.set_version for result in results}
    if len(versions) > 1:
        raise ValueError(
            "cannot pool probes with different set_version values "
            f"({sorted(versions)}): a share of voice computed across two "
            "instruments describes neither. Score each version separately."
        )

    usable = _usable(results)
    unprompted = [result for result in usable if not result.prompt_contains_brand]

    return ShareOfVoice(
        set_version=next(iter(versions), ""),
        set_fingerprint=prompt_set_fingerprint(result.prompt_id for result in results),
        prompts_probed=len({result.prompt_id for result in results}),
        probes_total=len(results),
        usable_answers=len(usable),
        no_answer_count=len(results) - len(usable),
        mentions=sum(1 for result in usable if result.mentioned),
        citations=sum(1 for result in usable if result.cited),
        unprompted_usable_answers=len(unprompted),
        unprompted_mentions=sum(1 for result in unprompted if result.mentioned),
        unprompted_citations=sum(1 for result in unprompted if result.cited),
        models=_model_shares(results),
        competitors=_competitor_shares(usable),
        categories=_category_shares(usable),
    )


def _model_shares(results: Sequence[ProbeOutcome]) -> list[ModelShare]:
    """One row per model, each with its own denominator.

    Built from *all* results, not just usable ones: a model that answered nothing
    must still appear, with `usable_answers=0` and its refusals counted. Dropping
    it would hide the outage that a reader needs in order to trust the rest of
    the table.
    """
    order: list[tuple[str, str]] = []
    buckets: dict[tuple[str, str], list[ProbeOutcome]] = {}
    for result in results:
        key = (result.provider, result.model)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(result)

    shares: list[ModelShare] = []
    for provider, model in order:
        bucket = buckets[provider, model]
        usable = _usable(bucket)
        shares.append(
            ModelShare(
                provider=provider,
                model=model,
                usable_answers=len(usable),
                no_answer_count=len(bucket) - len(usable),
                mentions=sum(1 for result in usable if result.mentioned),
                citations=sum(1 for result in usable if result.cited),
            )
        )
    return shares


def _competitor_shares(usable: Sequence[ProbeOutcome]) -> list[CompetitorShare]:
    """One row per competitor seen, on the brand's own denominator.

    The shared denominator is what makes the comparison fair: the same questions,
    the same models, the same usable answers. A competitor scored over "answers
    that mentioned somebody" would flatter whoever appears in fewer answers.
    """
    order: list[str] = []
    mentions: dict[str, int] = {}
    citations: dict[str, int] = {}

    for result in usable:
        for name in result.competitors_mentioned:
            if name not in mentions:
                mentions[name] = 0
                citations.setdefault(name, 0)
                order.append(name)
            mentions[name] += 1
        for name in result.competitors_cited:
            if name not in citations:
                mentions.setdefault(name, 0)
                citations[name] = 0
                order.append(name)
            citations[name] += 1

    return [
        CompetitorShare(
            name=name,
            mentions=mentions.get(name, 0),
            citations=citations.get(name, 0),
            usable_answers=len(usable),
        )
        for name in order
    ]


def _category_shares(usable: Sequence[ProbeOutcome]) -> list[CategoryShare]:
    """One row per question shape. "Invisible on cost questions" is actionable."""
    order: list[PromptCategory] = []
    buckets: dict[PromptCategory, list[ProbeOutcome]] = {}
    for result in usable:
        if result.category not in buckets:
            buckets[result.category] = []
            order.append(result.category)
        buckets[result.category].append(result)

    return [
        CategoryShare(
            category=category,
            usable_answers=len(buckets[category]),
            mentions=sum(1 for result in buckets[category] if result.mentioned),
            citations=sum(1 for result in buckets[category] if result.cited),
        )
        for category in order
    ]


def diff_share_of_voice(previous: ShareOfVoice | None, current: ShareOfVoice) -> SovDelta:
    """Compare two runs, or explain why they cannot be compared.

    `previous` accepts `None` because the first run is the common case, not an
    error: a business has no baseline until it has been probed once, and returning
    a delta of zero for it would be a claim that nothing changed.

    Three conditions block a delta, each with its own note:

    * no previous run;
    * a different prompt-set version or fingerprint -- the questions changed, so
      any subtraction measures the edit rather than the business;
    * a previous run with no usable answers -- there is no baseline to move from.

    Deltas are in **percentage points**, not percent-of-percent. "7.5% to 22.0%"
    is +14.5 pp; calling it "+193%" is technically defensible and reliably
    misread, so the field name says `pp` and the arithmetic matches it.
    """
    if previous is None:
        return SovDelta(
            current=current,
            previous=None,
            is_first_run=True,
            comparable=False,
            note=(
                "First run: there is no previous measurement to compare against. "
                "This is a baseline, not a movement."
            ),
        )

    if previous.set_version != current.set_version:
        return SovDelta(
            current=current,
            previous=previous,
            comparable=False,
            note=(
                f"Not comparable: prompt-set version changed from "
                f"{previous.set_version!r} to {current.set_version!r}, so the two runs "
                "asked different questions."
            ),
        )

    if previous.set_fingerprint != current.set_fingerprint:
        return SovDelta(
            current=current,
            previous=previous,
            comparable=False,
            note=(
                "Not comparable: the questions changed between runs (services or "
                "competitors were edited), so a difference would measure the edit "
                "rather than the business."
            ),
        )

    if previous.usable_answers == 0:
        return SovDelta(
            current=current,
            previous=previous,
            comparable=False,
            note=(
                "Not comparable: the previous run produced no usable answers "
                f"({previous.no_answer_count} of {previous.probes_total} probes returned "
                "nothing), so there is no baseline to move from."
            ),
        )

    return SovDelta(
        current=current,
        previous=previous,
        comparable=True,
        mention_share_delta_pp=_pp(previous.mention_share_pct, current.mention_share_pct),
        citation_share_delta_pp=_pp(previous.citation_share_pct, current.citation_share_pct),
        unprompted_mention_share_delta_pp=_pp(
            previous.unprompted_mention_share_pct, current.unprompted_mention_share_pct
        ),
        mentions_delta=current.mentions - previous.mentions,
        citations_delta=current.citations - previous.citations,
        note=(
            f"Comparable: same prompt set ({current.prompts_probed} questions, "
            f"fingerprint {current.set_fingerprint}). Previous sample "
            f"{previous.usable_answers} usable answers, current {current.usable_answers}."
        ),
    )


def _pp(before: float | None, after: float | None) -> float | None:
    """Percentage-point difference, or `None` if either side was never measured."""
    if before is None or after is None:
        return None
    return round(after - before, 1)
