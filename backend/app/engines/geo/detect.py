"""Answer -> presence. The part of the module a customer will argue with.

Every share-of-voice number rests on one claim: "the brand appears in this
answer". So the bias here is explicit and one-directional, and it is the opposite
of the `nap` engine's:

    a missed mention (false negative) understates our value;
    an invented mention (false positive) is a number the customer can disprove
    by reading the answer we stored next to it.

We store the excerpt precisely so a user can check. That makes a false positive
immediately visible and therefore unacceptable, so where a rule could go either
way it goes the strict way.

Two consequences worth naming up front, because they are the ones that surprise
people:

* Matching is **token-bounded**. `Müller Sanitär` matches `mueller-sanitaer` and
  `MÜLLER SANITÄR`, and does *not* match `Müllersanitärbedarf`.
* Mentions and citations are **counted separately**, and neither implies the
  other. A model can print a URL without naming the business, and praise a
  business without linking it.

The folding rules are the same *idea* as `engines/nap/normalise.py` and are
implemented independently on purpose: that module folds addresses for a
consistency audit and its rules will move for reasons that have nothing to do
with this one. Sharing the code would couple two products with opposite failure
costs.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Final

from .contract import AnswerStatus, BrandIdentity, PresenceResult

__all__ = [
    "answer_excerpt",
    "classify_answer",
    "detect_presence",
    "extract_hosts",
    "fold_for_matching",
    "looks_like_refusal",
    "mentions_name",
    "normalise_host",
]

# --------------------------------------------------------------------------- #
# Folding
# --------------------------------------------------------------------------- #

# Dash-like and space-like characters a model might emit, mapped to ASCII before
# anything else looks at the text.
_ASCII_PUNCT: Final = str.maketrans(
    {
        # Written as escapes, not as literals: ruff's RUF001 rightly flags a
        # confusable character in source, and the escape is also the only form a
        # reader can identify with certainty.
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        "\u00a0": " ",  # no-break space
        "\u2007": " ",  # figure space
        "\u2009": " ",  # thin space
        "\u202f": " ",  # narrow no-break space
        "\u2018": "'",  # curly quotes -- models emit these constantly
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
)

# The German transliteration collapse: "ue" -> "u" rather than "ü" -> "ue", which
# is the only direction that makes Müller / Mueller / Muller / MÜLLER one value.
# German businesses type all four, and any form that rejects umlauts forces the
# transliteration on them.
#
# The collapse is applied to both sides of every comparison, so it can never MISS
# a transliteration variant. It can, however, conflate two names that differ only
# by one of these digraphs -- "Baur" and "Bauer" fold to the same value, and they
# are two different companies. That is a real false positive, it is accepted
# knowingly rather than papered over (see the test of the same name), and the
# reason it is accepted is proportion: in this market the Mueller/Müller case
# occurs constantly and the Baur/Bauer collision almost never, and every counted
# answer is stored with an excerpt so a reader can see the mistake.
_DIGRAPHS: Final = (("ae", "a"), ("oe", "o"), ("ue", "u"))


def fold_for_matching(value: str) -> str:
    """Fold text into the comparison alphabet: lowercase, ASCII, single spaces.

    Lowercases, expands `ß` to `ss`, strips diacritics, collapses the German
    transliteration digraphs, and reduces every run of non-alphanumeric
    characters to one space. `"Müller Sanitär GmbH"` and `"MUELLER-SANITAER
    gmbh"` both become `"muller sanitar gmbh"`.

    Punctuation is destroyed rather than preserved because a model's punctuation
    is stylistic noise -- an answer may write `Müller-Sanitär`, `Müller Sanitär`
    or `Müller, Sanitär` for the same business.
    """
    folded = value.translate(_ASCII_PUNCT).lower().replace("ß", "ss")
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    for digraph, single in _DIGRAPHS:
        stripped = stripped.replace(digraph, single)
    return " ".join(re.split(r"[^a-z0-9]+", stripped)).strip()


def mentions_name(folded_haystack: str, name: str) -> bool:
    """Whether `name` appears in already-folded text, on token boundaries.

    The haystack is folded once by the caller and reused for every name, which is
    what keeps a 40-prompt run cheap.

    Token boundaries are the whole point: `"Kern"` must not match `"Kernel"`, and
    `"Bad Ems"` must not match `"Badems"`. Both sides are folded identically, so
    separators have already become single spaces and a plain escaped search with
    alphanumeric look-arounds is exact.
    """
    needle = fold_for_matching(name)
    if not needle:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
    return re.search(pattern, folded_haystack) is not None


# --------------------------------------------------------------------------- #
# Hosts and citations
# --------------------------------------------------------------------------- #

_URL: Final = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# A bare hostname: at least one dotted label followed by a 2+ letter TLD. The
# look-behind stops mid-host matches (`sub.example.com` must not also yield
# `example.com` as a separate bare host -- it is found by the host-suffix rule
# instead).
_BARE_HOST: Final = re.compile(
    r"(?<![a-z0-9\-.])((?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,})(?![a-z0-9\-])",
    re.IGNORECASE,
)


def normalise_host(value: str) -> str:
    """Reduce a URL, a bare domain or an email host to a comparable hostname.

    Strips scheme, userinfo, path, query, fragment, port, a trailing dot and a
    leading `www.`, then lowercases. `"https://WWW.Mueller-Sanitaer.de:443/kontakt"`
    becomes `"mueller-sanitaer.de"`.

    Umlaut folding is deliberately NOT applied: a hostname is not prose, and
    `müller.de` and `mueller.de` are two different registrations that can belong
    to two different companies. Folding them together here would be the one kind
    of false positive that is also a factual error.
    """
    text = value.strip().translate(_ASCII_PUNCT)
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text, flags=re.IGNORECASE)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.rsplit(":", 1)[0] if re.search(r":\d+$", text) else text
    text = text.strip().strip(".").lower()
    return text.removeprefix("www.")


def extract_hosts(answer: str) -> list[str]:
    """Every hostname an answer refers to, normalised, in order of appearance.

    Deliberately over-inclusive: a missing space after a full stop ("costs
    money.The next step") can yield a nonsense host. That is harmless, because
    nothing is ever *reported* from this list -- it is only ever compared against
    the specific domains a caller supplied. Being generous here costs nothing and
    catches the bare-domain style ("see mueller-sanitaer.de") that models
    actually use.
    """
    hosts: list[str] = []
    seen: set[str] = set()
    for match in _URL.finditer(answer):
        host = normalise_host(match.group(0))
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    for match in _BARE_HOST.finditer(answer):
        host = normalise_host(match.group(1))
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _cited_domains(hosts: Sequence[str], domains: Sequence[str]) -> list[str]:
    """Which of `domains` the answer cited, as the caller spelled them.

    A subdomain counts (`shop.example.com` cites `example.com`); a host that
    merely *contains* the domain does not (`example.com.phishing.ru` cites
    nothing). That distinction is a dotted-suffix check, and getting it wrong is
    how a citation counter becomes trivially spoofable.
    """
    matched: list[str] = []
    for domain in domains:
        target = normalise_host(domain)
        if not target:
            continue
        if any(host == target or host.endswith(f".{target}") for host in hosts):
            matched.append(domain)
    return matched


def _prose_only(answer: str) -> str:
    """The answer with URLs and bare hostnames removed, for *name* matching.

    Without this, `https://mueller-sanitaer.de/kontakt` folds to text containing
    `muller sanitar` and a bare link counts as a prose mention -- so one piece of
    evidence would fire both signals and the mention rate would silently absorb
    the citation rate. The two numbers are sold to the customer as different
    things, so they get different inputs: names are read from prose, domains from
    links.

    The cost of the rule: a brand whose *name* is a domain (`Auto.de`) is stripped
    out of its own prose and registers as cited-but-not-mentioned. That is a
    visible understatement of one signal rather than a silent inflation of
    another, which is the direction this module always chooses.
    """
    return _BARE_HOST.sub(" ", _URL.sub(" ", answer))


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #

#: An answer longer than this is treated as an answer even if it contains a
#: refusal-shaped phrase. Long responses that hedge ("I don't have real-time
#: data, but the established firms are ...") *are* answers, and calling them
#: refusals would quietly shrink the denominator and inflate every percentage.
REFUSAL_MAX_CHARS: Final = 320

# Matched against a lowercased, whitespace-collapsed copy of the answer -- not the
# digraph-folded form, which would mangle these words into unreadable patterns.
#
# German needs regexes rather than substrings: "ich kann" is the opening of both
# "ich kann Ihnen Müller Sanitär empfehlen" (an answer) and "ich kann dazu keine
# Auskunft geben" (a refusal), so the negation has to be part of the pattern.
_REFUSAL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bi (?:can'?t|cannot|can not)\b",
        r"\bi'?m (?:unable|not able)\b",
        r"\bi am (?:unable|not able)\b",
        r"\bas an ai\b",
        r"\bas a language model\b",
        r"\bi (?:don'?t|do not) have (?:access|enough|any|real-time|reliable)\b",
        r"\bi (?:don'?t|do not) know\b",
        r"\b(?:un)?able to provide\b",
        r"\bno (?:information|data) (?:is )?available\b",
        r"\bsorry\b.{0,40}\b(?:can'?t|cannot|unable)\b",
        r"\bich kann\b.{0,60}\bnicht\b",
        r"\bich habe keine\b",
        r"\bkeine (?:informationen|angaben|daten|auskunft)\b",
        r"\bleider\b.{0,40}\bnicht\b",
        r"\bals (?:ki|sprachmodell)\b",
        r"\bkann ich nicht\b",
    )
)


def looks_like_refusal(answer: str) -> bool:
    """Whether a *short* answer is a refusal rather than an answer.

    Empty or whitespace-only text is always a refusal. Otherwise the text must be
    short (`REFUSAL_MAX_CHARS`) *and* match a refusal pattern, which is what keeps
    a long hedged answer in the denominator where it belongs.
    """
    collapsed = " ".join(answer.translate(_ASCII_PUNCT).lower().split())
    if not collapsed:
        return True
    if len(collapsed) > REFUSAL_MAX_CHARS:
        return False
    return any(pattern.search(collapsed) for pattern in _REFUSAL_PATTERNS)


def classify_answer(answer: str, *, named_any_brand: bool = False) -> AnswerStatus:
    """Decide whether one response counts as a measurement at all.

    `named_any_brand` is the override that keeps the rule honest in both
    directions: a response that named *any* business -- ours or a competitor's --
    answered the question, whatever apology it opened with. Only a response that
    named nobody can be judged on refusal wording.

    Getting this wrong is costly in both directions and there is no neutral
    default. Misreading a refusal as an answer adds a false absence to the
    denominator and *understates* visibility; misreading an answer as a refusal
    shrinks the denominator and *overstates* it. The rule above is built to be
    wrong in the understating direction, because that is the direction a customer
    cannot be sold on.
    """
    if named_any_brand:
        return "answered"
    return "no_answer" if looks_like_refusal(answer) else "answered"


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #


def detect_presence(
    answer: str,
    *,
    brand: BrandIdentity,
    competitors: Sequence[BrandIdentity] = (),
) -> PresenceResult:
    """Read one answer for who is present in it.

    Folds the answer once and reuses it, so cost is linear in the number of
    identities rather than in the length of the text times the number of names.

    `matched_name` records *which* spelling matched, because "why did you count
    this?" has to be answerable from a stored row rather than by re-running the
    detector against a possibly-changed rule set.
    """
    folded = fold_for_matching(_prose_only(answer))
    hosts = extract_hosts(answer)

    matched_name: str | None = None
    for candidate in (brand.name, *brand.aliases):
        if mentions_name(folded, candidate):
            matched_name = candidate
            break

    matched_domains = _cited_domains(hosts, brand.domains)

    competitors_mentioned: list[str] = []
    competitors_cited: list[str] = []
    for rival in competitors:
        if any(mentions_name(folded, candidate) for candidate in (rival.name, *rival.aliases)):
            competitors_mentioned.append(rival.name)
        if _cited_domains(hosts, rival.domains):
            competitors_cited.append(rival.name)

    return PresenceResult(
        mentioned=matched_name is not None,
        cited=bool(matched_domains),
        matched_name=matched_name,
        matched_domains=matched_domains,
        competitors_mentioned=competitors_mentioned,
        competitors_cited=competitors_cited,
    )


def answer_excerpt(answer: str, *, limit: int = 280) -> str:
    """A short, whitespace-collapsed slice of an answer, for the evidence column.

    Truncated on a word boundary where one is close enough to the limit, because
    a mid-word cut reads like corruption. This is stored on every row: the number
    is only believable if the user can see what produced it.
    """
    collapsed = " ".join(answer.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:") + "..."
