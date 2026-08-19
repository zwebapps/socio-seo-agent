"""Typed contract for the `landing` engine.

These shapes cross three boundaries, so they are a published contract rather than
an internal detail:

* the `CONVERT` node writes :class:`LandingPageSpec` into
  ``AgentState["landing_page"]``, which is checkpointed to a JSONB column;
* `VALIDATE` writes :class:`LandingCheckResult` into
  ``AgentState["landing_report"]`` and :attr:`LandingFinding.fix_hint` is fed back
  into `CONVERT` **verbatim** on a retry, exactly as `SeoFinding.fix_hint` is
  (docs/AGENT_RUNTIME.md section 7);
* the served page is rendered from the stored spec, so a spec persisted today has
  to be renderable by tomorrow's template.

Every field is primitive for the same reason the seo contract's are: the result
crosses a process boundary as JSON, so nothing here may carry behaviour.

Two shapes are deliberately CLOSED, and both closures are load-bearing.

:data:`FormFieldName` is closed to the four fields the public lead endpoint
accepts. A generated form offering a `budget` field would render, submit, and be
refused by ``LeadSubmission`` (``extra="forbid"``) — so the visitor would fill it
in, press send, and the lead would be lost. That coupling is asserted by
``backend/tests/engines/test_landing.py``, which imports both sides.

:attr:`ProofPoint.source` is required and must be non-empty. A proof point is the
one part of a landing page that makes a factual assertion about the business, and
an unsourced one is an invented claim — which is the single thing this product
must never produce. The engine cannot verify that a source is TRUE; it can and
does refuse a claim that names none.
"""

from typing import Literal

from pydantic import BaseModel, Field

# One code per rule. Closed on purpose: the review screen renders a row per code
# and the retry loop keys hints by code, so a new rule is a deliberate contract
# change rather than a new free-text string appearing in the UI.
type LandingFindingCode = Literal[
    "headline",
    "subhead",
    "offer",
    "proof_points",
    "proof_sources",
    "form_fields",
    "reachability",
    "primary_cta",
    "consent",
    "channel_ctas",
]

# "info" is a PASSING rule, carrying zero penalty and an empty `fix_hint`, so it
# can never reach the model. Keeping passes in the list is what lets the review
# screen show a full checklist instead of only the problems.
type LandingSeverity = Literal["error", "warn", "info"]

#: The only field names a generated form may use. See the module docstring: this
#: is the set ``backend.app.api.leads.LeadSubmission`` accepts, and anything else
#: would be refused after the visitor had already typed it in.
type FormFieldName = Literal["name", "email", "phone", "message"]


class ProofPoint(BaseModel):
    """One reason to believe the offer, and where it came from.

    `source` is what the business's own material called it -- a document title, a
    page URL, a DNA field name. It is rendered on the page next to the claim, so a
    reader (and a reviewer) can check it.
    """

    text: str
    source: str


class FormField(BaseModel):
    """One input on the generated form.

    `name` is constrained to :data:`FormFieldName`, which is what makes the
    generated form and the endpoint that receives it the same shape.
    """

    name: FormFieldName
    label: str
    required: bool = False


class ChannelCta(BaseModel):
    """The ask, written for one channel.

    `channel` is a string rather than an enum here because the channel vocabulary
    lives in ``services.link_service.KNOWN_CHANNELS``, and an engine may not import
    a service (``tests/test_engine_boundary.py``). The known set therefore arrives
    as an ARGUMENT to :func:`~backend.app.engines.landing.checks.check_landing_page`
    -- the same decision, for the same reason, that the `channel` engine documents
    for its length limits.
    """

    channel: str
    text: str


class LandingPageSpec(BaseModel):
    """Everything needed to render one landing page, and nothing about where it lives.

    No URL, no id, no short-link code: those are minted by the service layer after
    the model has done its part, so this object stays a pure description of the
    page and can be re-rendered, re-checked and diffed without a database.
    """

    headline: str
    subhead: str = ""
    #: What the visitor gets in exchange for their details. The reason to act.
    offer: str
    proof_points: list[ProofPoint] = Field(default_factory=list)
    form_fields: list[FormField] = Field(default_factory=list)
    #: The button label. Exactly one, because a page with two primary CTAs has none.
    primary_cta: str
    #: The consent sentence shown beside the checkbox. Required: the form stores
    #: contact details, and storing them with no evidence of consent is the
    #: compliance problem this product would otherwise hand to every customer.
    consent_text: str
    ctas: list[ChannelCta] = Field(default_factory=list)

    def claim_text(self) -> str:
        """Every human-readable string on the page, one per line.

        This is what the regulated-claim gate checks. It exists here rather than in
        the caller so that a field added to this model is checked for banned claims
        by construction instead of by whoever remembers -- a landing page is the
        most claim-dangerous artifact in the product, and "we forgot to include the
        new field" is exactly how a forbidden promise gets published.
        """
        lines = [self.headline, self.subhead, self.offer, self.primary_cta, self.consent_text]
        lines.extend(point.text for point in self.proof_points)
        lines.extend(field.label for field in self.form_fields)
        lines.extend(cta.text for cta in self.ctas)
        return "\n".join(line for line in lines if line)


class LandingFinding(BaseModel):
    """One rule's verdict.

    `message` is written for a human reading the review screen. `fix_hint` is
    written for the model and must be quantitative -- it names the measured value
    and the target, because the retry loop is only as good as these hints. A
    passing rule carries ``fix_hint=""``.
    """

    code: LandingFindingCode
    severity: LandingSeverity
    message: str
    fix_hint: str
    measured: float | None
    expected: str


class LandingCheckResult(BaseModel):
    """The deterministic conversion verdict for one landing page.

    `passed` is stored rather than computed on read, for the same reason
    `SeoScoreResult.passed` is: a later change to the threshold must not silently
    re-judge a historical run.
    """

    score: int
    findings: list[LandingFinding] = Field(default_factory=list)
    passed: bool

    @property
    def fix_hints(self) -> list[str]:
        """The hints to feed back to `CONVERT`, in rule order. Passes excluded."""
        return [f.fix_hint for f in self.findings if f.severity != "info" and f.fix_hint]

    @property
    def errors(self) -> list[LandingFinding]:
        """Error-severity findings -- any one of these blocks `passed`.

        These are also what the service refuses to publish on: a page with no form,
        no CTA or no consent line is not a landing page, and serving it would break
        the conversion link the whole product is judged on.
        """
        return [f for f in self.findings if f.severity == "error"]
