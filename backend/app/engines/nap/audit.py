"""Diff every directory listing against the canonical record, and score the result.

The detection path is pure string comparison over the normalised forms from
:mod:`.normalise`. There is no LLM here and there must never be: finding an
inconsistency is normalisation, not language understanding. Elsewhere in the
system an agent explains what an inconsistency *costs* -- that is a different
job, done on top of these findings, never in place of them.

Two rules shape every decision below:

* **Never claim a difference we cannot prove.** A value we could not parse
  produces "could not be verified", not "wrong". A field the directory left
  empty produces ``info``, because we cannot know whether the directory even
  offers that field.
* **Severity follows consequence.** A wrong phone number or postcode misroutes a
  real customer and breaks the entity match that local ranking depends on, so
  those are ``error``. A name or street variant is a ranking drag, so ``warn``.
"""

from .contract import (
    CanonicalNap,
    DirectoryListing,
    NapAuditResult,
    NapComparison,
    NapField,
    NapFinding,
    Severity,
)
from .normalise import (
    comparison_form,
    fold_for_comparison,
    phone_extension_difference,
    split_street_and_number,
    strip_address_annotation,
)

__all__ = ["audit_nap", "consistency_score"]


# Human labels, used verbatim in the fix hints. Written for a UI, not for a log.
_LABELS: dict[NapField, str] = {
    "legal_name": "legal name",
    "trading_name": "business name",
    "street": "street",
    "house_number": "house number",
    "postcode": "postcode",
    "city": "city",
    "phone": "phone number",
    "email": "email address",
    "opening_hours": "opening hours",
    "primary_category": "category",
}

# Severity of a proven mismatch, per field. Phone and postcode break the entity
# match and misroute customers; name and street variants suppress ranking.
_MISMATCH_SEVERITY: dict[NapField, Severity] = {
    "legal_name": "warn",
    "trading_name": "warn",
    "street": "warn",
    "house_number": "warn",
    "postcode": "error",
    "city": "warn",
    "phone": "error",
    "email": "warn",
    # Hours drift legitimately and are parsed from free text, so a difference is
    # worth showing but never worth asserting as a ranking fault.
    "opening_hours": "info",
}

# Severity when a value is present but unreadable. Defaults to ``warn``: we
# cannot prove a mismatch. A German postcode is the exception -- "not five
# digits" is provably wrong, not merely unparsed.
_UNVERIFIABLE_SEVERITY: dict[NapField, Severity] = {"postcode": "error"}

# Fields whose absence on a listing is worth reporting: the core NAP triple. Email,
# hours and category are routinely absent by design, and reporting each of them on
# each directory would bury the findings that matter.
_MISSING_REPORTED: frozenset[NapField] = frozenset(
    {"street", "house_number", "postcode", "city", "phone"}
)

# Fields where an unreadable value is not reported at all. "Termine nach
# Vereinbarung" is a legitimate answer to "when are you open?", and flagging it
# would be noise on every listing that has it.
_UNVERIFIABLE_SILENT: frozenset[NapField] = frozenset({"opening_hours"})

# Compared in this order, so the findings list reads like an address.
_COMPARED_FIELDS: tuple[NapField, ...] = (
    "street",
    "house_number",
    "postcode",
    "city",
    "phone",
    "email",
    "opening_hours",
)

_NAME_FIELDS: tuple[NapField, ...] = ("legal_name", "trading_name")

# The score ledger. Deliberately a flat deduction per finding rather than a ratio:
# a user must be able to reconcile the number with the list in front of them, and
# one wrong phone number on one directory really is worth 15 points.
_PENALTY: dict[Severity, int] = {"error": 15, "warn": 5, "info": 0}


def consistency_score(findings: list[NapFinding]) -> int:
    """Score 0-100 from a finding list: ``100 - 15``/error ``- 5``/warn, floored at 0.

    ``info`` findings cost nothing. They record something we could not verify --
    usually a field a directory does not offer -- and a score that punished
    ignorance would punish the business for someone else's schema.

    Consequences worth stating: the score is 100 exactly when nothing mismatched,
    it is strictly lower as soon as anything did, and from seven errors onward it
    reads 0. Beyond that point the number stops discriminating and the findings
    list is the only thing worth looking at anyway.
    """
    return max(0, 100 - sum(_PENALTY[finding.severity] for finding in findings))


def _mismatch_hint(field: NapField, source: str, canonical: str | None, found: str | None) -> str:
    label = _LABELS[field]
    return f'Set the {label} on {source} to "{canonical}" -- it currently reads "{found}".'


def _missing_hint(field: NapField, source: str, canonical: str | None) -> str:
    label = _LABELS[field]
    return f'Add the {label} "{canonical}" to the {source} listing -- the field is empty.'


def _unverifiable_hint(
    field: NapField, source: str, canonical: str | None, found: str | None
) -> str:
    label = _LABELS[field]
    return (
        f'Check the {label} on {source}: "{found}" could not be read as a valid {label}; '
        f'the canonical value is "{canonical}".'
    )


def _extension_hint(
    source: str, canonical: str | None, found: str | None, extension: str, on_listing: bool
) -> str:
    detail = "with" if on_listing else "without"
    return (
        f"{source} shows the same line {detail} the extension -{extension} "
        f'("{found}"); the canonical number is "{canonical}". Not a wrong number, '
        f"but one consistent number across every listing matches more reliably."
    )


def _annotation_hint(source: str, canonical: str | None, found: str | None, annotation: str) -> str:
    return (
        f'{source} appends "{annotation}" to the address ("{found}"); the address itself '
        f'matches "{canonical}". Keep it if deliveries need it -- it does not affect the match.'
    )


def _matches(field: NapField, canonical: str, found: str) -> bool:
    """Compare two already-normalised values.

    Equality everywhere except city, where a token subset counts as a match so an
    appended district ("Köln Innenstadt" vs "Köln") is not reported as a different
    city. Postcode is an ``error``-severity field and does the real discriminating
    between same-named places, so leniency here costs nothing.
    """
    if field == "city":
        canonical_tokens, found_tokens = set(canonical.split()), set(found.split())
        return canonical_tokens <= found_tokens or found_tokens <= canonical_tokens
    return canonical == found


def _name_findings(
    canonical: CanonicalNap,
    listing: DirectoryListing,
    listing_comparison: NapComparison,
) -> list[NapFinding]:
    """Compare the listing's name(s) against *either* canonical name.

    Directories have one name field and no rule about which name goes in it, so a
    listing carrying the legal name where we hold a trading name is correct data,
    not an inconsistency. Only a name matching neither is reported.
    """
    accepted = {
        value
        for value in (canonical.comparison.legal_name, canonical.comparison.trading_name)
        if value is not None
    }
    if not accepted:
        return []

    present = [
        (field, str(getattr(listing, field)), getattr(listing_comparison, field))
        for field in _NAME_FIELDS
        if getattr(listing, field)
    ]

    preferred: NapField = "trading_name" if canonical.trading_name else "legal_name"
    if not present:
        return [
            NapFinding(
                field=preferred,
                canonical_value=getattr(canonical, preferred),
                found_value=None,
                source=listing.source,
                severity="info",
                fix_hint=_missing_hint(preferred, listing.source, getattr(canonical, preferred)),
            )
        ]

    findings: list[NapFinding] = []
    for field, raw, normalised in present:
        if normalised is not None and normalised in accepted:
            continue
        expected = getattr(canonical, field) or getattr(canonical, preferred)
        findings.append(
            NapFinding(
                field=field,
                canonical_value=expected,
                found_value=raw,
                source=listing.source,
                severity=_MISMATCH_SEVERITY[field],
                fix_hint=_mismatch_hint(field, listing.source, expected, raw),
            )
        )
    return findings


def _field_finding(
    canonical: CanonicalNap,
    listing: DirectoryListing,
    field: NapField,
    found_raw: str | None,
    found_normalised: str | None,
) -> NapFinding | None:
    """Compare one field of one listing. ``None`` means "nothing to report"."""
    canonical_normalised: str | None = getattr(canonical.comparison, field)
    canonical_display: str | None = getattr(canonical, field)
    if canonical_normalised is None:
        # We hold no verified value for this field, so there is nothing to diff
        # against. That is our own gap, not the directory's, and inventing a
        # finding from it would be exactly the false positive this engine exists
        # to avoid.
        return None

    if not found_raw:
        if field not in _MISSING_REPORTED:
            return None
        return NapFinding(
            field=field,
            canonical_value=canonical_display,
            found_value=None,
            source=listing.source,
            severity="info",
            fix_hint=_missing_hint(field, listing.source, canonical_display),
        )

    if found_normalised is None:
        if field in _UNVERIFIABLE_SILENT:
            return None
        return NapFinding(
            field=field,
            canonical_value=canonical_display,
            found_value=found_raw,
            source=listing.source,
            severity=_UNVERIFIABLE_SEVERITY.get(field, "warn"),
            fix_hint=_unverifiable_hint(field, listing.source, canonical_display, found_raw),
        )

    if _matches(field, canonical_normalised, found_normalised):
        return None

    if field == "phone":
        # A German switchboard number and its extension are one line. Reporting a
        # business's own "-0" in red next to a genuinely wrong number destroys the
        # severity signal: once an error row is wrong, no error row gets read. So
        # it is still surfaced -- one consistent number matches better -- but as
        # info, which costs nothing against the score.
        extension = phone_extension_difference(canonical_normalised, found_normalised)
        if extension is not None:
            return NapFinding(
                field=field,
                canonical_value=canonical_display,
                found_value=found_raw,
                source=listing.source,
                severity="info",
                fix_hint=_extension_hint(
                    listing.source,
                    canonical_display,
                    found_raw,
                    extension,
                    on_listing=len(found_normalised) > len(canonical_normalised),
                ),
            )

    return NapFinding(
        field=field,
        canonical_value=canonical_display,
        found_value=found_raw,
        source=listing.source,
        severity=_MISMATCH_SEVERITY[field],
        fix_hint=_mismatch_hint(field, listing.source, canonical_display, found_raw),
    )


_ADDRESS_FIELDS: frozenset[NapField] = frozenset({"street", "house_number"})


def _annotation_note(canonical: CanonicalNap, listing: DirectoryListing) -> NapFinding | None:
    """Note a floor or entrance annotation the canonical record does not carry.

    Only reached when the address otherwise matches, so this is never the reason a
    listing looks wrong. It is ``info`` because it is not an inconsistency at all
    -- the annotation is delivery detail, and often the directory added it.
    """
    _, annotation = strip_address_annotation(listing.street)
    line = listing.street
    if annotation is None:
        _, annotation = strip_address_annotation(listing.house_number)
        line = listing.house_number
    if annotation is None:
        return None

    _, canonical_annotation = strip_address_annotation(canonical.street)
    if canonical_annotation is None:
        _, canonical_annotation = strip_address_annotation(canonical.house_number)
    if canonical_annotation is not None and fold_for_comparison(
        canonical_annotation
    ) == fold_for_comparison(annotation):
        return None

    canonical_line = " ".join(
        part for part in (canonical.street, canonical.house_number) if part is not None
    )
    return NapFinding(
        field="street",
        canonical_value=canonical_line or None,
        found_value=line,
        source=listing.source,
        severity="info",
        fix_hint=_annotation_hint(listing.source, canonical_line or None, line, annotation),
    )


def audit_nap(canonical: CanonicalNap, found: list[DirectoryListing]) -> NapAuditResult:
    """Audit every listing against the canonical record.

    Deterministic in every respect: listings are examined in the order given,
    fields in a fixed order, and nothing is read except the arguments. The same
    input twice returns the identical result.

    ``primary_category`` is deliberately never diffed. Directory taxonomies are
    not the same taxonomy -- "Restaurant" on one site is "Gastronomie" on the
    next -- so comparing them is precisely the naive string compare that produces
    a screen full of wrong findings. The canonical category is still carried, for
    the per-directory submission pack that maps it.
    """
    findings: list[NapFinding] = []

    for listing in found:
        listing_comparison = comparison_form(listing, country=canonical.country)
        # Directories publish "Löhrstraße 12a" in one field about as often as they
        # split it, so the number is lifted out. The value *reported* back is the
        # split part, so a house-number finding never quotes a whole street.
        address, _ = strip_address_annotation(listing.street)
        street_display, inline_number = split_street_and_number(address)
        house_number_display, _ = strip_address_annotation(listing.house_number)

        for_listing: list[NapFinding] = _name_findings(canonical, listing, listing_comparison)

        for field in _COMPARED_FIELDS:
            if field == "street":
                found_raw: str | None = street_display
            elif field == "house_number":
                found_raw = house_number_display or inline_number
            else:
                found_raw = getattr(listing, field)
            finding = _field_finding(
                canonical,
                listing,
                field,
                found_raw,
                getattr(listing_comparison, field),
            )
            if finding is not None:
                for_listing.append(finding)

        # Only worth a note when the address agrees apart from the annotation; if
        # the street or number genuinely differs, that finding is the whole story.
        if not any(finding.field in _ADDRESS_FIELDS for finding in for_listing):
            note = _annotation_note(canonical, listing)
            if note is not None:
                for_listing.append(note)

        findings.extend(for_listing)

    return NapAuditResult(
        consistency_score=consistency_score(findings),
        findings=findings,
        sources_checked=len(found),
    )
