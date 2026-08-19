"""The serp engine: keyword expansion, intent, and competitor discovery.

Written before the module. Everything here is hermetic — the provider is a seam,
and no test performs a search.

The judgement this engine encodes: a keyword list is worthless without intent.
"Klempner Koblenz" and "wie funktioniert eine Waermepumpe" cost the same to rank
for and are worth wildly different amounts, so intent classification is the part
that has to be right.
"""

import pytest

from backend.app.engines.serp import (
    FakeSerpProvider,
    Intent,
    SerpPage,
    SerpResult,
    classify_intent,
    discover_competitors,
    expand_keywords,
    serp_config_status,
)


def _page(query: str, hosts: list[str], related: list[str] | None = None) -> SerpPage:
    return SerpPage(
        query=query,
        locale="de",
        results=[
            SerpResult(
                position=i + 1,
                url=f"https://{h}/seite",
                title=f"{query} — {h}",
                snippet="…",
                host=h,
            )
            for i, h in enumerate(hosts)
        ],
        related_queries=related or [],
    )


# --------------------------------------------------------------------------- #
# Intent — the part that decides what a keyword is worth
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        # Local: a place name present.
        ("klempner koblenz", Intent.LOCAL),
        ("sanitaer notdienst in koblenz", Intent.LOCAL),
        ("plumber near koblenz", Intent.LOCAL),
        # Commercial: money or hiring language.
        ("badsanierung kosten", Intent.COMMERCIAL),
        ("heizung preis pro qm", Intent.COMMERCIAL),
        ("klempner beauftragen", Intent.COMMERCIAL),
        ("how much does a boiler cost", Intent.COMMERCIAL),
        # Comparison.
        ("waermepumpe vs gasheizung", Intent.COMPARISON),
        ("buderus oder viessmann vergleich", Intent.COMPARISON),
        ("best boiler alternative", Intent.COMPARISON),
        # Informational.
        ("wie funktioniert eine waermepumpe", Intent.INFORMATIONAL),
        ("was ist eine spuelkasten dichtung", Intent.INFORMATIONAL),
        ("how to bleed a radiator", Intent.INFORMATIONAL),
    ],
)
def test_intent_is_classified_deterministically(term: str, expected: Intent) -> None:
    assert classify_intent(term, city="Koblenz") == expected


def test_local_beats_commercial_when_both_signals_are_present() -> None:
    """ "badsanierung kosten koblenz" is a local job enquiry, not a research query.
    Local intent converts far better, so it must win the tie."""
    assert classify_intent("badsanierung kosten koblenz", city="Koblenz") == Intent.LOCAL


def test_intent_is_stable_without_a_city() -> None:
    assert classify_intent("klempner koblenz", city=None) == Intent.INFORMATIONAL


def test_classification_is_case_and_umlaut_insensitive() -> None:
    assert classify_intent("BADSANIERUNG KOSTEN", city=None) == Intent.COMMERCIAL
    assert classify_intent("Wärmepumpe vs Gasheizung", city=None) == Intent.COMPARISON


# --------------------------------------------------------------------------- #
# Expansion
# --------------------------------------------------------------------------- #


def test_expansion_returns_the_seed_and_the_related_queries() -> None:
    page = _page(
        "klempner koblenz", ["a.de"], related=["klempner koblenz notdienst", "sanitaer koblenz"]
    )
    candidates = expand_keywords("klempner koblenz", pages=[page], city="Koblenz")

    terms = [c.term for c in candidates]
    assert "klempner koblenz" in terms
    assert "klempner koblenz notdienst" in terms
    assert "sanitaer koblenz" in terms


def test_expansion_deduplicates_and_normalises() -> None:
    page = _page(
        "x", ["a.de"], related=["Klempner  Koblenz", "klempner koblenz", "KLEMPNER KOBLENZ"]
    )
    candidates = expand_keywords("klempner koblenz", pages=[page], city="Koblenz")
    assert len([c for c in candidates if c.term == "klempner koblenz"]) == 1


def test_expansion_carries_intent_and_sorts_commercial_intent_first() -> None:
    """The list is read top-down by a human and by the agent. Burying the
    money terms under blog topics is how a keyword list wastes a month."""
    page = _page(
        "sanitaer",
        ["a.de"],
        related=["wie entlueftet man einen heizkoerper", "badsanierung kosten koblenz"],
    )
    candidates = expand_keywords("sanitaer", pages=[page], city="Koblenz")

    assert candidates[0].intent in (Intent.LOCAL, Intent.COMMERCIAL)
    assert candidates[-1].intent == Intent.INFORMATIONAL


def test_expansion_of_nothing_is_empty_not_an_error() -> None:
    assert expand_keywords("", pages=[], city=None) == []


# --------------------------------------------------------------------------- #
# Competitors
# --------------------------------------------------------------------------- #


def test_competitors_are_ranked_by_how_often_they_appear() -> None:
    pages = [
        _page("q1", ["rival.de", "other.de", "gelbeseiten.de"]),
        _page("q2", ["rival.de", "gelbeseiten.de"]),
        _page("q3", ["rival.de"]),
    ]
    found = discover_competitors(pages, own_host="mine.de")

    assert found[0].host == "rival.de"
    assert found[0].appearances == 3


def test_directories_and_aggregators_are_not_competitors() -> None:
    """Gelbe Seiten outranking a plumber is not a plumber to compete with. Listing
    a directory as a competitor sends the whole strategy after the wrong target."""
    pages = [
        _page("q", ["gelbeseiten.de", "dasoertliche.de", "yelp.de", "wikipedia.org", "rival.de"])
    ]
    hosts = [c.host for c in discover_competitors(pages, own_host="mine.de")]

    assert hosts == ["rival.de"]


def test_our_own_site_is_never_a_competitor() -> None:
    pages = [_page("q", ["mine.de", "www.mine.de", "rival.de"])]
    hosts = [c.host for c in discover_competitors(pages, own_host="mine.de")]
    assert "mine.de" not in hosts and "www.mine.de" not in hosts


def test_best_position_is_kept_not_the_last_one_seen() -> None:
    pages = [_page("q1", ["a.de", "rival.de"]), _page("q2", ["rival.de", "a.de"])]
    rival = next(c for c in discover_competitors(pages, own_host="mine.de") if c.host == "rival.de")
    assert rival.best_position == 1


# --------------------------------------------------------------------------- #
# The provider seam
# --------------------------------------------------------------------------- #


async def test_the_fake_provider_is_deterministic() -> None:
    a = await FakeSerpProvider().search("klempner koblenz", locale="de", limit=5)
    b = await FakeSerpProvider().search("klempner koblenz", locale="de", limit=5)
    assert a.model_dump() == b.model_dump()


async def test_the_fake_provider_respects_the_limit() -> None:
    page = await FakeSerpProvider().search("q", locale="de", limit=3)
    assert len(page.results) == 3


async def test_the_fake_provider_returns_plausible_related_queries() -> None:
    page = await FakeSerpProvider().search("badsanierung", locale="de", limit=5)
    assert page.related_queries, "expansion has nothing to work with otherwise"


def test_no_api_key_reports_the_fake_rather_than_pretending() -> None:
    """A search that silently returns invented results is worse than no search: the
    agent would plan a month of content against fiction."""
    status = serp_config_status(env={})
    assert status.using_fake is True
    assert "fake" in status.message.lower()


def test_a_configured_key_reports_the_real_provider() -> None:
    status = serp_config_status(env={"TAVILY_API_KEY": "tvly-x"})
    assert status.using_fake is False
    assert status.provider == "tavily"
