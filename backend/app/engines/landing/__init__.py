"""The `landing` engine: everything about a landing page that is computable.

CONVERSION is link three of the lead chain in docs/FEATURES.md section 0
(REACH → RELEVANCE → **CONVERSION** → ATTRIBUTION → COMPOUNDING), and it is the
link competitors skip. A tracked short link pointing at a page that does not exist
earns nothing, so this engine is what the click lands on.

The split between this engine and the `CONVERT` node is the project's one rule
applied literally -- *if the answer is computable, compute it; only ask a model to
decide, interpret, or write*:

* **The model writes** the headline, the sub-headline, the offer, the proof-point
  wording and the per-channel CTA copy. Those are judgements about a specific
  business and cannot be computed.
* **This engine decides** whether the result can actually convert
  (:func:`check_landing_page`, ten deterministic rules with quantitative fix hints)
  and turns it into markup (:func:`render_landing_page`, a total function of the
  spec). Neither is a model's job: "is there a form, and can it be answered" is set
  membership, and "what HTML does this spec become" is string building.

    from backend.app.engines.landing import (
        LandingCheckRequest, check_landing_page, render_landing_page,
    )

    verdict = check_landing_page(LandingCheckRequest(spec=spec, known_channels=[...]))
    if verdict.passed:
        html = render_landing_page(spec, business_name=..., form_action=...)

No I/O, no model, no database -- `tests/test_engine_boundary.py` enforces the
imports, which is also why the channel vocabulary and the form action arrive as
arguments rather than being read from the service layer.
"""

from backend.app.engines.landing.checks import (
    FINDING_ORDER,
    HEADLINE_MAX_CHARS,
    HEADLINE_MIN_CHARS,
    MAX_CTA_CHARS,
    MAX_FORM_FIELDS,
    MAX_PRIMARY_CTA_CHARS,
    MIN_PROOF_POINTS,
    OFFER_MIN_CHARS,
    PASS_SCORE,
    REACHABLE_FIELDS,
    RECOMMENDED_FORM_FIELDS,
    RULE_WEIGHTS,
    SEVERITY_PENALTY,
    LandingCheckRequest,
    aggregate,
    check_landing_page,
    penalty_for,
)
from backend.app.engines.landing.contract import (
    ChannelCta,
    FormField,
    FormFieldName,
    LandingCheckResult,
    LandingFinding,
    LandingFindingCode,
    LandingPageSpec,
    LandingSeverity,
    ProofPoint,
)
from backend.app.engines.landing.render import (
    HONEYPOT_FIELD,
    MAX_UTM_VALUE_CHARS,
    PageState,
    RenderRefusedError,
    render_landing_markdown,
    render_landing_page,
)

__all__ = [
    "FINDING_ORDER",
    "HEADLINE_MAX_CHARS",
    "HEADLINE_MIN_CHARS",
    "HONEYPOT_FIELD",
    "MAX_CTA_CHARS",
    "MAX_FORM_FIELDS",
    "MAX_PRIMARY_CTA_CHARS",
    "MAX_UTM_VALUE_CHARS",
    "MIN_PROOF_POINTS",
    "OFFER_MIN_CHARS",
    "PASS_SCORE",
    "REACHABLE_FIELDS",
    "RECOMMENDED_FORM_FIELDS",
    "RULE_WEIGHTS",
    "SEVERITY_PENALTY",
    "ChannelCta",
    "FormField",
    "FormFieldName",
    "LandingCheckRequest",
    "LandingCheckResult",
    "LandingFinding",
    "LandingFindingCode",
    "LandingPageSpec",
    "LandingSeverity",
    "PageState",
    "ProofPoint",
    "RenderRefusedError",
    "aggregate",
    "check_landing_page",
    "penalty_for",
    "render_landing_markdown",
    "render_landing_page",
]
