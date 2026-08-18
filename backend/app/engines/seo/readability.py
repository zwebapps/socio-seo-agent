"""Flesch Reading Ease, implemented here rather than taken as a dependency.

The formula is three counts and one line of arithmetic; the only hard part is
syllable counting, and every library does that with the same class of heuristic
we use below. Vendoring it keeps the `seo` engine dependency-free and, more
importantly, keeps the numbers *ours*: a scoring gate whose threshold moves when
a transitive dependency changes its syllable table is not a gate.

Formulas
--------
English (Flesch 1948)::

    206.835 - 1.015 * (words / sentences) - 84.6 * (syllables / words)

German (Amstad 1978 adaptation)::

    180 - (words / sentences) - 58.5 * (syllables / words)

`locale` selects the formula by its language subtag, so ``de``, ``de-DE`` and
``de_AT`` all use Amstad. **Any other locale falls back to the English
formula** -- including languages where Flesch is not validated at all (French,
Spanish, Turkish). That fallback is a deliberate, documented approximation: it
is better to return a comparable-over-time number for an unsupported language
than to refuse to score, but the absolute value should not be read as
"reading ease" outside English and German.

Honest limits of the syllable heuristic
---------------------------------------
`count_syllables` counts vowel groups and then applies one correction (silent
trailing "e" in English). It is a heuristic, not a pronunciation dictionary, and
it is wrong in predictable ways:

* diphthongs and hiatus are indistinguishable to it -- "queue" counts 2 (truly
  1), "create" counts 1 after the silent-e rule (truly 2), "poem" counts 1
  (truly 2);
* it cannot know stress or morpheme boundaries, so compounds like "coworker"
  are counted from letters alone;
* acronyms are read as words: "SEO" counts 2, though it is spoken as 3;
* numerals are counted as one syllable regardless of magnitude -- "2024" counts
  1, though it is spoken as 5+;
* German compounds are usually *fine* (German spelling is close to phonetic),
  but loanwords ("Recycling") are not.

The consequence to hold on to: the score is stable and comparable between two
drafts of the same kind of text, which is exactly what the retry loop needs. It
is not a claim about how a specific human reads a specific sentence, and the
`fix_hint` therefore reports the two underlying averages (words per sentence,
syllables per word) rather than only the composite number -- those two are what
a writer can actually act on.
"""

import re
from dataclasses import dataclass
from typing import Final

# A "word" is a run of letters/digits, optionally joined by an apostrophe or a
# hyphen ("didn't", "state-of-the-art" -> one word each). `[^\W_]` is used
# instead of `\w` so that underscores separate words rather than joining them,
# and it stays Unicode-aware, which matters for the German locale.
# The curly apostrophe is written as a regex escape rather than as a literal
# so the source file stays ASCII; `re` expands \uXXXX inside a pattern.
_WORD_RE: Final = re.compile(r"[^\W_]+(?:['\u2019\-][^\W_]+)*")

# Sentence terminators. A newline counts as one: the caller extracts text with
# one line per block element, so a heading or a list item that carries no full
# stop still ends a sentence. Without that, an unpunctuated heading would be
# glued to the paragraph below it and inflate the average sentence length.
#
# Deliberately naive in the other direction: abbreviations ("z. B.", "e.g.")
# over-count sentences, which makes the score slightly optimistic. Recognising
# them needs a locale-specific abbreviation list, which is more machinery than a
# relative quality signal justifies.
_SENTENCE_END_RE: Final = re.compile(r"[.!?…]+|\n+")

# Vowel groups, including the accented vowels that appear in German and in
# loanwords. `y` is treated as a vowel, which is right for "rhythm" and wrong
# for "yes" -- the net effect on a page-length text is negligible.
_VOWEL_GROUP_RE: Final = re.compile(
    r"[aeiouyäöüáàâéè"
    r"êíìîóòôúù"
    r"ûåæœß]+"
)

# English endings where a trailing "e" is pronounced, so the silent-e correction
# must not fire ("simple" -> 2, "see" -> 1 is already right without it).
_LIVE_TRAILING_E: Final = ("le", "ee", "ie", "oe", "ue", "ye")

GERMAN_SUBTAGS: Final[frozenset[str]] = frozenset({"de", "als", "bar", "gsw"})


@dataclass(frozen=True)
class ReadabilityStats:
    """The counts behind a Flesch score, kept so hints can be quantitative."""

    words: int
    sentences: int
    syllables: int
    words_per_sentence: float
    syllables_per_word: float
    score: float


def language_subtag(locale: str) -> str:
    """Reduce a locale to its lowercase language subtag: ``de-DE`` -> ``de``."""
    return re.split(r"[-_]", locale.strip(), maxsplit=1)[0].lower()


def is_german_locale(locale: str) -> bool:
    """True when `locale` should use the Amstad adaptation."""
    return language_subtag(locale) in GERMAN_SUBTAGS


def tokenize_words(text: str) -> list[str]:
    """Split `text` into word tokens. Shared by readability and keyword density
    so the two can never disagree about how many words a page has."""
    return _WORD_RE.findall(text)


def count_sentences(text: str) -> int:
    """Count sentences, treating unterminated trailing text as one sentence.

    Returns 0 only when there are no words at all, so callers can distinguish
    "empty input" from "one sentence with no full stop".
    """
    if not _WORD_RE.search(text):
        return 0
    parts = _SENTENCE_END_RE.split(text)
    return max(sum(1 for part in parts if _WORD_RE.search(part)), 1)


def count_syllables(word: str, *, german: bool = False) -> int:
    """Estimate the syllables in one word. Never returns less than 1.

    See the module docstring for the accuracy this does and does not have. The
    English path removes a silent trailing "e"; the German path does not,
    because a final "e" is pronounced in German ("Straße", "Wende").
    """
    lowered = word.lower()
    groups = len(_VOWEL_GROUP_RE.findall(lowered))

    if (
        not german
        and groups > 1
        and lowered.endswith("e")
        and not lowered.endswith(_LIVE_TRAILING_E)
    ):
        groups -= 1

    return max(groups, 1)


def analyse_readability(text: str, locale: str = "en") -> ReadabilityStats:
    """Count words, sentences and syllables, then apply the locale's formula.

    Empty or word-free input returns all-zero stats with a score of 0.0 rather
    than raising: a page with no prose is a content problem for the caller to
    report, not an exception for it to handle.
    """
    german = is_german_locale(locale)
    words = tokenize_words(text)
    sentences = count_sentences(text)

    if not words or sentences == 0:
        return ReadabilityStats(
            words=0,
            sentences=0,
            syllables=0,
            words_per_sentence=0.0,
            syllables_per_word=0.0,
            score=0.0,
        )

    syllables = sum(count_syllables(word, german=german) for word in words)
    words_per_sentence = len(words) / sentences
    syllables_per_word = syllables / len(words)

    if german:
        score = 180.0 - words_per_sentence - 58.5 * syllables_per_word
    else:
        score = 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word

    return ReadabilityStats(
        words=len(words),
        sentences=sentences,
        syllables=syllables,
        # Rounded so that a persisted result and a recomputed one compare equal
        # without float-noise tolerance. Two decimals is far finer than the
        # heuristic's real accuracy.
        words_per_sentence=round(words_per_sentence, 2),
        syllables_per_word=round(syllables_per_word, 2),
        score=round(score, 2),
    )


def flesch_reading_ease(text: str, locale: str = "en") -> float:
    """Flesch Reading Ease for `text`, by the formula `locale` selects.

    The value is *not* clamped to 0-100: the real formula is unbounded, and
    clamping would hide the difference between "hard" and "unreadable", which is
    information a writer can use.
    """
    return analyse_readability(text, locale).score
