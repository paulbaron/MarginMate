"""Recovering a receipt's VAT when the recogniser mangled its table.

Every line of a receipt is divided by (1 + rate) on its way in, so a ticket
whose rate goes unread is booked at its tax-inclusive price - a cost
overstated by the whole tax, on a receipt whose TTC sum still balances. These
cover the ways the rate can still be recovered honestly, and the rule that
anything computed rather than read is reported as such.

Rows are copied from real recognitions of the corpus; the amounts around them
are invented.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.parsers.receipt_base import (
    ReceiptTotals,
    VatSummary,
    build_checks,
    collect_vat_summaries,
    infer_vat_row,
    line_amounts,
    parse_vat_line,
)

D = Decimal
FIVE_FIVE = D("0.055")


class MoneyIsCentsTests(SimpleTestCase):
    def test_a_euro_sign_read_as_a_digit_is_not_a_third_decimal(self):
        """"8,44€" arrives as "8,448"; kept, it invented a 0,008 EUR discount."""
        self.assertEqual(line_amounts("TOTAL HORS AVANTAGES  8,448"), [D("8.44")])
        self.assertEqual(line_amounts("IOTAI HOR: AVANTAGES  14,302"), [D("14.30")])

    def test_a_plain_total(self):
        self.assertEqual(line_amounts("TOTAL A PAYER  13.06"), [D("13.06")])


class DerivedFromTheTotalTests(SimpleTestCase):
    def test_a_mangled_row_confirmed_by_one_of_its_own_numbers_counts_as_read(self):
        """The VAT amount is lost in "0.192063.671", but the HT the total
        implies (3.48) is printed on the row."""
        summary = parse_vat_line("15.5%1  3.481  0.192063.671", expected_total=D("3.67"))
        self.assertEqual((summary.rate, summary.base, summary.vat_amount), (FIVE_FIVE, D("3.48"), D("0.19")))
        self.assertFalse(summary.derived)

    def test_a_row_with_only_its_rate_legible_is_derived(self):
        summary = parse_vat_line("15.5%  44  0.471  7.85", expected_total=D("7.85"))
        self.assertEqual((summary.rate, summary.base, summary.vat_amount), (FIVE_FIVE, D("7.44"), D("0.41")))
        self.assertTrue(summary.derived)

    def test_without_a_printed_total_nothing_is_derived(self):
        summary = parse_vat_line("TVA5.50%:  .'O EUR")
        self.assertEqual(summary.rate, FIVE_FIVE)
        self.assertIsNone(summary.base)


class InferVatRowTests(SimpleTestCase):
    def test_an_illegible_rate_is_named_by_the_arithmetic(self):
        summary = infer_vat_row("XSG  5.74  0.32  6.061", D("6.06"))
        self.assertEqual((summary.rate, summary.base, summary.vat_amount), (FIVE_FIVE, D("5.74"), D("0.32")))

    def test_a_tax_inclusive_base(self):
        """Sabbh's "Base TVA" column is the TTC total."""
        summary = infer_vat_row("4,90  0,26", D("4.90"))
        self.assertEqual((summary.rate, summary.base, summary.vat_amount), (FIVE_FIVE, D("4.64"), D("0.26")))

    def test_two_rates_fitting_is_no_answer(self):
        """0,03 EUR of tax on 0,49 EUR fits 5.5% and 10% alike."""
        self.assertIsNone(infer_vat_row("0,49  0,03", D("0.49")))

    def test_a_line_that_is_not_a_vat_row(self):
        self.assertIsNone(infer_vat_row("CB SANS CONTACT  6.06", D("6.06")))
        self.assertIsNone(infer_vat_row("XSG  5.74  0.32  6.061", None))


class CollectVatSummariesTests(SimpleTestCase):
    def test_a_repeated_total_row_is_one_bucket(self):
        summaries, rate_only = collect_vat_summaries(
            ["TVA  H.T.  T.V.A.  T.T.C", "5,5%  9,74  0,54  10,28", "Total TVA 5,5%  9,74  0,54  10,28"], D("10.28")
        )
        self.assertEqual([(s.rate, s.base) for s in summaries], [(FIVE_FIVE, D("9.74"))])
        self.assertEqual(rate_only, [])

    def test_a_bucket_actually_read_beats_one_derived_from_the_total(self):
        summaries, _ = collect_vat_summaries(["15.5%  44  0.471  7.85", "5,5%  7,44  0,41  7,85"], D("7.85"))
        self.assertEqual(len(summaries), 1)
        self.assertFalse(summaries[0].derived)

    def test_a_rate_alone_with_no_total_to_work_from(self):
        summaries, rate_only = collect_vat_summaries(["TVA5.50%:  .'O EUR"], None)
        self.assertEqual((summaries, rate_only), ([], [FIVE_FIVE]))

    def test_a_rate_whose_row_reads_nothing_useful_is_derived_not_coherent(self):
        """Filled in from the grand total, a rate-only row used to pass as a
        coherent VAT table - trivially, since the split it computed agreed
        with itself."""
        summaries, _ = collect_vat_summaries(["TVA 5,50%  1,00"], D("6.06"))
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0].derived)

    def test_arithmetic_names_a_rate_only_when_none_is_legible(self):
        summaries, _ = collect_vat_summaries(["XSG  5.74  0.32  6.061"], D("6.06"))
        self.assertEqual([s.rate for s in summaries], [FIVE_FIVE])


class ReportingTests(SimpleTestCase):
    def test_a_derived_bucket_is_reported_as_unread(self):
        totals = ReceiptTotals(
            printed_total_ttc=D("7.85"),
            vat_summaries=[
                VatSummary(rate=FIVE_FIVE, base=D("7.44"), vat_amount=D("0.41"), total_ttc=D("7.85"), derived=True)
            ],
        )
        checks, _ = build_checks(totals, D("7.85"), lines_total_ht=D("7.44"), line_count=3)
        labels = {check.label: check for check in checks}
        self.assertNotIn("TVA 5.5% cohérente", labels)
        self.assertFalse(labels["Table TVA 5.5%"].passed)
        self.assertIn("calculés depuis le total", labels["Table TVA 5.5%"].detail)

    def test_a_table_with_only_its_rate_legible_says_which_rate(self):
        checks, _ = build_checks(ReceiptTotals(rate_only=[FIVE_FIVE]), D("13.51"))
        table = next(check for check in checks if check.label == "Table TVA lue")
        self.assertFalse(table.passed)
        self.assertIn("5.5%", table.detail)

    def test_lines_are_priced_at_the_single_legible_rate(self):
        self.assertEqual(ReceiptTotals(rate_only=[FIVE_FIVE]).line_rate, FIVE_FIVE)
        self.assertIsNone(ReceiptTotals(rate_only=[FIVE_FIVE, D("0.20")]).line_rate)
