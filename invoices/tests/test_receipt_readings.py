"""A product is known by every name its receipts have been read as.

One till label, "BAGUETTE BLANC", came back from OCR as BAGUETTE BLAND,
BLAVC, BLAVD, BAGLETTE, BAGUETRE, BAGUETIE and AGUETTE BLANC across fourteen
Franprix tickets: two readings of it can be three mistakes apart. Compared
with the product's one stored name - itself whichever reading came first -
most of them became products of their own. Widening the budget instead
joined real, different products ("CANTAL AOP ED" and "CANTAL AOP JNE").
"""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from inventory.models import Product
from invoices.importing import import_parsed_invoice, replace_invoice_lines
from invoices.models import Supplier
from invoices.parsers import ticket_parser_for
from invoices.parsers.base import ParsedInvoice, ParsedLine
from invoices.parsers.generic_receipt import GenericReceiptParser
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_type,
)

FIVE_FIVE = Decimal("0.055")
CHECKED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]


def line(name, read_as=None):
    """A line as the receipt parsers hand it over: named as it was read."""
    return ParsedLine(
        raw_name=name,
        quantity=1,
        total_volume=Decimal("0"),
        unit_cost_ht=Decimal("0.46"),
        total_ht=Decimal("0.46"),
        vat_rate=FIVE_FIVE,
        read_as=name if read_as is None else read_as,
    )


def ticket(*names, number="000001"):
    return ParsedInvoice(
        supplier_code="FRANPRIX",
        invoice_number=number,
        invoice_date=date(2026, 6, 20),
        lines=[line(name) for name in names],
        from_ocr=True,
    )


def products_of(invoice):
    return [invoice_line.product for invoice_line in invoice.lines.all()]


class OneTicketTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")
        self.baguette = make_product(supplier=self.shop, raw_name="BAGUETTE BLAND")

    def test_the_readings_of_one_ticket_find_the_product_together(self):
        """The ticket that showed it. Its first line alone is two mistakes
        from the stored name; it is one from the second line, which is one
        from the product - the same label, read twice on the same photo."""
        invoice = import_parsed_invoice(self.shop, ticket("AGUETTE BLANC", "AGUETTE BLAND", "AGUETTE BLANC"))
        self.assertEqual(set(products_of(invoice)), {self.baguette})
        self.assertEqual(Product.objects.filter(supplier=self.shop).count(), 1)

    def test_the_order_of_the_lines_does_not_decide(self):
        for number, names in enumerate((("AGUETTE BLAND", "AGUETTE BLANC"), ("AGUETTE BLANC", "AGUETTE BLAND"))):
            with self.subTest(order=names):
                invoice = import_parsed_invoice(self.shop, ticket(*names, number=str(number)))
                self.assertEqual(set(products_of(invoice)), {self.baguette})

    def test_each_line_keeps_what_was_read(self):
        invoice = import_parsed_invoice(self.shop, ticket("AGUETTE BLANC", "AGUETTE BLAND"))
        self.assertEqual([saved.read_as for saved in invoice.lines.all()], ["AGUETTE BLANC", "AGUETTE BLAND"])

    def test_misreadings_of_a_new_name_share_one_new_product(self):
        invoice = import_parsed_invoice(self.shop, ticket("TOMATE GRAPPE", "T0MATE GRAPPE"))
        tomato, again = products_of(invoice)
        self.assertEqual((tomato.raw_name, again), ("TOMATE GRAPPE", tomato))

    def test_a_reading_never_bridges_a_different_number(self):
        """Readings join spellings, never sizes."""
        lemons = make_product(supplier=self.shop, raw_name="CITRON SHT 500G")
        read_as_lemons, other_size = products_of(import_parsed_invoice(self.shop, ticket("CITRON SHT 5OOG", "CITRON SHT 250G")))
        self.assertEqual(read_as_lemons, lemons)
        self.assertNotEqual(other_size, lemons)


class AcrossTicketsTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")

    def test_a_reading_from_an_earlier_ticket_is_a_name_the_product_is_known_by(self):
        """AGUETTE BLANC is two mistakes from the name the product was
        created under, and one from how a later ticket read it."""
        (baguette,) = products_of(import_parsed_invoice(self.shop, ticket("BAGUETTE BLAND", number="1")))
        import_parsed_invoice(self.shop, ticket("BAGUETTE BLANC", number="2"))
        third = import_parsed_invoice(self.shop, ticket("AGUETTE BLANC", number="3"))
        self.assertEqual(products_of(third), [baguette])
        self.assertEqual(Product.objects.filter(supplier=self.shop).count(), 1)


class ReviewScreenTests(TestCase):
    """The reading is shown under its row and survives the save."""

    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")
        self.baguette = make_product(supplier=self.shop, raw_name="BAGUETTE BLAND")
        self.invoice = make_invoice(supplier=self.shop, parse_checks=CHECKED)
        make_invoice_line(
            invoice=self.invoice,
            product=self.baguette,
            total_ht="0.46",
            vat_rate=FIVE_FIVE,
            raw_name="AGUETTE BLANC",
            read_as="AGUETTE BLANC",
        )
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def post(self, rows, indices=None):
        indices = list(indices if indices is not None else range(len(rows)))
        payload = {
            "form-TOTAL_FORMS": str(max(indices) + 1),
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "invoice_date": "2026-01-01",
        }
        for index, (name, read_as) in zip(indices, rows):
            payload.update(
                {
                    f"form-{index}-product_name": name,
                    f"form-{index}-read_as": read_as,
                    f"form-{index}-quantity": "1",
                    f"form-{index}-total_ttc": "0.49",
                    f"form-{index}-vat_rate": "5.5",
                }
            )
        return self.client.post(self.url, payload)

    def test_the_row_shows_the_product_and_what_was_read(self):
        response = self.client.get(self.url)
        form = response.context["formset"].forms[0]
        self.assertEqual(
            (form.initial["product_name"], form.initial["read_as"]), ("BAGUETTE BLAND", "AGUETTE BLANC")
        )
        self.assertContains(response, "lu sur le ticket : « AGUETTE BLANC »")

    def test_saving_keeps_the_reading(self):
        self.assertEqual(self.post([("BAGUETTE BLAND", "AGUETTE BLANC")]).status_code, 302)
        saved = self.invoice.lines.get()
        self.assertEqual(
            (saved.product, saved.raw_name, saved.read_as), (self.baguette, "BAGUETTE BLAND", "AGUETTE BLANC")
        )
        self.assertContains(self.client.get(self.url), "lu sur le ticket : « AGUETTE BLANC »")

    def test_rows_removed_in_the_browser_leave_gaps_and_each_reading_stays_with_its_row(self):
        """What the page really posts after a row is taken out: 0 and 2, no 1."""
        response = self.post([("BAGUETTE BLAND", "AGUETTE BLANC"), ("CITRON VERT", "CITRCN VERT")], indices=[0, 2])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [(saved.raw_name, saved.read_as) for saved in self.invoice.lines.all()],
            [("BAGUETTE BLAND", "AGUETTE BLANC"), ("CITRON VERT", "CITRCN VERT")],
        )


class CorrectionTests(TestCase):
    """A misreading the matcher could not place, attached by hand."""

    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")
        self.baguette = make_product(supplier=self.shop, raw_name="BAGUETTE BLANC")

    def _correct(self, number):
        invoice = import_parsed_invoice(self.shop, ticket("BGT BLNC", number=number))
        invoice.parse_checks = CHECKED
        invoice.save(update_fields=["parse_checks"])
        (misread,) = products_of(invoice)
        replace_invoice_lines(invoice, [line("BAGUETTE BLANC", read_as="BGT BLNC")])
        return invoice, misread

    def test_it_is_recognised_on_the_next_ticket(self):
        invoice, misread = self._correct("1")
        self.assertNotEqual(misread, self.baguette, "too far from the name to be guessed")
        self.assertEqual(products_of(invoice), [self.baguette])
        later = import_parsed_invoice(self.shop, ticket("BGT BLNC", number="2"))
        self.assertEqual(products_of(later), [self.baguette])

    def test_the_product_the_misreading_had_created_goes(self):
        """Otherwise it waits in the review queue for ever, under a name no
        product has."""
        _invoice, misread = self._correct("1")
        self.assertFalse(Product.objects.filter(pk=misread.pk).exists())

    def test_a_product_someone_classified_or_another_invoice_uses_stays(self):
        classified = make_product(supplier=self.shop, raw_name="PAIN COMPLET", stock_type=make_stock_type())
        shared = make_product(supplier=self.shop, raw_name="CROISSANT")
        make_invoice_line(invoice=make_invoice(supplier=self.shop), product=shared)
        invoice = make_invoice(supplier=self.shop, parse_checks=CHECKED)
        for product in (classified, shared):
            make_invoice_line(invoice=invoice, product=product)
        replace_invoice_lines(invoice, [line("BAGUETTE BLANC")])
        self.assertEqual(Product.objects.filter(pk__in=[classified.pk, shared.pk]).count(), 2)


class ParseOcrPagesTests(SimpleTestCase):
    def test_a_named_line_keeps_its_reading_and_a_placeholder_has_none(self):
        """"Article divers" is a price, not a name: taken as a reading, it
        would name the next 0,70 line without asking the price list, whose
        answer changes with the date."""
        named, placeholder = line("MENTHE", read_as=""), line("Article divers", read_as="")
        placeholder.is_placeholder = True
        parsed = ParsedInvoice(
            supplier_code="WINGSENG", invoice_number="1", invoice_date=None, lines=[named, placeholder]
        )
        with mock.patch.object(GenericReceiptParser, "parse_pages", return_value=parsed):
            result = ticket_parser_for("WINGSENG").parse_ocr_pages([])
        self.assertEqual([parsed_line.read_as for parsed_line in result.lines], ["MENTHE", ""])
