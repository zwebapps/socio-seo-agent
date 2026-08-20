"""The rubric, tested at the boundaries that decide a pass.

The rubric is the graded artifact's backbone, so its failure modes matter more
than its happy path:

* **A fabricated citation scores exactly zero.** Citing a chunk that was never
  retrieved is not a weak answer, it is an invented source -- the single worst
  thing a RAG system can do -- so it is a floor, not a deduction.
* **A banned claim is binary.** "Pain-free treatment" on a dentist's page is a
  regulatory problem in Germany (HWG); half a violation does not exist.
* **`aggregate` on nothing must not divide by zero.** A crashing reporter turns a
  run with no results into a run with no report, which is the moment you most want
  one.
* **`score_seo` must delegate.** If the rubric re-implemented on-page scoring, the
  eval and the product's own gate could disagree, and the report would be measuring
  a second opinion rather than the shipped behaviour.

Also covers the report header, because "this was generated against canned
responses" is the sentence that separates evidence from decoration -- see
`docs/CRITERIA_MAP.md` §7. `evals/` holds only the harness; `pyproject.toml` pins
`testpaths = ["backend/tests"]`, so its tests live here.
"""

from __future__ import annotations

import pytest

from backend.app.engines.channel import has_spec, spec_for
from evals.dataset import CASES, VERTICALS, EvalCase
from evals.rubric import (
    Rendering,
    aggregate,
    extract_hashtags,
    score_brand,
    score_coverage,
    score_format,
    score_grounding,
    score_seo,
)
from evals.run import RunConfig, render_report

# --------------------------------------------------------------------------- #
# Fixtures: HTML that the shipped seo engine scores high and low
# --------------------------------------------------------------------------- #

GOOD_HTML = """
<html lang="de"><head>
<title>Notdienst Klempner Koblenz - in 60 Minuten vor Ort</title>
<meta name="description" content="Notdienst Klempner Koblenz: wir sind in 60 Minuten
vor Ort, 24 Stunden am Tag. Festpreis vorab, keine Anfahrtskosten im Stadtgebiet.">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Article","headline":"Notdienst Klempner Koblenz",
"author":{"@type":"Organization","name":"Rohr und Ruhe"}}
</script>
</head><body>
<h1>Notdienst Klempner Koblenz</h1>
<p>Ein Rohrbruch wartet nicht. Unser Notdienst Klempner Koblenz ist rund um die Uhr
erreichbar. Wir kommen in 60 Minuten. Der Preis steht vorher fest.</p>
<h2>Was der Notdienst kostet</h2>
<p>Die Anfahrt im Stadtgebiet ist frei. Wir nennen den Festpreis am Telefon.
Sie entscheiden dann. Es gibt keine Zuschlaege am Wochenende.</p>
<h2>So erreichen Sie uns</h2>
<p>Rufen Sie an. Wir gehen ans Telefon. Ein Mensch nimmt ab, kein Band.</p>
<p><a href="/leistungen">Unsere Leistungen</a> und <a href="/preise">Preise</a>
sowie <a href="/kontakt">Kontakt</a>.</p>
<p><a href="https://www.zvshk.de">Fachverband SHK</a></p>
<img src="/rohr.jpg" alt="Klempner bei der Rohrreinigung in Koblenz">
</body></html>
"""

# Empty on purpose: no title, no meta, no h1, no links, no schema. Every rule
# fails, so this is the floor of the delegated scorer.
BAD_HTML = "<html><body><p>Wir machen alles.</p></body></html>"


# --------------------------------------------------------------------------- #
# score_seo -- delegation, not a second opinion
# --------------------------------------------------------------------------- #


def test_score_seo_delegates_to_the_shipped_engine() -> None:
    """The rubric's number must be the product's number, on the same 0-100 scale."""
    from backend.app.engines.seo import SeoScoreRequest, score_page

    engine = score_page(
        SeoScoreRequest(html=GOOD_HTML, target_keyword="notdienst klempner koblenz", locale="de")
    )
    result = score_seo(GOOD_HTML, "notdienst klempner koblenz", "de")

    assert result.score == engine.score / 100
    assert result.passed is engine.passed
    assert "/100" in result.detail


def test_score_seo_floors_on_empty_markup() -> None:
    result = score_seo(BAD_HTML, "klempner koblenz", "de")

    assert result.score < 0.5
    assert result.passed is False
    assert result.violations, "a failing page must name what failed"


def test_score_seo_is_not_fatal() -> None:
    """A weak page is publishable after a retry; `fatal` is for compliance only."""
    assert score_seo(BAD_HTML, "klempner koblenz", "de").fatal is False


# --------------------------------------------------------------------------- #
# score_brand -- banned claims are binary
# --------------------------------------------------------------------------- #

BANNED = ("schmerzfrei", "garantierte Heilung", "beste Zahnarztpraxis")


def test_clean_text_scores_one() -> None:
    result = score_brand("Wir behandeln Sie so sanft wie moeglich.", BANNED)

    assert result.score == 1.0
    assert result.passed is True
    assert result.violations == ()


def test_one_banned_claim_is_fatal_and_scores_zero() -> None:
    """Not a deduction: a regulated claim cannot be published at any quality."""
    result = score_brand("Eine schmerzfrei Behandlung, versprochen.", BANNED)

    assert result.score == 0.0
    assert result.passed is False
    assert result.fatal is True
    assert any("schmerzfrei" in violation for violation in result.violations)


def test_banned_claim_matching_is_case_insensitive_and_survives_line_breaks() -> None:
    result = score_brand("Die beste\nZahnarztpraxis der Stadt.", BANNED)
    assert result.score == 0.0


def test_banned_claim_matching_respects_word_boundaries() -> None:
    """A substring hit would flag innocent copy and destroy trust in the gate."""
    result = score_brand("Unsere Schmerzfreiheitsgarantie gibt es nicht.", ("frei",))
    assert result.score == 1.0


def test_every_violation_is_listed_not_just_the_first() -> None:
    result = score_brand("schmerzfrei und garantierte Heilung", BANNED)
    assert len(result.violations) == 2


def test_no_banned_claims_configured_says_so() -> None:
    """An empty list is a real state -- the business has not set any rule yet."""
    result = score_brand("Anything at all.", ())

    assert result.score == 1.0
    assert "no banned claims" in result.detail.lower()


# --------------------------------------------------------------------------- #
# score_format -- length, hashtags, link mechanism
# --------------------------------------------------------------------------- #


def test_a_compliant_linkedin_post_passes() -> None:
    text = "Ein Rohrbruch wartet nicht. " * 50 + "#klempner #koblenz"
    result = score_format(Rendering(text=text), "linkedin")

    assert result.passed is True
    assert result.score == 1.0


def test_over_the_hard_limit_is_fatal() -> None:
    """The hard limit is the platform's own reject threshold, not our taste."""
    result = score_format(Rendering(text="a" * 4000), "linkedin")

    assert result.passed is False
    assert result.fatal is True
    assert result.score == 0.0


def test_over_the_soft_limit_is_penalised_but_publishable() -> None:
    limits = spec_for("linkedin")
    text = "a " * ((limits.max_chars // 2) + 200)
    result = score_format(Rendering(text=text), "linkedin")

    assert result.fatal is False
    assert 0.0 < result.score < 1.0


def test_too_many_hashtags_is_fatal_on_a_capped_channel() -> None:
    result = score_format(Rendering(text="Kurz. #a #b #c #d #e #f"), "linkedin")

    assert result.fatal is True
    assert any("hashtag" in violation.lower() for violation in result.violations)


def test_too_few_hashtags_is_a_soft_miss() -> None:
    result = score_format(Rendering(text="Ein Bild sagt mehr. " * 5), "instagram_caption")

    assert result.fatal is False
    assert result.score < 1.0
    assert any("hashtag" in violation.lower() for violation in result.violations)


def test_a_caption_url_is_fatal_where_links_do_not_work() -> None:
    """Instagram captions carry no clickable link (docs/CHANNELS.md section 6).

    A URL there is not a style problem: the CTA is dead and attribution is lost.
    """
    result = score_format(
        Rendering(text="Jetzt buchen: https://example.de/termin #zahnarzt #koblenz #prophylaxe"),
        "instagram_caption",
    )

    assert result.fatal is True
    assert any("link" in violation.lower() for violation in result.violations)


def test_an_article_below_the_minimum_length_fails() -> None:
    result = score_format(Rendering(text="Zu kurz."), "blog_article")

    assert result.passed is False
    assert any("short" in violation.lower() for violation in result.violations)


def test_explicit_hashtags_override_parsing() -> None:
    """A renderer that carries hashtags in their own field is believed."""
    result = score_format(
        Rendering(text="Kein Hashtag im Text.", hashtags=("#a", "#b", "#c", "#d", "#e", "#f")),
        "linkedin",
    )
    assert result.fatal is True


def test_an_unknown_channel_raises() -> None:
    """Silently scoring 1.0 for a channel with no spec would hide a harness bug."""
    with pytest.raises(KeyError):
        score_format(Rendering(text="hi"), "myspace")


def test_extract_hashtags_ignores_urls_and_ids() -> None:
    assert extract_hashtags("#Klempner #24h see https://x.de/a#anchor") == ("#Klempner", "#24h")


# --------------------------------------------------------------------------- #
# score_grounding -- a fabricated source is the worst failure
# --------------------------------------------------------------------------- #

CHUNKS = {
    "c1": "Der Notdienst ist 24 Stunden erreichbar. Die Anfahrt im Stadtgebiet kostet 0 Euro.",
    "c2": "Eine Rohrreinigung kostet ab 89 Euro inklusive Mehrwertsteuer.",
}


def test_a_citation_to_a_chunk_that_was_never_retrieved_scores_zero() -> None:
    """A fabricated source, so it is a floor rather than a deduction.

    Scoring this merely "low" would let a run with an invented citation average out
    above a run that honestly said it did not know.
    """
    result = score_grounding(
        "Eine Rohrreinigung kostet ab 89 Euro.",
        cited_chunk_ids=("c2", "c99"),
        available_chunks=CHUNKS,
    )

    assert result.score == 0.0
    assert result.passed is False
    assert result.fatal is True
    assert any("c99" in violation for violation in result.violations)


def test_fabrication_beats_an_otherwise_perfect_answer() -> None:
    """Even with every claim supported, one bad id is still zero."""
    supported = score_grounding(
        "Die Anfahrt kostet 0 Euro.", cited_chunk_ids=("c1",), available_chunks=CHUNKS
    )
    fabricated = score_grounding(
        "Die Anfahrt kostet 0 Euro.", cited_chunk_ids=("c1", "nope"), available_chunks=CHUNKS
    )

    assert supported.score == 1.0
    assert fabricated.score == 0.0


def test_a_supported_claim_scores_one() -> None:
    result = score_grounding(
        "Der Notdienst ist 24 Stunden erreichbar.",
        cited_chunk_ids=("c1",),
        available_chunks=CHUNKS,
    )

    assert result.score == 1.0
    assert result.passed is True


def test_a_claim_whose_figure_is_absent_from_the_cited_chunks_is_unsupported() -> None:
    result = score_grounding(
        "Eine Rohrreinigung kostet ab 49 Euro.",
        cited_chunk_ids=("c2",),
        available_chunks=CHUNKS,
    )

    assert result.score == 0.0
    assert result.passed is False
    # An unsupported figure is a fixable draft, not a fabricated source: the
    # generator can drop the claim. Only a bad citation is fatal.
    assert result.fatal is False


def test_partial_support_scores_the_fraction() -> None:
    text = "Der Notdienst ist 24 Stunden erreichbar. Wir haben 400 Kunden."
    result = score_grounding(text, cited_chunk_ids=("c1",), available_chunks=CHUNKS)

    assert result.score == 0.5


def test_a_claim_with_no_citation_at_all_is_unsupported() -> None:
    """This is the RAG-off shape: figures in the copy, nothing behind them."""
    result = score_grounding("Wir sind in 60 Minuten da.", cited_chunk_ids=(), available_chunks={})

    assert result.score == 0.0
    assert result.passed is False


def test_text_with_no_checkable_claim_is_not_punished() -> None:
    """Nothing factual was asserted, so nothing could be fabricated.

    The detail line has to say the dimension was not exercised, or a 1.00 here
    reads as proof of grounding when it is the absence of a test.
    """
    result = score_grounding(
        "Wir kuemmern uns sorgfaeltig um Ihr Zuhause.",
        cited_chunk_ids=(),
        available_chunks=CHUNKS,
    )

    assert result.score == 1.0
    assert result.passed is True
    assert "no checkable" in result.detail.lower()


def test_thousand_separators_do_not_break_support() -> None:
    """German "1.500" and English "1,500" must match the same figure."""
    result = score_grounding(
        "Wir haben 1.500 Auftraege erledigt.",
        cited_chunk_ids=("c3",),
        available_chunks={"c3": "Insgesamt 1,500 Auftraege seit 2009."},
    )
    assert result.score == 1.0


# --------------------------------------------------------------------------- #
# score_coverage -- the dataset's must-contain terms
# --------------------------------------------------------------------------- #


def test_coverage_counts_present_terms() -> None:
    result = score_coverage("Notdienst in Koblenz, Festpreis vorab.", ("Notdienst", "Festpreis"))

    assert result.score == 1.0
    assert result.passed is True


def test_coverage_reports_the_missing_term() -> None:
    result = score_coverage("Notdienst in Koblenz.", ("Notdienst", "Festpreis"))

    assert result.score == 0.5
    assert result.passed is False
    assert any("Festpreis" in violation for violation in result.violations)


def test_coverage_with_nothing_required_is_a_pass_that_says_so() -> None:
    result = score_coverage("anything", ())

    assert result.score == 1.0
    assert "no required" in result.detail.lower()


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #


def test_aggregate_of_nothing_does_not_divide_by_zero() -> None:
    summary = aggregate([])

    assert summary.count == 0
    assert summary.mean_score == 0.0
    assert summary.passed == 0
    assert summary.failed == 0
    assert summary.by_dimension == {}


def test_aggregate_means_are_per_dimension() -> None:
    results = [
        score_brand("clean", BANNED),
        score_brand("schmerzfrei", BANNED),
        score_coverage("Notdienst", ("Notdienst",)),
    ]
    summary = aggregate(results)

    assert summary.count == 3
    assert summary.passed == 2
    assert summary.failed == 1
    assert summary.by_dimension["brand"] == 0.5
    assert summary.by_dimension["coverage"] == 1.0


def test_aggregate_surfaces_fatal_violations() -> None:
    summary = aggregate([score_brand("schmerzfrei", BANNED)])
    assert summary.fatal_violations


# --------------------------------------------------------------------------- #
# The dataset
# --------------------------------------------------------------------------- #


def test_the_dataset_has_twenty_cases() -> None:
    assert len(CASES) == 20


def test_case_ids_are_unique() -> None:
    assert len({case.case_id for case in CASES}) == len(CASES)


def test_every_vertical_is_represented() -> None:
    covered = {case.business.vertical for case in CASES}
    assert covered == set(VERTICALS)


def test_every_case_states_what_must_and_must_not_appear() -> None:
    for case in CASES:
        assert case.must_contain, case.case_id
        assert case.banned_claims, case.case_id


def test_every_case_ships_facts_for_the_rag_arm() -> None:
    """Without facts the RAG-on arm has nothing to retrieve, so the comparison is
    vacuous for that case."""
    for case in CASES:
        assert case.facts, case.case_id


def test_every_case_uses_a_channel_the_rubric_knows() -> None:
    for case in CASES:
        assert has_spec(case.channel), case.case_id


def test_the_reference_answer_is_actually_correct_by_our_own_rubric() -> None:
    """The dataset's own exemplar must pass brand and coverage.

    If it does not, the "correct output" the case describes is not the one it
    carries, and every number computed against it is measuring the wrong target.
    """
    for case in CASES:
        brand = score_brand(case.reference_answer, case.banned_claims)
        coverage = score_coverage(case.reference_answer, case.must_contain)
        assert brand.passed, f"{case.case_id}: reference answer trips its own banned claims"
        assert coverage.passed, f"{case.case_id}: reference answer misses {coverage.violations}"


def test_the_violating_mutation_is_detected() -> None:
    """The rubric must discriminate, and the dataset proves it can.

    A rubric nobody has seen fail is a rubric nobody should believe.
    """
    for case in CASES:
        bad = case.violating_answer()
        assert score_brand(bad, case.banned_claims).fatal, case.case_id


# --------------------------------------------------------------------------- #
# The report header -- the sentence that makes this evidence
# --------------------------------------------------------------------------- #


def _one_case() -> EvalCase:
    return CASES[0]


def test_report_names_the_fake_provider_when_run_unconfigured() -> None:
    """Without this sentence the report measures the harness and pretends otherwise.

    `docs/CRITERIA_MAP.md` section 7: a number produced against canned responses is
    not a measurement of a model, and saying so is the difference between evidence
    and decoration.
    """
    report = render_report(config=RunConfig(live=False), rows=[], notes=[])

    assert "FAKE provider" in report
    assert "harness" in report.lower()


def test_report_marks_ragas_as_absent_rather_than_inventing_numbers() -> None:
    report = render_report(config=RunConfig(live=False), rows=[], notes=[])

    assert "ragas" in report.lower()
    assert "not installed" in report.lower()


def test_report_states_the_tracing_backend() -> None:
    report = render_report(config=RunConfig(live=False), rows=[], notes=[])
    assert "no-op" in report.lower()


# --------------------------------------------------------------------------- #
# "Not exercised" -- a 1.00 that was never tested must not read as a pass
# --------------------------------------------------------------------------- #


def test_a_dimension_with_nothing_to_check_is_marked_not_exercised() -> None:
    """Three ways a scorer can score 1.00 without having tested anything.

    Each is a legitimate state, and each would silently inflate an average if the
    report could not tell it apart from an earned 1.00.
    """
    assert score_brand("anything", ()).exercised is False
    assert score_coverage("anything", ()).exercised is False
    assert score_grounding("Wir arbeiten sorgfältig.", (), {}).exercised is False


def test_a_dimension_that_really_checked_something_is_exercised() -> None:
    assert score_brand("clean copy", BANNED).exercised is True
    assert score_coverage("Notdienst", ("Notdienst",)).exercised is True
    assert score_grounding("Ab 89 Euro.", ("c2",), available_chunks=CHUNKS).exercised is True


def test_aggregate_counts_the_untested_cells() -> None:
    summary = aggregate([score_brand("x", ()), score_brand("clean", BANNED)])

    assert summary.mean_score == 1.0
    assert summary.not_exercised == 1, "a perfect mean must disclose how much was untested"


def test_a_citation_marker_is_not_counted_as_a_hashtag() -> None:
    """The measurement bug behind the reported `format` regression.

    Chunk ids are `<case_id>#<ordinal>`, so `[chunk:plumber-01#0]` contains a `#`.
    Counting it as a hashtag penalised the RAG arm *for citing its sources* -- the
    one thing that arm exists to do. Every hashtag violation in the 2026-08-19 live
    report ("6 hashtags exceed the maximum of 0 for blog_article") was citations.
    """
    text = "Kosten 89 Euro. [chunk:plumber-01#0] Anfahrt frei. [chunk:plumber-01#1]"

    assert extract_hashtags(text) == ()


def test_real_hashtags_are_still_counted_alongside_citations() -> None:
    """The fix must not become a blanket excuse: a genuine tag still counts."""
    text = "Frisch gebacken. [chunk:bakery-01#0] #Baeckerei #Koblenz"

    assert extract_hashtags(text) == ("#Baeckerei", "#Koblenz")


def test_an_article_full_of_citations_no_longer_fails_the_hashtag_cap() -> None:
    """End to end through the scorer that was producing the wrong verdict."""
    body = "Guter Text über den Service. " * 100
    cited = body + " ".join(f"[chunk:plumber-01#{index}]" for index in range(6))

    result = score_format(Rendering(text=cited), "blog_article")

    assert not any("hashtag" in violation.lower() for violation in result.violations)
