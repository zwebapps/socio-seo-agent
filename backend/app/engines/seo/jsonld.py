"""Build and validate schema.org JSON-LD blocks.

Two jobs, deliberately in one module because they must agree: the builders emit
blocks, `validate_jsonld` judges them, and a builder whose own output fails the
validator is a bug the test suite catches directly.

Scope. This is *not* a schema.org conformance checker -- that would need the
vocabulary itself, which is a network fetch and therefore forbidden here. It
checks the properties that actually decide whether a block is eligible for a
rich result: `@context`, `@type`, and the small set of required properties for
the three types this product emits (`Article`, `LocalBusiness`, `FAQPage`).
Unknown types pass the generic checks and are not judged further, so a caller
emitting `Service` or `Product` is not blocked by our ignorance of it.

Builders take every value as an argument, including dates. Nothing reads the
clock: an engine that stamped `datePublished` with `now()` would return a
different result for the same input, which is the one property this layer is
not allowed to lose.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from backend.app.engines.seo.contract import SeoFinding

SCHEMA_CONTEXT: Final = "https://schema.org"

# Required properties per type, from Google's structured-data documentation for
# the corresponding rich result. Kept small: only properties whose absence makes
# the block ineligible, not every recommended property.
_REQUIRED_PROPERTIES: Final[dict[str, tuple[str, ...]]] = {
    "Article": ("headline",),
    "NewsArticle": ("headline",),
    "BlogPosting": ("headline",),
    "FAQPage": ("mainEntity",),
    "LocalBusiness": ("name", "address"),
}

_SCHEMA_EXPECTED: Final = 'a JSON-LD block with "@context" and "@type"'


def _finding(message: str, fix_hint: str) -> SeoFinding:
    """A `schema_invalid` finding. Always `warn`: missing or broken structured
    data costs rich-result eligibility, which is an opportunity, not a defect in
    the page's content -- so it must never be able to fail the whole gate on its
    own."""
    return SeoFinding(
        code="schema_invalid",
        severity="warn",
        message=message,
        fix_hint=fix_hint,
        measured=None,
        expected=_SCHEMA_EXPECTED,
    )


def _type_names(raw: Any) -> list[str]:
    """`@type` may be a string or a list of strings. Return the string ones."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def validate_jsonld(block: dict[str, Any]) -> list[SeoFinding]:
    """Validate one JSON-LD block. An empty list means the block is usable.

    Findings are returned rather than raised: this runs over markup an LLM wrote
    and a crawler fetched, where malformed structured data is an expected
    outcome, not an exceptional one.
    """
    findings: list[SeoFinding] = []

    context = block.get("@context")
    if context is None or (isinstance(context, str) and not context.strip()):
        findings.append(
            _finding(
                'JSON-LD block is missing "@context".',
                f'Add "@context": "{SCHEMA_CONTEXT}" to the JSON-LD block.',
            )
        )

    types = _type_names(block.get("@type"))
    if not types:
        findings.append(
            _finding(
                'JSON-LD block is missing a usable "@type".',
                'Add a schema.org "@type" to the JSON-LD block, for example '
                '"Article", "FAQPage" or "LocalBusiness".',
            )
        )

    for type_name in types:
        for prop in _REQUIRED_PROPERTIES.get(type_name, ()):
            value = block.get(prop)
            if value is None or (isinstance(value, str | list | dict) and len(value) == 0):
                findings.append(
                    _finding(
                        f'JSON-LD "{type_name}" block is missing required property "{prop}".',
                        f'Add "{prop}" to the "{type_name}" JSON-LD block; '
                        f"{type_name} is not eligible for a rich result without it.",
                    )
                )

    return findings


def _compact(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """Drop keys whose value is None or empty, preserving argument order.

    Emitting `"image": null` is worse than omitting the key: validators report it
    as a type error rather than as an absent optional property.
    """
    return {
        key: value
        for key, value in pairs
        if value is not None and not (isinstance(value, str | list | dict) and len(value) == 0)
    }


def build_article_jsonld(
    *,
    headline: str,
    url: str,
    date_published: str,
    author_name: str,
    publisher_name: str,
    description: str | None = None,
    date_modified: str | None = None,
    image_url: str | None = None,
    keywords: Sequence[str] | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Build an `Article` block.

    `date_published` / `date_modified` are passed through verbatim and should be
    ISO-8601. They are not parsed or defaulted here: guessing a publication date
    is a decision, and decisions belong to the caller, not to an engine.
    """
    return _compact(
        [
            ("@context", SCHEMA_CONTEXT),
            ("@type", "Article"),
            ("headline", headline),
            ("description", description),
            ("url", url),
            ("mainEntityOfPage", {"@type": "WebPage", "@id": url}),
            ("datePublished", date_published),
            ("dateModified", date_modified or date_published),
            ("author", {"@type": "Person", "name": author_name}),
            ("publisher", {"@type": "Organization", "name": publisher_name}),
            ("image", image_url),
            ("keywords", list(keywords) if keywords else None),
            ("inLanguage", language),
        ]
    )


def build_local_business_jsonld(
    *,
    name: str,
    url: str,
    street_address: str,
    city: str,
    postal_code: str,
    country: str,
    telephone: str | None = None,
    business_type: str = "LocalBusiness",
    email: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    opening_hours: Sequence[str] | None = None,
    price_range: str | None = None,
    image_url: str | None = None,
) -> dict[str, Any]:
    """Build a `LocalBusiness` block (or a subtype, via `business_type`).

    The address is always emitted as a nested `PostalAddress` rather than a
    single string, because that is the form the NAP-consistency work compares
    against directory listings -- a flattened address cannot be diffed field by
    field.
    """
    geo = (
        {"@type": "GeoCoordinates", "latitude": latitude, "longitude": longitude}
        if latitude is not None and longitude is not None
        else None
    )
    return _compact(
        [
            ("@context", SCHEMA_CONTEXT),
            ("@type", business_type),
            ("name", name),
            ("url", url),
            ("telephone", telephone),
            ("email", email),
            (
                "address",
                _compact(
                    [
                        ("@type", "PostalAddress"),
                        ("streetAddress", street_address),
                        ("addressLocality", city),
                        ("postalCode", postal_code),
                        ("addressCountry", country),
                    ]
                ),
            ),
            ("geo", geo),
            ("openingHours", list(opening_hours) if opening_hours else None),
            ("priceRange", price_range),
            ("image", image_url),
        ]
    )


def build_faq_jsonld(
    questions: Sequence[tuple[str, str]] | Mapping[str, str],
) -> dict[str, Any]:
    """Build an `FAQPage` block from ``(question, answer)`` pairs.

    Order is preserved -- a mapping is iterated in insertion order and a
    sequence in its own order -- because the block should mirror the FAQ as it
    is rendered on the page. Empty questions or answers are dropped rather than
    emitted blank; an `FAQPage` with a blank answer is rejected by validators, so
    silently shipping one would cost the whole block.
    """
    pairs = list(questions.items()) if isinstance(questions, Mapping) else list(questions)
    entities = [
        {
            "@type": "Question",
            "name": question,
            "acceptedAnswer": {"@type": "Answer", "text": answer},
        }
        for question, answer in pairs
        if question.strip() and answer.strip()
    ]
    return _compact([("@context", SCHEMA_CONTEXT), ("@type", "FAQPage"), ("mainEntity", entities)])


def render_jsonld_script(block: Mapping[str, Any]) -> str:
    """Serialise a block as an embeddable `<script>` tag.

    `</` is escaped so the JSON payload can never terminate the surrounding
    script element early -- the one XSS-shaped hazard in emitting JSON-LD into
    HTML. `sort_keys` is off so the builders' deliberate key order survives, and
    `ensure_ascii` is off so German text stays readable in the page source.
    """
    payload = json.dumps(dict(block), ensure_ascii=False, indent=2).replace("</", "<\\/")
    return f'<script type="application/ld+json">\n{payload}\n</script>'
