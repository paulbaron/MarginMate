"""One reading pipeline for every kind of statement (bank/statements.py):
a reader yields each operation as printed (`RawLine`), `parse_statement`
asks the rules what each is, and `finish` - the one choke point - refuses
the file or gives each line its fingerprint.

The CSV path is the old one to the byte (test_statement_formats.OracleTests
replays it over 400 files); what is pinned here is what the pipeline adds:
the order `finish` refuses in, the bounds a file is held to (operations,
rows, the text a refusal echoes), a layout with no column, and a CSV read
row by row rather than as a list of every row.

Every label, account number and amount below is invented.
"""

from __future__ import annotations

import tracemalloc
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from bank import statements
from bank.statements import RawLine, StatementLine, finish, parse_amount, parse_statement
from bank.tests.support import SEEDED_FORMAT, rule, rules_of

HEADER = "Compte;****0042;;;;\n"


def row(day="03/08/2026", label="LIBELLE EXEMPLE", amount="-4,10") -> str:
    return f"{day};PAIEMENT CB;CB;{label};{day};{amount}\n"


def bnp(*rows: str) -> bytes:
    return (HEADER + "".join(rows)).encode()


def line(amount="-4.10") -> StatementLine:
    return StatementLine(
        operation_date=date(2026, 8, 3),
        value_date=None,
        bank_type="",
        label="LIBELLE EXEMPLE",
        amount=Decimal(amount),
        kind="OTHER",
        counterparty="",
        card_date=None,
    )


class FinishTests(SimpleTestCase):
    """`finish` refuses in the order the CSV reader always did: no operation,
    then an account wider than its column, then a rule that could not be
    applied - and only then fingerprints."""

    def refusing_rules(self):
        return SimpleNamespace(
            refusal="Import annulé : la règle de reconnaissance « Essai » ne peut pas être appliquée."
        )

    def test_no_operation_comes_first(self):
        with self.assertRaisesMessage(ValueError, "Aucune opération (essai)"):
            finish("9" * 41, [], self.refusing_rules(), "Aucune opération (essai)")

    def test_then_an_account_too_long(self):
        with self.assertRaisesMessage(ValueError, statements.ACCOUNT_TOO_LONG):
            finish("9" * 41, [line()], self.refusing_rules(), "Aucune opération (essai)")

    def test_then_a_rule_that_cannot_be_applied(self):
        with self.assertRaisesMessage(ValueError, "Import annulé : la règle de reconnaissance « Essai »"):
            finish("9" * 40, [line()], self.refusing_rules(), "Aucune opération (essai)")

    def test_otherwise_every_line_is_fingerprinted(self):
        lines = [line(), line(), line("-4.1")]
        statement = finish("****0042", lines, rules_of(), "Aucune opération (essai)")
        self.assertEqual(statement.account, "****0042")
        fingerprints = [one.fingerprint for one in statement.lines]
        # Two identical lines are two operations; « -4.1 » is another one.
        self.assertEqual(len(set(fingerprints)), 3)
        self.assertTrue(all(len(one) == 64 for one in fingerprints))


class BoundsTests(SimpleTestCase):
    def test_too_many_operations_refuse_the_file(self):
        with mock.patch.object(statements, "MAX_OPERATIONS", 2):
            self.assertEqual(len(parse_statement(bnp(row(), row()), rules_of(), SEEDED_FORMAT).lines), 2)
            with self.assertRaises(ValueError) as refused:
                parse_statement(bnp(row(), row(), row()), rules_of(), SEEDED_FORMAT)
        self.assertEqual(
            str(refused.exception), "Ce relevé compte plus de 2 opérations : exportez une période plus courte."
        )

    def test_too_many_rows_refuse_the_file_before_any_is_read(self):
        """Blank rows count: they cost the reading all the same."""
        with mock.patch.object(statements, "MAX_ROWS", 3):
            parse_statement(bnp(row(), row()), rules_of(), SEEDED_FORMAT)
            with self.assertRaises(ValueError) as refused:
                # A date no day: refused for its rows before it is read.
                parse_statement(bnp(row(), "\n", row(day="31/02/2026")), rules_of(), SEEDED_FORMAT)
        self.assertEqual(
            str(refused.exception), "Ce relevé compte plus de 3 lignes : exportez une période plus courte."
        )

    def test_a_refusal_echoes_80_characters_of_the_file_at_most(self):
        huge = "9" * 1_000_000 + "x"
        with self.assertRaises(ValueError) as refused:
            parse_amount(huge)
        said = str(refused.exception)
        self.assertEqual(said, f"Montant illisible dans le relevé : {'9' * 80!r}")
        self.assertEqual(statements.echoed(huge), "9" * 80)
        self.assertEqual(statements.echoed("court"), "court")

    def test_a_short_amount_is_echoed_as_it_always_was(self):
        with self.assertRaisesMessage(ValueError, "Montant illisible dans le relevé : '12,3x'"):
            parse_amount("12,3x")

    def test_a_row_wider_than_any_statement_s_is_refused_before_the_csv_module_builds_it(self):
        """The csv module builds a whole row before anything counts its
        cells: one row of 9 MB held 231 MB. A row - over every line a quoted
        cell spans - past `MAX_ROW_CHARS` is no CSV, refused first."""
        wide = b"ab;" * 350_000 + b"\n"
        tracemalloc.start()
        try:
            with self.assertRaisesMessage(ValueError, statements.NOT_A_CSV):
                parse_statement(bnp(row()) + wide, rules_of(), SEEDED_FORMAT)
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # The file and its text, never its 350 000 cells (some 30 MB).
        self.assertLess(peak, 15_000_000)
        with mock.patch.object(statements, "MAX_ROW_CHARS", 200):
            fits = HEADER + row(label="X" * (200 - len(row(label=""))))
            self.assertEqual(len(fits) - len(HEADER), 200)
            self.assertEqual(len(parse_statement(fits.encode(), rules_of(), SEEDED_FORMAT).lines), 1)
            for content in (
                (fits + row(label="X" * 200)).encode(),
                # Short lines, one row: a quoted cell holding line breaks.
                bnp(row(label='"' + "X\n" * 150 + '"')),
            ):
                with self.subTest(content=content[-30:]):
                    with self.assertRaisesMessage(ValueError, statements.NOT_A_CSV):
                        parse_statement(content, rules_of(), SEEDED_FORMAT)
            # « Tester »'s rows are held to it too.
            with self.assertRaisesMessage(ValueError, statements.NOT_A_CSV):
                statements.rows(bnp(row(label="X" * 300)), statements.check_format(SEEDED_FORMAT), limit=1)


class LayoutTests(SimpleTestCase):
    def test_a_layout_with_no_column_needs_no_cell(self):
        layout = statements.Layout(
            name="Sans colonne",
            encoding="auto",
            delimiter=";",
            date_format="dd/mm/yyyy",
            decimal_mark=",",
            date=None,
            labels=(),
            amount=None,
            debit=None,
            credit=None,
            value_date=None,
            bank_type=None,
            account=None,
        )
        self.assertEqual(layout.width, 0)


class LazyReadingTests(SimpleTestCase):
    def test_the_import_never_builds_the_list_of_every_row(self):
        """`rows` is « Tester »'s, for the first rows it shows: the import
        reads a CSV row by row."""
        with mock.patch.object(statements, "rows", side_effect=AssertionError("every row in a list")):
            read = parse_statement(bnp(row(), row(amount="12,00")), rules_of(), SEEDED_FORMAT)
        self.assertEqual([one.amount for one in read.lines], [Decimal("-4.10"), Decimal("12.00")])

    def test_tester_s_rows_stop_at_what_it_shows(self):
        layout = statements.check_format(SEEDED_FORMAT)
        content = bnp(row(), "\n", row(label="DEUX"), row(label="TROIS"))
        self.assertEqual(len(statements.rows(content, layout)), 4)
        shown = statements.rows(content, layout, limit=2)
        self.assertEqual([one[3] for one in shown[1:]], ["LIBELLE EXEMPLE"])
        self.assertEqual(len(shown), 2)

    def test_a_file_the_csv_module_cannot_split_is_refused_whole_before_any_row_is_read(self):
        """Read row by row, the file is still refused for what the csv
        module cannot split anywhere in it, before a row's own refusal - as
        when every row was read first."""
        content = bnp(row(day="31/02/2026"), row()) + b"x\ry\n"
        with self.assertRaisesMessage(ValueError, statements.NOT_A_CSV):
            parse_statement(content, rules_of(), SEEDED_FORMAT)

    def test_each_raw_line_is_what_the_file_printed_before_the_rules_read_it(self):
        layout = statements.check_format(SEEDED_FORMAT)
        reading = statements._CsvReading(bnp(row(label="  CB   EPICERIE  ", amount="-1 234,50")), layout)
        self.assertEqual(reading.account, "")
        (raw,) = list(reading.lines())
        self.assertEqual(
            raw,
            RawLine(
                operation_date=date(2026, 8, 3),
                value_date=date(2026, 8, 3),
                bank_type="PAIEMENT CB",
                label="CB EPICERIE",
                amount=Decimal("-1234.50"),
            ),
        )
        # Final once every row was read.
        self.assertEqual(reading.account, "****0042")
        rules = rules_of(rule("Carte", "card_payment", r"^CB (?P<tiers>.+)$"))
        read = parse_statement(bnp(row(label="CB EPICERIE")), rules, SEEDED_FORMAT)
        self.assertEqual((read.lines[0].kind, read.lines[0].counterparty), ("CARD", "EPICERIE"))


class SniffTests(SimpleTestCase):
    """What a file plainly is, by its first bytes - never a guess at a CSV,
    which has no mark of its own."""

    def test_each_kind_is_told_by_its_marks(self):
        from bank.tests import camt_files, ofx_files

        for content, kind in (
            (ofx_files.sgml(), "ofx"),
            (ofx_files.xml(), "ofx"),
            (b"\xef\xbb\xbf" + ofx_files.xml(), "ofx"),
            (b"  \r\n<OFX>\n<SIGNONMSGSRSV1>", "ofx"),
            (ofx_files.xml().decode().encode("utf-16"), "ofx"),
            (camt_files.v02(), "camt053"),
            (camt_files.v08(), "camt053"),
            (camt_files.camt(declaration=False), "camt053"),
            # Another camt message is its own: refused under any format.
            (camt_files.v02().replace(b"camt.053", b"camt.052"), "camt.052"),
            (camt_files.v08().replace(b"camt.053", b"camt.054"), "camt.054"),
            (b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>', "xml"),
            (bnp(row()), None),
            (b"", None),
            (b"<b>Date</b>;Montant\n03/08/2026;-4,10\n", None),
            ("Date;Libellé\n".encode("utf-16"), None),
        ):
            with self.subTest(content=content[:40]):
                self.assertEqual(statements.sniff(content), kind)

    def test_a_file_of_another_kind_is_refused_with_both_names(self):
        from bank.tests import ofx_files

        with self.assertRaises(statements.WrongFileType) as refused:
            parse_statement(ofx_files.sgml(), rules_of(), SEEDED_FORMAT)
        self.assertTrue(str(refused.exception).startswith("Ce fichier est un relevé OFX / QFX (Money), et le format"))
        self.assertIsInstance(refused.exception, ValueError)
