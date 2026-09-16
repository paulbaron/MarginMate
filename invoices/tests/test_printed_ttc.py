"""A receipt line keeps the amount its ticket printed.

Receipt lines were stored in HT only, and the TTC shown on the review screen
was worked back from it. That does not always land on the printed figure: at
5.5%, 7,00 TTC is 6,6351 HT, stored as 6,64 - which is 7,0052 TTC, shown as
7,01. No HT to the cent gives 7,00 back (6,63 gives 6,99). A receipt of ten
pitas at 0,70 read 7,01 on the very screen meant to check it against the
photo. So each line carries `printed_ttc`, and everything shown in TTC uses it.

Structurally faithful, data invented.
"""

import os
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from invoices.importing import import_parsed_invoice
from invoices.models import Supplier
from invoices.parsers.base import ParsedInvoice, ParsedLine, PdfPage
from invoices.parsers.franprix import FranprixParser
from invoices.parsers.monoprix import MonoprixParser
from invoices.parsers.sabbh import SabbhParser
from invoices.parsers.wingseng import WingSengParser
from invoices.management.commands.restore_printed_ttc import restore_printed_ttc
from invoices.receipts import printed_unit_price
from invoices.tests import test_parser_franprix as franprix
from invoices.tests import test_parser_monoprix as monoprix
from invoices.tests import test_parser_sabbh as sabbh
from invoices.tests import test_parser_wingseng as wingseng
from tests.factories import make_invoice, make_invoice_line, make_product

FIVE_FIVE = Decimal("0.055")
D = Decimal

# Ten at 0,70: 7,00 TTC, which no HT to the cent converts back to.
SEVEN_EUROS = """Sabbh Oriental
12 Rue Inventee
Vendeur:V1  Numero de ticket:6800009
Heure: 11-09-2026 10:12:00  Balance: 68
PLU
kg(pcs) €/kg(pcs)
Article divers
10pcs  0,70  7,00 A
Articles: 1  Total: 7,00
Espèces  7,00€
Information TVA
Taux  Base TVA  TVA
TVAA 5.50%  7,00  0,36
"""


def parse(parser, text):
    return parser.parse_pages([PdfPage(text=text)], source_name="ticket.pdf")


class ParsersKeepThePrintedAmountTests(SimpleTestCase):
    def test_sabbh(self):
        invoice = parse(SabbhParser(), sabbh.BASIC)
        self.assertEqual([line.printed_ttc for line in invoice.lines], [D("2.40"), D("9.00")])

    def test_the_amount_that_does_not_survive_the_round_trip(self):
        (line,) = parse(SabbhParser(), SEVEN_EUROS).lines
        self.assertEqual((line.total_ht, line.printed_ttc), (D("6.64"), D("7.00")))
        self.assertNotEqual((line.total_ht * (1 + line.vat_rate)).quantize(D("0.01")), D("7.00"))

    def test_wing_seng(self):
        invoice = parse(WingSengParser(), wingseng.BASIC)
        self.assertEqual(
            {line.raw_name: line.printed_ttc for line in invoice.lines},
            {"MENTHE": D("1.00"), "CITRON VERT": D("9.00")},
        )

    def test_monoprix(self):
        invoice = parse(MonoprixParser(), monoprix.FRENCH)
        self.assertEqual([line.printed_ttc for line in invoice.lines], [D("2.40"), D("6.00")])

    def test_the_ticket_total_is_kept(self):
        for parser, text, total in (
            (SabbhParser(), sabbh.BASIC, D("11.40")),
            (WingSengParser(), wingseng.BASIC, D("10.00")),
            (MonoprixParser(), monoprix.FRENCH, D("8.40")),
            (FranprixParser(), franprix.DISCOUNTED, D("2.90")),
        ):
            with self.subTest(parser=parser.supplier_code):
                self.assertEqual(parse(parser, text).printed_total_ttc, total)

    def test_franprix(self):
        for text, expected in ((franprix.TWENTY_PERCENT, [D("3.60"), D("3.60")]), (franprix.MULTIPLIER, [D("3.30")])):
            with self.subTest(expected=expected):
                self.assertEqual([line.printed_ttc for line in parse(FranprixParser(), text).lines], expected)

    def test_a_discounted_line_no_longer_has_a_printed_amount(self):
        """The ticket prints the loaf at 0,55 and the promotion elsewhere:
        what the loaf finally cost is worked out, not read."""
        invoice = parse(FranprixParser(), franprix.DISCOUNTED)
        amounts = {(line.raw_name, line.printed_ttc) for line in invoice.lines}
        self.assertIn(("CITRON VERT 400G", D("1.80")), amounts)
        self.assertEqual({amount for name, amount in amounts if "PAIN" in name}, {None})

    def test_the_printed_unit_price_comes_from_the_printed_amount(self):
        line = ParsedLine(
            raw_name="Article divers", quantity=10, total_volume=D("0"), unit_cost_ht=D("0.664"),
            total_ht=D("6.64"), vat_rate=FIVE_FIVE, printed_ttc=D("7.00"),
        )
        self.assertEqual(printed_unit_price(line), D("0.70"))
        line.printed_ttc = None
        self.assertEqual(printed_unit_price(line), D("0.70"))


class TotalsTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="SABBH")

    def receipt(self, *lines, adjustment="0", paid=None):
        invoice = make_invoice(
            supplier=self.shop,
            parse_checks=[{"label": "x", "passed": True, "detail": ""}],
            reconciliation_adjustment=D(adjustment),
            printed_total_ttc=None if paid is None else D(paid),
        )
        for total_ht, printed_ttc in lines:
            make_invoice_line(
                invoice=invoice, product=make_product(supplier=self.shop), total_ht=total_ht,
                vat_rate=FIVE_FIVE, printed_ttc=None if printed_ttc is None else D(printed_ttc),
            )
        return invoice

    def test_a_line_shows_what_was_printed(self):
        invoice = self.receipt(("6.64", "7.00"))
        self.assertEqual(invoice.lines.get().total_ttc, D("7.00"))

    def test_a_line_without_a_printed_amount_is_worked_out_from_ht(self):
        invoice = self.receipt(("10.00", None))
        self.assertEqual(invoice.lines.get().total_ttc, D("10.55"))

    def test_the_receipt_total_is_what_was_printed(self):
        self.assertEqual(self.receipt(("6.64", "7.00")).total_ttc, D("7.00"))

    def test_the_ht_rounding_adjustment_is_not_added_on_top(self):
        """Six baguettes at 0,49: 0,46 HT each, and the ticket's HT base puts
        back the three cents that rounding lost. Those cents were never
        missing from the printed amounts."""
        invoice = self.receipt(*[("0.46", "0.49")] * 6, adjustment="0.03")
        self.assertEqual(invoice.total_ht, D("2.79"))
        self.assertEqual(invoice.total_ttc, D("2.94"))

    def test_one_line_without_a_printed_amount_keeps_the_ht_arithmetic(self):
        invoice = self.receipt(("0.46", "0.49"), ("0.50", None), adjustment="0.01")
        expected = D("0.46") * (1 + FIVE_FIVE) + D("0.50") * (1 + FIVE_FIVE) + D("0.01") * (1 + FIVE_FIVE)
        self.assertEqual(invoice.total_ttc, expected)

    def test_a_misread_cent_within_tolerance_still_totals_what_was_paid(self):
        """The parser lets lines 0,03 off the printed total pass; the total
        is still what the bank was charged."""
        self.assertEqual(self.receipt(("9.48", "10.00"), paid="10.03").total_ttc, D("10.03"))

    def test_a_ticket_the_lines_no_longer_add_up_to_totals_its_lines(self):
        """A line added or removed on the review screen: the lines decide."""
        self.assertEqual(self.receipt(("9.48", "10.00"), paid="11.00").total_ttc, D("10.00"))
        self.assertEqual(self.receipt(("9.48", "10.00")).total_ttc, D("10.00"))

    def test_the_import_stores_it(self):
        parsed = parse(SabbhParser(), SEVEN_EUROS)
        invoice = import_parsed_invoice(self.shop, parsed)
        self.assertEqual(invoice.lines.get().printed_ttc, D("7.00"))
        self.assertEqual(invoice.printed_total_ttc, D("7.00"))
        self.assertEqual(invoice.total_ttc, D("7.00"))


class ReviewScreenTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="SABBH")
        self.invoice = make_invoice(
            supplier=self.shop, invoice_date=date(2026, 9, 11),
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        )
        make_invoice_line(
            invoice=self.invoice, product=make_product(supplier=self.shop, raw_name="Pain Pita"),
            raw_name="Pain Pita", quantity=10, total_ht="6.64", unit_cost_ht="0.664",
            vat_rate=FIVE_FIVE, printed_ttc=D("7.00"),
        )
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def test_the_form_shows_the_printed_amount(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["formset"].forms[0].initial["total_ttc"], D("7.00"))
        self.assertContains(response, "7.00 € TTC")

    def test_saving_keeps_the_amount_typed(self):
        self.client.post(
            self.url,
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-product_name": "Pain Pita",
                "form-0-quantity": "10",
                "form-0-total_ttc": "7.00",
                "form-0-vat_rate": "5.5",
            },
        )
        line = self.invoice.lines.get()
        self.assertEqual((line.total_ht, line.printed_ttc), (D("6.64"), D("7.00")))
        self.assertEqual(self.invoice.total_ttc, D("7.00"))


class WorkedOutAmountsOnReviewTests(TestCase):
    """A line whose ticket amount is not known shows one worked out from HT.
    Saved untouched, it must stay worked out: stored as printed, the invoice
    total switched to adding up "printed" figures that were never printed."""

    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")
        self.invoice = make_invoice(
            supplier=self.shop, parse_checks=[{"label": "x", "passed": True, "detail": ""}]
        )
        for _ in range(3):
            make_invoice_line(
                invoice=self.invoice, product=make_product(supplier=self.shop, raw_name=f"PAIN {_}"),
                raw_name=f"PAIN {_}", total_ht="0.33", vat_rate=FIVE_FIVE,
            )
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def payload(self, **changes):
        forms = self.client.get(self.url).context["formset"].forms
        data = {
            "form-TOTAL_FORMS": str(len(forms)),
            "form-INITIAL_FORMS": str(len(forms)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for index, form in enumerate(forms):
            for field in ("product_name", "quantity", "total_ttc", "vat_rate", "read_as", "computed_ttc"):
                value = form.initial.get(field)
                data[f"form-{index}-{field}"] = "" if value is None else str(value)
        data.update(changes)
        return data

    def test_saved_untouched_it_stays_worked_out(self):
        before = self.invoice.total_ttc
        self.client.post(self.url, self.payload())
        self.assertEqual([line.printed_ttc for line in self.invoice.lines.all()], [None, None, None])
        self.assertEqual(self.invoice.total_ttc, before)

    def test_corrected_it_becomes_the_ticket_amount(self):
        self.client.post(self.url, self.payload(**{"form-0-total_ttc": "0.36"}))
        self.assertEqual([line.printed_ttc for line in self.invoice.lines.all()], [D("0.36"), None, None])

    def test_a_row_typed_in_is_a_ticket_amount(self):
        data = self.payload()
        data.update(
            {
                "form-TOTAL_FORMS": "4",
                "form-3-product_name": "CITRON",
                "form-3-quantity": "1",
                "form-3-total_ttc": "0.35",
                "form-3-vat_rate": "5.5",
            }
        )
        self.client.post(self.url, data)
        self.assertEqual(
            {line.raw_name: line.printed_ttc for line in self.invoice.lines.all()}["CITRON"], D("0.35")
        )


class RestorePrintedTtcTests(TestCase):
    """Receipts imported before lines kept their printed amount get it back
    from their own stored reading."""

    def setUp(self):
        self.shop = Supplier.objects.get(code="SABBH")
        self.invoice = make_invoice(
            supplier=self.shop, invoice_number="6800009", ocr_text=SEVEN_EUROS,
            parse_checks=[{"label": "x", "passed": True, "detail": ""}],
        )

    def line(self, total_ht="6.64", quantity=10, name="Pain Pita", **kwargs):
        return make_invoice_line(
            invoice=self.invoice, product=make_product(supplier=self.shop), raw_name=name,
            quantity=quantity, total_ht=total_ht, vat_rate=FIVE_FIVE, **kwargs,
        )

    def test_a_line_gets_the_amount_read_on_its_ticket(self):
        """By count and HT, not by name: a price list renames the line."""
        line = self.line()
        self.assertEqual(restore_printed_ttc(self.invoice), 1)
        line.refresh_from_db()
        self.assertEqual(line.printed_ttc, D("7.00"))
        self.assertEqual(self.invoice.total_ttc, D("7.00"))

    def test_a_line_corrected_since_is_left_alone(self):
        line = self.line(total_ht="6.16")
        self.assertEqual(restore_printed_ttc(self.invoice), 0)
        line.refresh_from_db()
        self.assertIsNone(line.printed_ttc)

    def test_a_reading_is_used_once(self):
        first, second = self.line(), self.line()
        self.assertEqual(restore_printed_ttc(self.invoice), 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.printed_ttc, second.printed_ttc), (D("7.00"), None))

    def test_a_line_that_already_has_it_is_not_touched(self):
        self.line(printed_ttc=D("7.00"))
        self.assertEqual(restore_printed_ttc(self.invoice), 0)

    def test_a_digital_invoice_has_nothing_to_restore(self):
        invoice = make_invoice(supplier=Supplier.objects.get(code="METRO"))
        make_invoice_line(invoice=invoice)
        self.assertEqual(restore_printed_ttc(invoice), 0)

    def test_the_printed_total_comes_back_too(self):
        self.line()
        restore_printed_ttc(self.invoice)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.printed_total_ttc, D("7.00"))

    def test_the_command_changes_nothing_on_a_dry_run(self):
        line = self.line()
        call_command("restore_printed_ttc", "--dry-run", stdout=open(os.devnull, "w"))
        line.refresh_from_db()
        self.assertIsNone(line.printed_ttc)
        call_command("restore_printed_ttc", stdout=open(os.devnull, "w"))
        line.refresh_from_db()
        self.assertEqual(line.printed_ttc, D("7.00"))

    def test_a_ticket_that_no_longer_parses_is_left_alone(self):
        self.invoice.ocr_text = "illisible"
        self.invoice.save(update_fields=["ocr_text"])
        self.line()
        self.assertEqual(restore_printed_ttc(self.invoice), 0)
