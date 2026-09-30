"""Invented bons du livreur, for every consignes test (read-only for the
other test modules: copy with `uba_rules(**changes)`, never mutate).

The LAYOUT is UBA's driver ticket as pdfplumber's extract_text() gives it
(spec §1): the header, « Le <date> <heure> » when it was printed, the
ticket number, the optional « ***ANNULE ET REMPLACE*** », the REPRISE VIDE
part and its 24-dash separator, labels CUT TO 20 CHARACTERS that may hold
digits, « Û » (U+00DB), one or more « BL No: … du … » lines with an
optional « Montant », « Deconsigne », the optional ANOMALIES block. Every
VALUE is invented - names, addresses, phones, account, driver, ticket and
BL numbers, dates, counts and amounts: the owner's real tickets were read
for their structure only, and this repository is public.

Dates are in the past (a reading refuses a date after today + 7 days).
All texts are Latin-1, so `invoices/tests/pdf_files.write_pdf` prints them
(Helvetica, WinAnsiEncoding keeps « Û »).
"""

from datetime import date, time
from decimal import Decimal
from types import SimpleNamespace

U_HAT = "\N{LATIN CAPITAL LETTER U WITH CIRCUMFLEX}"
SEPARATOR = "-" * 24
PLUSES = "+" * 40

# -- Designations as the ticket prints them (at most 20 characters) ------------------------------------------------

#: A keg, not cut (17 characters): the invoice's raw name is the same.
KEG = f"F{U_HAT}T 12/18/24/36 L"
#: A keg whose label the ticket cut at 20 characters, digits and all.
KEG_LONG = f"F{U_HAT}T 12/18/24/36/48/6"
CO2 = "BOUTEILLE CO2 4/8/12"
CRATE = "CAISSE 24X25 CL VERR"
WATER = "EAUX 33-50 CL - 1L"
JUICE = "JUS ET SODAS 20-33 C"
#: No seeded type recognises it.
PALLET = "PALETTE BOIS EUROPE"

#: The untruncated designation the seller's invoice prints (its raw_name):
#: the ticket's label is its first 20 characters.
FULL_NAMES = {
    KEG: KEG,
    KEG_LONG: f"F{U_HAT}T 12/18/24/36/48/60 L",
    CO2: "BOUTEILLE CO2 4/8/12 KG",
    CRATE: "CAISSE 24X25 CL VERRE",
    WATER: WATER,
    JUICE: "JUS ET SODAS 20-33 CL",
    PALLET: PALLET,
}

# -- The seeds (spec §4), written out here so tests do not depend on the migration --------------------------------

UBA_FORMAT_NAME = "UBA \N{EM DASH} bon du livreur"

UBA_MOTIFS = {
    "sender_pattern": r"mphone@uba\.paris",
    "subject_pattern": r"^\s*Livraison du\b",
    "attachment_pattern": r"(?i)\.pdf$",
    "section_start": r"^REPRISE VIDE\s*$",
    "section_end": r"^FACTURE\(S\)/BL DU JOUR",
    "line_pattern": (
        r"^(?P<designation>.+?)\s+(?P<quantite>-?\d+)\s+x\s+(?P<prix>-?\d+(?:[.,]\d+)?)"
        r"\s+=\s+(?P<montant>-?\d+(?:[.,]\d+)?)\s*$"
    ),
    "date_patterns": "\n".join(
        [
            r"BL No:\s*\d+\s+du\s+(?P<date>\d{2}/\d{2}/\d{4})",
            r"^Le\s+(?P<date>\d{2}/\d{2}/\d{4})",
        ]
    ),
    "printed_patterns": r"^Le\s+(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<heure>\d{2}:\d{2}(?::\d{2})?)",
    "number_patterns": r"Ticket No\s*:\s*0*(?P<numero>\d+)",
    "reference_patterns": r"BL No:\s*(?P<reference>\d+)",
    "replaces_pattern": r"ANNULE ET REMPLACE",
    "total_patterns": r"Deconsigne\s*:\s*(?P<total>-?\d+(?:[.,]\d+)?)",
    "remarks_start": r"^ANOMALIES\s*$",
    "remarks_end": r"^Merci de Votre Commande",
}

#: (name, position, motifs) of the three seeded types.
SEED_TYPES = (
    ("F\N{LATIN SMALL LETTER U WITH CIRCUMFLEX}ts", 1, rf"F[{U_HAT}U]TS?\b"),
    ("Caisses verre", 2, r"CAISSE|CASIER|JUS|SODA|\bEAUX?\b|\d+\s*CL\b"),
    ("Bouteilles CO2", 3, r"CO2|\bGAZ\b"),
)
KEGS, CRATES, BOTTLES = (name for name, _position, _motifs in SEED_TYPES)

#: The seeded type each designation above belongs to (None: unclassified).
EXPECTED_TYPE = {
    KEG: KEGS,
    KEG_LONG: KEGS,
    CO2: BOTTLES,
    CRATE: CRATES,
    WATER: CRATES,
    JUICE: CRATES,
    PALLET: None,
}


def uba_rules(**changes) -> SimpleNamespace:
    """A format carrying the seeded UBA motifs (and a name, active), as the
    pure modules take it - `changes` replacing any attribute."""
    values = {"name": UBA_FORMAT_NAME, "is_active": True, **UBA_MOTIFS}
    values.update(changes)
    return SimpleNamespace(**values)


#: The seeded UBA format, as a namespace. Do not mutate: use uba_rules().
UBA_RULES = uba_rules()


def seed_types() -> list:
    """The three seeded types as namespaces (pk 1, 2, 3), for classification."""
    return [
        SimpleNamespace(pk=position, id=position, name=name, position=position, is_active=True, slip_patterns=motifs)
        for name, position, motifs in SEED_TYPES
    ]


# -- Building a ticket ----------------------------------------------------------------------------------------------


def row(designation: str, quantity, unit: str, amount: str) -> str:
    """A line of the REPRISE VIDE part: « <label> <q> x <unit> = <amount> »."""
    return f"{designation} {quantity} x {unit} = {amount}"


def ticket(*, number: str, printed: str, bl_lines, rows=(), deconsigne: str, total_net: str,
           replaces: bool = False, anomalies=(), section: bool = True, trailer=()) -> str:
    """One ticket's text, in the order extract_text() gives it. `printed` is
    « dd/mm/yyyy hh:mm:ss »; `bl_lines` the lines under « FACTURE(S)/BL DU
    JOUR » before « Deconsigne »; `anomalies` the lines of an ANOMALIES
    block (none: no block); `section=False` leaves REPRISE VIDE out;
    `trailer` lines are printed after the last line."""
    lines = [
        "U.B.A.",
        "1 RUE DE L'EXEMPLE",
        "99999 VILLE-EXEMPLE",
        "TEL : 01.00.00.00.00",
        f"Le {printed}",
        SEPARATOR,
        "Tournee : ZZZ TOURNEE 9999",
        "Livreur : 00099 PRENOM",
        f"Ticket No : {number}",
    ]
    if replaces:
        lines.append("***ANNULE ET REMPLACE***")
    lines += [
        "Compte : 00000",
        "BAR DE L'EXEMPLE",
        "1 PLACE DU TEST",
        "75000 PARIS",
        "Tel : 00.00.00.00.00",
    ]
    if section:
        lines += ["REPRISE VIDE", SEPARATOR, *rows]
    lines += ["FACTURE(S)/BL DU JOUR", SEPARATOR, *bl_lines]
    lines += [
        f"Deconsigne : {deconsigne}",
        f"Total Net : {total_net}",
        "ENCAISSEMENT",
        SEPARATOR,
        "Tot. Encaissement : 0.00EUR",
    ]
    if anomalies:
        lines += ["ANOMALIES", PLUSES, *anomalies, PLUSES]
    lines += ["Merci de Votre Commande", "Signature Client", *trailer]
    return "\n".join(lines)


def _bon(name, text, *, number, delivery, printed, references, lines=(), unread=(), total, replaces=False,
         remarks="") -> SimpleNamespace:
    """A ticket and what the seeded UBA motifs read in it."""
    return SimpleNamespace(
        name=name,
        text=text,
        number=number,
        delivery_date=delivery,
        printed=printed,
        references=list(references),
        lines=list(lines),
        unread=list(unread),
        printed_total=total,
        replaces=replaces,
        remarks=remarks,
    )


D = Decimal

# -- The tickets: one per quirk -------------------------------------------------------------------------------------

#: Two rows (kegs, a cut CO2 label), one BL with its Montant.
NORMAL = _bon(
    "normal",
    ticket(
        number="0000001001",
        printed="14/05/2025 08:15:02",
        rows=[row(KEG, 3, "30.00", "90.00"), row(CO2, 1, "85.00", "85.00")],
        bl_lines=["BL No: 610001 du 14/05/2025", "Montant : 123.45"],
        deconsigne="-175.00",
        total_net="-51.55",
    ),
    number="1001",
    delivery=date(2025, 5, 14),
    printed=(date(2025, 5, 14), time(8, 15, 2)),
    references=["610001"],
    lines=[(KEG, 3, D("30.00"), D("90.00")), (CO2, 1, D("85.00"), D("85.00"))],
    total=D("-175.00"),
)

#: NORMAL e-mailed again, printed the next morning: identical but « Le … ».
RESEND = _bon(
    "resend",
    NORMAL.text.replace("Le 14/05/2025 08:15:02", "Le 15/05/2025 07:40:10"),
    number="1001",
    delivery=date(2025, 5, 14),
    printed=(date(2025, 5, 15), time(7, 40, 10)),
    references=["610001"],
    lines=NORMAL.lines,
    total=D("-175.00"),
)

#: Nothing handed back: the REPRISE VIDE part has its separator only.
EMPTY = _bon(
    "empty",
    ticket(
        number="0000001002",
        printed="16/05/2025 09:00:00",
        bl_lines=["BL No: 610002 du 16/05/2025", "Montant : 250.00"],
        deconsigne="0.00",
        total_net="250.00",
    ),
    number="1002",
    delivery=date(2025, 5, 16),
    printed=(date(2025, 5, 16), time(9, 0, 0)),
    references=["610002"],
    total=D("0.00"),
)

#: Two BL lines, then ONE Montant for both.
TWO_BLS = _bon(
    "two BLs, one Montant",
    ticket(
        number="0000001003",
        printed="20/05/2025 10:12:45",
        rows=[row(KEG, 6, "30.00", "180.00")],
        bl_lines=["BL No: 610003 du 20/05/2025", "BL No: 610004 du 20/05/2025", "Montant : 480.00"],
        deconsigne="-180.00",
        total_net="300.00",
    ),
    number="1003",
    delivery=date(2025, 5, 20),
    printed=(date(2025, 5, 20), time(10, 12, 45)),
    references=["610003", "610004"],
    lines=[(KEG, 6, D("30.00"), D("180.00"))],
    total=D("-180.00"),
)

#: A BL with no Montant line at all.
BL_WITHOUT_MONTANT = _bon(
    "BL without Montant",
    ticket(
        number="0000001004",
        printed="22/05/2025 08:30:00",
        rows=[row(KEG, 2, "30.00", "60.00")],
        bl_lines=["BL No: 610005 du 22/05/2025"],
        deconsigne="-60.00",
        total_net="-60.00",
    ),
    number="1004",
    delivery=date(2025, 5, 22),
    printed=(date(2025, 5, 22), time(8, 30, 0)),
    references=["610005"],
    lines=[(KEG, 2, D("30.00"), D("60.00"))],
    total=D("-60.00"),
)

#: A ticket later cancelled and replaced (by REPLACEMENT: same BL, a new
#: number, one keg less).
ORIGINAL = _bon(
    "original",
    ticket(
        number="0000001005",
        printed="26/05/2025 07:55:00",
        rows=[row(KEG, 5, "30.00", "150.00")],
        bl_lines=["BL No: 610006 du 26/05/2025", "Montant : 360.00"],
        deconsigne="-150.00",
        total_net="210.00",
    ),
    number="1005",
    delivery=date(2025, 5, 26),
    printed=(date(2025, 5, 26), time(7, 55, 0)),
    references=["610006"],
    lines=[(KEG, 5, D("30.00"), D("150.00"))],
    total=D("-150.00"),
)

REPLACEMENT = _bon(
    "replacement",
    ticket(
        number="0000001006",
        printed="26/05/2025 11:20:00",
        replaces=True,
        rows=[row(KEG, 4, "30.00", "120.00")],
        bl_lines=["BL No: 610006 du 26/05/2025", "Montant : 360.00"],
        deconsigne="-120.00",
        total_net="240.00",
    ),
    number="1006",
    delivery=date(2025, 5, 26),
    printed=(date(2025, 5, 26), time(11, 20, 0)),
    references=["610006"],
    lines=[(KEG, 4, D("30.00"), D("120.00"))],
    total=D("-120.00"),
    replaces=True,
)

#: An empty part, later corrected to kegs (by REPLACEMENT_WITH_KEGS).
ORIGINAL_EMPTY = _bon(
    "original, empty",
    ticket(
        number="0000001007",
        printed="28/05/2025 08:05:00",
        bl_lines=["BL No: 610007 du 28/05/2025", "Montant : 95.00"],
        deconsigne="0.00",
        total_net="95.00",
    ),
    number="1007",
    delivery=date(2025, 5, 28),
    printed=(date(2025, 5, 28), time(8, 5, 0)),
    references=["610007"],
    total=D("0.00"),
)

REPLACEMENT_WITH_KEGS = _bon(
    "replacement, with kegs",
    ticket(
        number="0000001008",
        printed="28/05/2025 16:30:00",
        replaces=True,
        rows=[row(KEG, 2, "30.00", "60.00")],
        bl_lines=["BL No: 610007 du 28/05/2025", "Montant : 95.00"],
        deconsigne="-60.00",
        total_net="35.00",
    ),
    number="1008",
    delivery=date(2025, 5, 28),
    printed=(date(2025, 5, 28), time(16, 30, 0)),
    references=["610007"],
    lines=[(KEG, 2, D("30.00"), D("60.00"))],
    total=D("-60.00"),
    replaces=True,
)

#: The ANOMALIES block: a keg taken back FULL, between two lines of pluses.
ANOMALY_LINES = ["REPRISE MARCHANDISE", "Quantite : 1 FUT", f"REPRISE 1 F{U_HAT}T DE BIERE TEST 20L"]
ANOMALIES = _bon(
    "anomalies",
    ticket(
        number="0000001009",
        printed="02/06/2025 09:30:00",
        rows=[row(KEG, 1, "30.00", "30.00")],
        bl_lines=["BL No: 610008 du 02/06/2025", "Montant : 140.00"],
        deconsigne="-30.00",
        total_net="110.00",
        anomalies=ANOMALY_LINES,
    ),
    number="1009",
    delivery=date(2025, 6, 2),
    printed=(date(2025, 6, 2), time(9, 30, 0)),
    references=["610008"],
    lines=[(KEG, 1, D("30.00"), D("30.00"))],
    total=D("-30.00"),
    remarks="\n".join(ANOMALY_LINES),
)

#: Every seeded type, labels cut at 20 characters, and one line no type
#: recognises (PALLET).
MIXED = _bon(
    "mixed",
    ticket(
        number="0000001010",
        printed="04/06/2025 08:45:00",
        rows=[
            row(KEG_LONG, 2, "30.00", "60.00"),
            row(CRATE, 3, "7.50", "22.50"),
            row(WATER, 2, "4.00", "8.00"),
            row(JUICE, 1, "4.00", "4.00"),
            row(CO2, 1, "85.00", "85.00"),
            row(PALLET, 1, "12.00", "12.00"),
        ],
        bl_lines=["BL No: 610009 du 04/06/2025", "Montant : 410.00"],
        deconsigne="-191.50",
        total_net="218.50",
    ),
    number="1010",
    delivery=date(2025, 6, 4),
    printed=(date(2025, 6, 4), time(8, 45, 0)),
    references=["610009"],
    lines=[
        (KEG_LONG, 2, D("30.00"), D("60.00")),
        (CRATE, 3, D("7.50"), D("22.50")),
        (WATER, 2, D("4.00"), D("8.00")),
        (JUICE, 1, D("4.00"), D("4.00")),
        (CO2, 1, D("85.00"), D("85.00")),
        (PALLET, 1, D("12.00"), D("12.00")),
    ],
    total=D("-191.50"),
)

#: Two lines of the part the line motif cannot read: one with no figures,
#: one without its amount.
UNREAD_LINES = ["CASIER DIVERS", f"{KEG} 1 x 30.00"]
UNREAD = _bon(
    "unread line",
    ticket(
        number="0000001011",
        printed="06/06/2025 08:00:00",
        rows=[row(KEG, 2, "30.00", "60.00"), *UNREAD_LINES],
        bl_lines=["BL No: 610010 du 06/06/2025", "Montant : 75.00"],
        deconsigne="-60.00",
        total_net="15.00",
    ),
    number="1011",
    delivery=date(2025, 6, 6),
    printed=(date(2025, 6, 6), time(8, 0, 0)),
    references=["610010"],
    lines=[(KEG, 2, D("30.00"), D("60.00"))],
    unread=UNREAD_LINES,
    total=D("-60.00"),
)

#: q × p ≠ m: 3 × 30.00 printed as 95.00 (the total agrees with the lines).
WRONG_PRODUCT = _bon(
    "q × p ≠ m",
    ticket(
        number="0000001012",
        printed="10/06/2025 08:10:00",
        rows=[row(KEG, 3, "30.00", "95.00")],
        bl_lines=["BL No: 610011 du 10/06/2025", "Montant : 100.00"],
        deconsigne="-95.00",
        total_net="5.00",
    ),
    number="1012",
    delivery=date(2025, 6, 10),
    printed=(date(2025, 6, 10), time(8, 10, 0)),
    references=["610011"],
    lines=[(KEG, 3, D("30.00"), D("95.00"))],
    total=D("-95.00"),
)

#: Σ rows ≠ total: 90.00 of lines, a Deconsigne of -120.00.
WRONG_TOTAL = _bon(
    "Σ ≠ total",
    ticket(
        number="0000001013",
        printed="12/06/2025 08:20:00",
        rows=[row(KEG, 3, "30.00", "90.00")],
        bl_lines=["BL No: 610012 du 12/06/2025", "Montant : 200.00"],
        deconsigne="-120.00",
        total_net="80.00",
    ),
    number="1013",
    delivery=date(2025, 6, 12),
    printed=(date(2025, 6, 12), time(8, 20, 0)),
    references=["610012"],
    lines=[(KEG, 3, D("30.00"), D("90.00"))],
    total=D("-120.00"),
)

#: No REPRISE VIDE part at all.
NO_SECTION = _bon(
    "no section",
    ticket(
        number="0000001014",
        printed="16/06/2025 08:40:00",
        section=False,
        bl_lines=["BL No: 610013 du 16/06/2025", "Montant : 55.00"],
        deconsigne="0.00",
        total_net="55.00",
    ),
    number="1014",
    delivery=date(2025, 6, 16),
    printed=(date(2025, 6, 16), time(8, 40, 0)),
    references=["610013"],
    total=D("0.00"),
)

#: The REPRISE VIDE part printed twice (a duplicated copy after the end):
#: only the first is read.
DOUBLE_SECTION = _bon(
    "section twice",
    ticket(
        number="0000001015",
        printed="18/06/2025 08:50:00",
        rows=[row(KEG, 1, "30.00", "30.00")],
        bl_lines=["BL No: 610014 du 18/06/2025", "Montant : 45.00"],
        deconsigne="-30.00",
        total_net="15.00",
        trailer=["REPRISE VIDE", SEPARATOR, row(KEG, 1, "30.00", "30.00"), "FACTURE(S)/BL DU JOUR"],
    ),
    number="1015",
    delivery=date(2025, 6, 18),
    printed=(date(2025, 6, 18), time(8, 50, 0)),
    references=["610014"],
    lines=[(KEG, 1, D("30.00"), D("30.00"))],
    total=D("-30.00"),
)

#: Not a bon: a mail's attachment of general conditions.
JUNK = "\n".join(
    [
        "Bonjour,",
        "Veuillez trouver nos conditions generales de vente.",
        "Article 1 : les emballages consignes restent notre propriete.",
        "Cordialement,",
        "Le service client",
    ]
)

#: Every ticket above that the seeded motifs read without a failed check.
CLEAN = (NORMAL, RESEND, EMPTY, TWO_BLS, BL_WITHOUT_MONTANT, ORIGINAL, REPLACEMENT, ORIGINAL_EMPTY,
         REPLACEMENT_WITH_KEGS, ANOMALIES, MIXED)
#: Every ticket above.
ALL = CLEAN + (UNREAD, WRONG_PRODUCT, WRONG_TOTAL, NO_SECTION, DOUBLE_SECTION)


def pdf_lines(text: str) -> list:
    """The lines to hand write_pdf for `text`."""
    return text.split("\n")
