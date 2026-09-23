"""What the till's money columns promise.

A sale's own amount is the only figure in this whole application that says
what came IN; everything else says what went out. So these tests pin how it
is read, line by line, from the shape L'Addition's export really has - and
above all the two ways it could be silently wrong:

- an amount multiplied by its quantity (the export prints one row per item
  rung up, its amount already the line's own, so a « x Qte » would double
  every line the day the till finally writes a 2 somewhere);
- an HT worked out at one rate for a day that mixed food at 10 % and drink
  at 20 %, which no total on any page would contradict.

Data invented throughout: every product name, price and day below is made
up, and the workbooks are built by hand. Nothing is downloaded, nothing is
read from scraped_invoices/.
"""

from __future__ import annotations

import tempfile
import zipfile
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from recipes.models import PosProduct, PosProductDailyQuantity
from recipes.pos.laddition_xlsx import (
    LadditionExportError,
    ParsedExport,
    parse_rows,
    parse_sales_export,
    parse_sales_exports,
)
from recipes.tasks import money_log, sync_pos_products

#: The columns this reader cares about, in the order the real export puts
#: them in (Jour first, Taux last) - they are found by name, but a fixture
#: that reorders them for convenience tests a file nobody sends.
HEADER = ["Jour", "Nom", "Qte", "Prix TTC", "Remises TTC", "TAG_Offered", "Offerts TTC", "Taux"]
#: The same export before the money columns existed - an older download.
BARE_HEADER = ["Jour", "Nom", "Qte", "TAG_Offered"]

CONTENT_TYPES = """<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
</Types>"""

WORKBOOK = """<?xml version="1.0"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="SalesDocumentLines" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>"""


def _letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _cell(ref: str, value) -> str:
    """A money cell is numeric in the real export and a label is a string -
    and the whole reason this package has its own reader is that the export
    puts text in numeric cells anyway, so which is which has to be faithful."""
    text = str(value)
    try:
        Decimal(text)
    except Exception:  # noqa: BLE001 - a label, then
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'
    return f'<c r="{ref}"><v>{escape(text)}</v></c>'


def write_workbook(rows, folder=None, name="ventes.xlsx") -> str:
    """One .xlsx holding just the SalesDocumentLines sheet, header first."""
    body = ""
    for number, row in enumerate(rows, start=1):
        cells = "".join(_cell(f"{_letter(at)}{number}", value) for at, value in enumerate(row))
        body += f'<row r="{number}">{cells}</row>'
    sheet = (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData></worksheet>"
    )
    path = str(Path(folder or tempfile.mkdtemp()) / name)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def line(day, name, price, rate, quantity=1, discount="0.00", offered="NON", offered_ttc="0.00"):
    return [day, name, quantity, price, discount, offered, offered_ttc, rate]


class MoneyColumnsTests(SimpleTestCase):
    """Reading `Prix TTC`, `Remises TTC` and `Taux` off the rows."""

    def parse(self, rows, header=None):
        return parse_rows([header or HEADER, *rows])

    def money(self, result, name, day):
        return result.money[(name, day)]

    def test_a_line_carries_its_own_amount_and_its_own_ht(self):
        result = self.parse([line("2026-06-01", "Pinte Exemple", "7.50", "20%")])
        money = self.money(result, "Pinte Exemple", date(2026, 6, 1))
        self.assertEqual(money.revenue_ttc, Decimal("7.50"))
        self.assertEqual(money.revenue_ht, Decimal("6.25"))

    def test_a_day_mixing_two_rates_is_worked_out_per_rate(self):
        """A day of food at 10 % and drink at 20 % read at one rate is wrong
        by cents a line and by nothing anyone can see."""
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
            line("2026-06-01", "Soda Exemple", "3.50", "10%"),
        ])
        self.assertEqual(result.revenue_ttc, Decimal("11.00"))
        self.assertEqual(result.revenue_ht, Decimal("9.43"))  # 6.25 + 3.18
        self.assertEqual(self.money(result, "Soda Exemple", date(2026, 6, 1)).revenue_ht, Decimal("3.18"))

    def test_the_ht_is_rounded_once_per_rate_not_once_per_line(self):
        """Three sodas at 3,50 make 10,50 TTC, which is 9,55 HT at 10 %.
        Rounded line by line they make 9,54, and a year of them drifts away
        from the VAT return by euros."""
        result = self.parse([line("2026-06-01", "Soda Exemple", "3.50", "10%")] * 3)
        self.assertEqual(self.money(result, "Soda Exemple", date(2026, 6, 1)).revenue_ht, Decimal("9.55"))

    def test_the_amount_is_never_multiplied_by_the_quantity(self):
        """`Prix TTC` is the line's own amount. It has been 1 x something on
        every row ever exported, so a « x Qte » would look right for years
        and double the first line that isn't."""
        result = self.parse([line("2026-06-01", "Planche Exemple", "18.00", "10%", quantity=3)])
        money = self.money(result, "Planche Exemple", date(2026, 6, 1))
        self.assertEqual(money.revenue_ttc, Decimal("18.00"))
        self.assertEqual(result.entries, [("Planche Exemple", date(2026, 6, 1), 3)])

    def test_a_comped_line_is_already_priced_zero(self):
        """Its stock left the shelf and no money came in - which is exactly
        what a margin wants. What it would have cost sits in `Offerts TTC`
        and is not revenue."""
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
            line("2026-06-01", "Pinte Exemple", "0", "20%", offered="OUI", offered_ttc="7.50"),
        ])
        money = self.money(result, "Pinte Exemple", date(2026, 6, 1))
        self.assertEqual(money.revenue_ttc, Decimal("7.50"))
        self.assertEqual(result.entries, [("Pinte Exemple", date(2026, 6, 1), 2)])
        self.assertEqual(result.offered, 1)

    def test_a_refund_keeps_its_minus_sign_and_its_rate(self):
        """A returned deposit prints Qte -1, a negative amount AND a rate of
        « -20% » - the minus belongs to the money, not to the rate, and a
        reader taking « -20% » for a rate France does not have would drop
        that line's HT and report it as unreadable."""
        result = self.parse([line("2026-06-21", "Consigne Exemple", "-1.50", "-20%", quantity="-1")])
        money = self.money(result, "Consigne Exemple", date(2026, 6, 21))
        self.assertEqual(money.revenue_ttc, Decimal("-1.50"))
        self.assertEqual(money.revenue_ht, Decimal("-1.25"))
        self.assertEqual(result.lines_without_rate, 0)

    def test_a_discount_comes_off_the_line_and_is_counted(self):
        """`Remises TTC` has been 0.00 on every row ever exported, which is
        exactly the column that will fire one day with nobody watching."""
        result = self.parse([line("2026-06-01", "Pinte Exemple", "7.50", "20%", discount="1.50")])
        self.assertEqual(self.money(result, "Pinte Exemple", date(2026, 6, 1)).revenue_ttc, Decimal("6.00"))
        self.assertEqual(result.discounted_lines, 1)
        self.assertEqual(result.discount_ttc, Decimal("1.50"))

    def test_a_rate_that_cannot_be_read_leaves_that_line_without_an_ht(self):
        """Never 20 % by default: assuming the drink rate on a plate of food
        overstates the margin and nothing downstream could tell."""
        result = self.parse([line("2026-06-01", "Planche Exemple", "18.00", "n/a")])
        money = self.money(result, "Planche Exemple", date(2026, 6, 1))
        self.assertEqual(money.revenue_ttc, Decimal("18.00"))
        self.assertEqual(money.revenue_ht, Decimal("0"))
        self.assertEqual(money.without_rate_ttc, Decimal("18.00"))
        self.assertEqual(result.lines_without_rate, 1)

    def test_a_rate_france_does_not_have_is_not_trusted(self):
        result = self.parse([line("2026-06-01", "Planche Exemple", "18.00", "26%")])
        self.assertEqual(result.lines_without_rate, 1)
        self.assertEqual(result.revenue_ht, Decimal("0"))

    def test_a_missing_rate_column_leaves_every_ht_unknown(self):
        header = [column for column in HEADER if column != "Taux"]
        row = line("2026-06-01", "Pinte Exemple", "7.50", "20%")[:-1]
        result = parse_rows([header, row])
        self.assertEqual(result.revenue_ttc, Decimal("7.50"))
        self.assertEqual(result.revenue_ht, Decimal("0"))
        self.assertEqual(result.lines_without_rate, 1)

    def test_an_export_with_no_money_columns_at_all_still_imports(self):
        """An older download. It must read as « no money in this file »,
        never as « this day took 0 € »."""
        result = parse_rows([BARE_HEADER, ["2026-06-01", "Pinte Exemple", "2", "NON"]])
        self.assertEqual(result.entries, [("Pinte Exemple", date(2026, 6, 1), 2)])
        self.assertFalse(result.money_columns)
        self.assertEqual(result.money, {})

    def test_an_unreadable_amount_is_counted_rather_than_guessed(self):
        result = self.parse([line("2026-06-01", "Pinte Exemple", "sept euros", "20%")])
        self.assertEqual(result.lines_without_amount, 1)
        self.assertEqual(result.revenue_ttc, Decimal("0"))

    def test_the_total_row_carries_no_money(self):
        """The export's own last row has a « - » where its day, its name and
        its amounts should be."""
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
            ["-", "-", "", "", "", "-", "", "-"],
        ])
        self.assertEqual(result.revenue_ttc, Decimal("7.50"))
        self.assertEqual(result.skipped, 1)


class SeveralFilesTests(SimpleTestCase):
    """Two downloads whose windows overlap - what a folder of them is."""

    def test_a_day_read_from_two_files_is_that_day_once(self):
        first = write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")])
        second = write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")])
        result = parse_sales_exports([first, second])
        self.assertEqual(result.revenue_ttc, Decimal("7.50"))
        self.assertEqual(result.total_quantity, 1)
        self.assertEqual(result.repeated_days, 1)

    def test_two_windows_that_do_not_overlap_add_up(self):
        first = write_workbook([HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")])
        second = write_workbook([HEADER, line("2026-06-02", "Pinte Exemple", "7.50", "20%")])
        result = parse_sales_exports([first, second])
        self.assertEqual(result.revenue_ttc, Decimal("15.00"))
        self.assertEqual(result.total_quantity, 2)
        self.assertEqual(result.repeated_days, 0)

    def test_one_file_read_whole(self):
        path = write_workbook([
            HEADER,
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
            line("2026-06-01", "Soda Exemple", "3.50", "10%"),
        ])
        result = parse_sales_export(path)
        self.assertEqual(result.revenue_ttc, Decimal("11.00"))
        self.assertEqual(result.revenue_ht, Decimal("9.43"))


class SyncRevenueTests(TestCase):
    """What the live import writes on PosProductDailyQuantity."""

    def export(self, rows):
        return parse_rows([HEADER, *rows])

    def test_a_day_keeps_the_money_it_took(self):
        sync_pos_products(self.export([line("2026-06-01", "Pinte Exemple", "7.50", "20%")]))
        day = PosProductDailyQuantity.objects.get()
        self.assertEqual(day.quantity, 1)
        self.assertEqual(day.revenue_ttc, Decimal("7.50"))
        self.assertEqual(day.revenue_ht, Decimal("6.25"))
        self.assertTrue(day.revenue_read)

    def test_importing_the_same_day_twice_corrects_it_rather_than_doubling(self):
        """The same guarantee the quantity already had - see
        PosProductDailyQuantity's docstring for what the naive version did."""
        rows = [
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
        ]
        sync_pos_products(self.export(rows))
        sync_pos_products(self.export(rows))
        day = PosProductDailyQuantity.objects.get()
        self.assertEqual(day.quantity, 2)
        self.assertEqual(day.revenue_ttc, Decimal("15.00"))

    def test_an_overlapping_import_replaces_the_shared_day_and_adds_the_new_one(self):
        sync_pos_products(self.export([
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
            line("2026-06-02", "Pinte Exemple", "7.50", "20%"),
        ]))
        sync_pos_products(self.export([
            line("2026-06-02", "Pinte Exemple", "7.50", "20%"),
            line("2026-06-03", "Pinte Exemple", "7.50", "20%"),
        ]))
        product = PosProduct.objects.get()
        self.assertEqual(product.total_quantity, 3)
        self.assertEqual(
            sum(row.revenue_ttc for row in PosProductDailyQuantity.objects.all()), Decimal("22.50")
        )

    def test_a_day_whose_money_was_never_read_says_so(self):
        """Not « this day took 0 € » - the margin page has to be able to tell
        the two apart, or a backfill that has not run yet reads as a bar that
        sold nothing."""
        sync_pos_products(ParsedExport(
            products={"Pinte Exemple": {"quantity": 2, "category": "", "typology": "",
                                        "first": date(2026, 6, 1), "last": date(2026, 6, 1)}},
            entries=[("Pinte Exemple", date(2026, 6, 1), 2)],
        ))
        day = PosProductDailyQuantity.objects.get()
        self.assertFalse(day.revenue_read)
        self.assertEqual(day.revenue_ttc, Decimal("0"))

    def test_an_export_with_no_money_columns_does_not_erase_what_was_read(self):
        """Re-importing an older download over a backfilled day must not
        quietly zero it."""
        sync_pos_products(self.export([line("2026-06-01", "Pinte Exemple", "7.50", "20%")]))
        sync_pos_products(parse_rows([BARE_HEADER, ["2026-06-01", "Pinte Exemple", "1", "NON"]]))
        day = PosProductDailyQuantity.objects.get()
        self.assertEqual(day.revenue_ttc, Decimal("7.50"))
        self.assertTrue(day.revenue_read)


class ImportLogTests(TestCase):
    """What the import says it read. Nothing is downloaded: the download and
    the file are both stubbed, and the rows are the hand-written ones above."""

    def run_import(self, rows, header=None):
        from unittest import mock

        from recipes.models import SalesImportJob
        from recipes.tasks import import_laddition_sales_task

        export = parse_rows([header or HEADER, *rows])
        job = SalesImportJob.objects.create(status=SalesImportJob.Status.PENDING)
        with (
            mock.patch("recipes.tasks.download_sales_lines", return_value=["ventes.xlsx"]),
            mock.patch("recipes.tasks.parse_sales_exports", return_value=export),
        ):
            import_laddition_sales_task(job.pk, date(2026, 6, 1), date(2026, 6, 30), download_dir="non-utilisé")
        job.refresh_from_db()
        return job.log

    def test_the_log_says_what_came_in(self):
        log = self.run_import([
            line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
            line("2026-06-01", "Soda Exemple", "3.50", "10%"),
        ])
        self.assertIn("Recettes lues : 11,00 € TTC, 9,43 € HT.", log)

    def test_a_file_with_no_price_column_says_so_rather_than_reading_zero(self):
        log = self.run_import([["2026-06-01", "Pinte Exemple", "2", "NON"]], header=BARE_HEADER)
        self.assertIn("ne porte pas les colonnes de prix", log)

    def test_the_log_names_a_rate_it_could_not_read(self):
        log = self.run_import([line("2026-06-01", "Planche Exemple", "18.00", "n/a")])
        self.assertIn("1 ligne(s) sans taux lisible", log)
        self.assertIn("aucun taux supposé", log)

    def test_the_log_names_a_quantity_the_till_has_never_printed(self):
        """The amount read stays the line's own; the import says the line
        exists rather than deciding what a « 3 » means."""
        log = self.run_import([line("2026-06-01", "Planche Exemple", "18.00", "10%", quantity=3)])
        self.assertIn("quantité autre que 1", log)


class BackfillCommandTests(TestCase):
    """`manage.py laddition_backfill_revenue` over the exports on disk."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        write_workbook(
            [HEADER,
             line("2026-06-01", "Pinte Exemple", "7.50", "20%"),
             line("2026-06-01", "Soda Exemple", "3.50", "10%"),
             line("2026-06-02", "Pinte Exemple", "7.50", "20%")],
            folder=self.folder, name="export-01.xlsx",
        )
        # A second download whose window overlaps the first one's last day.
        write_workbook(
            [HEADER,
             line("2026-06-02", "Pinte Exemple", "7.50", "20%"),
             line("2026-06-03", "Pinte Exemple", "7.50", "20%")],
            folder=self.folder, name="export-02.xlsx",
        )
        self.pinte = PosProduct.objects.create(name="Pinte Exemple", total_quantity=3)
        self.soda = PosProduct.objects.create(name="Soda Exemple", total_quantity=1)
        for day in (1, 2, 3):
            PosProductDailyQuantity.objects.create(
                product=self.pinte, sold_on=date(2026, 6, day), quantity=1
            )
        PosProductDailyQuantity.objects.create(product=self.soda, sold_on=date(2026, 6, 1), quantity=1)

    def run_command(self, *args):
        out = StringIO()
        call_command("laddition_backfill_revenue", "--folder", self.folder, *args, stdout=out)
        return out.getvalue()

    def test_the_dry_run_writes_nothing(self):
        output = self.run_command("--dry-run")
        self.assertIn("rien n'est enregistré", output)
        self.assertEqual(PosProductDailyQuantity.objects.filter(revenue_read=True).count(), 0)
        self.assertEqual(
            sum(row.revenue_ttc for row in PosProductDailyQuantity.objects.all()), Decimal("0")
        )

    def test_the_dry_run_says_what_it_would_do(self):
        output = self.run_command("--dry-run")
        self.assertIn("export-01.xlsx", output)
        self.assertIn("export-02.xlsx", output)
        self.assertIn("01/06/2026", output)
        self.assertIn("2026 :", output)  # the revenue per year
        self.assertIn("4 ligne(s) (produit, jour) à remplir", output)

    def test_a_day_read_from_two_files_is_not_counted_twice(self):
        self.run_command()
        second = PosProductDailyQuantity.objects.get(product=self.pinte, sold_on=date(2026, 6, 2))
        self.assertEqual(second.revenue_ttc, Decimal("7.50"))
        self.assertEqual(
            sum(row.revenue_ttc for row in PosProductDailyQuantity.objects.all()), Decimal("26.00")
        )

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.run_command()
        before = sorted(PosProductDailyQuantity.objects.values_list("pk", "revenue_ttc", "revenue_ht"))
        self.run_command()
        self.assertEqual(
            sorted(PosProductDailyQuantity.objects.values_list("pk", "revenue_ttc", "revenue_ht")), before
        )

    def test_a_day_with_no_row_here_is_reported_never_created(self):
        """The command fills what the import already recorded; inventing a
        day nobody imported would put money against a quantity nobody
        counted."""
        PosProductDailyQuantity.objects.filter(product=self.soda).delete()
        output = self.run_command()
        self.assertEqual(PosProductDailyQuantity.objects.filter(product=self.soda).count(), 0)
        self.assertIn("Soda Exemple", output)
        self.assertIn("sans correspondance", output)

    def test_what_matches_nothing_is_said_in_money_as_well_as_in_rows(self):
        """« 12 lignes sans correspondance » does not say whether that is a
        coffee or a fortnight of takings."""
        PosProductDailyQuantity.objects.filter(product=self.soda).delete()
        output = self.run_command("--dry-run")
        self.assertIn("3,50 € TTC", output)

    def test_a_till_product_unknown_here_is_reported(self):
        write_workbook(
            [HEADER, line("2026-06-04", "Produit Inconnu", "4.00", "10%")],
            folder=self.folder, name="export-03.xlsx",
        )
        output = self.run_command()
        self.assertEqual(PosProduct.objects.filter(name="Produit Inconnu").count(), 0)
        self.assertIn("Produit Inconnu", output)

    def test_the_revenue_per_year_is_the_sum_of_the_days(self):
        output = self.run_command()
        self.assertIn("2026", output)
        self.assertEqual(
            sum(row.revenue_ttc for row in PosProductDailyQuantity.objects.all()), Decimal("26.00")
        )

    def test_the_revenue_per_year_is_printed_in_euros_both_ways(self):
        """The per-year figures are what the run is checked against by hand:
        the reading was confirmed by a year's total matching what the owner
        knows the bar took (the number itself stays out of this repository,
        which is public). « 2026 : » alone is a heading; the amounts are the
        report."""
        output = self.run_command("--dry-run")

        self.assertIn("2026 : 26,00 € TTC / 21,93 € HT", output)

    def test_two_files_that_disagree_about_a_day_say_so_and_the_last_one_wins(self):
        """No two stored exports disagree today, which is exactly why this is
        pinned: a day quietly taking the first reading, or the second, or the
        two added together, looks the same on screen. The rule is « the last
        file read wins, and the page says which days that happened on » -
        averaged or summed, nothing would ever say a figure had two readings."""
        write_workbook(
            [HEADER, line("2026-06-02", "Pinte Exemple", "9.00", "20%")],
            folder=self.folder, name="export-03.xlsx",
        )

        output = self.run_command()

        self.assertIn("lus différemment selon le fichier", output)
        self.assertIn("Pinte Exemple", output)
        self.assertIn("7,50 €", output)
        self.assertIn("9,00 €", output)
        second = PosProductDailyQuantity.objects.get(product=self.pinte, sold_on=date(2026, 6, 2))
        self.assertEqual(second.revenue_ttc, Decimal("9.00"))

    def test_a_workbook_that_is_not_an_export_is_named_and_the_others_still_fill(self):
        """The folder is scanned whole, so it holds whatever else has been
        downloaded into it. One file that is not the « Lignes de ventes »
        export must be said and stepped over - stopping there would leave
        the other sixteen unread, and skipping it silently would lose a real
        export the day one is renamed."""
        write_workbook(
            [HEADER, line("2026-06-01", "Pinte Exemple", "7.50", "20%")],
            folder=self.folder, name="export-autre.xlsx",
        )
        path = Path(self.folder) / "export-autre.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("xl/workbook.xml", WORKBOOK.replace("SalesDocumentLines", "ProductAnalytics"))
            archive.writestr("xl/_rels/workbook.xml.rels", RELS)
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData/></worksheet>',
            )

        output = self.run_command()

        self.assertIn("export-autre.xlsx : illisible", output)
        self.assertEqual(
            sum(row.revenue_ttc for row in PosProductDailyQuantity.objects.all()), Decimal("26.00")
        )

    def test_without_a_folder_it_reads_the_downloads_folder(self):
        out = StringIO()
        with override_settings(SCRAPE_DOWNLOAD_DIR=self.folder):
            call_command("laddition_backfill_revenue", "--dry-run", stdout=out)
        self.assertIn("export-01.xlsx", out.getvalue())


class ADiscountThatWouldGrowTheLineTests(SimpleTestCase):
    """`Remises TTC` is 0,00 € on every line stored, so which way the
    till writes one has never been observed.

    Subtracted blind, a discount printed as -1,50 € against a 6,00 € line
    gives 7,50 € - more revenue than the gross price, which cannot happen.
    The other plausible reading (`Prix TTC` already net) would undercount
    instead. So the arithmetic is checked rather than the sign guessed: a
    discount that makes the line BIGGER is not taken, and is said.
    """

    def parse(self, rows):
        return parse_rows([HEADER, *rows])

    def test_a_discount_that_makes_the_line_bigger_is_not_taken(self):
        result = self.parse([line("2026-06-01", "Pinte Exemple", "6.00", "20%", discount="-1.50")])

        self.assertEqual(result.revenue_ttc, Decimal("6.00"))
        self.assertEqual(result.discounts_not_taken, 1)
        self.assertEqual(result.discounted_lines, 0)
        self.assertEqual(result.discount_ttc, Decimal("0"))

    def test_a_discount_that_makes_it_smaller_is_taken_as_before(self):
        result = self.parse([line("2026-06-01", "Pinte Exemple", "6.00", "20%", discount="1.50")])

        self.assertEqual(result.revenue_ttc, Decimal("4.50"))
        self.assertEqual(result.discounted_lines, 1)
        self.assertEqual(result.discounts_not_taken, 0)

    def test_a_refund_line_may_be_discounted_the_other_way_round(self):
        """Its amount is negative, so the discount that shrinks it is
        negative too: the rule is about SIZE, never about sign."""
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "-6.00", "-20%", quantity=-1, discount="-1.50")
        ])

        self.assertEqual(result.revenue_ttc, Decimal("-4.50"))
        self.assertEqual(result.discounted_lines, 1)

    def test_the_import_log_names_it(self):
        result = self.parse([line("2026-06-01", "Pinte Exemple", "6.00", "20%", discount="-1.50")])

        self.assertIn("1 ligne(s) dont la remise", " ".join(money_log(result)))


class ADayWithALineNobodyCanReadTests(SimpleTestCase):
    """A line whose `Prix TTC` cannot be read leaves that day short of an
    unknown amount.

    Kept as read, the day is filed at what the OTHER lines took - a figure
    that looks perfectly ordinary and is too small by however much the
    unreadable line was. So the whole (produit, jour) goes unread instead:
    its units and its cost still count, its revenue does not, and the page
    already has a banner for exactly that. Never seen on the lines
    stored, which is why it would fire unnoticed.
    """

    def parse(self, rows):
        return parse_rows([HEADER, *rows])

    def test_the_day_is_not_claimed_as_read(self):
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "6.00", "20%"),
            line("2026-06-01", "Pinte Exemple", "sept euros", "20%"),
        ])

        self.assertNotIn(("Pinte Exemple", date(2026, 6, 1)), result.money)
        self.assertEqual(result.lines_without_amount, 1)
        self.assertEqual(result.days_without_amount, 1)

    def test_the_other_days_of_the_same_product_are_read(self):
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "sept euros", "20%"),
            line("2026-06-02", "Pinte Exemple", "6.00", "20%"),
        ])

        self.assertEqual(result.money[("Pinte Exemple", date(2026, 6, 2))].revenue_ttc, Decimal("6.00"))
        self.assertEqual(result.revenue_ttc, Decimal("6.00"))

    def test_the_units_are_still_counted(self):
        """The stock left the shelf whatever the price column said."""
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "6.00", "20%"),
            line("2026-06-01", "Pinte Exemple", "sept euros", "20%"),
        ])

        self.assertEqual(result.entries, [("Pinte Exemple", date(2026, 6, 1), 2)])

    def test_the_import_log_names_it(self):
        result = self.parse([
            line("2026-06-01", "Pinte Exemple", "6.00", "20%"),
            line("2026-06-01", "Pinte Exemple", "sept euros", "20%"),
        ])

        said = " ".join(money_log(result))

        self.assertIn("1 ligne(s) sans montant lisible", said)
        self.assertIn("1 (produit, jour) laissé(s) non lu(s)", said)

    def test_another_file_reading_that_day_whole_still_fills_it(self):
        """Two overlapping exports: the one that could read every line of
        the day is the one kept."""
        broken = parse_rows([HEADER, line("2026-06-01", "Pinte Exemple", "sept euros", "20%")])
        whole = parse_rows([HEADER, line("2026-06-01", "Pinte Exemple", "6.00", "20%")])
        combined = ParsedExport()
        for part in (broken, whole):
            combined.money.update(part.money)

        self.assertEqual(combined.money[("Pinte Exemple", date(2026, 6, 1))].revenue_ttc, Decimal("6.00"))


class AFileThatIsNotAWorkbookTests(SimpleTestCase):
    """`scraped_invoices/` is scanned whole, so it holds whatever has landed
    in it - a half-finished download, a file renamed .xlsx by hand.

    An .xlsx is a zip; one that is not raises `zipfile.BadZipFile`, which is
    neither `XlsxError` nor `LadditionExportError` and escaped both the
    reader and the backfill's own « illisible » branch. One such file stopped
    the other sixteen exports being read at all, with a traceback.
    """

    def test_a_file_that_is_not_a_zip_is_refused_in_the_readers_own_terms(self):
        folder = tempfile.mkdtemp()
        path = str(Path(folder) / "moitie-telechargee.xlsx")
        with open(path, "wb") as handle:
            handle.write(b"pas un zip du tout")

        with self.assertRaises(LadditionExportError):
            parse_sales_export(path)

    def test_a_folder_missing_that_file_is_not_the_readers_business(self):
        """A path that does not exist is the same kind of answer, not an
        OSError out of the middle of a scan."""
        with self.assertRaises(LadditionExportError):
            parse_sales_export(str(Path(tempfile.mkdtemp()) / "rien.xlsx"))


class ARefundAloneOnADayTests(TestCase):
    """A refund rung up on a day the product did not otherwise sell nets to
    -1 for that (produit, jour).

    The seven refunds in the whole stored history all fall on days the
    product also sold, so the day nets positive and nothing has ever been
    written negative. The real case - sold Tuesday, taken back Wednesday -
    has simply not happened yet, and it used to fail the INSERT on a
    positive-only column:

        django.db.utils.IntegrityError: CHECK constraint failed: quantity

    `sync_pos_products` is one transaction, so that took the WHOLE window
    down with it - the quantities and the money of every other day in the
    import - and re-running it hit the same day again. The money was already
    signed (`revenue_ttc`); the count is now signed too, which is what the
    till actually rang up.
    """

    def setUp(self):
        self.export = parse_rows([
            HEADER,
            line("2026-06-01", "Pinte Exemple", "6.00", "20%"),
            line("2026-06-02", "Pinte Exemple", "-6.00", "-20%", quantity=-1),
        ])

    def test_the_import_goes_through_and_keeps_both_days(self):
        sync_pos_products(self.export)

        rows = PosProductDailyQuantity.objects.order_by("sold_on")

        self.assertEqual(
            [(row.sold_on, row.quantity, row.revenue_ttc) for row in rows],
            [
                (date(2026, 6, 1), 1, Decimal("6.00")),
                (date(2026, 6, 2), -1, Decimal("-6.00")),
            ],
        )

    def test_the_other_days_of_the_import_are_not_lost_with_it(self):
        """The whole point: one transaction, so the refund's day used to take
        every day of the window down with it."""
        sync_pos_products(self.export)

        self.assertEqual(PosProductDailyQuantity.objects.count(), 2)
        self.assertEqual(
            sum(row.revenue_ttc for row in PosProductDailyQuantity.objects.all()), Decimal("0.00")
        )

    def test_the_till_products_total_nets_out_too(self):
        sync_pos_products(self.export)

        self.assertEqual(PosProduct.objects.get(name="Pinte Exemple").total_quantity, 0)

    def test_a_refund_on_a_linked_product_reaches_its_recipe(self):
        """`resync_recipe_from_daily_quantities` sums a recipe's days, so a
        day netting negative arrives at RecipeSale negative too - and stopped
        there on the same constraint."""
        from recipes.models import Recipe, RecipeSale
        from recipes.sales import resync_recipe_from_daily_quantities

        recipe = Recipe.objects.create(
            name="Pinte Exemple", selling_price_ttc=Decimal("6.00"), vat_rate=Decimal("0.20")
        )
        sync_pos_products(self.export)
        PosProduct.objects.filter(name="Pinte Exemple").update(recipe=recipe)
        resync_recipe_from_daily_quantities(recipe)

        self.assertEqual(
            sorted(RecipeSale.objects.values_list("sold_on", "quantity")),
            [(date(2026, 6, 1), 1), (date(2026, 6, 2), -1)],
        )

    def test_a_sale_typed_by_hand_is_still_refused_below_zero(self):
        """Nothing types a refund in by hand - a negative here would be a
        slip, and the till is the only thing that has a reason to write
        one."""
        from recipes.forms import ManualSaleForm
        from recipes.models import Recipe

        recipe = Recipe.objects.create(
            name="Autre recette", selling_price_ttc=Decimal("6.00"), vat_rate=Decimal("0.20")
        )
        form = ManualSaleForm(data={"recipe": recipe.pk, "sold_on": "2026-06-01", "quantity": "-2"})

        self.assertFalse(form.is_valid())
        self.assertIn("quantity", form.errors)
