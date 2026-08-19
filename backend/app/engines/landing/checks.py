"""The deterministic conversion audit: one spec in, one 0-100 score out.

This is feature L1/C1 of docs/FEATURES.md ("is there a CTA, a form, a reason to
act?") applied to a page we are about to publish rather than to one we crawled.
Every rule here is arithmetic or set membership over the spec, so none of it is a
model's job -- the project rule is *if the answer is computable, compute it*.

The scoring model, stated in full because a gate nobody can predict is a gate
everybody works around. It is deliberately the same model the `seo` engine uses,
so a reader who understands one understands both:

1. Every rule owns a fixed **weight**, and the ten weights sum to 100. A page that
   passes everything scores exactly 100.
2. A finding's penalty is ``weight * severity multiplier``: an `error` costs the
   full weight, a `warn` half, an `info` (a pass) nothing.
3. ``score = 100 - Σ penalties``, rounded half-up and clamped to 0-100.
4. ``passed = score >= 85 and there is no error-severity finding``.

Why these weights. They are ordered by how much of the CONVERSION each rule
controls. The offer, the ask and the capture surface carry 15 each: remove any one
of the three and the page cannot produce a lead at all, whatever else it does.
Proof sourcing carries 12 alongside the headline, because an unsourced claim is
the one failure this product cannot ship -- and it is an `error`, so it fails the
gate regardless of the score. Channel CTAs carry 10: without one, nothing points
at the page and the traffic is zero. Consent carries 8, reachability 5 and the
sub-headline 2 -- all three are binary and cheap to get right, and the first two
are `error`s, so their real force comes from the error clause rather than from
their weight.

Four rules are `error`-severity and they are the ones the service refuses to
publish on: no form, no primary CTA, no consent line, no reachable field, an
unsourced proof point, or a channel CTA naming a channel the link builder cannot
tag. Each of those makes the page either unable to capture a lead or unable to
carry a truthful claim, and a landing page that cannot do those two things is not
a landing page.
"""

import math
from collections.abc import Collection
from typing import Final

from pydantic import BaseModel, Field

from backend.app.engines.landing.contract import (
    LandingCheckResult,
    LandingFinding,
    LandingFindingCode,
    LandingPageSpec,
    LandingSeverity,
)

#: Weights sum to 100. Changing one is a one-line, reviewable product decision.
RULE_WEIGHTS: Final[dict[LandingFindingCode, int]] = {
    "offer": 15,
    "primary_cta": 15,
    "form_fields": 15,
    "headline": 12,
    "proof_sources": 12,
    "channel_ctas": 10,
    "consent": 8,
    "proof_points": 6,
    "reachability": 5,
    "subhead": 2,
}

SEVERITY_PENALTY: Final[dict[LandingSeverity, float]] = {"error": 1.0, "warn": 0.5, "info": 0.0}

PASS_SCORE: Final = 85

#: A headline shorter than this is a label, longer is a paragraph. Both convert
#: worse, neither is fatal -- so both are `warn`.
HEADLINE_MIN_CHARS: Final = 25
HEADLINE_MAX_CHARS: Final = 70

#: An "offer" that fits in a headline is a headline. This is the length below which
#: the copy cannot have said what the visitor actually receives.
OFFER_MIN_CHARS: Final = 40

#: Two is the floor at which "proof" is a pattern rather than an anecdote.
MIN_PROOF_POINTS: Final = 2

#: The hard ceiling, and it is not a judgement: `contract.FormFieldName` has exactly
#: four legal names and a repeat is refused, so no form can exceed this.
MAX_FORM_FIELDS: Final = 4

#: The judgement. Every additional field costs conversions, so asking for all four
#: is a `warn` -- a name and one way to reach the person is usually the whole form.
#: Kept separate from the ceiling above so that "impossible" and "inadvisable" are
#: not the same number: a rule whose failing branch cannot be reached is dead code
#: pretending to be a control.
RECOMMENDED_FORM_FIELDS: Final = 3

#: A button label. Longer than this and it is a sentence, which reads as
#: instruction rather than as an action.
MAX_PRIMARY_CTA_CHARS: Final = 40

#: A channel CTA is one or two lines of copy plus the link. The per-channel post
#: ceiling belongs to the `channel` engine and is not duplicated here.
MAX_CTA_CHARS: Final = 200

#: The fields that make an enquiry answerable. The public endpoint requires one of
#: them (``LeadSubmission._must_be_answerable_and_consented``), so a form offering
#: neither cannot produce a lead however well it is written -- which is why this is
#: an `error` and not a style note.
REACHABLE_FIELDS: Final[frozenset[str]] = frozenset({"email", "phone"})

#: Fixed order: it is the order the review screen renders and the order hints reach
#: the model, so the highest-leverage fixes come first and a truncated hint list
#: loses the least.
FINDING_ORDER: Final[tuple[LandingFindingCode, ...]] = (
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
)


class LandingCheckRequest(BaseModel):
    """The spec to audit, and the channel vocabulary to audit its CTAs against.

    `known_channels` is an argument rather than a constant in this module because
    the vocabulary belongs to ``services.link_service`` and an engine may not import
    a service. It is required and non-empty: defaulting it to "anything goes" would
    make the one rule that catches a CTA the link builder cannot tag fail open.
    """

    spec: LandingPageSpec
    known_channels: list[str] = Field(min_length=1)


def _finding(
    code: LandingFindingCode,
    severity: LandingSeverity,
    message: str,
    *,
    fix_hint: str = "",
    measured: float | None = None,
    expected: str = "",
) -> LandingFinding:
    return LandingFinding(
        code=code,
        severity=severity,
        message=message,
        fix_hint=fix_hint,
        measured=measured,
        expected=expected,
    )


def _round_half_up(value: float) -> int:
    """Round half away from zero. Python's built-in rounds half to EVEN, so
    ``round(84.5)`` is 84 and a page would fail a gate it exactly met."""
    return math.floor(value + 0.5)


def penalty_for(finding: LandingFinding) -> float:
    """What one finding costs the score."""
    return RULE_WEIGHTS[finding.code] * SEVERITY_PENALTY[finding.severity]


def aggregate(findings: list[LandingFinding]) -> tuple[int, bool]:
    """Fold findings into ``(score, passed)``.

    The error clause is independent of the score, so a page cannot buy its way past
    a missing form by being excellent everywhere else.
    """
    total = sum(penalty_for(f) for f in findings)
    score = max(0, min(100, _round_half_up(100 - total)))
    has_error = any(f.severity == "error" for f in findings)
    return score, score >= PASS_SCORE and not has_error


def _check_headline(spec: LandingPageSpec) -> LandingFinding:
    text = spec.headline.strip()
    length = len(text)
    if not text:
        return _finding(
            "headline",
            "error",
            "The page has no headline.",
            fix_hint=(
                f"Write a headline of {HEADLINE_MIN_CHARS}-{HEADLINE_MAX_CHARS} characters "
                "that names the offer and who it is for."
            ),
            measured=0,
            expected=f"{HEADLINE_MIN_CHARS}-{HEADLINE_MAX_CHARS} characters",
        )
    if length < HEADLINE_MIN_CHARS or length > HEADLINE_MAX_CHARS:
        return _finding(
            "headline",
            "warn",
            f"The headline is {length} characters.",
            fix_hint=(
                f"The headline is {length} characters; rewrite it to "
                f"{HEADLINE_MIN_CHARS}-{HEADLINE_MAX_CHARS}."
            ),
            measured=length,
            expected=f"{HEADLINE_MIN_CHARS}-{HEADLINE_MAX_CHARS} characters",
        )
    return _finding(
        "headline",
        "info",
        f"The headline is {length} characters.",
        measured=length,
        expected=f"{HEADLINE_MIN_CHARS}-{HEADLINE_MAX_CHARS} characters",
    )


def _check_subhead(spec: LandingPageSpec) -> LandingFinding:
    text = spec.subhead.strip()
    if not text:
        return _finding(
            "subhead",
            "warn",
            "There is no sub-headline.",
            fix_hint=(
                "Add one sub-headline sentence that says what the visitor gets and "
                "what it costs them (their email address, one minute)."
            ),
            measured=0,
            expected="one sentence",
        )
    return _finding(
        "subhead", "info", "A sub-headline is present.", measured=len(text), expected="one sentence"
    )


def _check_offer(spec: LandingPageSpec) -> LandingFinding:
    text = spec.offer.strip()
    length = len(text)
    if not text:
        return _finding(
            "offer",
            "error",
            "The page states no offer, so there is no reason to fill the form in.",
            fix_hint=(
                "State the offer in at least "
                f"{OFFER_MIN_CHARS} characters: what the visitor receives, in concrete "
                "terms, in exchange for their details."
            ),
            measured=0,
            expected=f">= {OFFER_MIN_CHARS} characters",
        )
    if length < OFFER_MIN_CHARS:
        return _finding(
            "offer",
            "warn",
            f"The offer is {length} characters, which is a label rather than an offer.",
            fix_hint=(
                f"The offer is {length} characters; expand it to at least "
                f"{OFFER_MIN_CHARS} and say what the visitor actually receives."
            ),
            measured=length,
            expected=f">= {OFFER_MIN_CHARS} characters",
        )
    return _finding(
        "offer",
        "info",
        "The offer is stated.",
        measured=length,
        expected=f">= {OFFER_MIN_CHARS} characters",
    )


def _check_proof_count(spec: LandingPageSpec) -> LandingFinding:
    count = len([p for p in spec.proof_points if p.text.strip()])
    if count < MIN_PROOF_POINTS:
        return _finding(
            "proof_points",
            "warn",
            f"The page carries {count} proof point(s).",
            fix_hint=(
                f"The page has {count} proof point(s); add proof points until there are at "
                f"least {MIN_PROOF_POINTS}, each taken from the business's own documents "
                "or profile. Do not invent one."
            ),
            measured=count,
            expected=f">= {MIN_PROOF_POINTS}",
        )
    return _finding(
        "proof_points",
        "info",
        f"The page carries {count} proof points.",
        measured=count,
        expected=f">= {MIN_PROOF_POINTS}",
    )


def _check_proof_sources(spec: LandingPageSpec) -> LandingFinding:
    unsourced = [p.text for p in spec.proof_points if p.text.strip() and not p.source.strip()]
    empty = [p for p in spec.proof_points if not p.text.strip()]
    if unsourced:
        first = unsourced[0][:60]
        return _finding(
            "proof_sources",
            "error",
            f"{len(unsourced)} proof point(s) name no source.",
            fix_hint=(
                f"{len(unsourced)} proof point(s) name no source, starting with {first!r}. "
                "Every proof point must name the document, page or profile field it came "
                "from. If nothing in the evidence supports it, delete it rather than "
                "sourcing it loosely."
            ),
            measured=len(unsourced),
            expected="0 unsourced",
        )
    if empty:
        return _finding(
            "proof_sources",
            "error",
            f"{len(empty)} proof point(s) have a source but no text.",
            fix_hint=(
                f"{len(empty)} proof point(s) are empty. Remove them or write the claim they "
                "were meant to carry."
            ),
            measured=len(empty),
            expected="0 empty",
        )
    return _finding(
        "proof_sources",
        "info",
        "Every proof point names its source.",
        measured=0,
        expected="0 unsourced",
    )


def _check_form_fields(spec: LandingPageSpec) -> LandingFinding:
    names = [field.name for field in spec.form_fields]
    count = len(names)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if count == 0:
        return _finding(
            "form_fields",
            "error",
            "There is no form, so the page cannot capture anything.",
            fix_hint=(
                f"Add between 1 and {RECOMMENDED_FORM_FIELDS} form fields. Use only these "
                "names: name, email, phone, message."
            ),
            measured=0,
            expected=f"1-{RECOMMENDED_FORM_FIELDS} fields",
        )
    if duplicates:
        return _finding(
            "form_fields",
            "error",
            f"The form repeats the field(s) {', '.join(duplicates)}.",
            fix_hint=(
                f"The form lists {', '.join(duplicates)} more than once. Each field name may "
                "appear at most once; a repeated name renders two inputs and only one is "
                "submitted."
            ),
            measured=len(duplicates),
            expected="no repeats",
        )
    if count > RECOMMENDED_FORM_FIELDS:
        return _finding(
            "form_fields",
            "warn",
            f"The form asks for {count} fields.",
            fix_hint=(
                f"The form asks for {count} fields; reduce it to at most "
                f"{RECOMMENDED_FORM_FIELDS}. Every additional field costs conversions, and a "
                "name plus one way to reach the person is usually the whole form."
            ),
            measured=count,
            expected=f"1-{RECOMMENDED_FORM_FIELDS} fields",
        )
    return _finding(
        "form_fields",
        "info",
        f"The form asks for {count} field(s).",
        measured=count,
        expected=f"1-{RECOMMENDED_FORM_FIELDS} fields",
    )


def _check_reachability(spec: LandingPageSpec) -> LandingFinding:
    present = {field.name for field in spec.form_fields} & REACHABLE_FIELDS
    if not present:
        return _finding(
            "reachability",
            "error",
            "The form asks for no email address and no phone number.",
            fix_hint=(
                "Add an email or a phone field. A submission carrying neither is refused by "
                "the form endpoint, so the lead would be lost after the visitor had already "
                "filled the form in."
            ),
            measured=0,
            expected="email or phone",
        )
    return _finding(
        "reachability",
        "info",
        f"The form can be answered: it asks for {', '.join(sorted(present))}.",
        measured=len(present),
        expected="email or phone",
    )


def _check_primary_cta(spec: LandingPageSpec) -> LandingFinding:
    text = spec.primary_cta.strip()
    length = len(text)
    if not text:
        return _finding(
            "primary_cta",
            "error",
            "The form has no button label, so the page makes no ask.",
            fix_hint=(
                f"Write a primary CTA of at most {MAX_PRIMARY_CTA_CHARS} characters, as an "
                "action the visitor takes ('Checkliste anfordern')."
            ),
            measured=0,
            expected=f"1-{MAX_PRIMARY_CTA_CHARS} characters",
        )
    if length > MAX_PRIMARY_CTA_CHARS:
        return _finding(
            "primary_cta",
            "warn",
            f"The button label is {length} characters.",
            fix_hint=(
                f"The button label is {length} characters; shorten it to at most "
                f"{MAX_PRIMARY_CTA_CHARS} so it reads as an action rather than an "
                "instruction."
            ),
            measured=length,
            expected=f"1-{MAX_PRIMARY_CTA_CHARS} characters",
        )
    return _finding(
        "primary_cta",
        "info",
        "The page makes one clear ask.",
        measured=length,
        expected=f"1-{MAX_PRIMARY_CTA_CHARS} characters",
    )


def _check_consent(spec: LandingPageSpec) -> LandingFinding:
    if not spec.consent_text.strip():
        return _finding(
            "consent",
            "error",
            "There is no consent sentence beside the form.",
            fix_hint=(
                "Write one consent sentence saying what the business will do with the "
                "details and that they will not be passed on. The form stores contact "
                "data, so it cannot be submitted without it."
            ),
            measured=0,
            expected="one sentence",
        )
    return _finding(
        "consent",
        "info",
        "A consent sentence is present.",
        measured=len(spec.consent_text.strip()),
        expected="one sentence",
    )


def _check_channel_ctas(spec: LandingPageSpec, known: Collection[str]) -> LandingFinding:
    usable = [cta for cta in spec.ctas if cta.text.strip() and cta.channel.strip()]
    if not usable:
        return _finding(
            "channel_ctas",
            "error",
            "No channel CTA was written, so nothing points at this page.",
            fix_hint=(
                "Write one CTA per channel, using only these channel names: "
                f"{', '.join(sorted(known))}."
            ),
            measured=0,
            expected=">= 1 CTA",
        )

    channels = [cta.channel.strip().lower() for cta in usable]
    unknown = sorted({channel for channel in channels if channel not in known})
    if unknown:
        return _finding(
            "channel_ctas",
            "error",
            f"CTA(s) name the unknown channel(s) {', '.join(unknown)}.",
            fix_hint=(
                f"The channel(s) {', '.join(unknown)} cannot be tagged, so a click on them "
                "would be attributed to nothing. Use only: "
                f"{', '.join(sorted(known))}."
            ),
            measured=len(unknown),
            expected="known channels only",
        )

    repeated = sorted({channel for channel in channels if channels.count(channel) > 1})
    if repeated:
        return _finding(
            "channel_ctas",
            "error",
            f"Two CTAs were written for {', '.join(repeated)}.",
            fix_hint=(
                f"There is more than one CTA for {', '.join(repeated)}. One per channel: two "
                "links for one channel split its clicks across two rows and the channel "
                "comparison stops being a comparison."
            ),
            measured=len(repeated),
            expected="one CTA per channel",
        )

    long = [cta.channel for cta in usable if len(cta.text.strip()) > MAX_CTA_CHARS]
    if long:
        return _finding(
            "channel_ctas",
            "warn",
            f"The CTA for {', '.join(long)} is longer than {MAX_CTA_CHARS} characters.",
            fix_hint=(
                f"The CTA for {', '.join(long)} exceeds {MAX_CTA_CHARS} characters. A CTA is "
                "one or two lines plus the link."
            ),
            measured=len(long),
            expected=f"<= {MAX_CTA_CHARS} characters",
        )
    return _finding(
        "channel_ctas",
        "info",
        f"{len(usable)} channel CTA(s), each on a taggable channel.",
        measured=len(usable),
        expected=">= 1 CTA",
    )


def check_landing_page(req: LandingCheckRequest) -> LandingCheckResult:
    """Audit one landing-page spec. Deterministic: same spec, same verdict, always."""
    spec = req.spec
    known = {channel.strip().lower() for channel in req.known_channels}

    by_code: dict[LandingFindingCode, LandingFinding] = {
        "headline": _check_headline(spec),
        "subhead": _check_subhead(spec),
        "offer": _check_offer(spec),
        "proof_points": _check_proof_count(spec),
        "proof_sources": _check_proof_sources(spec),
        "form_fields": _check_form_fields(spec),
        "reachability": _check_reachability(spec),
        "primary_cta": _check_primary_cta(spec),
        "consent": _check_consent(spec),
        "channel_ctas": _check_channel_ctas(spec, known),
    }
    findings = [by_code[code] for code in FINDING_ORDER]
    score, passed = aggregate(findings)
    return LandingCheckResult(score=score, findings=findings, passed=passed)
