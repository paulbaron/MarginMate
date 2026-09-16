"""Renaming a product from the ticket review screen.

A receipt's product is named after its first reading - "BAGUETTE BLAND" for
a Franprix baguette read under a dozen spellings - and that name is what
every ticket's form is filled in with. Typing the right name in a line only
relabels that line: the typed name still resolves to the same product (it is
one of its readings), and the next ticket shows the misreading again. Renaming
is its own action, under the line, and says it applies everywhere.

Structurally faithful, data invented.
"""

from datetime import date
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from inventory.matching import resolve_products
from invoices.models import ShopItemPrice, Supplier
from invoices.receipts import rename_product
from tests.factories import make_invoice, make_invoice_line, make_product

FIVE_FIVE = Decimal("0.055")
D20 = Decimal("0.20")


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class RenameProductTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")
        self.product = make_product(supplier=self.shop, raw_name="PAIN SEIGLE BLAND")
        self.receipt = make_invoice(
            supplier=self.shop, parse_checks=[{"label": "x", "passed": True, "detail": ""}]
        )

    def line(self, raw_name, read_as="", product=None, invoice=None):
        return make_invoice_line(
            invoice=invoice or self.receipt, product=product or self.product, raw_name=raw_name,
            read_as=read_as, vat_rate=FIVE_FIVE,
        )

    def test_the_product_takes_the_new_name(self):
        rename_product(self.product, "PAIN SEIGLE BLANC")
        self.product.refresh_from_db()
        self.assertEqual(self.product.raw_name, "PAIN SEIGLE BLANC")

    def test_what_ocr_read_is_left_as_read_and_still_recognised(self):
        """The old spelling is a reading of the product: the next ticket
        that misreads it the same way still lands on it."""
        read = self.line("PAIN SEIGLE BLAND", read_as="PAIN SEIGLE BLAND")
        rename_product(self.product, "PAIN SEIGLE BLANC")
        read.refresh_from_db()
        self.assertEqual((read.raw_name, read.read_as), ("PAIN SEIGLE BLAND", "PAIN SEIGLE BLAND"))
        self.product.refresh_from_db()
        [(found, created)] = resolve_products(self.shop, [("PAIN SEIGLE BLAND", "")], ocr_tolerant=True)
        self.assertEqual((found, created), (self.product, False))

    def test_lines_named_after_the_product_take_the_new_name(self):
        typed = self.line("PAIN SEIGLE BLAND", read_as="PAIN SEIGIE BLAND")
        unread = self.line("PAIN SEIGLE BLAND")
        other = self.line("PAIN AU SEIGLE")
        self.assertEqual(rename_product(self.product, "PAIN SEIGLE BLANC"), 2)
        for line, expected in ((typed, "PAIN SEIGLE BLANC"), (unread, "PAIN SEIGLE BLANC"), (other, "PAIN AU SEIGLE")):
            line.refresh_from_db()
            self.assertEqual(line.raw_name, expected)

    def test_the_price_list_follows_the_product(self):
        """Or the next ticket would bring the old name back as a new product."""
        sabbh = Supplier.objects.get(code="SABBH")
        pita = make_product(supplier=sabbh, raw_name="Pain Pita")
        ShopItemPrice.objects.create(supplier=sabbh, unit_price_ttc=Decimal("0.70"), label="Pain Pita")
        ShopItemPrice.objects.create(supplier=self.shop, unit_price_ttc=Decimal("0.70"), label="Pain Pita")
        rename_product(pita, "Pain pita libanais")
        self.assertEqual(
            sorted(ShopItemPrice.objects.values_list("supplier__code", "label")),
            [("FRANPRIX", "Pain Pita"), ("SABBH", "Pain pita libanais")],
        )

    def test_spaces_are_tidied(self):
        rename_product(self.product, "  PAIN   SEIGLE BLANC ")
        self.product.refresh_from_db()
        self.assertEqual(self.product.raw_name, "PAIN SEIGLE BLANC")

    def test_an_empty_name_is_refused(self):
        with self.assertRaisesMessage(ValueError, "Donnez un nom"):
            rename_product(self.product, "   ")

    def test_a_name_another_product_of_the_shop_has_is_refused(self):
        """Two products of one name would split one item's purchases. The
        line is where a ticket is attached to the other one."""
        make_product(supplier=self.shop, raw_name="PAIN SEIGLE BLANC")
        with self.assertRaisesMessage(ValueError, "tapez ce nom dans la ligne"):
            rename_product(self.product, "pain seigle blanc")
        self.product.refresh_from_db()
        self.assertEqual(self.product.raw_name, "PAIN SEIGLE BLAND")

    def test_another_shops_product_of_that_name_is_no_obstacle(self):
        make_product(supplier=Supplier.objects.get(code="MONOPRIX"), raw_name="PAIN SEIGLE BLANC")
        rename_product(self.product, "PAIN SEIGLE BLANC")

    def test_a_name_too_long_is_refused(self):
        """The review form refuses a name over 255 characters: a product
        renamed so would block every ticket showing it."""
        with self.assertRaisesMessage(ValueError, "255 caractères au plus"):
            rename_product(self.product, "B" * 256)

    def test_a_digital_suppliers_product_is_never_renamed(self):
        """Metro's own invoices find this product by its exact name, with no
        reading to fall back on."""
        metro = Supplier.objects.get(code="METRO")
        product = make_product(supplier=metro, raw_name="RICARD 45D 1L")
        line = make_invoice_line(invoice=make_invoice(supplier=metro), product=product)
        with self.assertRaisesMessage(ValueError, "ne se renomment pas depuis un ticket"):
            rename_product(product, "Ricard 1L")
        product.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((product.raw_name, line.raw_name), ("RICARD 45D 1L", "RICARD 45D 1L"))

    def test_changing_only_the_case_is_allowed(self):
        rename_product(self.product, "Pain seigle bland")
        self.product.refresh_from_db()
        self.assertEqual(self.product.raw_name, "Pain seigle bland")


class RenameOnReviewScreenTests(TestCase):
    def setUp(self):
        self.shop = Supplier.objects.get(code="FRANPRIX")
        self.product = make_product(supplier=self.shop, raw_name="PAIN SEIGLE BLAND")
        self.receipt = self._receipt()
        self.url = reverse("invoices:receipt_review", args=[self.receipt.pk])

    def _receipt(self, read_as="PAIN SEIGIE BLANC"):
        receipt = make_invoice(
            supplier=self.shop, invoice_date=date(2026, 7, 7),
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        )
        make_invoice_line(
            invoice=receipt, product=self.product, raw_name=read_as, read_as=read_as, quantity=1,
            total_ht="0.46", vat_rate=FIVE_FIVE, printed_ttc=Decimal("0.49"),
        )
        return receipt

    def rename(self, name, product=None):
        return self.client.post(
            self.url,
            {"action": "rename_product", "product": product or self.product.pk, "name": name},
        )

    def test_the_line_offers_to_rename_its_product_everywhere(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Renommer « PAIN SEIGLE BLAND » sur tous les tickets Franprix")
        self.assertContains(response, 'name="action" value="rename_product"')
        self.assertContains(response, f'name="product" value="{self.product.pk}"')
        self.assertContains(response, "ne change que ce ticket")

    def test_a_product_on_several_lines_is_offered_once(self):
        make_invoice_line(
            invoice=self.receipt, product=self.product, raw_name="PAIN SEIGLE BLAND", read_as="PAIN SEIGLE BLAND",
            total_ht="0.46", vat_rate=FIVE_FIVE,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.content.decode().count("<summary>Renommer « PAIN SEIGLE BLAND »"), 1)

    def test_renaming_changes_every_ticket_that_shows_it(self):
        other = self._receipt(read_as="PAIN SEIGLE BLAVD")
        response = self.rename("PAIN SEIGLE BLANC")
        self.assertRedirects(response, self.url)
        self.assertIn(
            "Produit renommé sur tous les tickets Franprix : « PAIN SEIGLE BLAND » devient « PAIN SEIGLE BLANC ».",
            messages_of(response),
        )
        form = self.client.get(reverse("invoices:receipt_review", args=[other.pk])).context["formset"].forms[0]
        self.assertEqual(form.initial["product_name"], "PAIN SEIGLE BLANC")
        self.receipt.refresh_from_db()
        self.assertIsNone(self.receipt.reviewed_at, "renaming a product is not checking the ticket")

    def test_a_refused_name_is_said_so(self):
        make_product(supplier=self.shop, raw_name="PAIN SEIGLE BLANC")
        response = self.rename("PAIN SEIGLE BLANC")
        self.assertRedirects(response, self.url)
        self.assertTrue(any("s'appelle déjà" in message for message in messages_of(response)))

    def test_only_a_product_on_this_ticket_can_be_renamed_here(self):
        elsewhere = make_product(supplier=self.shop, raw_name="CITRON VERT")
        for product in (elsewhere.pk, "abc"):
            with self.subTest(product=product):
                response = self.rename("RENOMME", product=product)
                self.assertRedirects(response, self.url)
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.raw_name, "CITRON VERT")

    def test_an_unnamed_line_offers_no_rename(self):
        """"Article divers (0.70 EUR/u)" is a placeholder the ticket's
        validation replaces - renaming it would name a product about to go."""
        sabbh = Supplier.objects.get(code="SABBH")
        receipt = make_invoice(supplier=sabbh, parse_checks=[{"label": "x", "passed": True, "detail": ""}])
        make_invoice_line(
            invoice=receipt, product=make_product(supplier=sabbh, raw_name="Article divers (0.70 EUR/u)"),
            raw_name="Pain Pita", quantity=3, total_ht="1.99", vat_rate=FIVE_FIVE,
        )
        response = self.client.get(reverse("invoices:receipt_review", args=[receipt.pk]))
        self.assertNotContains(response, "Renommer « ")

    def test_a_typed_spelling_on_the_old_product_offers_it_as_the_new_name(self):
        """The row says ORANGE, the product is still RANGE: the rename under
        it names RANGE and is filled in with ORANGE."""
        line = self.receipt.lines.get()
        line.raw_name = "PAIN SEIGLE BLANC"
        line.save(update_fields=["raw_name"])
        response = self.client.get(self.url)
        self.assertContains(response, "Renommer « PAIN SEIGLE BLAND » sur tous les tickets Franprix")
        self.assertContains(response, 'name="name" value="PAIN SEIGLE BLANC"')

    def test_a_paper_ticket_filed_under_metro_offers_no_rename(self):
        metro = Supplier.objects.get(code="METRO")
        product = make_product(supplier=metro, raw_name="RICARD 45D 1L")
        receipt = make_invoice(supplier=metro, parse_checks=[{"label": "x", "passed": True, "detail": ""}])
        make_invoice_line(invoice=receipt, product=product, total_ht="15.00", vat_rate=D20)
        url = reverse("invoices:receipt_review", args=[receipt.pk])
        self.assertNotContains(self.client.get(url), "Renommer « ")
        refused = self.client.post(url, {"action": "rename_product", "product": product.pk, "name": "Ricard 1L"})
        self.assertTrue(any("ne se renomment pas" in message for message in messages_of(refused)))
        saved = self.client.post(
            url,
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-product_name": "RICARD 45D 1 L",
                "form-0-quantity": "1",
                "form-0-total_ttc": "18.00",
                "form-0-vat_rate": "20",
            },
        )
        self.assertFalse(any("Renommer" in message for message in messages_of(saved)))

    def test_a_typed_name_kept_on_its_product_says_where_to_rename_it(self):
        """The trap this screen had: the right spelling typed in the line,
        attached straight back to the product with the wrong one."""
        response = self.client.post(
            self.url,
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-product_name": "PAIN SEIGLE BLANC",
                "form-0-read_as": "PAIN SEIGIE BLANC",
                "form-0-quantity": "1",
                "form-0-total_ttc": "0.49",
                "form-0-vat_rate": "5.5",
            },
        )
        self.assertEqual(self.receipt.lines.get().product, self.product)
        self.assertTrue(
            any(
                "« PAIN SEIGLE BLANC » est rattaché au produit « PAIN SEIGLE BLAND »" in message
                and "Renommer" in message
                for message in messages_of(response)
            ),
            messages_of(response),
        )
