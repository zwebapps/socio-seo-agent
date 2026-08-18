"""NAP normalisation. German conventions are first-class here, not an afterthought.

This module is the product. Every finding the audit emits rests on the claim
"these two values are genuinely different", and one false claim -- a `Str.`
reported against `Straße`, a `Mueller` against `Müller` -- teaches the user that
the whole screen is guesswork. So the bias throughout is deliberate and
one-directional:

    a missed inconsistency (false negative) is a lost opportunity;
    an invented inconsistency (false positive) is a lost customer.

Where a rule could go either way it goes the lenient way, and the reason is
written next to it.

The folding is lossy on purpose and exists only for comparison. The display
forms produced by :func:`normalise_nap` keep the umlauts, the legal form and the
business's own wording, because those are what actually gets published.
"""

import re
import unicodedata

import phonenumbers

from .contract import CanonicalNap, NapComparison, NapFieldSet, RawNap

__all__ = [
    "comparison_form",
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
    "phone_extension_difference",
    "split_street_and_number",
    "strip_address_annotation",
]


# --------------------------------------------------------------------------- #
# Character folding
# --------------------------------------------------------------------------- #

# Every dash-like and space-like character a directory might emit, mapped to
# ASCII before any range parsing happens. Written as escapes so the source stays
# unambiguous to a reader and to the linter.
_ASCII_PUNCT = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        "\u00a0": " ",  # no-break space
        "\u2007": " ",  # figure space
        "\u2009": " ",  # thin space
        "\u202f": " ",  # narrow no-break space
    }
)

# The digraph collapse. "ue" -> "u" (rather than "ü" -> "ue") is the only rule
# that makes Müller / Mueller / Muller / MÜLLER one value, which this market
# requires: German businesses type all four, and any directory form that rejects
# umlauts forces the transliteration.
#
# It over-collapses innocent sequences ("Aktuell" -> "aktull") but does so
# symmetrically -- both sides of every comparison fold identically -- so it can
# only ever hide a difference, never invent one. That is the safe direction.
_DIGRAPHS = (("ae", "a"), ("oe", "o"), ("ue", "u"))


def _clean(value: str | None) -> str | None:
    """Collapse whitespace and unify punctuation, or return ``None`` if nothing is left."""
    if value is None:
        return None
    collapsed = " ".join(value.translate(_ASCII_PUNCT).split())
    return collapsed or None


def fold_for_comparison(value: str) -> str:
    """Fold a string into the comparison alphabet.

    Lowercases, expands ``ß`` to ``ss``, strips diacritics, then collapses the
    German transliteration digraphs. Punctuation and whitespace survive, because
    the callers that need them -- legal forms, street types, opening hours --
    still have to see them.
    """
    folded = value.translate(_ASCII_PUNCT).lower().replace("ß", "ss")
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    for digraph, single in _DIGRAPHS:
        stripped = stripped.replace(digraph, single)
    return stripped


def _tokens(value: str) -> list[str]:
    """Split on anything that is not a letter or digit. Punctuation is never significant."""
    return [token for token in re.split(r"[^a-z0-9]+", value) if token]


# --------------------------------------------------------------------------- #
# Business name
# --------------------------------------------------------------------------- #

# Legal forms are stripped for comparison and preserved in the display value. A
# directory that omits "GmbH" is not misnaming the business, so flagging it would
# be a textbook false positive -- and the Rechtsform is exactly the part a
# listing form most often has no room for.
#
# Applied to the folded string *before* punctuation is collapsed, so "e.K." is
# still recognisable as one abbreviation rather than the letters "e" and "k".
_LEGAL_FORMS = tuple(
    re.compile(pattern)
    for pattern in (
        # Compound forms first: "GmbH & Co. KG" must not be eaten as "GmbH" + "KG".
        r"\bug\s*\(?\s*haftungsbeschrankt\s*\)?",
        r"\bg?gmbh\s*(?:und\s*)?co\.?\s*kgaa\b",
        r"\bg?gmbh\s*(?:und\s*)?co\.?\s*kg\b",
        r"\bund\s*co\.?\s*kg\b",
        r"\bg?gmbh\b",
        r"\bmbh\b",
        r"\be\s*\.?\s*kfm?\.?(?=\s|$)",  # e.K. / e.Kfm.
        r"\be\s*\.?\s*k\.?(?=\s|$)",
        r"\be\s*\.?\s*v\.?(?=\s|$)",
        r"\be\s*\.?\s*g\.?(?=\s|$)",
        r"\bkgaa\b",
        r"\bag\b",
        r"\bohg\b",
        r"\bgbr\b",
        r"\bkg\b",
        r"\bug\b",
        r"\bse\b",
        r"\bltd\.?\b",
        r"\binc\.?\b",
        r"\bund\s*co\.?(?=\s|$)",
    )
)


def normalise_business_name(value: str | None) -> str | None:
    """Comparison form of a business name: folded, legal form removed.

    Word order is deliberately *not* normalised. "Bäckerei Müller" and "Müller
    Bäckerei" stay different, because a reordered name genuinely is an
    inconsistency worth fixing -- unlike a dropped "GmbH", which is a form-field
    limitation.
    """
    text = _clean(value)
    if text is None:
        return None
    folded = fold_for_comparison(text).replace("&", " und ")
    for pattern in _LEGAL_FORMS:
        folded = pattern.sub(" ", folded)
    return " ".join(_tokens(folded)) or None


# --------------------------------------------------------------------------- #
# Street and house number
# --------------------------------------------------------------------------- #

# A trailing house number inside the street field. Most sources publish
# "Löhrstraße 12a" in one field while a directory splits it in two, so this
# splitter runs on both sides of every comparison.
_TRAILING_HOUSE_NUMBER = re.compile(
    r"""
    \s*,?\s*                    # an optional comma before the number
    (
        \d+                     # 12
        (?:\s*[-/]\s*\d+)?      # -14  /  / 14
        \s*[a-zA-Z]?            # a  or  " a"
    )
    \s*$
    """,
    re.VERBOSE,
)

# Street-type words. Only the abbreviations that actually occur in German
# directory data need rules; the unabbreviated types (Allee, Weg, Gasse, Ring,
# Ufer, Damm) only need attached-vs-detached handled, which the final space
# removal does for free.
# The lookahead is "not followed by another letter or digit" rather than
# "space or end", so a type abbreviation works mid-name too: "St.-Anna-Straße",
# "Karl-Marx-Str.-Ecke".
_STREET_TYPES = (
    # standalone ("Löhr Str.")                 attached ("Löhrstr.")                     canonical
    (re.compile(r"\bstr\.?(?![a-z0-9])"), re.compile(r"(?<=[a-z])str\.?(?![a-z0-9])"), "strasse"),
    (re.compile(r"\bpl\.?(?![a-z0-9])"), re.compile(r"(?<=[a-z])pl\.?(?![a-z0-9])"), "platz"),
    # "Sankt" has no attached form: an attached rule would rewrite the "st" in
    # Oststraße, Poststraße and Propsteigasse into nonsense.
    (re.compile(r"\bst\.?(?![a-z0-9])"), None, "sankt"),
)


# Floor, entrance and addressee annotations. "Hauptstraße 12a, 3. OG" is the same
# address as "Hauptstraße 12a" -- the floor says where in the building to go, and
# a directory appends it freely. Stripped from the comparison form only; the
# display value keeps it, because the owner may need it on a parcel.
#
# Two safety rules, because a greedy stripper here would silently delete real
# addresses: a separator before the annotation is mandatory (so the "haus" in
# "Hausvogteiplatz" and the "weg" in "Hochhausweg" cannot match), and a strip that
# would leave nothing behind is refused.
_ADDRESS_ANNOTATION = re.compile(
    r"""
    [\s,;]+                                       # mandatory separator
    (?P<annotation>
        (?:
            \d{1,2}\s*\.?\s*(?:og|obergeschoss|etage|stock)\b
          | (?:og|eg|ug|dg)\b
          | (?:erd|ober|unter|dach)geschoss\b
          | (?:og|etage|stock)\s*\.?\s*\d{1,2}\b
          | (?:hinter|vorder|r(?:ü|ue)ck|seiten)(?:haus|geb(?:ä|ae)ude|fl(?:ü|ue)gel)\b
          | (?:eingang|aufgang|haus|geb(?:ä|ae)ude|whg|wohnung|app|apartment|zimmer)
            \s*\.?\s*[0-9a-z]{1,4}\b
          | c\s*/\s*o\b.*
          | z\.?\s*hd\.?\b.*
        )
        \s*\.?
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_MAX_ANNOTATIONS = 3
_HAS_LETTER = re.compile(r"[^\W\d_]")
_BARE_HOUSE_NUMBER = re.compile(r"\d+\s*[a-z]?", re.IGNORECASE)


def _keeps_a_real_address(remainder: str) -> bool:
    """Would stripping still leave something that looks like an address?

    A remainder with a letter is a street. A bare "12" or "12a" is a house number,
    which is the case where the annotation sat in the house-number field. A
    remainder of "3." is neither: that digit is the ordinal of "3. OG" and the
    value was nothing but an annotation to begin with.
    """
    if _HAS_LETTER.search(remainder):
        return True
    return _BARE_HOUSE_NUMBER.fullmatch(remainder) is not None


def strip_address_annotation(value: str | None) -> tuple[str | None, str | None]:
    """Split ``"Hauptstraße 12a, 3. OG"`` into ``("Hauptstraße 12a", "3. OG")``.

    Handles a stack of them ("Hauptstraße 12, Hinterhaus, 2. OG") and returns the
    annotations in the order they were written. Returns the input unchanged with
    ``None`` when there is nothing to strip -- including when the value is
    *entirely* an annotation, since throwing away the only address line we have
    would be worse than comparing a strange one.
    """
    text = _clean(value)
    if text is None:
        return None, None

    annotations: list[str] = []
    for _ in range(_MAX_ANNOTATIONS):
        match = _ADDRESS_ANNOTATION.search(text)
        if match is None:
            break
        remainder = text[: match.start()].strip(" ,;")
        if not _keeps_a_real_address(remainder):
            break
        annotations.append(match.group("annotation").strip(" ,;"))
        text = remainder

    if not annotations:
        return text, None
    return text, ", ".join(reversed(annotations))


def split_street_and_number(street: str | None) -> tuple[str | None, str | None]:
    """Split ``"Löhrstraße 12a"`` into ``("Löhrstraße", "12a")``.

    Returns the street unchanged with ``None`` for the number when there is no
    trailing number -- which is also the right answer for "Straße des 17. Juni".
    """
    text = _clean(street)
    if text is None:
        return None, None
    match = _TRAILING_HOUSE_NUMBER.search(text)
    if match is None:
        return text, None
    name = text[: match.start()].strip(" ,")
    if not name:
        # The field held nothing but a number. Keep it as the street rather than
        # discarding the only address line we were given.
        return text, None
    return name, match.group(1).strip()


def normalise_street_name(value: str | None) -> str | None:
    """Comparison form of a street *name*, house number excluded.

    ``Straße`` / ``Strasse`` / ``Str.`` / ``str`` / ``-straße`` all converge, as do
    ``Platz`` / ``Pl.`` and ``St.`` / ``Sankt``. ``Allee``, ``Weg`` and ``Gasse``
    converge through the final space-and-hyphen removal, which is also what makes
    ``Karl-Marx-Straße`` equal ``Karl Marx Strasse``.
    """
    text, _ = strip_address_annotation(value)
    if text is None:
        return None
    folded = fold_for_comparison(text)
    for standalone, attached, canonical in _STREET_TYPES:
        folded = standalone.sub(canonical, folded)
        if attached is not None:
            folded = attached.sub(canonical, folded)
    return "".join(_tokens(folded)) or None


def normalise_house_number(value: str | None) -> str | None:
    """Comparison form of a house number.

    ``"12a"`` == ``"12 a"`` == ``"12A"``; ``"12-14"`` == ``"12 - 14"`` == ``"12/14"``.
    """
    text, _ = strip_address_annotation(value)
    if text is None:
        return None
    parts = re.findall(r"(\d+)\s*([a-z]*)", fold_for_comparison(text))
    if not parts:
        return None
    return "-".join(f"{digits}{suffix}" for digits, suffix in parts)


# --------------------------------------------------------------------------- #
# Postcode
# --------------------------------------------------------------------------- #

_POSTCODE_COUNTRY_PREFIX = re.compile(r"^(?:de|d)(?=\d)")
_GERMAN_POSTCODE = re.compile(r"\d{5}")


def normalise_postcode(value: str | None, *, country: str = "DE") -> str | None:
    """Comparison form of a postcode. ``56068`` == ``D-56068`` == ``DE-56068``.

    Returns ``None`` for a German postcode that is not exactly five digits. That
    is not an unknown -- it is provably wrong, and the audit says so rather than
    guessing what was meant.
    """
    text = _clean(value)
    if text is None:
        return None
    cleaned = _POSTCODE_COUNTRY_PREFIX.sub("", re.sub(r"[^0-9a-z]", "", text.lower()))
    if country.upper() == "DE":
        return cleaned if _GERMAN_POSTCODE.fullmatch(cleaned) else None
    return cleaned.upper() or None


# --------------------------------------------------------------------------- #
# City
# --------------------------------------------------------------------------- #

# Connectors carrying no distinguishing information: "Frankfurt am Main" and
# "Frankfurt/Main" must not read as two different cities.
_CITY_FILLERS = frozenset({"am", "an", "der", "den", "im", "ob", "bei", "vor", "auf"})


def normalise_city(value: str | None) -> str | None:
    """Comparison form of a city: folded, connectors dropped, token order kept.

    Parentheses become ordinary tokens (``"Köln (Innenstadt)"`` -> ``"koln
    innenstadt"``). The audit compares cities by token subset rather than
    equality, so an appended district does not read as a different city while
    ``Frankfurt Main`` vs ``Frankfurt Oder`` still does.
    """
    text = _clean(value)
    if text is None:
        return None
    tokens = [
        token
        for token in _tokens(fold_for_comparison(text))
        # Single letters are how German abbreviates the same connectors:
        # "Frankfurt a.M.", "Neuburg a.d. Donau", "Rothenburg o.d.T.".
        if len(token) > 1 and token not in _CITY_FILLERS
    ]
    return " ".join(tokens) or None


# --------------------------------------------------------------------------- #
# Phone
# --------------------------------------------------------------------------- #


def normalise_phone(value: str | None, *, country: str = "DE") -> str | None:
    """Comparison form of a phone number: E.164, via Google's ``phonenumbers``.

    ``+49 261 123456`` == ``0261/123456`` == ``0261 123-456`` == ``(0261) 123456``
    == ``0049261123456``.

    Returns ``None`` when the value is not a *valid* number for ``country``.
    Validity, not mere possibility, is the gate: ``phonenumbers`` considers "12" a
    possible German number, and treating that as comparable would let the audit
    silently compare junk. A number that fails validation is reported as "could
    not be verified" rather than as a mismatch we cannot prove -- and a wrong
    digit count is itself worth telling the business about.
    """
    text = _clean(value)
    if text is None:
        return None
    try:
        parsed = phonenumbers.parse(text, country.upper())
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _display_phone(value: str | None, *, country: str) -> str | None:
    """Pasteable international form (``+49 261 123456``), falling back to the input."""
    text = _clean(value)
    if text is None:
        return None
    try:
        parsed = phonenumbers.parse(text, country.upper())
    except phonenumbers.NumberParseException:
        return text
    if not phonenumbers.is_valid_number(parsed):
        return text
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)


# A German switchboard publishes a Durchwahl: the same line plus an extension,
# "-0" being the switchboard itself. Two numbers where one is the other plus a
# short numeric tail are one line, not two subscribers.
#
# The bounds matter. Up to five extension digits covers every Durchwahl scheme in
# use; requiring six shared digits means a genuinely short number can never be
# read as somebody else's base. Both sides have already passed
# ``is_valid_number``, so the shared part is a real number rather than a fragment.
_MAX_EXTENSION_DIGITS = 5
_MIN_SHARED_DIGITS = 6


def phone_extension_difference(left: str, right: str) -> str | None:
    """Return the extension digits when two E.164 numbers are one line, else ``None``.

    ``+49261123456`` vs ``+492611234560`` -> ``"0"``: the same line, reached
    through the switchboard. ``+4926112345612`` vs ``+4926199999912`` -> ``None``:
    the base numbers differ, so these are two different subscribers no matter what
    they share at the end.

    Direction-agnostic -- it does not matter which side carries the extension.
    """
    try:
        left_number = phonenumbers.parse(left, None)
        right_number = phonenumbers.parse(right, None)
    except phonenumbers.NumberParseException:
        return None
    if left_number.country_code != right_number.country_code:
        return None

    left_digits = phonenumbers.national_significant_number(left_number)
    right_digits = phonenumbers.national_significant_number(right_number)
    if left_digits == right_digits:
        return None

    shorter, longer = sorted((left_digits, right_digits), key=len)
    # Equal lengths cannot be a prefix pair, so this also rejects "one digit
    # different" -- which is the typo we most want to keep reporting as an error.
    if not longer.startswith(shorter) or len(shorter) < _MIN_SHARED_DIGITS:
        return None
    extension = longer[len(shorter) :]
    if len(extension) > _MAX_EXTENSION_DIGITS:
        return None
    return extension


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}")


def normalise_email(value: str | None) -> str | None:
    """Comparison form of an email address: lowercased, ``mailto:`` stripped.

    Returns ``None`` for anything not shaped like an address. The local part is
    technically case-sensitive; no mail system in this market treats it that way,
    and a directory certainly does not.
    """
    text = _clean(value)
    if text is None:
        return None
    cleaned = "".join(text.lower().removeprefix("mailto:").strip("<>").split())
    return cleaned if _EMAIL.fullmatch(cleaned) else None


# --------------------------------------------------------------------------- #
# Opening hours
# --------------------------------------------------------------------------- #

# fmt: off
_DAY_INDEX: dict[str, int] = {
    "mo": 0, "mon": 0, "montag": 0, "monday": 0,
    "di": 1, "die": 1, "dienstag": 1, "tu": 1, "tue": 1, "tues": 1, "tuesday": 1,
    "mi": 2, "mit": 2, "mittwoch": 2, "we": 2, "wed": 2, "wednesday": 2,
    "do": 3, "don": 3, "donnerstag": 3, "th": 3, "thu": 3, "thurs": 3, "thursday": 3,
    "fr": 4, "fre": 4, "freitag": 4, "fri": 4, "friday": 4,
    "sa": 5, "sam": 5, "samstag": 5, "sonnabend": 5, "sat": 5, "saturday": 5,
    "so": 6, "son": 6, "sonntag": 6, "su": 6, "sun": 6, "sunday": 6,
}
# fmt: on
_DAY_LABELS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_EVERY_DAY = frozenset({"taglich", "tagl", "daily", "everyday", "durchgehend"})
_CLOSED = frozenset({"geschlossen", "geschl", "closed", "ruhetag", "zu"})
# Words carrying no information once "bis" has become "-".
_HOURS_NOISE = frozenset(
    {
        "und",
        "u",
        "von",
        "ab",
        "uhr",
        "o",
        "clock",
        "open",
        "offen",
        "oder",
        # "So und Feiertage geschlossen" is near-universal on German listings. The
        # holiday clause is dropped rather than modelled -- symmetrically, on both
        # sides -- so it cannot manufacture a difference.
        "feiertag",
        "feiertage",
        "feiertags",
        "feiertagen",
        "holidays",
    }
)

_DAY_RANGE = re.compile(r"\b([a-z]+)\s*-\s*([a-z]+)\b")
_TIME_RANGE = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s*-\s*(\d{1,2})(?:[:.](\d{2}))?")
_MINUTES_PER_DAY = 24 * 60


def _parse_days(segment: str) -> tuple[set[int], bool]:
    """Return the days named in one segment, plus whether an unknown word appeared."""
    days: set[int] = set()
    unknown = False

    def _expand(match: re.Match[str]) -> str:
        start, end = match.group(1), match.group(2)
        if start in _DAY_INDEX and end in _DAY_INDEX:
            first, last = _DAY_INDEX[start], _DAY_INDEX[end]
            days.update((first + step) % 7 for step in range((last - first) % 7 + 1))
            return " "
        return match.group(0)

    for word in re.findall(r"[a-z]+", _DAY_RANGE.sub(_expand, segment)):
        if word in _DAY_INDEX:
            days.add(_DAY_INDEX[word])
        elif word in _EVERY_DAY:
            days.update(range(7))
        elif word not in _HOURS_NOISE and word not in _CLOSED:
            unknown = True
    return days, unknown


def _parse_times(segment: str) -> set[tuple[int, int]] | None:
    """Return time ranges as (open, close) minutes past midnight, or ``None`` if malformed."""
    ranges: set[tuple[int, int]] = set()
    for match in _TIME_RANGE.finditer(segment):
        open_h, open_m, close_h, close_m = (int(group or 0) for group in match.groups())
        if open_h > 24 or close_h > 24 or open_m > 59 or close_m > 59:
            return None
        opens, closes = open_h * 60 + open_m, close_h * 60 + close_m
        if closes <= opens:
            closes += _MINUTES_PER_DAY  # a window running past midnight
        ranges.add((opens, closes))
    return ranges


def normalise_opening_hours(value: str | None) -> str | None:
    """Comparison form of opening hours, or ``None`` when they cannot be parsed.

    Handles what German directories actually publish -- ``"Mo-Fr 08:00-18:00, Sa
    09:00-14:00"``, ``"Montag bis Freitag 8-18 Uhr"``, ``"Mo-Fr 9-12 und 14-18"``,
    ``"täglich 11-22"``, ``"Sa geschlossen"`` -- and refuses everything else.

    Refusing is the important half. "Termine nach Vereinbarung" is not a set of
    hours, and the audit emits no finding at all when either side fails to parse.
    Only days that are *open* appear in the canonical form, so "Sunday absent"
    and "Sunday closed" agree -- which they should.
    """
    text = _clean(value)
    if text is None:
        return None
    folded = re.sub(r"\bbis\b", "-", fold_for_comparison(text))
    # "und" is a separator, not noise: in "Mo-Fr 8-18 und Sa 9-14" it joins two
    # day groups, and in "Mo-Fr 9-12 und 14-18" it joins two windows of the same
    # group. Splitting on it handles both, because a segment with times but no
    # days inherits the days of the segment before it.
    folded = re.sub(r"\bund\b|&", ",", folded)
    segments = [segment for segment in re.split(r"[;\n,]", folded) if segment.strip()]
    if not segments:
        return None

    schedule: dict[int, set[tuple[int, int]]] = {}
    pending: set[int] = set()
    previous: set[int] = set()

    for segment in segments:
        days, unknown = _parse_days(segment)
        if unknown:
            return None
        times = _parse_times(segment)
        if times is None:
            return None
        closed = any(word in _CLOSED for word in re.findall(r"[a-z]+", segment))

        if days:
            current = days | pending
            pending = set()
        elif times or closed:
            current = pending or previous
            pending = set()
        else:
            return None  # neither days nor times: this is not opening-hours data
        if not current:
            return None

        if times:
            for day in current:
                schedule.setdefault(day, set()).update(times)
            previous = current
        elif closed:
            for day in current:
                schedule.setdefault(day, set())
            previous = current
        else:
            pending = current  # days announced here, their times arrive next segment

    open_days = sorted(day for day in schedule if schedule[day])
    if not open_days:
        return None
    return ";".join(
        f"{_DAY_LABELS[day]}="
        + ",".join(f"{opens}-{closes}" for opens, closes in sorted(schedule[day]))
        for day in open_days
    )


# --------------------------------------------------------------------------- #
# Whole-record entry points
# --------------------------------------------------------------------------- #


def comparison_form(fields: NapFieldSet, *, country: str = "DE") -> NapComparison:
    """Fold one set of NAP fields into its comparison forms.

    Used for the business's own record *and* for every directory listing, so the
    two sides of a diff can never be normalised by different rules.
    """
    # Annotations come off before the house number is split, or "Hauptstraße 12a,
    # 3. OG" has no trailing number to find and the whole line becomes the street.
    address, _ = strip_address_annotation(fields.street)
    street_name, inline_number = split_street_and_number(address)
    house_number, _ = strip_address_annotation(fields.house_number)
    return NapComparison(
        legal_name=normalise_business_name(fields.legal_name),
        trading_name=normalise_business_name(fields.trading_name),
        street=normalise_street_name(street_name),
        house_number=normalise_house_number(house_number or inline_number),
        postcode=normalise_postcode(fields.postcode, country=country),
        city=normalise_city(fields.city),
        phone=normalise_phone(fields.phone, country=country),
        email=normalise_email(fields.email),
        opening_hours=normalise_opening_hours(fields.opening_hours),
        primary_category=normalise_business_name(fields.primary_category),
    )


def _display_address(raw: RawNap) -> tuple[str | None, str | None]:
    """Display street and house number, with any annotation preserved.

    The annotation rides on the house number, because that is where a German
    address line puts it: concatenating the two display fields gives back
    "Hauptstraße 12a, 3. OG". It is dropped from the comparison form only, so the
    owner keeps the detail a courier needs while the diff ignores it.

    (A dedicated ``address_annotation`` field would be tidier still, but that
    widens the public contract; noted rather than done.)
    """
    address, street_annotation = strip_address_annotation(raw.street)
    street_name, inline_number = split_street_and_number(address)
    number, number_annotation = strip_address_annotation(raw.house_number)

    house_number = number or inline_number
    annotation = street_annotation or number_annotation
    if annotation is None:
        return street_name, house_number
    if house_number is not None:
        return street_name, f"{house_number}, {annotation}"
    if street_name is not None:
        return f"{street_name}, {annotation}", None
    return street_name, house_number


def normalise_nap(raw: RawNap, *, country: str = "DE") -> CanonicalNap:
    """Build the canonical record: display values a human can paste, plus comparison forms.

    Nothing is thrown away. The legal name keeps its Rechtsform, the street keeps
    its umlauts, and the phone is rendered in the international form German
    directories expect (``+49 261 123456``). Only :attr:`CanonicalNap.comparison`
    is folded.

    An inline house number is lifted out of ``street`` so the display record has
    the two fields directories ask for separately, and a floor or entrance
    annotation stays with it.
    """
    street_display, house_number_display = _display_address(raw)
    return CanonicalNap(
        legal_name=_clean(raw.legal_name),
        trading_name=_clean(raw.trading_name),
        street=street_display,
        house_number=house_number_display,
        # A valid postcode is displayed in its bare five-digit form; an invalid one
        # is echoed back unchanged rather than silently "corrected".
        postcode=normalise_postcode(raw.postcode, country=country) or _clean(raw.postcode),
        city=_clean(raw.city),
        phone=_display_phone(raw.phone, country=country),
        email=normalise_email(raw.email) or _clean(raw.email),
        opening_hours=_clean(raw.opening_hours),
        primary_category=_clean(raw.primary_category),
        country=country.upper(),
        comparison=comparison_form(raw, country=country),
    )
