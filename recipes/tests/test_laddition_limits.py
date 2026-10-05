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

    def test_a_float_s_tiny_noise_in_exponent_notation_reads_as_before(self):
        """PHP prints 0.1 + 0.2 - 0.3 as 5.5511151231258E-17: an owner's
        export holding one is read as it always was, never refused."""
        result = parse_rows(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%", discount="5.5511151231258E-17")]
        )
        money = result.money[("Pinte Exemple", date(2026, 6, 1))]
        self.assertEqual(money.revenue_ttc, Decimal("7.50") - Decimal("5.5511151231258E-17"))
        self.assertEqual(result.discounted_lines, 1)

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


def _wide_workbook(rows: int) -> str:
    """The lines sheet, its header the export's, every row below carrying
    one more cell at XFD - a row padded to it is 16 384 cells."""
    from xml.sax.saxutils import escape

    def cell(ref, value):
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'

    letters = "ABCDEFGH"
    header = "".join(cell(f"{letters[at]}1", title) for at, title in enumerate(HEADER))
    body = []
    for number in range(2, rows + 2):
        values = line("2026-06-01", "Pinte Exemple", "7.50", "20%")
        cells = "".join(cell(f"{letters[at]}{number}", value) for at, value in enumerate(values))
        body.append(f'<row r="{number}">{cells}{cell(f"XFD{number}", "loin")}</row>')
    path = str(Path(tempfile.mkdtemp()) / "ventes.xlsx")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData><row r="1">{header}</row>{"".join(body)}</sheetData></worksheet>',
        )
    return path


class AnUploadIsBoundedTests(SimpleTestCase):
    """An export uploaded on « Ventes » is read only as wide as the columns
    this parser reads, and holds at most MAX_ROWS rows and MAX_PRODUCTS
    products a sheet: 20 000 rows each with a cell at XFD - a 104 KB upload -
    took 11 s, in the one process every espace shares. The owner's fetched
    exports are held to none of it."""

    def widths(self, path, **kwargs) -> list[int]:
        """The width of every row the parser was handed, header first."""
        from recipes.pos import xlsx_reader

        real = xlsx_reader.read_sheet
        seen: list[int] = []

        def spy(*args, **options):
            for row in real(*args, **options):
                seen.append(len(row))
                yield row

        with mock.patch.object(xlsx_reader, "read_sheet", spy):
            result = parse_sales_export(path, **kwargs)
        self.assertEqual(result.total_quantity, 3)
        return seen

    def test_an_upload_is_read_no_wider_than_the_columns_read(self):
        path = _wide_workbook(3)
        self.assertEqual(self.widths(path, untrusted=True), [len(HEADER)] * 4)
        # The owner's downloads read as they always did.
        self.assertEqual(self.widths(path), [len(HEADER)] + [16_384] * 3)

    def test_an_upload_of_more_rows_than_a_file_may_hold_is_refused(self):
        rows = [HEADER] + [line("2026-06-01", "Pinte Exemple", "7.50", "20%")] * 4
        path = write_workbook(rows)
        with mock.patch.object(laddition_xlsx, "MAX_ROWS", 3):
            with self.assertRaises(LadditionExportError) as caught:
                parse_sales_export(path, untrusted=True)
            self.assertEqual(str(caught.exception), "Plus de 3 lignes : exportez une période plus courte.")
            self.assertEqual(parse_sales_export(path).total_quantity, 4)

    def test_an_upload_of_more_products_than_a_till_sells_is_refused(self):
        path = write_workbook(
            [HEADER] + [line("2026-06-01", f"Produit Exemple {number}", "1.00", "20%") for number in range(3)]
        )
        with mock.patch.object(laddition_xlsx, "MAX_PRODUCTS", 2):
            with self.assertRaises(LadditionExportError) as caught:
                parse_sales_export(path, untrusted=True)
            self.assertIn("Plus de 2 produits différents", str(caught.exception))
            self.assertEqual(len(parse_sales_export(path).products), 3)

    def test_an_upload_s_ticket_sheet_is_bounded_too_the_lines_kept(self):
        path = write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            tickets=[TICKET_HEADER] + [ticket("2026-06-01", number, "7.5", "CB(7,50)") for number in range(3)],
        )
        with mock.patch.object(laddition_xlsx, "MAX_ROWS", 2):
            result = parse_sales_export(path, untrusted=True, file_name="Lignes de ventes.xlsx")
        self.assertEqual(result.total_quantity, 1)
        self.assertFalse(result.payments_read)
        self.assertEqual(
            result.payment_sheet_errors,
            ["Lignes de ventes.xlsx : Plus de 2 lignes : exportez une période plus courte."],
        )


class TheNameAPersonKnowsTests(SimpleTestCase):
    def test_a_ticket_sheet_refused_names_the_file_uploaded_not_its_staging_name(self):
        """An upload is read under « <uuid>.part »: a log line naming that is
        a name nobody uploaded."""
        source = write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "NaN", "CB(7,50)")],
        )
        staged = Path(source).with_name("0123456789abcdef.part")
        Path(source).rename(staged)
        result = parse_sales_export(staged, untrusted=True, file_name="Lignes de ventes.xlsx")
        (said,) = result.payment_sheet_errors
        self.assertTrue(said.startswith("Lignes de ventes.xlsx : "), said)
        self.assertNotIn(".part", said)
