"""The regulated-claim guard: what it must catch, and what it must NOT flag.

Half of this file is about false positives, deliberately. A missed claim is a
compliance risk; a false positive blocks a legitimate piece of content, and a gate
that cries wolf is a gate somebody switches off — after which it catches nothing at
all. So every tolerance is tested in both directions: the variant it is meant to
catch, and the innocent text next door that it must leave alone.

The strongest evidence here is not hand-written: it runs the whole 20-case eval
dataset — real German copy for a plumber, a dentist, a bakery, a Steuerberater and a
SaaS — and asserts the engine flags every deliberately-violating variant while
flagging none of the 20 reference answers.
"""

import pytest

from backend.app.engines.claims import (
    MIN_INFLECTION_STEM,
    MIN_SUFFIXABLE_WORD,
    ClaimCheckRequest,
    ClaimCheckResult,
    check_claims,
    claim_pattern,
    normalise,
    strip_markup,
)

DENTIST = ("schmerzfrei", "garantierte Heilung", "beste Zahnarztpraxis", "ohne Risiko")


def _check(
    text: str, claims: tuple[str, ...] = DENTIST, *, markup: bool = True
) -> ClaimCheckResult:
    return check_claims(
        ClaimCheckRequest(content=text, banned_claims=list(claims), contains_markup=markup)
    )


def _blocked(text: str, claims: tuple[str, ...] = DENTIST, *, markup: bool = True) -> bool:
    return not check_claims(
        ClaimCheckRequest(content=text, banned_claims=list(claims), contains_markup=markup)
    ).passed


# --------------------------------------------------------------------------- #
# The verdict is binary, and it says what it found
# --------------------------------------------------------------------------- #


def test_clean_copy_passes_and_says_how_many_claims_it_checked() -> None:
    result = check_claims(
        ClaimCheckRequest(
            content="<p>Wir behandeln Sie so sanft wie moeglich.</p>",
            banned_claims=list(DENTIST),
        )
    )

    assert result.passed is True
    assert result.exercised is True, "four claims were configured, so the gate did work"
    assert result.checked == 4
    assert result.hits == []
    assert result.fix_hint == "", "there is nothing to fix"


def test_one_banned_claim_blocks_and_names_the_phrase_and_the_context() -> None:
    """Not a deduction: a regulated claim cannot be published at any quality."""
    result = check_claims(
        ClaimCheckRequest(
            content="<h1>Praxis</h1><p>Eine schmerzfreie Behandlung, versprochen.</p>",
            banned_claims=list(DENTIST),
        )
    )

    assert result.passed is False
    assert result.claims_found == ("schmerzfrei",)
    hit = result.hits[0]
    assert hit.matched == "schmerzfreie", "the text as written, not the configured phrase"
    assert "Behandlung" in hit.context, "a reviewer needs the sentence, not just the phrase"


def test_every_occurrence_is_reported_not_only_the_first() -> None:
    result = _check("schmerzfrei am Anfang und schmerzfrei am Ende, ohne Risiko.")

    assert len(result.hits) == 3
    assert set(result.claims_found) == {"schmerzfrei", "ohne Risiko"}


def test_no_claims_configured_is_reported_as_not_exercised_rather_than_as_a_pass() -> None:
    """An empty list is a real state: the business has configured no rule yet.

    Reporting it as an earned pass would let a review screen show a green
    compliance tick for a check that never ran.
    """
    result = check_claims(ClaimCheckRequest(content="Absolutely anything.", banned_claims=[]))

    assert result.passed is True
    assert result.exercised is False
    assert result.checked == 0
    assert "not a compliance pass" in result.detail


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_blank_claim_is_not_a_rule_that_matches_everything(blank: str) -> None:
    """A blank entry compiles to a pattern that matches at every position.

    Left unfiltered it would block every draft the business ever produces, which is
    the worst false positive available.
    """
    result = check_claims(ClaimCheckRequest(content="Ganz normale Kopie.", banned_claims=[blank]))

    assert result.passed is True
    assert result.exercised is False


# --------------------------------------------------------------------------- #
# The fix hint is what makes the retry loop mean something
# --------------------------------------------------------------------------- #


def test_the_fix_hint_names_the_phrase_and_forbids_paraphrasing_it() -> None:
    """Fed to GENERATE verbatim. "Improve the copy" would be useless; worse, a hint
    that said "soften it" would invite a paraphrase that passes this gate while
    making the same forbidden promise."""
    hint = _check("Eine schmerzfreie Behandlung.").fix_hint

    assert "schmerzfrei" in hint
    assert "schmerzfreie" in hint, "the model must be shown the words it actually wrote"
    assert "do not paraphrase" in hint.lower()


# --------------------------------------------------------------------------- #
# Case, whitespace, punctuation
# --------------------------------------------------------------------------- #


def test_case_is_ignored() -> None:
    assert _blocked("SCHMERZFREI behandeln wir.")
    assert _blocked("Schmerzfrei behandeln wir.")


def test_a_phrase_still_matches_across_the_line_break_a_renderer_inserted() -> None:
    assert _blocked("Die beste\n   Zahnarztpraxis der Stadt.")


@pytest.mark.parametrize(
    "punctuated", ["schmerzfrei.", "(schmerzfrei)", "schmerzfrei!", '"schmerzfrei"']
)
def test_surrounding_punctuation_does_not_hide_a_claim(punctuated: str) -> None:
    assert _blocked(f"Wir behandeln {punctuated}")


def test_punctuation_inside_the_configured_phrase_is_matched_literally() -> None:
    assert _blocked("Wir geben 100% Garantie.", ("100% Garantie",))
    assert not _blocked("Wir geben 100 Garantie.", ("100% Garantie",))


# --------------------------------------------------------------------------- #
# Word boundaries -- the primary false-positive defence
# --------------------------------------------------------------------------- #


def test_a_claim_does_not_match_inside_a_longer_word() -> None:
    """A bare substring search for "frei" flags "Schmerzfreiheitsgarantie" and every
    compound built on it. That is the false positive that destroys trust in the gate."""
    assert not _blocked("Unsere Schmerzfreiheitsgarantie gibt es nicht.", ("frei",))


def test_a_claim_does_not_match_as_part_of_a_hyphenated_compound() -> None:
    """German builds compounds with hyphens constantly, and a component of a compound
    is a different word. "ohne Risiko-Aufschlag" means the OPPOSITE of "ohne Risiko",
    so a hyphen has to end the match exactly as a letter does."""
    assert not _blocked("Das ist keine Vollnarkose-Heilung.", ("Heilung",))
    assert not _blocked("Wir arbeiten ohne Risiko-Aufschlag.")


def test_the_hyphen_rule_costs_recall_and_the_cost_is_pinned_here() -> None:
    """The honest other half: "Schmerzfrei-Garantie" IS the claim and is NOT caught.
    Precision was chosen over recall on purpose -- a missed claim is visible to the
    human reviewing the draft, while a false positive silently blocks publishable
    copy. This test exists so the trade-off cannot be forgotten or misreported."""
    assert not _blocked("Unsere Schmerzfrei-Garantie.")


# --------------------------------------------------------------------------- #
# Markup: the draft is HTML, and a browser reads it differently from a regex
# --------------------------------------------------------------------------- #


def test_an_inline_tag_inside_a_word_does_not_hide_the_claim() -> None:
    """`bes<b>te</b> Zahnarztpraxis` renders as one word to a reader, so it must read
    as one word here. Replacing every tag with a space would split it and miss."""
    assert _blocked("Die bes<b>te</b> Zahnarztpraxis der Stadt.")


def test_a_block_tag_does_not_fuse_two_words_into_a_false_positive() -> None:
    """The mirror image: removing block tags with no whitespace would turn
    `</p><p>` into a word join, and invent phrases nobody wrote."""
    assert not _blocked("<p>Wir sind ohne</p><p>Risiko-Aufschlag teuer.</p>")


def test_text_hidden_by_css_is_still_checked() -> None:
    """It ships to the client and a crawler reads it, so it is published content."""
    assert _blocked('<span style="display:none">schmerzfrei</span>')


def test_text_in_an_html_comment_is_still_checked() -> None:
    """Not rendered, but shipped in the source of the published page."""
    assert _blocked("<!-- schmerzfrei -->Sichtbarer Text")


def test_a_claim_only_inside_a_script_body_is_not_a_published_claim() -> None:
    """`script` and `style` bodies are code, not copy. Flagging a substring of a
    tracking snippet would block a draft over something no reader can see."""
    assert not _blocked('<script>var label = "schmerzfrei";</script><p>Sanfte Behandlung.</p>')


def test_an_html_entity_is_decoded_before_matching() -> None:
    assert _blocked("<p>schmerz&#102;rei behandeln</p>")


def test_an_escaped_tag_in_the_copy_is_treated_as_text_not_as_markup() -> None:
    stripped = strip_markup("<p>Schreibe &lt;p&gt; fuer einen Absatz.</p>")
    assert "<p>" in stripped, "unescaping must happen after tag removal, not before"


def test_malformed_markup_does_not_raise() -> None:
    """A gate that crashes on one bad draft is a gate that lets that draft through."""
    for broken in ("<p>unclosed", "a < b and c > d", "<!-- never closed", "<<>>", "<p"):
        assert (
            check_claims(ClaimCheckRequest(content=broken, banned_claims=list(DENTIST))).passed
            is True
        )


def test_plain_text_mode_does_not_eat_a_literal_angle_bracket() -> None:
    """A social post is plain text: `<3 schmerzfrei` must not lose its first word to
    a tag stripper that thinks `<3 s...>` is markup."""
    assert _blocked("Preis < 100 Euro und schmerzfrei", markup=False)


# --------------------------------------------------------------------------- #
# Invisible characters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("invisible", ["­", "​", "﻿"])
def test_an_invisible_character_inside_a_word_does_not_hide_the_claim(invisible: str) -> None:
    """A soft hyphen is invisible on the page, so removing it cannot create a false
    positive -- and leaving it in would let a claim through that every reader sees."""
    assert _blocked(f"schmerz{invisible}frei behandeln")


# --------------------------------------------------------------------------- #
# German inflection -- the one tolerance with real precision risk
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "written",
    ["schmerzfreie", "schmerzfreien", "schmerzfreier", "schmerzfreies", "schmerzfreiem"],
)
def test_an_inflected_adjective_is_still_the_claim(written: str) -> None:
    assert _blocked(f"Eine {written} Behandlung.")


@pytest.mark.parametrize("written", ["beste", "besten", "bester", "bestes", "bestem"])
def test_an_ending_already_present_is_interchanged_not_appended(written: str) -> None:
    assert _blocked(f"Die {written} Zahnarztpraxis.")


def test_a_noun_plural_from_the_same_ending_set_is_caught() -> None:
    assert _blocked("Wir geben Garantien.", ("Garantie",))


def test_a_word_too_short_to_inflect_safely_is_matched_literally() -> None:
    """The named hazard: "die" + "s" is "dies", a different word entirely. Below the
    stem threshold the tolerance is switched off rather than made cleverer."""
    assert not _blocked("Dies ist unsere Praxis.", ("die",))
    assert not _blocked("Ohnehin sind wir teuer.", ("ohne",))


def test_a_four_letter_word_does_not_acquire_a_different_words_ending() -> None:
    """ "Brot" + "e" is "Brote" and "Rat" + "e" is "Rate" -- a rate, not advice. Both
    are why MIN_SUFFIXABLE_WORD is 5 and not 4."""
    assert not _blocked("Wir verkaufen Brote.", ("Brot",))
    assert not _blocked("Die Rate ist niedrig.", ("Rat",))


def test_the_inflection_thresholds_are_the_documented_numbers() -> None:
    """Pinned: these are the values the false-positive argument above rests on."""
    assert MIN_INFLECTION_STEM == 4
    assert MIN_SUFFIXABLE_WORD == 5


def test_inflection_is_not_stemming_and_does_not_reach_a_different_word_family() -> None:
    """No suffix outside the five-ending set, so "-ung", "-heit", "-keit", "-lich"
    and every other derivation are out of scope by construction."""
    assert not _blocked("Die Heilungschancen sind gut.", ("Heilung",))
    assert not _blocked("Wir arbeiten schmerzarm.", ("schmerzfrei",))


# --------------------------------------------------------------------------- #
# Umlaut transliteration
# --------------------------------------------------------------------------- #


def test_a_transliterated_umlaut_in_the_copy_matches_the_umlaut_in_the_claim() -> None:
    assert _blocked("Wir sind guenstigster Anbieter.", ("günstigster Anbieter",))


def test_an_umlaut_in_the_copy_matches_a_transliterated_claim() -> None:
    """Symmetric on purpose: the business types the claim list by hand, and typing
    "guenstigster" must not silently disable the rule."""
    assert _blocked("Wir sind günstigster Anbieter.", ("guenstigster Anbieter",))


def test_the_sharp_s_and_double_s_spellings_are_the_same_word() -> None:
    assert _blocked("Der grösste Anbieter.", ("größte Anbieter",))


# --------------------------------------------------------------------------- #
# What the guard deliberately does NOT do -- pinned so nobody claims otherwise
# --------------------------------------------------------------------------- #


def test_a_paraphrase_is_not_detected_and_that_is_the_documented_limit() -> None:
    """This test exists to keep the claim honest. The gate guarantees the configured
    phrases do not appear; it is not a semantic compliance check, and any UI copy
    that implies otherwise is wrong. If this ever starts failing, the engine has
    grown a fuzzy matcher and the false-positive argument needs redoing."""
    assert not _blocked("Eine Behandlung voellig ohne Schmerzen.", ("schmerzfrei",))


def test_a_claim_split_across_a_sentence_boundary_is_not_detected() -> None:
    assert not _blocked("Die Praxis ist die beste. Zahnarztpraxis in Koblenz.")


# --------------------------------------------------------------------------- #
# The compiled pattern, for the record
# --------------------------------------------------------------------------- #


def test_claim_pattern_is_reusable_across_many_drafts() -> None:
    pattern = claim_pattern("beste Zahnarztpraxis")

    assert pattern.search("beste Zahnarztpraxis")
    assert pattern.search("BESTEN ZAHNARZTPRAXIS")
    assert not pattern.search("Zahnarztpraxis beste".replace(" ", ""))


def test_normalise_is_the_string_the_offsets_refer_to() -> None:
    text = normalise("<p>Die <b>beste</b> Praxis</p>")
    result = check_claims(
        ClaimCheckRequest(content="<p>Die <b>beste</b> Praxis</p>", banned_claims=["beste Praxis"])
    )
    hit = result.hits[0]
    assert text[hit.start : hit.end] == hit.matched


# --------------------------------------------------------------------------- #
# The real corpus: 20 eval cases of hand-written German copy
# --------------------------------------------------------------------------- #


def test_no_reference_answer_in_the_eval_set_trips_its_own_banned_claims() -> None:
    """Twenty pieces of human-written German copy across five verticals, each checked
    against its own business's claim list. Any hit here is a false positive on
    genuinely publishable content -- which is the failure this engine is designed
    around, so it is asserted against real text rather than only against fixtures."""
    from evals.dataset import CASES

    offenders = []
    for case in CASES:
        result = check_claims(
            ClaimCheckRequest(
                content=case.reference_answer,
                banned_claims=list(case.banned_claims),
                contains_markup=False,
            )
        )
        if not result.passed:
            offenders.append(f"{case.case_id}: {[h.matched for h in result.hits]}")

    assert not offenders, "false positives on legitimate copy:\n  " + "\n  ".join(offenders)


def test_every_deliberately_violating_variant_in_the_eval_set_is_blocked() -> None:
    """The mirror: each case carries the same answer with a banned claim appended, so
    the guard has to separate the two. A gate nobody has seen fail is a gate nobody
    should believe."""
    from evals.dataset import CASES

    missed = []
    for case in CASES:
        result = check_claims(
            ClaimCheckRequest(
                content=case.violating_answer(),
                banned_claims=list(case.banned_claims),
                contains_markup=False,
            )
        )
        if result.passed:
            missed.append(case.case_id)

    assert not missed, f"a planted banned claim went undetected in: {missed}"


def test_the_runtime_guard_catches_at_least_what_the_eval_rubric_catches() -> None:
    """Two matchers now exist: `evals.rubric.score_brand` grades the eval report, and
    this engine gates the product. The backlog already records what happens when two
    copies of a rule diverge (see the CHANNEL_LIMITS item), so the relationship is
    pinned rather than assumed: anything the rubric would fail, the shipped gate must
    also fail. The converse is deliberately NOT asserted -- the engine is strictly
    stronger (markup, invisible characters, inflection, transliteration), and pinning
    equality would forbid that."""
    from evals.rubric import score_brand

    samples = [
        ("Eine schmerzfrei Behandlung.", DENTIST),
        ("Die beste\nZahnarztpraxis.", DENTIST),
        ("Wir behandeln sanft.", DENTIST),
        ("Unsere Schmerzfreiheitsgarantie.", ("frei",)),
    ]
    for text, claims in samples:
        rubric_failed = not score_brand(text, list(claims)).passed
        engine_failed = _blocked(text, claims, markup=False)
        if rubric_failed:
            assert engine_failed, f"the rubric fails {text!r} but the shipped gate does not"
