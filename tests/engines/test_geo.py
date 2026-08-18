"""Tests for the `geo` engine: prompt sets, presence detection, share-of-voice maths.

Everything here is pure. No network, no database, no model -- the engine cannot
reach any of them by construction (docs/ARCHITECTURE.md section 3), so these
tests need no fixtures at all.

The bias of this file mirrors the bias of the module: most cases exist to prove
the number is **honest** rather than merely present. A share-of-voice figure that
counts a model outage as absence, or that reads "Mueller Sanitaer" as a different
business from "Müller Sanitär", is worse than no figure -- it is a confident lie
on a dashboard tile.
"""

import hashlib
from collections.abc import Sequence

from pydantic import ValidationError

from backend.app.engines.geo import (
    PROMPT_SET_VERSION,
    BrandIdentity,
    GeoPrompt,
    ProbeOutcome,
    ShareOfVoice,
    answer_excerpt,
    build_prompt_set,
    classify_answer,
    detect_presence,
    diff_share_of_voice,
    extract_hosts,
    fold_for_matching,
    looks_like_refusal,
    prompt_set_fingerprint,
    share_of_voice,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

SERVICES = ("Badsanierung", "Heizungswartung")
COMPETITORS = ("Sanitär Weber",)


def _set(**overrides: object) -> list[GeoPrompt]:
    kwargs: dict[str, object] = {
        "business_name": "Müller Sanitär",
        "city": "Koblenz",
        "services": SERVICES,
        "competitors": COMPETITORS,
    }
    kwargs.update(overrides)
    return build_prompt_set(**kwargs)  # type: ignore[arg-type]


def _texts(prompts: list[GeoPrompt]) -> list[str]:
    return [prompt.text for prompt in prompts]


# --------------------------------------------------------------------------- #
# Determinism and versioning
# --------------------------------------------------------------------------- #


def test_prompt_set_is_byte_identical_across_calls() -> None:
    """Same inputs, same prompts, same order -- or week-over-week is meaningless."""
    first = _set()
    second = _set()

    assert [p.model_dump() for p in first] == [p.model_dump() for p in second]
    assert len(first) > 0


def test_every_prompt_carries_the_set_version() -> None:
    prompts = _set()

    assert {prompt.set_version for prompt in prompts} == {PROMPT_SET_VERSION}


def test_prompt_id_is_a_documented_content_hash() -> None:
    """The id is content-addressed, so a changed question cannot reuse an old id.

    Pinned against the documented canonical string rather than a captured
    literal: if the scheme changes, this fails and the change has to be
    deliberate.
    """
    prompt = _set()[0]
    canonical = f"{prompt.set_version}|{prompt.locale}|{prompt.category}|{prompt.text}"

    assert prompt.prompt_id == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def test_prompt_ids_are_unique_within_a_set() -> None:
    prompts = _set()

    assert len({prompt.prompt_id for prompt in prompts}) == len(prompts)


def test_reordering_services_reorders_prompts_but_keeps_their_ids() -> None:
    """Order is input order; identity is content. Both properties are needed:
    order for a stable UI, identity for a valid run-over-run comparison."""
    forward = _set(services=("Badsanierung", "Heizungswartung"))
    reverse = _set(services=("Heizungswartung", "Badsanierung"))

    assert _texts(forward) != _texts(reverse)
    assert {p.prompt_id for p in forward} == {p.prompt_id for p in reverse}


def test_fingerprint_is_order_insensitive_and_content_sensitive() -> None:
    """The fingerprint answers "were these the same questions?" -- nothing else."""
    forward = _set(services=("Badsanierung", "Heizungswartung"))
    reverse = _set(services=("Heizungswartung", "Badsanierung"))
    narrower = _set(services=("Badsanierung",))

    ids = [p.prompt_id for p in forward]
    assert prompt_set_fingerprint(ids) == prompt_set_fingerprint([p.prompt_id for p in reverse])
    assert prompt_set_fingerprint(ids) != prompt_set_fingerprint([p.prompt_id for p in narrower])
    assert prompt_set_fingerprint(ids) == prompt_set_fingerprint(reversed(ids))


# --------------------------------------------------------------------------- #
# The high-intent shapes
# --------------------------------------------------------------------------- #


def test_all_five_high_intent_shapes_are_present() -> None:
    prompts = _set()
    categories = {prompt.category for prompt in prompts}

    assert categories == {"best_in_city", "near_city", "cost", "comparison", "reputation"}


def test_shapes_interpolate_the_business_inputs() -> None:
    prompts = _set(locale="en")
    by_category: dict[str, list[str]] = {}
    for prompt in prompts:
        by_category.setdefault(prompt.category, []).append(prompt.text)

    assert "best" in by_category["best_in_city"][0].lower()
    assert "Badsanierung" in by_category["best_in_city"][0]
    assert "Koblenz" in by_category["best_in_city"][0]

    assert "cost" in by_category["cost"][0].lower()
    assert "Koblenz" not in by_category["cost"][0]  # the shape is price, not locality

    assert "near" in by_category["near_city"][0].lower()
    assert "Koblenz" in by_category["near_city"][0]

    assert "vs" in by_category["comparison"][0].lower()
    assert "Müller Sanitär" in by_category["comparison"][0]
    assert "Sanitär Weber" in by_category["comparison"][0]

    assert "Müller Sanitär" in by_category["reputation"][0]


def test_prompts_naming_the_brand_are_flagged_as_such() -> None:
    """A prompt containing the brand nearly guarantees a mention, so it can never
    be pooled with brand-free prompts without a label. This flag is what lets the
    score report an unprompted rate separately."""
    prompts = _set()
    flagged = {prompt.category for prompt in prompts if prompt.contains_brand}

    assert flagged == {"comparison", "reputation"}


def test_prompt_count_scales_with_services_and_competitors() -> None:
    prompts = _set(services=("a", "b", "c"), competitors=("x", "y"))

    # 3 services x 3 service-shaped categories + 2 comparisons + 1 reputation
    assert len(prompts) == 3 * 3 + 2 + 1


def test_no_services_still_produces_brand_prompts() -> None:
    prompts = _set(services=())

    assert {prompt.category for prompt in prompts} == {"comparison", "reputation"}


# --------------------------------------------------------------------------- #
# Input hygiene
# --------------------------------------------------------------------------- #


def test_duplicate_and_blank_services_are_collapsed() -> None:
    """Duplicates would double-count one question in the denominator."""
    prompts = _set(services=("Badsanierung", "badsanierung ", "", "  ", "BADSANIERUNG"))

    assert len([p for p in prompts if p.category == "cost"]) == 1


def test_duplicate_competitors_are_collapsed() -> None:
    prompts = _set(competitors=("Sanitär Weber", "sanitaer weber"))

    assert len([p for p in prompts if p.category == "comparison"]) == 1


def test_a_competitor_equal_to_the_brand_is_dropped() -> None:
    """ "Müller Sanitär vs Mueller Sanitaer" is not a comparison, it is noise."""
    prompts = _set(competitors=("Mueller Sanitaer",))

    assert [p for p in prompts if p.category == "comparison"] == []


def test_blank_business_name_is_rejected() -> None:
    try:
        _set(business_name="   ")
    except ValueError as exc:
        assert "business_name" in str(exc)
    else:  # pragma: no cover - the assertion below reports the miss
        raise AssertionError("a blank business name must not silently build a prompt set")


def test_blank_city_is_rejected() -> None:
    try:
        _set(city="")
    except ValueError as exc:
        assert "city" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a blank city must not silently build a prompt set")


# --------------------------------------------------------------------------- #
# Locale
# --------------------------------------------------------------------------- #


def test_german_is_the_default_and_regional_variants_resolve_to_it() -> None:
    default = _set()
    austrian = _set(locale="de-AT")

    assert {prompt.locale for prompt in default} == {"de"}
    assert _texts(default) == _texts(austrian)


def test_an_unsupported_locale_falls_back_to_english_visibly() -> None:
    """Silent fallback would leave a French business comparing German prompts to
    English ones. The resolved locale is recorded, so the fallback is auditable."""
    prompts = _set(locale="fr-FR")

    assert {prompt.locale for prompt in prompts} == {"en"}
    assert _texts(prompts) == _texts(_set(locale="en"))


# =========================================================================== #
# Detection: mention vs citation
# =========================================================================== #

BRAND = BrandIdentity(
    name="Müller Sanitär",
    aliases=["Müller Sanitär GmbH", "Sanitär Müller"],
    domains=["mueller-sanitaer.de"],
)
RIVAL = BrandIdentity(name="Sanitär Weber", domains=["sanitaer-weber.de"])


def test_a_name_without_a_link_is_a_mention_and_not_a_citation() -> None:
    """The two are different products of value and are never collapsed."""
    result = detect_presence(
        "For bathrooms in Koblenz, Müller Sanitär is well regarded.", brand=BRAND
    )

    assert result.mentioned is True
    assert result.cited is False
    assert result.matched_name == "Müller Sanitär"


def test_a_link_without_a_name_is_a_citation_and_not_a_mention() -> None:
    result = detect_presence("See https://mueller-sanitaer.de/kontakt for details.", brand=BRAND)

    assert result.cited is True
    assert result.mentioned is False
    assert result.matched_domains == ["mueller-sanitaer.de"]
    assert result.present is True


def test_both_signals_can_fire_on_one_answer() -> None:
    result = detect_presence(
        "Müller Sanitär (https://www.mueller-sanitaer.de) covers the whole region.", brand=BRAND
    )

    assert (result.mentioned, result.cited) == (True, True)


def test_an_empty_answer_yields_no_signal() -> None:
    result = detect_presence("", brand=BRAND, competitors=[RIVAL])

    assert (result.mentioned, result.cited, result.present) == (False, False, False)
    assert result.competitors_mentioned == []


# --------------------------------------------------------------------------- #
# Near-misses in both directions
# --------------------------------------------------------------------------- #


def test_transliterated_and_uppercased_brand_still_matches() -> None:
    """German businesses type all of these, and directories force some of them."""
    for variant in (
        "Mueller Sanitaer",
        "MÜLLER SANITÄR",
        "Müller-Sanitär",
        "mueller sanitaer",
        "Müller  Sanitär",
    ):
        result = detect_presence(f"I would call {variant} first.", brand=BRAND)
        assert result.mentioned is True, variant


def test_an_alias_matches_and_is_recorded_as_the_thing_that_matched() -> None:
    result = detect_presence("Try Sanitär Müller in the old town.", brand=BRAND)

    assert result.mentioned is True
    assert result.matched_name == "Sanitär Müller"


def test_a_substring_inside_an_unrelated_word_is_not_a_mention() -> None:
    """The failure that would make the whole tile untrustworthy."""
    kern = BrandIdentity(name="Kern")

    assert detect_presence("That is the kernel of the problem.", brand=kern).mentioned is False
    assert detect_presence("Kern is the local supplier.", brand=kern).mentioned is True


def test_a_run_together_compound_is_not_a_mention() -> None:
    result = detect_presence("Buy from Müllersanitärbedarf instead.", brand=BRAND)

    assert result.mentioned is False


def test_digraph_collapse_conflates_baur_and_bauer_a_known_false_positive() -> None:
    """Documented limit, asserted so it cannot be forgotten.

    Folding `ue` -> `u` is what makes Müller/Mueller one business; the same rule
    makes Baur and Bauer one business, which they are not. The transliteration
    case is far more common in this market, and the stored excerpt makes the rare
    conflation visible to whoever reads the row -- but it is a false positive and
    it is not pretended away.
    """
    assert fold_for_matching("Bauer") == fold_for_matching("Baur")
    assert detect_presence("Ask Bauer.", brand=BrandIdentity(name="Baur")).mentioned is True


# --------------------------------------------------------------------------- #
# Citations: hosts
# --------------------------------------------------------------------------- #


def test_bare_domains_urls_ports_paths_and_www_all_normalise() -> None:
    for text in (
        "see mueller-sanitaer.de",
        "https://WWW.Mueller-Sanitaer.DE:443/leistungen?utm_source=x",
        "http://mueller-sanitaer.de.",
        "mail to info@mueller-sanitaer.de",
        "[Müller](https://mueller-sanitaer.de/bad)",
    ):
        assert detect_presence(text, brand=BRAND).cited is True, text


def test_a_subdomain_cites_the_domain() -> None:
    assert detect_presence("shop.mueller-sanitaer.de sells taps", brand=BRAND).cited is True


def test_a_lookalike_host_does_not_cite() -> None:
    """`example.com.attacker.ru` must never count. A contains-check would make the
    citation counter trivially spoofable."""
    assert detect_presence("go to mueller-sanitaer.de.billig-shop.ru", brand=BRAND).cited is False
    assert detect_presence("not-mueller-sanitaer.de is a copy", brand=BRAND).cited is False


def test_hostnames_are_not_umlaut_folded() -> None:
    """A hostname is a registration, not prose: müller-sanitär.de and
    mueller-sanitaer.de can belong to two different companies."""
    assert detect_presence("visit müller-sanitär.de today", brand=BRAND).cited is False


def test_extract_hosts_is_ordered_and_deduplicated() -> None:
    hosts = extract_hosts("a.de then https://www.a.de/x then b.de then a.de")

    assert hosts == ["a.de", "b.de"]


# --------------------------------------------------------------------------- #
# Competitors
# --------------------------------------------------------------------------- #


def test_competitors_are_detected_separately_for_mention_and_citation() -> None:
    result = detect_presence(
        "The options are Sanitär Weber and Bäder Klein (sanitaer-weber.de).",
        brand=BRAND,
        competitors=[RIVAL, BrandIdentity(name="Bäder Klein", domains=["baeder-klein.de"])],
    )

    assert result.mentioned is False
    assert result.competitors_mentioned == ["Sanitär Weber", "Bäder Klein"]
    assert result.competitors_cited == ["Sanitär Weber"]


# --------------------------------------------------------------------------- #
# Refusals -- the rule the whole metric rests on
# --------------------------------------------------------------------------- #


def test_empty_and_whitespace_answers_are_refusals() -> None:
    assert looks_like_refusal("") is True
    assert looks_like_refusal("   \n\t ") is True
    assert classify_answer("") == "no_answer"


def test_short_refusals_in_both_languages_are_no_answer() -> None:
    for text in (
        "I can't help with that.",
        "I'm unable to provide recommendations for specific businesses.",
        "As an AI, I don't have access to local business listings.",
        "Sorry, I cannot recommend a provider.",
        "Ich kann dazu leider keine Auskunft geben.",
        "Dazu habe ich keine Informationen.",
    ):
        assert classify_answer(text) == "no_answer", text


def test_a_long_hedged_answer_is_still_an_answer() -> None:
    """Hedging is not refusing. Treating it as a refusal would shrink the
    denominator and inflate every percentage on the dashboard."""
    text = (
        "I don't have real-time access to local directories, but I can describe how to "
        "choose a bathroom fitter in Koblenz. Look for a Meisterbetrieb with references, "
        "ask for a written Angebot covering materials and labour, check whether they "
        "handle the Bauantrag where one is needed, and compare at least three quotes. "
        "Established regional firms usually publish their references online."
    )

    assert classify_answer(text) == "answered"


def test_an_answer_that_names_a_business_is_an_answer_however_it_opens() -> None:
    """The override that keeps the rule from eating real data: a model that named
    somebody answered the question."""
    text = "I can't verify current details, but Müller Sanitär is the usual recommendation."

    assert classify_answer(text) == "no_answer"  # judged on wording alone
    assert classify_answer(text, named_any_brand=True) == "answered"


def test_answer_excerpt_collapses_whitespace_and_truncates_on_a_word_boundary() -> None:
    assert answer_excerpt("  a   b \n c ") == "a b c"

    long = " ".join(["Koblenz"] * 100)
    excerpt = answer_excerpt(long, limit=50)

    assert len(excerpt) <= 53
    assert excerpt.endswith("...")
    assert "Koblen." not in excerpt


# =========================================================================== #
# Scoring: the denominator is the whole product
# =========================================================================== #


def _outcome(
    *,
    prompt_id: str = "p1",
    category: str = "best_in_city",
    contains_brand: bool = False,
    model: str = "m1",
    provider: str = "prov",
    status: str = "answered",
    mentioned: bool = False,
    cited: bool = False,
    competitors: Sequence[str] = (),
    competitors_cited: Sequence[str] = (),
    set_version: str = PROMPT_SET_VERSION,
    error: str | None = None,
) -> ProbeOutcome:
    return ProbeOutcome(
        prompt_id=prompt_id,
        prompt_text=f"question {prompt_id}",
        category=category,  # type: ignore[arg-type]
        set_version=set_version,
        prompt_contains_brand=contains_brand,
        provider=provider,
        model=model,
        status=status,  # type: ignore[arg-type]
        mentioned=mentioned,
        cited=cited,
        competitors_mentioned=list(competitors),
        competitors_cited=list(competitors_cited),
        error=error,
    )


def test_a_no_answer_outcome_cannot_carry_a_mention() -> None:
    """Enforced by the type, not by the caller remembering.

    Without this an outcome could contribute to a numerator it was excluded from
    in the denominator, which can produce a share above 100%.
    """
    try:
        _outcome(status="no_answer", mentioned=True)
    except ValidationError as exc:
        assert "no_answer" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a no_answer must not be allowed to claim a mention")


def test_no_answer_is_excluded_from_the_denominator() -> None:
    """Three of eight probes mention the brand and two returned nothing, so the
    answer is 3/6 = 50.0% -- never 3/8 = 37.5%.

    This is the difference between a measurement and a fabrication: a provider
    outage must never read as the brand being absent.
    """
    outcomes = [
        *(_outcome(prompt_id=f"p{i}", mentioned=True) for i in range(3)),
        *(_outcome(prompt_id=f"p{i + 3}", mentioned=False) for i in range(3)),
        _outcome(prompt_id="p6", status="no_answer", error="429 rate limited"),
        _outcome(prompt_id="p7", status="no_answer", error="refusal"),
    ]

    sov = share_of_voice(outcomes)

    assert sov.probes_total == 8
    assert sov.no_answer_count == 2
    assert sov.usable_answers == 6
    assert sov.mentions == 3
    assert sov.mention_share_pct == 50.0
    assert sov.mention_share_pct != 37.5


def test_zero_usable_answers_is_unknown_not_zero() -> None:
    """No division, and no "0%" claim we never measured."""
    sov = share_of_voice([_outcome(status="no_answer") for _ in range(4)])

    assert sov.usable_answers == 0
    assert sov.mention_share_pct is None
    assert sov.citation_share_pct is None
    assert "unknown, not zero" in sov.headline


def test_an_empty_run_scores_without_dividing_by_zero() -> None:
    sov = share_of_voice([])

    assert (sov.probes_total, sov.usable_answers, sov.no_answer_count) == (0, 0, 0)
    assert sov.mention_share_pct is None
    assert sov.models == []


def test_zero_and_one_hundred_percent_both_land_exactly() -> None:
    none_mentioned = share_of_voice([_outcome(prompt_id=f"p{i}") for i in range(4)])
    all_mentioned = share_of_voice(
        [_outcome(prompt_id=f"p{i}", mentioned=True, cited=True) for i in range(4)]
    )

    assert none_mentioned.mention_share_pct == 0.0
    assert all_mentioned.mention_share_pct == 100.0
    assert all_mentioned.citation_share_pct == 100.0


def test_mentions_and_citations_are_counted_separately() -> None:
    sov = share_of_voice(
        [
            _outcome(prompt_id="p1", mentioned=True, cited=False),
            _outcome(prompt_id="p2", mentioned=False, cited=True),
            _outcome(prompt_id="p3"),
            _outcome(prompt_id="p4"),
        ]
    )

    assert (sov.mentions, sov.citations) == (1, 1)
    assert sov.mention_share_pct == 25.0
    assert sov.citation_share_pct == 25.0


# --------------------------------------------------------------------------- #
# Sample size and breakdowns travel with the number
# --------------------------------------------------------------------------- #


def test_the_headline_cannot_omit_the_denominator() -> None:
    """A caller physically cannot render "22%" without "9 of 41 across 3 models"."""
    outcomes = [
        *(_outcome(prompt_id=f"a{i}", model="m1", mentioned=i < 2) for i in range(3)),
        *(_outcome(prompt_id=f"b{i}", model="m2", mentioned=False) for i in range(3)),
        _outcome(prompt_id="c1", model="m3", status="no_answer", error="timeout"),
    ]

    sov = share_of_voice(outcomes)

    assert sov.usable_answers == 6
    assert sov.models_probed == 3
    assert "2 of 6 answers" in sov.headline
    assert "3 model(s)" in sov.headline
    assert "1 excluded as no_answer" in sov.headline


def test_per_model_breakdown_keeps_its_own_sample_size() -> None:
    """A pooled 33% that is really "100% on one model, 0% on two" is a different
    fact about the business, so the split is not optional."""
    outcomes = [
        _outcome(prompt_id="p1", model="strong", mentioned=True),
        _outcome(prompt_id="p2", model="strong", mentioned=True),
        _outcome(prompt_id="p1", model="weak"),
        _outcome(prompt_id="p2", model="weak"),
        _outcome(prompt_id="p3", model="broken", status="no_answer", error="503"),
    ]

    sov = share_of_voice(outcomes)
    by_model = {m.model: m for m in sov.models}

    assert [m.model for m in sov.models] == ["strong", "weak", "broken"]  # first-seen order
    assert by_model["strong"].mention_share_pct == 100.0
    assert by_model["weak"].mention_share_pct == 0.0
    assert by_model["broken"].usable_answers == 0
    assert by_model["broken"].no_answer_count == 1
    assert by_model["broken"].mention_share_pct is None
    assert sov.mention_share_pct == 50.0


def test_competitors_are_scored_on_the_same_denominator() -> None:
    outcomes = [
        _outcome(prompt_id="p1", mentioned=True, competitors=["Weber"]),
        _outcome(prompt_id="p2", competitors=["Weber", "Klein"], competitors_cited=["Weber"]),
        _outcome(prompt_id="p3", competitors=["Weber"]),
        _outcome(prompt_id="p4", status="no_answer", error="refusal"),
    ]

    sov = share_of_voice(outcomes)
    by_name = {c.name: c for c in sov.competitors}

    assert [c.name for c in sov.competitors] == ["Weber", "Klein"]
    assert by_name["Weber"].usable_answers == 3
    assert by_name["Weber"].mention_share_pct == 100.0
    assert by_name["Weber"].citation_share_pct == round(100 / 3, 1)
    assert by_name["Klein"].mentions == 1
    assert sov.mention_share_pct == round(100 / 3, 1)


def test_per_category_breakdown_says_which_questions_we_lose() -> None:
    outcomes = [
        _outcome(prompt_id="p1", category="cost", mentioned=False),
        _outcome(prompt_id="p2", category="cost", mentioned=False),
        _outcome(prompt_id="p3", category="best_in_city", mentioned=True),
    ]

    sov = share_of_voice(outcomes)
    by_category = {c.category: c for c in sov.categories}

    assert by_category["cost"].mention_share_pct == 0.0
    assert by_category["best_in_city"].mention_share_pct == 100.0


def test_prompts_that_name_the_brand_are_excluded_from_the_unprompted_rate() -> None:
    """ "Is Müller Sanitär any good?" nearly always yields a mention. Pooling that
    with brand-free questions would manufacture a flattering headline."""
    outcomes = [
        _outcome(prompt_id="p1", category="reputation", contains_brand=True, mentioned=True),
        _outcome(prompt_id="p2", category="comparison", contains_brand=True, mentioned=True),
        _outcome(prompt_id="p3", category="best_in_city", mentioned=True),
        _outcome(prompt_id="p4", category="cost", mentioned=False),
        _outcome(prompt_id="p5", category="cost", mentioned=False),
    ]

    sov = share_of_voice(outcomes)

    assert sov.mention_share_pct == 60.0  # 3 of 5, prompted questions included
    assert sov.unprompted_usable_answers == 3
    assert sov.unprompted_mentions == 1
    assert sov.unprompted_mention_share_pct == round(100 / 3, 1)


def test_prompts_probed_counts_distinct_questions_not_probes() -> None:
    outcomes = [
        _outcome(prompt_id="p1", model="m1"),
        _outcome(prompt_id="p1", model="m2"),
        _outcome(prompt_id="p2", model="m1"),
    ]

    sov = share_of_voice(outcomes)

    assert (sov.prompts_probed, sov.probes_total) == (2, 3)


def test_fingerprint_is_derived_from_the_questions_actually_probed() -> None:
    outcomes = [_outcome(prompt_id="p1"), _outcome(prompt_id="p2")]

    sov = share_of_voice(outcomes)

    assert sov.set_fingerprint == prompt_set_fingerprint(["p1", "p2"])
    assert sov.set_version == PROMPT_SET_VERSION


def test_mixing_prompt_set_versions_is_refused() -> None:
    """Averaging two instruments produces a number that describes neither."""
    try:
        share_of_voice([_outcome(prompt_id="p1"), _outcome(prompt_id="p2", set_version="geo-v2")])
    except ValueError as exc:
        assert "set_version" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("two prompt-set versions must not be pooled into one score")


# --------------------------------------------------------------------------- #
# Run-over-run diff
# --------------------------------------------------------------------------- #


def _sov(
    *,
    mentions: int,
    usable: int,
    unprompted_mentions: int = 0,
    unprompted: int = 0,
    fingerprint: str = "sha256:fixed",
    version: str = PROMPT_SET_VERSION,
) -> ShareOfVoice:
    return ShareOfVoice(
        set_version=version,
        set_fingerprint=fingerprint,
        prompts_probed=usable,
        probes_total=usable,
        usable_answers=usable,
        no_answer_count=0,
        mentions=mentions,
        citations=mentions,
        unprompted_usable_answers=unprompted,
        unprompted_mentions=unprompted_mentions,
        unprompted_citations=0,
    )


def test_diff_reports_percentage_point_movement() -> None:
    delta = diff_share_of_voice(_sov(mentions=3, usable=40), _sov(mentions=9, usable=41))

    assert delta.comparable is True
    assert delta.is_first_run is False
    assert delta.mention_share_delta_pp == round(9 * 100 / 41, 1) - round(3 * 100 / 40, 1)
    assert delta.mentions_delta == 6
    assert delta.direction == "up"


def test_a_first_run_has_no_delta_and_says_so() -> None:
    delta = diff_share_of_voice(None, _sov(mentions=3, usable=40))

    assert delta.is_first_run is True
    assert delta.comparable is False
    assert delta.mention_share_delta_pp is None
    assert delta.direction == "unknown"
    assert "first" in delta.note.lower()


def test_a_changed_prompt_set_is_not_comparable() -> None:
    """Same version, different questions. Subtracting these would be a fiction."""
    delta = diff_share_of_voice(
        _sov(mentions=3, usable=40, fingerprint="sha256:aaa"),
        _sov(mentions=9, usable=41, fingerprint="sha256:bbb"),
    )

    assert delta.comparable is False
    assert delta.mention_share_delta_pp is None
    assert "question" in delta.note.lower()


def test_a_changed_prompt_set_version_is_not_comparable() -> None:
    delta = diff_share_of_voice(
        _sov(mentions=3, usable=40, version="geo-v0"), _sov(mentions=9, usable=41)
    )

    assert delta.comparable is False
    assert "version" in delta.note.lower()


def test_a_previous_run_with_no_usable_answers_is_not_comparable() -> None:
    """The previous run measured nothing, so there is no baseline to move from."""
    delta = diff_share_of_voice(_sov(mentions=0, usable=0), _sov(mentions=9, usable=41))

    assert delta.comparable is False
    assert delta.mention_share_delta_pp is None
    assert "usable" in delta.note.lower()


def test_a_flat_run_reports_flat_rather_than_unknown() -> None:
    delta = diff_share_of_voice(_sov(mentions=4, usable=10), _sov(mentions=4, usable=10))

    assert delta.comparable is True
    assert delta.mention_share_delta_pp == 0.0
    assert delta.direction == "flat"


def test_a_drop_is_reported_as_a_drop() -> None:
    delta = diff_share_of_voice(_sov(mentions=8, usable=10), _sov(mentions=2, usable=10))

    assert delta.direction == "down"
    assert delta.mention_share_delta_pp == -60.0
    assert delta.mentions_delta == -6


def test_unprompted_movement_is_diffed_separately() -> None:
    delta = diff_share_of_voice(
        _sov(mentions=5, usable=10, unprompted_mentions=1, unprompted=8),
        _sov(mentions=5, usable=10, unprompted_mentions=4, unprompted=8),
    )

    assert delta.unprompted_mention_share_delta_pp == round(4 * 100 / 8, 1) - round(1 * 100 / 8, 1)
