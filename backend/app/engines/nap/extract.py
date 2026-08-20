"""Where a NAP audit's LISTINGS come from: the business's own crawled pages.

`audit_nap` compares a canonical record against listings and has been shipped and
tested since Phase 4. It was never called by the running agent for a boring reason:
nothing produced the listings. `nap/__init__.py` says "listing data is fetched by the
crawl layer and passed in", and no crawl-layer extractor existed, so `nap.audit` sat
in HARVEST's allowlist implemented by nothing.

This module is that extractor, and the scope it can honestly claim is narrower than
the engine's name suggests. **We do not read Gelbe Seiten, Das Örtliche or a Google
Business Profile.** Scraping a directory at scale is a terms-of-service and blocking
problem, and no paid aggregator is decided (`docs/FREE_CHANNELS.md` is the free-first
rule this respects). What we CAN read is the business's own site, which publishes its
NAP in two places and routinely disagrees with itself:

* **`LocalBusiness` / `Organization` JSON-LD**, which is what Google actually reads
  for the knowledge panel. It is structured, so extracting it is not guesswork;
* **the Impressum page**, which German law requires and which therefore carries the
  authoritative postal address and phone number in prose.

A finding from these two reads "your structured data says +49 261 123456 and your
Impressum says 0261/12 34 56" — a true, checkable statement about work the owner can
do today, and the exact input Google's entity resolution uses. It is a smaller claim
than a directory audit and it is a claim we can support.

Pure, like every engine: no network, no model, no database. It reads page facts the
crawl engine already produced.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from backend.app.engines.nap.contract import DirectoryListing

__all__ = ["IMPRESSUM_SOURCE", "JSONLD_SOURCE", "extract_nap_listings"]

#: Source keys. Echoed verbatim into every finding, so they have to read as an
#: explanation rather than as a code: "your structured data" and "your Impressum" are
#: places an owner can go and look.
JSONLD_SOURCE: Final = "website_jsonld"
IMPRESSUM_SOURCE: Final = "website_impressum"

#: schema.org types that describe the business itself.
#:
#: Kept SHORT on purpose. `LocalBusiness` has well over a hundred subtypes -- `Plumber`,
#: `Dentist`, `Bakery`, `Electrician`, `HVACBusiness` -- and schema.org keeps adding
#: them, so an enumeration is a list that silently stops finding addresses. The real
#: test is structural and lives in :func:`_is_business`: a block that publishes a
#: postal address or a telephone number is describing something with a physical
#: presence, and on a business's own site that is the business.
_BUSINESS_TYPES: Final[frozenset[str]] = frozenset(
    {"localbusiness", "organization", "corporation", "store", "restaurant"}
)

#: Types that carry an address or a phone number and are still NOT the business.
#:
#: `Person` is the interesting one and it is excluded deliberately: a German Impressum
#: names its Geschäftsführer, and while that person's address is usually the company's,
#: publishing a named individual as a business listing is an attribution we should not
#: make on a guess. `Event` and `Product` can both carry a location that is somewhere
#: else entirely.
_NOT_THE_BUSINESS: Final[frozenset[str]] = frozenset(
    {
        "person",
        "website",
        "webpage",
        "breadcrumblist",
        "searchaction",
        "article",
        "blogposting",
        "newsarticle",
        "product",
        "offer",
        "review",
        "event",
        "faqpage",
        "itemlist",
    }
)

#: German postcode followed by a place name. Five digits is unambiguous in DE, and
#: anchoring the city to it is what stops a house number or a price becoming a city.
_POSTCODE_CITY_RE: Final = re.compile(
    r"\b(?P<postcode>\d{5})\s+(?P<city>[A-ZÄÖÜ][\w.\-]*(?:[ \-][A-ZÄÖÜ][\w.\-]*){0,3})"
)

#: `Löhrstraße 12a`, `Am Markt 3`, `Hauptstr. 17-19`. The street-type suffix is what
#: keeps this from matching an arbitrary capitalised word followed by a number.
_STREET_RE: Final = re.compile(
    r"\b(?P<street>[A-ZÄÖÜ][\wäöüß.\-]*"
    r"(?:[ \-][A-ZÄÖÜa-zäöüß][\wäöüß.\-]*){0,3}"
    r"(?:stra(?:ss|ß)e|str\.|weg|platz|allee|gasse|ring|damm|ufer|markt))"
    r"[ ,]+(?P<number>\d{1,4}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,4}\s?[a-zA-Z]?)?)",
    re.IGNORECASE,
)

#: Deliberately permissive on separators and deliberately strict on length: German
#: numbers are written `0261/123456`, `+49 261 123 45 6`, `0261 - 12 34 56`, and the
#: normaliser folds all of those. Nine digits is the floor at which a match stops
#: being a price or a date.
_PHONE_RE: Final = re.compile(
    r"(?:tel(?:efon)?\.?|fon|phone|tel:)\s*:?\s*"
    r"(?P<phone>\+?[\d][\d\s()/.\-]{8,24}\d)",
    re.IGNORECASE,
)
_EMAIL_RE: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")

#: How a page announces itself as the imprint. `impressum` covers DE; the English
#: words are here because a bilingual site often serves both.
_IMPRESSUM_HINTS: Final = ("impressum", "imprint", "legal-notice", "legal_notice")

#: Characters of a page scanned for an address. The imprint is short and the address
#: is near the top of it; scanning a whole page invites a footer on an unrelated page
#: to contribute a phone number.
_SCAN_CHARS: Final = 4000


def extract_nap_listings(pages: Iterable[Any]) -> list[DirectoryListing]:
    """The business's own published NAP, as listings an audit can diff.

    Takes anything with the attributes `PageFacts` has (`url`, `title`, `main_text`,
    `jsonld_blocks`) rather than importing the crawl contract, so a caller can pass
    the summarised dict the run checkpoint carries without either engine depending on
    the other's shape.

    Returns at most one listing per source per page, de-duplicated across pages: a
    site with the same `LocalBusiness` block in every page footer would otherwise
    produce forty identical listings and an audit that reports every disagreement
    forty times.
    """
    found: list[DirectoryListing] = []
    seen: set[tuple[str, ...]] = set()

    for page in pages:
        for listing in (_from_jsonld(page), _from_impressum(page)):
            if listing is None:
                continue
            fingerprint = (
                listing.source,
                listing.legal_name or "",
                listing.street or "",
                listing.house_number or "",
                listing.postcode or "",
                listing.city or "",
                listing.phone or "",
                listing.email or "",
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            found.append(listing)

    return found


def _from_jsonld(page: Any) -> DirectoryListing | None:
    """The `LocalBusiness`/`Organization` block, if the page carries one."""
    for block in _blocks(page):
        if not _is_business(block.get("@type"), block):
            continue
        address = block.get("address")
        address_map: Mapping[str, Any] = address if isinstance(address, Mapping) else {}

        street_raw = _text(address_map.get("streetAddress"))
        street, number = _split_street(street_raw) if street_raw else (None, None)

        listing = DirectoryListing(
            source=JSONLD_SOURCE,
            legal_name=_text(block.get("legalName")) or _text(block.get("name")),
            trading_name=_text(block.get("name")),
            street=street,
            house_number=number,
            postcode=_text(address_map.get("postalCode")),
            city=_text(address_map.get("addressLocality")),
            phone=_text(block.get("telephone")),
            email=_text(block.get("email")),
        )
        # A block with a type and nothing else is not a listing. Returning it would
        # produce an audit full of `info` findings about fields the site never
        # published, which is noise dressed as a report.
        if _is_substantive(listing):
            return listing
    return None


def _from_impressum(page: Any) -> DirectoryListing | None:
    """The postal address and phone from an Impressum page, or None.

    Only ever reads a page that announces itself as the imprint. The same regexes
    against an arbitrary page would happily take a customer testimonial's town and a
    supplier's phone number and call them the business's own.
    """
    if not _is_impressum(page):
        return None

    text = str(getattr(page, "main_text", "") or "")[:_SCAN_CHARS]
    if not text:
        return None

    place = _POSTCODE_CITY_RE.search(text)
    street_match = _STREET_RE.search(text)
    phone_match = _PHONE_RE.search(text)
    email_match = _EMAIL_RE.search(text)

    listing = DirectoryListing(
        source=IMPRESSUM_SOURCE,
        street=street_match.group("street").strip() if street_match else None,
        house_number=street_match.group("number").strip() if street_match else None,
        postcode=place.group("postcode") if place else None,
        city=place.group("city").strip() if place else None,
        phone=phone_match.group("phone").strip() if phone_match else None,
        email=email_match.group(0) if email_match else None,
    )
    return listing if _is_substantive(listing) else None


def _blocks(page: Any) -> Sequence[Mapping[str, Any]]:
    raw = getattr(page, "jsonld_blocks", None)
    if raw is None and isinstance(page, Mapping):
        raw = page.get("jsonld_blocks")
    if not isinstance(raw, Sequence):
        return ()
    return [block for block in raw if isinstance(block, Mapping)]


def _is_business(declared: Any, block: Mapping[str, Any]) -> bool:
    """Whether a JSON-LD block describes the business itself.

    Structural rather than an enumeration, and that is the whole design: `@type` is
    legitimately a list (`["Organization", "Dentist"]`), schema.org has well over a
    hundred `LocalBusiness` subtypes, and a hardcoded list is a list that quietly stops
    finding a plumber's address the day it is out of date. So a block qualifies if it
    names a known business type, or ends in `Business`/`Service`, or simply PUBLISHES A
    POSTAL ADDRESS OR A PHONE NUMBER -- on a business's own site, a thing with a
    physical presence is the business.

    :data:`_NOT_THE_BUSINESS` is the deny half, checked first, because a few types
    legitimately carry a location that is somebody else's.
    """
    candidates = declared if isinstance(declared, list) else [declared]
    names = [str(candidate or "").strip().lower().rsplit("/", 1)[-1] for candidate in candidates]

    if any(name in _NOT_THE_BUSINESS for name in names):
        return False
    if any(
        name in _BUSINESS_TYPES or name.endswith("business") or name.endswith("service")
        for name in names
    ):
        return True
    return isinstance(block.get("address"), Mapping) or bool(block.get("telephone"))


def _is_impressum(page: Any) -> bool:
    haystack = " ".join(str(getattr(page, field, "") or "").lower() for field in ("url", "title"))
    if isinstance(page, Mapping):
        haystack += " " + " ".join(str(page.get(field) or "").lower() for field in ("url", "title"))
    return any(hint in haystack for hint in _IMPRESSUM_HINTS)


def _split_street(value: str) -> tuple[str | None, str | None]:
    """`"Löhrstraße 12a"` -> `("Löhrstraße", "12a")`, or the whole string as a street.

    The `nap` normaliser splits an inline house number itself, so this is a
    convenience rather than a requirement -- but keeping the two fields separate here
    means a JSON-LD listing and an Impressum listing are shaped identically, and the
    audit's per-field findings then line up between them.
    """
    match = _STREET_RE.search(value)
    if match:
        return match.group("street").strip(), match.group("number").strip()
    return value.strip() or None, None


def _is_substantive(listing: DirectoryListing) -> bool:
    """At least one field an audit can actually compare."""
    return any(
        (
            listing.legal_name,
            listing.trading_name,
            listing.street,
            listing.postcode,
            listing.city,
            listing.phone,
            listing.email,
        )
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        # schema.org allows a repeated property; the first value is the one a
        # directory would be given.
        return _text(value[0]) if value else None
    text = str(value).strip()
    return text or None
