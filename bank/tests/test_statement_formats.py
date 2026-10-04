"""Reading a statement laid out as a `StatementFormat` says (bank/models.py,
migration 0007, `bank.statements.check_format` / `parse_statement`) - never
as one bank's columns written in the code.

Wrong money is this codebase's worst failure, and a layout misread is
exactly that, silently: a column taken for another, a thousands mark read as
a decimal point, a debit read as a credit, a row passed over. So each test
holds one promise:

* the seeded format reads every file exactly as the code read it before the
  format existed - replayed against that code, copied here as the historical
  reference (`OracleTests`), its few deliberate differences pinned apart;
* the seed is pinned with literals, reverses to nothing, is left as a person
  edited it when seeded again, and every new espace has it;
* `check_format` refuses, in French and on its own field, what cannot read a
  statement - a column missing, out of range or given two roles, an amount
  said twice or not at all, an unknown choice, an account pattern the guard
  refuses (never compiled: `regex.compile` is a sentinel there);
* another bank's export - commas, a header of column names, ISO dates,
  English decimals, debits and credits apart, a label over two columns, a
  quoted cell holding the separator, a byte order mark - goes through the
  real import, read by rules written for it, and is known again when
  imported again; so are a tab-separated Windows-1252 export and UTF-16;
* what cannot be read whole refuses the FILE, nothing written;
* with no format at all an import is refused; the default is the first by
  (position, name).

Every label, payee, account number and amount below is invented. A slow
pattern is SIMULATED: a pattern that really freezes a machine is never run.
"""

from __future__ import annotations

import calendar
import codecs
import csv
import hashlib
import importlib
import io
import random
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from unittest import mock

import regex
from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, migrations
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from bank import recognition, reconcile, statements, views
from bank.forms import FORMAT_FIELDS
from bank.models import BankTransaction, StatementFormat
from bank.statements import FormatError, Layout, check_format, label_columns, parse_amount, parse_statement
from bank.tests.support import (
    FORMAT_SEED,
    SEEDED_FORMAT,
    make_format,
    make_rule,
    pause_seeded_rules,
    rule,
    rules_of,
    seeded,
)
from returnables import patterns
from returnables.patterns import PatternError

CARD, DEBIT, TRANSFER, OTHER = (kind.value for kind in BankTransaction.Kind)

#: The seeded format's name, as the pages and the refusals say it.
SEEDED_NAME = "BNP Paribas (CSV)"
#: The refusals said in French, as the import shows them.
NO_OPERATION = "Aucune opération trouvée : ce fichier ne ressemble pas à un relevé bancaire exporté en CSV"
NOT_A_CSV = "Ce fichier ne se lit pas comme un CSV : ce n'est pas un relevé bancaire exporté."
ACCOUNT_TOO_SLOW = "Le motif du numéro de compte est trop lent : simplifiez-le dans le format du relevé."
ACCOUNT_TOO_LONG = (
    "Le numéro de compte lu fait plus de 40 caractères : resserrez le motif du format du relevé avec (?P<compte>…)."
)
WIDER_THAN_HEADER = (
    "Ligne plus longue que l'en-tête : un montant non entre guillemets ? "
    "Exportez avec un autre séparateur et changez celui du format du relevé"
)


def not_in(label: str) -> str:
    return f"Ce fichier n'est pas en {label} : changez l'encodage du format du relevé, ou exportez-le à nouveau."


#: The owner's bank's export, shaped as it is, every figure invented.
BNP_HEADER = '"Compte de chèques";"Compte de chèques";****0042;14/09/2026;;1 234,56'


def bnp_row(amount="-4,10", label="LIBELLE EXEMPLE", day="03/08/2026", value="03/08/2026") -> str:
    return f"{day};PAIEMENT CB;CB;{label};{value};{amount}"


def bnp(*rows, header=BNP_HEADER, encoding="utf-8", end="\n") -> bytes:
    return "".join(row + end for row in (header, *rows) if row is not None).encode(encoding)


def plain(**changes) -> SimpleNamespace:
    """Another bank's format as a form hands it over - the date, the label,
    a signed amount, « ; » and French decimals -, `changes` made."""
    fields = {
        "name": "Banque d'essai",
        "position": 0,
        "encoding": "auto",
        "delimiter": ";",
        "date_format": "dd/mm/yyyy",
        "decimal_mark": ",",
        "date_column": 1,
        "label_columns": "2",
        "amount_column": 3,
        "debit_column": None,
        "credit_column": None,
        "value_date_column": None,
        "bank_type_column": None,
        "account_pattern": "",
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def lines_of(content: bytes, fmt, rules=None) -> list[tuple]:
    """What `fmt` reads: (day, label, amount as printed by Decimal)."""
    read = parse_statement(content, rules_of() if rules is None else rules, fmt)
    return [(line.operation_date, line.label, str(line.amount)) for line in read.lines]


def refusal_of(content: bytes, fmt, rules=None) -> str:
    try:
        parse_statement(content, rules_of() if rules is None else rules, fmt)
    except ValueError as error:
        return str(error)
    raise AssertionError("the file was read")


# -- The seeded format is the old reader ----------------------------------------------------------------------------
#
# The historical reference: bank/statements.py as it read a statement before
# migration 0007 (commit 79e638c), copied as it was - its two patterns, its
# loop, its amount, its decoding, its dates and its fingerprints. The seeded
# format must read every file of the corpus below exactly as this did: the
# same account, the same lines to the character, the same fingerprints - or
# a statement imported again is imported twice.

OLD_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
OLD_ACCOUNT_RE = re.compile(r"\*{2,}\d+")


def old_parse_amount(text: str) -> Decimal:
    cleaned = (
        text.strip()
        .replace("\N{NO-BREAK SPACE}", "")
        .replace("\N{NARROW NO-BREAK SPACE}", "")
        .replace(" ", "")
        .replace(",", ".")
    )
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Montant illisible dans le relevé : {text!r}") from None


def old_decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def old_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()  # noqa: DTZ007 - copied as it was


def old_fingerprint(account: str, lines: list[dict]) -> None:
    seen: Counter = Counter()
    for line in lines:
        value_date = line["value_date"].isoformat() if line["value_date"] else ""
        key = f"{account}|{line['operation_date'].isoformat()}|{value_date}|{line['label']}|{line['amount']}"
        occurrence = seen[key]
        seen[key] += 1
        line["fingerprint"] = hashlib.sha256(f"{key}|{occurrence}".encode()).hexdigest()


def old_parse_statement(content: bytes, rules) -> tuple[str, list[dict]]:
    rows = [
        row for row in csv.reader(io.StringIO(old_decode(content)), delimiter=";") if any(cell.strip() for cell in row)
    ]
    account = ""
    lines: list[dict] = []
    for row in rows:
        if not OLD_DATE_RE.match(row[0].strip()):
            found = OLD_ACCOUNT_RE.search(";".join(row))
            if found and not lines:
                account = found.group()
            continue
        if len(row) < 6:
            raise ValueError(f"Ligne incomplète dans le relevé : {';'.join(row)[:80]}")
        label = " ".join(row[3].split())
        bank_type = row[1].strip()
        operation_date = old_date(row[0])
        described = recognition.describe(rules, label, bank_type, operation_date)
        lines.append(
            {
                "operation_date": operation_date,
                "value_date": old_date(row[4]) if OLD_DATE_RE.match(row[4].strip()) else None,
                "bank_type": bank_type,
                "label": label,
                "amount": old_parse_amount(row[5]),
                "kind": described.kind,
                "counterparty": described.counterparty,
                "card_date": described.card_date,
            }
        )
    if not lines:
        raise ValueError("Aucune opération trouvée : ce fichier ne ressemble pas à un relevé bancaire exporté en CSV.")
    if rules.refusal:
        raise ValueError(rules.refusal)
    old_fingerprint(account, lines)
    return account, lines


LINE_FIELDS = (
    "operation_date",
    "value_date",
    "bank_type",
    "label",
    "amount",
    "kind",
    "counterparty",
    "card_date",
    "fingerprint",
)


def line_tuple(fields: dict) -> tuple:
    """A line field by field, its amount as `str` spells it - digit for
    digit, as a fingerprint does."""
    return tuple(str(fields[name]) if name == "amount" else fields[name] for name in LINE_FIELDS)


def old_outcome(content: bytes) -> tuple:
    try:
        account, lines = old_parse_statement(content, seeded())
    except ValueError as error:
        return ("refused", str(error))
    except csv.Error as error:
        # Not a ValueError: the old import let it through as a 500.
        return ("crashed", str(error))
    return ("read", account, tuple(line_tuple(line) for line in lines))


def new_outcome(content: bytes, fmt=SEEDED_FORMAT) -> tuple:
    try:
        read = parse_statement(content, seeded(), fmt)
    except ValueError as error:
        return ("refused", str(error))
    return ("read", read.account, tuple(line_tuple(vars(line)) for line in read.lines))


def as_the_seeded_format_says(outcome: tuple) -> tuple:
    """What the old reader said, as the seeded format says it: the one
    sentence that changed names the format, now that there may be two."""
    if outcome == ("refused", f"{NO_OPERATION}."):
        return ("refused", f"{NO_OPERATION} (format « {SEEDED_NAME} »).")
    return outcome


def brief(outcome: tuple) -> tuple:
    """An outcome as the deliberate differences are pinned: the account and
    each line's day, label and amount - or the sentence."""
    if outcome[0] != "read":
        return outcome
    return ("read", outcome[1], tuple((line[0].isoformat(), line[3], line[4]) for line in outcome[2]))


# The corpus: BNP-shaped files, generated from a fixed seed - every shape
# the old reader read or refused, each in any order, any number of times.

CORPUS_SEED = 20261001
CORPUS_FILES = 400

HEADERS = (
    BNP_HEADER,
    '"Compte courant";****12345678;x',
    "Date;Libellé;Montant",
    "Compte **42 ouvert;;",
    "un *1 seul",
    "Comptes ****0001 et ****0002",
    "Relevé;;;;;",
)
BANK_TYPES = ("PAIEMENT CB", "PRELEVEMENT", " VIREMENT ", "VIREMENT", "VERSEMENT ESPECES", "REMISE CHEQUE", "")
SHORT_TYPES = ("FACTURE CARTE", "PRLV SEPA", "VIR SEPA RECU", "X", "")
LABELS = (
    "FACTURE CARTE DU 010726 WING SENG       PARIS   CARTE 4974XXXXXXXX1111",
    "FACTURE CARTE DU 150826 BOULANGERIE EXEMPLE PARIS CARTE   4974XXXXXXXX2222",
    "PRLV SEPA METRO FRANCE S.A.S. ECH/090726 ID EMETTEUR/FR00ZZZ000000",
    "PRLV SEPA B2B DGFIP IMPOT 0750750 ECH/250826 ID EMETTEUR/FR00ZZZ000000",
    "VIR SEPA RECU /FRM AU COMPTOIR /EID /RNF TRANSFERT 1000001 TOTAL ENCAISSE 1127.5 EUROS",
    "VIR SEPA INST EMIS /MOTIF FACTURE /BEN SCEA EXEMPLE ET FILS /REFDO 0000 /REF NOTPROVIDED",
    "ECHEANCE PRÊT 00000 00000000",
    "VERSEMENT ESPÈCES AGENCE EXEMPLE",
    "REMISE CHÈQUES 0000002",
    '"QUOTED; WITH SEMI"',
    '"ON ""QUOTE"" INSIDE"',
    'AB "CD" EF',
    "  spaced   label  ",
    "TAB\tINSIDE",
    "NBSP\N{NO-BREAK SPACE}INSIDE",
    "COMMISSION € 0,50",
    "PAIEMENT A ****9999",
    "",
)
SIGNS = ("-", "-", "", "+")
WHOLES = (
    "0",
    "4",
    "12",
    "120",
    "999",
    "007",
    "1 234",
    "12 345",
    "1\N{NO-BREAK SPACE}234",
    "1\N{NARROW NO-BREAK SPACE}234",
    "100 000",
    "1 234 567",
)
DECIMALS = (",00", ",5", ",10", ",99", "", ",1", ",05")
UNREADABLE_AMOUNTS = ("12,3x", "", "abc", "1,2,3")
BLANKS = ("", ";;;;;", "   ", " ; ; ")
FOOTERS = (";;;Solde au 31/08/2026;;1 234,56", "Fin de relevé ****7777", "Total;;;;;-12,00")


def random_day(rng: random.Random) -> str:
    year = rng.randint(2000, 2099)
    month = rng.randint(1, 12)
    day = rng.randint(1, calendar.monthrange(year, month)[1])
    return f"{day:02d}/{month:02d}/{year}"


def random_amount(rng: random.Random) -> str:
    if rng.random() < 0.03:
        return rng.choice(UNREADABLE_AMOUNTS)
    text = rng.choice(SIGNS) + rng.choice(WHOLES) + rng.choice(DECIMALS)
    return rng.choice((text, text, f" {text} "))


def random_operation(rng: random.Random) -> str:
    day = random_day(rng)
    cells = [
        rng.choice((day, day, f" {day} ")),
        rng.choice(BANK_TYPES),
        rng.choice(SHORT_TYPES),
        rng.choice(LABELS),
        rng.choice((day, day, "", "  ", random_day(rng), "N/A")),
        random_amount(rng),
    ]
    cells += rng.choice(([], [], [""], ["EXTRA"], ["", "x"]))
    if rng.random() < 0.02:
        cells = cells[: rng.randint(1, 5)]
    return ";".join(cells)


def corpus(count: int = CORPUS_FILES, seed: int = CORPUS_SEED) -> list[bytes]:
    rng = random.Random(seed)
    files = []
    for _ in range(count):
        rows = [rng.choice(HEADERS) for _ in range(rng.choice((0, 1, 1, 1, 1, 2, 3)))]
        for _ in range(rng.choice((0, 1, 2, 3, 4, 5, 6, 8) * 3 + (0,))):
            row = random_operation(rng)
            rows.append(row)
            if rng.random() < 0.2:
                rows.append(row)  # the same operation twice: two lines
            if rng.random() < 0.1:
                rows.append(rng.choice(BLANKS))
        if rng.random() < 0.3:
            rows.append(rng.choice(FOOTERS))
        text = rng.choice(("\n", "\r\n")).join(rows) + rng.choice(("\n", "\r\n", ""))
        encoding = rng.choice(("utf-8", "utf-8-sig", "cp1252"))
        try:
            files.append(text.encode(encoding))
        except UnicodeEncodeError:
            files.append(text.encode("utf-8"))
    return files


class OracleTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.corpus = corpus()

    def test_the_seeded_format_reads_every_file_as_the_old_reader_did(self):
        """The account, every line to the character - its day, its value
        date, its type, its label, its amount digit for digit, what the
        rules read on it - and its fingerprint."""
        for index, content in enumerate(self.corpus):
            with self.subTest(file=index, content=content[:160]):
                self.assertEqual(new_outcome(content), as_the_seeded_format_says(old_outcome(content)))

    def test_no_file_of_the_corpus_is_taken_for_another_kind(self):
        """`statements.sniff` reads a CSV as nothing (it has no mark of its
        own): the seeded format's path is untouched by the check that refuses
        a file of another kind."""
        self.assertEqual({statements.sniff(content) for content in self.corpus}, {None})

    def test_the_corpus_holds_every_shape_it_is_meant_to(self):
        """A corpus of files the two readers agree on because they read
        nothing would prove nothing."""
        outcomes = [old_outcome(content) for content in self.corpus]
        read = [outcome for outcome in outcomes if outcome[0] == "read"]
        refused = Counter(outcome[1].split(" : ")[0] for outcome in outcomes if outcome[0] == "refused")
        lines = [line for outcome in read for line in outcome[2]]
        self.assertGreater(len(read), 250)
        self.assertGreater(len(lines), 1000)
        for refusal in (
            "Aucune opération trouvée",
            "Ligne incomplète dans le relevé",
            "Montant illisible dans le relevé",
        ):
            with self.subTest(refusal=refusal):
                self.assertGreater(refused[refusal], 3)
        self.assertNotIn("crashed", {outcome[0] for outcome in outcomes})
        self.assertLessEqual({"", "****0042", "****12345678", "**42", "****0001"}, {outcome[1] for outcome in read})
        self.assertEqual({line[5] for line in lines}, {CARD, DEBIT, TRANSFER, OTHER})
        self.assertGreater(sum(line[1] is None for line in lines), 100)
        self.assertGreater(sum(line[1] not in (None, line[0]) for line in lines), 100)
        for label in ("QUOTED; WITH SEMI", 'ON "QUOTE" INSIDE', "spaced label", "TAB INSIDE", "NBSP INSIDE", ""):
            with self.subTest(label=label):
                self.assertIn(label, {line[3] for line in lines})
        amounts = {line[4] for line in lines}
        self.assertLessEqual({"-1234.00", "1234567.99", "-0", "0.5", "7.10"}, amounts)
        # Files read as Windows-1252, and files holding one operation twice -
        # two lines, two fingerprints.
        self.assertGreater(sum(not is_utf8(content) for content in self.corpus), 50)
        twins = [outcome[2] for outcome in read if has_twins(outcome[2])]
        self.assertGreater(len(twins), 30)
        self.assertTrue(all(len({line[8] for line in lines}) == len(lines) for lines in twins))

    def test_what_reads_otherwise_now_on_purpose(self):
        """Never in a statement of the owner's bank: each is a file the old
        reader misread, crashed on, refused in English, or read where it
        could not be sure. Pinned, so that changing one is a decision."""
        account_in_other_digits = BNP_HEADER.replace(
            "****0042", "****\N{ARABIC-INDIC DIGIT FOUR}\N{ARABIC-INDIC DIGIT TWO}"
        )
        twelve_in_other_digits = "\N{ARABIC-INDIC DIGIT ONE}\N{ARABIC-INDIC DIGIT TWO},00"
        unknown_byte = bnp(bnp_row()).replace(b"LIBELLE", b"LIBELLE\x81")
        at = unknown_byte.index(b"\x81")
        lone_returns = bnp(bnp_row(), end="\r")
        read = "2026-08-03", "LIBELLE EXEMPLE"
        cases = (
            # An amount ending on its decimal mark is no amount, not 5.
            (bnp(bnp_row("5,")), ("read", "****0042", ((*read, "5"),)), refused("Montant illisible", "'5,'")),
            # A minus printed after the amount, or as the typographic minus.
            (
                bnp(bnp_row("12,00-")),
                refused("Montant illisible", "'12,00-'"),
                ("read", "****0042", ((*read, "-12.00"),)),
            ),
            (
                bnp(bnp_row("\N{MINUS SIGN}12,00")),
                refused("Montant illisible", "'\N{MINUS SIGN}12,00'"),
                ("read", "****0042", ((*read, "-12.00"),)),
            ),
            # Thousands grouped by a point or an apostrophe.
            (
                bnp(bnp_row("1.234,56")),
                refused("Montant illisible", "'1.234,56'"),
                ("read", "****0042", ((*read, "1234.56"),)),
            ),
            (
                bnp(bnp_row("1'234,56")),
                refused("Montant illisible", repr("1'234,56")),
                ("read", "****0042", ((*read, "1234.56"),)),
            ),
            # A point beside French decimals groups thousands, three digits
            # at a time: « 1.234 » was one euro and 234 thousandths.
            (
                bnp(bnp_row("1.234")),
                ("read", "****0042", ((*read, "1.234"),)),
                ("read", "****0042", ((*read, "1234"),)),
            ),
            (bnp(bnp_row("12.5")), ("read", "****0042", ((*read, "12.5"),)), refused("Montant illisible", "'12.5'")),
            # What `Decimal` reads and no statement prints.
            (bnp(bnp_row("1e5")), ("read", "****0042", ((*read, "1E+5"),)), refused("Montant illisible", "'1e5'")),
            (bnp(bnp_row("NaN")), ("read", "****0042", ((*read, "NaN"),)), refused("Montant illisible", "'NaN'")),
            (
                bnp(bnp_row("1_000,00")),
                ("read", "****0042", ((*read, "1000.00"),)),
                refused("Montant illisible", "'1_000,00'"),
            ),
            (
                bnp(bnp_row(twelve_in_other_digits)),
                ("read", "****0042", ((*read, "12.00"),)),
                refused("Montant illisible", repr(twelve_in_other_digits)),
            ),
            # Wider than BankTransaction.amount: a line no read would survive.
            (
                bnp(bnp_row("10 000 000 000,00")),
                ("read", "****0042", ((*read, "10000000000.00"),)),
                refused("Montant illisible", "'10 000 000 000,00'"),
            ),
            # Under ten billion by a third decimal, and stored as SQLite's
            # REAL it reads back as ten billion: the same line no read, nor a
            # delete, survives. The bound is the column, before any rounding.
            (
                bnp(bnp_row("-9 999 999 999,995")),
                ("read", "****0042", ((*read, "-9999999999.995"),)),
                refused("Montant illisible", "'-9 999 999 999,995'"),
            ),
            # A day and a month printed without their zero: the row was
            # passed over in silence - a payment lost -, now read.
            (
                bnp(bnp_row(), bnp_row("-2,00", "SANS ZERO", day="3/8/2026", value="3/8/2026")),
                ("read", "****0042", ((*read, "-4.10"),)),
                ("read", "****0042", ((*read, "-4.10"), ("2026-08-03", "SANS ZERO", "-2.00"))),
            ),
            # An account wider than BankTransaction.account (40): stored, a
            # « Données » restore skipped every line of it. The seeded
            # pattern reads any run of digits behind its stars.
            (
                bnp(bnp_row(), header=f"Compte ****{'1' * 37};;"),
                ("read", f"****{'1' * 37}", ((*read, "-4.10"),)),
                ("refused", ACCOUNT_TOO_LONG),
            ),
            # A date of the layout that is no day: English, now French.
            (
                bnp(bnp_row(day="31/02/2026")),
                ("refused", "day is out of range for month"),
                refused("Date illisible", "'31/02/2026'"),
            ),
            (
                bnp(bnp_row(value="31/02/2026")),
                ("refused", "day is out of range for month"),
                refused("Date illisible", "'31/02/2026'"),
            ),
            # A day no statement holds: a year 1999 put every window out.
            (
                bnp(bnp_row(day="31/12/1999", value="31/12/1999")),
                ("read", "****0042", (("1999-12-31", "LIBELLE EXEMPLE", "-4.10"),)),
                refused("Date illisible", "'31/12/1999'"),
            ),
            # A NUL byte: no text export holds one.
            (
                bnp(bnp_row(label="LIBELLE\N{NULL}EXEMPLE")),
                ("read", "****0042", (("2026-08-03", "LIBELLE\N{NULL}EXEMPLE", "-4.10"),)),
                ("refused", NOT_A_CSV),
            ),
            # A byte Windows-1252 has no letter for: English, now French.
            (
                unknown_byte,
                ("refused", f"'charmap' codec can't decode byte 0x81 in position {at}: character maps to <undefined>"),
                ("refused", not_in("Windows-1252")),
            ),
            # A UTF-8 byte order mark before what is no UTF-8: read as
            # Windows-1252 behind « ï»¿ », now refused.
            (
                codecs.BOM_UTF8 + bnp(bnp_row()).replace(b"LIBELLE", b"LIBELLE\xe9"),
                ("read", "****0042", (("2026-08-03", "LIBELLE\N{LATIN SMALL LETTER E WITH ACUTE} EXEMPLE", "-4.10"),)),
                ("refused", not_in("UTF-8")),
            ),
            # UTF-16 behind its byte order mark: garbage, now read.
            (
                bnp(bnp_row(), encoding="utf-16"),
                ("refused", f"{NO_OPERATION}."),
                ("read", "****0042", ((*read, "-4.10"),)),
            ),
            # An account number in digits that are not ASCII.
            (
                bnp(bnp_row(), header=account_in_other_digits),
                ("read", "****\N{ARABIC-INDIC DIGIT FOUR}\N{ARABIC-INDIC DIGIT TWO}", ((*read, "-4.10"),)),
                ("read", "", ((*read, "-4.10"),)),
            ),
            # Lines ended by a lone carriage return: a 500, now French.
            (
                lone_returns,
                (
                    "crashed",
                    "new-line character seen in unquoted field - do you need to open the file with newline=''?",
                ),
                ("refused", NOT_A_CSV),
            ),
            # The sentence for a file with no operation names the format.
            (
                b"nom;prenom\nDupont;Jean\n",
                ("refused", f"{NO_OPERATION}."),
                ("refused", f"{NO_OPERATION} (format « {SEEDED_NAME} »)."),
            ),
        )
        for content, old, new in cases:
            with self.subTest(content=content[-60:]):
                self.assertEqual(brief(old_outcome(content)), old)
                self.assertEqual(brief(new_outcome(content)), new)
        # What `brief` leaves out. A value date printed without its zeros
        # was no value date - and the fingerprint was another.
        unpadded = bnp(bnp_row(value="3/8/2026"))
        self.assertIsNone(old_outcome(unpadded)[2][0][1])
        self.assertEqual(new_outcome(unpadded)[2][0][1], date(2026, 8, 3))
        self.assertEqual(new_outcome(unpadded)[2][0][8], new_outcome(bnp(bnp_row()))[2][0][8])
        # A type wider than BankTransaction.bank_type (80) is cut to it -
        # the rules read the type as it is stored.
        long_type = bnp(f"03/08/2026;{'T' * 90};CB;LIBELLE EXEMPLE;03/08/2026;-4,10")
        self.assertEqual(old_outcome(long_type)[2][0][2], "T" * 90)
        self.assertEqual(new_outcome(long_type)[2][0][2], "T" * 80)


def refused(prefix: str, shown: str) -> tuple:
    return ("refused", f"{prefix} dans le relevé : {shown}")


def is_utf8(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def has_twins(lines) -> bool:
    keys = [(line[0], line[1], line[3], line[4]) for line in lines]
    return len(set(keys)) < len(keys)


# -- The seed -------------------------------------------------------------------------------------------------------

FIELDS = (
    "name",
    "position",
    "encoding",
    "delimiter",
    "date_format",
    "decimal_mark",
    "date_column",
    "label_columns",
    "amount_column",
    "debit_column",
    "credit_column",
    "value_date_column",
    "bank_type_column",
    "account_pattern",
)


class SeedTests(TestCase):
    """Pinned with literals, like test_recognition.SeedTests: a slip in the
    migration shows here rather than being copied into
    support.SEEDED_FORMAT."""

    def test_the_owners_bank_is_seeded_as_one_format_laid_out_as_the_code_read_it(self):
        self.assertEqual(
            list(StatementFormat.objects.values_list(*FIELDS)),
            [(SEEDED_NAME, 1, "auto", ";", "dd/mm/yyyy", ",", 1, "4", 6, None, None, 5, 2, r"\*{2,}[0-9]+")],
        )

    def test_the_seeded_format_passes_the_check_counted_from_zero(self):
        stored = StatementFormat.objects.get()
        stored.full_clean()
        layout = check_format(stored)
        self.assertEqual(
            (layout.name, layout.encoding, layout.delimiter, layout.date_format, layout.decimal_mark),
            (SEEDED_NAME, "auto", ";", "dd/mm/yyyy", ","),
        )
        self.assertEqual(
            (layout.date, layout.labels, layout.amount, layout.debit, layout.credit, layout.value_date),
            (0, (3,), 5, None, None, 4),
        )
        self.assertEqual((layout.bank_type, layout.account.pattern, layout.width), (1, r"\*{2,}[0-9]+", 6))

    def test_the_tests_pure_copy_is_the_databases(self):
        """`support.SEEDED_FORMAT` (the pure tests' format) is what every
        database holds, and reads a file as the stored row does."""
        stored = StatementFormat.objects.get()
        self.assertEqual(
            [getattr(SEEDED_FORMAT, name, None) for name in FIELDS], [getattr(stored, name) for name in FIELDS]
        )
        content = bnp(bnp_row(), bnp_row("1 234,50", "PRLV SEPA EXEMPLE ECH/030826"))
        self.assertEqual(new_outcome(content, stored), new_outcome(content))

    def test_the_migration_follows_0006_and_reverses_to_nothing(self):
        migration = FORMAT_SEED.Migration
        self.assertEqual(migration.dependencies, [("bank", "0006_operation_rules")])
        create, seed = migration.operations
        self.assertIsInstance(create, migrations.CreateModel)
        self.assertEqual((create.name, create.options), ("StatementFormat", {"ordering": ["position", "name"]}))
        self.assertIs(seed.code, FORMAT_SEED.seed)
        self.assertIs(seed.reverse_code, migrations.RunPython.noop)

    def test_seeding_again_leaves_what_a_person_changed(self):
        StatementFormat.objects.filter(name=SEEDED_NAME).update(
            delimiter=",", label_columns="3, 4", account_pattern="", position=7
        )
        FORMAT_SEED.seed(apps, None)
        self.assertEqual(
            list(
                StatementFormat.objects.values_list("name", "delimiter", "label_columns", "account_pattern", "position")
            ),
            [(SEEDED_NAME, ",", "3, 4", "", 7)],
        )

    def test_formats_come_by_position_then_name(self):
        make_format("Zeta", position=0, date_column=1, label_columns="2", amount_column=3)
        make_format("Alpha", position=0, date_column=1, label_columns="2", amount_column=3)
        make_format("Banque", position=4, date_column=1, label_columns="2", amount_column=3)
        self.assertEqual(
            list(StatementFormat.objects.values_list("name", flat=True)), ["Alpha", "Zeta", SEEDED_NAME, "Banque"]
        )


class TenantTests(TwoTenantsTestCase):
    """Real espaces (accounts.provisioning): each is copied from the
    migrated _template, so each can import from its first day - a hosted one
    reading the standard files first (bank.presets.set_up_new_espace), the
    owner's bank's format kept, as seeded, after them."""

    def test_every_new_espace_is_given_the_owners_bank_format_after_the_standard_ones(self):
        for bar in (self.bar_a, self.bar_b):
            with self.subTest(bar=bar.name), bound_tenant(bar):
                self.assertEqual(
                    list(StatementFormat.objects.values_list("name", "position", "file_type")),
                    [("Relevé OFX", 1, "ofx"), ("Relevé CAMT.053", 2, "camt053"), (SEEDED_NAME, 3, "csv")],
                )
                self.assertEqual(
                    list(StatementFormat.objects.filter(name=SEEDED_NAME).values_list(*FIELDS)),
                    [(SEEDED_NAME, 3, "auto", ";", "dd/mm/yyyy", ",", 1, "4", 6, None, None, 5, 2, r"\*{2,}[0-9]+")],
                )
                self.assertEqual(reconcile.default_format().name, "Relevé OFX")


# -- The check ------------------------------------------------------------------------------------------------------


class NeverCompile:
    """Stands in for regex.compile: records the call and refuses to compile
    (returnables/tests/test_patterns.py's sentinel)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        raise AssertionError("regex.compile called on a pattern the guard had to refuse")


class CheckFormatTests(SimpleTestCase):
    def refusal(self, **changes) -> tuple[str, str]:
        with self.assertRaises(FormatError) as refused:
            check_format(plain(**changes))
        return refused.exception.field, refused.exception.message

    def test_a_format_that_can_read_a_statement_is_compiled_counted_from_zero(self):
        layout = check_format(
            plain(
                delimiter=",",
                date_format="yyyy-mm-dd",
                decimal_mark=".",
                date_column=2,
                label_columns="5, 4",
                amount_column=None,
                debit_column=7,
                credit_column=8,
                value_date_column=1,
                bank_type_column=3,
                account_pattern=r"  compte (?P<compte>[0-9]+)  ",
            )
        )
        self.assertIsInstance(layout, Layout)
        self.assertEqual(
            (layout.date, layout.labels, layout.amount, layout.debit, layout.credit, layout.value_date),
            (1, (4, 3), None, 6, 7, 0),
        )
        self.assertEqual((layout.bank_type, layout.account.pattern, layout.width), (2, "compte (?P<compte>[0-9]+)", 8))
        self.assertIsNone(check_format(plain(account_pattern="   ")).account)

    def test_the_date_column_is_required(self):
        for missing in (None, ""):
            with self.subTest(missing=missing):
                self.assertEqual(self.refusal(date_column=missing), ("date_column", "Indiquez une colonne."))

    def test_a_column_is_a_number_from_1_to_50(self):
        for field in ("date_column", "amount_column", "debit_column", "credit_column", "value_date_column"):
            for value in (0, -1, 51, 1000, True, False, "3", 3.0, Decimal(3)):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.refusal(**{field: value}), (field, "Un numéro de colonne de 1 à 50."))
        self.assertEqual(self.refusal(bank_type_column=51), ("bank_type_column", "Un numéro de colonne de 1 à 50."))
        self.assertEqual(check_format(plain(date_column=50)).date, 49)

    def test_the_label_columns_are_read_as_a_person_types_them(self):
        for typed, read in (
            ("4", [4]),
            ("3, 4", [3, 4]),
            ("3;4", [3, 4]),
            ("3 4", [3, 4]),
            ("  3 ,  4  ", [3, 4]),
            ("3,,4", [3, 4]),
            ("4, 3", [4, 3]),
            ("03", [3]),
            ("1 2 3 4 5", [1, 2, 3, 4, 5]),
            (4, [4]),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(label_columns(typed), read)

    def test_label_columns_that_are_no_columns_are_refused_on_their_field(self):
        empty = "Indiquez au moins une colonne pour le libellé."
        for typed, said in (
            ("", empty),
            ("   ", empty),
            (None, empty),
            (" , ; ", empty),
            ("x", "« x » n'est pas un numéro de colonne (de 1 à 50)."),
            ("0", "« 0 » n'est pas un numéro de colonne (de 1 à 50)."),
            ("51", "« 51 » n'est pas un numéro de colonne (de 1 à 50)."),
            ("3.5", "« 3.5 » n'est pas un numéro de colonne (de 1 à 50)."),
            ("3-4", "« 3-4 » n'est pas un numéro de colonne (de 1 à 50)."),
            ("-3", "« -3 » n'est pas un numéro de colonne (de 1 à 50)."),
            ("+3", "« +3 » n'est pas un numéro de colonne (de 1 à 50)."),
            (
                "\N{ARABIC-INDIC DIGIT THREE}",
                "« \N{ARABIC-INDIC DIGIT THREE} » n'est pas un numéro de colonne (de 1 à 50).",
            ),
            ("\N{SUPERSCRIPT TWO}", "« \N{SUPERSCRIPT TWO} » n'est pas un numéro de colonne (de 1 à 50)."),
            ("3, 3", "La colonne 3 est indiquée deux fois."),
            ("3 03", "La colonne 03 est indiquée deux fois."),
            ("1 2 3 4 5 6", "5 colonnes au plus pour le libellé."),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(
                    self.refusal(label_columns=typed, date_column=9, amount_column=10), ("label_columns", said)
                )

    def test_a_column_number_too_long_for_int_is_refused_never_a_crash(self):
        """`int` refuses a string past 4 300 digits with a ValueError of its
        own: it escaped `check_format` - and so the model's `clean`, and a
        « Données » import's - as a 500."""
        for typed in ("9" * 5000, "0" * 5000 + "3"):
            with self.subTest(digits=len(typed)):
                with self.assertRaises(FormatError) as refused:
                    label_columns(typed)
                self.assertEqual(refused.exception.field, "label_columns")
                self.assertIn("n'est pas un numéro de colonne (de 1 à 50).", refused.exception.message)

    def test_an_amount_is_one_signed_column_or_debits_and_credits(self):
        self.assertEqual(
            self.refusal(amount_column=None),
            ("amount_column", "Indiquez la colonne du montant, ou celles des débits et des crédits."),
        )
        both = "Un montant signé OU des débits et des crédits : pas les deux (laissez l'un vide)."
        for changes in ({"debit_column": 4}, {"credit_column": 4}, {"debit_column": 4, "credit_column": 5}):
            with self.subTest(changes=changes):
                self.assertEqual(self.refusal(**changes), ("amount_column", both))
        # Debits alone, credits alone, or both: each an amount.
        for changes in ({"debit_column": 3}, {"credit_column": 3}, {"debit_column": 3, "credit_column": 4}):
            with self.subTest(changes=changes):
                check_format(plain(amount_column=None, **changes))

    def test_a_column_given_two_roles_names_both(self):
        for changes, field, said in (
            ({"label_columns": "1"}, "label_columns", "La colonne 1 sert deux fois : pour la date et pour le libellé."),
            ({"amount_column": 1}, "amount_column", "La colonne 1 sert deux fois : pour la date et pour le montant."),
            (
                {"label_columns": "2, 3"},
                "amount_column",
                "La colonne 3 sert deux fois : pour le libellé et pour le montant.",
            ),
            (
                {"amount_column": None, "debit_column": 4, "credit_column": 4},
                "credit_column",
                "La colonne 4 sert deux fois : pour les débits et pour les crédits.",
            ),
            (
                {"value_date_column": 3},
                "value_date_column",
                "La colonne 3 sert deux fois : pour le montant et pour la date de valeur.",
            ),
            (
                {"value_date_column": 5, "bank_type_column": 5},
                "bank_type_column",
                "La colonne 5 sert deux fois : pour la date de valeur et pour le type d'opération.",
            ),
            (
                {"bank_type_column": 1},
                "bank_type_column",
                "La colonne 1 sert deux fois : pour la date et pour le type d'opération.",
            ),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.refusal(**changes), (field, said))

    def test_a_choice_the_model_does_not_offer_is_refused_on_its_field(self):
        for field, value, said in (
            ("encoding", "latin9", "Encodage inconnu."),
            ("encoding", None, "Encodage inconnu."),
            ("delimiter", ":", "Séparateur inconnu."),
            ("delimiter", "", "Séparateur inconnu."),
            ("date_format", "dd/mm/yyyy hh:mm", "Format de date inconnu."),
            ("date_format", "%d/%m/%Y", "Format de date inconnu."),
            ("decimal_mark", "'", "Séparateur décimal inconnu."),
            ("decimal_mark", None, "Séparateur décimal inconnu."),
        ):
            with self.subTest(field=field, value=value):
                self.assertEqual(self.refusal(**{field: value}), (field, said))
        # Every choice the model offers is one the reader knows.
        self.assertEqual(set(statements.DATE_FORMATS), set(StatementFormat.DateFormat.values))

    def test_an_account_pattern_the_guard_refuses_is_never_compiled(self):
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        never = NeverCompile()
        for pattern, reason in (
            (r"(?:x{65535}){65535}", "répétition trop grande"),
            (r"(?x)\*+ [0-9]+", "le mode (?x) n'est pas accepté dans un motif"),
            (r"(?:(?:(?:x{100,}){100,}){100,}){100,}", "répétition trop grande"),
        ):
            with self.subTest(pattern=pattern), mock.patch.object(regex, "compile", new=never):
                field, said = self.refusal(account_pattern=pattern)
                self.assertEqual(field, "account_pattern")
                self.assertTrue(said.startswith("Motif du numéro de compte : "), said)
                self.assertIn(reason, said)
        self.assertEqual(never.calls, [])

    def test_an_account_pattern_finding_an_empty_line_is_refused(self):
        self.assertEqual(
            self.refusal(account_pattern=r"[0-9]*"),
            (
                "account_pattern",
                (
                    "Motif du numéro de compte : le motif accepte une ligne vide : il trouverait quelque chose sur "
                    "n'importe quelle ligne."
                ),
            ),
        )

    def test_an_account_pattern_reads_its_compte_group_and_no_other(self):
        self.assertEqual(
            self.refusal(account_pattern=r"Compte (?P<numero>[0-9]+)"),
            (
                "account_pattern",
                "Motif du numéro de compte : le groupe (?P<numero>…) ne sert à rien ; seul (?P<compte>…) est lu.",
            ),
        )
        self.assertEqual(
            check_format(plain(account_pattern=r"Compte (?P<compte>[0-9]+)")).account.groupindex, {"compte": 1}
        )


class ModelCleanTests(TestCase):
    """The model's `clean` is `check_format`: each refusal lands on the
    field the form draws it under."""

    def refusal(self, **changes) -> dict:
        fields = {**FORMAT_SEED.FORMAT, "name": "Banque d'essai", **changes}
        with self.assertRaises(ValidationError) as refused:
            StatementFormat(**fields).full_clean()
        return refused.exception.message_dict

    def test_each_refusal_is_said_on_its_field(self):
        for changes, field, said in (
            ({"date_column": 51}, "date_column", "Un numéro de colonne de 1 à 50."),
            ({"label_columns": "3, 3"}, "label_columns", "La colonne 3 est indiquée deux fois."),
            (
                {"debit_column": 7},
                "amount_column",
                "Un montant signé OU des débits et des crédits : pas les deux (laissez l'un vide).",
            ),
            (
                {"value_date_column": 6},
                "value_date_column",
                "La colonne 6 sert deux fois : pour le montant et pour la date de valeur.",
            ),
            (
                {"account_pattern": r"\*+(?P<numero>[0-9]+)"},
                "account_pattern",
                "Motif du numéro de compte : le groupe (?P<numero>…) ne sert à rien ; seul (?P<compte>…) est lu.",
            ),
        ):
            with self.subTest(field=field):
                self.assertEqual(self.refusal(**changes), {field: [said]})

    def test_a_refusal_of_the_field_itself_keeps_the_formats_sentence_beside_it(self):
        self.assertIn("Encodage inconnu.", self.refusal(encoding="latin9")["encoding"])
        self.assertIn("Indiquez une colonne.", self.refusal(date_column=None)["date_column"])

    def test_a_label_column_too_long_for_int_is_a_refusal_never_a_crash(self):
        said = self.refusal(label_columns="9" * 5000)["label_columns"]
        self.assertTrue(any("n'est pas un numéro de colonne" in sentence for sentence in said), said)

    def test_a_format_that_can_read_a_statement_is_saved(self):
        made = make_format(
            "Banque Exemple", delimiter=",", date_column=1, label_columns="2, 3", debit_column=4, credit_column=5
        )
        self.assertEqual(check_format(made).labels, (1, 2))


# -- Amounts --------------------------------------------------------------------------------------------------------


class AmountTests(SimpleTestCase):
    def test_an_amount_is_read_digit_for_digit_by_its_decimal_mark(self):
        for text, mark, read in (
            ("1 234,56", ",", "1234.56"),
            ("-1\N{NO-BREAK SPACE}234,56", ",", "-1234.56"),
            ("1\N{NARROW NO-BREAK SPACE}234,56", ",", "1234.56"),
            ("1.234,56", ",", "1234.56"),
            ("1.234", ",", "1234"),
            ("1.234.567,89", ",", "1234567.89"),
            ("1'234,56", ",", "1234.56"),
            ("120,00", ",", "120.00"),
            (",50", ",", "0.50"),
            ("+12,00", ",", "12.00"),
            ("12,00-", ",", "-12.00"),
            ("\N{MINUS SIGN}12,00", ",", "-12.00"),
            ("- 12,00", ",", "-12.00"),
            ("1,234.56", ".", "1234.56"),
            ("1,234", ".", "1234"),
            ("-1,234,567.89", ".", "-1234567.89"),
            ("1 234.5", ".", "1234.5"),
            ("12.5", ".", "12.5"),
            ("9 999 999 999,99", ",", "9999999999.99"),
            ("-9 999 999 999,99", ",", "-9999999999.99"),
        ):
            with self.subTest(text=text, mark=mark):
                self.assertEqual(str(parse_amount(text, mark)), read)

    def test_an_amount_that_cannot_be_read_whole_is_refused_never_guessed(self):
        for text, mark in (
            ("1.23", ","),
            ("1.2345", ","),
            ("12.5", ","),
            ("1,23", "."),
            ("1,234,56", "."),
            ("1,2,3", ","),
            ("1.2.3", "."),
            ("1,234.56", ","),
            ("1.234,56", "."),
            ("12,00\N{MINUS SIGN}", ","),
            ("--12", ","),
            ("-12-", ","),
            ("+-12", ","),
            ("-", ","),
            ("", ","),
            ("  ", ","),
            ("5,", ","),
            (",", ","),
            ("12,3x", ","),
            ("1e5", ","),
            ("NaN", ","),
            ("1_000", ","),
            ("\N{ARABIC-INDIC DIGIT ONE}\N{ARABIC-INDIC DIGIT TWO}", ","),
            ("10 000 000 000,00", ","),
            ("-10 000 000 000,00", ","),
            ("12,00 €", ","),
        ):
            with self.subTest(text=text, mark=mark):
                with self.assertRaises(ValueError) as refused:
                    parse_amount(text, mark)
                self.assertEqual(str(refused.exception), f"Montant illisible dans le relevé : {text!r}")

    def test_an_amount_is_bounded_by_its_column_before_any_rounding(self):
        """`BankTransaction.amount` is (12, 2), and SQLite keeps a REAL:
        9 999 999 999,995 comes back as ten billion, which raises on every
        read of the line - a delete included. The cliff is the column."""
        column = BankTransaction._meta.get_field("amount")
        widest = Decimal(10) ** (column.max_digits - column.decimal_places) - Decimal(10) ** -column.decimal_places
        self.assertEqual(statements.MAX_AMOUNT, widest)
        self.assertEqual(statements.MAX_AMOUNT, Decimal("9999999999.99"))
        for text, mark in (
            ("-9 999 999 999,995", ","),
            ("9 999 999 999,991", ","),
            ("9 999 999 999,9901", ","),
            ("9,999,999,999.995", "."),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as refused:
                    parse_amount(text, mark)
                self.assertEqual(str(refused.exception), f"Montant illisible dans le relevé : {text!r}")
        for text, mark, read in (
            ("-9 999 999 999,99", ",", "-9999999999.99"),
            ("9 999 999 999,990", ",", "9999999999.990"),
            ("9,999,999,999.99", ".", "9999999999.99"),
        ):
            with self.subTest(text=text):
                self.assertEqual(str(parse_amount(text, mark)), read)


# -- Reading with a format, no database -----------------------------------------------------------------------------


class ReadingTests(SimpleTestCase):
    def test_a_label_over_several_columns_is_joined_in_the_order_given(self):
        fmt = plain(label_columns="4, 2", amount_column=3)
        content = b"03/08/2026;MONTPELLIER;-4,10;CB   BOULANGERIE\n04/08/2026;;-1,00;FRAIS\n05/08/2026;  ;-2,00;  \n"
        self.assertEqual(
            lines_of(content, fmt),
            [
                (date(2026, 8, 3), "CB BOULANGERIE MONTPELLIER", "-4.10"),
                (date(2026, 8, 4), "FRAIS", "-1.00"),
                (date(2026, 8, 5), "", "-2.00"),
            ],
        )

    def test_debits_and_credits_are_signed_as_money_out_and_money_in(self):
        fmt = plain(amount_column=None, debit_column=3, credit_column=4)
        content = (
            b"01/08/2026;DEBIT;12,50;\n"
            b"02/08/2026;DEBIT AVEC SON MOINS;-12,50;\n"
            b"03/08/2026;CREDIT;;300,00\n"
            b"04/08/2026;LES DEUX;10,00;2,50\n"
            b"05/08/2026;ZERO;0,00;\n"
            # |credit| - |debit|, whatever sign either is printed with.
            b"06/08/2026;CREDIT SIGNE;;-300,00\n"
            b"07/08/2026;LES DEUX DEBIT SIGNE;-10,00;2,50\n"
            b"08/08/2026;CREDIT A ZERO;-10,00;0,00\n"
        )
        self.assertEqual(
            [amount for _day, _label, amount in lines_of(content, fmt)],
            ["-12.50", "-12.50", "300.00", "-7.50", "0.00", "300.00", "-7.50", "-10.00"],
        )
        # One column alone: debits only, or credits only.
        self.assertEqual(lines_of(b"01/08/2026;X;12,50\n", plain(amount_column=None, debit_column=3))[0][2], "-12.50")
        self.assertEqual(lines_of(b"01/08/2026;X;12,50\n", plain(amount_column=None, credit_column=3))[0][2], "12.50")

    def test_each_date_format_reads_its_own_dates_and_passes_over_others(self):
        for date_format, printed in (
            ("dd/mm/yyyy", "03/08/2026"),
            ("dd/mm/yy", "03/08/26"),
            ("dd-mm-yyyy", "03-08-2026"),
            ("dd.mm.yyyy", "03.08.2026"),
            ("yyyy-mm-dd", "2026-08-03"),
            ("mm/dd/yyyy", "08/03/2026"),
        ):
            with self.subTest(date_format=date_format):
                fmt = plain(date_format=date_format, value_date_column=4)
                content = f"Date;Libellé;Montant\n{printed};X;-1,00;{printed}\n04-08-2026T00;Y;-2,00\n".encode()
                read = parse_statement(content, rules_of(), fmt)
                self.assertEqual(
                    [(line.operation_date, line.value_date, line.label) for line in read.lines],
                    [(date(2026, 8, 3), date(2026, 8, 3), "X")],
                )

    def test_a_day_and_a_month_without_their_zero_are_read_beside_padded_ones(self):
        """A US export, or a CSV re-saved by a spreadsheet: passed over, the
        rows dated « 7/3 » or « 11/2 » were payments lost in silence, while
        the rest of the file imported as if whole."""
        content = (
            b"Date,Description,Amount\n"
            b"7/3/2026,COFFEE,-12.30\n"
            b"12/15/2026,RENT,-40.00\n"
            b"11/2/2026,SALARY,2000.00\n"
            b"10/20/2026,REFUND,250.00\n"
            b"1/15/2026,SNACK,-3.40\n"
        )
        self.assertEqual(
            lines_of(content, plain(delimiter=",", date_format="mm/dd/yyyy", decimal_mark=".")),
            [
                (date(2026, 7, 3), "COFFEE", "-12.30"),
                (date(2026, 12, 15), "RENT", "-40.00"),
                (date(2026, 11, 2), "SALARY", "2000.00"),
                (date(2026, 10, 20), "REFUND", "250.00"),
                (date(2026, 1, 15), "SNACK", "-3.40"),
            ],
        )
        for date_format, printed in (
            ("dd/mm/yyyy", ("3/8/2026", "03/8/2026", "3/08/2026", "13/08/2026")),
            ("dd/mm/yy", ("3/8/26", "03/8/26", "3/08/26", "13/08/26")),
            ("dd-mm-yyyy", ("3-8-2026", "03-8-2026", "3-08-2026", "13-08-2026")),
            ("dd.mm.yyyy", ("3.8.2026", "03.8.2026", "3.08.2026", "13.08.2026")),
            ("yyyy-mm-dd", ("2026-8-3", "2026-08-3", "2026-8-03", "2026-08-13")),
            ("mm/dd/yyyy", ("8/3/2026", "08/3/2026", "8/03/2026", "08/13/2026")),
        ):
            with self.subTest(date_format=date_format):
                content = "".join(f"{day};X;-1,00\n" for day in printed).encode()
                self.assertEqual(
                    [line[0] for line in lines_of(content, plain(date_format=date_format))],
                    [date(2026, 8, 3)] * 3 + [date(2026, 8, 13)],
                )

    def test_one_day_printed_with_or_without_its_zeros_has_one_fingerprint(self):
        """The fingerprint spells the day as `isoformat` does: an export
        re-saved without the zeros is known again, not imported twice."""
        fmt = plain(value_date_column=4)
        padded = parse_statement(b"03/08/2026;X;-1,00;04/08/2026\n", rules_of(), fmt).lines
        unpadded = parse_statement(b"3/8/2026;X;-1,00;4/8/2026\n", rules_of(), fmt).lines
        self.assertEqual(unpadded[0].value_date, date(2026, 8, 4))
        self.assertEqual([line.fingerprint for line in unpadded], [line.fingerprint for line in padded])

    def test_a_day_month_file_read_month_first_is_refused_where_it_shows(self):
        """« 13/08/2026 » is no month-first date: the file is refused
        rather than read with every day before the 13th misdated."""
        content = b"03/08/2026;X;-1,00\n13/08/2026;Y;-2,00\n"
        self.assertEqual(
            refusal_of(content, plain(date_format="mm/dd/yyyy")), "Date illisible dans le relevé : '13/08/2026'"
        )
        # Without its zeros all the same.
        self.assertEqual(
            refusal_of(b"3/8/2026;X;-1,00\n13/7/2026;Y;-2,00\n", plain(date_format="mm/dd/yyyy")),
            "Date illisible dans le relevé : '13/7/2026'",
        )

    def test_a_value_date_that_is_no_date_of_the_format_is_left_empty(self):
        content = b"03/08/2026;X;-1,00;N/A\n04/08/2026;Y;-1,00;\n05/08/2026;Z;-1,00;2026-08-05\n"
        read = parse_statement(content, rules_of(), plain(value_date_column=4))
        self.assertEqual([line.value_date for line in read.lines], [None, None, None])

    def test_the_operation_type_is_read_from_its_column(self):
        content = b"03/08/2026;X;-1,00;  VERSEMENT ESPECES \n"
        (line,) = parse_statement(content, rules_of(), plain(bank_type_column=4)).lines
        self.assertEqual(line.bank_type, "VERSEMENT ESPECES")
        (line,) = parse_statement(content, rules_of(), plain()).lines
        self.assertEqual(line.bank_type, "")

    def test_a_type_wider_than_its_column_is_cut_before_the_rules_read_it(self):
        """`BankTransaction.bank_type` holds 80: a long column picked as the
        type was stored whole, and a « Données » restore skipped the line.
        Cut for the rules too: what they read is what is stored, so
        « Relire » finds what the import found."""
        self.assertEqual(statements.BANK_TYPE_MAX, BankTransaction._meta.get_field("bank_type").max_length)
        self.assertEqual(statements.BANK_TYPE_MAX, 80)
        rules = rules_of(rule("Virement", "transfer", "VIREMENT", "bank_type"))
        fmt = plain(bank_type_column=4)
        cut = "PAIEMENT " + "X" * 75 + " VIREMENT"
        (line,) = parse_statement(f"03/08/2026;X;-1,00;{cut}\n".encode(), rules, fmt).lines
        self.assertEqual((line.bank_type, line.kind), (cut[:80], OTHER))
        # Within the column it is read whole, by the rules too.
        whole = "PAIEMENT " + "X" * 60 + " VIREMENT"
        (line,) = parse_statement(f"03/08/2026;X;-1,00;{whole}\n".encode(), rules, fmt).lines
        self.assertEqual((line.bank_type, line.kind), (whole, TRANSFER))
        # Spaces the cut leaves at its end go, as the type's own do.
        spaced = "Y" * 79 + "  Z"
        (line,) = parse_statement(f"03/08/2026;X;-1,00;{spaced}\n".encode(), rules, fmt).lines
        self.assertEqual(line.bank_type, "Y" * 79)

    def test_a_pipe_separated_file_is_read(self):
        content = 'Relevé|x\n03/08/2026|"A | B"|-1 234,00\n'.encode()
        self.assertEqual(lines_of(content, plain(delimiter="|")), [(date(2026, 8, 3), "A | B", "-1234.00")])

    def test_a_row_wider_than_its_header_where_the_separator_can_be_in_an_amount(self):
        """« , » as the separator AND the decimal mark (or the thousands'):
        « -4,10 » unquoted is two cells, « -4 » and « 10 », and every
        amount lost its cents, nothing said. Every such row grows by the
        same cell, so only the header gives it away."""
        commas = b"Date,Libelle,Montant\n03/08/2026,CB A,-4,10\n04/08/2026,CB B,-1 234,56\n"
        self.assertEqual(
            refusal_of(commas, plain(delimiter=",", decimal_mark=",")),
            f"{WIDER_THAN_HEADER} - 03/08/2026,CB A,-4,10",
        )
        thousands = b"Date,Libelle,Montant\n03/08/2026,CB A,-4.10\n04/08/2026,CB B,-1,234.56\n"
        self.assertEqual(
            refusal_of(thousands, plain(delimiter=",", decimal_mark=".")),
            f"{WIDER_THAN_HEADER} - 04/08/2026,CB B,-1,234.56",
        )
        # Quoted, as a well-made export prints them: read whole.
        for decimal_mark, content in (
            (",", b'Date,Libelle,Montant\n03/08/2026,CB A,"-4,10"\n04/08/2026,CB B,"-1 234,56"\n'),
            (".", b'Date,Libelle,Montant\n03/08/2026,CB A,-4.10\n04/08/2026,CB B,"-1,234.56"\n'),
        ):
            with self.subTest(decimal_mark=decimal_mark):
                read = lines_of(content, plain(delimiter=",", decimal_mark=decimal_mark))
                self.assertEqual([amount for _day, _label, amount in read], ["-4.10", "-1234.56"])
        # Debits and credits: the debit's cents went to the credit column.
        fmt = plain(delimiter=",", decimal_mark=",", amount_column=None, debit_column=3, credit_column=4)
        content = b"Date,Libelle,Debit,Credit\n03/08/2026,CB A,12,50,\n"
        self.assertEqual(refusal_of(content, fmt), f"{WIDER_THAN_HEADER} - 03/08/2026,CB A,12,50,")

    def test_a_title_or_an_account_line_is_no_header_to_measure_a_row_by(self):
        """Only a row holding the cells the format reads is a header of
        columns: measured by a title, every row of a sound file was refused."""
        content = b"Releve du compte,FR76 0000\n03/08/2026,CB A,-4.10\n"
        self.assertEqual(
            lines_of(content, plain(delimiter=",", decimal_mark=".")), [(date(2026, 8, 3), "CB A", "-4.10")]
        )
        # Of a title and a header, the header measures.
        content = b"Releve du compte\nDate,Libelle,Montant\n03/08/2026,CB A,-4,10\n"
        self.assertEqual(refusal_of(content, plain(delimiter=",")), f"{WIDER_THAN_HEADER} - 03/08/2026,CB A,-4,10")

    def test_where_no_separator_can_be_in_an_amount_a_row_may_be_wider_than_its_header(self):
        """« ; », a tab or « | » is never printed in an amount: a row with a
        cell more than its header is read, as the owner's bank's always was."""
        for delimiter in (";", "\t", "|"):
            with self.subTest(delimiter=repr(delimiter)):
                content = delimiter.join(("Date", "Libelle", "Montant")) + "\n"
                content += delimiter.join(("03/08/2026", "CB A", "-4,10", "EXTRA", "")) + "\n"
                self.assertEqual(
                    lines_of(content.encode(), plain(delimiter=delimiter)), [(date(2026, 8, 3), "CB A", "-4.10")]
                )

    def test_a_row_cut_short_is_said_as_the_file_prints_it(self):
        self.assertEqual(
            refusal_of(b"03/08/2026\tX\n", plain(delimiter="\t")), "Ligne incomplète dans le relevé : 03/08/2026 X"
        )
        self.assertEqual(
            refusal_of(b"03/08/2026;X;-1,00\n", plain(value_date_column=4)),
            "Ligne incomplète dans le relevé : 03/08/2026;X;-1,00",
        )

    def test_a_file_with_no_operation_names_the_format_it_was_read_with(self):
        self.assertEqual(
            refusal_of(b"03/08/2026;X;-1,00\n", plain(name="Banque ISO", date_format="yyyy-mm-dd")),
            f"{NO_OPERATION} (format « Banque ISO »).",
        )
        self.assertEqual(refusal_of(b"", plain(name="")), f"{NO_OPERATION}.")

    def test_a_format_is_read_alike_as_a_row_a_namespace_or_its_layout(self):
        fields = vars(plain(label_columns="2, 4", account_pattern=r"compte (?P<compte>[0-9]+)"))
        content = b"Compte 001;;;\n03/08/2026;CB;-4,10;  PARIS  \n03/08/2026;CB;-4,10;PARIS\n"
        readings = [
            parse_statement(content, rules_of(), fmt)
            for fmt in (SimpleNamespace(**fields), StatementFormat(**fields), check_format(SimpleNamespace(**fields)))
        ]
        self.assertEqual(readings[0].account, "001")
        self.assertEqual(len({str(reading) for reading in readings}), 1)

    def test_the_account_is_read_above_the_first_operation_only(self):
        """An operation's label printing an account's shape never sets it,
        nor does a line under the operations; of two above them, the last
        - as the old reader did."""
        fmt = plain(account_pattern=r"\*{2,}[0-9]+")
        for rows, account in (
            (("03/08/2026;PAIEMENT A ****9999;-1,00",), ""),
            (("Compte ****0042;;", "03/08/2026;PAIEMENT A ****9999;-1,00", "Fin ****7777;;"), "****0042"),
            (("Compte ****0042;;", "Autre compte ****0099;;", "03/08/2026;X;-1,00"), "****0099"),
            (("Comptes ****0001 et ****0002;;", "03/08/2026;X;-1,00"), "****0001"),
        ):
            with self.subTest(rows=rows):
                content = "".join(row + "\n" for row in rows).encode()
                self.assertEqual(parse_statement(content, rules_of(), fmt).account, account)

    def test_the_account_is_its_compte_group_whatever_the_case(self):
        fmt = plain(account_pattern=r"compte n° (?P<compte>[0-9]+)")
        content = "Relevé du COMPTE N° 0001234 au 31/08/2026;;\n03/08/2026;X;-1,00\n".encode()
        self.assertEqual(parse_statement(content, rules_of(), fmt).account, "0001234")

    def test_an_account_wider_than_its_column_refuses_the_file_never_cut(self):
        """`BankTransaction.account` holds 40, and the account is in every
        fingerprint: stored longer, a « Données » restore skipped every line
        of the statement; cut, it would be no account. Refused, in French."""
        self.assertEqual(statements.ACCOUNT_MAX, BankTransaction._meta.get_field("account").max_length)
        self.assertEqual(statements.ACCOUNT_TOO_LONG, ACCOUNT_TOO_LONG)
        header = "IBAN FR00 1111 2222 3333 4444 5555 666 - Titulaire EXEMPLE;;\n"
        content = f"{header}03/08/2026;X;-1,00\n".encode()
        self.assertEqual(refusal_of(content, plain(account_pattern=r"IBAN .*")), ACCOUNT_TOO_LONG)
        # Narrowed by its group, the same header gives an account that fits.
        narrowed = plain(account_pattern=r"IBAN (?P<compte>[A-Z0-9 ]+?) -")
        self.assertEqual(parse_statement(content, rules_of(), narrowed).account, "FR00 1111 2222 3333 4444 5555 666")
        # The column exactly is read; one more is refused.
        fmt = plain(account_pattern=r"compte (?P<compte>[0-9]+)")
        for digits, refused in ((40, False), (41, True)):
            with self.subTest(digits=digits):
                content = f"Compte {'7' * digits};;\n03/08/2026;X;-1,00\n".encode()
                if refused:
                    self.assertEqual(refusal_of(content, fmt), ACCOUNT_TOO_LONG)
                else:
                    self.assertEqual(parse_statement(content, rules_of(), fmt).account, "7" * digits)
        # The last account found above the operations is the account: a
        # long one followed by one that fits is no refusal.
        either = plain(account_pattern=r"IBAN .*|compte [0-9]+")
        content = f"{header}Compte 0042;;\n03/08/2026;X;-1,00\n".encode()
        self.assertEqual(parse_statement(content, rules_of(), either).account, "Compte 0042")

    def test_an_account_pattern_out_of_time_refuses_the_file(self):
        fmt = plain(account_pattern=r"\*{2,}[0-9]+")
        with mock.patch.object(patterns, "search", side_effect=PatternError("trop lent")):
            self.assertEqual(refusal_of(b"Compte ****0042;;\n03/08/2026;X;-1,00\n", fmt), ACCOUNT_TOO_SLOW)

    def test_an_account_search_out_of_time_once_is_asked_again(self):
        """The per-match limit is the clock's, and one search can lose it to
        a busy server: asked again, as a rule's match is
        (`recognition.search`), before the pattern is blamed. Twice running
        is too slow."""
        fmt = plain(account_pattern=r"\*{2,}[0-9]+")
        content = b"Compte ****0042;;\n03/08/2026;X;-1,00\n"
        real = patterns.search

        def out_of_time(times: int):
            asked = []

            def search(pattern, text, budget):
                asked.append(text)
                if len(asked) <= times:
                    raise PatternError("trop lent")
                return real(pattern, text, budget)

            return asked, search

        asked, search = out_of_time(1)
        with mock.patch.object(patterns, "search", side_effect=search):
            self.assertEqual(parse_statement(content, rules_of(), fmt).account, "****0042")
        self.assertEqual(asked, ["Compte ****0042;;"] * 2)
        asked, search = out_of_time(2)
        with mock.patch.object(patterns, "search", side_effect=search):
            self.assertEqual(refusal_of(content, fmt), ACCOUNT_TOO_SLOW)
        self.assertEqual(asked, ["Compte ****0042;;"] * 2)

    def test_an_account_pattern_short_of_the_limit_on_row_after_row_is_too_slow_all_the_same(self):
        """Each search under the per-match limit, and still a file of
        thousands of rows read for nothing: the account's searches share
        `recognition.RULE_SECONDS` over the file, billed in the thread's
        own time (simulated here: a second a search)."""
        fmt = plain(account_pattern=r"\*{2,}[0-9]+")
        ticks = iter(range(10_000))

        def statement_of(headers: int, footers: int = 0) -> bytes:
            rows = ["Ligne ****0042;;"] * headers + ["03/08/2026;X;-1,00"] + ["Pied ****0042;;"] * footers
            return "".join(row + "\n" for row in rows).encode()

        with mock.patch("bank.statements.thread_time", side_effect=lambda: float(next(ticks))):
            self.assertEqual(parse_statement(statement_of(5), rules_of(), fmt).account, "****0042")
            # Under the operations nothing is searched any more.
            self.assertEqual(parse_statement(statement_of(1, footers=50), rules_of(), fmt).account, "****0042")
            self.assertEqual(refusal_of(statement_of(6), fmt), ACCOUNT_TOO_SLOW)


# -- Another bank, through the real import --------------------------------------------------------------------------


def stored_lines() -> list[tuple]:
    return list(
        BankTransaction.objects.order_by("operation_date", "pk").values_list(
            "operation_date", "value_date", "label", "amount", "kind", "counterparty", "card_date", "account"
        )
    )


class AnotherBankTests(TestCase):
    """A bank whose export is nothing like the owner's: commas, a header of
    column names, ISO dates, English decimals (« 1,234.56 »), debits and
    credits in two columns - one empty a row -, the label over two columns,
    a quoted cell holding the separator, a byte order mark, no account
    number. Its format and its rules are typed as a person would; the file
    goes through the real import."""

    ROWS = (
        "Date,Date de valeur,Libellé,Détail,Débit,Crédit",
        '2026-07-02,2026-07-02,CB BOULANGERIE EXEMPLE,"PARIS, CARTE 01/07",4.20,',
        '2026-07-03,2026-07-04,PRELEVEMENT FOURNISSEUR EXEMPLE,"ECHEANCE, REF 0001","1,234.56",',
        "2026-07-05,2026-07-05,VIREMENT DE ASSOCIATION EXEMPLE,,,300.00",
        '2026-07-06,,REMISE TPE 0042,COMMERCANT EXEMPLE,,"12,345.60"',
        '2026-07-08,2026-07-08,CB BOULANGERIE EXEMPLE,"PARIS, CARTE 07/07",4.20,',
        '2026-07-08,2026-07-08,CB BOULANGERIE EXEMPLE,"PARIS, CARTE 07/07",4.20,',
        ",,Solde au 31/07/2026,,,12960.04",
    )
    READ = [
        (
            date(2026, 7, 2),
            date(2026, 7, 2),
            "CB BOULANGERIE EXEMPLE PARIS, CARTE 01/07",
            Decimal("-4.20"),
            CARD,
            "BOULANGERIE EXEMPLE",
            date(2026, 7, 1),
            "",
        ),
        (
            date(2026, 7, 3),
            date(2026, 7, 4),
            "PRELEVEMENT FOURNISSEUR EXEMPLE ECHEANCE, REF 0001",
            Decimal("-1234.56"),
            DEBIT,
            "FOURNISSEUR EXEMPLE",
            None,
            "",
        ),
        (
            date(2026, 7, 5),
            date(2026, 7, 5),
            "VIREMENT DE ASSOCIATION EXEMPLE",
            Decimal("300.00"),
            TRANSFER,
            "ASSOCIATION EXEMPLE",
            None,
            "",
        ),
        (date(2026, 7, 6), None, "REMISE TPE 0042 COMMERCANT EXEMPLE", Decimal("12345.60"), OTHER, "", None, ""),
        *[
            (
                date(2026, 7, 8),
                date(2026, 7, 8),
                "CB BOULANGERIE EXEMPLE PARIS, CARTE 07/07",
                Decimal("-4.20"),
                CARD,
                "BOULANGERIE EXEMPLE",
                date(2026, 7, 7),
                "",
            )
        ]
        * 2,
    ]

    def setUp(self):
        super().setUp()
        pause_seeded_rules()
        make_rule("Carte", "card_payment", r"^CB (?P<tiers>.+?) PARIS, CARTE (?P<jour>[0-9]{2})/(?P<mois>[0-9]{2})$")
        make_rule("Prélèvement", "debit", r"^PRELEVEMENT (?P<tiers>.+?) ECHEANCE,")
        make_rule("Virement reçu", "transfer", r"^VIREMENT DE (?P<tiers>.+)$")
        self.format = make_format(
            "Banque Exemple (CSV)",
            encoding="utf-8",
            delimiter=",",
            date_format="yyyy-mm-dd",
            decimal_mark=".",
            date_column=1,
            value_date_column=2,
            label_columns="3, 4",
            debit_column=5,
            credit_column=6,
        )
        self.content = codecs.BOM_UTF8 + "\r\n".join(self.ROWS).encode()

    def test_its_statement_is_read_by_its_format_and_its_rules(self):
        summary = reconcile.import_statement(self.content, fmt=self.format)
        self.assertEqual((summary.lines, summary.created), (6, 6))
        self.assertEqual(stored_lines(), self.READ)
        self.assertEqual(set(BankTransaction.objects.values_list("bank_type", flat=True)), {""})

    def test_imported_again_every_line_is_known_again(self):
        reconcile.import_statement(self.content, fmt=self.format)
        stored = sorted(BankTransaction.objects.values_list("fingerprint", flat=True))
        again = reconcile.import_statement(self.content, fmt=self.format)
        self.assertEqual((again.lines, again.created, again.known), (6, 0, 6))
        self.assertEqual(sorted(BankTransaction.objects.values_list("fingerprint", flat=True)), stored)
        # The two identical card payments are two lines, told apart.
        self.assertEqual(len(set(stored)), 6)

    def test_a_byte_order_mark_before_the_first_operation_loses_no_line(self):
        """With no header, the mark sits in the first operation's date:
        not taken away, that operation would be passed over in silence."""
        content = codecs.BOM_UTF8 + "\n".join(self.ROWS[1:]).encode()
        for encoding in ("utf-8", "auto"):
            with self.subTest(encoding=encoding):
                StatementFormat.objects.filter(pk=self.format.pk).update(encoding=encoding)
                self.format.refresh_from_db()
                self.assertEqual(len(parse_statement(content, rules_of(), self.format).lines), 6)
        reconcile.import_statement(content, fmt=self.format)
        self.assertEqual(stored_lines(), self.READ)

    def test_read_with_the_owners_format_it_is_no_statement(self):
        with self.assertRaises(ValueError) as refused:
            reconcile.import_statement(self.content)
        self.assertEqual(str(refused.exception), f"{NO_OPERATION} (format « {SEEDED_NAME} »).")
        self.assertFalse(BankTransaction.objects.exists())


class TabSeparatedBankTests(TestCase):
    """Tabs, Windows-1252, dd.mm.yyyy, French decimals with a no-break
    space, a signed amount, and an account number read from a header line
    by its (?P<compte>…) group."""

    TEXT = (
        "Relevé du Compte n° 12345678901\t\t\t\n"
        "Date\tValeur\tLibellé\tMontant\n"
        "02.07.2026\t02.07.2026\tPRÉLÈVEMENT ÉLECTRICITÉ EXEMPLE\t-1\N{NO-BREAK SPACE}234,56\n"
        "05.07.2026\t06.07.2026\tVIREMENT REÇU DE CAFÉ EXEMPLE\t250,00\n"
        "\t\tSolde\t-984,56\n"
    )

    def setUp(self):
        super().setUp()
        pause_seeded_rules()
        make_rule("Prélèvement", "debit", r"^PRÉLÈVEMENT (?P<tiers>.+)$")
        make_rule("Virement reçu", "transfer", r"^VIREMENT REÇU DE (?P<tiers>.+)$")
        self.format = make_format(
            "Banque Tabulée",
            encoding="cp1252",
            delimiter="\t",
            date_format="dd.mm.yyyy",
            decimal_mark=",",
            date_column=1,
            value_date_column=2,
            label_columns="3",
            amount_column=4,
            account_pattern=r"compte n° (?P<compte>[0-9]{11})",
        )
        self.content = self.TEXT.encode("cp1252")

    def test_its_statement_is_read_with_its_accents_and_its_account(self):
        summary = reconcile.import_statement(self.content, fmt=self.format)
        self.assertEqual((summary.lines, summary.created), (2, 2))
        self.assertEqual(
            stored_lines(),
            [
                (
                    date(2026, 7, 2),
                    date(2026, 7, 2),
                    "PRÉLÈVEMENT ÉLECTRICITÉ EXEMPLE",
                    Decimal("-1234.56"),
                    DEBIT,
                    "ÉLECTRICITÉ EXEMPLE",
                    None,
                    "12345678901",
                ),
                (
                    date(2026, 7, 5),
                    date(2026, 7, 6),
                    "VIREMENT REÇU DE CAFÉ EXEMPLE",
                    Decimal("250.00"),
                    TRANSFER,
                    "CAFÉ EXEMPLE",
                    None,
                    "12345678901",
                ),
            ],
        )

    def test_a_fingerprint_is_still_account_day_value_label_amount_occurrence(self):
        reconcile.import_statement(self.content, fmt=self.format)
        key = "12345678901|2026-07-02|2026-07-02|PRÉLÈVEMENT ÉLECTRICITÉ EXEMPLE|-1234.56|0"
        self.assertTrue(BankTransaction.objects.filter(fingerprint=hashlib.sha256(key.encode()).hexdigest()).exists())
        again = reconcile.import_statement(self.content, fmt=self.format)
        self.assertEqual((again.created, again.known), (0, 2))


class Utf16Tests(TestCase):
    def test_a_utf16_export_behind_its_byte_order_mark_is_read_in_auto(self):
        rows = (bnp_row(), bnp_row("1 234,50", "PRLV SEPA FOURNISSEUR EXEMPLE ECH/030826 ID"))
        first = reconcile.import_statement(bnp(*rows, encoding="utf-16"))
        self.assertEqual((first.lines, first.created), (2, 2))
        # The same statement in UTF-8 is the same operations, known again.
        again = reconcile.import_statement(bnp(*rows))
        self.assertEqual((again.created, again.known), (0, 2))
        self.assertEqual(
            list(BankTransaction.objects.order_by("pk").values_list("label", "amount", "account")),
            [
                ("LIBELLE EXEMPLE", Decimal("-4.10"), "****0042"),
                ("PRLV SEPA FOURNISSEUR EXEMPLE ECH/030826 ID", Decimal("1234.50"), "****0042"),
            ],
        )


# -- Refused whole, nothing written ---------------------------------------------------------------------------------


def format_fields(**changes) -> dict:
    """`plain`'s fields as `make_format` takes them - the name apart."""
    fields = vars(plain(**changes))
    del fields["name"]
    return fields


class RefusalTests(TestCase):
    """What a format cannot read whole refuses the FILE, before anything is
    written: one line dropped, misread or half written is a payment nobody
    looks for again."""

    def refused(self, content: bytes, **changes) -> str:
        fmt = make_format("Banque d'essai", **format_fields(**changes))
        with self.assertRaises(ValueError) as refused:
            reconcile.import_statement(content, rules_of(), fmt)
        self.assertFalse(BankTransaction.objects.exists())
        fmt.delete()
        return str(refused.exception)

    def read(self, content: bytes, **changes) -> list[tuple]:
        fmt = make_format("Banque lue", **format_fields(**changes))
        reconcile.import_statement(content, rules_of(), fmt)
        read = list(BankTransaction.objects.order_by("pk").values_list("operation_date", "label", "amount"))
        BankTransaction.objects.all().delete()
        fmt.delete()
        return read

    def test_a_file_in_another_encoding_than_its_format_says(self):
        latin = "03/08/2026;CAFÉ EXEMPLE;-4,10\n".encode("cp1252")
        self.assertEqual(self.refused(latin, encoding="utf-8"), not_in("UTF-8"))
        undefined = b"03/08/2026;CAFE\x81;-4,10\n"
        self.assertEqual(self.refused(undefined, encoding="cp1252"), not_in("Windows-1252"))
        self.assertEqual(self.refused(undefined, encoding="auto"), not_in("Windows-1252"))
        cut = "03/08/2026;CAFÉ;-4,10\n".encode("utf-16")[:-1]
        self.assertEqual(self.refused(cut, encoding="utf-16"), not_in("UTF-16"))
        # Behind a UTF-8 byte order mark, « auto » never falls back on
        # Windows-1252: the file says it is UTF-8, and is a broken one.
        broken = codecs.BOM_UTF8 + b"03/08/2026;CAFE\xe9;-4,10\n"
        self.assertEqual(self.refused(broken, encoding="auto"), not_in("UTF-8"))

    def test_a_unicode_file_is_no_single_byte_file_behind_its_byte_order_mark(self):
        """Read as Windows-1252, the mark is « ï»¿ » in the first date: with
        no header, the first operation went unread, and nothing said so."""
        content = codecs.BOM_UTF8 + "03/08/2026;CAFÉ;-4,10\n04/08/2026;THÉ;-2,00\n".encode()
        self.assertEqual(self.refused(content, encoding="cp1252"), not_in("Windows-1252"))
        self.assertEqual(self.refused(content, encoding="iso-8859-1"), not_in("ISO-8859-1"))
        wide = "03/08/2026;CAFÉ;-4,10\n".encode("utf-16")
        self.assertEqual(self.refused(wide, encoding="cp1252"), not_in("Windows-1252"))
        self.assertEqual(self.refused(wide, encoding="iso-8859-1"), not_in("ISO-8859-1"))
        self.assertEqual(
            self.read(content, encoding="utf-8"),
            [(date(2026, 8, 3), "CAFÉ", Decimal("-4.10")), (date(2026, 8, 4), "THÉ", Decimal("-2.00"))],
        )

    def test_a_nul_byte_is_no_csv(self):
        """Python's csv module reads a NUL since 3.11: the label went into
        the database with it."""
        self.assertEqual(self.refused(b"03/08/2026;CAFE\x00;-4,10\n"), NOT_A_CSV)

    def test_a_cell_past_the_csv_limit_is_no_csv(self):
        content = b"03/08/2026;" + b"X" * (csv.field_size_limit() + 1) + b";-4,10\n"
        self.assertEqual(self.refused(content), NOT_A_CSV)

    def test_a_date_of_the_format_that_is_no_day(self):
        for printed in ("31/02/2026", "29/02/2027", "00/08/2026", "32/01/2026", "15/13/2026"):
            with self.subTest(printed=printed):
                self.assertEqual(
                    self.refused(f"{printed};X;-1,00\n".encode()), f"Date illisible dans le relevé : '{printed}'"
                )
        self.assertEqual(
            self.refused(b"03/08/2026;X;-1,00;31/02/2026\n", value_date_column=4),
            "Date illisible dans le relevé : '31/02/2026'",
        )

    def test_a_day_no_statement_holds(self):
        for printed, date_format in (
            ("31/12/1999", "dd/mm/yyyy"),
            ("01/01/2100", "dd/mm/yyyy"),
            ("31/12/99", "dd/mm/yy"),
            ("1999-12-31", "yyyy-mm-dd"),
        ):
            with self.subTest(printed=printed):
                self.assertEqual(
                    self.refused(f"{printed};X;-1,00\n".encode(), date_format=date_format),
                    f"Date illisible dans le relevé : '{printed}'",
                )
        self.assertEqual(
            self.read(b"01/01/2000;PREMIER;-1,00\n31/12/2099;DERNIER;-1,00\n"),
            [(date(2000, 1, 1), "PREMIER", Decimal("-1.00")), (date(2099, 12, 31), "DERNIER", Decimal("-1.00"))],
        )
        self.assertEqual(self.read(b"01/01/00;AN 2000;-1,00\n", date_format="dd/mm/yy")[0][0], date(2000, 1, 1))

    def test_an_amount_wider_than_its_column(self):
        self.assertEqual(
            self.refused(b"03/08/2026;X;10 000 000 000,00\n"),
            "Montant illisible dans le relevé : '10 000 000 000,00'",
        )
        self.assertEqual(
            self.read(b"03/08/2026;X;-9 999 999 999,99\n"), [(date(2026, 8, 3), "X", Decimal("-9999999999.99"))]
        )

    def test_an_amount_the_column_would_round_past_itself(self):
        """Under ten billion by a third decimal: stored as SQLite's REAL it
        came back as ten billion, and every read of the line raised - the
        bank page, a delete. Refused at the door; the column's widest
        figure, either sign, is imported and read back."""
        for printed in ("-9 999 999 999,995", "9 999 999 999,991"):
            with self.subTest(printed=printed):
                self.assertEqual(
                    self.refused(f"03/08/2026;X;{printed}\n".encode()),
                    f"Montant illisible dans le relevé : {printed!r}",
                )
        self.assertEqual(
            self.read(b"03/08/2026;X;-9 999 999 999,99\n04/08/2026;Y;9 999 999 999,99\n"),
            [
                (date(2026, 8, 3), "X", Decimal("-9999999999.99")),
                (date(2026, 8, 4), "Y", Decimal("9999999999.99")),
            ],
        )

    def test_the_other_mark_groups_thousands_only_three_digits_at_a_time(self):
        self.assertEqual(self.read(b"03/08/2026;X;1.234\n"), [(date(2026, 8, 3), "X", Decimal("1234"))])
        self.assertEqual(self.refused(b"03/08/2026;X;1.23\n"), "Montant illisible dans le relevé : '1.23'")
        self.assertEqual(
            self.read(b'03/08/2026;X;"1,234.50"\n', decimal_mark="."), [(date(2026, 8, 3), "X", Decimal("1234.50"))]
        )
        self.assertEqual(
            self.refused(b"03/08/2026;X;1,23\n", decimal_mark="."), "Montant illisible dans le relevé : '1,23'"
        )

    def test_two_decimal_marks(self):
        self.assertEqual(self.refused(b"03/08/2026;X;1,2,3\n"), "Montant illisible dans le relevé : '1,2,3'")
        self.assertEqual(
            self.refused(b"03/08/2026;X;1.2.3\n", decimal_mark="."), "Montant illisible dans le relevé : '1.2.3'"
        )

    def test_a_minus_behind_or_typographic_is_money_out_and_only_once(self):
        self.assertEqual(self.read(b"03/08/2026;X;12,00-\n"), [(date(2026, 8, 3), "X", Decimal("-12.00"))])
        minus = "03/08/2026;X;\N{MINUS SIGN}12,00\n".encode()
        self.assertEqual(self.read(minus), [(date(2026, 8, 3), "X", Decimal("-12.00"))])
        for printed in ("12,00\N{MINUS SIGN}", "-12,00-", "--12,00", "+-12,00", "-"):
            with self.subTest(printed=printed):
                self.assertEqual(
                    self.refused(f"03/08/2026;X;{printed}\n".encode()),
                    f"Montant illisible dans le relevé : {printed!r}",
                )

    def test_a_row_with_neither_debit_nor_credit(self):
        for row in ("03/08/2026;X;;", "03/08/2026;X;  ;  "):
            with self.subTest(row=row):
                self.assertEqual(
                    self.refused(f"{row}\n".encode(), amount_column=None, debit_column=3, credit_column=4),
                    f"Montant illisible dans le relevé : ni débit ni crédit sur {row!r}",
                )

    def test_a_row_shorter_than_the_widest_column_used(self):
        self.assertEqual(
            self.refused(b"03/08/2026;X;4,10\n", amount_column=None, debit_column=3, credit_column=4),
            "Ligne incomplète dans le relevé : 03/08/2026;X;4,10",
        )
        self.assertEqual(
            self.refused(b"03/08/2026;X;-4,10;03/08/2026\n", bank_type_column=5),
            "Ligne incomplète dans le relevé : 03/08/2026;X;-4,10;03/08/2026",
        )

    def test_a_row_wider_than_its_header_writes_nothing(self):
        content = b"Date,Libelle,Montant\n02/08/2026,CB A,-1,00\n03/08/2026,CB B,-4,10\n"
        self.assertEqual(
            self.refused(content, delimiter=",", decimal_mark=","), f"{WIDER_THAN_HEADER} - 02/08/2026,CB A,-1,00"
        )

    def test_an_account_wider_than_its_column_writes_nothing(self):
        content = b"IBAN FR00 1111 2222 3333 4444 5555 666 - Titulaire EXEMPLE;;\n03/08/2026;X;-1,00\n"
        self.assertEqual(self.refused(content, account_pattern=r"IBAN .*"), ACCOUNT_TOO_LONG)

    def test_a_type_wider_than_its_column_is_stored_as_the_column_holds_it(self):
        """Stored whole, SQLite said nothing, and the « Données » restore
        (which checks every field's width) skipped the line, its links and
        its decisions with it."""
        fmt = make_format("Banque au long type", **format_fields(bank_type_column=4))
        long_type = "Paiement par carte bancaire chez un commercant en France metropolitaine (zone euro)"
        reconcile.import_statement(f"03/08/2026;X;-1,00;{long_type}\n".encode(), rules_of(), fmt)
        line = BankTransaction.objects.get()
        self.assertEqual(line.bank_type, long_type[:80])
        line.full_clean()

    def test_one_row_refused_refuses_the_rows_read_before_it(self):
        content = b"01/08/2026;BON;-1,00\n02/08/2026;BON;-2,00\n03/08/2026;MAUVAIS;-3,0x\n"
        self.assertEqual(self.refused(content), "Montant illisible dans le relevé : '-3,0x'")

    def test_an_account_pattern_out_of_time_writes_nothing(self):
        with mock.patch.object(patterns, "search", side_effect=PatternError("trop lent")):
            self.assertEqual(
                self.refused(b"Compte ****0042;;\n03/08/2026;X;-1,00\n", account_pattern=r"\*{2,}[0-9]+"),
                ACCOUNT_TOO_SLOW,
            )


@contextmanager
def bound_variables(limit: int):
    """SQLite's cap on the variables one query binds, lowered to `limit`:
    999 is the builds' default before 3.32, 32 766 the bundled one's - a
    long statement crosses either the same way."""
    connection.ensure_connection()
    raw = connection.connection
    before = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
    try:
        yield
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, before)


class LongStatementTests(TestCase):
    """More operations than SQLite binds variables in one query: « Tester »
    counted the operations already imported with one `__in` of every
    fingerprint - an OperationalError, a 500 instead of the page."""

    def test_tester_counts_the_operations_already_imported_in_chunks(self):
        content = bnp(*(bnp_row(f"-{number},00", f"OPERATION {number}") for number in range(1, 1001)))
        self.assertEqual(reconcile.import_statement(content).created, 1000)
        fmt = StatementFormat.objects.get(name=SEEDED_NAME)
        data = {name: "" if getattr(fmt, name) is None else getattr(fmt, name) for name in ("name", *FORMAT_FIELDS)}
        data[views.RULE_ACTION] = views.TEST
        data[views.TEST_FILE] = SimpleUploadedFile("releve.csv", content, content_type="text/csv")
        with bound_variables(999):
            answer = self.client.post(reverse("bank:statement_format", args=[fmt.pk]), data)
        self.assertEqual(answer.status_code, 200)
        test = answer.context["test"]
        self.assertEqual((test.refusal, test.count, test.known), ("", 1000, 1000))


# -- Which format an import reads with ------------------------------------------------------------------------------


class NoFormatTests(TestCase):
    def test_with_no_format_an_import_is_refused_before_anything_is_read(self):
        reconcile.import_statement(bnp(bnp_row()))
        StatementFormat.objects.all().delete()
        # The rules are not even read: there is nothing to read a file with.
        with mock.patch.object(recognition, "load", side_effect=AssertionError("rules read")):
            with self.assertRaises(ValueError) as refused:
                reconcile.import_statement(bnp(bnp_row(), bnp_row("-2,00", "AUTRE")))
        self.assertEqual(str(refused.exception), "Aucun format de relevé : ajoutez-en un sur « Format du relevé ».")
        self.assertEqual(str(refused.exception), reconcile.NO_FORMAT)
        self.assertIsNone(reconcile.default_format())
        # What was imported before stays as it was.
        self.assertEqual(list(BankTransaction.objects.values_list("label", flat=True)), ["LIBELLE EXEMPLE"])

    def test_handed_a_format_it_needs_none_stored(self):
        StatementFormat.objects.all().delete()
        summary = reconcile.import_statement(bnp(bnp_row()), fmt=SEEDED_FORMAT)
        self.assertEqual(summary.created, 1)


class DefaultFormatTests(TestCase):
    #: A file only Alpha's layout reads: commas, ISO dates.
    ALPHA_FILE = b"Date,Libelle,Montant\n2026-08-03,CAFE EXEMPLE,-4.10\n"

    def make_alpha(self, position=0):
        return make_format(
            "Alpha",
            position=position,
            delimiter=",",
            date_format="yyyy-mm-dd",
            decimal_mark=".",
            date_column=1,
            label_columns="2",
            amount_column=3,
        )

    def test_alone_the_seeded_format_is_the_default(self):
        self.assertEqual(reconcile.default_format().name, SEEDED_NAME)
        self.assertEqual(reconcile.import_statement(bnp(bnp_row())).created, 1)

    def test_the_default_is_the_first_by_position_then_name(self):
        make_format("Zeta", position=0, date_column=1, label_columns="2", amount_column=3)
        self.make_alpha(position=0)
        self.assertEqual(reconcile.default_format().name, "Alpha")
        summary = reconcile.import_statement(self.ALPHA_FILE)
        self.assertEqual((summary.created, BankTransaction.objects.get().amount), (1, Decimal("-4.10")))
        StatementFormat.objects.filter(name="Alpha").update(position=5)
        self.assertEqual(reconcile.default_format().name, "Zeta")
        with self.assertRaises(ValueError) as refused:
            reconcile.import_statement(b"2026-08-04,AUTRE,-1.00\n")
        self.assertEqual(str(refused.exception), f"{NO_OPERATION} (format « Zeta »).")

    def test_a_format_handed_in_beats_the_default(self):
        alpha = self.make_alpha(position=9)
        self.assertEqual(reconcile.default_format().name, SEEDED_NAME)
        self.assertEqual(reconcile.import_statement(self.ALPHA_FILE, fmt=alpha).created, 1)
        self.assertEqual(reconcile.import_statement(self.ALPHA_FILE, fmt=check_format(alpha)).known, 1)

    def test_what_is_not_handed_in_costs_one_query_each(self):
        """The format and the rules, read once when not given - a caller
        importing several files reads them once and hands them in."""
        content = bnp(bnp_row(), bnp_row("-2,00", "AUTRE"))
        rules, fmt = recognition.load(), reconcile.default_format()
        with CaptureQueriesContext(connection) as handed:
            reconcile.import_statement(content, rules, fmt)
        BankTransaction.objects.all().delete()
        with CaptureQueriesContext(connection) as loaded:
            reconcile.import_statement(content)
        self.assertEqual(len(loaded) - len(handed), 2)


class StoredFormatRefusedTests(TestCase):
    """A stored format the check refuses now - written before the guard
    learnt to refuse its pattern, or by hand - names itself in the import's
    refusal: a field's sentence alone says nothing of where to correct it."""

    def refused(self) -> str:
        with self.assertRaises(ValueError) as refused:
            reconcile.import_statement(bnp(bnp_row()))
        self.assertFalse(BankTransaction.objects.exists())
        return str(refused.exception)

    def test_an_account_pattern_the_guard_refuses(self):
        StatementFormat.objects.update(account_pattern=r"(?x)\*+ [0-9]+")
        self.assertEqual(
            self.refused(),
            f"Import annulé : le format « {SEEDED_NAME} » est à corriger sur « Format du relevé » - "
            "Motif du numéro de compte : le mode (?x) n'est pas accepté dans un motif.",
        )

    def test_a_column_given_two_roles(self):
        StatementFormat.objects.update(date_column=4)
        self.assertEqual(
            self.refused(),
            f"Import annulé : le format « {SEEDED_NAME} » est à corriger sur « Format du relevé » - "
            "La colonne 4 sert deux fois : pour la date et pour le libellé.",
        )


# -- The kind of file (migration 0009) ------------------------------------------------------------------------------

FILE_TYPE_MIGRATION = importlib.import_module("bank.migrations.0009_statement_format_file_type")


class FileTypeMigrationTests(SimpleTestCase):
    """Schema only: the operations are what proves no stored row is touched -
    a new column whose default every existing format takes (`csv`, how it
    reads today) and three fields relaxed, never a RunPython."""

    def test_one_column_added_three_fields_relaxed_and_nothing_run(self):
        migration = FILE_TYPE_MIGRATION.Migration
        self.assertEqual(migration.dependencies, [("bank", "0008_treasury")])
        operations = migration.operations
        self.assertEqual(
            [(type(one), one.name) for one in operations],
            [
                (migrations.AddField, "file_type"),
                (migrations.AlterField, "date_column"),
                (migrations.AlterField, "encoding"),
                (migrations.AlterField, "label_columns"),
            ],
        )
        self.assertFalse(any(isinstance(one, migrations.RunPython) for one in operations))
        self.assertEqual({one.model_name for one in operations}, {"statementformat"})
        added = operations[0].field
        self.assertEqual((added.default, added.null, added.blank), ("csv", False, False))
        self.assertEqual([value for value, _label in added.choices], ["csv", "ofx", "camt053"])
        # Relaxed, never tightened: a row stored before reads as before.
        self.assertTrue(operations[1].field.null and operations[1].field.blank)
        self.assertTrue(operations[3].field.blank)
        self.assertEqual(operations[2].field.default, "auto")
        self.assertIn("Going back fails", FILE_TYPE_MIGRATION.__doc__)


class StoredFileTypeTests(TestCase):
    def test_the_seeded_format_is_a_csv(self):
        stored = StatementFormat.objects.get(name=SEEDED_NAME)
        self.assertEqual(stored.file_type, "csv")
        self.assertEqual(check_format(stored).file_type, "csv")

    def test_a_file_that_says_where_each_datum_is_needs_no_column(self):
        made = make_format(
            "Relevé structuré",
            file_type="ofx",
            date_column=None,
            label_columns="",
            account_pattern="",
        )
        made.full_clean()
        self.assertEqual(check_format(made).file_type, "ofx")


class FileTypeCheckTests(SimpleTestCase):
    def test_a_format_saying_no_kind_is_a_csv(self):
        self.assertEqual(statements.file_type_of(plain()), "csv")
        self.assertEqual(statements.file_type_of(plain(file_type="")), "csv")
        self.assertEqual(check_format(plain()).file_type, "csv")

    def test_ofx_and_camt_compile_with_no_column_nor_account_pattern(self):
        for file_type in ("ofx", "camt053"):
            with self.subTest(file_type=file_type):
                layout = check_format(
                    plain(
                        file_type=file_type,
                        date_column=None,
                        label_columns="",
                        amount_column=None,
                        account_pattern="",
                    )
                )
                self.assertEqual(layout.file_type, file_type)
                self.assertEqual(
                    (layout.date, layout.labels, layout.amount, layout.account, layout.width), (None, (), None, None, 0)
                )

    def test_what_such_a_format_holds_in_its_columns_is_not_read(self):
        """Never compiled either: the account pattern of an OFX format is
        none of the file's business."""
        never = NeverCompile()
        with mock.patch.object(regex, "compile", never):
            layout = check_format(plain(file_type="ofx", label_columns="1", account_pattern="(?x)x{6 5}"))
        self.assertEqual((layout.date, layout.labels, layout.account), (None, (), None))
        self.assertEqual(never.calls, [])

    def test_a_kind_of_file_the_model_does_not_offer_is_refused_on_its_field(self):
        for value in ("pdf", "qif", "CSV", "mt940"):
            with self.subTest(value=value):
                with self.assertRaises(FormatError) as refused:
                    check_format(plain(file_type=value))
                self.assertEqual(
                    (refused.exception.field, refused.exception.message), ("file_type", "Type de fichier inconnu.")
                )

    def test_a_csv_still_needs_its_date_column_and_its_label(self):
        with self.assertRaises(FormatError) as refused:
            check_format(plain(file_type="csv", date_column=None))
        self.assertEqual((refused.exception.field, refused.exception.message), ("date_column", "Indiquez une colonne."))
        with self.assertRaises(FormatError) as refused:
            check_format(plain(file_type="csv", label_columns=""))
        self.assertEqual(refused.exception.field, "label_columns")
