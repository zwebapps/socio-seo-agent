"""Keyword expansion and competitor discovery. Pure functions over SerpPages."""

import re
from collections.abc import Sequence

from backend.app.engines.serp.contract import (
    INTENT_RANK,
    CompetitorCandidate,
    KeywordCandidate,
    SerpPage,
)
from backend.app.engines.serp.intent import classify_intent, fold

_WHITESPACE = re.compile(r"\s+")

#: Hosts that are directories, aggregators or reference sites rather than
#: businesses to compete with. Gelbe Seiten outranking a plumber is not a plumber:
#: naming a directory as a competitor points the whole content strategy at a target
#: that cannot be beaten and would not be worth beating.
#:
#: Matched on the registrable-looking suffix, so `www.gelbeseiten.de` and
#: `m.yelp.de` are both excluded.
NON_COMPETITOR_HOSTS: frozenset[str] = frozenset(
    {
        # German directories
        "gelbeseiten.de",
        "dasoertliche.de",
        "11880.com",
        "meinestadt.de",
        "cylex.de",
        "wlw.de",
        "werkenntdenbesten.de",
        "provenexpert.com",
        "yelp.de",
        "yelp.com",
        "trustpilot.com",
        "golocal.de",
        "stadtbranchenbuch.com",
        "marktplatz-mittelstand.de",
        # Platforms and marketplaces
        "google.com",
        "google.de",
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "youtube.com",
        "tiktok.com",
        "pinterest.de",
        "pinterest.com",
        "x.com",
        "twitter.com",
        "amazon.de",
        "ebay.de",
        "etsy.com",
        "myhammer.de",
        "check24.de",
        "aroundhome.de",
        # Reference
        "wikipedia.org",
        "wiktionary.org",
        "reddit.com",
        "quora.com",
        "gutefrage.net",
        "wikihow.com",
    }
)


def normalise_term(value: str) -> str:
    """One spelling per term, so a list does not repeat itself."""
    return _WHITESPACE.sub(" ", value.strip()).lower()


def normalise_host(value: str) -> str:
    """Drop a leading ``www.`` or ``m.`` so one site is one host."""
    host = value.strip().lower().removeprefix("www.").removeprefix("m.")
    return host


def is_competitor_host(host: str, *, own_host: str | None) -> bool:
    """Whether a host is a business we are actually competing against."""
    candidate = normalise_host(host)
    if not candidate:
        return False
    if own_host and candidate == normalise_host(own_host):
        return False
    return not any(
        candidate == excluded or candidate.endswith(f".{excluded}")
        for excluded in NON_COMPETITOR_HOSTS
    )


def expand_keywords(
    seed: str,
    *,
    pages: Sequence[SerpPage],
    city: str | None,
) -> list[KeywordCandidate]:
    """Build a deduplicated, intent-sorted candidate list from search output.

    Sorted by intent, then by how often the term was seen, then alphabetically. The
    alphabetical tiebreak exists so the output is stable: an unstable order makes
    two runs look different when nothing changed, and a keyword list that appears to
    churn stops being trusted.

    ``seen`` is a count of appearances, NOT a search volume. We do not have volume
    data and labelling a proxy as volume would be inventing a number.
    """
    counts: dict[str, int] = {}
    sources: dict[str, str] = {}

    def add(term: str, source: str) -> None:
        normalised = normalise_term(term)
        if not normalised or len(normalised) < 3:
            return
        counts[normalised] = counts.get(normalised, 0) + 1
        # Keep the earliest source: "seed" is more informative than "related".
        sources.setdefault(normalised, source)

    add(seed, "seed")
    for page in pages:
        for related in page.related_queries:
            add(related, "related")

    candidates = [
        KeywordCandidate(
            term=term,
            intent=classify_intent(term, city=city),
            source=sources[term],
            seen=count,
        )
        for term, count in counts.items()
    ]

    return sorted(
        candidates,
        key=lambda c: (INTENT_RANK[c.intent], -c.seen, fold(c.term)),
    )


def discover_competitors(
    pages: Sequence[SerpPage],
    *,
    own_host: str | None,
) -> list[CompetitorCandidate]:
    """Hosts that keep appearing where this business wants to be.

    Ranked by appearances, then by best position. Appearing three times at position
    eight is a stronger signal of a systematic competitor than appearing once at
    position one, which may be a single lucky page.
    """
    appearances: dict[str, int] = {}
    best: dict[str, int] = {}

    for page in pages:
        for result in page.results:
            if not is_competitor_host(result.host, own_host=own_host):
                continue
            host = normalise_host(result.host)
            appearances[host] = appearances.get(host, 0) + 1
            best[host] = min(best.get(host, result.position), result.position)

    return sorted(
        (
            CompetitorCandidate(host=host, appearances=count, best_position=best[host])
            for host, count in appearances.items()
        ),
        key=lambda c: (-c.appearances, c.best_position, c.host),
    )
