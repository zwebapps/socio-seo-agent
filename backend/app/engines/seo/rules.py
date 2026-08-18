"""One function per `SeoFinding.code`, plus the HTML parse they all share.

Design rules that hold for every function in this module:

* **One finding per code, always returned.** A rule never returns `None` and
  never returns two findings for its own code. A passing rule returns `info`, so
  the review screen renders a complete checklist and the caller never has to
  guess whether a missing code means "passed" or "not checked".
* **Every failing `fix_hint` states the measured value and the target.** These
  strings are fed to the content model verbatim (`docs/AGENT_RUNTIME.md` §7);
  "improve the meta description" wastes a retry, "extend it to 140-160
  characters; it currently stops at 118" does not.
* **No I/O, no clock, no randomness.** Input is a string; output is data.
"""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

from bs4 import BeautifulSoup, Tag

from backend.app.engines.seo.contract import SeoFinding, SeoSeverity
from backend.app.engines.seo.jsonld import validate_jsonld
from backend.app.engines.seo.readability import (
    ReadabilityStats,
    analyse_readability,
    tokenize_words,
)

# --------------------------------------------------------------------------- #
# Thresholds. Every number a rule compares against lives here, so "why did this
# page score 78?" is answerable by reading one screen.
# --------------------------------------------------------------------------- #

TITLE_TARGET_MIN: Final = 50
TITLE_TARGET_MAX: Final = 60
# Outside this wider band the title is not merely suboptimal, it is broken: a
# 20-character title wastes the strongest on-page signal there is, and a
# 90-character one is truncated in the result. Hence error, not warn.
TITLE_HARD_MIN: Final = 30
TITLE_HARD_MAX: Final = 70

META_TARGET_MIN: Final = 140
META_TARGET_MAX: Final = 160

# Density thresholds are held as integers per 10,000 words rather than as
# percentages, and compared with integer arithmetic. A gate that flips on
# `0.8000000000000001 >= 0.8` is a gate nobody can reason about, and a boundary
# bug here quietly blocks or passes everything.
DENSITY_MIN_PER_10K: Final = 80  # 0.8%
DENSITY_MAX_PER_10K: Final = 250  # 2.5%
DENSITY_MIN_PCT: Final = 0.8
DENSITY_MAX_PCT: Final = 2.5

READABILITY_MIN: Final = 55.0

MIN_INTERNAL_LINKS: Final = 1
MIN_EXTERNAL_LINKS: Final = 2

# Tags whose text is not page content. `head` goes too, so the `<title>` is not
# counted twice -- once as the title rule's subject and again as body prose,
# which would skew keyword density on short pages.
_NON_CONTENT_TAGS: Final = ("head", "script", "style", "noscript", "template", "svg")

# Block-level elements. Each one is fenced with newlines in the extracted text,
# which is what lets the readability pass treat a block boundary as a sentence
# end. Inline elements are deliberately absent: text inside `<a>`, `<strong>` or
# `<em>` belongs to the sentence around it, and splitting there would shatter one
# 25-word sentence into four short ones and hand the page a flattering score it
# has not earned.
_BLOCK_SEPARATOR: Final = "\n"
_BLOCK_TAGS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_HEADING_TAGS: Final = ("h1", "h2", "h3", "h4", "h5", "h6")

# Hrefs that are neither an internal navigation link nor an outbound citation.
_IGNORED_HREF_PREFIXES: Final = ("mailto:", "tel:", "sms:", "javascript:", "data:", "#", "?")
# `//host/path` is protocol-relative and therefore off-site.
_ABSOLUTE_HREF_PREFIXES: Final = ("http://", "https://", "//")

_MAX_HINT_EXAMPLES: Final = 3
_MAX_HINT_TEXT: Final = 60

_JSONLD_MIME: Final = "application/ld+json"


# --------------------------------------------------------------------------- #
# Parsed facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ImageFact:
    """One `<img>`. `alt is None` means the attribute is absent; `alt == ""`
    means it is present but empty. The rule reports the two separately because
    the fixes differ, and because a missing attribute is often a template bug
    while an empty one is often a deliberate (if usually wrong) choice."""

    src: str
    alt: str | None


@dataclass(frozen=True)
class PageFacts:
    """Everything the nine rules need, extracted from the HTML exactly once.

    Frozen and tuple-valued so a rule cannot mutate what the next rule reads --
    the cheapest possible guarantee that rule order does not affect the score.
    """

    title: str | None
    meta_description: str | None
    body_text: str
    headings: tuple[tuple[int, str], ...]
    internal_links: tuple[str, ...]
    external_links: tuple[str, ...]
    images: tuple[ImageFact, ...]
    jsonld_sources: tuple[str, ...]


def classify_href(href: str) -> str:
    """Classify one `href` as ``internal``, ``external`` or ``ignored``.

    Judgement call worth knowing about: `SeoScoreRequest` carries no base URL, so
    there is no way to tell `https://example.com/about` on example.com's own page
    from a genuinely outbound link. Absolute http(s) URLs are therefore counted
    as **external** and relative paths as **internal**. For content this system
    generates that is the right default -- generated internal links are written
    relative -- but a caller scoring third-party HTML full of self-referencing
    absolute URLs will see its internal-link count read low.
    """
    candidate = href.strip()
    if not candidate:
        return "ignored"
    lowered = candidate.lower()
    if lowered.startswith(_IGNORED_HREF_PREFIXES):
        return "ignored"
    if lowered.startswith(_ABSOLUTE_HREF_PREFIXES):
        return "external"
    return "internal"


def _attr(tag: Tag, name: str) -> str | None:
    """Read one attribute as a string, or None when absent.

    bs4 returns a list for attributes HTML defines as space-separated (and for
    duplicated attributes), so the multi-value case is joined rather than being
    allowed to reach a rule as an unexpected type.
    """
    raw = tag.get(name)
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return " ".join(str(item) for item in raw)


def _text_of(tag: Tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ")).strip()


def _extract_body_text(html: str) -> str:
    """Extract visible prose, one line per block element.

    Parsed a second time on purpose: `decompose()` mutates the tree, and the
    other rules must see the document intact. Two parses of a page-sized
    document is a trade of microseconds for the guarantee that rules cannot
    interfere with each other.
    """
    soup = BeautifulSoup(html, "lxml")
    for element in soup.find_all(_NON_CONTENT_TAGS):
        element.decompose()

    # Fence block elements with newlines, then extract with an empty separator.
    # An empty separator is what preserves inline runs: the text nodes of
    # "See <a>our rates</a> for details." already carry their own spaces, so
    # concatenating them reproduces the sentence exactly, while any separator
    # would inject spurious gaps (and any *newline* separator would turn one
    # sentence into three).
    for element in soup.find_all(list(_BLOCK_TAGS)):
        if element.parent is not None:
            element.insert_before(_BLOCK_SEPARATOR)
            element.insert_after(_BLOCK_SEPARATOR)

    root = soup.body if isinstance(soup.body, Tag) else soup
    lines = [
        re.sub(r"[^\S\n]+", " ", line).strip() for line in root.get_text("").split(_BLOCK_SEPARATOR)
    ]
    return _BLOCK_SEPARATOR.join(line for line in lines if line)


def extract_facts(html: str) -> PageFacts:
    """Parse `html` once into the facts the rules score.

    `lxml` is used rather than `html.parser` because real crawled markup is
    malformed and lxml recovers from it the way a browser does; a scorer that
    raises on a stray `</div>` is useless against the live web.
    """
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.title
    title_text = _text_of(title_tag) if isinstance(title_tag, Tag) else None

    meta_description: str | None = None
    for element in soup.find_all("meta"):
        name = (_attr(element, "name") or "").strip().lower()
        if name == "description":
            meta_description = (_attr(element, "content") or "").strip()
            break

    headings: list[tuple[int, str]] = []
    for element in soup.find_all(_HEADING_TAGS):
        headings.append((int(element.name[1]), _text_of(element)))

    internal: list[str] = []
    external: list[str] = []
    for element in soup.find_all("a"):
        href = _attr(element, "href")
        if href is None:
            continue
        kind = classify_href(href)
        if kind == "internal":
            internal.append(href.strip())
        elif kind == "external":
            external.append(href.strip())

    images: list[ImageFact] = []
    for element in soup.find_all("img"):
        images.append(
            ImageFact(src=(_attr(element, "src") or "").strip(), alt=_attr(element, "alt"))
        )

    jsonld: list[str] = []
    for element in soup.find_all("script"):
        mime = (_attr(element, "type") or "").strip().lower()
        if mime == _JSONLD_MIME:
            jsonld.append(element.get_text())

    return PageFacts(
        title=title_text or None,
        meta_description=meta_description or None,
        body_text=_extract_body_text(html),
        headings=tuple(headings),
        internal_links=tuple(internal),
        external_links=tuple(external),
        images=tuple(images),
        jsonld_sources=tuple(jsonld),
    )


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _clip(text: str, limit: int = _MAX_HINT_TEXT) -> str:
    """Shorten text for use inside a hint. Hints are read by a model with a
    token budget; quoting a 300-character heading back at it wastes both."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _examples(values: Sequence[str], limit: int = _MAX_HINT_EXAMPLES) -> str:
    shown = [_clip(value, 40) or "(no src)" for value in values[:limit]]
    joined = ", ".join(shown)
    remainder = len(values) - len(shown)
    return f"{joined} and {remainder} more" if remainder > 0 else joined


def count_phrase_occurrences(words: Sequence[str], phrase: Sequence[str]) -> int:
    """Count non-overlapping, case-insensitive occurrences of a token phrase.

    Token-based rather than substring-based: `"ear"` must not match inside
    `"research"`, and a substring count is how keyword density reports get
    laughed out of a review. Non-overlapping so a doubled phrase
    ("widget widget widget" for the phrase "widget widget") counts once per
    consumed pair rather than inflating itself.
    """
    if not phrase:
        return 0
    haystack = [word.lower() for word in words]
    needle = [token.lower() for token in phrase]
    size = len(needle)

    found = 0
    index = 0
    while index + size <= len(haystack):
        if haystack[index : index + size] == needle:
            found += 1
            index += size
        else:
            index += 1
    return found


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _uses(count: int) -> str:
    """ "1 use" / "3 uses". Hints are read by a model and shown to a human;
    "1 use(s)" reads like a placeholder nobody finished."""
    return f"{count} use" if count == 1 else f"{count} uses"


def _links(count: int) -> str:
    return f"{count} link" if count == 1 else f"{count} links"


def _images(count: int) -> str:
    return f"{count} image" if count == 1 else f"{count} images"


# --------------------------------------------------------------------------- #
# The nine rules
# --------------------------------------------------------------------------- #


def check_title_length(facts: PageFacts) -> SeoFinding:
    """`title_length`: 50-60 characters. Error if absent or outside 30-70."""
    expected = f"{TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} characters"

    if facts.title is None:
        return SeoFinding(
            code="title_length",
            severity="error",
            message="The page has no <title>.",
            fix_hint=(
                f"Add a <title> of {TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} characters that "
                "leads with the target keyword; the page currently has none (0 characters)."
            ),
            measured=0.0,
            expected=expected,
        )

    length = len(facts.title)
    if TITLE_TARGET_MIN <= length <= TITLE_TARGET_MAX:
        return SeoFinding(
            code="title_length",
            severity="info",
            message=f"Title is {length} characters.",
            fix_hint="",
            measured=float(length),
            expected=expected,
        )

    too_short = length < TITLE_TARGET_MIN
    severity: SeoSeverity = (
        "error" if length < TITLE_HARD_MIN or length > TITLE_HARD_MAX else "warn"
    )
    if too_short:
        delta = TITLE_TARGET_MIN - length
        hint = (
            f"Extend the title from {length} to {TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} "
            f"characters (add about {delta}); keep the target keyword at the front."
        )
    else:
        delta = length - TITLE_TARGET_MAX
        hint = (
            f"Shorten the title from {length} to {TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} "
            f"characters (remove about {delta}); anything past ~60 is truncated in the "
            "search result."
        )

    return SeoFinding(
        code="title_length",
        severity=severity,
        message=f"Title is {length} characters, outside the {expected} target.",
        fix_hint=hint,
        measured=float(length),
        expected=expected,
    )


def check_meta_length(facts: PageFacts) -> SeoFinding:
    """`meta_length`: 140-160 characters. Error only when absent."""
    expected = f"{META_TARGET_MIN}-{META_TARGET_MAX} characters"

    if facts.meta_description is None:
        return SeoFinding(
            code="meta_length",
            severity="error",
            message="The page has no meta description.",
            fix_hint=(
                f'Add <meta name="description"> of {META_TARGET_MIN}-{META_TARGET_MAX} '
                "characters summarising the page and naming the target keyword once; "
                "the page currently has none (0 characters)."
            ),
            measured=0.0,
            expected=expected,
        )

    length = len(facts.meta_description)
    if META_TARGET_MIN <= length <= META_TARGET_MAX:
        return SeoFinding(
            code="meta_length",
            severity="info",
            message=f"Meta description is {length} characters.",
            fix_hint="",
            measured=float(length),
            expected=expected,
        )

    if length < META_TARGET_MIN:
        hint = (
            f"Extend the meta description to {META_TARGET_MIN}-{META_TARGET_MAX} "
            f"characters; it currently stops at {length}."
        )
    else:
        hint = (
            f"Trim the meta description to {META_TARGET_MIN}-{META_TARGET_MAX} "
            f"characters; it currently runs to {length} and is cut off mid-sentence."
        )

    return SeoFinding(
        code="meta_length",
        severity="warn",
        message=f"Meta description is {length} characters, outside the {expected} target.",
        fix_hint=hint,
        measured=float(length),
        expected=expected,
    )


def check_keyword_density(
    facts: PageFacts,
    target_keyword: str,
    secondary_keywords: Sequence[str] = (),
) -> SeoFinding:
    """`keyword_density`: the target keyword is 0.8-2.5% of body words.

    A multi-word keyword contributes its own word count per occurrence -- three
    uses of "emergency plumber koblenz" is nine words of the page, not three --
    because that is what a term-frequency measure actually is.
    """
    expected = f"{DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}% of body words"
    words = tokenize_words(facts.body_text)
    total = len(words)
    phrase = tokenize_words(target_keyword)

    if not phrase:
        return SeoFinding(
            code="keyword_density",
            severity="error",
            message="No target keyword was supplied, so density cannot be scored.",
            fix_hint=(
                "Supply a target keyword in the scoring request; on-page density cannot "
                "be measured without one."
            ),
            measured=None,
            expected=expected,
        )

    if total == 0:
        return SeoFinding(
            code="keyword_density",
            severity="error",
            message="The page has no body text, so the keyword cannot appear in it.",
            fix_hint=(
                f"Write body copy: the page has 0 words, so '{target_keyword}' has a "
                f"density of 0.00% against a {DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}% target."
            ),
            measured=0.0,
            expected=expected,
        )

    occurrences = count_phrase_occurrences(words, phrase)
    units = occurrences * len(phrase)
    percent = round(units * 100 / total, 2)

    # Integer comparison -- see DENSITY_MIN_PER_10K.
    scaled = units * 10_000
    below_band = scaled < DENSITY_MIN_PER_10K * total
    within = not below_band and scaled <= DENSITY_MAX_PER_10K * total

    min_occurrences = _ceil_div(DENSITY_MIN_PER_10K * total, 10_000 * len(phrase))
    max_occurrences = (DENSITY_MAX_PER_10K * total) // (10_000 * len(phrase))

    if within:
        return SeoFinding(
            code="keyword_density",
            severity="info",
            message=(
                f"'{target_keyword}' appears {occurrences} times in {total:,} words ({percent}%)."
            ),
            fix_hint="",
            measured=percent,
            expected=expected,
        )

    missing_secondary = [
        keyword
        for keyword in secondary_keywords
        if keyword.strip() and count_phrase_occurrences(words, tokenize_words(keyword)) == 0
    ]
    secondary_note = (
        f" Secondary keywords not used yet: {', '.join(missing_secondary)}."
        if missing_secondary
        else ""
    )
    window = f"{min_occurrences}-{max_occurrences} times"
    observed = f"'{target_keyword}' appears {occurrences} times in {total:,} words ({percent}%)."

    # Below ~40 words per keyword token there is no occurrence count that lands
    # inside the band at all: one use of a one-word keyword is already 2.5% of a
    # 40-word page. Telling the writer to "use it 1-0 times" would be nonsense,
    # so the real fix -- more copy -- is what the hint asks for.
    if max_occurrences < min_occurrences:
        min_words = _ceil_div(10_000 * len(phrase), DENSITY_MAX_PER_10K)
        severity: SeoSeverity = "error" if occurrences == 0 else "warn"
        return SeoFinding(
            code="keyword_density",
            severity=severity,
            message=(
                f"The body is only {total:,} words, too short to carry "
                f"'{target_keyword}' at {DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}%."
            ),
            fix_hint=(
                f"{observed} No count fits {DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}% at this "
                f"length: expand the body to at least {min_words:,} words (ideally 600+ for a "
                f"ranking page), then use the keyword {DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}% of "
                f"the time.{secondary_note}"
            ),
            measured=percent,
            expected=expected,
        )

    if occurrences == 0:
        return SeoFinding(
            code="keyword_density",
            severity="error",
            message=f"'{target_keyword}' does not appear in the {total:,}-word body.",
            fix_hint=(
                f"'{target_keyword}' appears 0 times in {total:,} words (0.00%). Use it "
                f"{window} to reach {DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}%, including once in "
                f"the opening paragraph and once in an H2.{secondary_note}"
            ),
            measured=0.0,
            expected=expected,
        )

    if below_band:
        hint = (
            f"{observed} Add {_uses(max(min_occurrences - occurrences, 1))} to reach "
            f"{DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}% ({window} in total); put one in an H2 and "
            f"one in the opening paragraph.{secondary_note}"
        )
    else:
        hint = (
            f"{observed} That is above the {DENSITY_MAX_PCT}% ceiling: remove "
            f"{_uses(occurrences - max_occurrences)} to land within {window}, replacing them "
            f"with pronouns or a secondary keyword.{secondary_note}"
        )

    return SeoFinding(
        code="keyword_density",
        severity="warn",
        message=(
            f"'{target_keyword}' density is {percent}%, outside the "
            f"{DENSITY_MIN_PCT}-{DENSITY_MAX_PCT}% target."
        ),
        fix_hint=hint,
        measured=percent,
        expected=expected,
    )


def check_readability(facts: PageFacts, locale: str = "en") -> SeoFinding:
    """`readability`: Flesch Reading Ease at or above 55."""
    expected = f"Flesch reading ease ≥ {READABILITY_MIN:g}"
    stats: ReadabilityStats = analyse_readability(facts.body_text, locale)

    if stats.words == 0:
        return SeoFinding(
            code="readability",
            severity="warn",
            message="The page has no body text to score for readability.",
            fix_hint=(
                "Write body copy: there are 0 words to score, so reading ease is 0.0 "
                f"against a target of {READABILITY_MIN:g}."
            ),
            measured=0.0,
            expected=expected,
        )

    if stats.score >= READABILITY_MIN:
        return SeoFinding(
            code="readability",
            severity="info",
            message=f"Flesch reading ease is {stats.score} ({stats.words:,} words).",
            fix_hint="",
            measured=stats.score,
            expected=expected,
        )

    return SeoFinding(
        code="readability",
        severity="warn",
        message=(f"Flesch reading ease is {stats.score}, below the {READABILITY_MIN:g} target."),
        fix_hint=(
            f"Reading ease is {stats.score}; raise it to at least {READABILITY_MIN:g}. "
            f"Sentences average {stats.words_per_sentence} words (aim for 15-20) and words "
            f"average {stats.syllables_per_word} syllables (aim for under 1.6): split the "
            "longest sentences and replace long nouns with plain ones."
        ),
        measured=stats.score,
        expected=expected,
    )


def check_heading_tree(facts: PageFacts) -> SeoFinding:
    """`heading_tree`: exactly one `h1`, and no skipped levels.

    Both problems share one code, so both are reported in one finding rather
    than one of them being hidden until the other is fixed -- a retry loop with
    a two-loop budget cannot afford to discover the second problem on loop two.
    """
    expected = "exactly one <h1> and no skipped heading levels"
    h1_count = sum(1 for level, _ in facts.headings if level == 1)
    h1_texts = [text for level, text in facts.headings if level == 1]

    skips = [
        (previous[0], current[0], current[1])
        for previous, current in pairwise(facts.headings)
        if current[0] > previous[0] + 1
    ]

    problems: list[str] = []
    hints: list[str] = []

    if h1_count == 0:
        problems.append("no <h1>")
        hints.append("Add exactly one <h1> carrying the page's main heading; the page has 0.")
    elif h1_count > 1:
        problems.append(f"{h1_count} <h1> elements")
        hints.append(
            f"Keep exactly one <h1>: the page has {h1_count} "
            f"({_examples([_clip(text) for text in h1_texts])}). Demote "
            f"{h1_count - 1} of them to <h2>."
        )

    for parent, child, text in skips:
        problems.append(f"<h{parent}> → <h{child}>")
        hints.append(
            f'Do not skip heading levels: "{_clip(text)}" is an <h{child}> directly under '
            f"an <h{parent}>. Change it to <h{parent + 1}> or add an intervening "
            f"<h{parent + 1}>."
        )

    if not problems:
        return SeoFinding(
            code="heading_tree",
            severity="info",
            message=(
                f"Heading tree is valid: one <h1> and {len(facts.headings) - 1} "
                "sub-headings, no skipped levels."
            ),
            fix_hint="",
            measured=float(h1_count),
            expected=expected,
        )

    return SeoFinding(
        code="heading_tree",
        severity="error",
        message="Heading structure is invalid: " + "; ".join(problems) + ".",
        fix_hint=" ".join(hints),
        measured=float(h1_count),
        expected=expected,
    )


def check_internal_links(facts: PageFacts) -> SeoFinding:
    """`internal_links`: at least one link to another page on the same site."""
    expected = f"≥ {MIN_INTERNAL_LINKS} internal link"
    count = len(facts.internal_links)

    if count >= MIN_INTERNAL_LINKS:
        return SeoFinding(
            code="internal_links",
            severity="info",
            message=f"{_links(count)} to other pages on this site.",
            fix_hint="",
            measured=float(count),
            expected=expected,
        )

    return SeoFinding(
        code="internal_links",
        severity="error",
        message="No internal links found.",
        fix_hint=(
            f"Add at least {MIN_INTERNAL_LINKS} internal link; the page currently has 0. "
            "Link with descriptive anchor text to a relevant service page or to the "
            "contact page, using a relative URL such as /services/emergency-callout."
        ),
        measured=0.0,
        expected=expected,
    )


def check_external_links(facts: PageFacts) -> SeoFinding:
    """`external_links`: at least two outbound citations."""
    expected = f"≥ {MIN_EXTERNAL_LINKS} external links"
    count = len(facts.external_links)

    if count >= MIN_EXTERNAL_LINKS:
        return SeoFinding(
            code="external_links",
            severity="info",
            message=f"{_links(count)} to outside sources.",
            fix_hint="",
            measured=float(count),
            expected=expected,
        )

    return SeoFinding(
        code="external_links",
        severity="warn",
        message=(
            f"Only {_links(count)} to outside sources, below the {MIN_EXTERNAL_LINKS} expected."
        ),
        fix_hint=(
            f"The page has {_links(count)} to outside sources; add "
            f"{MIN_EXTERNAL_LINKS - count} more so "
            f"at least {MIN_EXTERNAL_LINKS} factual claims cite an authoritative outside "
            "source (a standards body, a manufacturer, or official statistics)."
        ),
        measured=float(count),
        expected=expected,
    )


def check_image_alt(facts: PageFacts) -> SeoFinding:
    """`image_alt`: every `<img>` carries non-empty alt text.

    `alt=""` is flagged as well as a missing attribute. That is deliberate: an
    empty alt is the correct markup for a purely decorative image, but in
    generated marketing content a decorative image is the exception, so the
    default assumption is an omission. It is a `warn`, not an `error`, precisely
    because the exception is real.
    """
    expected = "every <img> has non-empty alt text"
    absent = [image for image in facts.images if image.alt is None]
    blank = [image for image in facts.images if image.alt is not None and not image.alt.strip()]
    missing = absent + blank

    if not missing:
        return SeoFinding(
            code="image_alt",
            severity="info",
            message=f"All {_images(len(facts.images))} have alt text."
            if facts.images
            else "The page has no images.",
            fix_hint="",
            measured=0.0,
            expected=expected,
        )

    detail = ", ".join(
        part
        for part in (
            f"{len(absent)} with no alt attribute" if absent else "",
            f'{len(blank)} with alt=""' if blank else "",
        )
        if part
    )
    return SeoFinding(
        code="image_alt",
        severity="warn",
        message=(f"{len(missing)} of {_images(len(facts.images))} lack alt text ({detail})."),
        fix_hint=(
            f"{len(missing)} of {len(facts.images)} <img> elements have no usable alt text "
            f"({detail}): {_examples([image.src for image in missing])}. Give each a short "
            'factual description of what the image shows; use alt="" only if an image is '
            "purely decorative."
        ),
        measured=float(len(missing)),
        expected=expected,
    )


def _iter_jsonld_objects(payload: Any) -> Iterator[dict[str, Any]]:
    """Yield every schema.org object in one parsed JSON-LD payload.

    Handles the three shapes real pages use: a single object, a top-level array,
    and an `@graph` wrapper. Members of an `@graph` inherit the wrapper's
    `@context`, because that is what the JSON-LD spec says they do -- validating
    them without it would report a missing context on a perfectly valid block.
    """
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_jsonld_objects(item)
        return

    if not isinstance(payload, dict):
        return

    graph = payload.get("@graph")
    if isinstance(graph, list):
        context = payload.get("@context")
        for member in graph:
            if isinstance(member, dict):
                inherited: dict[str, Any] = dict(member)
                if context is not None:
                    inherited.setdefault("@context", context)
                yield inherited
        return

    yield payload


def check_schema(facts: PageFacts) -> SeoFinding:
    """`schema_invalid`: at least one usable JSON-LD block on the page.

    Malformed JSON is a finding with the parser's own position in it, never an
    exception: this input is machine-written markup, so broken structured data is
    an expected case and the writer needs to be told *where* it broke.
    """
    expected = 'at least one JSON-LD block with "@context" and "@type"'
    valid = 0
    reasons: list[str] = []

    for index, source in enumerate(facts.jsonld_sources, start=1):
        stripped = source.strip()
        if not stripped:
            reasons.append(f"block {index} is empty")
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            reasons.append(
                f"block {index} is not valid JSON ({exc.msg} at line {exc.lineno} "
                f"column {exc.colno})"
            )
            continue

        objects = list(_iter_jsonld_objects(payload))
        if not objects:
            reasons.append(f"block {index} contains no JSON-LD object")
            continue
        for obj in objects:
            problems = validate_jsonld(obj)
            if problems:
                reasons.append(f"block {index}: {problems[0].message}")
            else:
                valid += 1

    if valid:
        return SeoFinding(
            code="schema_invalid",
            severity="info",
            message=f"{valid} valid JSON-LD block(s) found.",
            fix_hint="",
            measured=float(valid),
            expected=expected,
        )

    reason_note = f" Problems: {'; '.join(reasons[:_MAX_HINT_EXAMPLES])}." if reasons else ""
    return SeoFinding(
        code="schema_invalid",
        severity="warn",
        message=(
            f"No valid JSON-LD block found ({len(facts.jsonld_sources)} block(s) present)."
            if facts.jsonld_sources
            else "No JSON-LD structured data found."
        ),
        fix_hint=(
            f"The page has {len(facts.jsonld_sources)} JSON-LD block(s) and 0 valid ones. Embed "
            '<script type="application/ld+json"> containing "@context": "https://schema.org" '
            'and an "@type" of Article, FAQPage or LocalBusiness, with that type\'s required '
            f"properties.{reason_note}"
        ),
        measured=0.0,
        expected=expected,
    )
