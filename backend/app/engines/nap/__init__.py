"""NAP consistency audit engine: name, address and phone across every directory.

Local search ranking and entity resolution both depend on a business's name,
address and phone being *identical* everywhere it is listed. They almost never
are, because every listing was typed by a different person on a different day.
Telling a business "your phone number differs across four listings and your name
has three spellings" is concrete, verifiable and immediately actionable -- and no
tool they own does it.

Two functions, both pure:

    canonical = normalise_nap(raw)                  # one true record + comparison forms
    result    = audit_nap(canonical, listings)      # findings + a 0-100 score

Deterministic, no I/O, no network, no LLM. Listing data is fetched by the crawl
layer and passed in; this engine only computes. The LLM's job elsewhere is to
explain what an inconsistency costs -- never to find one.

The normaliser is the load-bearing part. A false "inconsistency" from a naive
string compare destroys trust in every other row on the screen, so
``tests/engines/test_nap.py`` spends most of its cases proving that the German
equivalences -- ``Str.``/``Straße``, ``Müller``/``Mueller``, ``GmbH`` suffixes,
``0261/123456``/``+49 261 123456``, ``D-56068``/``56068`` -- produce no findings
at all.
"""

from .audit import audit_nap, consistency_score
from .contract import (
    CanonicalNap,
    DirectoryListing,
    NapAuditResult,
    NapComparison,
    NapField,
    NapFieldSet,
    NapFinding,
    RawNap,
    Severity,
)
from .normalise import (
    comparison_form,
    fold_for_comparison,
    normalise_business_name,
    normalise_city,
    normalise_email,
    normalise_house_number,
    normalise_nap,
    normalise_opening_hours,
    normalise_phone,
    normalise_postcode,
    normalise_street_name,
    split_street_and_number,
)

__all__ = [
    "CanonicalNap",
    "DirectoryListing",
    "NapAuditResult",
    "NapComparison",
    "NapField",
    "NapFieldSet",
    "NapFinding",
    "RawNap",
    "Severity",
    "audit_nap",
    "comparison_form",
    "consistency_score",
    "fold_for_comparison",
    "normalise_business_name",
    "normalise_city",
    "normalise_email",
    "normalise_house_number",
    "normalise_nap",
    "normalise_opening_hours",
    "normalise_phone",
    "normalise_postcode",
    "normalise_street_name",
    "split_street_and_number",
]
