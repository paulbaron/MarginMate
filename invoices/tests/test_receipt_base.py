"""The shared arithmetic every receipt parser leans on.

These are the pieces that decide whether a misread digit becomes a wrong
price or a flagged receipt, so they are tested on their own rather than only
through a parser: a bug here is invisible in a passing end-to-end test that
happens to use a clean fixture.

The anchors - where the total is, where the items stop, whether a promotion
is real - are found by the numbers and never by the words, so their tests
use the words as a real recogniser read them.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.receipt_base import (
    ReceiptTotals,
    VatSummary,
    amount_candidates,
    amount_printed,
    assign_rates_by_bucket,
    build_checks,
    compose_invoice_number,
    ends_items,
    finalise_summary,
    format_rate,
    line_amounts,
    parse_vat_line,
    printed_promotion,
    printed_total,
    read_date,
    read_rate,
    to_ht,
)

D = Decimal
FIVE_FIVE = D("0.055")
TWENTY = D("0.20")


class ReadRateTests(SimpleTestCase):
    def test_reads_the_ordinary_forms(self):
        self.assertEqual(read_rate("5,5%"), FIVE_FIVE)
        self.assertEqual(read_rate("TVA5.50%:"), FIVE_FIVE)
        self.assertEqual(read_rate("20%"), TWENTY)

    def test_undoes_a_column_rule_read_as_a_leading_digit(self):
        """Franprix's "| 5.5%" still comes back as "15.5%" from the current
        engine - nine times on the 42 receipts. Only accepted because the
        stripped reading is a rate France actually has."""
        self.assertEqual(read_rate("15.5%"), FIVE_FIVE)

    def test_rejects_a_rate_that_france_does_not_have(self):
        """The VAT *amount* "0,26" was once read as a 26% rate, which would
        have divided every line on that receipt by the wrong number."""
        self.assertIsNone(read_rate("26%"))
        self.assertIsNone(read_rate("0%"))

    def test_no_rate_at_all(self):
        self.assertIsNone(read_rate("MERCI ET A BIENTOT"))
        self.assertIsNone(read_rate(""))


class AmountCandidateTests(SimpleTestCase):
    def test_a_two_decimal_amount_has_one_reading(self):
        self.assertEqual(amount_candidates("2,26"), [D("2.26")])

    def test_a_three_decimal_amount_offers_the_truncated_reading_too(self):
        """"2.261" is "| 2,26 |" with the rules read as digits."""
        self.assertIn(D("2.26"), amount_candidates("2.261"))
        self.assertIn(D("2.261"), amount_candidates("2.261"))

    def test_no_amounts(self):
        self.assertEqual(amount_candidates("CUMULEZ DES EUROS"), [])


class LineAmountsTests(SimpleTestCase):
    def test_both_decimal_separators_and_the_sign(self):
        self.assertEqual(line_amounts("Le 2eme a moins 50%  -3,58€"), [D("-3.58")])
        self.assertEqual(line_amounts("CB EMV  10,21€  Rendu 0.00"), [D("10.21"), D("0.00")])

    def test_money_is_cents_so_a_trailing_digit_is_not_a_decimal(self):
        """"6.061" is a Franprix VAT table's column rule read as a digit."""
        self.assertEqual(line_amounts("5.5%  5.74  0.32  6.061"), [D("5.74"), D("0.32"), D("6.06")])

    def test_a_count_or_a_rate_is_not_money(self):
        self.assertEqual(line_amounts("NOMBRE D'ARTICLES  5"), [])
        self.assertEqual(line_amounts("TVA 5,5%"), [])


class PrintedTotalTests(SimpleTestCase):
    def test_the_amount_paid_is_the_largest_one_printed_twice(self):
        """However "TOTAL" came out - these are real readings of it."""
        lines = [
            "BAGUETTE BLANC  T1 0.49",
            "BAGUETTE BLANC  T1 0.49",
            "CITRON  T1 2.29",
            "ISQUS-OTAL I  3.27",
            "OTAL A PAYER  3.27",
            "B SANS CONTACT  3.27",
            "5.5%  3.10  0.17  3.27",
        ]
        self.assertEqual(printed_total(lines), D("3.27"))

    def test_a_pre_discount_total_printed_twice_loses_to_the_one_the_vat_table_adds_up_to(self):
        lines = [
            "2 X BONBONS INVENTES 7,15€  14,30€",
            "Le 2eme a moins 50%  -3,58€",
            "TOTAI HORS AVANTAGES  14,30",
            "RESTE A PAYER  10,72€",
            "C3 EMV  10,72€",
            "20%  8,93  1.79  10.72",
            "Total TVA  8,93  1.79  10.72",
        ]
        self.assertEqual(printed_total(lines), D("10.72"))

    def test_the_vat_tables_ht_base_printed_twice_is_never_the_total(self):
        """Every total unreadable, Monoprix's HT base is the largest figure
        printed twice. Taken for the total, it would put the receipt's own
        tax-exclusive sum where the amount paid belongs."""
        lines = [
            "SUCRE ROUX PU  2,40",
            "4X BASILIC FRAIS  1,50  6,00",
            "RESTE A PAYER  ....",
            "5,5%  7,96  0,44  ....",
            "Total TVA  7,96  0,44  ....",
        ]
        self.assertIsNone(printed_total(lines))

    def test_with_no_readable_vat_table_repetition_decides(self):
        """Wing Seng's VAT amount faded; the total is still printed three times."""
        lines = [
            "#CITRON VERT  12.51",
            "MENTHE  1.00",
            "S TOTAL  EUR  13.51",
            "13.51",
            "TOIALUR  244  13.51",
            "TVA 5.50 %;  'O EUR",
        ]
        self.assertEqual(printed_total(lines), D("13.51"))

    def test_a_tax_inclusive_table_that_fits_either_reading(self):
        lines = [
            "Article divers",
            "4pcs  0,70  2,80 A",
            "Articles: 1  Total: 2,80",
            "Espèces  2,80€",
            "TVA A 5.50%  2,80  0,15",
        ]
        self.assertEqual(printed_total(lines), D("2.80"))

    def test_nothing_printed_twice_is_no_total(self):
        self.assertIsNone(printed_total(["MENTHE  1.00", "TOTAL  CEUR  1351"]))
        self.assertIsNone(printed_total([]))


class EndsItemsTests(SimpleTestCase):
    def test_the_first_line_showing_the_amount_paid(self):
        self.assertTrue(ends_items(D("3.27"), 3, D("3.27"), D("3.27"), D("3.27")))

    def test_the_first_item_is_never_a_total(self):
        """A one-item ticket prints the same figure on the item and the total."""
        self.assertFalse(ends_items(D("1.00"), 0, D("0"), D("0"), D("1.00")))

    def test_the_running_sum_repeated_once_the_items_reach_the_total(self):
        """"TOTAL HORS AVANTAGES 14,30" after a 14,30 item and a -3,58 promotion."""
        self.assertTrue(ends_items(D("14.30"), 1, D("14.30"), D("10.72"), D("10.72")))

    def test_a_subtotal_part_way_down_the_list_is_not_the_end(self):
        """Franprix prints a SOUS-TOTAL mid-list: 12.65 of a 13.63 ticket."""
        self.assertFalse(ends_items(D("12.65"), 5, D("12.65"), D("12.65"), D("13.63")))

    def test_without_a_total_it_takes_two_items_summing_to_a_later_figure(self):
        self.assertTrue(ends_items(D("13.51"), 2, D("13.51"), D("13.51"), None))
        self.assertFalse(ends_items(D("1.00"), 1, D("1.00"), D("1.00"), None))


class AmountPrintedTests(SimpleTestCase):
    def test_finds_the_first_line_from_the_start(self):
        lines = ["PAIN  T1 3.45", "SOUS-TOTAL  2.90", "TOTAL SANS AVANTAGES  3.45"]
        self.assertEqual(amount_printed(lines, D("3.45"), start=1), 2)
        self.assertIsNone(amount_printed(lines, D("9.99")))

    def test_a_promotion_is_found_by_its_size(self):
        self.assertEqual(amount_printed(["TOTAL renise  0.55-", "Le 2eme  -3,58€"], D("3.58")), 1)


class PrintedPromotionTests(SimpleTestCase):
    PROMOTION = (
        "PAIN COMPLET  T1 0.55",
        "SOUS-TOTAL  2.90",
        "TOTAL SANS AVANTAGES  3.45",
        "3 pour 2",
        "PAIN COMPLET  0.55",
        "TOTAL remise  0.55-",
        "TOTAL A PAYER  2.90",
    )

    def test_a_pre_discount_total_and_its_discount_printed_twice(self):
        self.assertEqual(printed_promotion(self.PROMOTION, 1, D("2.90")), (D("3.45"), D("0.55")))

    def test_cash_and_change_are_not_a_promotion_even_on_a_ticket_printed_twice(self):
        """10,00 handed over, 0,30 back. The photo held the ticket twice, so
        the change was printed twice: "un article manque" on a ticket that
        was complete. A promotion is printed before the amount paid comes
        round again; change is printed after it."""
        copy = ["POIG COUL ULTR  T2 4.85", "SOUS-TOTAL  9.70", "TOTAL A PAYER  9.70", "ESPECES  10.00", "RENDU  0.30",
                "20%  8.08  1.62  9.70"]
        self.assertIsNone(printed_promotion(copy + copy, 1, D("9.70")))

    def test_no_total_no_promotion(self):
        self.assertIsNone(printed_promotion(self.PROMOTION, 1, None))


class VatSummaryTests(SimpleTestCase):
    def test_a_tax_exclusive_base_is_kept_as_is(self):
        summary = VatSummary(rate=FIVE_FIVE, base=D("9.68"), vat_amount=D("0.53")).resolve()
        self.assertEqual(summary.base, D("9.68"))
        self.assertEqual(summary.total_ttc, D("10.21"))
        self.assertTrue(summary.is_consistent)

    def test_a_tax_inclusive_base_is_converted(self):
        """Sabbh's "Base TVA" column is the TTC total, unlike every other
        shop's - worked out from the arithmetic, not from the shop name."""
        summary = VatSummary(rate=FIVE_FIVE, base=D("16.90"), vat_amount=D("0.88")).resolve()
        self.assertEqual(summary.total_ttc, D("16.90"))
        self.assertEqual(summary.base, D("16.02"))
        self.assertTrue(summary.is_consistent)

    def test_a_total_alone_yields_the_rest(self):
        summary = VatSummary(rate=FIVE_FIVE, total_ttc=D("11.05")).resolve()
        self.assertEqual(summary.vat_amount, D("0.58"))
        self.assertEqual(summary.base, D("10.47"))

    def test_nothing_at_all_stays_unresolved(self):
        summary = VatSummary(rate=FIVE_FIVE).resolve()
        self.assertIsNone(summary.base)
        self.assertFalse(summary.is_consistent)


class ParseVatLineTests(SimpleTestCase):
    def test_franprix_four_column_table(self):
        summary = parse_vat_line("15.5%  12.38  0.68  13.06", expected_total=D("13.06"))
        self.assertEqual((summary.base, summary.vat_amount, summary.total_ttc), (D("12.38"), D("0.68"), D("13.06")))

    def test_column_rules_read_as_digits_are_undone(self):
        summary = parse_vat_line("5.5%|  2.261  0.121  2.381", expected_total=D("2.38"))
        self.assertEqual(summary.base, D("2.26"))
        self.assertEqual(summary.vat_amount, D("0.12"))

    def test_monoprix_four_decimal_ht_is_not_mistaken_for_a_rule(self):
        """The opposite case: "3.0237" really is the printed HT. Only the
        receipt's own grand total separates the two."""
        summary = parse_vat_line("6  3.0237  5.50%  0.1663  3.19", expected_total=D("3.19"))
        self.assertEqual(summary.total_ttc, D("3.19"))

    def test_a_lone_amount_equal_to_the_total_is_the_base_not_the_tax(self):
        """A Sabbh ticket whose VAT column was cut off leaves the TTC total
        standing alone. Reading it as the tax gave a base of 0.00 EUR - and
        what is left is arithmetic, not a reading, so it says so."""
        summary = finalise_summary(parse_vat_line("TVAA 5.50%  7,70", expected_total=D("7.70")), D("7.70"))
        self.assertEqual(summary.base, D("7.30"))
        self.assertEqual(summary.vat_amount, D("0.40"))
        self.assertTrue(summary.derived)

    def test_a_lone_amount_that_is_the_tax_is_read_as_the_tax(self):
        """Wing Seng's whole VAT table is "TVA 5,50%: 0,58 EUR"."""
        summary = finalise_summary(parse_vat_line("TVA5.50%:  0.58 EUR", expected_total=D("11.05")), D("11.05"))
        self.assertEqual(summary.vat_amount, D("0.58"))
        self.assertEqual(summary.total_ttc, D("11.05"))
        self.assertFalse(summary.derived)

    def test_a_row_with_no_rate_is_not_a_vat_row(self):
        self.assertIsNone(parse_vat_line("TOTAL A PAYER  13.06"))

    def test_a_small_tax_inclusive_row_that_fits_both_readings(self):
        """2,80 x 5.5% and 2,80 x 5.5% / 1.055 both round to 0,15. Only the
        first reading was ever tried: the row came out as 2,95 TTC, and no
        total was found on five Sabbh tickets that print 2,80 four times."""
        summary = parse_vat_line("TVA A 5.50%  2,80  0,15", expected_total=D("2.80"))
        self.assertEqual((summary.base, summary.vat_amount, summary.total_ttc), (D("2.65"), D("0.15"), D("2.80")))
        unprompted = parse_vat_line("TVA A 5.50%  2,80  0,15")
        self.assertEqual(unprompted.total_ttc, D("2.80"))

    def test_the_same_numbers_as_a_tax_exclusive_row(self):
        summary = parse_vat_line("5.5%  2,80  0,15  2,95", expected_total=D("2.95"))
        self.assertEqual((summary.base, summary.total_ttc), (D("2.80"), D("2.95")))


class FormatRateTests(SimpleTestCase):
    def test_whole_percentages_do_not_go_scientific(self):
        """Decimal.normalize() renders 20% as "2E+1", which reached the
        review screen once."""
        self.assertEqual(format_rate(TWENTY), "20")
        self.assertEqual(format_rate(FIVE_FIVE), "5.5")


class ToHtTests(SimpleTestCase):
    def test_converts_and_rounds_to_the_cent(self):
        self.assertEqual(to_ht(D("13.06"), FIVE_FIVE), D("12.38"))

    def test_a_zero_rate_is_the_identity(self):
        self.assertEqual(to_ht(D("13.06"), D("0")), D("13.06"))

    def test_zero_amount(self):
        self.assertEqual(to_ht(D("0"), FIVE_FIVE), D("0"))


class AssignRatesTests(SimpleTestCase):
    def test_one_bucket_covers_every_code(self):
        rates, confident = assign_rates_by_bucket(
            {"T1": D("13.06")}, [VatSummary(rate=FIVE_FIVE, base=D("12.38"), vat_amount=D("0.68")).resolve()]
        )
        self.assertEqual(rates, {"T1": FIVE_FIVE})
        self.assertTrue(confident)

    def test_two_buckets_are_matched_by_what_each_code_totals(self):
        summaries = [
            VatSummary(rate=FIVE_FIVE, base=D("9.48"), vat_amount=D("0.52")).resolve(),
            VatSummary(rate=TWENTY, base=D("5.98"), vat_amount=D("1.20")).resolve(),
        ]
        rates, confident = assign_rates_by_bucket({"T1": D("10.00"), "T2": D("7.18")}, summaries)
        self.assertEqual(rates["T2"], TWENTY)
        self.assertEqual(rates["T1"], FIVE_FIVE)
        self.assertTrue(confident)

    def test_no_table_means_no_guess(self):
        rates, confident = assign_rates_by_bucket({"T1": D("10.00")}, [])
        self.assertEqual(rates, {})
        self.assertFalse(confident)


class BuildChecksTests(SimpleTestCase):
    def _totals(self):
        return ReceiptTotals(
            printed_total_ttc=D("2.90"),
            vat_summaries=[VatSummary(rate=FIVE_FIVE, base=D("2.75"), vat_amount=D("0.15")).resolve()],
        )

    def test_a_receipt_that_balances_passes_everything(self):
        checks, adjustment = build_checks(self._totals(), D("2.90"), lines_total_ht=D("2.75"), line_count=4)
        self.assertTrue(all(check.passed for check in checks))
        self.assertEqual(adjustment, D("0"))

    def test_per_line_rounding_becomes_a_reconciliation_adjustment(self):
        """Six items at 0.49 TTC are 0.4645 HT each, stored as 0.46: the
        lines say 2.76 and the receipt says 2.79. The TTC arithmetic
        balances perfectly, so only the HT check catches it."""
        totals = ReceiptTotals(
            printed_total_ttc=D("2.94"),
            vat_summaries=[VatSummary(rate=FIVE_FIVE, base=D("2.79"), vat_amount=D("0.15")).resolve()],
        )
        checks, adjustment = build_checks(totals, D("2.94"), lines_total_ht=D("2.76"), line_count=6)
        self.assertTrue(all(check.passed for check in checks))
        self.assertEqual(adjustment, D("0.03"))

    def test_a_real_discrepancy_fails_and_is_not_absorbed(self):
        checks, adjustment = build_checks(self._totals(), D("1.90"), lines_total_ht=D("1.80"), line_count=2)
        self.assertFalse(all(check.passed for check in checks))
        self.assertEqual(adjustment, D("0"))

    def test_no_printed_total_is_reported_rather_than_assumed(self):
        checks, adjustment = build_checks(ReceiptTotals(), D("2.90"), lines_total_ht=D("2.75"), line_count=2)
        labels = {check.label for check in checks if not check.passed}
        self.assertIn("Total imprimé lu", labels)
        self.assertIn("Table TVA lue", labels)
        self.assertEqual(adjustment, D("0"))

    def test_low_recognition_confidence_is_flagged_even_when_the_sums_agree(self):
        checks, _ = build_checks(
            self._totals(), D("2.90"), lines_total_ht=D("2.75"), line_count=4, confidence=D("0.55")
        )
        self.assertFalse(all(check.passed for check in checks))


class ComposeInvoiceNumberTests(SimpleTestCase):
    def test_a_real_ticket_number_wins(self):
        self.assertEqual(compose_invoice_number("6888884", None, None), "6888884")

    def test_date_and_total_stand_in(self):
        self.assertEqual(compose_invoice_number("", read_date("15-07-2026"), D("13.06")), "20260715-13.06")

    def test_nothing_to_go_on_yields_no_number(self):
        self.assertEqual(compose_invoice_number("", None, D("13.06")), "")
        self.assertEqual(compose_invoice_number("", read_date("15-07-2026"), None), "")


class ReadDateTests(SimpleTestCase):
    def test_day_first(self):
        self.assertEqual(read_date("15-07-2026 WEDNESDAY").isoformat(), "2026-07-15")
        self.assertEqual(read_date("28/01/2026 16:03").isoformat(), "2026-01-28")

    def test_a_time_glued_to_the_date_still_parses(self):
        """"Heure:14-07-2026 14:49:26" comes back with the space closed.
        Guarding the year with "not followed by a digit" left every Sabbh
        receipt dateless."""
        self.assertEqual(read_date("Heure:14-07-202614:49:26").isoformat(), "2026-07-14")

    def test_a_two_digit_year_is_not_a_date(self):
        """"21/04/26-16:17" is a real Monoprix header. Reading "26" as a year
        would date the invoice to the year 26."""
        self.assertIsNone(read_date("21/04/26-16:17730738908"))

    def test_an_impossible_date_is_skipped_not_raised(self):
        self.assertIsNone(read_date("45/45/2026"))

    def test_falls_back_to_the_hint(self):
        hint = read_date("01/02/2026")
        self.assertEqual(read_date("no date here", date_hint=hint), hint)
