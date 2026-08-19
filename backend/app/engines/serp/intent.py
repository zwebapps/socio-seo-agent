"""Intent classification: deterministic rules, no model.

Why rules and not an LLM: this runs over hundreds of terms per business, the answer
must be identical between runs so a keyword list can be diffed, and the signals are
literally keyword presence. An LLM here would cost money to be less reliable.

German first, English alongside, because the target market is Germany and a
German-blind classifier would label most of a real keyword list "informational".
"""

import re
import unicodedata

from backend.app.engines.serp.contract import Intent

# Money and hiring language. A searcher using these has a budget in mind.
_COMMERCIAL = (
    "kosten",
    "preis",
    "preise",
    "kostet",
    "guenstig",
    "billig",
    "angebot",
    "angebote",
    "beauftragen",
    "buchen",
    "bestellen",
    "kaufen",
    "mieten",
    "auftrag",
    "offerte",
    "cost",
    "costs",
    "price",
    "pricing",
    "cheap",
    "quote",
    "hire",
    "buy",
    "book",
    "order",
)

# Comparison shapes. These convert, but later in the journey than a local job.
_COMPARISON = (
    "vs",
    "versus",
    "vergleich",
    "vergleichen",
    "oder",
    "unterschied",
    "alternative",
    "alternativen",
    "besser",
    "test",
    "testsieger",
    "compare",
    "comparison",
    "difference",
    "best",
    "alternatives",
    "review",
    "reviews",
)

# Research shapes.
_INFORMATIONAL = (
    "wie",
    "was",
    "warum",
    "wieso",
    "wann",
    "welche",
    "welcher",
    "anleitung",
    "tipps",
    "bedeutung",
    "erklaert",
    "funktioniert",
    "selber",
    "diy",
    "how",
    "what",
    "why",
    "when",
    "which",
    "guide",
    "tips",
    "tutorial",
    "meaning",
    "explained",
)

# Words that make a query local even without a place name.
_LOCAL_MARKERS = (
    "in der naehe",
    "naehe",
    "umgebung",
    "vor ort",
    "near me",
    "near",
    "nearby",
    "local",
)

_WORD = re.compile(r"[a-z0-9]+")


def fold(value: str) -> str:
    """Lowercase, strip accents, expand German digraphs.

    ``Wärmepumpe`` and ``waermepumpe`` are the same query to a searcher, so they must
    be the same query here. Folding happens once, in one place, so every rule below
    sees the same shape.
    """
    lowered = value.lower().strip()
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(source, target)
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _tokens(value: str) -> set[str]:
    return set(_WORD.findall(fold(value)))


def classify_intent(term: str, *, city: str | None) -> Intent:
    """Classify one term.

    Precedence is deliberate and is the only interesting decision in this module:

    LOCAL wins outright. "badsanierung kosten koblenz" carries a money word, but it
    is a person in Koblenz wanting a bathroom done -- a job enquiry, not research.
    Ranking it as COMMERCIAL would bury it beneath national research terms in a list
    that is read top-down.

    Then COMMERCIAL, then COMPARISON, then INFORMATIONAL as the floor: an
    unrecognised term is treated as low-value rather than optimistically, because
    over-promising on a keyword list is the more expensive error.
    """
    folded = fold(term)
    tokens = _tokens(term)

    if city:
        city_tokens = _tokens(city)
        if city_tokens and city_tokens <= tokens:
            return Intent.LOCAL
    if any(marker in folded for marker in _LOCAL_MARKERS):
        return Intent.LOCAL

    if tokens & set(_COMMERCIAL):
        return Intent.COMMERCIAL
    if tokens & set(_COMPARISON):
        return Intent.COMPARISON
    if tokens & set(_INFORMATIONAL):
        return Intent.INFORMATIONAL

    return Intent.INFORMATIONAL
