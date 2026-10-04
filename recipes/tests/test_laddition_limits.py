"""The L'Addition parser bounded by what stores its figures
(recipes/pos/laddition_xlsx.py).

Opened to uploads - « Export L'Addition » on « Ventes », by any bar - the
parser that only ever read the owner's downloads reads anything. Django does
not check a decimal on its way into SQLite, and a figure wider than its
column makes EVERY later read of the row raise: Marges, « Entrées d'argent »
and « Données » a 500 for good, and only raw SQL gets it out. So each figure
is bounded at the door, and a number `Decimal` reads but no export writes -
an exponent, NaN, Infinity - refuses the file. The owner's own exports never
come near: their rows read exactly as before (test_pos_xlsx, test_pos_revenue).

Every name and figure is invented.
"""

from __future__ import annotations

import tempfile
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from recipes.pos import laddition_xlsx
from recipes.pos.laddition_xlsx import LadditionExportError, parse_payment_rows, parse_rows, parse_sales_export
from recipes.tests.test_pos_payments import TICKET_HEADER, ticket
from recipes.tests.test_pos_revenue import CONTENT_TYPES, HEADER, RELS, WORKBOOK, line, write_workbook


def refused(rows) -> str:
    raise_on = SimpleTestCase()
    with raise_on.assertRaises(LadditionExportError) as caught:
        parse_rows([HEADER, *rows])
    return str(caught.exception)


class NumbersNoExportWritesTests(SimpleTestCase):
    def test_an_amount_with_an_exponent_nan_or_infinity_refuses_the_file(self):
        for amount in ("1E+20", "1E+500", "1e5", "NaN", "nan", "inf", "Infinity", "-Infinity", "sNaN"):
            with self.subTest(amount=amount):
                said = refused([line("2026-06-01", "Pinte Exemple", amount, "20%")])
                self.assertIn("refusé", said)

    def test_a_quantity_so_written_refuses_the_file_too(self):
        for quantity in ("1E+20", "inf", "NaN"):
            with self.subTest(quantity=quantity):
                refused([line("2026-06-01", "Pinte Exemple", "7.50", "20%", quantity=quantity)])

    def test_what_is_no_number_at_all_reads_as_before(self):
        """« sept euros » is an amount nobody can read (the day left unread),
        « - » the Total row's quantity (skipped): neither is new."""
        result = parse_rows([HEADER, line("2026-06-01", "Pinte Exemple", "sept euros", "20%")])
        self.assertEqual(result.lines_without_amount, 1)
        result = parse_rows([HEADER, ["-", "Total", "-", "-", "-", "-", "-", "-"]])
        self.assertEqual(result.skipped, 1)

    def test_a_rate_with_an_exponent_is_no_rate(self):
        result = parse_rows([HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "2E+1")])
        self.assertEqual(result.lines_without_rate, 1)


class WiderThanTheColumnTests(SimpleTestCase):
    def test_a_days_revenue_past_its_column_refuses_the_file_naming_it(self):
        said = refused([line("2026-06-01", "Pinte Exemple", "123456789.00", "20%")])
        self.assertIn("Pinte Exemple", said)
        self.assertIn("01/06/2026", said)

    def test_two_lines_that_only_together_pass_the_column_refuse_it(self):
        refused(
            [
                line("2026-06-01", "Pinte Exemple", "60000000.00", "20%"),
                line("2026-06-01", "Pinte Exemple", "60000000.00", "20%"),
            ]
        )

    def test_the_column_exactly_is_read(self):
        result = parse_rows([HEADER, line("2026-06-01", "Pinte Exemple", "99999999.99", "")])
        self.assertEqual(result.money[("Pinte Exemple", date(2026, 6, 1))].revenue_ttc, Decimal("99999999.99"))

    def test_a_quantity_no_bar_sells_refuses_the_file(self):
        refused([line("2026-06-01", "Pinte Exemple", "7.50", "20%", quantity="100001")])
        rows = [line("2026-06-01", "Pinte Exemple", "7.50", "20%", quantity="90000") for _ in range(12)]
        refused(rows)

    def test_a_day_outside_the_centuries_a_till_prints_refuses_the_file(self):
        for day in ("0001-01-01", "1999-12-31", "2100-01-01"):
            with self.subTest(day=day):
                refused([line(day, "Pinte Exemple", "7.50", "20%")])

    def test_an_arithmetic_error_is_the_files_refusal_never_a_traceback(self):
        with mock.patch.object(laddition_xlsx, "_to_ht", side_effect=InvalidOperation):
            said = refused([line("2026-06-01", "Pinte Exemple", "7.50", "20%")])
        self.assertIn("nombre", said)

    def test_a_name_is_cut_to_its_column_not_refused(self):
        """A name is not money: cut to 255, like an e-invoice's."""
        name = "Pinte " + "x" * 300
        category = "Catégorie " + "y" * 300
        result = parse_rows(
            [
                [*HEADER, "TAG_Catégorie", "TAG_Typologie"],
                [*line("2026-06-01", name, "7.50", "20%"), category, category],
            ]
        )
        ((read, _day, _quantity),) = result.entries
        self.assertEqual(len(read), 255)
        self.assertEqual(read, name[:255])
        self.assertEqual(len(result.products[read]["category"]), 255)
        self.assertEqual(len(result.products[read]["typology"]), 255)


class PaymentsBoundTests(SimpleTestCase):
    def test_a_days_payments_past_their_column_refuse_the_sheet(self):
        rows = [TICKET_HEADER, ticket("2026-06-01", 1, "12345678901.00", "CB(12345678901,00)")]
        with self.assertRaises(LadditionExportError) as caught:
            parse_payment_rows(rows)
        self.assertIn("01/06/2026", str(caught.exception))

    def test_a_ticket_total_with_an_exponent_refuses_the_sheet(self):
        with self.assertRaises(LadditionExportError):
            parse_payment_rows([TICKET_HEADER, ticket("2026-06-01", 1, "1E+20", "")])

    def test_digits_of_another_script_are_no_payment(self):
        self.assertIsNone(laddition_xlsx.read_payments("CB(٤,50)"))

    def test_the_lines_import_all_the_same_and_the_sheet_says_why_in_french(self):
        path = write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "NaN", "CB(7,50)")],
        )
        result = parse_sales_export(path)
        self.assertEqual(result.total_quantity, 1)
        self.assertFalse(result.payments_read)
        (said,) = result.payment_sheet_errors
        self.assertTrue(said.startswith("ventes.xlsx : "))
        self.assertIn("refusé", said)
        self.assertNotIn(str(Path(path).parent), said)


class TheFileRefusedTests(SimpleTestCase):
    def test_an_uploaded_export_with_a_hostile_figure_is_refused_in_french_naming_no_folder(self):
        path = write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", "1E+500", "20%")])
        with self.assertRaises(LadditionExportError) as caught:
            parse_sales_export(path, untrusted=True)
        self.assertNotIn(str(Path(path).parent), str(caught.exception))

    def test_a_cell_past_xfd_is_this_exports_refusal(self):
        path = str(Path(tempfile.mkdtemp()) / "ventes.xlsx")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("xl/workbook.xml", WORKBOOK)
            archive.writestr("xl/_rels/workbook.xml.rels", RELS)
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="ZZZZZZZZZZZZ1"><v>1</v></c></row></sheetData></worksheet>',
            )
        with self.assertRaises(LadditionExportError) as caught:
            parse_sales_export(path)
        self.assertIn("XFD", str(caught.exception))
        self.assertIn(laddition_xlsx.NOT_THE_EXPORT, str(caught.exception))
