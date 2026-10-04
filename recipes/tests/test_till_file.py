"""Reading any till's export with a format a person describes
(recipes/pos/till_file.py).

Each test is a way a till's file could be silently wrong money - a day
dropped, a quantity truncated, a rate guessed, a cent lost to a float - or
could reach the database wider than its column. The files are invented,
row for row: no real till's export is in this repository.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from recipes.models import PosDailyPayment, TillFormat
from recipes.pos import till_file
from recipes.pos.till_file import FormatError, TillFileError, check_format, read, read_rate

CB, CASH, CHEQUE, UNREAD = PosDailyPayment.CARD, PosDailyPayment.CASH, PosDailyPayment.CHEQUE, PosDailyPayment.UNREAD


def sales_format(**fields):
    """A « Ventes par produit » format, by titles unless said otherwise."""
    values = {
        "name": "Caisse Exemple",
        "kind": "ventes",
        "encoding": "auto",
        "delimiter": ";",
        "decimal_mark": ",",
        "date_format": "dd/mm/yyyy",
        "service_day_end_hour": 0,
        "sheet": "",
        "day_column": "Date",
        "time_column": "",
        "product_column": "Article",
        "quantity_column": "Qté",
        "amount_column": "Total TTC",
        "amount_ht_column": "",
        "rate_column": "TVA",
        "category_column": "",
        "typology_column": "",
        "method_column": "",
        "paid_column": "",
        "amount_is_unit_price": False,
        "method_map": "",
    }
    values.update(fields)
    return SimpleNamespace(**values)


def payments_format(**fields):
    values = {
        "kind": "paiements",
        "product_column": "",
        "quantity_column": "",
        "amount_column": "",
        "rate_column": "",
        "method_column": "Moyen",
        "paid_column": "Montant",
    }
    values.update(fields)
    return sales_format(**values)


def csv_file(*lines: str, encoding: str = "utf-8") -> io.BytesIO:
    return io.BytesIO("\n".join(lines).encode(encoding))


def reading(lines, fmt=None, *, file_name="export.csv", **kwargs):
    source = lines if isinstance(lines, io.BytesIO) else csv_file(*lines)
    return read(source, check_format(fmt or sales_format()), file_name=file_name, **kwargs)


def refusal(lines, fmt=None, **kwargs) -> str:
    with SimpleTestCase().assertRaises(TillFileError) as caught:
        reading(lines, fmt, **kwargs)
    return str(caught.exception)


HEADER = "Date;Article;Qté;Total TTC;TVA"


# -- xlsx, built by hand -----------------------------------------------------------------------------------------------
def _col(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def xlsx_file(rows, *, date1904=False, sheet_name="Ventes") -> io.BytesIO:
    """Each cell: a str (text), or ("n", text) a number, or ("d", text) a date."""
    body = ""
    for number, row in enumerate(rows, start=1):
        cells = ""
        for at, value in enumerate(row):
            ref = f"{_col(at)}{number}"
            if isinstance(value, tuple):
                kind, text = value
                kind_attr = ' t="d"' if kind == "d" else ""
                cells += f'<c r="{ref}"{kind_attr}><v>{text}</v></c>'
            else:
                cells += f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
        body += f'<row r="{number}">{cells}</row>'
    pr = '<workbookPr date1904="1"/>' if date1904 else ""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'{pr}<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{body}</sheetData></worksheet>",
        )
    buffer.seek(0)
    return buffer


class SalesByTitlesTests(SimpleTestCase):
    def test_a_semicolon_csv_read_by_its_titles(self):
        read_ = reading([HEADER, "03/07/2026;Pinte Exemple;2;13,00;20 %", "03/07/2026;Pinte Exemple;1;6,50;20 %"])
        export = read_.export
        self.assertEqual(export.entries, [("Pinte Exemple", date(2026, 7, 3), 3)])
        money = export.money[("Pinte Exemple", date(2026, 7, 3))]
        self.assertEqual(money.revenue_ttc, Decimal("19.50"))
        self.assertEqual(money.revenue_ht, Decimal("16.25"))
        self.assertEqual(read_.header_row, 1)
        self.assertEqual(read_.mapped[till_file.PRODUCT], (2, "Article"))

    def test_titles_are_found_accents_case_and_spaces_aside_below_a_title_row(self):
        read_ = reading(
            [
                "Export de la caisse;;;;",
                "Période : juillet;;;;",
                "DATE;  article ;QTE;total ttc;tva",
                "3/7/2026;Soda;1;3,50;10",
            ]
        )
        self.assertEqual(read_.header_row, 3)
        self.assertEqual(read_.rows_above_header, 2)
        self.assertEqual(read_.export.entries, [("Soda", date(2026, 7, 3), 1)])

    def test_titles_never_found_refuse_the_file(self):
        said = refusal(["Jour;Produit;Nombre", "03/07/2026;Soda;1"])
        self.assertIn("« Date »", said)
        self.assertIn("30 premières lignes", said)

    def test_a_title_printed_twice_in_the_header_refuses_the_file(self):
        self.assertIn("deux fois", refusal(["Date;Article;Qté;Qté;Total TTC;TVA", "03/07/2026;Soda;1;1;3,50;10"]))


class SalesByNumbersTests(SimpleTestCase):
    def numbered(self, **fields):
        values = {
            "delimiter": ",",
            "decimal_mark": ".",
            "date_format": "yyyy-mm-dd",
            "day_column": "1",
            "product_column": "2",
            "quantity_column": "3",
            "amount_column": "4",
            "rate_column": "5",
        }
        values.update(fields)
        return sales_format(**values)

    def test_a_comma_csv_read_by_numbers_its_header_passed_over(self):
        read_ = reading(["Date,Article,Qty,Total,VAT", "2026-07-03,Soda,2,7.00,10%"], self.numbered())
        self.assertEqual(read_.export.entries, [("Soda", date(2026, 7, 3), 2)])
        # The header row reads no day and no sale: counted, never a refusal.
        self.assertEqual(read_.rows_without_day, 1)

    def test_a_titled_and_a_numbered_column_on_one_column_refuse_the_file(self):
        fmt = self.numbered(rate_column="", category_column="Article", product_column="2")
        self.assertIn("sert deux fois", refusal(["Date,Article,Qty,Total", "2026-07-03,Soda,2,7.00"], fmt))


class DayTests(SimpleTestCase):
    def test_a_day_and_a_month_of_one_digit_or_two_are_one_day(self):
        export = reading([HEADER, "3/8/2026;Soda;1;3,50;10", "03/08/2026;Soda;1;3,50;10"]).export
        self.assertEqual(export.entries, [("Soda", date(2026, 8, 3), 2)])

    def test_the_end_of_service_puts_the_small_hours_on_the_day_before(self):
        fmt = sales_format(date_format="yyyy-mm-dd", service_day_end_hour=5)
        export = reading(
            [
                HEADER,
                "2026-07-03 23:41;Soda;1;3,50;10",
                "2026-07-04T01:30:12.250;Soda;1;3,50;10",
                "2026-07-04 05:00;Soda;1;3,50;10",
            ],
            fmt,
        ).export
        self.assertEqual(export.entries, [("Soda", date(2026, 7, 3), 2), ("Soda", date(2026, 7, 4), 1)])

    def test_a_time_column_shifts_the_day_too(self):
        fmt = sales_format(time_column="Heure", service_day_end_hour=4)
        export = reading(
            [
                "Date;Heure;Article;Qté;Total TTC;TVA",
                "04/07/2026;02:10;Soda;1;3,50;10",
                "04/07/2026;18:00;Soda;1;3,50;10",
            ],
            fmt,
        ).export
        self.assertEqual(export.entries, [("Soda", date(2026, 7, 3), 1), ("Soda", date(2026, 7, 4), 1)])

    def test_a_day_cell_with_digits_that_is_no_date_refuses_the_file_naming_the_row(self):
        said = refusal([HEADER, "03/07/2026;Soda;1;3,50;10", "2026-07-03;Soda;1;3,50;10"])
        self.assertIn("Ligne 3", said)
        self.assertIn("2026-07-03", said)
        self.assertIn("Ligne 2", refusal([HEADER, "31/02/2026;Soda;1;3,50;10"]))

    def test_a_day_outside_2000_2099_refuses_the_file(self):
        self.assertIn("hors limites", refusal([HEADER, "03/07/1999;Soda;1;3,50;10"]))

    def test_blank_days_and_footers_are_counted_a_sale_with_a_wordy_day_refused(self):
        read_ = reading([HEADER, "03/07/2026;Soda;1;3,50;10", ";;;3,50;", "Total;;1;3,50;"])
        self.assertEqual(read_.rows_without_day, 2)
        self.assertEqual(read_.export.total_quantity, 1)
        self.assertIn("Ligne 3", refusal([HEADER, "03/07/2026;Soda;1;3,50;10", "lundi;Soda;1;3,50;10"]))

    def test_the_day_given_with_the_upload_for_a_format_without_one(self):
        fmt = sales_format(day_column="")
        export = reading(["Article;Qté;Total TTC;TVA", "Soda;4;14,00;10"], fmt, day=date(2026, 7, 3)).export
        self.assertEqual(export.entries, [("Soda", date(2026, 7, 3), 4)])
        self.assertEqual(refusal(["Article;Qté;Total TTC;TVA", "Soda;4;14,00;10"], fmt), till_file.NO_DAY)
        self.assertEqual(
            refusal([HEADER, "03/07/2026;Soda;1;3,50;10"], day=date(2026, 7, 3)), till_file.DAY_GIVEN_TWICE
        )


class MoneyTests(SimpleTestCase):
    def test_three_sodas_at_3_50_are_9_55_ht(self):
        """HT per rate bucket, not per line: 9,54 line by line."""
        export = reading([HEADER, *["03/07/2026;Soda;1;3,50;5,5 %"] * 3]).export
        self.assertEqual(export.money[("Soda", date(2026, 7, 3))].revenue_ht, Decimal("9.95"))
        export = reading([HEADER, *["03/07/2026;Soda;1;3,50;10"] * 3]).export
        self.assertEqual(export.money[("Soda", date(2026, 7, 3))].revenue_ht, Decimal("9.55"))

    def test_rates_read_as_a_percentage_or_a_fraction_never_guessed(self):
        for printed in ("20 %", "20%", "20", "0,2", "0.2", "20,0", "-20 %"):
            with self.subTest(printed=printed):
                self.assertEqual(read_rate(printed), Decimal("0.20"))
        for printed, rate in (("5,5", "0.055"), ("2,1 %", "0.021"), ("0", "0"), ("10", "0.10")):
            with self.subTest(printed=printed):
                self.assertEqual(read_rate(printed), Decimal(rate))
        for printed in ("19,6", "0,2 %", "", "abc", "1E1"):
            with self.subTest(printed=printed):
                self.assertIsNone(read_rate(printed))

    def test_a_line_without_a_readable_rate_has_no_ht(self):
        export = reading([HEADER, "03/07/2026;Soda;1;3,50;19,6"]).export
        money = export.money[("Soda", date(2026, 7, 3))]
        self.assertEqual((money.revenue_ht, money.without_rate_ttc), (Decimal("0"), Decimal("3.50")))
        self.assertEqual(export.lines_without_rate, 1)

    def test_an_ht_column_wins_over_the_rate(self):
        fmt = sales_format(amount_ht_column="Total HT")
        export = reading(
            [
                "Date;Article;Qté;Total TTC;Total HT;TVA",
                "03/07/2026;Soda;1;3,50;3,1818;10",
                "03/07/2026;Soda;1;3,50;;10",
            ],
            fmt,
        ).export
        money = export.money[("Soda", date(2026, 7, 3))]
        self.assertEqual(money.revenue_ttc, Decimal("7.00"))
        self.assertEqual(money.revenue_ht, Decimal("3.18") + Decimal("3.18"))

    def test_a_unit_price_is_multiplied_by_the_quantity_a_line_amount_never(self):
        lines = [HEADER, "03/07/2026;Pinte;2;6,50;20 %"]
        line_amount = reading(lines).export.money[("Pinte", date(2026, 7, 3))].revenue_ttc
        unit_price = reading(lines, sales_format(amount_is_unit_price=True)).export
        self.assertEqual(line_amount, Decimal("6.50"))
        self.assertEqual(unit_price.money[("Pinte", date(2026, 7, 3))].revenue_ttc, Decimal("13.00"))

    def test_an_unreadable_amount_leaves_the_day_unread_not_short(self):
        export = reading(
            [HEADER, "03/07/2026;Soda;1;3,50;10", "03/07/2026;Soda;1;trois;10", "04/07/2026;Soda;1;;10"]
        ).export
        self.assertEqual(export.days_without_amount, 2)
        self.assertEqual(export.lines_without_amount, 2)
        self.assertEqual(export.money, {})
        self.assertEqual(export.total_quantity, 3)

    def test_an_exponent_is_no_amount_and_nothing_wide_is_stored(self):
        export = reading([HEADER, "03/07/2026;Soda;1;1E+500;10"]).export
        self.assertEqual(export.money, {})
        self.assertEqual(export.days_without_amount, 1)

    def test_a_refund_is_negative_a_comp_is_free(self):
        export = reading(
            [
                HEADER,
                "03/07/2026;Pinte;-1;-6,50;-20 %",
                "04/07/2026;Pinte;1;0,00;20 %",
                "05/07/2026;Pinte;1;6,50 €;20 %",
            ]
        ).export
        self.assertEqual([quantity for _n, _d, quantity in export.entries], [-1, 1, 1])
        self.assertEqual(export.money[("Pinte", date(2026, 7, 3))].revenue_ht, Decimal("-5.42"))
        self.assertEqual(export.money[("Pinte", date(2026, 7, 4))].revenue_ttc, Decimal("0.00"))
        self.assertEqual(export.money[("Pinte", date(2026, 7, 5))].revenue_ttc, Decimal("6.50"))
        self.assertEqual(export.refund_lines, 1)

    def test_thousands_and_more_decimals_than_cents(self):
        export = reading([HEADER, "03/07/2026;Privatisation;1;1 234,50;20"]).export
        self.assertEqual(export.money[("Privatisation", date(2026, 7, 3))].revenue_ttc, Decimal("1234.50"))
        self.assertIn("décimales", refusal([HEADER, "03/07/2026;Soda;1;3,505;10"]))

    def test_a_days_revenue_past_its_column_refuses_the_file(self):
        lines = [HEADER, *["03/07/2026;Soda;1;60 000 000,00;10"] * 2]
        self.assertIn("99 999 999,99", refusal(lines))


class QuantityTests(SimpleTestCase):
    def test_fractions_are_summed_exactly_then_rounded_half_away_from_zero_and_counted(self):
        export = reading(
            [
                HEADER,
                "03/07/2026;Vin au verre;0,5;3,00;20",
                "03/07/2026;Vin au verre;0,5;3,00;20",
                "04/07/2026;Vin au verre;1,5;9,00;20",
                "05/07/2026;Vin au verre;-1,5;-9,00;20",
            ]
        ).export
        self.assertEqual([quantity for _n, _d, quantity in export.entries], [1, 2, -2])
        self.assertEqual(export.quantities_rounded, 2)

    def test_an_unreadable_quantity_refuses_the_file_naming_the_row(self):
        said = refusal([HEADER, "03/07/2026;Soda;1;3,50;10", "03/07/2026;Soda;deux;7,00;10"])
        self.assertIn("Ligne 3", said)
        self.assertIn("Soda", said)
        self.assertIn("Ligne 2", refusal([HEADER, "03/07/2026;Soda;;3,50;10"]))

    def test_a_quantity_no_bar_sells_refuses_the_file(self):
        self.assertIn("au plus", refusal([HEADER, "03/07/2026;Soda;100001;3,50;10"]))

    def test_a_row_with_no_product_is_counted(self):
        read_ = reading([HEADER, "03/07/2026;Soda;1;3,50;10", "03/07/2026;;1;3,50;10"])
        self.assertEqual(read_.rows_without_product, 1)

    def test_a_name_is_cut_to_its_column(self):
        name = "Cocktail " + "x" * 300
        ((read_name, _day, _quantity),) = reading([HEADER, f"03/07/2026;{name};1;9,00;20"]).export.entries
        self.assertEqual(read_name, name[:255])


class EncodingTests(SimpleTestCase):
    LINES = (HEADER, "03/07/2026;Café crème;1;2,50;10")

    def test_utf_8_with_or_without_its_mark_utf_16_and_windows_1252(self):
        for content in (
            "\n".join(self.LINES).encode("utf-8"),
            b"\xef\xbb\xbf" + "\n".join(self.LINES).encode("utf-8"),
            "\n".join(self.LINES).encode("utf-16"),
            "\n".join(self.LINES).encode("cp1252"),
        ):
            with self.subTest(start=content[:4]):
                export = reading(io.BytesIO(content)).export
                self.assertEqual(export.entries, [("Café crème", date(2026, 7, 3), 1)])

    def test_a_nul_and_a_broken_utf_8_mark_are_refused(self):
        self.assertEqual(refusal(io.BytesIO(b"Date;Article\x00;Qt\n")), till_file.NOT_A_CSV)
        self.assertIn("UTF-8", refusal(io.BytesIO(b"\xef\xbb\xbfDate;\xe9\n")))

    def test_windows_1252_never_reads_a_file_behind_a_unicode_mark(self):
        content = io.BytesIO(b"\xef\xbb\xbf" + "\n".join(self.LINES).encode("utf-8"))
        self.assertIn("Windows-1252", refusal(content, sales_format(encoding="cp1252")))

    def test_only_csv_txt_and_xlsx(self):
        self.assertEqual(refusal([HEADER], file_name="export.xls"), till_file.XLS_REFUSED)
        self.assertEqual(refusal([HEADER], file_name="export.pdf"), till_file.SUFFIX_REFUSED)
        self.assertEqual(reading(list(self.LINES), file_name="EXPORT.TXT").export.total_quantity, 1)

    def test_a_file_reading_nothing_is_refused(self):
        self.assertEqual(refusal([HEADER]), till_file.NOTHING_READ)

    def test_a_line_wider_than_any_export_is_refused(self):
        with mock.patch.object(till_file, "MAX_SEPARATORS", 10):
            self.assertIn("colonnes", refusal([HEADER, ";" * 11]))


class CapsTests(SimpleTestCase):
    def test_too_many_rows_or_products_are_refused(self):
        lines = [HEADER, *[f"03/07/2026;Produit {n};1;1,00;20" for n in range(5)]]
        with mock.patch.object(till_file, "MAX_ROWS", 3):
            self.assertIn("lignes", refusal(lines))
        with mock.patch.object(till_file, "MAX_PRODUCTS", 3):
            self.assertIn("produits différents", refusal(lines))

    def test_a_limit_stops_the_reading_and_says_so(self):
        lines = [HEADER, *[f"03/07/2026;Produit {n};1;1,00;20" for n in range(5)]]
        read_ = reading(lines, limit=2)
        self.assertTrue(read_.truncated)
        self.assertEqual(read_.rows_read, 2)


class WorkbookTests(SimpleTestCase):
    def test_excel_serials_numbers_and_their_binary_noise(self):
        workbook = xlsx_file(
            [
                ["Date", "Article", "Qté", "Total TTC", "TVA"],
                [("n", "46206"), "Pinte", ("n", "1"), ("n", "10.499999999999998"), ("n", "0.2")],
                [("n", "46206.0625"), "Pinte", ("n", "1"), ("n", "6.5"), "20 %"],
            ]
        )
        fmt = sales_format(service_day_end_hour=2)
        export = reading(workbook, fmt, file_name="export.xlsx").export
        # 46206 is 03/07/2026; .0625 is 01:30, the service of the day before.
        self.assertEqual(export.entries, [("Pinte", date(2026, 7, 3), 1), ("Pinte", date(2026, 7, 2), 1)])
        self.assertEqual(export.money[("Pinte", date(2026, 7, 3))].revenue_ttc, Decimal("10.50"))

    def test_more_than_noise_is_refused(self):
        workbook = xlsx_file(
            [
                ["Date", "Article", "Qté", "Total TTC", "TVA"],
                [("n", "46206"), "Pinte", ("n", "1"), ("n", "3.505"), "20"],
            ]
        )
        self.assertIn("décimales", refusal(workbook, file_name="export.xlsx"))

    def test_the_1904_calendar(self):
        workbook = xlsx_file(
            [["Date", "Article", "Qté", "Total TTC", "TVA"], [("n", "44744"), "Pinte", ("n", "1"), ("n", "6.5"), "20"]],
            date1904=True,
        )
        export = reading(workbook, file_name="export.xlsx").export
        self.assertEqual(export.entries[0][1], date(2026, 7, 3))

    def test_a_date_cell_is_read_whatever_the_format_says(self):
        workbook = xlsx_file(
            [
                ["Date", "Article", "Qté", "Total TTC", "TVA"],
                [("d", "2026-07-03T23:41:00"), "Pinte", ("n", "1"), ("n", "6.5"), "20"],
            ]
        )
        export = reading(workbook, file_name="export.xlsx").export
        self.assertEqual(export.entries[0][1], date(2026, 7, 3))

    def test_a_named_sheet_and_the_row_named_as_excel_shows_it(self):
        workbook = xlsx_file(
            [["Date", "Article", "Qté", "Total TTC", "TVA"], [("n", "46206"), "Pinte", "deux", ("n", "6.5"), "20"]],
            sheet_name="Détail",
        )
        self.assertIn("Ligne 2", refusal(workbook, sales_format(sheet="Détail"), file_name="export.xlsx"))
        self.assertIn("Détail", refusal(workbook, sales_format(sheet="Autre"), file_name="export.xlsx"))

    def test_a_file_that_is_no_workbook_is_refused_in_french(self):
        self.assertEqual(refusal(io.BytesIO(b"pas un zip"), file_name="export.xlsx"), till_file.NOT_A_WORKBOOK)


class PaymentsTests(TestCase):
    """Payments map through PosDailyPayment's vocabulary (a database-free
    lookup, but the model's)."""

    LINES = ("Date;Moyen;Montant",)

    def test_methods_mapped_folded_the_app_s_words_the_till_s_spellings_and_the_rest_kept(self):
        fmt = payments_format(method_map="Carte bancaire = Carte\nTicket resto = Titres-restaurant")
        export = reading(
            [
                *self.LINES,
                "03/07/2026;CARTE BANCAIRE;12,50",
                "03/07/2026;Carte bancaire;7,50",
                "03/07/2026;espèces;5,00",
                "03/07/2026;CB;1,00",
                "03/07/2026;Ticket Resto;9,00",
                "03/07/2026;Lydia;4,00",
                "03/07/2026;;2,00",
                "03/07/2026;Espèces;-1,00",
            ],
            fmt,
        ).export
        day = date(2026, 7, 3)
        self.assertEqual(export.payments[(day, CB)].amount, Decimal("21.00"))
        self.assertEqual(export.payments[(day, CB)].count, 3)
        self.assertEqual(export.payments[(day, CASH)].amount, Decimal("4.00"))
        self.assertEqual(export.payments[(day, PosDailyPayment.MEAL_VOUCHER)].amount, Decimal("9.00"))
        self.assertEqual(export.payments[(day, "Lydia")].amount, Decimal("4.00"))
        self.assertEqual(export.payments[(day, UNREAD)].amount, Decimal("2.00"))
        self.assertEqual(export.unread_payment_tickets, 1)
        self.assertEqual(export.unmapped_methods, ["Lydia"])
        self.assertTrue(export.payments_read)
        self.assertFalse(export.sales_read)
        self.assertEqual(export.entries, [])

    def test_a_payment_without_a_readable_amount_refuses_the_file(self):
        self.assertIn("Ligne 2", refusal([*self.LINES, "03/07/2026;Carte;douze"], payments_format()))

    def test_a_row_with_neither_method_nor_amount_is_counted(self):
        read_ = reading([*self.LINES, "03/07/2026;Carte;3,00", "03/07/2026;;"], payments_format())
        self.assertEqual(read_.rows_without_payment, 1)

    def test_a_days_payments_past_their_column_refuse_the_file(self):
        lines = [*self.LINES, *["03/07/2026;Carte;6 000 000 000,00"] * 2]
        self.assertIn("colonne", refusal(lines, payments_format()))


class CheckFormatTests(SimpleTestCase):
    def refused(self, fmt) -> tuple[str, str]:
        with self.assertRaises(FormatError) as caught:
            check_format(fmt)
        return caught.exception.field, caught.exception.message

    def test_each_refusal_is_on_its_field(self):
        cases = (
            (sales_format(product_column=""), "product_column"),
            (sales_format(quantity_column=""), "quantity_column"),
            (payments_format(paid_column=""), "paid_column"),
            (sales_format(method_column="Moyen"), "method_column"),
            (payments_format(product_column="Article"), "product_column"),
            (sales_format(amount_column="", rate_column="TVA"), "rate_column"),
            (sales_format(amount_ht_column="HT", amount_column=""), "amount_ht_column"),
            (sales_format(amount_column="", rate_column="", amount_is_unit_price=True), "amount_is_unit_price"),
            (sales_format(day_column="", time_column="Heure"), "time_column"),
            (sales_format(service_day_end_hour=12), "service_day_end_hour"),
            (sales_format(service_day_end_hour=-1), "service_day_end_hour"),
            (sales_format(day_column="101"), "day_column"),
            (sales_format(day_column="0"), "day_column"),
            (sales_format(day_column="x" * 101), "day_column"),
            (sales_format(day_column="0301"), "day_column"),
            (sales_format(day_column="́"), "day_column"),
            (sales_format(product_column="Article", category_column="article"), "category_column"),
            (sales_format(day_column="2", product_column="2"), "product_column"),
            (sales_format(kind="autre"), "kind"),
            (sales_format(encoding="ebcdic"), "encoding"),
            (sales_format(delimiter=":"), "delimiter"),
            (sales_format(date_format="yyyy"), "date_format"),
            (payments_format(method_map="Carte bancaire"), "method_map"),
            (payments_format(method_map="Lydia = Bitcoin"), "method_map"),
            (payments_format(method_map="CB = Carte\ncb = Espèces"), "method_map"),
            (payments_format(method_map="= Carte"), "method_map"),
        )
        for fmt, field in cases:
            with self.subTest(field=field, fmt=fmt):
                self.assertEqual(self.refused(fmt)[0], field)

    def test_the_two_roles_are_named(self):
        self.assertIn("pour produit et pour catégorie", self.refused(sales_format(category_column="ARTICLE"))[1])

    def test_a_method_map_reads_the_app_s_words_and_the_till_s_spellings(self):
        layout = check_format(payments_format(method_map="Carte bancaire = carte\nLiquide = Espèces\nResto = TR"))
        self.assertEqual(
            layout.method_map,
            {"carte bancaire": CB, "liquide": CASH, "resto": PosDailyPayment.MEAL_VOUCHER},
        )

    def test_a_format_without_a_day_column_is_one(self):
        self.assertNotIn(till_file.DAY, check_format(sales_format(day_column="")).columns)


class ModelTests(TestCase):
    def test_the_model_s_choices_are_the_reader_s(self):
        self.assertEqual(dict(TillFormat.Kind.choices), till_file.KINDS)
        self.assertEqual(dict(TillFormat.Encoding.choices), till_file.ENCODINGS)
        self.assertEqual(dict(TillFormat.Delimiter.choices), till_file.DELIMITERS)
        self.assertEqual(dict(TillFormat.DecimalMark.choices), till_file.DECIMAL_MARKS)
        self.assertEqual(
            dict(TillFormat.DateFormat.choices), {key: value[0] for key, value in till_file.DATE_FORMATS.items()}
        )
        fields = {field.name for field in TillFormat._meta.get_fields()}
        self.assertTrue(set(till_file.COLUMN_FIELDS) <= fields)

    def test_clean_puts_the_reader_s_refusal_on_its_field(self):
        fmt = TillFormat(name="Caisse Exemple", product_column="Article")
        with self.assertRaises(ValidationError) as caught:
            fmt.full_clean()
        self.assertIn("quantity_column", caught.exception.message_dict)
        TillFormat(
            name="Caisse Exemple", day_column="Date", product_column="Article", quantity_column="Qté"
        ).full_clean()
