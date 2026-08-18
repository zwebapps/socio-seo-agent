"""Tests for the NAP consistency audit engine.

Two halves, and the second one is the deliverable.

The first half proves the engine finds genuine inconsistencies with the right
field, the right severity, and a fix hint naming both values.

The second half -- :class:`TestNoFalsePositives` and the equivalence tables it
draws on -- proves it invents none. That asymmetry is the point. A missed
inconsistency costs the customer an opportunity; an invented one costs us the
customer, because a user who sees one wrong row stops believing every other row
on the screen. So every German equivalence the normaliser claims to handle gets
its own case here, asserted end-to-end through ``audit_nap`` rather than only
against the normaliser, because "the fold is equal" and "the audit stays silent"
are two different promises.
"""

import pytest

from backend.app.engines.nap import (
    CanonicalNap,
    DirectoryListing,
    NapAuditResult,
    RawNap,
    audit_nap,
    consistency_score,
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

# The benchmark business: a real-shaped German SMB. Every field populated, so a
# listing that omits nothing can be compared field for field.
BENCHMARK = RawNap(
    legal_name="Müller Bäckerei GmbH & Co. KG",
    trading_name="Bäckerei Müller",
    street="Löhrstraße",
    house_number="12a",
    postcode="56068",
    city="Koblenz",
    phone="+49 261 123456",
    email="info@baeckerei-mueller.de",
    opening_hours="Mo-Fr 08:00-18:00, Sa 09:00-14:00",
    primary_category="Bäckerei",
)


def canonical(**overrides: str | None) -> CanonicalNap:
    """The benchmark canonical record, with optional field overrides."""
    return normalise_nap(BENCHMARK.model_copy(update=overrides))


def listing(**overrides: str | None) -> DirectoryListing:
    """A listing that agrees with the benchmark on every field, before overrides.

    Complete by default on purpose: a partial listing would emit ``info``
    findings for its gaps, which would drown out whatever a case is actually
    testing.
    """
    fields: dict[str, str | None] = {
        "source": "gelbeseiten",
        "trading_name": "Bäckerei Müller",
        "street": "Löhrstraße",
        "house_number": "12a",
        "postcode": "56068",
        "city": "Koblenz",
        "phone": "+49 261 123456",
        "email": "info@baeckerei-mueller.de",
        "opening_hours": "Mo-Fr 08:00-18:00, Sa 09:00-14:00",
    }
    fields.update(overrides)
    source = fields.pop("source") or "gelbeseiten"
    return DirectoryListing(source=source, **fields)


def audit_one(**overrides: str | None) -> NapAuditResult:
    """Audit a single listing that differs from the benchmark only as overridden."""
    return audit_nap(canonical(), [listing(**overrides)])


# --------------------------------------------------------------------------- #
# Equivalence tables. Shared by the normaliser tests and the audit tests, so a
# rule can never be proven at one level and quietly broken at the other.
# --------------------------------------------------------------------------- #

# All the same number. This is the single most valuable equivalence in the engine:
# every German business writes its phone number differently on every form.
PHONE_EQUIVALENTS = [
    "+49 261 123456",
    "0261/123456",
    "0261 123-456",
    "(0261) 123456",
    "0049261123456",
    "+49 (0)261 123456",
    "0261 12 34 56",
    "  0261 123456  ",
    "+49-261-123456",
    "Tel. 0261 123456",
]

# Street spellings of the benchmark's own street.
STREET_EQUIVALENTS = [
    "Löhrstraße",
    "Löhrstrasse",
    "Loehrstrasse",
    "Loehrstraße",
    "Lohrstrasse",
    "Löhrstr.",
    "Löhrstr",
    "Loehrstr.",
    "Löhr Straße",
    "Löhr Str.",
    "LÖHRSTRASSE",
    "löhrstraße",
]

# Street *types* beyond Straße, each pair a (canonical, directory) spelling.
STREET_TYPE_PAIRS = [
    ("Löhrstraße", "Löhrstr."),
    ("Münzplatz", "Münzpl."),
    ("Münzplatz", "Muenzplatz"),
    ("Münz Platz", "Münzplatz"),
    ("Lindenallee", "Linden Allee"),
    ("Hochhausweg", "Hochhaus Weg"),
    ("Kirchgasse", "Kirch Gasse"),
    ("Karl-Marx-Straße", "Karl Marx Strasse"),
    ("Karl-Marx-Straße", "Karl-Marx-Str."),
    ("Am Alten Hof", "am alten hof"),
    ("Straße des 17. Juni", "Strasse des 17. Juni"),
    ("St.-Anna-Straße", "Sankt-Anna-Straße"),
    ("St. Anna Str.", "Sankt-Anna-Straße"),
    ("Konrad-Adenauer-Str.", "Konrad Adenauer Straße"),
    ("Kurfürstenallee", "Kurfürsten-Allee"),
    ("Alter Postweg", "Alter Post Weg"),
]

# House numbers. "12a" == "12 a" == "12A"; ranges tolerate any separator.
HOUSE_NUMBER_PAIRS = [
    ("12a", "12a"),
    ("12a", "12 a"),
    ("12a", "12A"),
    ("12a", "12 A"),
    ("12", "12"),
    ("12-14", "12-14"),
    ("12-14", "12 - 14"),
    ("12-14", "12/14"),
    ("12-14", "12 / 14"),
    ("12ab", "12 ab"),
    ("12a-14b", "12 a - 14 b"),
]

# Umlaut and transliteration variants of the trading name.
NAME_EQUIVALENTS = [
    "Bäckerei Müller",
    "Baeckerei Mueller",
    "Backerei Muller",
    "BÄCKEREI MÜLLER",
    "BAECKEREI MUELLER",
    "bäckerei müller",
    "Bäckerei  Müller",
    "Bäckerei Müller GmbH & Co. KG",
    "Bäckerei Müller GmbH",
    "Bäckerei Müller e.K.",
    "Bäckerei Müller AG",
    # The legal name in the directory's single name field is correct data.
    "Müller Bäckerei GmbH & Co. KG",
    "Müller Bäckerei",
]

# Legal forms, as (canonical name, directory name) pairs. The form is stripped for
# comparison in both directions -- a directory dropping it is not a misnaming.
LEGAL_FORM_PAIRS = [
    ("Müller Bäckerei GmbH", "Müller Bäckerei"),
    ("Müller Bäckerei gGmbH", "Müller Bäckerei"),
    ("Müller Bäckerei GmbH & Co. KG", "Müller Bäckerei"),
    ("Müller Bäckerei GmbH & Co. KG", "Müller Bäckerei GmbH"),
    ("Müller Bäckerei GmbH und Co. KG", "Müller Bäckerei GmbH & Co. KG"),
    ("Müller Bäckerei UG (haftungsbeschränkt)", "Müller Bäckerei"),
    ("Müller Bäckerei UG (haftungsbeschraenkt)", "Müller Bäckerei UG"),
    ("Müller Bäckerei e.K.", "Müller Bäckerei"),
    ("Müller Bäckerei e. K.", "Müller Bäckerei eK"),
    ("Müller Bäckerei e.V.", "Müller Bäckerei"),
    ("Müller Bäckerei e.V.", "Müller Bäckerei eV"),
    ("Müller Bäckerei AG", "Müller Bäckerei"),
    ("Müller Bäckerei OHG", "Müller Bäckerei"),
    ("Müller Bäckerei GbR", "Müller Bäckerei"),
    ("Müller Bäckerei KG", "Müller Bäckerei"),
]

# Postcodes: the country prefix is decoration.
POSTCODE_EQUIVALENTS = ["56068", "D-56068", "DE-56068", "D 56068", "DE 56068", " 56068 "]

# Cities. An appended district is not a different city; the connector in
# "Frankfurt am Main" is not information.
CITY_PAIRS = [
    ("Koblenz", "Koblenz"),
    ("Koblenz", "koblenz"),
    ("Koblenz", "KOBLENZ"),
    ("Koblenz", "Koblenz (Altstadt)"),
    ("Koblenz (Altstadt)", "Koblenz"),
    ("Frankfurt am Main", "Frankfurt/Main"),
    ("Frankfurt am Main", "Frankfurt a. Main"),
    ("Frankfurt am Main", "Frankfurt Main"),
    ("München", "Muenchen"),
    ("München", "Munchen"),
    ("Köln", "KÖLN"),
    ("Rothenburg ob der Tauber", "Rothenburg o.d. Tauber"),
    ("Frankfurt am Main", "Frankfurt a.M."),
    ("Neuburg an der Donau", "Neuburg a.d. Donau"),
    ("Berlin", "Berlin-Mitte"),
    ("Koblenz", "Koblenz/Rhein"),
    ("Bad Neuenahr-Ahrweiler", "Bad Neuenahr Ahrweiler"),
]

# Opening hours: the same week, written the way each directory writes it.
HOURS_EQUIVALENTS = [
    "Mo-Fr 08:00-18:00, Sa 09:00-14:00",
    "Mo-Fr 8-18, Sa 9-14",
    "Mo - Fr 08:00 - 18:00, Sa 09:00 - 14:00",
    "Montag bis Freitag 08:00-18:00, Samstag 09:00-14:00",
    "Mo-Fr 08:00-18:00 Uhr, Sa 09:00-14:00 Uhr",
    "Mo-Fr 08:00-18:00; Sa 09:00-14:00",
    "Mo-Fr 08.00-18.00, Sa 09.00-14.00",
    # An en dash where a hyphen belongs: typographic quotes and dashes are how a
    # directory's own CMS mangles the hours the business submitted.
    "Mo\u2013Fr 08:00\u201318:00, Sa 09:00\u201314:00",
    "Mo-Fr 08:00-18:00, Sa 09:00-14:00, So geschlossen",
    "MO-FR 08:00-18:00, SA 09:00-14:00",
    "Mo-Fr 08:00-18:00, Sa 09:00-14:00, So und Feiertage geschlossen",
    "Mo, Di, Mi, Do, Fr 08:00-18:00, Sa 09:00-14:00",
    "Mo-Fr 08:00-18:00 und Sa 09:00-14:00",
    "Mo-Fr 08:00-18:00 & Sa 09:00-14:00",
]

EMAIL_EQUIVALENTS = [
    "info@baeckerei-mueller.de",
    "Info@Baeckerei-Mueller.de",
    "INFO@BAECKEREI-MUELLER.DE",
    "mailto:info@baeckerei-mueller.de",
    " info@baeckerei-mueller.de ",
]


# --------------------------------------------------------------------------- #
# Half one: the normaliser folds equivalents together and keeps real differences apart
# --------------------------------------------------------------------------- #


class TestNormaliserEquivalence:
    """Unit level: equal inputs fold to equal comparison values."""

    @pytest.mark.parametrize("written", PHONE_EQUIVALENTS)
    def test_phone_variants_all_fold_to_one_e164(self, written: str) -> None:
        assert normalise_phone(written) == "+49261123456"

    @pytest.mark.parametrize("written", STREET_EQUIVALENTS)
    def test_street_variants_fold_together(self, written: str) -> None:
        assert normalise_street_name(written) == normalise_street_name("Löhrstraße")

    @pytest.mark.parametrize(("left", "right"), STREET_TYPE_PAIRS)
    def test_street_type_pairs_fold_together(self, left: str, right: str) -> None:
        assert normalise_street_name(left) == normalise_street_name(right)

    @pytest.mark.parametrize(("left", "right"), HOUSE_NUMBER_PAIRS)
    def test_house_number_pairs_fold_together(self, left: str, right: str) -> None:
        assert normalise_house_number(left) == normalise_house_number(right)

    @pytest.mark.parametrize("written", NAME_EQUIVALENTS)
    def test_name_variants_fold_to_a_canonical_name(self, written: str) -> None:
        record = canonical()
        accepted = {record.comparison.legal_name, record.comparison.trading_name}
        assert normalise_business_name(written) in accepted

    @pytest.mark.parametrize(("left", "right"), LEGAL_FORM_PAIRS)
    def test_legal_forms_are_stripped_for_comparison(self, left: str, right: str) -> None:
        assert normalise_business_name(left) == normalise_business_name(right)

    @pytest.mark.parametrize("written", POSTCODE_EQUIVALENTS)
    def test_postcode_country_prefixes_are_ignored(self, written: str) -> None:
        assert normalise_postcode(written) == "56068"

    @pytest.mark.parametrize(("left", "right"), CITY_PAIRS)
    def test_city_pairs_are_treated_as_one_place(self, left: str, right: str) -> None:
        left_tokens = set((normalise_city(left) or "").split())
        right_tokens = set((normalise_city(right) or "").split())
        assert left_tokens <= right_tokens or right_tokens <= left_tokens

    @pytest.mark.parametrize("written", HOURS_EQUIVALENTS)
    def test_opening_hours_variants_fold_together(self, written: str) -> None:
        expected = normalise_opening_hours("Mo-Fr 08:00-18:00, Sa 09:00-14:00")
        assert normalise_opening_hours(written) == expected

    @pytest.mark.parametrize("written", EMAIL_EQUIVALENTS)
    def test_email_variants_fold_together(self, written: str) -> None:
        assert normalise_email(written) == "info@baeckerei-mueller.de"

    @pytest.mark.parametrize(
        ("umlaut", "transliterated", "stripped"),
        [
            ("Müller", "Mueller", "Muller"),
            ("Bäckerei", "Baeckerei", "Backerei"),
            ("Öllampen", "Oellampen", "Ollampen"),
            ("MÜLLER", "MUELLER", "MULLER"),
        ],
    )
    def test_umlaut_transliteration_and_stripped_forms_agree(
        self, umlaut: str, transliterated: str, stripped: str
    ) -> None:
        folded = {normalise_business_name(v) for v in (umlaut, transliterated, stripped)}
        assert len(folded) == 1

    def test_sharp_s_folds_to_double_s(self) -> None:
        assert normalise_business_name("Weißbrot Straßen") == normalise_business_name(
            "Weissbrot Strassen"
        )


class TestNormaliserKeepsRealDifferences:
    """The other side of the coin: folding must not erase a genuine difference."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Löhrstraße", "Rizzastraße"),
            ("Löhrstraße", "Löhrgasse"),
            ("Münzplatz", "Münzweg"),
            ("Karl-Marx-Straße", "Karl-Marx-Allee"),
            ("Oststraße", "Poststraße"),
            ("St. Anna Straße", "St. Georg Straße"),
        ],
    )
    def test_different_streets_stay_different(self, left: str, right: str) -> None:
        assert normalise_street_name(left) != normalise_street_name(right)

    @pytest.mark.parametrize(("left", "right"), [("12a", "12b"), ("12", "14"), ("12-14", "12-16")])
    def test_different_house_numbers_stay_different(self, left: str, right: str) -> None:
        assert normalise_house_number(left) != normalise_house_number(right)

    def test_different_postcodes_stay_different(self) -> None:
        assert normalise_postcode("56068") != normalise_postcode("56070")

    def test_different_phone_numbers_stay_different(self) -> None:
        assert normalise_phone("0261 123456") != normalise_phone("0261 123457")

    def test_different_names_stay_different(self) -> None:
        assert normalise_business_name("Bäckerei Müller") != normalise_business_name(
            "Bäckerei Schmidt"
        )

    def test_reordered_name_is_not_silently_accepted_as_identical(self) -> None:
        # Word order is a real inconsistency, unlike a dropped legal form.
        assert normalise_business_name("Bäckerei Müller") != normalise_business_name(
            "Müller Bäckerei"
        )

    def test_different_cities_stay_different(self) -> None:
        assert normalise_city("Frankfurt am Main") != normalise_city("Frankfurt an der Oder")

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Mo-Fr 9-12 und 14-18", "Mo-Fr 09:00-12:00, 14:00-18:00"),
            ("Mo, Di und Mi 9-18", "Mo-Mi 09:00-18:00"),
            ("Mo-Fr 22-02", "Mo-Fr 22:00-02:00"),
        ],
    )
    def test_hours_written_in_either_shape_agree(self, left: str, right: str) -> None:
        assert normalise_opening_hours(left) == normalise_opening_hours(right)
        assert normalise_opening_hours(left) is not None

    def test_different_hours_stay_different(self) -> None:
        assert normalise_opening_hours("Mo-Fr 8-18") != normalise_opening_hours("Mo-Fr 9-18")


class TestNormaliserRefusals:
    """Unreadable values must return ``None`` rather than a confident wrong answer."""

    @pytest.mark.parametrize("written", ["", "   ", None])
    def test_blank_values_are_none(self, written: str | None) -> None:
        assert normalise_phone(written) is None
        assert normalise_postcode(written) is None
        assert normalise_street_name(written) is None
        assert normalise_business_name(written) is None
        assert normalise_opening_hours(written) is None

    @pytest.mark.parametrize("written", ["5606", "560688", "5606A", "abcde"])
    def test_a_german_postcode_must_be_exactly_five_digits(self, written: str) -> None:
        assert normalise_postcode(written) is None

    @pytest.mark.parametrize("written", ["bitte anrufen", "n/a", "-", "12"])
    def test_unusable_phone_values_are_refused(self, written: str) -> None:
        assert normalise_phone(written) is None

    @pytest.mark.parametrize(
        "written",
        ["Termine nach Vereinbarung", "rund um die Uhr erreichbar", "siehe Website"],
    )
    def test_free_text_hours_are_refused_rather_than_guessed(self, written: str) -> None:
        assert normalise_opening_hours(written) is None

    @pytest.mark.parametrize("written", ["info(at)example.de", "not an email", "info@example"])
    def test_malformed_emails_are_refused(self, written: str) -> None:
        assert normalise_email(written) is None

    def test_a_non_german_postcode_is_not_forced_into_five_digits(self) -> None:
        assert normalise_postcode("1010", country="AT") == "1010"


class TestStreetSplitting:
    """Most sources publish the house number inside the street field."""

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("Löhrstraße 12a", ("Löhrstraße", "12a")),
            ("Löhrstraße 12 a", ("Löhrstraße", "12 a")),
            ("Löhrstr. 12-14", ("Löhrstr.", "12-14")),
            ("Am Markt 3", ("Am Markt", "3")),
            ("Löhrstraße, 12a", ("Löhrstraße", "12a")),
            ("Löhrstraße", ("Löhrstraße", None)),
            ("Straße des 17. Juni", ("Straße des 17. Juni", None)),
        ],
    )
    def test_trailing_house_number_is_lifted_out(
        self, written: str, expected: tuple[str, str | None]
    ) -> None:
        assert split_street_and_number(written) == expected


class TestCanonicalRecord:
    """Both forms are kept: display for humans, folded only for the diff."""

    def test_display_form_keeps_umlauts_and_the_legal_form(self) -> None:
        record = canonical()
        assert record.legal_name == "Müller Bäckerei GmbH & Co. KG"
        assert record.trading_name == "Bäckerei Müller"
        assert record.street == "Löhrstraße"

    def test_comparison_form_is_folded(self) -> None:
        record = canonical()
        assert record.comparison.legal_name == "muller backerei"
        assert record.comparison.street == "lohrstrasse"
        assert record.comparison.phone == "+49261123456"

    def test_phone_display_is_pasteable_international_format(self) -> None:
        assert canonical(phone="0261/123456").phone == "+49 261 123456"

    def test_an_inline_house_number_is_split_into_its_own_display_field(self) -> None:
        record = normalise_nap(RawNap(street="Löhrstraße 12a", postcode="56068", city="Koblenz"))
        assert (record.street, record.house_number) == ("Löhrstraße", "12a")

    def test_an_invalid_postcode_is_echoed_not_corrected(self) -> None:
        record = canonical(postcode="5606")
        assert record.postcode == "5606"
        assert record.comparison.postcode is None

    def test_country_is_carried_on_the_record(self) -> None:
        assert canonical().country == "DE"
        assert normalise_nap(BENCHMARK, country="at").country == "AT"

    def test_normalisation_does_not_mutate_its_input(self) -> None:
        before = BENCHMARK.model_dump()
        normalise_nap(BENCHMARK)
        assert BENCHMARK.model_dump() == before


# --------------------------------------------------------------------------- #
# Half two: NO FALSE POSITIVES. The suite that makes the feature trustworthy.
# --------------------------------------------------------------------------- #


class TestNoFalsePositives:
    """Every equivalence above, asserted end-to-end: zero findings, score 100."""

    @staticmethod
    def assert_silent(result: NapAuditResult) -> None:
        assert result.findings == [], [f.fix_hint for f in result.findings]
        assert result.consistency_score == 100

    @pytest.mark.parametrize("written", PHONE_EQUIVALENTS)
    def test_phone_variants_produce_no_findings(self, written: str) -> None:
        self.assert_silent(audit_one(phone=written))

    @pytest.mark.parametrize("written", STREET_EQUIVALENTS)
    def test_street_variants_produce_no_findings(self, written: str) -> None:
        self.assert_silent(audit_one(street=written))

    @pytest.mark.parametrize(("canonical_street", "found_street"), STREET_TYPE_PAIRS)
    def test_street_type_variants_produce_no_findings(
        self, canonical_street: str, found_street: str
    ) -> None:
        result = audit_nap(canonical(street=canonical_street), [listing(street=found_street)])
        self.assert_silent(result)

    @pytest.mark.parametrize(("canonical_number", "found_number"), HOUSE_NUMBER_PAIRS)
    def test_house_number_variants_produce_no_findings(
        self, canonical_number: str, found_number: str
    ) -> None:
        result = audit_nap(
            canonical(house_number=canonical_number), [listing(house_number=found_number)]
        )
        self.assert_silent(result)

    @pytest.mark.parametrize("written", NAME_EQUIVALENTS)
    def test_name_variants_produce_no_findings(self, written: str) -> None:
        self.assert_silent(audit_one(trading_name=written))

    @pytest.mark.parametrize("written", NAME_EQUIVALENTS)
    def test_a_name_in_the_legal_name_field_produces_no_findings(self, written: str) -> None:
        # Directories have one name field and no rule about which name belongs in it.
        self.assert_silent(audit_one(trading_name=None, legal_name=written))

    @pytest.mark.parametrize(("canonical_name", "found_name"), LEGAL_FORM_PAIRS)
    def test_legal_form_variants_produce_no_findings(
        self, canonical_name: str, found_name: str
    ) -> None:
        result = audit_nap(
            canonical(legal_name=canonical_name, trading_name=None),
            [listing(trading_name=None, legal_name=found_name)],
        )
        self.assert_silent(result)

    @pytest.mark.parametrize("written", POSTCODE_EQUIVALENTS)
    def test_postcode_variants_produce_no_findings(self, written: str) -> None:
        self.assert_silent(audit_one(postcode=written))

    @pytest.mark.parametrize(("canonical_city", "found_city"), CITY_PAIRS)
    def test_city_variants_produce_no_findings(self, canonical_city: str, found_city: str) -> None:
        self.assert_silent(audit_nap(canonical(city=canonical_city), [listing(city=found_city)]))

    @pytest.mark.parametrize("written", HOURS_EQUIVALENTS)
    def test_opening_hours_variants_produce_no_findings(self, written: str) -> None:
        self.assert_silent(audit_one(opening_hours=written))

    @pytest.mark.parametrize("written", EMAIL_EQUIVALENTS)
    def test_email_variants_produce_no_findings(self, written: str) -> None:
        self.assert_silent(audit_one(email=written))

    def test_a_house_number_inline_in_the_street_field_produces_no_findings(self) -> None:
        self.assert_silent(audit_one(street="Löhrstraße 12a", house_number=None))

    def test_an_identical_listing_produces_no_findings(self) -> None:
        self.assert_silent(audit_one())

    def test_every_field_varying_at_once_still_produces_no_findings(self) -> None:
        # The realistic case: one directory that typed everything its own way.
        self.assert_silent(
            audit_one(
                trading_name="BAECKEREI MUELLER GmbH",
                street="Loehrstr. 12 a",
                house_number=None,
                postcode="D-56068",
                city="Koblenz (Altstadt)",
                phone="0261/123-456",
                email="Info@Baeckerei-Mueller.DE",
                opening_hours="Montag bis Freitag 8-18 Uhr, Samstag 9-14 Uhr",
            )
        )

    def test_a_category_worded_differently_is_never_a_finding(self) -> None:
        # Directory taxonomies are not the same taxonomy: "Bäckerei" on one site is
        # "Lebensmittel / Backwaren" on the next. Comparing them is the classic
        # naive-compare mistake, so the field is carried but never diffed.
        self.assert_silent(audit_one(primary_category="Lebensmittel / Backwaren"))

    def test_a_field_we_hold_no_canonical_value_for_is_never_a_finding(self) -> None:
        record = canonical(email=None, opening_hours=None)
        result = audit_nap(record, [listing(email="anything@example.de")])
        self.assert_silent(result)

    def test_free_text_opening_hours_on_a_listing_are_not_a_finding(self) -> None:
        # "By appointment" is a legitimate answer, not an inconsistency.
        self.assert_silent(audit_one(opening_hours="Termine nach Vereinbarung"))

    def test_an_empty_listing_list_is_a_clean_audit(self) -> None:
        result = audit_nap(canonical(), [])
        self.assert_silent(result)
        assert result.sources_checked == 0


# --------------------------------------------------------------------------- #
# True positives
# --------------------------------------------------------------------------- #


class TestTruePositives:
    """A genuine difference is found, on the right field, at the right severity."""

    @pytest.mark.parametrize(
        ("overrides", "expected_field", "expected_severity", "found_value"),
        [
            ({"phone": "0261 999999"}, "phone", "error", "0261 999999"),
            ({"phone": "+49 30 123456"}, "phone", "error", "+49 30 123456"),
            ({"postcode": "56070"}, "postcode", "error", "56070"),
            ({"postcode": "D-56070"}, "postcode", "error", "D-56070"),
            ({"street": "Rizzastraße"}, "street", "warn", "Rizzastraße"),
            ({"street": "Löhrgasse"}, "street", "warn", "Löhrgasse"),
            ({"house_number": "14"}, "house_number", "warn", "14"),
            ({"house_number": "12b"}, "house_number", "warn", "12b"),
            ({"trading_name": "Bäckerei Schmidt"}, "trading_name", "warn", "Bäckerei Schmidt"),
            ({"city": "Bonn"}, "city", "warn", "Bonn"),
            ({"email": "kontakt@example.de"}, "email", "warn", "kontakt@example.de"),
            ({"opening_hours": "Mo-Fr 09:00-17:00"}, "opening_hours", "info", "Mo-Fr 09:00-17:00"),
        ],
    )
    def test_a_real_difference_is_reported(
        self,
        overrides: dict[str, str | None],
        expected_field: str,
        expected_severity: str,
        found_value: str,
    ) -> None:
        result = audit_one(**overrides)
        assert len(result.findings) == 1, [f.fix_hint for f in result.findings]
        finding = result.findings[0]
        assert finding.field == expected_field
        assert finding.severity == expected_severity
        assert finding.source == "gelbeseiten"
        assert finding.found_value == found_value
        # The hint must name both values, or it is not actionable.
        assert found_value in finding.fix_hint
        assert finding.canonical_value is not None
        assert finding.canonical_value in finding.fix_hint

    def test_an_inline_house_number_difference_reports_only_the_number(self) -> None:
        result = audit_one(street="Löhrstraße 14", house_number=None)
        assert [f.field for f in result.findings] == ["house_number"]
        assert result.findings[0].found_value == "14"

    def test_a_reordered_name_is_reported(self) -> None:
        result = audit_nap(
            canonical(legal_name=None, trading_name="Bäckerei Müller"),
            [listing(trading_name="Müller Bäckerei")],
        )
        assert [f.field for f in result.findings] == ["trading_name"]

    @pytest.mark.parametrize(
        ("overrides", "expected_field", "expected_severity"),
        [
            # Provably wrong: a German postcode has five digits.
            ({"postcode": "5606"}, "postcode", "error"),
            # Present but unreadable: we cannot prove a mismatch, so we do not claim one.
            ({"phone": "bitte anrufen"}, "phone", "warn"),
            ({"email": "info(at)example.de"}, "email", "warn"),
        ],
    )
    def test_an_unreadable_value_is_flagged_without_claiming_a_mismatch(
        self, overrides: dict[str, str | None], expected_field: str, expected_severity: str
    ) -> None:
        result = audit_one(**overrides)
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert (finding.field, finding.severity) == (expected_field, expected_severity)
        assert "could not be read" in finding.fix_hint

    @pytest.mark.parametrize("field", ["street", "house_number", "postcode", "city", "phone"])
    def test_a_missing_core_field_is_info_not_an_error(self, field: str) -> None:
        result = audit_one(**{field: None})
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.severity == "info"
        assert finding.found_value is None
        assert finding.canonical_value is not None
        assert finding.canonical_value in finding.fix_hint
        # An info finding never costs the business points -- we cannot know whether
        # the directory even offers the field.
        assert result.consistency_score == 100

    def test_a_listing_with_every_field_missing_yields_only_info_findings(self) -> None:
        result = audit_nap(canonical(), [DirectoryListing(source="cylex")])
        assert result.sources_checked == 1
        assert result.findings != []
        assert {f.severity for f in result.findings} == {"info"}
        assert {f.found_value for f in result.findings} == {None}
        assert result.consistency_score == 100

    def test_findings_name_their_source(self) -> None:
        result = audit_nap(
            canonical(),
            [
                listing(source="gelbeseiten", phone="0261 999999"),
                listing(source="das_oertliche", postcode="56070"),
            ],
        )
        assert [(f.source, f.field) for f in result.findings] == [
            ("gelbeseiten", "phone"),
            ("das_oertliche", "postcode"),
        ]

    def test_the_same_wrong_value_on_several_directories_is_reported_each_time(self) -> None:
        # "Your phone number differs across four listings" is the sentence the
        # product exists to be able to say.
        listings = [
            listing(source=source, phone="0261 999999")
            for source in ("gelbeseiten", "das_oertliche", "11880", "cylex")
        ]
        result = audit_nap(canonical(), listings)
        assert len(result.findings) == 4
        assert result.sources_checked == 4
        assert {f.field for f in result.findings} == {"phone"}


# --------------------------------------------------------------------------- #
# Scoring and determinism
# --------------------------------------------------------------------------- #


class TestScoring:
    def test_a_perfect_audit_scores_100(self) -> None:
        assert audit_one().consistency_score == 100

    def test_any_mismatch_scores_strictly_lower(self) -> None:
        assert audit_one(phone="0261 999999").consistency_score < 100
        assert audit_one(street="Rizzastraße").consistency_score < 100

    def test_the_ledger_is_15_per_error_and_5_per_warn(self) -> None:
        assert audit_one(phone="0261 999999").consistency_score == 85
        assert audit_one(street="Rizzastraße").consistency_score == 95
        assert audit_one(phone="0261 999999", postcode="56070").consistency_score == 70

    def test_more_wrong_means_a_lower_score(self) -> None:
        one = audit_one(phone="0261 999999").consistency_score
        two = audit_one(phone="0261 999999", postcode="56070").consistency_score
        three = audit_one(
            phone="0261 999999", postcode="56070", street="Rizzastraße"
        ).consistency_score
        assert one > two > three

    def test_the_score_never_goes_below_zero(self) -> None:
        listings = [
            listing(source=f"directory_{index}", phone="0261 999999", postcode="56070")
            for index in range(10)
        ]
        assert audit_nap(canonical(), listings).consistency_score == 0

    def test_the_score_is_computable_from_the_findings_alone(self) -> None:
        result = audit_one(phone="0261 999999", street="Rizzastraße")
        assert consistency_score(result.findings) == result.consistency_score

    def test_an_empty_finding_list_scores_100(self) -> None:
        assert consistency_score([]) == 100


class TestDeterminism:
    def test_the_same_input_twice_gives_an_identical_result(self) -> None:
        listings = [
            listing(source="gelbeseiten", phone="0261 999999"),
            listing(source="cylex", postcode="56070", city=None),
            DirectoryListing(source="yelp_de"),
        ]
        first = audit_nap(canonical(), listings)
        second = audit_nap(canonical(), listings)
        assert first.model_dump() == second.model_dump()

    def test_findings_follow_the_order_the_listings_were_given(self) -> None:
        forward = audit_nap(
            canonical(),
            [listing(source="a", phone="0261 999999"), listing(source="b", postcode="56070")],
        )
        reverse = audit_nap(
            canonical(),
            [listing(source="b", postcode="56070"), listing(source="a", phone="0261 999999")],
        )
        assert [f.source for f in forward.findings] == ["a", "b"]
        assert [f.source for f in reverse.findings] == ["b", "a"]
        assert forward.consistency_score == reverse.consistency_score

    def test_auditing_does_not_mutate_its_inputs(self) -> None:
        record = canonical()
        listings = [listing(phone="0261 999999")]
        before = (record.model_dump(), [item.model_dump() for item in listings])
        audit_nap(record, listings)
        assert (record.model_dump(), [item.model_dump() for item in listings]) == before
