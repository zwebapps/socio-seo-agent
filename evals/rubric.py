"""Deterministic scoring for the eval harness. **No model is used as a judge.**

That is the load-bearing decision, and it follows the project's own rule -- *if the
answer is computable, compute it* (`CLAUDE.md`, `docs/ARCHITECTURE.md` §14). An
LLM-as-judge would be non-deterministic, billable, and unfalsifiable exactly where
the report has to be defensible: a grader asking "why did this case score 0.62?"
must get an arithmetic answer, not "the judge thought so". Every number here is
reproducible from the inputs, offline, in milliseconds.

Five dimensions, and each one exists because a different kind of failure needs a
different kind of consequence:

======================  ==========================================  ==============
Dimension               Question                                    Failure shape
======================  ==========================================  ==============
``seo``                 does the page pass the shipped on-page gate  graded
``brand``               did it make a claim it is not allowed to     **fatal**
``format``              will the channel actually accept it          fatal / graded
``grounding``           is every figure traceable to a real source   **fatal** if faked
``coverage``            does it say what the case required           graded
======================  ==========================================  ==============

**Fatal is not a synonym for zero.** It means *unpublishable at any quality*: a
regulated claim, a platform that will reject the post, or a citation to a source
that does not exist. A weak SEO score is a retry; a fabricated citation is not.
Keeping those two apart is what stops an averaged report from hiding the one
result that actually matters.

**What this module deliberately does not do**, stated here because the report
repeats it and the limitations must not live only in prose: it does not judge
whether copy is *good*, whether a claim is *semantically* entailed by a source
(only whether its figures appear in one), whether German grammar is correct, or
whether a page will rank. Those are the honest gaps, and a deterministic rubric
that pretended otherwise would be worse than none.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from backend.app.engines.seo import SeoScoreRequest, score_page

# --------------------------------------------------------------------------- #
# Result shapes
# --------------------------------------------------------------------------- #

type Dimension = Literal["seo", "brand", "format", "grounding", "coverage"]

#: Report column order. Fixed so two runs are comparable line by line.
DIMENSIONS: Final[tuple[Dimension, ...]] = ("seo", "brand", "format", "grounding", "coverage")


@dataclass(frozen=True, slots=True)
class RubricResult:
    """One dimension's verdict on one candidate output.

    ``score`` is normalised to 0.0-1.0 on every dimension so the aggregate is a
    mean of comparable numbers rather than of mixed scales.

    ``detail`` is written to be pasted into a report row: it names the measured
    value and the target, in the same spirit as ``SeoFinding.fix_hint``. A score
    with no explanation is unreviewable, and an unreviewable eval is decoration.
    """

    dimension: Dimension
    score: float
    passed: bool
    detail: str
    violations: tuple[str, ...] = ()
    #: Unpublishable regardless of score: a banned claim, a platform hard limit, or
    #: a fabricated citation. Reported separately so an average can never bury it.
    fatal: bool = False
    #: False when there was nothing for this dimension to check -- no banned claims
    #: configured, no required terms, no figures to trace. The score is still 1.0
    #: because nothing was violated, but it is the ABSENCE of a test rather than a
    #: pass, and a report that renders the two identically is lying by table layout.
    exercised: bool = True


@dataclass(frozen=True, slots=True)
class RubricSummary:
    """The aggregate over many results."""

    count: int
    mean_score: float
    passed: int
    failed: int
    #: Mean score per dimension, for the report's aggregate table.
    by_dimension: Mapping[str, float] = field(default_factory=dict)
    fatal_violations: tuple[str, ...] = ()
    #: Results that scored 1.0 only because there was nothing to check. Counted so a
    #: high mean can be read against how much of it was actually earned.
    not_exercised: int = 0


# --------------------------------------------------------------------------- #
# Channel limits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChannelLimits:
    """What one channel will accept.

    Mirrors the ``ChannelSpec`` sketch in `docs/CHANNELS.md` §4, reduced to the
    fields a deterministic length/hashtag/link check needs.

    ``max_chars`` is the editorial target; ``hard_max_chars`` is the platform's own
    reject threshold. Two numbers, not one, because "longer than we would like" and
    "the API will refuse this" deserve different consequences.
    """

    max_chars: int
    hard_max_chars: int
    hashtags_min: int
    hashtags_max: int
    #: False where a URL in the body is not clickable (Instagram, TikTok). A link
    #: there is not untidy, it is a dead CTA and lost attribution.
    link_in_body: bool
    min_chars: int = 0


#: **Starting values, to be verified against provider documentation** -- exactly
#: as `docs/CHANNELS.md` §4 instructs ("treat any number here as a default to
#: check, never as truth").
#:
#: These live in code *only because the `channel_specs` config table does not
#: exist yet* (it lands with the renderers in Phase 6). When it does, this table
#: must be deleted and the rubric must read from it: two copies of a platform
#: limit is how the eval starts disagreeing with the product it is grading.
CHANNEL_LIMITS: Final[Mapping[str, ChannelLimits]] = {
    # Long-form article. The minimum is the real gate here: a 300-word "article"
    # cannot cover a commercial-intent query, whatever it scores on-page.
    "blog_article": ChannelLimits(
        max_chars=40_000,
        hard_max_chars=200_000,
        hashtags_min=0,
        hashtags_max=0,
        link_in_body=True,
        min_chars=2_500,
    ),
    # 1,300-1,700 chars, 3 hashtags max (docs/CHANNELS.md section 6).
    "linkedin": ChannelLimits(
        max_chars=1_700,
        hard_max_chars=3_000,
        hashtags_min=0,
        hashtags_max=3,
        link_in_body=True,
    ),
    # ~2,200 char ceiling, 3-5 hashtags, and never a caption URL.
    "instagram_caption": ChannelLimits(
        max_chars=2_200,
        hard_max_chars=2_200,
        hashtags_min=3,
        hashtags_max=5,
        link_in_body=False,
    ),
    # Short post (~80-150 words); the link preview does the work.
    "facebook_post": ChannelLimits(
        max_chars=1_000,
        hard_max_chars=63_206,
        hashtags_min=0,
        hashtags_max=3,
        link_in_body=True,
    ),
    # Email body. Subject and preheader are separate deliverables (Phase 8).
    "email": ChannelLimits(
        max_chars=2_500,
        hard_max_chars=100_000,
        hashtags_min=0,
        hashtags_max=0,
        link_in_body=True,
        min_chars=300,
    ),
}


@dataclass(frozen=True, slots=True)
class Rendering:
    """One channel-ready output.

    ``hashtags=None`` means "parse them out of the text", which is what a single
    blob of generated copy needs. An explicit tuple -- including an empty one --
    is believed, because some renderers carry hashtags in their own field
    (`docs/CHANNELS.md` §6 does this for on-screen text and tags) and parsing the
    body would then find none and wrongly report a miss.
    """

    text: str
    hashtags: tuple[str, ...] | None = None


# --------------------------------------------------------------------------- #
# Text helpers -- pure, and shared so the report and the scorers agree
# --------------------------------------------------------------------------- #

_URL_RE: Final = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HASHTAG_RE: Final = re.compile(r"#\w+", re.UNICODE)
#: A citation marker, e.g. ``[chunk:plumber-01#0]``. Excluded from the hashtag
#: count for the same reason URLs are: the ``#`` inside it is part of an
#: identifier, not a tag. Getting this wrong was not cosmetic -- chunk ids are
#: ``<case_id>#<ordinal>``, so every citation counted as a hashtag, and since only
#: the RAG arm cites anything, the rubric was penalising that arm *for doing the
#: thing RAG is for*. The 2026-08-19 live run read `format` 0.35 against 0.95 on
#: that basis, and every single one of its reported hashtag violations
#: ("6 hashtags exceed the maximum of 0 for blog_article") was citations.
_CITATION_RE: Final = re.compile(r"\[chunk:[^\]\s]+\]", re.IGNORECASE)
_SENTENCE_SPLIT_RE: Final = re.compile(r"(?<=[.!?])\s+|\n+")
_NUMBER_RE: Final = re.compile(r"\d+(?:[.,]\d+)*")
_THOUSANDS_RE: Final = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$")


def extract_urls(text: str) -> tuple[str, ...]:
    """Every URL in the text. Used for the link-mechanism check."""
    return tuple(_URL_RE.findall(text))


def extract_hashtags(text: str) -> tuple[str, ...]:
    """Every hashtag, with URLs and citation markers removed first.

    Without stripping URLs, a fragment identifier (``.../a#anchor``) counts as a
    hashtag and a perfectly compliant post fails the cap.
    """
    return tuple(_HASHTAG_RE.findall(_CITATION_RE.sub(" ", _URL_RE.sub(" ", text))))


def normalise_number(token: str) -> str:
    """Canonicalise one numeric token so 1.500 / 1,500 compare equal.

    Thousands grouping is stripped; a lone separator is treated as a decimal point.
    This is deliberately narrow: it makes German and English figure formatting
    comparable without pretending to parse units or currency.
    """
    if _THOUSANDS_RE.match(token):
        return token.replace(".", "").replace(",", "")
    return token.replace(",", ".")


def numeric_tokens(text: str) -> frozenset[str]:
    """The normalised figures asserted in a piece of text."""
    return frozenset(normalise_number(match) for match in _NUMBER_RE.findall(text))


def claim_sentences(text: str) -> tuple[str, ...]:
    """Sentences that assert something checkable.

    "Checkable" is defined narrowly and on purpose: a sentence containing a figure.
    Those are the claims a model fabricates -- prices, response times, years in
    business, counts -- and they are the only ones a rubric with no model can
    verify. A sentence with no figure ("we work carefully") is not scored, and the
    report says as much rather than counting it as grounded.
    """
    sentences = (part.strip() for part in _SENTENCE_SPLIT_RE.split(text))
    return tuple(part for part in sentences if part and _NUMBER_RE.search(part))


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """A word-boundary, whitespace-tolerant, case-insensitive phrase matcher.

    Boundaries matter: a bare substring search for "frei" flags
    "Schmerzfreiheitsgarantie", and a gate that cries wolf is a gate that gets
    switched off. Internal whitespace becomes ``\\s+`` so a phrase still matches
    across the line break a renderer inserted.
    """
    words = [re.escape(word) for word in phrase.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# The scorers
# --------------------------------------------------------------------------- #


def score_seo(html: str, keyword: str, locale: str) -> RubricResult:
    """Delegate to the shipped `seo` engine and normalise its 0-100 to 0-1.

    Delegation, not reimplementation. The engine is the gate the product itself
    applies to every draft (`docs/AGENT_RUNTIME.md` §7); if the rubric scored
    on-page quality its own way, the report would be a second opinion about a gate
    nobody ships, and the two would drift apart on the first rule change.

    Claims discipline: this measures a deterministic on-page audit. It does **not**
    predict rankings (`docs/CRITERIA_MAP.md` §7).
    """
    engine = score_page(SeoScoreRequest(html=html, target_keyword=keyword, locale=locale))
    violations = tuple(
        f"{finding.code} ({finding.severity}): {finding.message}"
        for finding in engine.findings
        if finding.severity != "info"
    )
    return RubricResult(
        dimension="seo",
        score=engine.score / 100,
        passed=engine.passed,
        detail=(
            f"seo engine scored {engine.score}/100 "
            f"({len(engine.errors)} error, {len(violations) - len(engine.errors)} warn)"
        ),
        violations=violations,
    )


def score_brand(text: str, banned_claims: Sequence[str]) -> RubricResult:
    """Binary: does the copy make a claim the business is not allowed to make.

    Binary rather than graded because these are compliance rules, not preferences.
    A German dentist may not promise a treatment outcome (HWG), and "only one
    forbidden promise" is not a partial success -- the piece cannot be published
    either way. So any hit is a zero *and* fatal.
    """
    if not banned_claims:
        return RubricResult(
            dimension="brand",
            score=1.0,
            passed=True,
            detail="no banned claims configured for this business, so nothing was checked",
            exercised=False,
        )

    hits = tuple(
        f"banned claim present: {phrase!r}"
        for phrase in banned_claims
        if _phrase_pattern(phrase).search(text)
    )
    if hits:
        return RubricResult(
            dimension="brand",
            score=0.0,
            passed=False,
            detail=f"{len(hits)} of {len(banned_claims)} banned claim(s) present",
            violations=hits,
            fatal=True,
        )

    return RubricResult(
        dimension="brand",
        score=1.0,
        passed=True,
        detail=f"none of {len(banned_claims)} banned claim(s) present",
    )


#: Deduction per soft (editorial) format miss. Four of them reach zero, which is
#: the intent: individually survivable, collectively a rewrite.
SOFT_FORMAT_PENALTY: Final = 0.25

#: A rendering still passes with one soft miss and fails with two.
FORMAT_PASS_SCORE: Final = 0.75

#: Below this fraction of a channel's minimum length, being short stops being an
#: editorial miss and becomes a hard failure: the deliverable is missing, not thin.
SEVERELY_SHORT_RATIO: Final = 0.5


def score_format(rendering: Rendering, channel: str) -> RubricResult:
    """Length, hashtag count and link mechanism, against the channel's limits.

    Raises ``KeyError`` for an unknown channel. Deliberate: scoring 1.0 for a
    channel with no spec would let a harness bug read as a perfect result, and the
    dataset has a test asserting every case names a known channel.

    Hard violations (the platform's own reject threshold, the hashtag cap, a URL
    where links do not work, or a deliverable less than half its minimum length)
    are fatal and floor the score, because the post simply will not do its job.
    Soft violations (over the editorial target, under the hashtag minimum, mildly
    short) are deductions.

    The length rule is graded in both directions for the same reason the seo
    engine grades severity: "a bit long" and "the API will refuse this" carry the
    same information only if you never intend to act on the number.
    """
    limits = CHANNEL_LIMITS[channel]
    text = rendering.text
    hashtags = rendering.hashtags if rendering.hashtags is not None else extract_hashtags(text)

    hard: list[str] = []
    soft: list[str] = []

    length = len(text)
    if length > limits.hard_max_chars:
        hard.append(
            f"length {length} exceeds the platform hard limit of {limits.hard_max_chars} chars"
        )
    elif length > limits.max_chars:
        soft.append(f"length {length} is over the {limits.max_chars}-char editorial target")

    if length < limits.min_chars * SEVERELY_SHORT_RATIO:
        # Not an editorial nudge. A "long-form article" at a fraction of the
        # minimum is not a short article, it is not an article -- there is no
        # amount of on-page polish that makes 8 characters cover a commercial
        # query, so it is treated the way a rejected post is.
        hard.append(
            f"too short to be a {channel}: {length} chars against a {limits.min_chars}-char minimum"
        )
    elif length < limits.min_chars:
        soft.append(f"too short: {length} chars against a {limits.min_chars}-char minimum")

    if len(hashtags) > limits.hashtags_max:
        hard.append(
            f"{len(hashtags)} hashtags exceed the maximum of {limits.hashtags_max} for {channel}"
        )
    elif len(hashtags) < limits.hashtags_min:
        soft.append(
            f"{len(hashtags)} hashtags are under the minimum of {limits.hashtags_min} for {channel}"
        )

    urls = extract_urls(text)
    if urls and not limits.link_in_body:
        hard.append(
            f"{len(urls)} link(s) in the body, but {channel} carries no clickable link -- "
            "the CTA is dead and attribution is lost"
        )

    violations = tuple(hard + soft)
    if hard:
        return RubricResult(
            dimension="format",
            score=0.0,
            passed=False,
            detail=f"{channel}: {len(hard)} hard violation(s) -- the channel would reject this",
            violations=violations,
            fatal=True,
        )

    score = max(0.0, 1.0 - SOFT_FORMAT_PENALTY * len(soft))
    return RubricResult(
        dimension="format",
        score=score,
        passed=score >= FORMAT_PASS_SCORE,
        detail=(
            f"{channel}: {length} chars, {len(hashtags)} hashtags, {len(soft)} editorial miss(es)"
        ),
        violations=violations,
    )


def score_grounding(
    text: str,
    cited_chunk_ids: Sequence[str],
    available_chunks: Mapping[str, str],
) -> RubricResult:
    """Is every checkable claim traceable to a chunk that was really retrieved.

    Two failures, and they are **not** the same size:

    * **A citation to a chunk that was never retrieved is a fabricated source.**
      It scores exactly ``0.0`` and is fatal, whatever else the text got right. A
      merely-low score would let one invented citation average away against nine
      good cases -- and an invented source is the single worst thing a RAG system
      can produce, because it is indistinguishable from a real one to the reader.
    * **A claim whose figures are absent from the cited chunks is unsupported.**
      That is a graded fraction and not fatal: the generator's own instruction is
      to drop an unsupported claim, so this is a fixable draft.

    "Checkable" means "contains a figure" -- see :func:`claim_sentences`. Text with
    no figures scores 1.0 *and says the dimension was not exercised*, which is the
    honest reading: nothing was asserted, so nothing could be fabricated.
    """
    fabricated = tuple(
        f"citation to chunk {chunk_id!r}, which was never retrieved (fabricated source)"
        for chunk_id in cited_chunk_ids
        if chunk_id not in available_chunks
    )
    if fabricated:
        return RubricResult(
            dimension="grounding",
            score=0.0,
            passed=False,
            detail=(
                f"{len(fabricated)} of {len(cited_chunk_ids)} citation(s) point at a chunk "
                "that was never retrieved"
            ),
            violations=fabricated,
            fatal=True,
        )

    claims = claim_sentences(text)
    if not claims:
        return RubricResult(
            dimension="grounding",
            score=1.0,
            passed=True,
            detail=(
                "no checkable claims found (no figures asserted), so grounding was not "
                "exercised -- this 1.00 is an absence of risk, not evidence of grounding"
            ),
            exercised=False,
        )

    cited_text = " ".join(available_chunks[chunk_id] for chunk_id in cited_chunk_ids)
    supported_figures = numeric_tokens(cited_text)

    unsupported: list[str] = []
    for claim in claims:
        missing = numeric_tokens(claim) - supported_figures
        if missing:
            unsupported.append(f"unsupported figure(s) {sorted(missing)} in: {claim[:90]!r}")

    supported = len(claims) - len(unsupported)
    score = supported / len(claims)
    return RubricResult(
        dimension="grounding",
        score=score,
        passed=score >= 1.0,
        detail=(
            f"{supported}/{len(claims)} checkable claim(s) traceable to "
            f"{len(cited_chunk_ids)} cited chunk(s)"
        ),
        violations=tuple(unsupported),
    )


def score_coverage(text: str, required_terms: Sequence[str]) -> RubricResult:
    """Did the output actually say what the case required.

    The counterpart to ``banned_claims``: a case states both what a correct output
    must contain and what it must not, and scoring only the second half would pass
    an empty page. Graded, not binary -- a piece missing one of four required
    points is a weaker draft, not an illegal one.
    """
    if not required_terms:
        return RubricResult(
            dimension="coverage",
            score=1.0,
            passed=True,
            detail="no required terms for this case, so nothing was checked",
            exercised=False,
        )

    missing = tuple(
        f"required term absent: {term!r}"
        for term in required_terms
        if not _phrase_pattern(term).search(text)
    )
    present = len(required_terms) - len(missing)
    score = present / len(required_terms)
    return RubricResult(
        dimension="coverage",
        score=score,
        passed=not missing,
        detail=f"{present}/{len(required_terms)} required term(s) present",
        violations=missing,
    )


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def aggregate(results: Iterable[RubricResult]) -> RubricSummary:
    """Fold many results into one summary.

    An empty input returns zeros rather than raising: a run that produced no
    results is exactly when a report is most wanted, and a ``ZeroDivisionError``
    in the reporter turns "nothing to show" into "nothing at all".

    Fatal violations are carried up verbatim. A mean is a summary; a fabricated
    citation is a finding, and summarising it away would defeat the point of
    scoring it zero.
    """
    collected = list(results)
    if not collected:
        return RubricSummary(count=0, mean_score=0.0, passed=0, failed=0, by_dimension={})

    per_dimension: dict[str, list[float]] = {}
    for result in collected:
        per_dimension.setdefault(result.dimension, []).append(result.score)

    return RubricSummary(
        count=len(collected),
        mean_score=sum(result.score for result in collected) / len(collected),
        passed=sum(1 for result in collected if result.passed),
        failed=sum(1 for result in collected if not result.passed),
        by_dimension={
            dimension: sum(scores) / len(scores) for dimension, scores in per_dimension.items()
        },
        fatal_violations=tuple(
            violation for result in collected if result.fatal for violation in result.violations
        ),
        not_exercised=sum(1 for result in collected if not result.exercised),
    )
