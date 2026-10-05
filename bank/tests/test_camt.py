"""Reading a CAMT.053 statement (bank/camt.py), versions .02 and .08 alike,
through the one pipeline every statement goes through
(`statements.parse_statement`, `statements.finish`).

The XML comes from outside: every guard is pinned with the parser made a
sentinel where it must not run (a DOCTYPE, a wide or unknown encoding), the
bounds (size, elements, depth) and the memory it keeps (every finished
element let go of) are measured, and mutated files are run through it -
whatever the bytes, a refusal is one of the reader's French sentences.

Fixtures: bank/tests/camt_files.py (the standard's structure, invented data).
"""

from __future__ import annotations

import random
import time
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree

from django.test import SimpleTestCase, TestCase

from bank import camt, reconcile, statements
from bank.models import BankTransaction
from bank.statements import parse_statement
from bank.tests.camt_files import ENTRIES, IBAN, V02, Detail, changed, statement_block, v02, v08
from bank.tests.camt_files import camt as camt_file
from bank.tests.support import SEEDED_FORMAT, make_format, rule, rules_of

CAMT_FORMAT = SimpleNamespace(
    name="Relevé CAMT.053",
    file_type="camt053",
    encoding="auto",
    delimiter=";",
    date_format="dd/mm/yyyy",
    decimal_mark=",",
    date_column=None,
    label_columns="",
    amount_column=None,
    debit_column=None,
    credit_column=None,
    value_date_column=None,
    bank_type_column=None,
    account_pattern="",
)


def ofx_format() -> SimpleNamespace:
    return SimpleNamespace(**{**vars(CAMT_FORMAT), "name": "Relevé OFX", "file_type": "ofx"})


def read(content: bytes, rules=None):
    return parse_statement(content, rules_of() if rules is None else rules, CAMT_FORMAT)


def refusal(content: bytes) -> str:
    try:
        read(content)
    except ValueError as error:
        return str(error)
    raise AssertionError("the file was read")


def brief(statement) -> list[tuple]:
    return [(line.operation_date, line.value_date, line.bank_type, line.label, line.amount) for line in statement.lines]


READ = [
    (date(2026, 8, 3), date(2026, 8, 3), "PMNT/CCRD/POSD", "CB EPICERIE EXEMPLE 01/08", Decimal("-4.10")),
    (date(2026, 8, 3), date(2026, 8, 3), "PMNT/CCRD/POSD", "CB EPICERIE EXEMPLE 01/08", Decimal("-4.10")),
    (
        date(2026, 8, 9),
        date(2026, 8, 9),
        "PMNT/RDDT/ESDD",
        "PRLV SEPA FOURNISSEUR EXEMPLE ECH/090826",
        Decimal("-120.35"),
    ),
    (date(2026, 8, 5), date(2026, 8, 6), "PMNT/RCDT/ESCT", "VIR SEPA RECU CLIENT EXEMPLE", Decimal("250.00")),
    (
        date(2026, 8, 20),
        date(2026, 8, 20),
        "PMNT/ICDT/ESCT",
        "SALARIE UN EXEMPLE SALAIRE AOUT SALARIE DEUX EXEMPLE SALAIRE AOUT",
        Decimal("-1500.00"),
    ),
    (date(2026, 8, 31), None, "ACMT/MDOP/CHRG FRAIS", "FRAIS TENUE DE COMPTE CAFÉ & FILS", Decimal("-8.50")),
]


class NeverParse:
    """Stands in for the parser where a guard must refuse first."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("the XML was parsed")


class ReadingTests(SimpleTestCase):
    def test_a_02_statement_is_read_where_the_standard_puts_each_datum(self):
        statement = read(v02())
        self.assertEqual(statement.account, IBAN)
        self.assertEqual(brief(statement), READ)
        self.assertEqual(str(statement.lines[3].amount), "250.00")

    def test_a_08_statement_reads_the_same_lines_and_fingerprints(self):
        self.assertEqual(brief(read(v08())), READ)
        self.assertEqual(
            [line.fingerprint for line in read(v02()).lines], [line.fingerprint for line in read(v08()).lines]
        )
        # Two identical entries are two lines, two fingerprints.
        self.assertEqual(len({line.fingerprint for line in read(v02()).lines}), len(ENTRIES))

    def test_a_batch_is_one_line_its_counterparties_and_remittances_its_label_when_the_bank_says_none(self):
        statement = read(v08(ENTRIES[4:5]))
        self.assertEqual(len(statement.lines), 1)
        self.assertEqual(statement.lines[0].label, "SALARIE UN EXEMPLE SALAIRE AOUT SALARIE DEUX EXEMPLE SALAIRE AOUT")
        # A credit names who paid it.
        credit = changed(ENTRIES[3], information=None)
        self.assertEqual(read(v02((credit,))).lines[0].label, "CLIENT EXEMPLE FACTURE 2026-071")

    def test_a_statement_a_day_of_one_account_is_one_file(self):
        content = camt_file(
            statement_block(ENTRIES[:1], number=1), statement_block(ENTRIES[2:3], number=2), version=V02
        )
        self.assertEqual(len(read(content).lines), 2)

    def test_the_other_account_number_is_read_without_an_iban(self):
        content = v02(ENTRIES[:1]).replace(f"<IBAN>{IBAN}</IBAN>".encode(), b"<Othr><Id>00012345678</Id></Othr>")
        self.assertEqual(read(content).account, "00012345678")

    def test_windows_1252_and_iso_8859_15_are_read(self):
        for encoding in ("windows-1252", "ISO-8859-15"):
            with self.subTest(encoding=encoding):
                content = camt_file(statement_block(ENTRIES[5:6]), encoding=encoding)
                self.assertEqual(read(content).lines[0].label, "FRAIS TENUE DE COMPTE CAFÉ & FILS")

    def test_the_rules_read_the_iso_code(self):
        rules = rules_of(
            rule("Carte (ISO : PMNT/CCRD/POSD)", "card_payment", r"^PMNT/CCRD/POSD\b", searched="bank_type")
        )
        self.assertEqual(
            [line.kind for line in read(v02(), rules).lines], ["CARD", "CARD", "OTHER", "OTHER", "OTHER", "OTHER"]
        )

    def test_the_type_is_cut_to_its_column(self):
        line = read(v02((changed(proprietary="X" * 200),))).lines[0]
        self.assertEqual(len(line.bank_type), statements.BANK_TYPE_MAX)
        self.assertTrue(line.bank_type.startswith("PMNT/CCRD/POSD XXX"))

    def test_zeros_past_the_cents_change_nothing(self):
        """The standard allows five decimals; the euro has two."""
        self.assertEqual(str(read(v02((changed(amount="8.50000"),))).lines[0].amount), "-8.50000")
        self.assertEqual(str(read(v02((changed(amount="8.5"),))).lines[0].amount), "-8.5")

    def test_the_label_is_cut_to_what_the_rules_read(self):
        """A batch's label joins every detail's payee and remittance - a
        payroll of fifty is past `LABEL_MAX`, and is read, cut; so is an
        `AddtlNtryInf` of a megabyte."""
        payroll = changed(
            ENTRIES[4],
            details=tuple(
                Detail(creditor=f"SALARIE {number:02d} EXEMPLE", remittance=("SALAIRE AOUT",)) for number in range(50)
            ),
        )
        (line,) = read(v02((payroll,))).lines
        self.assertEqual(len(line.label), statements.LABEL_MAX - 1)
        self.assertTrue(line.label.startswith("SALARIE 00 EXEMPLE SALAIRE AOUT SALARIE 01 EXEMPLE"))
        (line,) = read(v02((changed(information="I" * 1_000_000),))).lines
        self.assertEqual(line.label, "I" * statements.LABEL_MAX)


class GuardTests(SimpleTestCase):
    """Refused before any parser sees the file."""

    def assertRefusedUnparsed(self, content: bytes, said: str):
        with mock.patch.object(ElementTree, "XMLPullParser", NeverParse):
            self.assertEqual(refusal(content), said)

    def test_a_doctype_or_an_entity_the_billion_laughs_included(self):
        entities = [b'<!ENTITY lol0 "lol">'] + [
            b'<!ENTITY lol%d "%s">' % (number, (b"&lol%d;" % (number - 1)) * 10) for number in range(1, 10)
        ]
        doctype = b"<!DOCTYPE Document [" + b"".join(entities) + b"]>\n"
        content = v02().replace(b"<Document", doctype + b"<Document", 1).replace(b"CB EPICERIE", b"&lol9;", 1)
        self.assertRefusedUnparsed(content, camt.UNSAFE_XML)
        entity = v02().replace(b"<Document", b'<!ENTITY x "y"><Document', 1)
        self.assertRefusedUnparsed(entity, camt.UNSAFE_XML)

    def test_a_wide_encoding(self):
        utf16 = v02().decode().replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
        self.assertRefusedUnparsed(utf16, camt.WIDE_XML)
        self.assertRefusedUnparsed(v02().decode().encode("utf-16-be"), camt.WIDE_XML)

    def test_a_declared_encoding_off_the_list(self):
        for encoding in ("shift_jis", "euc-jp", "big5", "UTF-16", "x-unknown", "utf8", "a" * 300):
            with self.subTest(encoding=encoding):
                content = v02().replace(b'encoding="UTF-8"', f'encoding="{encoding}"'.encode())
                self.assertRefusedUnparsed(
                    content, f"{camt.REFUSED_ENCODING} « {encoding[:80]} » : il est refusé sans être lu."
                )

    def test_a_file_too_heavy(self):
        with mock.patch.object(statements, "STRUCTURED_MAX_BYTES", 1000):
            self.assertRefusedUnparsed(v02(), "Ce relevé dépasse 1 Ko : exportez une période plus courte.")

    def test_a_flood_of_attributes(self):
        """expat builds all of an element's attributes at once - 700 000 on
        the root held 226 MB - and `MAX_ELEMENTS` counts elements: every
        attribute has its « = », counted on the bytes first."""
        flood = " ".join(f'a{number}=""' for number in range(camt.MAX_ATTRIBUTES + 1)).encode()
        content = v02().replace(b"<Document", b"<Document " + flood, 1)
        self.assertLess(len(content), statements.STRUCTURED_MAX_BYTES)
        self.assertRefusedUnparsed(content, camt.TOO_MANY_ATTRIBUTES)
        # A statement's own attributes - its amounts' currencies - are far
        # from it.
        self.assertLess(v02().count(b"="), 100)


class RefusalTests(SimpleTestCase):
    def test_what_the_parser_raises_is_said_in_french(self):
        """ParseError, the ValueError of a multi-byte encoding and the
        LookupError of an unknown one - each reached here only through a
        parser the guard did not stop, and each BROKEN_XML."""
        for raised in (ElementTree.ParseError("syntax error"), ValueError("multi-byte encodings"), LookupError("x")):
            with self.subTest(raised=type(raised).__name__):

                class Raising:
                    def __init__(self, *args, raised=raised, **kwargs):
                        self.raised = raised

                    def feed(self, data):
                        raise self.raised

                with mock.patch.object(ElementTree, "XMLPullParser", Raising):
                    self.assertEqual(refusal(v02()), camt.BROKEN_XML)
        self.assertEqual(refusal(v02()[:-40]), camt.BROKEN_XML)
        self.assertEqual(refusal(b"<<>>"), camt.BROKEN_XML)

    def test_a_file_that_is_no_xml_at_all_points_to_a_csv_format(self):
        """Most often a CSV, the usual export, under a CAMT.053 format: told
        where a CSV is read, before any parser sees it."""
        said = (
            "Ce fichier n'est pas un relevé CAMT.053 (XML ISO 20022) : un relevé exporté en CSV se lit avec un "
            "format CSV - choisissez-en un à l'import, ou ajoutez-en un sur « Format du relevé » (« Partir d'un "
            "modèle »)."
        )
        for content in (b"", b"Date;Montant\n03/08/2026;-4,10\n", b"\xef\xbb\xbfDate;Montant\n", b"  \n"):
            with self.subTest(content=content):
                with mock.patch.object(ElementTree, "XMLPullParser", NeverParse):
                    self.assertEqual(refusal(content), said)
        # XML behind a byte order mark is still XML.
        self.assertEqual(len(read(b"\xef\xbb\xbf" + v02()).lines), len(ENTRIES))

    def test_another_camt_message_or_another_xml(self):
        report = v02().replace(b"camt.053.001.02", b"camt.052.001.02")
        self.assertEqual(
            refusal(report),
            "Ce fichier est un message camt.052 : seul le relevé de fin de journée (camt.053) s'importe ; "
            "exportez le relevé de compte.",
        )
        notifications = v02().replace(b"camt.053.001.02", b"camt.054.001.08")
        self.assertTrue(refusal(notifications).startswith("Ce fichier est un message camt.054 :"))
        # Whatever the format it is imported with: called a CAMT.053 by the
        # sniffing, it was sent to a CAMT.053 format that then refused it as
        # a camt.052 - two sentences saying two things.
        for fmt in (CAMT_FORMAT, ofx_format(), SEEDED_FORMAT):
            with self.subTest(format=fmt.name):
                try:
                    parse_statement(report, rules_of(), fmt)
                except ValueError as error:
                    self.assertEqual(str(error), statements.other_camt("camt.052"))
                else:
                    raise AssertionError("the file was read")
        invoice = b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
        self.assertEqual(refusal(invoice), camt.NOT_CAMT)
        self.assertEqual(
            refusal(v02().replace(b"<Document", b"<Doc", 1).replace(b"</Document>", b"</Doc>")), camt.NOT_CAMT
        )

    def test_only_euros(self):
        self.assertEqual(
            refusal(v02((changed(currency="USD"),))), "Ce relevé est en USD : seuls les relevés en euros s'importent."
        )
        self.assertEqual(refusal(v02(currency="CHF")), "Ce relevé est en CHF : seuls les relevés en euros s'importent.")
        self.assertEqual(refusal(v02((changed(currency=None),))), camt.NO_CURRENCY)

    def test_only_booked_entries(self):
        for version in (v02, v08):
            for status in ("PDNG", "INFO"):
                with self.subTest(version=version.__name__, status=status):
                    self.assertEqual(
                        refusal(version((ENTRIES[0], changed(status=status)))),
                        f"Ce relevé contient des opérations non comptabilisées (« {status} ») : "
                        "exportez le relevé de fin de journée.",
                    )

    def test_two_accounts(self):
        content = camt_file(
            statement_block(ENTRIES[:1]), statement_block(ENTRIES[:1], iban="FR7600000000000000000000000")
        )
        self.assertEqual(refusal(content), "Ce relevé contient plusieurs comptes (2) : exportez-les un par un.")

    def test_amounts_dates_and_directions_read_whole(self):
        for entry, said in (
            (changed(amount="1,234.56"), "Montant illisible dans le relevé : '1,234.56'"),
            (changed(amount="-4.10"), "Montant illisible dans le relevé : '-4.10'"),
            (changed(amount="4.123456"), "Montant illisible dans le relevé : '4.123456'"),
            # A third decimal is no amount in euros: the column rounded
            # « 8.505 » while the fingerprint kept it (review, 04/10/2026).
            (changed(amount="8.505"), "Montant illisible dans le relevé : '8.505'"),
            (changed(amount="8.50001"), "Montant illisible dans le relevé : '8.50001'"),
            (changed(amount="8.500000"), "Montant illisible dans le relevé : '8.500000'"),
            (changed(amount="10000000000.00"), "Montant illisible dans le relevé : '10000000000.00'"),
            (changed(direction="DEBIT"), "Sens de l'opération illisible dans le relevé : 'DEBIT'"),
            (changed(booked="2026-02-30"), "Date illisible dans le relevé : '2026-02-30'"),
            (changed(booked="1999-12-31"), "Date illisible dans le relevé : '1999-12-31'"),
            (changed(booked="03/08/2026"), "Date illisible dans le relevé : '03/08/2026'"),
            (changed(value="2026-13-01"), "Date illisible dans le relevé : '2026-13-01'"),
            (
                changed(booked=None, booked_at=None),
                "Opération incomplète dans le relevé CAMT.053 (sans date de comptabilisation) : exportez-le à nouveau.",
            ),
        ):
            with self.subTest(said=said):
                self.assertEqual(refusal(v02((entry,))), said)

    def test_a_statement_with_no_entry_names_its_format(self):
        self.assertEqual(
            refusal(v02(())), "Aucune opération trouvée dans ce relevé CAMT.053 (format « Relevé CAMT.053 »)."
        )

    def test_an_account_wider_than_its_column(self):
        """Its own sentence: a CAMT.053 format has no account pattern to
        tighten, which the CSV's sentence tells a person to do."""
        self.assertEqual(refusal(v02(iban="F" * 41)), camt.ACCOUNT_TOO_LONG)
        self.assertNotIn("(?P<compte>", camt.ACCOUNT_TOO_LONG)

    def test_too_many_elements_or_too_deep(self):
        with mock.patch.object(camt, "MAX_ELEMENTS", 50):
            self.assertEqual(refusal(v02()), camt.TOO_MANY_ELEMENTS)
        deep = v02().replace(b"<AddtlNtryInf>", b"<X>" * 100 + b"<AddtlNtryInf>", 1)
        self.assertEqual(refusal(deep), camt.NOT_CAMT)


class MemoryTests(SimpleTestCase):
    def test_every_finished_element_is_let_go_of(self):
        """Without it, the tree of a whole file stays in memory while it is
        read: a statement of ten thousand entries would hold every one."""
        entries = [changed(reference=f"R{number:05d}", amount=f"{number}.00") for number in range(1, 3001)]
        layout = statements.check_format(CAMT_FORMAT)
        reading = camt.CamtReading(v02(entries), layout)
        widest = read_lines = 0
        for _line in reading.lines():
            read_lines += 1
            widest = max(widest, sum(1 for _element in reading.root.iter()))
        self.assertEqual(read_lines, 3000)
        # What one chunk of the parser holds ahead at most, never the whole
        # file - the root, its statement and the entry being read.
        self.assertLess(widest, 300)
        self.assertEqual(len(reading.root), 0)

    def test_a_finished_element_leaves_its_parent_empty(self):
        """Once an element ends, its parent holds no child: those before it
        were let go of, and those after it - built ahead by the parser - are
        still reached through their events. Taken out one `remove` at a
        time, every sibling the chunk built ahead was moved for each: a
        flood of them inside one entry took twenty seconds."""
        real = camt._events
        left = []

        def watching(content):
            stack = []
            for event, element in real(content):
                if event == "start":
                    stack.append(element)
                else:
                    stack.pop()
                yield event, element
                # Asked for the next event: the reader has handled this one,
                # and the parser has built nothing more yet.
                if event == "end" and stack:
                    left.append(len(stack[-1]))

        content = v02(ENTRIES[:1]).replace(b"<AddtlNtryInf>", b"<a/>" * 5_000 + b"<AddtlNtryInf>", 1)
        with mock.patch.object(camt, "_events", watching):
            self.assertEqual(len(read(content).lines), 1)
        self.assertGreater(len(left), 5_000)
        self.assertEqual(max(left), 0)

    def test_a_flood_of_siblings_is_read_in_seconds(self):
        """Nearly a million empty elements inside one entry - 4 Mo, under
        `MAX_ELEMENTS`: twenty-two seconds of a request thread when each was
        taken out of its parent one at a time."""
        content = v02(ENTRIES[:1]).replace(b"<AddtlNtryInf>", b"<a/>" * 990_000 + b"<AddtlNtryInf>", 1)
        self.assertLess(len(content), statements.STRUCTURED_MAX_BYTES)
        # This process's CPU time, not the wall clock: a machine loaded by
        # the suite's --parallel stretched the wall clock past the bound
        # (11.4 s once) for a read that takes two.
        started = time.process_time()
        self.assertEqual(len(read(content).lines), 1)
        self.assertLess(time.process_time() - started, 10)

    def test_ten_thousand_entries_are_read_in_seconds(self):
        entries = [
            changed(reference=f"R{number:05d}", amount=f"{number % 997}.{number % 100:02d}", details=())
            for number in range(10_000)
        ]
        content = v02(entries)
        self.assertLess(len(content), statements.STRUCTURED_MAX_BYTES)
        started = time.process_time()
        self.assertEqual(len(read(content).lines), 10_000)
        self.assertLess(time.process_time() - started, 20)


FRENCH = (*camt.REFUSALS, *statements.REFUSALS)
MUTATIONS = ("<", ">", "/", "&", "#", ";", "0", "9", ".", "-", "\x00", "é", "<!--", "&#0;", "&lt;", "]]>", '"', "=")


class MutationTests(SimpleTestCase):
    def test_every_mutated_file_is_read_or_refused_in_french(self):
        generator = random.Random(20261004)
        seeds = [v02(), v08(), v02(ENTRIES[4:6])]
        refused = read_whole = 0
        for _ in range(1500):
            content = bytearray(generator.choice(seeds))
            for _change in range(generator.randint(1, 3)):
                at = generator.randrange(len(content))
                action = generator.random()
                if action < 0.3:
                    del content[at : at + generator.randint(1, 12)]
                elif action < 0.85:
                    content[at:at] = generator.choice(MUTATIONS).encode()
                else:
                    content = content[:at]
                    break
            try:
                read(bytes(content))
            except ValueError as error:
                said = str(error)
                self.assertTrue(said.startswith(FRENCH), said)
                refused += 1
            else:
                read_whole += 1
        self.assertGreater(refused, 300)
        self.assertGreater(read_whole, 50)


class ImportTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fmt = make_format(
            "Relevé CAMT.053", file_type="camt053", date_column=None, label_columns="", account_pattern=""
        )

    def test_imported_then_imported_again(self):
        first = reconcile.import_statement(v02(), rules_of(), self.fmt)
        self.assertEqual((first.lines, first.created), (len(ENTRIES), len(ENTRIES)))
        self.assertEqual(set(BankTransaction.objects.values_list("account", flat=True)), {IBAN})
        again = reconcile.import_statement(v08(), rules_of(), self.fmt)
        self.assertEqual((again.lines, again.created), (len(ENTRIES), 0))

    def test_a_refused_file_writes_nothing(self):
        with self.assertRaises(ValueError):
            reconcile.import_statement(v02((*ENTRIES, changed(status="PDNG"))), rules_of(), self.fmt)
        self.assertFalse(BankTransaction.objects.exists())


class FixtureTests(SimpleTestCase):
    def test_the_fixtures_follow_the_standard_s_shapes(self):
        self.assertIn(f'<Document xmlns="{V02}"'.encode(), v02())
        self.assertIn(b"<Sts>BOOK</Sts>", v02())
        self.assertIn(b"<Sts><Cd>BOOK</Cd></Sts>", v08())
        self.assertIn(b"<Cdtr>\n<Pty><Nm>EPICERIE EXEMPLE</Nm></Pty>\n</Cdtr>", v08())
        self.assertIn(b"<Cdtr>\n<Nm>EPICERIE EXEMPLE</Nm>\n</Cdtr>", v02())
        ElementTree.fromstring(v02())
        ElementTree.fromstring(v08())
        self.assertEqual(Detail().remittance, ())
