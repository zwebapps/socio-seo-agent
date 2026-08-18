"""Weighting and aggregation: nine findings in, one 0-100 score out.

The scoring model, stated in full because a gate nobody can predict is a gate
everybody works around:

1. Every rule owns a fixed **weight**, and the nine weights sum to 100. A page
   that passes everything scores exactly 100.
2. A finding's penalty is ``weight * severity multiplier``, where an `error`
   costs the full weight, a `warn` costs half, and an `info` (a pass) costs
   nothing.
3. ``score = 100 - Σ penalties``, rounded half-up and clamped to 0-100.
4. ``passed = score >= 85 and there is no error-severity finding``.

Two properties fall out of this that were the point of choosing it:

* **Severity is graded, so the score is graded.** A 45-character title is a near
  miss and costs 7.5, not 15. A missing title is fatal and costs 15. If the
  penalty ignored severity, "slightly short" and "absent" would score the same
  and the number would stop carrying information the writer can use.
* **No single warn can fail the gate, and no single error can pass it.** The
  heaviest warn is 6 points, comfortably inside the 15-point headroom above 85,
  so a page is never blocked by one nice-to-have. Conversely the error clause in
  `passed` is independent of the score, so a page cannot buy its way past a
  missing `<h1>` by being excellent elsewhere.

Why these weights. They are ordered by how much of the outcome each rule
controls, not by how easy it is to measure: title and heading structure are the
strongest on-page signals and are also the cheapest things for a writer to get
right, so they carry the most weight (15 each). Keyword presence and internal
linking are next (12 each) -- both are about whether the page can be found and
understood at all. Meta description, readability and structured data are worth
10 each: they change click-through, comprehension and rich-result eligibility
rather than whether the page ranks. External links and image alt text carry 8:
real quality signals, but the last ones a reader would notice. The weights are
a deliberate product judgement, not a measured coefficient, and they are in one
dict so that changing them is a one-line, reviewable decision.
"""

import math
from typing import Final

from backend.app.engines.seo.contract import (
    SeoFinding,
    SeoFindingCode,
    SeoScoreRequest,
    SeoScoreResult,
    SeoSeverity,
)
from backend.app.engines.seo.rules import (
    check_external_links,
    check_heading_tree,
    check_image_alt,
    check_internal_links,
    check_keyword_density,
    check_meta_length,
    check_readability,
    check_schema,
    check_title_length,
    extract_facts,
)

RULE_WEIGHTS: Final[dict[SeoFindingCode, int]] = {
    "title_length": 15,
    "heading_tree": 15,
    "keyword_density": 12,
    "internal_links": 12,
    "meta_length": 10,
    "readability": 10,
    "schema_invalid": 10,
    "external_links": 8,
    "image_alt": 8,
}

SEVERITY_PENALTY: Final[dict[SeoSeverity, float]] = {"error": 1.0, "warn": 0.5, "info": 0.0}

PASS_SCORE: Final = 85

# The order findings are returned in, and therefore the order the review screen
# renders and the order hints reach the model. Fixed rather than incidental: a
# stable order is part of what makes two runs comparable, and it puts the
# highest-leverage fixes first so a truncated hint list loses the least.
FINDING_ORDER: Final[tuple[SeoFindingCode, ...]] = (
    "title_length",
    "meta_length",
    "heading_tree",
    "keyword_density",
    "readability",
    "internal_links",
    "external_links",
    "image_alt",
    "schema_invalid",
)


def _round_half_up(value: float) -> int:
    """Round to the nearest integer, halves away from zero.

    `round()` is banker's rounding, so `round(92.5)` is 92. Deterministic, but a
    reviewer comparing 92.5 to a displayed 92 will report it as a bug -- and on a
    scale where every penalty is a multiple of 0.5, halves are the common case,
    not the edge case.
    """
    return math.floor(value + 0.5)


def penalty_for(finding: SeoFinding) -> float:
    """The points `finding` costs. Zero for a passing (`info`) rule."""
    return RULE_WEIGHTS[finding.code] * SEVERITY_PENALTY[finding.severity]


def aggregate(findings: list[SeoFinding]) -> tuple[int, bool]:
    """Fold findings into `(score, passed)`.

    Split out from `score_page` so the weighting can be tested against
    hand-built findings, without having to construct HTML that provokes a
    particular combination of failures.
    """
    total_penalty = sum(penalty_for(finding) for finding in findings)
    score = max(0, min(100, _round_half_up(100.0 - total_penalty)))
    has_error = any(finding.severity == "error" for finding in findings)
    return score, score >= PASS_SCORE and not has_error


def score_page(req: SeoScoreRequest) -> SeoScoreResult:
    """Score one page against all nine rules.

    Deterministic by construction: the HTML is parsed once, each rule is a pure
    function of those facts and the request, and nothing consults a clock, a
    network or a random source. The same request always yields an identical
    result -- which is the whole reason this gate is Python and not a model.
    """
    facts = extract_facts(req.html)

    by_code: dict[SeoFindingCode, SeoFinding] = {
        "title_length": check_title_length(facts),
        "meta_length": check_meta_length(facts),
        "heading_tree": check_heading_tree(facts),
        "keyword_density": check_keyword_density(facts, req.target_keyword, req.secondary_keywords),
        "readability": check_readability(facts, req.locale),
        "internal_links": check_internal_links(facts),
        "external_links": check_external_links(facts),
        "image_alt": check_image_alt(facts),
        "schema_invalid": check_schema(facts),
    }

    findings = [by_code[code] for code in FINDING_ORDER]
    score, passed = aggregate(findings)
    return SeoScoreResult(score=score, findings=findings, passed=passed)
