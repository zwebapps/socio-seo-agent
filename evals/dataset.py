"""The evaluation set: 20 cases over five real German SMB shapes.

Five businesses -- a plumber, a dentist, a bakery, a Steuerberater and a small
SaaS -- with four content pieces each, across five channels. They are the shapes
this product is actually for (`PROBLEM.md`), and they are chosen because each one
carries a *different* failure mode:

* **Plumber** -- local, urgent, price-sensitive. Comparative superlatives
  ("cheapest provider") are unlawful advertising in Germany when unverifiable.
* **Dentist** -- regulated. The Heilmittelwerbegesetz forbids promising a
  treatment outcome, so "pain-free" and "guaranteed cure" are not merely
  off-brand, they are illegal. This is the case that proves the banned-claim check
  is worth having.
* **Bakery** -- health-claim rules (HCVO) bite on "healthiest bread"; and this is
  the visual channel, so the hashtag and no-clickable-link constraints are real.
* **Steuerberater** -- professional-conduct rules (StBerG) bar promising a tax
  saving. Also the case with the most figures, which is where grounding matters.
* **SaaS** -- B2B, and the home of the invented technical claim: there is no such
  thing as "GDPR-certified", and "100% uptime" is a claim no one can honour.

**Every case states both halves.** ``must_contain`` is what a correct output has to
say; ``banned_claims`` is what it may never say. A dataset with only the second
half would pass an empty page, and one with only the first would pass a compliant
lie.

**``facts`` is the business's own document, chunked.** It is the corpus the RAG-on
arm retrieves from, and it is the *only* place the figures in ``reference_answer``
come from -- so a claim in the reference is traceable by construction, which is
what makes the grounding scorer's discrimination testable.

**``reference_answer`` is a human-written exemplar, not a target for a model to
match.** Its job is to prove the rubric can recognise a correct output, next to
:meth:`EvalCase.violating_answer`, which is the same text with a forbidden claim
appended. A rubric nobody has seen both pass and fail is a rubric nobody should
believe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The business shapes covered. Asserted against the cases at import.
VERTICALS: Final[tuple[str, ...]] = (
    "plumber",
    "dentist",
    "bakery",
    "steuerberater",
    "saas",
)


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    """The business a case is written for.

    Deliberately thin: this is what the intake node would have extracted as
    Business DNA, not a database row. No ids, because nothing here touches a
    database -- the harness mints its own.
    """

    name: str
    vertical: str
    city: str
    locale: str
    services: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One business, one topic, one channel, and what "correct" means for it."""

    case_id: str
    business: BusinessProfile
    #: The keyword the piece is written for. Feeds the seo engine on article cases.
    target_keyword: str
    secondary_keywords: tuple[str, ...]
    #: A key in ``rubric.CHANNEL_LIMITS``. Decides the format constraints.
    channel: str
    #: The instruction handed to the generator. Written the way a node would.
    brief: str
    #: Terms a correct output must contain (scored by ``score_coverage``).
    must_contain: tuple[str, ...]
    #: Claims a correct output must NOT contain (scored by ``score_brand``).
    banned_claims: tuple[str, ...]
    #: The business's own document, chunked. The RAG corpus for this case.
    facts: tuple[str, ...]
    #: A human-written correct output, drawing every figure from ``facts``.
    reference_answer: str

    def chunk_ids(self) -> tuple[str, ...]:
        """Stable ids for this case's chunks: ``<case_id>#<ordinal>``.

        Readable rather than UUIDs, because these ids are printed in the report and
        a human has to be able to follow a citation back to a passage.
        """
        return tuple(f"{self.case_id}#{index}" for index in range(len(self.facts)))

    def chunk_map(self) -> dict[str, str]:
        """``chunk_id -> text``, the shape ``score_grounding`` expects."""
        return dict(zip(self.chunk_ids(), self.facts, strict=True))

    def violating_answer(self) -> str:
        """The reference answer with this case's first banned claim appended.

        The negative control. Derived rather than hand-written so it cannot drift
        away from the banned list it is meant to trip.
        """
        return f"{self.reference_answer} Und: {self.banned_claims[0]}."


# --------------------------------------------------------------------------- #
# 1. Plumber -- local, urgent, price-sensitive
# --------------------------------------------------------------------------- #

PLUMBER = BusinessProfile(
    name="Rohr & Ruhe GmbH",
    vertical="plumber",
    city="Koblenz",
    locale="de",
    services=("Notdienst", "Rohrreinigung", "Badsanierung"),
)

# Unverifiable superlatives and absolute guarantees: comparative advertising in
# Germany has to be substantiable, and "cheapest" never is.
PLUMBER_BANNED: Final = ("günstigster Anbieter", "100% Garantie", "billigste")

PLUMBER_FACTS: Final = (
    "Notdienst: Wir sind rund um die Uhr erreichbar und im Stadtgebiet Koblenz in "
    "60 Minuten vor Ort. Die Anfahrt im Stadtgebiet kostet 0 Euro.",
    "Rohrreinigung ab 89 Euro inklusive Mehrwertsteuer. Der Festpreis wird vor "
    "Beginn der Arbeiten telefonisch genannt.",
    "Wir arbeiten seit 1998 in Koblenz und haben 12 Mitarbeiter. Am Wochenende "
    "gibt es keine Zuschläge auf den Stundensatz.",
    "Badsanierung: Planung, Fliesen und Sanitär aus einer Hand. Ein komplettes Bad "
    "dauert im Schnitt 10 Werktage.",
)

# --------------------------------------------------------------------------- #
# 2. Dentist -- regulated: no promised outcomes (HWG)
# --------------------------------------------------------------------------- #

DENTIST = BusinessProfile(
    name="Zahnarztpraxis Dr. Weber",
    vertical="dentist",
    city="Trier",
    locale="de",
    services=("Prophylaxe", "Implantate", "Angstpatienten"),
)

DENTIST_BANNED: Final = (
    "schmerzfrei",
    "garantierte Heilung",
    "beste Zahnarztpraxis",
    "ohne Risiko",
)

DENTIST_FACTS: Final = (
    "Prophylaxe: Die professionelle Zahnreinigung dauert 45 Minuten und kostet "
    "95 Euro. Wir empfehlen zwei Termine pro Jahr.",
    "Angstpatienten: Auf Wunsch behandeln wir in Sedierung. Ein Erstgespräch von "
    "20 Minuten ist kostenfrei und ohne Behandlung.",
    "Implantate: Wir setzen Implantate seit 2011. Die Einheilzeit beträgt in der "
    "Regel 3 bis 6 Monate.",
    "Öffnungszeiten: Montag bis Freitag von 8 bis 18 Uhr. Notfalltermine "
    "vergeben wir am selben Tag.",
)

# --------------------------------------------------------------------------- #
# 3. Bakery -- health claims (HCVO), and the visual channels
# --------------------------------------------------------------------------- #

BAKERY = BusinessProfile(
    name="Bäckerei Kluth",
    vertical="bakery",
    city="Mainz",
    locale="de",
    services=("Backwaren", "Torten auf Bestellung", "Filialen"),
)

BAKERY_BANNED: Final = ("beste Bäckerei", "gesündestes Brot", "macht schlank")

BAKERY_FACTS: Final = (
    "Wir backen 14 Brotsorten täglich ab 5 Uhr morgens. Das Roggenmischbrot ruht "
    "18 Stunden im Sauerteig.",
    "Torten auf Bestellung: Bitte 3 Tage vorher bestellen. Eine Hochzeitstorte "
    "für 40 Personen kostet ab 180 Euro.",
    "Vier Filialen in Mainz. Die Filiale am Hauptbahnhof öffnet um 6 Uhr, sonntags um 7 Uhr.",
    "Wir verwenden Mehl aus einer Mühle in Rheinhessen, 30 Kilometer entfernt.",
)

# --------------------------------------------------------------------------- #
# 4. Steuerberater -- professional conduct (StBerG), and the most figures
# --------------------------------------------------------------------------- #

STEUERBERATER = BusinessProfile(
    name="Kanzlei Lindner",
    vertical="steuerberater",
    city="Bonn",
    locale="de",
    services=("Lohnbuchhaltung", "Jahresabschluss", "digitale Belege"),
)

STEUERBERATER_BANNED: Final = (
    "garantierte Steuerersparnis",
    "beste Kanzlei",
    "wir senken Ihre Steuerlast immer",
)

STEUERBERATER_FACTS: Final = (
    "Wir betreuen 210 kleine und mittlere Unternehmen. Die Erstberatung dauert "
    "30 Minuten und ist kostenfrei.",
    "Lohnbuchhaltung ab 12 Euro pro Mitarbeiter und Monat. Die Meldungen gehen "
    "bis zum 10. des Folgemonats raus.",
    "Jahresabschluss: Wir arbeiten mit DATEV. Die Frist für die Steuererklärung "
    "endet am 31. Juli des Folgejahres.",
    "Digitale Belege: Sie fotografieren den Beleg, wir buchen ihn innerhalb von 2 Werktagen.",
)

# --------------------------------------------------------------------------- #
# 5. SaaS -- B2B, and the home of the invented technical claim
# --------------------------------------------------------------------------- #

SAAS = BusinessProfile(
    name="Schichtplan Cloud",
    vertical="saas",
    city="Kiel",
    locale="de",
    services=("Schichtplanung", "Zeiterfassung", "Excel-Import"),
)

# "DSGVO-zertifiziert" is the important one: no such certification exists under
# Article 42, so it is a fabricated credential rather than a strong claim.
SAAS_BANNED: Final = ("100% Verfügbarkeit", "unhackbar", "DSGVO-zertifiziert")

SAAS_FACTS: Final = (
    "Der Tarif Team kostet 4 Euro pro Nutzer und Monat, mindestens 5 Nutzer. Die "
    "Testphase dauert 14 Tage und verlangt keine Zahlungsdaten.",
    "Unsere Server stehen in Frankfurt. Wir schließen einen Vertrag zur "
    "Auftragsverarbeitung nach Artikel 28 DSGVO. Backups halten wir 30 Tage.",
    "Import aus Excel in 3 Schritten. Ein Team von 20 Personen ist in etwa "
    "25 Minuten eingerichtet.",
    "Support per E-Mail, Antwort innerhalb von 4 Stunden an Werktagen. Im Tarif "
    "Team gibt es keinen Telefonsupport.",
)


# --------------------------------------------------------------------------- #
# The cases
# --------------------------------------------------------------------------- #

CASES: Final[tuple[EvalCase, ...]] = (
    # ---------------------------- plumber ---------------------------------- #
    EvalCase(
        case_id="plumber-01",
        business=PLUMBER,
        target_keyword="notdienst klempner koblenz",
        secondary_keywords=("rohrbruch", "festpreis"),
        channel="blog_article",
        brief=(
            "Write the money page for the emergency plumbing service: what happens "
            "when someone calls at night, how fast we arrive, and how the price is "
            "agreed before any work starts."
        ),
        must_contain=("Notdienst", "60 Minuten", "Festpreis"),
        banned_claims=PLUMBER_BANNED,
        facts=(PLUMBER_FACTS[0], PLUMBER_FACTS[1], PLUMBER_FACTS[2]),
        reference_answer=(
            "Unser Notdienst ist rund um die Uhr erreichbar. Im Stadtgebiet Koblenz "
            "sind wir in 60 Minuten vor Ort. Den Festpreis nennen wir vorab am "
            "Telefon: eine Rohrreinigung beginnt bei 89 Euro, die Anfahrt im "
            "Stadtgebiet kostet 0 Euro."
        ),
    ),
    EvalCase(
        case_id="plumber-02",
        business=PLUMBER,
        target_keyword="rohrreinigung koblenz",
        secondary_keywords=("abfluss verstopft",),
        channel="linkedin",
        brief=(
            "A short professional post for local property managers about drain "
            "cleaning: what it costs and how the appointment works."
        ),
        must_contain=("Rohrreinigung", "89 Euro"),
        banned_claims=PLUMBER_BANNED,
        facts=(PLUMBER_FACTS[1], PLUMBER_FACTS[2]),
        reference_answer=(
            "Verstopfter Abfluss im Objekt? Eine Rohrreinigung beginnt bei 89 Euro "
            "inklusive Mehrwertsteuer, und der Festpreis steht vor dem ersten "
            "Handgriff. Wir arbeiten seit 1998 in Koblenz. #klempner #koblenz"
        ),
    ),
    EvalCase(
        case_id="plumber-03",
        business=PLUMBER,
        target_keyword="badsanierung koblenz",
        secondary_keywords=("bad renovieren",),
        channel="instagram_caption",
        brief=(
            "A feed caption for a finished bathroom renovation. No link in the "
            "caption -- point people to the profile."
        ),
        must_contain=("Badsanierung", "10 Werktage"),
        banned_claims=PLUMBER_BANNED,
        facts=(PLUMBER_FACTS[3],),
        reference_answer=(
            "Neues Bad, alles aus einer Hand: Planung, Fliesen und Sanitär. Eine "
            "Badsanierung dauert bei uns im Schnitt 10 Werktage. Termine über den "
            "Link im Profil. #badsanierung #koblenz #handwerk"
        ),
    ),
    EvalCase(
        case_id="plumber-04",
        business=PLUMBER,
        target_keyword="klempner wochenende koblenz",
        secondary_keywords=("zuschlag", "sonntag"),
        channel="facebook_post",
        brief=(
            "A short page post for the weekend: we answer the phone, and there is "
            "no weekend surcharge on the hourly rate."
        ),
        must_contain=("Wochenende", "Zuschläge"),
        banned_claims=PLUMBER_BANNED,
        facts=(PLUMBER_FACTS[2], PLUMBER_FACTS[0]),
        reference_answer=(
            "Auch am Wochenende geht bei uns jemand ans Telefon. Es gibt keine "
            "Zuschläge auf den Stundensatz, und im Stadtgebiet sind wir in "
            "60 Minuten da. Seit 1998 in Koblenz."
        ),
    ),
    # ---------------------------- dentist ---------------------------------- #
    EvalCase(
        case_id="dentist-01",
        business=DENTIST,
        target_keyword="professionelle zahnreinigung trier",
        secondary_keywords=("prophylaxe", "zahnreinigung kosten"),
        channel="blog_article",
        brief=(
            "The service page for professional cleaning: what happens in the "
            "appointment, how long it takes, what it costs. Describe the procedure; "
            "do not promise how the patient will feel or what the outcome will be."
        ),
        must_contain=("Zahnreinigung", "95 Euro", "45 Minuten"),
        banned_claims=DENTIST_BANNED,
        facts=(DENTIST_FACTS[0], DENTIST_FACTS[3]),
        reference_answer=(
            "Die professionelle Zahnreinigung dauert bei uns 45 Minuten und kostet "
            "95 Euro. Wir empfehlen zwei Termine pro Jahr. Sie erreichen uns "
            "montags bis freitags von 8 bis 18 Uhr."
        ),
    ),
    EvalCase(
        case_id="dentist-02",
        business=DENTIST,
        target_keyword="zahnarzt angstpatienten trier",
        secondary_keywords=("zahnarztangst",),
        channel="facebook_post",
        brief=(
            "A short, calm post for anxious patients about the free first "
            "conversation. Describe the process only -- no claim about how the "
            "treatment will feel."
        ),
        must_contain=("Erstgespräch", "20 Minuten"),
        banned_claims=DENTIST_BANNED,
        facts=(DENTIST_FACTS[1],),
        reference_answer=(
            "Angst vor dem Zahnarzt ist verbreitet. Bei uns beginnt es mit einem "
            "Erstgespräch von 20 Minuten, kostenfrei und ohne Behandlung. Auf "
            "Wunsch behandeln wir in Sedierung."
        ),
    ),
    EvalCase(
        case_id="dentist-03",
        business=DENTIST,
        target_keyword="zahnimplantat trier",
        secondary_keywords=("implantat einheilzeit",),
        channel="instagram_caption",
        brief=(
            "A caption explaining what an implant timeline looks like. State the "
            "healing time as a range, and put no link in the caption."
        ),
        must_contain=("Implantate", "Einheilzeit"),
        banned_claims=DENTIST_BANNED,
        facts=(DENTIST_FACTS[2],),
        reference_answer=(
            "Wir setzen Implantate seit 2011. Die Einheilzeit beträgt in der Regel "
            "3 bis 6 Monate, danach kommt die Krone. Fragen? Schreibt uns eine "
            "Nachricht. #implantat #zahnarzt #trier"
        ),
    ),
    EvalCase(
        case_id="dentist-04",
        business=DENTIST,
        target_keyword="zahnarzt notfalltermin trier",
        secondary_keywords=("zahnschmerzen",),
        channel="email",
        brief=(
            "A short email to existing patients: how to get a same-day emergency "
            "appointment, and the opening hours."
        ),
        must_contain=("Notfalltermine", "8 bis 18 Uhr"),
        banned_claims=DENTIST_BANNED,
        facts=(DENTIST_FACTS[3], DENTIST_FACTS[0]),
        reference_answer=(
            "Guten Tag,\n\nakute Zahnschmerzen sollten nicht warten. "
            "Notfalltermine vergeben wir am selben Tag. Rufen Sie einfach an: wir "
            "sind montags bis freitags von 8 bis 18 Uhr erreichbar. Wer ohnehin "
            "einen Prophylaxetermin einplanen möchte: die professionelle "
            "Zahnreinigung dauert 45 Minuten und kostet 95 Euro.\n\n"
            "Mit freundlichen Grüßen\nIhre Praxis Dr. Weber"
        ),
    ),
    # ---------------------------- bakery ----------------------------------- #
    EvalCase(
        case_id="bakery-01",
        business=BAKERY,
        target_keyword="sauerteigbrot mainz",
        secondary_keywords=("bäckerei mainz", "roggenmischbrot"),
        channel="blog_article",
        brief=(
            "An article about how the rye bread is actually made: the long "
            "fermentation, the flour, the daily bake. Describe the craft; make no "
            "nutrition or health claim."
        ),
        must_contain=("Sauerteig", "18 Stunden", "14 Brotsorten"),
        banned_claims=BAKERY_BANNED,
        facts=(BAKERY_FACTS[0], BAKERY_FACTS[3]),
        reference_answer=(
            "Wir backen 14 Brotsorten täglich ab 5 Uhr morgens. Unser "
            "Roggenmischbrot ruht 18 Stunden im Sauerteig, bevor es in den Ofen "
            "geht. Das Mehl kommt aus einer Mühle in Rheinhessen, 30 Kilometer "
            "entfernt."
        ),
    ),
    EvalCase(
        case_id="bakery-02",
        business=BAKERY,
        target_keyword="hochzeitstorte mainz",
        secondary_keywords=("torte bestellen",),
        channel="instagram_caption",
        brief=(
            "A caption for a wedding cake photo: the lead time and the starting "
            "price. No link in the caption."
        ),
        must_contain=("Hochzeitstorte", "3 Tage", "180 Euro"),
        banned_claims=BAKERY_BANNED,
        facts=(BAKERY_FACTS[1],),
        reference_answer=(
            "Eine Hochzeitstorte für 40 Personen gibt es bei uns ab 180 Euro. "
            "Bitte 3 Tage vorher bestellen, dann planen wir Form und Geschmack "
            "gemeinsam. #hochzeitstorte #mainz #handwerk"
        ),
    ),
    EvalCase(
        case_id="bakery-03",
        business=BAKERY,
        target_keyword="bäckerei hauptbahnhof mainz",
        secondary_keywords=("öffnungszeiten sonntag",),
        channel="facebook_post",
        brief="A short post about the station branch and its opening hours, including Sunday.",
        must_contain=("Hauptbahnhof", "6 Uhr", "sonntags"),
        banned_claims=BAKERY_BANNED,
        facts=(BAKERY_FACTS[2],),
        reference_answer=(
            "Frühschicht oder früher Zug? Unsere Filiale am Hauptbahnhof öffnet um "
            "6 Uhr, sonntags um 7 Uhr. Insgesamt finden Sie uns an vier Standorten "
            "in Mainz."
        ),
    ),
    EvalCase(
        case_id="bakery-04",
        business=BAKERY,
        target_keyword="brot vorbestellen mainz",
        secondary_keywords=("stammkunden",),
        channel="email",
        brief=(
            "An email to regulars: pre-order breads and cakes, with the lead time "
            "for cakes and the daily bake time for bread."
        ),
        must_contain=("vorbestellen", "3 Tage", "5 Uhr"),
        banned_claims=BAKERY_BANNED,
        facts=(BAKERY_FACTS[0], BAKERY_FACTS[1]),
        reference_answer=(
            "Guten Morgen,\n\nab jetzt können Sie bei uns vorbestellen. Brot backen "
            "wir täglich ab 5 Uhr, aus 14 Brotsorten wählen Sie am Vorabend. Für "
            "Torten brauchen wir 3 Tage Vorlauf, eine Hochzeitstorte für "
            "40 Personen kostet ab 180 Euro.\n\nBis bald in Mainz\n"
            "Ihre Bäckerei Kluth"
        ),
    ),
    # ------------------------- Steuerberater -------------------------------- #
    EvalCase(
        case_id="steuerberater-01",
        business=STEUERBERATER,
        target_keyword="lohnbuchhaltung bonn",
        secondary_keywords=("lohnabrechnung kosten",),
        channel="linkedin",
        brief=(
            "A post for SMB owners about outsourcing payroll: the price per "
            "employee and the monthly deadline. Do not promise a tax saving."
        ),
        must_contain=("Lohnbuchhaltung", "12 Euro"),
        banned_claims=STEUERBERATER_BANNED,
        facts=(STEUERBERATER_FACTS[1], STEUERBERATER_FACTS[0]),
        reference_answer=(
            "Lohnbuchhaltung auslagern rechnet sich meist ab der fünften "
            "Gehaltsabrechnung. Bei uns beginnt sie bei 12 Euro pro Mitarbeiter und "
            "Monat, die Meldungen gehen bis zum 10. des Folgemonats raus. Wir "
            "betreuen 210 kleine und mittlere Unternehmen. #lohnbuchhaltung #bonn"
        ),
    ),
    EvalCase(
        case_id="steuerberater-02",
        business=STEUERBERATER,
        target_keyword="steuerberater jahresabschluss bonn",
        secondary_keywords=("datev", "frist steuererklärung"),
        channel="blog_article",
        brief=(
            "The service page for the annual accounts: what we need from the "
            "client, which software we work in, and the statutory deadline. State "
            "the deadline as fact and promise no outcome."
        ),
        must_contain=("Jahresabschluss", "DATEV", "31. Juli"),
        banned_claims=STEUERBERATER_BANNED,
        facts=(STEUERBERATER_FACTS[2], STEUERBERATER_FACTS[3]),
        reference_answer=(
            "Für den Jahresabschluss arbeiten wir mit DATEV. Belege fotografieren "
            "Sie, wir buchen sie innerhalb von 2 Werktagen. Die Frist für die "
            "Steuererklärung endet am 31. Juli des Folgejahres."
        ),
    ),
    EvalCase(
        case_id="steuerberater-03",
        business=STEUERBERATER,
        target_keyword="belege digital einreichen",
        secondary_keywords=("digitale buchhaltung",),
        channel="email",
        brief=(
            "An email to clients introducing photo receipt submission: how it "
            "works and how quickly it is booked."
        ),
        must_contain=("Belege", "2 Werktagen"),
        banned_claims=STEUERBERATER_BANNED,
        facts=(STEUERBERATER_FACTS[3],),
        reference_answer=(
            "Guten Tag,\n\nab sofort reichen Sie Belege digital ein: Sie "
            "fotografieren den Beleg, wir buchen ihn innerhalb von 2 Werktagen. "
            "Der Schuhkarton im Januar entfällt damit, und Ihre Zahlen sind das "
            "ganze Jahr aktuell. Rückfragen beantworten wir gern telefonisch.\n\n"
            "Mit freundlichen Grüßen\nKanzlei Lindner"
        ),
    ),
    EvalCase(
        case_id="steuerberater-04",
        business=STEUERBERATER,
        target_keyword="steuerberater erstberatung bonn",
        secondary_keywords=("kostenlose erstberatung",),
        channel="facebook_post",
        brief="A short post offering the free 30-minute first consultation.",
        must_contain=("Erstberatung", "30 Minuten"),
        banned_claims=STEUERBERATER_BANNED,
        facts=(STEUERBERATER_FACTS[0],),
        reference_answer=(
            "Wechselgedanken? Die Erstberatung dauert 30 Minuten und ist "
            "kostenfrei. Wir betreuen 210 kleine und mittlere Unternehmen in und "
            "um Bonn."
        ),
    ),
    # ------------------------------ SaaS ------------------------------------ #
    EvalCase(
        case_id="saas-01",
        business=SAAS,
        target_keyword="schichtplanung software preis",
        secondary_keywords=("dienstplan software", "kosten pro nutzer"),
        channel="blog_article",
        brief=(
            "The pricing page: what the Team plan costs, the minimum seat count, "
            "and what the trial includes. No availability or security claim we "
            "cannot substantiate."
        ),
        must_contain=("Tarif Team", "4 Euro", "14 Tage"),
        banned_claims=SAAS_BANNED,
        facts=(SAAS_FACTS[0], SAAS_FACTS[3]),
        reference_answer=(
            "Der Tarif Team kostet 4 Euro pro Nutzer und Monat, ab 5 Nutzern. Die "
            "Testphase dauert 14 Tage und verlangt keine Zahlungsdaten. Support "
            "läuft per E-Mail, mit Antwort innerhalb von 4 Stunden an Werktagen."
        ),
    ),
    EvalCase(
        case_id="saas-02",
        business=SAAS,
        target_keyword="dsgvo konforme schichtplanung",
        secondary_keywords=("server deutschland", "auftragsverarbeitung"),
        channel="linkedin",
        brief=(
            "A post for HR leads about where the data lives and on what legal "
            "basis. Be exact: name the DPA article, and claim no certification."
        ),
        must_contain=("Frankfurt", "Artikel 28"),
        banned_claims=SAAS_BANNED,
        facts=(SAAS_FACTS[1],),
        reference_answer=(
            "Wo liegen Ihre Dienstpläne? Unsere Server stehen in Frankfurt, und wir "
            "schließen einen Vertrag zur Auftragsverarbeitung nach Artikel 28 "
            "DSGVO. Backups halten wir 30 Tage. #dsgvo #hr"
        ),
    ),
    EvalCase(
        case_id="saas-03",
        business=SAAS,
        target_keyword="excel dienstplan importieren",
        secondary_keywords=("umstieg von excel",),
        channel="email",
        brief=(
            "An onboarding email: import the existing Excel roster in three steps, "
            "with the realistic setup time for a 20-person team."
        ),
        must_contain=("Excel", "3 Schritten", "25 Minuten"),
        banned_claims=SAAS_BANNED,
        facts=(SAAS_FACTS[2], SAAS_FACTS[0]),
        reference_answer=(
            "Hallo,\n\nIhr Dienstplan liegt noch in Excel? Der Import läuft in "
            "3 Schritten: Datei hochladen, Spalten zuordnen, prüfen. Ein Team von "
            "20 Personen ist in etwa 25 Minuten eingerichtet. Die Testphase dauert "
            "14 Tage und verlangt keine Zahlungsdaten.\n\nViele Grüße\n"
            "Ihr Team von Schichtplan Cloud"
        ),
    ),
    EvalCase(
        case_id="saas-04",
        business=SAAS,
        target_keyword="schichtplanung support",
        secondary_keywords=("reaktionszeit support",),
        channel="facebook_post",
        brief=(
            "A short post about what support actually promises, including what the "
            "Team plan does not include."
        ),
        must_contain=("4 Stunden", "Telefonsupport"),
        banned_claims=SAAS_BANNED,
        facts=(SAAS_FACTS[3],),
        reference_answer=(
            "Support ohne Kleingedrucktes: E-Mail, Antwort innerhalb von "
            "4 Stunden an Werktagen. Einen Telefonsupport gibt es im Tarif Team "
            "nicht, und das schreiben wir lieber hin, als es zu verschweigen."
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Import-time integrity. A broken dataset must fail here, not silently produce a
# report with nineteen rows.
# --------------------------------------------------------------------------- #

EXPECTED_CASE_COUNT: Final = 20

if len(CASES) != EXPECTED_CASE_COUNT:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"The eval set must hold exactly {EXPECTED_CASE_COUNT} cases, found {len(CASES)}. "
        "The count is quoted in docs/BUILD_ORDER.md Phase 12 and in the report, so it "
        "is asserted rather than assumed."
    )

_DUPLICATE_IDS = sorted(
    {
        case.case_id
        for case in CASES
        if sum(1 for other in CASES if other.case_id == case.case_id) > 1
    }
)
if _DUPLICATE_IDS:  # pragma: no cover - import-time guard
    raise RuntimeError(f"Duplicate case ids in the eval set: {_DUPLICATE_IDS}.")

_UNCOVERED = sorted(set(VERTICALS) - {case.business.vertical for case in CASES})
if _UNCOVERED:  # pragma: no cover - import-time guard
    raise RuntimeError(f"Verticals declared but not covered by any case: {_UNCOVERED}.")
