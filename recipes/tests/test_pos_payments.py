"""The till's means of payment, from the export's « SalesDocument » sheet.

What the bank is paid from: a ticket's `Paiements` cell - « CB(4,50) »,
« Cash(5,00)/CB(3,50) », « Avoir(1 350,00) » - summed per (day, method) and
stored whole per day (recipes/payments.py). The ways this could be silently
wrong, each pinned below:

- a ticket read half-way (its card payment kept, its cash lost) - so a cell
  that is not the till's grammar is refused WHOLE, and filed at the ticket's
  total under « Illisible » so the day still adds up;
- a ticket with a total and no payment dropped - filed under « Sans
  paiement enregistré », never lost;
- a thousands separator that is a no-break space read as the end of the
  number;
- a day read from two overlapping exports counted twice, or merged method
  by method so it keeps a payment its later reading no longer has.

Data invented throughout: every name, ticket number, amount and day below is
made up and the workbooks are built by hand. Nothing is downloaded, nothing
is read from scraped_invoices/.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from accounts import paths
from recipes.models import PosDailyPayment, PosProduct, PosProductDailyQuantity, SalesImportJob
from recipes.payments import changed_days, record_payments, replace_days
from recipes.pos.laddition_xlsx import (
    DayPayment,
    LadditionExportError,
    ParsedExport,
    PaymentsSheetMissing,
    parse_payment_rows,
    parse_payments_export,
    parse_sales_export,
    parse_sales_exports,
    read_payments,
)
from recipes.tasks import import_laddition_sales_task, payments_log
from recipes.tests.test_pos_revenue import (
    CONTENT_TYPES,
    HEADER,
    RELS_WITH_TICKETS,
    WORKBOOK_WITH_TICKETS,
    _sheet_xml,
    line,
    write_workbook,
)

#: The columns this reader cares about, in the order the real export puts
#: them (it has thirty-odd; they are found by name).
TICKET_HEADER = ["Etablissement", "Jour", "ID Ticket", "Total TTC", "Trop perçus", "Avoir", "Paiements"]
CB, CASH, CHEQUE, CREDIT = PosDailyPayment.CARD, PosDailyPayment.CASH, PosDailyPayment.CHEQUE, PosDailyPayment.CREDIT
UNREAD, UNPAID = PosDailyPayment.UNREAD, PosDailyPayment.UNPAID


def ticket(day, number, total, payments, overpaid="0"):
    """One ticket row. `total` dot decimal, `payments` as the till prints them."""
    return ["Bar Exemple", day, f"T-{number:04d}", total, overpaid, "0", payments]


#: The export's own last row: « - » where a day should be.
TOTAL_ROW = ["Bar Exemple", "-", "-", "-", "-", "-", "-"]


def june(day: int) -> date:
    return date(2026, 6, day)


def paid(result, day, method) -> tuple[Decimal, int]:
    payment = result.payments[(day, method)]
    return payment.amount, payment.count


def stored() -> list[tuple]:
    return list(PosDailyPayment.objects.values_list("sold_on", "method", "amount", "payments"))


class PaymentsCellTests(SimpleTestCase):
    """`read_payments`: one cell, the whole cell or nothing."""

    def test_one_payment(self):
        self.assertEqual(read_payments("CB(4,50)"), [("CB", Decimal("4.50"))])

    def test_several_payments_in_order(self):
        self.assertEqual(read_payments("Cash(5,00)/CB(3,50)"), [("Cash", Decimal("5.00")), ("CB", Decimal("3.50"))])

    def test_an_empty_cell_is_no_payment_not_an_unreadable_one(self):
        self.assertEqual(read_payments(""), [])
        self.assertEqual(read_payments("   "), [])

    def test_every_thousands_separator_a_formatter_may_print(self):
        """A space, a no-break space, a narrow no-break space: the three look
        the same on screen, and a reader that knew only the first would stop
        « 1 350,00 » at « 1 »."""
        for separator in (" ", " ", " "):
            with self.subTest(separator=hex(ord(separator))):
                self.assertEqual(read_payments(f"Avoir(1{separator}350,00)"), [("Avoir", Decimal("1350.00"))])

    def test_money_handed_back_is_negative(self):
        self.assertEqual(
            read_payments("Cash(10,00)/Cash(-1,50)"), [("Cash", Decimal("10.00")), ("Cash", Decimal("-1.50"))]
        )

    def test_what_is_not_the_grammar_reads_as_nothing(self):
        """None, not a partial list: a ticket kept as its card payment alone
        would look perfectly ordinary and be short of its cash."""
        for text in (
            "Carte 4,50",  # no brackets
            "CB(4,50)/",  # a trailing separator
            "CB(4,50)/Cash(2,00",  # a bracket never closed
            "(4,50)",  # no method
            "CB(1,00)/ (2,00)",  # a blank method
            "CB(quatre)",  # no amount
            "CB(1.800,00)",  # two decimal separators: refused, not guessed
            "CB(4,505)",  # three decimals is no amount of money
            "-",
        ):
            with self.subTest(text=text):
                self.assertIsNone(read_payments(text))


class PaymentRowsTests(SimpleTestCase):
    """`parse_payment_rows`: tickets summed per (day, method)."""

    def parse(self, rows, header=None):
        return parse_payment_rows([header or TICKET_HEADER, *rows])

    def test_a_ticket_paid_by_card(self):
        result = self.parse([ticket("2026-06-01", 1, "4.5", "CB(4,50)")])
        self.assertEqual(paid(result, june(1), CB), (Decimal("4.50"), 1))
        self.assertEqual(result.payment_days, {june(1)})
        self.assertEqual(result.tickets, 1)
        self.assertTrue(result.payments_read)

    def test_payments_sum_per_day_and_method_and_count_one_each(self):
        result = self.parse(
            [
                ticket("2026-06-01", 1, "14.5", "CB(10,00)/CB(4,50)"),
                ticket("2026-06-01", 2, "8.5", "Cash(5,00)/CB(3,50)"),
                ticket("2026-06-02", 3, "4", "CB(4,00)"),
            ]
        )
        self.assertEqual(paid(result, june(1), CB), (Decimal("18.00"), 3))
        self.assertEqual(paid(result, june(1), CASH), (Decimal("5.00"), 1))
        self.assertEqual(paid(result, june(2), CB), (Decimal("4.00"), 1))
        self.assertEqual(result.payments_total, Decimal("27.00"))

    def test_a_refund_nets_into_its_day(self):
        result = self.parse(
            [
                ticket("2026-06-01", 1, "8.5", "Cash(10,00)/Cash(-1,50)"),
                ticket("2026-06-01", 2, "-6", "CB(-6,00)"),
            ]
        )
        self.assertEqual(paid(result, june(1), CASH), (Decimal("8.50"), 2))
        self.assertEqual(paid(result, june(1), CB), (Decimal("-6.00"), 1))

    def test_large_amounts_with_each_separator(self):
        result = self.parse(
            [
                ticket("2026-06-01", 1, "1350", "Avoir(1 350,00)"),
                ticket("2026-06-01", 2, "1250", "Chèque(1 250,00)"),
                ticket("2026-06-01", 3, "2000", "CB(2 000,00)"),
            ]
        )
        self.assertEqual(paid(result, june(1), CREDIT), (Decimal("1350.00"), 1))
        self.assertEqual(paid(result, june(1), CHEQUE), (Decimal("1250.00"), 1))
        self.assertEqual(paid(result, june(1), CB), (Decimal("2000.00"), 1))
        self.assertEqual(result.unread_payment_tickets, 0)

    def test_a_comped_ticket_pays_nothing_and_its_day_is_still_read(self):
        """An empty `Paiements` with a total of 0: nothing to file - but the
        day WAS read, and a writer must be able to tell it from a day nobody
        read."""
        result = self.parse([ticket("2026-06-01", 1, "0", "")])
        self.assertEqual(result.payments, {})
        self.assertEqual(result.payment_days, {june(1)})
        self.assertEqual((result.unpaid_tickets, result.unread_payment_tickets), (0, 0))

    def test_a_ticket_with_a_total_and_no_payment_is_filed_never_dropped(self):
        result = self.parse([ticket("2026-06-01", 1, "12.5", "")])
        self.assertEqual(paid(result, june(1), UNPAID), (Decimal("12.50"), 1))
        self.assertEqual(result.unpaid_tickets, 1)

    def test_a_cell_that_does_not_read_files_the_tickets_total_under_unread(self):
        """So the day still adds up to what it took, and the page can say
        how much of it nobody can attribute."""
        result = self.parse(
            [
                ticket("2026-06-01", 1, "9.5", "CB(7,50)/Cash(2,00"),
                ticket("2026-06-01", 2, "4.5", "CB(4,50)"),
            ]
        )
        self.assertEqual(paid(result, june(1), UNREAD), (Decimal("9.5"), 1))
        self.assertEqual(paid(result, june(1), CB), (Decimal("4.50"), 1))
        self.assertNotIn((june(1), CASH), result.payments)
        self.assertEqual(result.unread_payment_tickets, 1)

    def test_an_unreadable_ticket_whose_total_does_not_read_either_is_still_counted(self):
        result = self.parse(
            [
                ticket("2026-06-01", 1, "n/a", "CB(quatre)"),
                ticket("2026-06-01", 2, "n/a", ""),
            ]
        )
        self.assertEqual(paid(result, june(1), UNREAD), (Decimal("0"), 2))
        self.assertEqual(result.unread_payment_tickets, 2)

    def test_the_total_row_is_skipped_and_counted(self):
        result = self.parse([ticket("2026-06-01", 1, "4.5", "CB(4,50)"), TOTAL_ROW])
        self.assertEqual(result.payment_days, {june(1)})
        self.assertEqual(result.ticket_rows_skipped, 1)
        self.assertEqual(result.tickets, 1)

    def test_a_ticket_printed_twice_in_one_file_is_read_once(self):
        result = self.parse(
            [
                ticket("2026-06-01", 1, "4.5", "CB(4,50)"),
                ticket("2026-06-01", 1, "4.5", "CB(4,50)"),
            ]
        )
        self.assertEqual(paid(result, june(1), CB), (Decimal("4.50"), 1))
        self.assertEqual(result.duplicate_tickets, 1)

    def test_tickets_with_no_id_are_not_taken_for_duplicates(self):
        row = ["Bar Exemple", "2026-06-01", "", "4.5", "0", "0", "CB(4,50)"]
        result = self.parse([row, list(row)])
        self.assertEqual(paid(result, june(1), CB), (Decimal("9.00"), 2))
        self.assertEqual(result.duplicate_tickets, 0)

    def test_a_method_is_one_method_whatever_its_accents_and_case(self):
        """« Cheque », « chèque » and « CHÈQUE » are one method, stored as the
        till spells it. A method nobody named here stays as printed."""
        result = self.parse(
            [
                ticket("2026-06-01", 1, "10", "Cheque(10,00)"),
                ticket("2026-06-01", 2, "20", "chèque(20,00)"),
                ticket("2026-06-01", 3, "30", "CHÈQUE(30,00)"),
                ticket("2026-06-01", 4, "4", "cb(4,00)"),
                ticket("2026-06-01", 5, "6", "Bon cadeau(6,00)"),
            ]
        )
        self.assertEqual(paid(result, june(1), CHEQUE), (Decimal("60.00"), 3))
        self.assertEqual(paid(result, june(1), CB), (Decimal("4.00"), 1))
        self.assertEqual(paid(result, june(1), "Bon cadeau"), (Decimal("6.00"), 1))

    def test_payments_are_the_total_plus_the_overpayment(self):
        """The receipt's own arithmetic, true of every ticket stored: a tip
        left on the card makes the payment larger than the ticket."""
        result = self.parse([ticket("2026-06-01", 1, "4.5", "CB(5,00)", overpaid="0.5")])
        self.assertEqual(result.tickets_not_adding_up, 0)
        self.assertEqual(paid(result, june(1), CB), (Decimal("5.00"), 1))

    def test_a_ticket_that_does_not_add_up_is_kept_as_paid_and_counted(self):
        """The payments are what the bank sees, so they are what is kept -
        and the day it happens, it is said."""
        result = self.parse([ticket("2026-06-01", 1, "4.5", "CB(6,00)")])
        self.assertEqual(result.tickets_not_adding_up, 1)
        self.assertEqual(paid(result, june(1), CB), (Decimal("6.00"), 1))

    def test_without_the_overpayment_column_nothing_is_checked(self):
        """A tip would otherwise read as a ticket that does not add up."""
        header = [column for column in TICKET_HEADER if column != "Trop perçus"]
        row = ["Bar Exemple", "2026-06-01", "T-0001", "4.5", "0", "CB(5,00)"]
        self.assertEqual(parse_payment_rows([header, row]).tickets_not_adding_up, 0)

    def test_columns_are_found_by_name_not_position(self):
        result = parse_payment_rows(
            [
                ["Paiements", "Total TTC", "Jour"],
                ["CB(4,50)", "4.5", "2026-06-01"],
            ]
        )
        self.assertEqual(paid(result, june(1), CB), (Decimal("4.50"), 1))

    def test_a_missing_column_says_which_one(self):
        with self.assertRaises(LadditionExportError) as caught:
            parse_payment_rows([["Jour", "Total TTC"], ["2026-06-01", "4.5"]])
        self.assertIn("Paiements", str(caught.exception))

    def test_an_empty_sheet_is_an_error(self):
        with self.assertRaises(LadditionExportError):
            parse_payment_rows([])

    def test_a_short_row_reads_its_missing_cells_as_blank(self):
        """The reader pads a row only to its own last cell: a ticket whose
        last cells are empty arrives short."""
        result = self.parse([["Bar Exemple", "2026-06-01", "T-0001", "0"]])
        self.assertEqual(result.payment_days, {june(1)})
        self.assertEqual(result.payments, {})


class MethodVocabularyTests(SimpleTestCase):
    """PosDailyPayment's vocabulary: one place, for the bank's pages too."""

    def test_the_known_methods_are_stored_in_the_tills_spelling(self):
        for printed, stored_as in (
            ("CB", CB),
            ("cb", CB),
            ("Cash", CASH),
            ("CASH", CASH),
            ("Cheque", CHEQUE),
            ("chèque", CHEQUE),
            ("avoir", CREDIT),
            ("tr", PosDailyPayment.MEAL_VOUCHER),
            ("  CB ", CB),
        ):
            with self.subTest(printed=printed):
                self.assertEqual(PosDailyPayment.canonical(printed), stored_as)

    def test_anything_else_as_printed(self):
        self.assertEqual(PosDailyPayment.canonical("Bon cadeau"), "Bon cadeau")
        self.assertEqual(PosDailyPayment.canonical(None), UNPAID)

    def test_a_method_longer_than_the_column_is_cut_to_it(self):
        self.assertEqual(len(PosDailyPayment.canonical("X" * 60)), 40)

    def test_the_french_names(self):
        self.assertEqual(
            [
                PosDailyPayment.label_for(method)
                for method in (CB, CASH, CHEQUE, CREDIT, "TR", UNREAD, UNPAID, "Bon cadeau")
            ],
            [
                "Carte",
                "Espèces",
                "Chèque",
                "Avoir",
                "Titres-restaurant",
                "Illisible",
                "Sans paiement enregistré",
                "Bon cadeau",
            ],
        )
        self.assertEqual(PosDailyPayment.label_for("cheque"), "Chèque")
        self.assertEqual(PosDailyPayment(method=CASH).label, "Espèces")

    def test_the_display_order(self):
        """The ways money reaches the account first; what could not be told
        last, so a listing never opens on it."""
        methods = [UNPAID, "Bon cadeau", UNREAD, CREDIT, CB, "TR", CASH, CHEQUE]
        self.assertEqual(
            sorted(methods, key=PosDailyPayment.sort_key),
            [CB, CASH, CHEQUE, "TR", CREDIT, "Bon cadeau", UNREAD, UNPAID],
        )


def with_a_damaged_ticket_sheet(path: str) -> str:
    """`path` packed again DEFLATED, the ticket sheet's compressed stream
    damaged from its first byte (0xFF: a block type deflate does not have).

    The zip's directory is intact, so the file opens, lists its sheets and
    reads its lines; it is inflating that one member that fails - and with
    `zlib.error`, which is none of the errors a zip or an XML parser raises
    (a stored member cut short fails its CRC instead: a BadZipFile)."""
    with zipfile.ZipFile(path) as source:
        members = [(info.filename, source.read(info)) for info in source.infolist()]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("xl/worksheets/sheet2.xml")
    raw = bytearray(Path(path).read_bytes())
    # The local header: 30 bytes, then the name and the extra field, whose
    # lengths it carries at offsets 26 and 28.
    offset = info.header_offset
    name_length = int.from_bytes(raw[offset + 26 : offset + 28], "little")
    extra_length = int.from_bytes(raw[offset + 28 : offset + 30], "little")
    raw[offset + 30 + name_length + extra_length] = 0xFF
    Path(path).write_bytes(bytes(raw))
    return path


def _workbook_with_ticket_xml(ticket_xml: str, folder=None, name="ventes.xlsx") -> str:
    """A workbook whose ticket sheet is `ticket_xml` as given - for a sheet
    that is broken."""
    path = str(Path(folder or tempfile.mkdtemp()) / name)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", WORKBOOK_WITH_TICKETS)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS_WITH_TICKETS)
        archive.writestr(
            "xl/worksheets/sheet1.xml", _sheet_xml([HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")])
        )
        archive.writestr("xl/worksheets/sheet2.xml", ticket_xml)
    return path


class ReadingAFileTests(SimpleTestCase):
    """The ticket sheet beside the lines: optional, and never at the lines'
    expense."""

    LINES = [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")]

    def test_a_workbook_with_both_sheets_reads_both(self):
        path = write_workbook(
            self.LINES, tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)"), TOTAL_ROW]
        )
        result = parse_sales_export(path)
        self.assertEqual(result.entries, [("Pinte Exemple", june(1), 1)])
        self.assertTrue(result.payments_read)
        self.assertEqual(paid(result, june(1), CB), (Decimal("7.50"), 1))

    def test_a_workbook_without_the_ticket_sheet_still_imports_its_lines(self):
        """An older or another export: its days simply carry no payments -
        and say so."""
        result = parse_sales_export(write_workbook(self.LINES))
        self.assertEqual(result.entries, [("Pinte Exemple", june(1), 1)])
        self.assertFalse(result.payments_read)
        self.assertEqual(result.payment_sheets_missing, 1)
        self.assertEqual(result.payment_sheet_errors, [])

    def test_a_ticket_sheet_that_does_not_read_costs_the_lines_nothing(self):
        path = write_workbook(
            self.LINES, tickets=[["Jour", "Total TTC"], ["2026-06-01", "7.5"]], name="sans-paiements.xlsx"
        )
        result = parse_sales_export(path)
        self.assertEqual(result.entries, [("Pinte Exemple", june(1), 1)])
        self.assertFalse(result.payments_read)
        self.assertEqual(len(result.payment_sheet_errors), 1)
        self.assertIn("sans-paiements.xlsx", result.payment_sheet_errors[0])
        self.assertIn("Paiements", result.payment_sheet_errors[0])

    def test_a_sheet_broken_half_way_gives_no_half_reading(self):
        """Its first tickets read before the XML breaks - and are thrown
        away with the rest: half a day's payments would look like a quiet
        day."""
        good = _sheet_xml([TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")])
        broken = good.replace("</sheetData></worksheet>", '<row r="3"><c r="A3"')
        result = parse_sales_export(_workbook_with_ticket_xml(broken))
        self.assertEqual(result.entries, [("Pinte Exemple", june(1), 1)])
        self.assertFalse(result.payments_read)
        self.assertEqual(result.payments, {})
        self.assertEqual(result.payment_days, set())
        self.assertEqual(len(result.payment_sheet_errors), 1)

    def test_the_payments_alone_for_the_backfill(self):
        path = write_workbook(self.LINES, tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")])
        result = parse_payments_export(path)
        self.assertEqual(result.entries, [])
        self.assertEqual(paid(result, june(1), CB), (Decimal("7.50"), 1))

    def test_the_backfill_reading_tells_a_missing_sheet_from_a_broken_file(self):
        with self.assertRaises(PaymentsSheetMissing):
            parse_payments_export(write_workbook(self.LINES))
        not_a_zip = Path(tempfile.mkdtemp()) / "moitie.xlsx"
        not_a_zip.write_bytes(b"pas un zip du tout")
        with self.assertRaises(LadditionExportError) as caught:
            parse_payments_export(str(not_a_zip))
        self.assertNotIsInstance(caught.exception, PaymentsSheetMissing)

    def test_a_ticket_sheet_whose_deflate_stream_is_damaged_costs_the_lines_nothing(self):
        """zlib.error escaped `_unreadable()`: the lines that had read were
        lost with it, and the import job failed on a file whose sales
        were fine."""
        path = with_a_damaged_ticket_sheet(
            write_workbook(
                self.LINES, tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")], name="abime.xlsx"
            )
        )
        result = parse_sales_export(path)
        self.assertEqual(result.entries, [("Pinte Exemple", june(1), 1)])
        self.assertFalse(result.payments_read)
        self.assertEqual(len(result.payment_sheet_errors), 1)
        self.assertIn("abime.xlsx", result.payment_sheet_errors[0])

    def test_the_backfill_reading_of_that_file_is_refused_in_the_readers_own_terms(self):
        path = with_a_damaged_ticket_sheet(
            write_workbook(self.LINES, tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")])
        )
        with self.assertRaises(LadditionExportError) as caught:
            parse_payments_export(path)
        self.assertNotIsInstance(caught.exception, PaymentsSheetMissing)

    def test_a_member_whose_stream_stops_short_is_refused_too(self):
        """A deflated member that ends before its end-of-stream marker
        raises EOFError, which is no zip error either."""
        path = write_workbook(self.LINES, tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")])
        stopped = EOFError("Compressed file ended before the end-of-stream marker was reached")
        with mock.patch("recipes.pos.xlsx_reader.read_sheet", side_effect=stopped):
            with self.assertRaises(LadditionExportError):
                parse_payments_export(path)
            with self.assertRaises(LadditionExportError):
                parse_sales_export(path)

    def test_a_zip_that_holds_no_workbook_is_refused_in_the_readers_own_terms(self):
        """`archive.read("xl/workbook.xml")` raises KeyError on a zip that is
        no workbook at all - no XlsxError, no BadZipFile - and escaped every
        « illisible » branch, stopping a whole folder's backfill."""
        path = Path(tempfile.mkdtemp()) / "autre-chose.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("lisez-moi.txt", "rien à voir")
        for reader in (parse_sales_export, parse_payments_export):
            with self.subTest(reader=reader.__name__), self.assertRaises(LadditionExportError):
                reader(str(path))


class SeveralFilesTests(SimpleTestCase):
    """A folder of exports whose windows overlap."""

    def workbook(self, *tickets):
        return write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")], tickets=[TICKET_HEADER, *tickets]
        )

    def test_a_day_read_again_is_replaced_whole_never_added(self):
        """Every method of the day, one the later reading lacks included:
        merged method by method, the day would keep a cash payment its
        second reading no longer has."""
        first = self.workbook(ticket("2026-06-01", 1, "7.5", "CB(7,50)"), ticket("2026-06-01", 2, "2", "Cash(2,00)"))
        second = self.workbook(ticket("2026-06-01", 1, "7.5", "CB(7,50)"))
        result = parse_sales_exports([first, second])
        self.assertEqual(result.payments, {(june(1), CB): DayPayment(Decimal("7.50"), 1)})
        self.assertEqual(result.repeated_payment_days, 1)

    def test_the_same_file_twice_is_that_day_once(self):
        path = self.workbook(ticket("2026-06-01", 1, "7.5", "CB(7,50)"))
        result = parse_sales_exports([path, path])
        self.assertEqual(result.payments_total, Decimal("7.50"))

    def test_days_that_do_not_overlap_add_up(self):
        first = self.workbook(ticket("2026-06-01", 1, "7.5", "CB(7,50)"))
        second = self.workbook(ticket("2026-06-02", 2, "4.5", "CB(4,50)"))
        result = parse_sales_exports([first, second])
        self.assertEqual(result.payments_total, Decimal("12.00"))
        self.assertEqual(result.payment_days, {june(1), june(2)})
        self.assertEqual(result.repeated_payment_days, 0)

    def test_a_file_without_payments_replaces_nothing(self):
        first = self.workbook(ticket("2026-06-01", 1, "7.5", "CB(7,50)"))
        second = write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")])
        result = parse_sales_exports([first, second])
        self.assertTrue(result.payments_read)
        self.assertEqual(paid(result, june(1), CB), (Decimal("7.50"), 1))
        self.assertEqual(result.payment_sheets_missing, 1)


class RecordPaymentsTests(TestCase):
    """`record_payments`: a day is stored whole, and re-reading corrects it."""

    def export(self, *tickets):
        return parse_payment_rows([TICKET_HEADER, *tickets])

    def test_one_row_per_day_and_method(self):
        record_payments(
            self.export(
                ticket("2026-06-01", 1, "14.5", "CB(10,00)/CB(4,50)"),
                ticket("2026-06-01", 2, "5", "Cash(5,00)"),
                ticket("2026-06-02", 3, "12.5", ""),
            )
        )
        self.assertEqual(
            stored(),
            [
                (june(1), CB, Decimal("14.50"), 2),
                (june(1), CASH, Decimal("5.00"), 1),
                (june(2), UNPAID, Decimal("12.50"), 1),
            ],
        )

    def test_recording_the_same_reading_twice_changes_nothing(self):
        """Not deleted and written back: the same rows, the same ids."""
        export = self.export(ticket("2026-06-01", 1, "7.5", "CB(7,50)"), ticket("2026-06-01", 2, "2", "Cash(2,00)"))
        first = record_payments(export)
        before = list(PosDailyPayment.objects.values_list("pk", "sold_on", "method", "amount", "payments"))
        second = record_payments(export)
        self.assertEqual((first.days_written, first.rows_created), (1, 2))
        self.assertEqual((second.days_written, second.days_unchanged, second.rows_created), (0, 1, 0))
        self.assertEqual(
            list(PosDailyPayment.objects.values_list("pk", "sold_on", "method", "amount", "payments")), before
        )

    def test_a_day_read_again_is_replaced_and_the_others_left_alone(self):
        """A day imported half-way through its service is corrected by the
        next import - a method it no longer has goes - and a day the new
        reading does not cover keeps what it had."""
        record_payments(
            self.export(
                ticket("2026-06-01", 1, "7.5", "CB(7,50)"),
                ticket("2026-06-01", 2, "2", "Cash(2,00)"),
                ticket("2026-06-02", 3, "4.5", "CB(4,50)"),
            )
        )
        result = record_payments(self.export(ticket("2026-06-01", 1, "9", "CB(9,00)")))
        self.assertEqual(
            stored(),
            [
                (june(1), CB, Decimal("9.00"), 1),
                (june(2), CB, Decimal("4.50"), 1),
            ],
        )
        self.assertEqual((result.days_written, result.rows_deleted, result.rows_created), (1, 2, 1))

    def test_a_day_read_with_nothing_paid_is_emptied(self):
        """Every ticket comped: read, and paid nothing - not left holding
        what an earlier reading said."""
        record_payments(self.export(ticket("2026-06-01", 1, "7.5", "CB(7,50)")))
        record_payments(self.export(ticket("2026-06-01", 1, "0", "")))
        self.assertEqual(stored(), [])

    def test_an_export_whose_payments_were_not_read_replaces_nothing(self):
        """No payments sheet is not « paid nothing »: the days keep what they
        have, as the day's money does."""
        record_payments(self.export(ticket("2026-06-01", 1, "7.5", "CB(7,50)")))
        unread = ParsedExport(payment_days={june(1)})
        result = record_payments(unread)
        self.assertEqual(stored(), [(june(1), CB, Decimal("7.50"), 1)])
        self.assertEqual(result.days_written, 0)

    def test_a_negative_day_is_stored_as_it_is(self):
        record_payments(self.export(ticket("2026-06-03", 1, "-6", "CB(-6,00)")))
        self.assertEqual(stored(), [(june(3), CB, Decimal("-6.00"), 1)])

    def test_the_dry_run_answer_and_the_write_are_one(self):
        record_payments(self.export(ticket("2026-06-01", 1, "7.5", "CB(7,50)")))
        readings = self.export(
            ticket("2026-06-01", 1, "7.5", "CB(7,50)"), ticket("2026-06-02", 2, "4.5", "CB(4,50)")
        ).payments_by_day()
        self.assertEqual(changed_days(readings, readings), ([june(2)], 1))
        self.assertEqual(replace_days(readings, readings).days_written, 1)
        self.assertEqual(changed_days(readings, readings), ([], 2))


class PaymentsLogTests(SimpleTestCase):
    """What the import says about the payments, in its own log."""

    def test_what_came_in_per_method(self):
        said = payments_log(
            parse_payment_rows(
                [
                    TICKET_HEADER,
                    ticket("2026-06-01", 1, "9.5", "CB(7,50)/Cash(2,00)"),
                    ticket("2026-06-02", 2, "4.5", "CB(4,50)"),
                ]
            )
        )
        self.assertIn("Paiements lus : 14,00 € sur 2 jour(s), 2 ticket(s) (Carte 12,00 €, Espèces 2,00 €).", said)

    def test_the_amounts_are_grouped_by_thousands(self):
        """The log is read on the sales page: « 2 600,00 € », a no-break
        space between the thousands, the comma decimals kept."""
        nbsp = "\N{NO-BREAK SPACE}"
        said = " ".join(
            payments_log(
                parse_payment_rows(
                    [
                        TICKET_HEADER,
                        ticket("2026-06-01", 1, "1250", "CB(1250,00)"),
                        ticket("2026-06-02", 2, "1350", "Avoir(1 350,00)"),
                    ]
                )
            )
        )
        self.assertIn(f"Paiements lus : 2{nbsp}600,00 € sur 2 jour(s)", said)
        self.assertIn(f"Carte 1{nbsp}250,00 €", said)
        self.assertIn(f"Avoir 1{nbsp}350,00 €", said)

    def test_everything_unusual_is_named(self):
        said = " ".join(
            payments_log(
                parse_payment_rows(
                    [
                        TICKET_HEADER,
                        ticket("2026-06-01", 1, "9.5", "CB(7,50"),
                        ticket("2026-06-01", 2, "4.5", ""),
                        ticket("2026-06-01", 3, "4.5", "CB(6,00)"),
                        ticket("2026-06-01", 3, "4.5", "CB(6,00)"),
                    ]
                )
            )
        )
        self.assertIn("1 ticket(s) aux paiements illisibles", said)
        self.assertIn("« Illisible »", said)
        self.assertIn("1 ticket(s) sans paiement mais avec un total", said)
        self.assertIn("« Sans paiement enregistré »", said)
        self.assertIn("1 ticket(s) dont les paiements ne font pas", said)
        self.assertIn("1 ticket(s) en double", said)

    def test_a_file_with_no_ticket_sheet_says_so(self):
        said = " ".join(payments_log(ParsedExport(payment_sheets_missing=1)))
        self.assertIn("ne porte pas la feuille des tickets", said)
        self.assertIn("ceux déjà enregistrés restent", said)

    def test_a_ticket_sheet_that_did_not_read_is_named(self):
        said = " ".join(payments_log(ParsedExport(payment_sheet_errors=["ventes.xlsx : feuille abîmée"])))
        self.assertIn("Feuille des tickets illisible", said)
        self.assertIn("ventes.xlsx : feuille abîmée", said)
        self.assertNotIn("ne porte pas", said)


class ImportJobTests(TestCase):
    """The import job writes the payments after the sales. The download is
    stubbed: it hands over a workbook written here."""

    def run_job(self, path):
        job = SalesImportJob.objects.create(status=SalesImportJob.Status.PENDING)
        with mock.patch("recipes.tasks.download_sales_lines", return_value=[path]):
            import_laddition_sales_task(job.pk, june(1), june(30), download_dir="non-utilisé")
        job.refresh_from_db()
        return job

    def test_the_job_writes_the_payments_and_says_so(self):
        path = write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "9.5", "CB(7,50)/Cash(2,00)"), TOTAL_ROW],
        )
        job = self.run_job(path)
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS)
        self.assertEqual(stored(), [(june(1), CB, Decimal("7.50"), 1), (june(1), CASH, Decimal("2.00"), 1)])
        self.assertIn("Paiements lus : 9,50 € sur 1 jour(s), 1 ticket(s)", job.log)
        self.assertIn("Paiements enregistrés : 1 jour(s) de caisse remplacé(s), 0 déjà à jour.", job.log)
        # The sales went in all the same.
        self.assertEqual(PosProductDailyQuantity.objects.get().quantity, 1)

    def test_a_ticket_sheet_that_does_not_read_leaves_the_sales_and_the_stored_payments(self):
        PosDailyPayment.objects.create(sold_on=june(1), method=CB, amount=Decimal("5.00"), payments=1)
        path = write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            tickets=[["Jour", "Total TTC"], ["2026-06-01", "7.5"]],
        )
        job = self.run_job(path)
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS)
        self.assertEqual(PosProductDailyQuantity.objects.get().revenue_ttc, Decimal("7.50"))
        self.assertEqual(stored(), [(june(1), CB, Decimal("5.00"), 1)])
        self.assertIn("Feuille des tickets illisible", job.log)
        self.assertNotIn("Paiements enregistrés", job.log)

    def test_a_ticket_sheet_that_does_not_inflate_leaves_the_sales_in(self):
        path = with_a_damaged_ticket_sheet(
            write_workbook(
                [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
                tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")],
            )
        )
        job = self.run_job(path)
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS, job.log)
        self.assertEqual(PosProductDailyQuantity.objects.get().quantity, 1)
        self.assertIn("Feuille des tickets illisible", job.log)
        self.assertEqual(stored(), [])


class ImportCommandTests(TestCase):
    """`manage.py laddition_import --file`: nothing is downloaded."""

    def setUp(self):
        self.path = write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")],
        )
        forbidden = mock.patch(
            "recipes.management.commands.laddition_import.download_sales_lines",
            side_effect=AssertionError("rien ne se télécharge dans un test"),
        )
        forbidden.start()
        self.addCleanup(forbidden.stop)

    def run_command(self, *args):
        out = StringIO()
        call_command("laddition_import", "--file", self.path, *args, stdout=out)
        return out.getvalue()

    def test_the_command_writes_the_payments(self):
        output = self.run_command()
        self.assertEqual(stored(), [(june(1), CB, Decimal("7.50"), 1)])
        self.assertIn("Paiements lus : 7,50 €", output)
        self.assertIn("Recorded the payments of 1 till day(s)", output)

    def test_the_dry_run_reads_them_and_writes_nothing(self):
        output = self.run_command("--dry-run")
        self.assertEqual(stored(), [])
        self.assertIn("Paiements lus : 7,50 €", output)
        self.assertNotIn("Recorded the payments", output)
        self.assertEqual(PosProductDailyQuantity.objects.count(), 0)

    def test_the_command_writes_the_days_sales_with_their_payments(self):
        """One rule for every writer: a day's payments only beside the day
        « Ventes » holds. The command wrote the payments and never the till
        products' days (the job does both), so a day stored its card and
        cash with no takings behind them - the very day the backfill
        refuses to write, and its « Remplacer » prunes."""
        output = self.run_command()
        self.assertEqual(
            list(PosProductDailyQuantity.objects.values_list("product__name", "sold_on", "revenue_ttc")),
            [("Pinte Exemple", june(1), Decimal("7.50"))],
        )
        self.assertIn("1 till product(s) seen.", output)
        out = StringIO()
        call_command("laddition_backfill_payments", "--folder", str(Path(self.path).parent), "--dry-run", stdout=out)
        self.assertIn("1 jour(s) déjà à jour.", out.getvalue())
        self.assertNotIn("sans ventes enregistrées", out.getvalue())


class BackfillPaymentsCommandTests(TestCase):
    """`manage.py laddition_backfill_payments` over the exports on disk."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        lines = [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")]
        write_workbook(
            lines,
            folder=self.folder,
            name="export-01.xlsx",
            tickets=[
                TICKET_HEADER,
                ticket("2026-06-01", 1, "7.5", "CB(7,50)"),
                ticket("2026-06-01", 2, "3.5", "Cash(3,50)"),
                ticket("2026-06-02", 3, "9.5", "CB(7,50)/Cash(2,00)"),
                TOTAL_ROW,
            ],
        )
        # A second download whose window overlaps the first one's last day,
        # and runs a day past what « Ventes » holds.
        write_workbook(
            lines,
            folder=self.folder,
            name="export-02.xlsx",
            tickets=[
                TICKET_HEADER,
                ticket("2026-06-02", 3, "9.5", "CB(7,50)/Cash(2,00)"),
                ticket("2026-06-03", 4, "7.5", "CB(7,50)"),
                TOTAL_ROW,
            ],
        )
        product = PosProduct.objects.create(name="Pinte Exemple", total_quantity=2)
        for day in (1, 2):
            PosProductDailyQuantity.objects.create(product=product, sold_on=june(day), quantity=1)

    def run_command(self, *args):
        out = StringIO()
        call_command("laddition_backfill_payments", "--folder", self.folder, *args, stdout=out)
        return out.getvalue()

    def test_the_dry_run_writes_nothing(self):
        output = self.run_command("--dry-run")
        self.assertIn("rien n'est enregistré", output)
        self.assertEqual(stored(), [])

    def test_the_dry_run_says_what_it_would_do(self):
        output = self.run_command("--dry-run")
        self.assertIn("export-01.xlsx : 3 ticket(s), du 01/06/2026 au 02/06/2026, 20,50 € payés.", output)
        self.assertIn("export-02.xlsx", output)
        self.assertIn("2 jour(s) de caisse à remplir.", output)

    def test_it_prints_the_payments_per_year_and_per_method(self):
        """The figures a person checks the reading against by hand - as
        read, before the days « Ventes » does not hold are set aside."""
        output = self.run_command("--dry-run")
        self.assertIn("2026 : 28,00 € (Carte 22,50 €, Espèces 5,50 €)", output)
        self.assertIn("Carte : 22,50 € en 3 paiement(s)", output)
        self.assertIn("Espèces : 5,50 € en 2 paiement(s)", output)

    def test_it_fills_only_the_days_the_sales_hold(self):
        """Money for a day « Ventes » does not hold would be a figure the
        sales pages contradict: said, never written."""
        output = self.run_command()
        self.assertEqual(
            stored(),
            [
                (june(1), CB, Decimal("7.50"), 1),
                (june(1), CASH, Decimal("3.50"), 1),
                (june(2), CB, Decimal("7.50"), 1),
                (june(2), CASH, Decimal("2.00"), 1),
            ],
        )
        self.assertIn("1 jour(s) sans ventes enregistrées ici, 7,50 € payés", output)
        self.assertIn("le 03/06/2026 : 7,50 €", output)

    def test_a_day_read_from_two_files_is_not_counted_twice(self):
        self.run_command()
        self.assertEqual(sum(amount for day, _m, amount, _c in stored() if day == june(2)), Decimal("9.50"))

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.run_command()
        before = list(PosDailyPayment.objects.values_list("pk", "sold_on", "method", "amount", "payments"))
        output = self.run_command()
        self.assertEqual(
            list(PosDailyPayment.objects.values_list("pk", "sold_on", "method", "amount", "payments")), before
        )
        self.assertIn("0 jour(s) de caisse à remplir.", output)
        self.assertIn("2 jour(s) déjà à jour.", output)

    def test_two_files_that_disagree_about_a_day_say_so_and_the_last_one_wins_whole(self):
        write_workbook(
            [HEADER, line("2026-06-02", "Pinte Exemple", "7.50", "20%")],
            folder=self.folder,
            name="export-03.xlsx",
            tickets=[TICKET_HEADER, ticket("2026-06-02", 3, "9.5", "CB(9,50)")],
        )
        output = self.run_command()
        self.assertIn("lus différemment selon le fichier", output)
        self.assertIn("le 02/06/2026 : Carte 7,50 €, Espèces 2,00 € puis Carte 9,50 €", output)
        self.assertEqual([row for row in stored() if row[0] == june(2)], [(june(2), CB, Decimal("9.50"), 1)])

    def test_an_unreadable_file_is_named_and_the_others_still_fill(self):
        (Path(self.folder) / "export-00-moitie.xlsx").write_bytes(b"pas un zip du tout")
        output = self.run_command()
        self.assertIn("export-00-moitie.xlsx : illisible", output)
        self.assertEqual(len(stored()), 4)

    def test_a_file_whose_ticket_sheet_does_not_inflate_is_named_and_the_others_still_fill(self):
        """zlib.error went past the « illisible » branch: one such file
        stopped the folder with a traceback, the good files unwritten."""
        with_a_damaged_ticket_sheet(
            write_workbook(
                [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
                folder=self.folder,
                name="export-00-abime.xlsx",
                tickets=[TICKET_HEADER, ticket("2026-06-01", 1, "7.5", "CB(7,50)")],
            )
        )
        output = self.run_command()
        self.assertIn("export-00-abime.xlsx : illisible", output)
        self.assertEqual(len(stored()), 4)

    def test_a_day_that_paid_nothing_is_not_sent_back_to_the_import(self):
        """Every ticket of 04/06 comped, and no line on 04/06: no import can
        ever give it a day in « Ventes », and there is nothing to write on
        it. Counted among the days « left aside », it asked the owner on
        every run to import again what no import can bring."""
        write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            folder=self.folder,
            name="export-03.xlsx",
            tickets=[TICKET_HEADER, ticket("2026-06-04", 5, "0", ""), ticket("2026-06-04", 6, "0", "")],
        )
        output = self.run_command()
        self.assertIn("1 jour(s) sans ventes enregistrées ici, 7,50 € payés", output)
        self.assertNotIn("le 04/06/2026", output)
        self.assertIn(
            "1 jour(s) lus sans rien de payé (tickets à 0 €) et sans ventes enregistrées : rien à y écrire.", output
        )
        self.assertEqual([row for row in stored() if row[0] == june(4)], [])

    def test_with_only_such_days_the_import_is_not_asked_for(self):
        folder = tempfile.mkdtemp()
        write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            folder=folder,
            name="offerts.xlsx",
            tickets=[TICKET_HEADER, ticket("2026-06-04", 5, "0", "")],
        )
        out = StringIO()
        call_command("laddition_backfill_payments", "--folder", folder, stdout=out)
        self.assertNotIn("Relancez l'import", out.getvalue())
        self.assertNotIn("sans ventes enregistrées ici", out.getvalue())

    def test_an_export_without_the_ticket_sheet_is_said_as_such(self):
        write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")], folder=self.folder, name="ancien.xlsx"
        )
        output = self.run_command("--dry-run")
        self.assertIn("ancien.xlsx : pas de feuille des tickets", output)

    def test_a_folder_with_no_payments_at_all_says_so(self):
        folder = tempfile.mkdtemp()
        write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")], folder=folder, name="ancien.xlsx")
        out = StringIO()
        call_command("laddition_backfill_payments", "--folder", folder, stdout=out)
        self.assertIn("Aucun paiement lu", out.getvalue())
        self.assertEqual(stored(), [])

    def test_what_is_unusual_in_a_file_is_named_under_it(self):
        write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            folder=self.folder,
            name="export-03.xlsx",
            tickets=[TICKET_HEADER, ticket("2026-06-01", 9, "4.5", "CB(4,50")],
        )
        output = self.run_command("--dry-run")
        self.assertIn("1 ticket(s) aux paiements illisibles", output)

    def test_without_a_folder_it_reads_the_tenant_s_downloads_folder(self):
        out = StringIO()
        root = tempfile.mkdtemp(prefix="marginmate-tests-tenants-")
        self.addCleanup(shutil.rmtree, root, True)
        with override_settings(TENANTS_ROOT=root):
            shutil.copytree(self.folder, paths.downloads_dir(), dirs_exist_ok=True)
            call_command("laddition_backfill_payments", "--dry-run", stdout=out)
        self.assertIn("export-01.xlsx", out.getvalue())
