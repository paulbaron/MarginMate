"""Suppliers whose documents are charges, not goods: a phone subscription,
the rent, the water.

Their invoices have nothing to classify - there is no product behind a rent -
so they are filed as one line per VAT rate, named after the supplier, on a
product that never reaches the stock pages. What they cost is shown on
"Produits" all the same, in a fold of its own: charges are not stock, but
they are spending.

Data invented.
"""

from datetime import date
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product, StockMovement
from inventory.views import review_panel_context
from invoices.importing import import_parsed_invoice, redo_as_expenses, replace_invoice_lines
from invoices.receipts import pending_receipts, reread_receipt
from invoices.models import Invoice, Supplier
from invoices.parsers.base import ParsedInvoice, ParsedLine
from tests.factories import make_supplier

D = Decimal


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def parsed(lines=(), breakdown=(), total=None, number="F-1", when=date(2026, 5, 19), text=""):
    return ParsedInvoice(
        supplier_code="FREE_X",
        invoice_number=number,
        invoice_date=when,
        lines=list(lines),
        vat_breakdown=list(breakdown),
        printed_total_ttc=total,
        source_text=text,
    )


# A rent statement, as invoices/tests/test_charges_postes.py describes it.
STATEMENT = """AVIS D'ECHEANCE du mois de décembre 2025
Montant  Détaildel'avisd'échéance  Montant
Soldeantérieurau21/10/2025  0,00  LOYERLOCAUXACTIVITEHT  600,00
ECHEANCEau01/11/2025  830,00  PROV.CHARGESIMMEUBLE  90,00
PRELV.SEPAau10/11/2025  -  830,00  PROVISIONEAUFROIDE  10,00
TVATAUXNORMAL  120,00
Totaldevotreavisd'échéance(B)  820,00
Prélevéle10/12/2025  820,00"""


def line(name, total_ht, rate="0.20"):
    return ParsedLine(
        raw_name=name, quantity=1, total_volume=D("0"), unit_cost_ht=D(total_ht),
        total_ht=D(total_ht), vat_rate=D(rate),
    )


class PosteByPosteTests(TestCase):
    """A charge that says what it is made of keeps it: the rent is not the
    provisions, and the provisions are the ones that get regularised."""

    def setUp(self):
        self.supplier = make_supplier(code="BAILLEUR", name="Bailleur Exemple", parser_key="", expenses_only=True)

    def test_each_poste_is_its_own_line_at_its_own_rate(self):
        invoice = import_parsed_invoice(self.supplier, parsed(total=D("830.00"), text=STATEMENT))
        self.assertEqual(
            [(line.raw_name, line.total_ht, line.vat_rate) for line in invoice.lines.all()],
            [
                ("LOYERLOCAUXACTIVITEHT", D("600.00"), D("0.20")),
                ("PROV.CHARGESIMMEUBLE", D("90.00"), D("0")),
                ("PROVISIONEAUFROIDE", D("10.00"), D("0")),
            ],
        )

    def test_the_statement_is_worth_what_it_charges(self):
        """830,00 is last month's échéance, printed twice; 820,00 is what
        this avis calls for."""
        invoice = import_parsed_invoice(self.supplier, parsed(total=D("830.00"), text=STATEMENT))
        self.assertEqual((invoice.printed_total_ttc, invoice.total_ttc), (D("820.00"), D("820.00")))
        self.assertEqual(invoice.status, Invoice.Status.COMPLETE)

    def test_no_poste_is_a_product_to_classify(self):
        import_parsed_invoice(self.supplier, parsed(total=D("830.00"), text=STATEMENT))
        products = Product.objects.filter(supplier=self.supplier)
        self.assertEqual(products.count(), 3)
        self.assertTrue(all(product.is_expense for product in products))
        self.assertEqual(review_panel_context()["review_total"], 0)
        self.assertFalse(StockMovement.objects.exists())

    def test_every_month_files_on_the_same_postes(self):
        for month in (1, 2):
            import_parsed_invoice(
                self.supplier, parsed(total=D("830.00"), text=STATEMENT, number=f"A-{month}", when=date(2026, month, 1))
            )
        self.assertEqual(Product.objects.filter(supplier=self.supplier).count(), 3)

    def test_marking_the_supplier_redoes_what_is_filed_from_the_document(self):
        """The documents are read again from the text they kept: filed as
        stock, their lines say nothing about what the statement charges."""
        plain = make_supplier(code="BAILLEUR2", name="Bailleur Deux", parser_key="")
        invoice = import_parsed_invoice(
            plain,
            parsed(lines=[line("LOYER", "500.00")], total=D("830.00"), text=STATEMENT),
        )
        # What the document itself said, kept at import (Invoice.source_text).
        Invoice.objects.filter(pk=invoice.pk).update(source_text=STATEMENT)
        Supplier.objects.filter(pk=plain.pk).update(expenses_only=True)
        plain.refresh_from_db()
        self.assertEqual(redo_as_expenses(plain), 1)
        invoice.refresh_from_db()
        self.assertEqual(
            [(line.raw_name, line.total_ht) for line in invoice.lines.all()],
            [("LOYERLOCAUXACTIVITEHT", D("600.00")), ("PROV.CHARGESIMMEUBLE", D("90.00")),
             ("PROVISIONEAUFROIDE", D("10.00"))],
        )
        self.assertEqual(invoice.printed_total_ttc, D("820.00"))


class ImportedAsOneLineTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="FREE_X", name="Free Exemple", parser_key="", expenses_only=True)

    def test_the_detail_is_replaced_by_one_line_per_rate(self):
        invoice = import_parsed_invoice(
            self.supplier,
            parsed(
                lines=[line("Abonnement mobile", "8.33"), line("Option", "2.00")],
                breakdown=[(D("0.20"), D("8.33"), D("1.66")), (D("0.055"), D("2.00"), D("0.11"))],
                total=D("12.10"),
            ),
        )
        self.assertEqual(
            [(line.raw_name, line.quantity, line.total_ht, line.vat_rate) for line in invoice.lines.order_by("vat_rate")],
            [("Free Exemple", 1, D("2.00"), D("0.055")), ("Free Exemple", 1, D("8.33"), D("0.20"))],
        )
        self.assertEqual(invoice.status, Invoice.Status.COMPLETE)

    def test_the_amount_kept_is_the_one_the_document_charges(self):
        """A subscription is a TTC figure - it is what leaves the bank.
        33,33 € HT at 20% works back out to 40,00 € where the invoice says
        39,99 €, and 39,99 € is what was paid."""
        invoice = import_parsed_invoice(
            self.supplier,
            parsed(breakdown=[(D("0.20"), D("33.33"), D("6.66"))], total=D("39.99")),
        )
        (only,) = invoice.lines.all()
        self.assertEqual((only.printed_ttc, only.total_ttc), (D("39.99"), D("39.99")))
        self.assertEqual(invoice.total_ttc, D("39.99"))

    def test_the_products_page_shows_that_amount(self):
        import_parsed_invoice(
            self.supplier,
            parsed(breakdown=[(D("0.20"), D("33.33"), D("6.66"))], total=D("39.99"), when=date(2026, 5, 19)),
        )
        page = self.client.get(reverse("inventory:stock_list"))
        (row,) = page.context["charge_suppliers"]
        self.assertEqual(row["total_ttc"], D("39.99"))

    def test_nothing_of_it_reaches_the_products(self):
        import_parsed_invoice(self.supplier, parsed(lines=[line("Abonnement mobile", "8.33")], total=D("9.99")))
        product = Product.objects.get(supplier=self.supplier)
        self.assertTrue(product.is_expense)
        self.assertFalse(product.needs_review)
        self.assertEqual(review_panel_context()["review_total"], 0)
        self.assertFalse(StockMovement.objects.exists())

    def test_every_document_of_the_supplier_shares_the_one_product(self):
        for number in ("F-1", "F-2"):
            import_parsed_invoice(self.supplier, parsed(lines=[line("Abonnement", "8.33")], number=number, total=D("9.99")))
        self.assertEqual(Product.objects.filter(supplier=self.supplier).count(), 1)

    def test_a_vat_table_that_does_not_account_for_the_total_is_ignored(self):
        """A rent statement lists the previous balance, the direct debit and
        the tax beside the rent: what was paid is its own total, and a rate
        that does not add up to it is a rate nobody should book."""
        invoice = import_parsed_invoice(
            self.supplier,
            parsed(
                lines=[line("TVA TAUX NORMAL", "120.27", rate="0.055")],
                breakdown=[(D("0.055"), D("120.27"), D("6.61"))],
                total=D("861.27"),
            ),
        )
        (only,) = invoice.lines.all()
        self.assertEqual((only.total_ht, only.vat_rate), (D("861.27"), D("0")))

    def test_with_no_vat_table_the_total_is_the_line(self):
        invoice = import_parsed_invoice(self.supplier, parsed(lines=[], total=D("861.27")))
        (only,) = invoice.lines.all()
        self.assertEqual((only.raw_name, only.total_ht, only.vat_rate), ("Free Exemple", D("861.27"), D("0")))
        self.assertEqual(invoice.status, Invoice.Status.COMPLETE)

    def test_a_charge_whose_total_was_not_read_waits_for_a_person(self):
        """The figure filed is then whatever was read instead, which is not
        what was paid: it must not pass for settled."""
        invoice = import_parsed_invoice(
            self.supplier, parsed(lines=[line("Solde antérieur", "26.53", rate="0")], total=None)
        )
        (only,) = invoice.lines.all()
        self.assertEqual((only.total_ht, invoice.status), (D("26.53"), Invoice.Status.NEEDS_REVIEW))
        self.assertIn("vérifiez-le sur le document", invoice.error_message)
        # And its own check says so, under the row a person opens.
        self.assertEqual(
            [(check["label"], check["passed"]) for check in invoice.failed_checks],
            [("Total de la charge", False)],
        )

    def test_with_nothing_readable_it_waits_for_a_person(self):
        invoice = import_parsed_invoice(self.supplier, parsed(lines=[], total=None))
        self.assertEqual((invoice.lines.count(), invoice.status), (0, Invoice.Status.NEEDS_REVIEW))

    def test_an_ordinary_supplier_keeps_its_products(self):
        shop = make_supplier(code="EPICERIE", name="Épicerie du coin", parser_key="")
        invoice = import_parsed_invoice(shop, parsed(lines=[line("TOMATES", "2.00")], total=D("2.11")))
        self.assertEqual([line.raw_name for line in invoice.lines.all()], ["TOMATES"])
        self.assertEqual(review_panel_context()["review_total"], 1)


class MarkingTheSupplierTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="FREE_X", name="Free Exemple", parser_key="")
        self.invoice = import_parsed_invoice(
            self.supplier,
            parsed(
                lines=[line("Abonnement mobile", "8.33"), line("Option", "2.00", rate="0.055")],
                total=D("12.11"),
            ),
        )
        self.url = reverse("invoices:supplier_expenses", args=[self.supplier.pk])

    def test_the_sources_tab_offers_the_box(self):
        page = self.client.get(reverse("invoices:invoice_type_list"))
        self.assertContains(page, f'action="{self.url}"')
        self.assertContains(page, "Charges")

    def test_ticking_it_redoes_what_is_already_filed(self):
        response = self.client.post(self.url, {"expenses_only": "1"})
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))
        self.supplier.refresh_from_db()
        self.assertTrue(self.supplier.expenses_only)
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        # The rates its lines carried are kept: one line each, not one lump.
        self.assertEqual(
            [(line.raw_name, line.total_ht, line.vat_rate) for line in invoice.lines.order_by("vat_rate")],
            [("Free Exemple", D("2.00"), D("0.055")), ("Free Exemple", D("8.33"), D("0.20"))],
        )
        # The products its lines used to name are gone with them.
        self.assertEqual([product.raw_name for product in Product.objects.all()], ["Free Exemple"])
        self.assertTrue(any("1 document" in message for message in messages_of(response)))

    def test_unticking_it_leaves_the_documents_alone(self):
        self.client.post(self.url, {"expenses_only": "1"})
        response = self.client.post(self.url, {})
        self.supplier.refresh_from_db()
        self.assertFalse(self.supplier.expenses_only)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).lines.count(), 2)
        self.assertTrue(any("documents déjà enregistrés" in message for message in messages_of(response)))

    def test_redoing_is_safe_to_run_twice(self):
        self.supplier.expenses_only = True
        self.supplier.save()
        self.assertEqual(redo_as_expenses(self.supplier), 1)
        self.assertEqual(redo_as_expenses(self.supplier), 0)


class ReadAgainTests(TestCase):
    """Reading a charge document again files it as a charge - the same way
    importing it does."""

    def setUp(self):
        # Filed as stock first, the way it was before the supplier was known
        # to be one of charges.
        self.supplier = make_supplier(code="BAILLEUR", name="Bailleur Exemple", parser_key="")
        self.invoice = import_parsed_invoice(
            self.supplier, parsed(lines=[line("ECHEANCE", "830.00")], total=D("830.00"), text=STATEMENT)
        )
        # What the document said, kept as a receipt keeps its reading.
        Invoice.objects.filter(pk=self.invoice.pk).update(
            ocr_text=STATEMENT, parse_checks=[{"label": "Somme des lignes", "passed": False, "detail": "x"}]
        )
        Supplier.objects.filter(pk=self.supplier.pk).update(expenses_only=True)
        self.supplier.refresh_from_db()
        self.invoice.refresh_from_db()

    def test_reading_it_again_keeps_its_postes(self):
        """Read as a ticket, a rent statement's lines are the previous
        balance and the direct debit beside the rent: 'Relire' put those
        back in place of the charge."""
        self.assertTrue(reread_receipt(self.invoice))
        self.invoice.refresh_from_db()
        self.assertEqual(
            [line.raw_name for line in self.invoice.lines.all()],
            ["LOYERLOCAUXACTIVITEHT", "PROV.CHARGESIMMEUBLE", "PROVISIONEAUFROIDE"],
        )
        self.assertEqual((self.invoice.printed_total_ttc, self.invoice.status), (D("820.00"), Invoice.Status.COMPLETE))

    def test_reading_it_again_changes_nothing_when_nothing_changed(self):
        reread_receipt(self.invoice)
        self.invoice.refresh_from_db()
        self.assertFalse(reread_receipt(self.invoice))

    def test_its_checks_are_about_the_charge(self):
        """Kept from the ticket reading it is no longer, they said "lignes
        946,11 € / ticket 304,74 €" over two lines adding up to exactly what
        the document charges - a warning about nothing under its row."""
        reread_receipt(self.invoice)
        self.invoice.refresh_from_db()
        self.assertEqual(
            [(check["label"], check["passed"]) for check in self.invoice.parse_checks],
            [("Total de la charge", True)],
        )
        self.assertEqual(self.invoice.failed_checks, [])

    def test_a_reading_that_finds_nothing_changes_nothing(self):
        """The same rule as any re-read: with nothing readable, the document
        stays exactly as it was filed."""
        Invoice.objects.filter(pk=self.invoice.pk).update(ocr_text="AVIS D'ECHEANCE\nRien de lisible")
        self.invoice.refresh_from_db()
        before = [(line.raw_name, line.total_ht) for line in self.invoice.lines.all()]
        self.assertFalse(reread_receipt(self.invoice))
        self.invoice.refresh_from_db()
        self.assertEqual([(line.raw_name, line.total_ht) for line in self.invoice.lines.all()], before)

    def test_a_charge_is_not_in_the_queue_of_tickets_to_check(self):
        """There is nothing to type on a rent, and forty-two of them behind
        the tickets is a queue nobody works through."""
        self.assertNotIn(self.invoice, pending_receipts())
        Supplier.objects.filter(pk=self.supplier.pk).update(expenses_only=False)
        self.assertIn(self.invoice, pending_receipts())


class ChargesOnTheProductsPageTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="FREE_X", name="Free Exemple", parser_key="", expenses_only=True)
        for month, amount in ((3, "9.99"), (4, "10.27")):
            import_parsed_invoice(
                self.supplier,
                parsed(lines=[], total=D(amount), number=f"F-{month}", when=date(2026, month, 19)),
            )

    def test_the_page_shows_what_the_charges_cost(self):
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(page, "Charges et abonnements")
        self.assertContains(page, "Free Exemple")
        charges = page.context["charge_suppliers"]
        self.assertEqual(
            [(row["supplier"].name, row["documents"], row["total_ttc"], row["last"]) for row in charges],
            [("Free Exemple", 2, D("20.26"), date(2026, 4, 19))],
        )

    def test_the_postes_are_shown_under_their_supplier(self):
        bailleur = make_supplier(code="BAILLEUR", name="Bailleur Exemple", parser_key="", expenses_only=True)
        import_parsed_invoice(bailleur, parsed(total=D("830.00"), text=STATEMENT, when=date(2026, 5, 1)))
        page = self.client.get(reverse("inventory:stock_list"))
        (row,) = [row for row in page.context["charge_suppliers"] if row["supplier"] == bailleur]
        self.assertEqual(
            [(poste["name"], poste["total_ttc"]) for poste in row["postes"]],
            [("LOYERLOCAUXACTIVITEHT", D("720.00")), ("PROV.CHARGESIMMEUBLE", D("90.00")),
             ("PROVISIONEAUFROIDE", D("10.00"))],
        )
        self.assertContains(page, "PROV.CHARGESIMMEUBLE")

    def test_the_navigation_badge_counts_the_same_queue(self):
        """Written twice, one of them counting the postes of charge, the
        badge said 110 over a page of 97."""
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(page.context["review_count_nav"], page.context["review_total"])
        self.assertEqual(page.context["review_count_nav"], 0)

    def test_a_poste_renamed_by_hand_is_still_a_charge(self):
        """Corrected on the document's own page, a poste keeps a name of its
        own - and a new product to classify is exactly what a charge must
        never make."""
        invoice = Invoice.objects.filter(supplier=self.supplier).first()
        replace_invoice_lines(
            invoice,
            [ParsedLine(
                raw_name="Loyer", quantity=1, total_volume=D("0"), unit_cost_ht=D("10.00"),
                total_ht=D("10.00"), vat_rate=D("0"),
            )],
        )
        renamed = Product.objects.get(supplier=self.supplier, raw_name="Loyer")
        self.assertTrue(renamed.is_expense)
        self.assertFalse(renamed.needs_review)
        self.assertEqual(review_panel_context()["review_total"], 0)

    def test_a_supplier_filed_in_one_line_shows_no_poste(self):
        """One poste is the charge itself under another name."""
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual([row["postes"] for row in page.context["charge_suppliers"]], [[]])

    def test_nothing_is_shown_when_no_supplier_is_a_charge(self):
        Supplier.objects.filter(pk=self.supplier.pk).update(expenses_only=False)
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(page.context["charge_suppliers"], [])
        self.assertNotContains(page, "Charges et abonnements")
