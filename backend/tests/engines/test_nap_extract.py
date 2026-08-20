"""Where a NAP audit's listings come from: the business's own pages.

`audit_nap` shipped in Phase 4 and the agent never called it, because nothing
produced listings to diff — `nap.audit` sat in HARVEST's allowlist implemented by
nothing. These tests are about the two sources this extractor can honestly read (the
`LocalBusiness` JSON-LD and the Impressum) and, just as importantly, about what it
refuses to read: the same regexes against an arbitrary page would take a
testimonial's town and a supplier's phone number and call them the business's own.
"""

from typing import Any

from backend.app.engines.crawl.contract import PageFacts
from backend.app.engines.nap import (
    IMPRESSUM_SOURCE,
    JSONLD_SOURCE,
    extract_nap_listings,
)

LOCAL_BUSINESS: dict[str, Any] = {
    "@context": "https://schema.org",
    "@type": "Plumber",
    "name": "Müller Sanitär GmbH",
    "telephone": "+49 261 123456",
    "email": "info@mueller-sanitaer.de",
    "address": {
        "@type": "PostalAddress",
        "streetAddress": "Löhrstraße 12a",
        "postalCode": "56068",
        "addressLocality": "Koblenz",
    },
}

IMPRESSUM_TEXT = (
    "Impressum\n\n"
    "Müller Sanitär GmbH\n"
    "Löhrstraße 12a\n"
    "56068 Koblenz\n"
    "Telefon: 0261 / 12 34 56\n"
    "E-Mail: info@mueller-sanitaer.de\n"
    "Geschäftsführer: Anna Müller\n"
)


def _page(**over: Any) -> PageFacts:
    base: dict[str, Any] = {
        "url": "https://mueller-sanitaer.de/",
        "title": "Müller Sanitär GmbH",
        "main_text": "Ihr Partner für Sanitär und Heizung.",
        "jsonld_blocks": [],
    }
    base.update(over)
    return PageFacts(**base)


# --------------------------------------------------------------------------- #
# The two sources it can read
# --------------------------------------------------------------------------- #


def test_a_local_business_block_becomes_a_listing() -> None:
    """Structured data is what Google actually reads for the knowledge panel, so it is
    the one source here that needs no guessing."""
    listings = extract_nap_listings([_page(jsonld_blocks=[LOCAL_BUSINESS])])

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == JSONLD_SOURCE
    assert listing.trading_name == "Müller Sanitär GmbH"
    assert listing.street == "Löhrstraße"
    assert listing.house_number == "12a"
    assert listing.postcode == "56068"
    assert listing.city == "Koblenz"
    assert listing.phone == "+49 261 123456"
    assert listing.email == "info@mueller-sanitaer.de"


def test_a_subtype_nobody_enumerated_is_still_a_business() -> None:
    """`LocalBusiness` has dozens of subtypes and schema.org keeps adding them, so a
    hardcoded list would go stale and silently stop finding addresses."""
    listings = extract_nap_listings(
        [_page(jsonld_blocks=[{**LOCAL_BUSINESS, "@type": ["Organization", "Dentist"]}])]
    )

    assert [listing.source for listing in listings] == [JSONLD_SOURCE]


def test_the_impressum_yields_the_address_german_law_requires_it_to_publish() -> None:
    listings = extract_nap_listings(
        [
            _page(
                url="https://mueller-sanitaer.de/impressum",
                title="Impressum",
                main_text=IMPRESSUM_TEXT,
            )
        ]
    )

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == IMPRESSUM_SOURCE
    assert listing.postcode == "56068"
    assert listing.city == "Koblenz"
    assert listing.street == "Löhrstraße"
    assert listing.house_number == "12a"
    assert listing.phone == "0261 / 12 34 56"
    assert listing.email == "info@mueller-sanitaer.de"


def test_both_sources_are_returned_so_the_audit_can_diff_them_against_each_other() -> None:
    """The whole point of reading two places: a site's structured data and its
    Impressum routinely disagree, and that disagreement is the finding."""
    pages = [
        _page(jsonld_blocks=[{**LOCAL_BUSINESS, "telephone": "+49 261 999999"}]),
        _page(url="https://x.de/impressum", title="Impressum", main_text=IMPRESSUM_TEXT),
    ]

    listings = extract_nap_listings(pages)

    assert {listing.source for listing in listings} == {JSONLD_SOURCE, IMPRESSUM_SOURCE}


# --------------------------------------------------------------------------- #
# What it refuses to read
# --------------------------------------------------------------------------- #


def test_an_ordinary_page_yields_nothing_even_when_it_mentions_a_town() -> None:
    """A testimonial naming a town and a supplier's phone number are not the
    business's own NAP, and reporting them as listings would produce an audit full of
    inconsistencies the owner cannot act on because they are not real."""
    listings = extract_nap_listings(
        [
            _page(
                url="https://mueller-sanitaer.de/referenzen",
                title="Referenzen",
                main_text=(
                    "Familie Schmidt aus 56070 Neuendorf war zufrieden. "
                    "Unser Zulieferer erreichen Sie unter Telefon: 0221 987654."
                ),
            )
        ]
    )

    assert listings == []


def test_a_json_ld_block_that_is_only_a_type_is_not_a_listing() -> None:
    """An audit of a listing with no fields is a column of `info` findings about
    values the site never published — noise dressed as a report."""
    listings = extract_nap_listings([_page(jsonld_blocks=[{"@type": "LocalBusiness"}])])

    assert listings == []


def test_a_website_block_is_not_the_business() -> None:
    """`WebSite` and `BreadcrumbList` blocks are on nearly every page and describe the
    site, not the company."""
    listings = extract_nap_listings(
        [_page(jsonld_blocks=[{"@type": "WebSite", "name": "Müller Sanitär"}])]
    )

    assert listings == []


def test_the_same_footer_block_on_forty_pages_produces_one_listing() -> None:
    """Otherwise an audit reports every disagreement forty times, and a real finding is
    buried in its own duplicates."""
    listings = extract_nap_listings([_page(jsonld_blocks=[LOCAL_BUSINESS]) for _ in range(40)])

    assert len(listings) == 1
