"""Tests for the `seo` engine.

Two things are being protected here, and they are not the same thing.

**The thresholds.** This module is the gate every generated page passes through,
so an off-by-one in it does not produce a wrong number in a report -- it silently
blocks everything or passes everything, and either failure is invisible until a
customer notices. Every rule is therefore tested at its exact boundary, on both
sides, including the two the arithmetic makes fragile: the character-count edges
(49/50/60/61) and the keyword-density edges (0.79/0.80/2.50/2.51 %), where a
float comparison would flip on representation error rather than on content.

**The hints.** `fix_hint` is fed to the content model verbatim on a retry, so a
hint that does not name the measured value burns a loop out of a budget of two.
That is asserted mechanically, not by eye.

Pages are built by `build_page`, whose defaults are a page that passes all nine
rules. Every test overrides exactly the one thing it is about, which keeps a
failure pointing at one rule instead of at the fixture.
"""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from backend.app.engines.seo import (
    FINDING_ORDER,
    PASS_SCORE,
    RULE_WEIGHTS,
    SeoFinding,
    SeoFindingCode,
    SeoScoreRequest,
    SeoScoreResult,
    SeoSeverity,
    analyse_readability,
    build_article_jsonld,
    build_faq_jsonld,
    build_local_business_jsonld,
    flesch_reading_ease,
    score_page,
    validate_jsonld,
)
from backend.app.engines.seo.readability import count_sentences, count_syllables, tokenize_words
from backend.app.engines.seo.rules import (
    DENSITY_MAX_PCT,
    DENSITY_MIN_PCT,
    READABILITY_MIN,
    classify_href,
    extract_facts,
)
from backend.app.engines.seo.score import aggregate

KEYWORD = "widget"

# Filler prose that deliberately contains neither the target keyword nor any
# word long enough to matter to the readability rule.
_FILLER = "Emergency tap repair and drain care for busy homes in Koblenz today"


def text_of_length(length: int) -> str:
    """Readable filler of an exact character length, never ending in a space
    (a trailing space would be stripped and make the length a lie)."""
    padded = ((_FILLER + " ") * (length // len(_FILLER) + 2))[:length]
    return padded[:-1] + "x" if padded.endswith(" ") else padded


def body_with_keyword(occurrences: int = 6, sentences: int = 40) -> str:
    """Simple 10-word sentences, `occurrences` of which name the keyword.

    Kept short-worded on purpose so that this fixture comfortably passes the
    readability rule; the readability tests supply their own prose.
    """
    step = max(sentences // max(occurrences, 1), 1)
    used = 0
    out: list[str] = []
    for index in range(sentences):
        if used < occurrences and index % step == 0:
            out.append(f"We fix each {KEYWORD} fast and we do it well.")
            used += 1
        else:
            out.append("We fix each tap fast and we do it well.")
    return " ".join(out)


def body_of_exact_length(total_words: int, occurrences: int) -> str:
    """A body of exactly `total_words` words, `occurrences` of them the keyword.

    Exactness is the whole point: the density boundary cases are only boundary
    cases if the denominator is the number the test says it is.
    """
    words = ["alpha"] * total_words
    if occurrences:
        step = total_words // occurrences
        for index in range(occurrences):
            words[index * step] = KEYWORD
    return " ".join(
        " ".join(words[start : start + 10]) + "." for start in range(0, total_words, 10)
    )


VALID_JSONLD = json.dumps(
    {"@context": "https://schema.org", "@type": "Article", "headline": "Tap repair in Koblenz"}
)

GOOD_TITLE = text_of_length(55)
GOOD_META = text_of_length(150)
GOOD_BODY = body_with_keyword()
GOOD_HEADINGS: tuple[tuple[int, str], ...] = ((1, "Tap repair"), (2, "How it works"))
GOOD_IMAGES: tuple[tuple[str, str | None], ...] = (("tap.png", "A repaired kitchen tap"),)


def build_page(
    *,
    title: str | None = GOOD_TITLE,
    meta: str | None = GOOD_META,
    body: str = GOOD_BODY,
    headings: Sequence[tuple[int, str]] = GOOD_HEADINGS,
    internal_links: int = 2,
    external_links: int = 2,
    extra_links: Sequence[str] = (),
    images: Sequence[tuple[str, str | None]] = GOOD_IMAGES,
    jsonld: str | None = VALID_JSONLD,
) -> str:
    """Build a page. The defaults pass all nine rules; override one at a time.

    `None` means "omit the element entirely", which is how the missing-title and
    missing-meta cases are expressed without a second builder.
    """
    head: list[str] = []
    if title is not None:
        head.append(f"<title>{title}</title>")
    if meta is not None:
        head.append(f'<meta name="description" content="{meta}">')
    if jsonld is not None:
        head.append(f'<script type="application/ld+json">{jsonld}</script>')

    parts = [f"<h{level}>{text}</h{level}>" for level, text in headings]
    parts.append(f"<p>{body}</p>")
    parts += [
        f'<p><a href="/service-{index}">Our service page {index}</a></p>'
        for index in range(internal_links)
    ]
    parts += [
        f'<p><a href="https://standards{index}.example.org/spec">Source {index}</a></p>'
        for index in range(external_links)
    ]
    parts += [f'<p><a href="{href}">See this</a></p>' for href in extra_links]
    parts += [
        f'<img src="{src}">' if alt is None else f'<img src="{src}" alt="{alt}">'
        for src, alt in images
    ]

    return (
        '<!doctype html><html lang="en"><head>'
        + "".join(head)
        + "</head><body>"
        + "".join(parts)
        + "</body></html>"
    )


def score(
    html: str, *, keyword: str = KEYWORD, locale: str = "en", **kwargs: Any
) -> SeoScoreResult:
    return score_page(SeoScoreRequest(html=html, target_keyword=keyword, locale=locale, **kwargs))


def finding(result: SeoScoreResult, code: str) -> SeoFinding:
    """The single finding for `code`. Fails loudly if a rule ever emits two."""
    matches = [item for item in result.findings if item.code == code]
    assert len(matches) == 1, f"expected exactly one {code} finding, got {len(matches)}"
    return matches[0]


def severity_of(html: str, code: str, **kwargs: Any) -> str:
    return finding(score(html, **kwargs), code).severity


# --------------------------------------------------------------------------- #
# The two whole-page cases
# --------------------------------------------------------------------------- #


def test_good_page_passes_with_a_perfect_score() -> None:
    result = score(build_page())

    assert result.passed is True
    assert result.score >= PASS_SCORE
    # Nothing to feed back to the model when every rule passes -- if this list is
    # non-empty the retry loop would spin on a page that is already fine.
    assert result.fix_hints == []
    assert [item.severity for item in result.findings] == ["info"] * 9
    assert result.score == 100


def test_findings_are_returned_in_the_declared_order() -> None:
    result = score(build_page())
    assert tuple(item.code for item in result.findings) == FINDING_ORDER


def test_bad_page_fails_with_the_expected_findings() -> None:
    html = build_page(
        title="Taps",  # 4 chars -> error
        meta=None,  # absent -> error
        body="One short line about plumbing.",  # keyword absent -> error
        headings=((1, "First"), (1, "Second"), (3, "Skipped")),  # two h1 + a skip -> error
        internal_links=0,  # -> error
        external_links=0,  # -> warn
        images=(("hero.jpg", None), ("chart.png", "")),  # -> warn
        jsonld="{not json",  # -> warn, never an exception
    )
    result = score(html)

    assert result.passed is False
    assert result.score < PASS_SCORE

    severities = {item.code: item.severity for item in result.findings}
    assert severities == {
        "title_length": "error",
        "meta_length": "error",
        "heading_tree": "error",
        "keyword_density": "error",
        "readability": "info",  # one short simple sentence really is readable
        "internal_links": "error",
        "external_links": "warn",
        "image_alt": "warn",
        "schema_invalid": "warn",
    }
    # Every failure produces a hint; every pass produces none.
    assert len(result.fix_hints) == 8
    assert all(item.fix_hint == "" for item in result.findings if item.severity == "info")


def test_empty_document_scores_rather_than_raising() -> None:
    """The crawler can hand us a blank or JS-only page; that is a score of ~0,
    not a traceback."""
    result = score("")
    assert result.passed is False
    assert 0 <= result.score <= 100


# --------------------------------------------------------------------------- #
# title_length -- 50-60, error outside 30-70
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (29, "error"),
        (30, "warn"),
        (49, "warn"),
        (50, "info"),
        (55, "info"),
        (60, "info"),
        (61, "warn"),
        (70, "warn"),
        (71, "error"),
    ],
)
def test_title_length_boundaries(length: int, expected: str) -> None:
    result = score(build_page(title=text_of_length(length)))
    assert finding(result, "title_length").measured == float(length)
    assert finding(result, "title_length").severity == expected


def test_missing_title_is_an_error_measuring_zero() -> None:
    item = finding(score(build_page(title=None)), "title_length")
    assert item.severity == "error"
    assert item.measured == 0.0


def test_empty_title_element_counts_as_missing() -> None:
    assert severity_of(build_page(title="   "), "title_length") == "error"


# --------------------------------------------------------------------------- #
# meta_length -- 140-160, error only when absent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("length", "expected"),
    [(1, "warn"), (139, "warn"), (140, "info"), (150, "info"), (160, "info"), (161, "warn")],
)
def test_meta_length_boundaries(length: int, expected: str) -> None:
    result = score(build_page(meta=text_of_length(length)))
    assert finding(result, "meta_length").measured == float(length)
    assert finding(result, "meta_length").severity == expected


def test_missing_meta_is_an_error() -> None:
    item = finding(score(build_page(meta=None)), "meta_length")
    assert item.severity == "error"
    assert item.measured == 0.0


def test_meta_hint_matches_the_documented_example() -> None:
    """`docs/AGENT_RUNTIME.md` section 7 uses this exact hint as its example of a
    usable one, so it is pinned rather than left to drift."""
    item = finding(score(build_page(meta=text_of_length(118))), "meta_length")
    assert item.fix_hint == (
        "Extend the meta description to 140-160 characters; it currently stops at 118."
    )


# --------------------------------------------------------------------------- #
# keyword_density -- 0.8-2.5 %
# --------------------------------------------------------------------------- #


def density_page(total_words: int, occurrences: int) -> str:
    """A page whose body is the only prose on it, so the word count is exact."""
    return build_page(
        body=body_of_exact_length(total_words, occurrences),
        headings=(),
        internal_links=0,
        external_links=0,
        images=(),
    )


@pytest.mark.parametrize(
    ("total_words", "occurrences", "percent", "expected"),
    [
        (1000, 7, 0.7, "warn"),
        (1000, 8, 0.8, "info"),
        (1000, 25, 2.5, "info"),
        (1000, 26, 2.6, "warn"),
        # The literal 0.79 / 2.51 edges need a 10,000-word denominator to be
        # expressible at all. This is where a float comparison would flip on
        # representation error rather than on the content.
        (10_000, 79, 0.79, "warn"),
        (10_000, 80, 0.8, "info"),
        (10_000, 250, 2.5, "info"),
        (10_000, 251, 2.51, "warn"),
    ],
)
def test_keyword_density_boundaries(
    total_words: int, occurrences: int, percent: float, expected: str
) -> None:
    result = score(density_page(total_words, occurrences))
    item = finding(result, "keyword_density")
    assert item.measured == pytest.approx(percent)
    assert item.severity == expected
    assert item.expected == f"{DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}% of body words"


def test_zero_occurrences_is_an_error_not_a_warning() -> None:
    item = finding(score(density_page(1000, 0)), "keyword_density")
    assert item.severity == "error"
    assert item.measured == 0.0
    assert "0 times" in item.fix_hint


def test_multi_word_keyword_counts_its_own_words() -> None:
    """Three uses of a three-word phrase occupy nine words of the page, not
    three -- otherwise a long-tail keyword can never reach 0.8 %."""
    body = " ".join(["emergency plumber koblenz"] * 3 + ["alpha"] * 291) + "."
    result = score(
        build_page(body=body, headings=(), internal_links=0, external_links=0, images=()),
        keyword="emergency plumber koblenz",
    )
    item = finding(result, "keyword_density")
    assert item.measured == pytest.approx(3.0)  # 9 of 300 words


def test_keyword_matching_is_token_based_not_substring() -> None:
    """'ear' must not be found inside 'research'."""
    body = " ".join(["research"] * 100) + "."
    result = score(
        build_page(body=body, headings=(), internal_links=0, external_links=0, images=()),
        keyword="ear",
    )
    assert finding(result, "keyword_density").severity == "error"


def test_short_body_reports_the_word_count_it_needs() -> None:
    """Below ~40 words no occurrence count fits the band, so the hint must ask
    for more copy instead of an impossible '1-0 occurrences'."""
    item = finding(score(density_page(20, 1)), "keyword_density")
    assert item.severity == "warn"
    assert "40 words" in item.fix_hint
    assert "-0" not in item.fix_hint


def test_unused_secondary_keywords_are_named_in_the_hint() -> None:
    result = score(
        density_page(1000, 1),
        secondary_keywords=["drain cleaning", "boiler service"],
    )
    hint = finding(result, "keyword_density").fix_hint
    assert "drain cleaning" in hint
    assert "boiler service" in hint


# --------------------------------------------------------------------------- #
# readability -- Flesch >= 55, and the German formula
# --------------------------------------------------------------------------- #


# Built so the arithmetic is exact: alternating one- and two-syllable words give
# exactly 1.5 syllables per word, and the sentence length is the only variable.
#   24 words/sentence, 1.50 syll/word -> 206.835 - 24.36 - 126.90 = 55.58 (>= 55, passes)
#   25 words/sentence, 1.52 syll/word -> 206.835 - 25.375 - 128.59 = 52.87 (<  55, warns)
# The 25-word case cannot be exactly 1.5 syllables per word (an odd word count
# cannot split evenly), which is why the two numbers are not symmetric.
def _measured_prose(words_per_sentence: int, sentences: int = 2) -> str:
    half = words_per_sentence // 2
    pattern = ["alpha", "tap"] * half + (["alpha"] if words_per_sentence % 2 else [])
    return " ".join(" ".join(pattern) + "." for _ in range(sentences))


def readability_page(text: str) -> str:
    return build_page(body=text, headings=(), internal_links=0, external_links=0, images=())


@pytest.mark.parametrize(
    ("words_per_sentence", "expected"),
    [(24, "info"), (25, "warn")],
)
def test_readability_threshold_is_inclusive(words_per_sentence: int, expected: str) -> None:
    text = _measured_prose(words_per_sentence, sentences=2)
    item = finding(score(readability_page(text)), "readability")
    assert (item.measured is not None) and (item.measured >= READABILITY_MIN) == (
        expected == "info"
    )
    assert item.severity == expected


def test_readability_hint_quantifies_both_averages() -> None:
    item = finding(score(readability_page(_measured_prose(25))), "readability")
    assert "52.87" in item.fix_hint
    assert "25.0 words" in item.fix_hint
    assert "1.52 syllables" in item.fix_hint


GERMAN_BODY = (
    "Wir reparieren Ihren Wasserhahn schnell und sauber. "
    "Unser Notdienst kommt am selben Tag zu Ihnen nach Hause. "
    "Die Kosten nennen wir vorher klar und ohne Aufschlag."
)


def test_german_locale_uses_the_amstad_formula() -> None:
    english = flesch_reading_ease(GERMAN_BODY, "en")
    german = flesch_reading_ease(GERMAN_BODY, "de")
    assert german != english
    # Amstad's smaller syllable coefficient (58.5 vs 84.6) makes it kinder to
    # German's long words, which is the whole reason it exists.
    assert german > english


@pytest.mark.parametrize("locale", ["de", "de-DE", "de_AT"])
def test_german_locale_variants_all_select_amstad(locale: str) -> None:
    result = score(readability_page(GERMAN_BODY), locale=locale)
    item = finding(result, "readability")
    assert item.measured == pytest.approx(flesch_reading_ease(GERMAN_BODY, "de"))


def test_unknown_locale_falls_back_to_the_english_formula() -> None:
    """Documented behaviour, so it is pinned: an unsupported language must still
    produce a comparable number rather than refusing to score."""
    assert flesch_reading_ease(GERMAN_BODY, "fr") == flesch_reading_ease(GERMAN_BODY, "en")


def test_readability_of_empty_text_is_zero_not_an_error() -> None:
    stats = analyse_readability("", "en")
    assert (stats.words, stats.sentences, stats.score) == (0, 0, 0.0)


@pytest.mark.parametrize(
    ("word", "expected"),
    [("tap", 1), ("alpha", 2), ("repair", 2), ("simple", 2), ("see", 1), ("rhythm", 1)],
)
def test_syllable_heuristic_on_representative_words(word: str, expected: int) -> None:
    assert count_syllables(word) == expected


def test_german_keeps_its_pronounced_trailing_e() -> None:
    assert count_syllables("wende", german=True) == 2
    assert count_syllables("wende", german=False) == 1


def test_a_block_boundary_ends_a_sentence() -> None:
    """An unpunctuated heading must not be glued to the paragraph under it: if it
    were, every page would look as though it had one enormous sentence."""
    text = extract_facts("<body><h1>A heading</h1><p>A sentence here</p></body>").body_text
    assert count_sentences(text) == 2
    assert len(tokenize_words(text)) == 5


def test_inline_markup_does_not_split_a_sentence() -> None:
    """The mirror image: splitting at `<a>` would shatter one long sentence into
    several short ones and hand the page a score it has not earned."""
    text = extract_facts(
        "<body><p>See <a href='/rates'>our rates</a> for details today.</p></body>"
    ).body_text
    assert text == "See our rates for details today."
    assert count_sentences(text) == 1


# --------------------------------------------------------------------------- #
# heading_tree -- exactly one h1, no skipped levels
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("headings", "expected"),
    [
        (((1, "One"), (2, "Two")), "info"),
        (((1, "One"), (2, "Two"), (3, "Three"), (2, "Back")), "info"),
        (((1, "One"),), "info"),
        ((), "error"),
        (((2, "No h1 here"),), "error"),
        (((1, "One"), (1, "Two")), "error"),
        (((1, "One"), (3, "Skipped")), "error"),
        (((1, "One"), (2, "Two"), (4, "Skipped")), "error"),
    ],
)
def test_heading_tree_cases(headings: tuple[tuple[int, str], ...], expected: str) -> None:
    assert severity_of(build_page(headings=headings), "heading_tree") == expected


def test_two_h1_elements_are_counted_and_named() -> None:
    item = finding(score(build_page(headings=((1, "First"), (1, "Second")))), "heading_tree")
    assert item.severity == "error"
    assert item.measured == 2.0
    assert "2" in item.fix_hint
    assert "First" in item.fix_hint and "Second" in item.fix_hint


def test_skipped_level_names_the_offending_heading_and_the_fix() -> None:
    item = finding(score(build_page(headings=((1, "One"), (3, "Pricing")))), "heading_tree")
    assert item.severity == "error"
    assert "Pricing" in item.fix_hint
    assert "<h2>" in item.fix_hint


def test_both_heading_problems_are_reported_in_one_finding() -> None:
    """A two-loop retry budget cannot afford to discover the second problem on
    loop two, so one code must report both."""
    item = finding(
        score(build_page(headings=((1, "One"), (1, "Two"), (4, "Deep")))), "heading_tree"
    )
    assert "<h1>" in item.message
    assert "→" in item.message


# --------------------------------------------------------------------------- #
# internal_links / external_links
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("count", "expected"), [(0, "error"), (1, "info"), (3, "info")])
def test_internal_link_boundary(count: int, expected: str) -> None:
    item = finding(score(build_page(internal_links=count)), "internal_links")
    assert item.severity == expected
    assert item.measured == float(count)


@pytest.mark.parametrize(
    ("count", "expected"), [(0, "warn"), (1, "warn"), (2, "info"), (5, "info")]
)
def test_external_link_boundary(count: int, expected: str) -> None:
    item = finding(score(build_page(external_links=count)), "external_links")
    assert item.severity == expected
    assert item.measured == float(count)


@pytest.mark.parametrize(
    "href",
    ["#top", "mailto:hello@example.org", "tel:+4926112345", "javascript:void(0)", "?page=2"],
)
def test_non_navigation_hrefs_count_as_neither(href: str) -> None:
    """An anchor, a mail link and a phone link are not internal navigation. If
    they counted, a page whose only 'internal link' is a tel: would pass."""
    assert classify_href(href) == "ignored"
    result = score(build_page(internal_links=0, extra_links=[href]))
    assert finding(result, "internal_links").severity == "error"
    assert finding(result, "internal_links").measured == 0.0


def test_protocol_relative_links_are_external() -> None:
    assert classify_href("//cdn.example.org/a") == "external"
    assert classify_href("/services") == "internal"
    assert classify_href("services/tap") == "internal"


def test_internal_link_hint_states_the_count_and_the_target() -> None:
    item = finding(score(build_page(internal_links=0)), "internal_links")
    assert "0" in item.fix_hint
    assert "1" in item.fix_hint


def test_external_link_hint_states_how_many_more_are_needed() -> None:
    item = finding(score(build_page(external_links=1)), "external_links")
    assert "1 link" in item.fix_hint
    assert "add 1 more" in item.fix_hint


# --------------------------------------------------------------------------- #
# image_alt
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("images", "expected", "missing"),
    [
        ((("a.png", "A tap"),), "info", 0),
        ((), "info", 0),
        # Absent attribute and empty attribute are different markup with the
        # same consequence, and both must be caught.
        ((("a.png", None),), "warn", 1),
        ((("a.png", ""),), "warn", 1),
        ((("a.png", "   "),), "warn", 1),
        ((("a.png", "A tap"), ("b.png", None), ("c.png", "")), "warn", 2),
    ],
)
def test_image_alt_cases(
    images: tuple[tuple[str, str | None], ...], expected: str, missing: int
) -> None:
    item = finding(score(build_page(images=images)), "image_alt")
    assert item.severity == expected
    assert item.measured == float(missing)


def test_image_alt_hint_separates_absent_from_empty_and_names_files() -> None:
    item = finding(score(build_page(images=(("hero.jpg", None), ("chart.png", "")))), "image_alt")
    assert "1 with no alt attribute" in item.fix_hint
    assert '1 with alt=""' in item.fix_hint
    assert "hero.jpg" in item.fix_hint and "chart.png" in item.fix_hint


# --------------------------------------------------------------------------- #
# schema_invalid, and the JSON-LD builders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("jsonld", "expected"),
    [
        (VALID_JSONLD, "info"),
        (None, "warn"),
        ("", "warn"),
        # Malformed input must produce a finding, never an exception.
        ("{not json at all", "warn"),
        ('{"@context": "https://schema.org", "@type":}', "warn"),
        ("[1, 2, 3]", "warn"),
        ('"just a string"', "warn"),
        ('{"@type": "Article", "headline": "No context"}', "warn"),
        ('{"@context": "https://schema.org", "headline": "No type"}', "warn"),
        ('{"@context": "https://schema.org", "@type": "Article"}', "warn"),
        ('{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": []}', "warn"),
        # A top-level array and an @graph wrapper are both legal shapes.
        (f"[{VALID_JSONLD}]", "info"),
        (
            json.dumps(
                {
                    "@context": "https://schema.org",
                    "@graph": [{"@type": "Article", "headline": "Inherited context"}],
                }
            ),
            "info",
        ),
    ],
)
def test_schema_cases(jsonld: str | None, expected: str) -> None:
    assert severity_of(build_page(jsonld=jsonld), "schema_invalid") == expected


def test_malformed_json_hint_names_the_parse_position() -> None:
    item = finding(score(build_page(jsonld="{not json at all")), "schema_invalid")
    assert item.severity == "warn"
    assert "line 1" in item.fix_hint


def test_one_valid_block_beside_a_broken_one_is_enough() -> None:
    """A page carrying both a good Article block and a broken one is eligible for
    the rich result, so it must not be failed for the broken one."""
    html = build_page(jsonld=VALID_JSONLD).replace(
        "</head>", '<script type="application/ld+json">{oops</script></head>'
    )
    assert severity_of(html, "schema_invalid") == "info"


def test_case_insensitive_script_type_is_still_json_ld() -> None:
    html = build_page(jsonld=VALID_JSONLD).replace("application/ld+json", "Application/LD+JSON")
    assert severity_of(html, "schema_invalid") == "info"


@pytest.mark.parametrize(
    ("block", "problem"),
    [
        ({"@context": "https://schema.org", "@type": "Article", "headline": "H"}, False),
        ({"@type": "Article", "headline": "H"}, True),
        ({"@context": "https://schema.org"}, True),
        ({"@context": "", "@type": "Article", "headline": "H"}, True),
        ({"@context": "https://schema.org", "@type": "Article"}, True),
        (
            {
                "@context": "https://schema.org",
                "@type": ["Article", "BlogPosting"],
                "headline": "H",
            },
            False,
        ),
        # An unknown type passes the generic checks: we do not have the
        # vocabulary, so we must not pretend to judge it.
        ({"@context": "https://schema.org", "@type": "Sandwich"}, False),
    ],
)
def test_validate_jsonld(block: dict[str, Any], problem: bool) -> None:
    findings = validate_jsonld(block)
    assert bool(findings) is problem
    assert all(item.code == "schema_invalid" for item in findings)
    assert all(item.severity == "warn" for item in findings)


def test_builders_produce_blocks_their_own_validator_accepts() -> None:
    article = build_article_jsonld(
        headline="Tap repair in Koblenz",
        url="https://example.org/tap-repair",
        date_published="2026-08-18",
        author_name="A Plumber",
        publisher_name="Example Plumbing",
        description="What to do when a tap will not stop dripping.",
    )
    business = build_local_business_jsonld(
        name="Example Plumbing",
        url="https://example.org",
        street_address="Hauptstrasse 1",
        city="Koblenz",
        postal_code="56068",
        country="DE",
        telephone="+49 261 12345",
    )
    faq = build_faq_jsonld([("Do you work weekends?", "Yes, at no extra charge.")])

    for block in (article, business, faq):
        assert validate_jsonld(block) == []
        assert block["@context"] == "https://schema.org"


def test_builders_omit_absent_optionals_rather_than_emitting_null() -> None:
    """`"image": null` is a validator error; an absent key is not."""
    article = build_article_jsonld(
        headline="H",
        url="https://example.org/a",
        date_published="2026-08-18",
        author_name="A",
        publisher_name="P",
    )
    assert "image" not in article
    assert "keywords" not in article
    # dateModified defaults to datePublished rather than to the clock: an engine
    # that read `now()` would stop being deterministic.
    assert article["dateModified"] == "2026-08-18"


def test_faq_builder_preserves_order_and_drops_blank_entries() -> None:
    faq = build_faq_jsonld(
        [("First?", "Yes."), ("", "orphan answer"), ("Blank answer?", "  "), ("Second?", "No.")]
    )
    names = [entity["name"] for entity in faq["mainEntity"]]
    assert names == ["First?", "Second?"]


def test_local_business_address_is_structured_not_flattened() -> None:
    business = build_local_business_jsonld(
        name="Example Plumbing",
        url="https://example.org",
        street_address="Hauptstrasse 1",
        city="Koblenz",
        postal_code="56068",
        country="DE",
        business_type="Plumber",
        latitude=50.35,
        longitude=7.6,
    )
    assert business["@type"] == "Plumber"
    assert business["address"]["addressLocality"] == "Koblenz"
    assert business["geo"]["latitude"] == 50.35


# --------------------------------------------------------------------------- #
# Determinism, weighting, and hint quality
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "html",
    [
        build_page(),
        build_page(title="Taps", meta=None, internal_links=0, jsonld="{broken"),
        "",
        "<html><body>no metadata at all</body></html>",
    ],
)
def test_scoring_is_deterministic(html: str) -> None:
    """The load-bearing property of this engine: the same input must always give
    the same result, byte for byte, or a stored score means nothing."""
    request = SeoScoreRequest(html=html, target_keyword=KEYWORD)
    first = score_page(request)
    second = score_page(request)
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


def test_weights_sum_to_one_hundred_and_cover_every_rule() -> None:
    assert sum(RULE_WEIGHTS.values()) == 100
    assert set(RULE_WEIGHTS) == set(FINDING_ORDER)


def _synthetic(code: SeoFindingCode, severity: SeoSeverity) -> SeoFinding:
    return SeoFinding(
        code=code, severity=severity, message="m", fix_hint="h", measured=None, expected="e"
    )


def test_aggregate_passes_exactly_at_the_threshold() -> None:
    """Two heavy warns cost 15, landing on 85 -- which must pass, not fail."""
    findings = [_synthetic(code, "info") for code in FINDING_ORDER]
    findings[0] = _synthetic("title_length", "warn")
    findings[2] = _synthetic("heading_tree", "warn")
    assert aggregate(findings) == (85, True)


def test_aggregate_fails_one_point_below_the_threshold() -> None:
    findings = [_synthetic(code, "info") for code in FINDING_ORDER]
    warned: tuple[tuple[int, SeoFindingCode], ...] = (
        (3, "keyword_density"),  # 12 * 0.5
        (5, "internal_links"),  # 12 * 0.5
        (6, "external_links"),  # 8 * 0.5  -> 16 total
    )
    for index, code in warned:
        findings[index] = _synthetic(code, "warn")
    assert aggregate(findings) == (84, False)


def test_an_error_blocks_the_gate_however_high_the_score() -> None:
    """The error clause is independent of the score: a page cannot buy its way
    past a missing <h1> by being excellent everywhere else."""
    findings = [_synthetic(code, "info") for code in FINDING_ORDER]
    findings[2] = _synthetic("heading_tree", "error")
    result_score, passed = aggregate(findings)
    assert result_score == 85
    assert passed is False


def test_half_penalties_round_up_not_to_even() -> None:
    """`round(92.5)` is 92 in Python. A displayed 92 for a computed 92.5 gets
    reported as a bug, and halves are the common case on this scale."""
    findings = [_synthetic(code, "info") for code in FINDING_ORDER]
    findings[7] = _synthetic("image_alt", "warn")  # 8 * 0.5 = 4.0
    findings[4] = _synthetic("readability", "warn")  # 10 * 0.5 = 5.0
    findings[0] = _synthetic("title_length", "warn")  # 15 * 0.5 = 7.5
    assert aggregate(findings)[0] == 84  # 100 - 16.5 = 83.5 -> 84


def test_score_never_leaves_the_zero_to_one_hundred_range() -> None:
    all_errors = [_synthetic(code, "error") for code in FINDING_ORDER]
    assert aggregate(all_errors) == (0, False)


def _measured_as_written(value: float) -> str:
    """How a measured value appears inside a hint."""
    return str(int(value)) if value.is_integer() else str(value)


def test_every_failing_hint_contains_its_measured_value() -> None:
    """The single most important quality property in this module: a hint without
    a number wastes one of only two retry loops."""
    html = build_page(
        title=text_of_length(20),
        meta=text_of_length(118),
        body=body_of_exact_length(400, 1),
        headings=((1, "One"), (1, "Two")),
        internal_links=0,
        external_links=1,
        images=(("hero.jpg", None),),
        jsonld=None,
    )
    result = score(html)

    failures = [item for item in result.findings if item.severity != "info"]
    assert len(failures) >= 7
    for item in failures:
        assert item.fix_hint, f"{item.code} failed without a hint"
        assert item.expected, f"{item.code} failed without a stated target"
        if item.measured is not None:
            assert _measured_as_written(item.measured) in item.fix_hint, (
                f"{item.code} hint omits its measured value {item.measured}: {item.fix_hint}"
            )


def test_fix_hints_property_carries_only_failures() -> None:
    result = score(build_page(meta=text_of_length(118)))
    assert result.fix_hints == [finding(result, "meta_length").fix_hint]
    assert result.errors == []
