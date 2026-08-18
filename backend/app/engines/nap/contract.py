"""Typed contract for the NAP (Name / Address / Phone) consistency audit engine.

Local search ranking and entity resolution both depend on a business's name,
address and phone being *identical* across every online directory. Divergence
suppresses local ranking and misroutes customers, and it is extremely common
because every listing was typed by a different person on a different day.

Two forms of every value are kept, deliberately:

* the **display** form -- what a human should paste into a directory. Umlauts,
  the legal form (`GmbH`), casing and punctuation are all preserved, because
  that is the value the business actually wants published.
* the **comparison** form (:class:`NapComparison`) -- an aggressively folded
  value used only by the diff. It is lossy by design and must never be shown
  to a user or submitted anywhere.

Keeping them apart is the whole reason this engine can be both trustworthy and
useful: the diff may be brutal, the advice must stay correct German.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: Severity of a single finding.
#:
#: ``error`` -- breaks entity resolution or misroutes a customer (phone, postcode).
#: ``warn``  -- a real inconsistency a human should fix (name, street, city, email).
#: ``info``  -- we cannot know whether this is a problem (a field the directory
#:              simply may not offer). Never counted against the score.
Severity = Literal["error", "warn", "info"]

#: Every field the audit can report on. Kept as a ``Literal`` so a typo in a
#: finding is a type error rather than a silently unrenderable row in the UI.
NapField = Literal[
    "legal_name",
    "trading_name",
    "street",
    "house_number",
    "postcode",
    "city",
    "phone",
    "email",
    "opening_hours",
    "primary_category",
]


class NapFieldSet(BaseModel):
    """The ten NAP fields, all optional. Base for both input shapes.

    Immutable and whitespace-stripped on construction: an engine that mutates
    its own input is not deterministic, and a trailing space is never a finding.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    legal_name: str | None = None
    trading_name: str | None = None
    street: str | None = None
    house_number: str | None = None
    postcode: str | None = None
    city: str | None = None
    phone: str | None = None
    email: str | None = None
    opening_hours: str | None = None
    primary_category: str | None = None


class RawNap(NapFieldSet):
    """The business's own NAP data, as harvested from its site and documents.

    ``street`` may carry the house number inline (``"Löhrstraße 12a"``), which is
    how most sources publish it; the normaliser splits it out.
    """


class DirectoryListing(NapFieldSet):
    """One listing found on one directory.

    ``source`` is the directory key -- ``"gelbeseiten"``, ``"das_oertliche"``,
    ``"google_business"``, ``"11880"``, ``"cylex"``, ``"yelp_de"`` -- and is
    echoed verbatim into every finding so the fix list is actionable.

    Every NAP field is optional because directories genuinely differ in what
    they store. A missing field is reported as ``info``, never as an error.
    """

    source: str


class NapComparison(NapFieldSet):
    """Normalised, comparison-only forms. Lossy: never display these.

    A ``None`` field means either "absent" or "present but not parseable as this
    kind of value" -- the two are distinguished by the audit, which still holds
    the raw input.
    """


class CanonicalNap(BaseModel):
    """The one true NAP record: display values plus their comparison forms.

    The top-level fields are the **display** form (paste these into a
    directory). :attr:`comparison` holds the folded forms the diff uses.
    """

    model_config = ConfigDict(frozen=True)

    legal_name: str | None = None
    trading_name: str | None = None
    street: str | None = None
    house_number: str | None = None
    postcode: str | None = None
    city: str | None = None
    phone: str | None = None
    email: str | None = None
    opening_hours: str | None = None
    primary_category: str | None = None

    #: ISO 3166-1 alpha-2 region used for phone and postcode rules. Carried on
    #: the record so ``audit_nap`` needs no second country argument and can
    #: never disagree with the normaliser about which country's rules apply.
    country: str = "DE"

    comparison: NapComparison = NapComparison()


class NapFinding(BaseModel):
    """One inconsistency, on one field, on one directory."""

    model_config = ConfigDict(frozen=True)

    field: NapField
    canonical_value: str | None
    found_value: str | None
    source: str
    severity: Severity
    #: Instruction naming both values, so the fix needs no further thought and
    #: an agent explaining the finding has nothing left to invent.
    fix_hint: str


class NapAuditResult(BaseModel):
    """Outcome of diffing every listing against the canonical record."""

    model_config = ConfigDict(frozen=True)

    #: 0-100, deterministic. See ``audit.consistency_score`` for the ledger.
    consistency_score: int
    findings: list[NapFinding]
    #: Number of listings audited (not distinct directories -- two listings on
    #: one directory is itself a duplicate-listing problem worth surfacing).
    sources_checked: int
