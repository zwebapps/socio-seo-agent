"""The `landing` engine: the conversion audit, and the markup it produces.

Written against the properties, not the prose. What is pinned down here:

* **the four structural errors fail the gate whatever the score is** -- no form, no
  ask, no consent, no answerable field. Those are the parts without which a landing
  page cannot produce a lead, so they cannot be traded off against a good headline;
* **a proof point with no source is an error**, because an unsourced claim about a
  business is an invented one, and that is the single output this product must never
  ship;
* **the generated form and the endpoint that receives it are the same shape.** This
  is the test that would have caught a form nobody could submit: it imports both
  sides and compares them;
* **nothing from the spec reaches the markup unescaped**, since the spec is written
  by a model from crawled pages and uploaded documents;
* **the page carries no script and no third-party asset**, so it works with
  JavaScript off and sets nobody's cookie.
"""

import re
from typing import get_args

import pytest

from backend.app.api.leads import LeadSubmission
from backend.app.engines.landing import (
    HEADLINE_MAX_CHARS,
    HONEYPOT_FIELD,
    MAX_FORM_FIELDS,
    PASS_SCORE,
    RECOMMENDED_FORM_FIELDS,
    RULE_WEIGHTS,
    ChannelCta,
    FormField,
    FormFieldName,
    LandingCheckRequest,
    LandingCheckResult,
    LandingPageSpec,
    ProofPoint,
    RenderRefusedError,
    check_landing_page,
    render_landing_page,
)

KNOWN = ["facebook", "instagram", "linkedin", "email", "link_hub"]
FORM_ACTION = "/public/forms/11111111-1111-4111-8111-111111111111"


def _spec(**over: object) -> LandingPageSpec:
    """A spec that passes every rule, so a test can break exactly one thing."""
    base: dict[str, object] = {
        "headline": "Notdienst-Checkliste für Hauseigentümer in Koblenz",
        "subhead": "Was Sie prüfen können, bevor Sie den Notdienst rufen.",
        "offer": (
            "Eine zweiseitige Checkliste mit den fünf Prüfungen, die wir bei jedem "
            "Wasserschaden zuerst durchführen."
        ),
        "proof_points": [
            ProofPoint(text="Seit 1998 in Koblenz tätig.", source="Leistungsübersicht 2026, S. 1"),
            ProofPoint(
                text="24-Stunden-Notdienst an 365 Tagen.",
                source="mueller-sanitaer.de/notdienst",
            ),
        ],
        "form_fields": [
            FormField(name="name", label="Ihr Name", required=True),
            FormField(name="email", label="E-Mail-Adresse", required=True),
        ],
        "primary_cta": "Checkliste anfordern",
        "consent_text": (
            "Ich möchte die Checkliste erhalten und bin mit der Kontaktaufnahme einverstanden."
        ),
        "ctas": [
            ChannelCta(channel="facebook", text="Wasserschaden? Die fünf Prüfungen vorab:"),
            ChannelCta(channel="linkedin", text="Unsere Notdienst-Checkliste, kostenlos:"),
        ],
    }
    base.update(over)
    return LandingPageSpec.model_validate(base)


def _check(spec: LandingPageSpec) -> LandingCheckResult:
    return check_landing_page(LandingCheckRequest(spec=spec, known_channels=KNOWN))


# --------------------------------------------------------------------------- #
# The scoring model
# --------------------------------------------------------------------------- #


def test_the_weights_sum_to_one_hundred_so_a_perfect_page_scores_exactly_that() -> None:
    """The score is only interpretable if the top of the range is reachable."""
    assert sum(RULE_WEIGHTS.values()) == 100


def test_a_page_that_passes_every_rule_scores_one_hundred_and_passes() -> None:
    result = _check(_spec())

    assert result.score == 100
    assert result.passed is True
    assert result.errors == []
    assert result.fix_hints == [], "a passing rule must never send a hint to the model"


def test_every_finding_code_is_reported_including_the_passes() -> None:
    """The review screen renders a checklist, not only the problems."""
    result = _check(_spec())
    assert {f.code for f in result.findings} == set(RULE_WEIGHTS)


def test_one_warning_alone_cannot_fail_the_gate() -> None:
    """The heaviest warn is well inside the headroom above the pass mark, so a page
    is never blocked by one nice-to-have."""
    result = _check(_spec(subhead=""))

    assert result.passed is True
    assert result.score < 100
    assert any(f.code == "subhead" and f.severity == "warn" for f in result.findings)


def test_severity_is_graded_so_a_near_miss_costs_less_than_an_absence() -> None:
    """A too-long headline is a near miss (half weight); a missing one is fatal."""
    near_miss = _check(_spec(headline="x" * (HEADLINE_MAX_CHARS + 5))).score
    absent = _check(_spec(headline="")).score

    assert near_miss > absent, "if these scored the same the number would carry no information"
    assert near_miss == 100 - RULE_WEIGHTS["headline"] * 0.5
    assert absent == 100 - RULE_WEIGHTS["headline"]


# --------------------------------------------------------------------------- #
# The four structural errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"form_fields": []}, "form_fields"),
        ({"primary_cta": ""}, "primary_cta"),
        ({"consent_text": ""}, "consent"),
        (
            {
                "form_fields": [
                    FormField(name="name", label="Name"),
                    FormField(name="message", label="Nachricht"),
                ]
            },
            "reachability",
        ),
    ],
)
def test_a_structural_error_fails_the_gate_and_names_itself(
    override: dict[str, object], code: str
) -> None:
    result = _check(_spec(**override))
    offending = next(f for f in result.findings if f.code == code)

    assert result.passed is False
    assert offending.severity == "error"
    assert offending.fix_hint, "an error with no fix hint cannot be retried"


def test_an_error_fails_the_gate_even_when_the_score_would_pass() -> None:
    """`consent` is worth 8 points, so a page missing it still scores 92 -- above the
    pass mark. The error clause is what stops it, and it must be independent of the
    score or a page could buy its way past a compliance requirement."""
    result = _check(_spec(consent_text=""))

    assert result.score >= PASS_SCORE
    assert result.passed is False


def test_a_proof_point_with_no_source_is_an_error_not_a_warning() -> None:
    result = _check(
        _spec(
            proof_points=[
                ProofPoint(text="Wir sind die Nummer eins in Koblenz.", source=""),
                ProofPoint(text="Seit 1998 tätig.", source="Leistungsübersicht 2026"),
            ]
        )
    )

    assert result.passed is False
    assert [f.code for f in result.errors] == ["proof_sources"]
    hint = next(f.fix_hint for f in result.findings if f.code == "proof_sources")
    assert "Nummer eins" in hint, "the hint must name the claim so the retry is a correction"
    assert "delete it" in hint, "the honest instruction is deletion, not looser sourcing"


def test_too_few_proof_points_is_a_warning_and_never_invents_one() -> None:
    result = _check(_spec(proof_points=[]))

    assert result.passed is True
    hint = next(f.fix_hint for f in result.findings if f.code == "proof_points")
    assert "Do not invent one." in hint


# --------------------------------------------------------------------------- #
# The form, and the endpoint that receives it
# --------------------------------------------------------------------------- #


def test_the_generated_form_can_only_use_fields_the_public_endpoint_accepts() -> None:
    """The coupling that matters most, asserted across the boundary.

    ``LeadSubmission`` is declared ``extra="forbid"``, so a generated field it does
    not know is refused AFTER the visitor has typed it in -- the lead is lost and
    nothing says so. This test fails if either side moves.
    """
    engine_names = set(get_args(FormFieldName.__value__))
    accepted = set(LeadSubmission.model_fields)

    assert engine_names <= accepted, (
        "the landing engine can generate a form field the lead endpoint refuses: "
        f"{sorted(engine_names - accepted)}"
    )
    assert engine_names == {"name", "email", "phone", "message"}


def test_the_honeypot_field_name_matches_the_one_the_endpoint_reads() -> None:
    """A honeypot under a different name is not a honeypot: the endpoint would refuse
    the submission as an unexpected key instead of silently dropping the bot."""
    assert HONEYPOT_FIELD in LeadSubmission.model_fields


def test_a_repeated_field_name_is_an_error_because_only_one_would_submit() -> None:
    result = _check(
        _spec(
            form_fields=[
                FormField(name="email", label="E-Mail"),
                FormField(name="email", label="E-Mail wiederholen"),
            ]
        )
    )
    assert result.passed is False
    assert [f.code for f in result.errors] == ["form_fields"]


def test_asking_for_every_field_is_a_warning_that_names_the_recommended_number() -> None:
    """The reachable over-asking case. There are only four legal field names and a
    repeat is an error, so the ceiling can never be exceeded -- which is exactly why
    the judgement (`RECOMMENDED_FORM_FIELDS`) is a different number from the
    structural limit (`MAX_FORM_FIELDS`); a rule whose failing branch is unreachable
    is dead code pretending to be a control."""
    result = _check(
        _spec(
            form_fields=[
                FormField(name="name", label="Name"),
                FormField(name="email", label="E-Mail"),
                FormField(name="phone", label="Telefon"),
                FormField(name="message", label="Nachricht"),
            ]
        )
    )

    assert result.passed is True, "over-asking costs conversions; it does not break the page"
    finding = next(f for f in result.findings if f.code == "form_fields")
    assert finding.severity == "warn"
    assert finding.measured == MAX_FORM_FIELDS
    assert str(RECOMMENDED_FORM_FIELDS) in finding.fix_hint


# --------------------------------------------------------------------------- #
# Channel CTAs -- the traffic path
# --------------------------------------------------------------------------- #


def test_no_channel_cta_is_an_error_because_nothing_would_point_at_the_page() -> None:
    result = _check(_spec(ctas=[]))

    assert result.passed is False
    assert [f.code for f in result.errors] == ["channel_ctas"]


def test_a_cta_on_a_channel_the_link_builder_cannot_tag_is_an_error() -> None:
    """`link_service.build_utm` raises on an unknown channel, so publishing this page
    would fail at the point of minting the link. Catching it here turns a crash into
    a fix hint that names the legal channels."""
    result = _check(_spec(ctas=[ChannelCta(channel="myspace", text="Hier entlang:")]))

    assert result.passed is False
    hint = next(f.fix_hint for f in result.findings if f.code == "channel_ctas")
    assert "myspace" in hint
    assert "linkedin" in hint, "the hint must list what IS allowed"


def test_two_ctas_for_one_channel_is_an_error_because_it_splits_the_measurement() -> None:
    result = _check(
        _spec(
            ctas=[
                ChannelCta(channel="linkedin", text="Variante A"),
                ChannelCta(channel="LinkedIn", text="Variante B"),
            ]
        )
    )
    assert result.passed is False
    assert [f.code for f in result.errors] == ["channel_ctas"]


def test_the_known_channel_list_is_required_rather_than_defaulted() -> None:
    """A default of "anything goes" would make the unknown-channel rule fail open."""
    with pytest.raises(ValueError, match="known_channels"):
        LandingCheckRequest(spec=_spec(), known_channels=[])


# --------------------------------------------------------------------------- #
# claim_text -- what the regulated-claim gate sees
# --------------------------------------------------------------------------- #


def test_claim_text_carries_every_written_string_on_the_page() -> None:
    """A landing page is the most claim-dangerous artifact in the product, so the
    text handed to the claim gate must be assembled from the model rather than by
    whoever remembers to add the new field."""
    spec = _spec(
        headline="SENTINEL_HEADLINE",
        subhead="SENTINEL_SUBHEAD",
        offer="SENTINEL_OFFER",
        primary_cta="SENTINEL_CTA",
        consent_text="SENTINEL_CONSENT",
        proof_points=[ProofPoint(text="SENTINEL_PROOF", source="SENTINEL_SOURCE")],
        form_fields=[FormField(name="email", label="SENTINEL_LABEL")],
        ctas=[ChannelCta(channel="facebook", text="SENTINEL_CHANNEL_CTA")],
    )

    text = spec.claim_text()

    for sentinel in (
        "SENTINEL_HEADLINE",
        "SENTINEL_SUBHEAD",
        "SENTINEL_OFFER",
        "SENTINEL_CTA",
        "SENTINEL_CONSENT",
        "SENTINEL_PROOF",
        "SENTINEL_LABEL",
        "SENTINEL_CHANNEL_CTA",
    ):
        assert sentinel in text, f"{sentinel} is not checked for banned claims"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render(spec: LandingPageSpec | None = None, **over: object) -> str:
    kwargs: dict[str, object] = {
        "business_name": "Müller Sanitär GmbH",
        "form_action": FORM_ACTION,
    }
    kwargs.update(over)
    return render_landing_page(spec or _spec(), **kwargs)  # type: ignore[arg-type]


def test_the_page_carries_no_script_and_no_third_party_asset() -> None:
    """It has to work with JavaScript off, and it must not let anyone track the
    visitor on our behalf. Both are properties of the string, so both are asserted."""
    html = _render()

    lowered = html.lower()
    assert "<script" not in lowered
    assert "javascript:" not in lowered
    assert " onclick" not in lowered and "onsubmit" not in lowered
    assert "<link" not in lowered, "no external stylesheet"
    assert not re.search(r'src\s*=\s*"https?://', lowered), "no remote asset"
    assert "set-cookie" not in lowered


def test_the_form_posts_to_the_action_it_was_given_with_a_plain_post() -> None:
    html = _render()

    assert f'<form method="post" action="{FORM_ACTION}">' in html
    assert 'type="submit"' in html


def test_everything_from_the_spec_is_escaped() -> None:
    """The spec is written by a model from crawled pages, so it is untrusted text."""
    html = _render(
        _spec(
            headline="</h1><script>alert(1)</script>",
            consent_text='" onmouseover="x',
            proof_points=[ProofPoint(text="<img src=x onerror=1>", source='"><b>')],
        )
    )

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert 'onmouseover="x"' not in html
    assert "&quot;" in html


def test_the_honeypot_is_present_hidden_and_out_of_the_tab_order() -> None:
    html = _render()

    assert f'name="{HONEYPOT_FIELD}"' in html
    assert 'aria-hidden="true"' in html
    assert 'tabindex="-1"' in html
    assert 'autocomplete="off"' in html


def test_consent_is_a_required_checkbox_carrying_the_generated_sentence() -> None:
    html = _render()

    assert 'type="checkbox" name="consent" value="on" required' in html
    assert "Kontaktaufnahme einverstanden" in html


def test_utm_parameters_ride_along_as_hidden_inputs_so_the_lead_carries_them() -> None:
    html = _render(utm={"utm_source": "linkedin", "utm_campaign": "notdienst-checkliste"})

    assert '<input type="hidden" name="utm_source" value="linkedin">' in html
    assert 'name="utm_campaign" value="notdienst-checkliste"' in html


def test_anything_not_shaped_like_a_utm_parameter_is_dropped_rather_than_rendered() -> None:
    """The query string is caller-controlled, so the form must not become a carrier
    for arbitrary data somebody appended to a campaign link."""
    html = _render(utm={"redirect": "https://evil.example", "utm_source": "email", "UTM_X": "1"})

    assert "evil.example" not in html
    assert "UTM_X" not in html
    assert 'name="utm_source" value="email"' in html


def test_a_reference_code_is_accepted_by_shape_and_a_malformed_one_is_dropped() -> None:
    good = _render(ref="Ab3xY7kp")
    bad = _render(ref='"><script>')

    assert '<input type="hidden" name="ref" value="Ab3xY7kp">' in good
    assert 'name="ref"' not in bad, "a code that cannot be one of ours is not reflected at all"


def test_a_page_that_cannot_capture_a_lead_is_refused_rather_than_rendered() -> None:
    """Serving it would look like success and convert nothing."""
    with pytest.raises(RenderRefusedError) as exc:
        _render(_spec(form_fields=[], primary_cta="", consent_text=""))

    assert len(exc.value.missing) == 3
    assert "nothing to submit" in str(exc.value)


def test_the_confirmation_state_replaces_the_form_instead_of_needing_a_second_page() -> None:
    sent = _render(state="sent")

    assert "<form" not in sent, "a submitted form must not be re-offered"
    assert 'role="status"' in sent
    assert "Danke" in sent


def test_the_error_state_keeps_the_form_and_explains_what_is_needed() -> None:
    errored = _render(state="error")

    assert "<form" in errored
    assert 'role="alert"' in errored
    assert "Einwilligung" in errored


def test_the_language_attribute_is_shape_validated_rather_than_escaped() -> None:
    """It lands in an attribute on the root element, so it is checked against a shape
    rather than escaped: a locale is either a language tag or a bug."""
    assert '<html lang="de">' in _render(locale="de")
    assert '<html lang="en">' in _render(locale='"><script>')
    assert "script" not in _render(locale='"><script>')[:120]


def test_a_locale_we_have_no_copy_for_keeps_its_language_tag_and_falls_back_to_english() -> None:
    """The tag describes the GENERATED copy, which is whatever language the business
    writes in; only our own five chrome strings fall back."""
    french = _render(locale="fr", state="sent")

    assert '<html lang="fr">' in french
    assert "Thank you" in french
