"""Suppliers whose documents are charges, not goods: a phone subscription,
the rent, the water.

Their invoices have nothing to classify - there is no product behind a rent -
so they are filed as one line per VAT rate, named after the supplier, on a
product that never reaches the stock pages. What they cost is shown on
"Produits" all the same, in a fold of its own: charges are not stock, but
they are spending.

Data invented.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse

from inventory.models import Product, StockMovement
from inventory.views import review_panel_context
from invoices.importing import import_parsed_invoice, redo_as_expenses, replace_invoice_lines
from invoices.receipts import pending_receipts, reread_receipt
from invoices.models import Invoice, Supplier
from invoices.parsers.base import ParseCheck, ParsedInvoice, ParsedLine
from invoices.tests.test_unknown_shops import staged_file
from tests.factories import make_product, make_supplier

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


class FetchedChargeTests(TestCase):
    """A charge fetched by a portal or the mailbox goes through the ticket
    reader, then is filed by the charge reading - its VAT table here. The
    ticket reader's checks, about lines the charge reading replaced, were
    stored over the charge's own: Eau de Paris' water bills, filed at
    exactly what they charge (HT 303,28 €, TVA 23,92 € at 5,5 % and 10 %,
    TTC 327,20 €), waited in « À vérifier » saying « 5.5 % supposé » and
    « lignes 310,15 € HT / ticket 303,28 € »."""

    def test_it_keeps_its_own_checks_and_is_settled(self):
        from invoices.receipts import ReceiptRead, import_receipt

        water = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)
        reading = parsed(
            lines=[line("PRODUCTION ET DISTRIBUTION", "150.00", "0.055"), line("COLLECTE ET TRAITEMENT", "160.15", "0.055")],
            breakdown=[(D("0.055"), D("142.34"), D("7.82")), (D("0.10"), D("160.94"), D("16.10"))],
            total=D("327.20"), number="2026100000001", when=date(2026, 4, 10), text="EAU EXEMPLE\nTotal 327,20",
        )
        reading.checks = [
            ParseCheck(label="Taux par article", passed=False, detail="5.5 % supposé, à vérifier."),
            ParseCheck(label="Somme HT des lignes = base HT du ticket", passed=False, detail="lignes 310.15 € HT / ticket 303.28 € HT"),
        ]
        read = ReceiptRead(parser=None, parsed=reading, preview=None, text=reading.source_text)
        with mock.patch("invoices.receipts.read_receipt", return_value=read):
            invoice = import_receipt(staged_file(self, "eau.pdf"), supplier=water, chosen_because="Téléchargée par « Eau ».")
        invoice.refresh_from_db()
        labels = {check["label"] for check in invoice.parse_checks}
        self.assertNotIn("Taux par article", labels)
        self.assertNotIn("Somme HT des lignes = base HT du ticket", labels)
        self.assertIn("Total de la charge", labels)
        self.assertEqual(invoice.status, Invoice.Status.COMPLETE)
        self.assertEqual(invoice.review_state["label"], "Charge")
        self.assertEqual((invoice.total_ht, invoice.total_ttc), (D("303.28"), D("327.20")))


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

    def test_the_suppliers_page_offers_it_behind_a_confirmation(self):
        """A box on the Sources tab rewrote documents on a click."""
        self.assertContains(self.client.get(reverse("invoices:supplier_detail", args=[self.supplier.pk])), self.url)
        page = self.client.get(self.url)
        self.assertContains(page, "Oui, ce sont des charges")
        # An old page posting the box: shown what it would do, nothing done.
        self.client.post(self.url, {"expenses_only": "1"})
        self.supplier.refresh_from_db()
        self.assertFalse(self.supplier.expenses_only)

    def test_ticking_it_redoes_what_is_already_filed(self):
        response = self.client.post(self.url, {"expenses_only": "1", "confirme": "1"})
        self.assertRedirects(response, reverse("invoices:supplier_detail", args=[self.supplier.pk]))
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
        self.client.post(self.url, {"expenses_only": "1", "confirme": "1"})
        response = self.client.post(self.url, {"confirme": "1"})
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
        """One poste is the charge itself under another name - and the
        supplier's own row is what opens on it."""
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual([row["postes"] for row in page.context["charge_suppliers"]], [[]])

    def test_every_supplier_opens_on_its_documents_and_its_curve(self):
        """The water bill, the phone, the alarm: one poste each, so the
        postes under them are nothing to click - and five suppliers out of
        six had no way at all to reach their own documents."""
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(page, reverse("inventory:charge_supplier_documents", args=[self.supplier.pk]))
        self.assertContains(page, reverse("inventory:charge_supplier_history", args=[self.supplier.pk]))

        documents = self.client.get(reverse("inventory:charge_supplier_documents", args=[self.supplier.pk]))
        self.assertEqual(
            [(row["invoice"].invoice_number, row["total_ttc"]) for row in documents.context["rows"]],
            [("F-4", D("10.27")), ("F-3", D("9.99"))],
        )
        self.assertEqual(documents.context["total_ttc"], D("20.26"))
        for row in documents.context["rows"]:
            self.assertContains(documents, reverse("invoices:invoice_detail", args=[row["invoice"].pk]))
            self.assertContains(documents, reverse("invoices:invoice_edit_lines", args=[row["invoice"].pk]))

        curve = self.client.get(reverse("inventory:charge_supplier_history", args=[self.supplier.pk]))
        self.assertTrue(curve.context["has_enough_data"])
        self.assertContains(curve, "<svg")

    def test_a_bill_printing_two_rates_is_one_document(self):
        """Read as two lines, one per rate (charge_reading), it is still one
        bill: a person opening a charge is counting documents, and 29 of
        them showed up as 46."""
        import_parsed_invoice(
            self.supplier,
            parsed(
                breakdown=[(D("0.055"), D("2.00"), D("0.11")), (D("0.20"), D("8.33"), D("1.67"))],
                total=D("12.11"),
                number="F-5",
                when=date(2026, 5, 19),
            ),
        )
        documents = self.client.get(reverse("inventory:charge_supplier_documents", args=[self.supplier.pk]))
        (newest, *_) = documents.context["rows"]
        self.assertEqual((newest["invoice"].invoice_number, newest["total_ttc"]), ("F-5", D("12.11")))
        # Its two lines add up on one row - and the tax is an amount, since
        # naming one of the two rates would be a lie about the other.
        self.assertEqual((newest["total_ht"], newest["total_vat"]), (D("10.33"), D("1.78")))
        self.assertEqual(len(documents.context["rows"]), 3)

    def test_a_supplier_quiet_over_the_window_keeps_its_row(self):
        """A water bill arrives twice a year. Listed only when the window
        holds one, it would drop off the page between two of them, taking
        its whole history with it."""
        eau = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)
        long_ago = timezone.localdate() - timedelta(days=600)
        import_parsed_invoice(eau, parsed(total=D("260.63"), number="E-1", when=long_ago))
        page = self.client.get(reverse("inventory:stock_list"))
        (row,) = [row for row in page.context["charge_suppliers"] if row["supplier"] == eau]
        self.assertEqual((row["documents"], row["total_ttc"], row["last"]), (0, D("0"), long_ago))
        documents = self.client.get(reverse("inventory:charge_supplier_documents", args=[eau.pk]))
        self.assertEqual(len(documents.context["rows"]), 1)

    def test_only_a_supplier_of_charges_opens_that_way(self):
        ordinary = make_supplier(code="EPICERIE_X", name="Epicerie Exemple")
        self.assertEqual(
            self.client.get(reverse("inventory:charge_supplier_documents", args=[ordinary.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("inventory:charge_supplier_history", args=[ordinary.pk])).status_code, 404
        )

    def test_a_poste_opens_on_its_documents_and_its_curve(self):
        """Like any stock item: what it cost month after month, each line
        linking to the document it came from."""
        bailleur = make_supplier(code="BAILLEUR", name="Bailleur Exemple", parser_key="", expenses_only=True)
        for month in (5, 6):
            import_parsed_invoice(
                bailleur,
                parsed(total=D("830.00"), text=STATEMENT, number=f"A-{month}", when=date(2026, month, 1)),
            )
        page = self.client.get(reverse("inventory:stock_list"))
        (row,) = [row for row in page.context["charge_suppliers"] if row["supplier"] == bailleur]
        rent = next(poste for poste in row["postes"] if poste["name"] == "LOYERLOCAUXACTIVITEHT")
        self.assertContains(page, reverse("inventory:charge_documents", args=[rent["product"].pk]))
        self.assertContains(page, reverse("inventory:charge_history", args=[rent["product"].pk]))

        documents = self.client.get(reverse("inventory:charge_documents", args=[rent["product"].pk]))
        self.assertEqual(len(documents.context["rows"]), 2)
        self.assertEqual(documents.context["total_ttc"], D("1440.00"))
        for row in documents.context["rows"]:
            self.assertContains(documents, reverse("invoices:invoice_detail", args=[row["invoice"].pk]))

        curve = self.client.get(reverse("inventory:charge_history", args=[rent["product"].pk]))
        self.assertTrue(curve.context["has_enough_data"])
        self.assertContains(curve, "<svg")

    def test_one_document_is_not_a_curve(self):
        once = make_supplier(code="ASSURANCE", name="Assurance Exemple", parser_key="", expenses_only=True)
        import_parsed_invoice(once, parsed(total=D("120.00"), number="A-1", when=date(2026, 5, 2)))
        curve = self.client.get(
            reverse("inventory:charge_history", args=[Product.objects.get(supplier=once).pk])
        )
        self.assertFalse(curve.context["has_enough_data"])
        self.assertContains(curve, "Pas assez d'historique")

    def test_only_a_poste_of_charge_opens_that_way(self):
        """A stock product has its own history page; this one answers for
        charges alone, so a wrong id is a 404 rather than a blank panel."""
        ordinary = make_product(supplier=make_supplier(code="EPICERIE"), raw_name="TOMATES")
        self.assertEqual(
            self.client.get(reverse("inventory:charge_documents", args=[ordinary.pk])).status_code, 404
        )

    def test_a_poste_cannot_be_classified_as_stock(self):
        """A rent is not stock. The panel never offers one - it lists what
        needs review, and a charge never does - but the address took it, and
        classified, the rent became bottles with a stock movement behind."""
        from inventory.models import StockType

        poste = Product.objects.get(supplier=self.supplier)
        article = StockType.objects.create(name="Absinthe")
        response = self.client.post(
            reverse("inventory:assign_product", args=[poste.pk]),
            {"stock_type_name": article.name, "stock_equivalent": "1"},
            follow=True,
        )
        poste.refresh_from_db()
        self.assertIsNone(poste.stock_type)
        self.assertFalse(StockMovement.objects.exists())
        self.assertIn("poste de charge", [str(message) for message in response.context["messages"]][0])

    def test_unticking_gives_its_products_back(self):
        """A box ticked by mistake has to be reversible. Unticked, the
        supplier's postes stayed flagged as charges for ever: out of the
        review queue, out of every stock page, out of every stock movement,
        and no screen could put them back - correcting the document by hand
        resolved the very same flagged product."""
        poste = Product.objects.get(supplier=self.supplier)
        self.assertTrue(poste.is_expense)
        response = self.client.post(
            reverse("invoices:supplier_expenses", args=[self.supplier.pk]), {"confirme": "1"}, follow=True
        )
        poste.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertFalse(self.supplier.expenses_only)
        self.assertFalse(poste.is_expense)
        self.assertTrue(poste.needs_review)
        self.assertEqual(review_panel_context()["review_total"], 1)
        self.assertIn("repassent à classer", " ".join(messages_of(response)))

    def test_ticking_it_again_takes_them_back_out(self):
        self.client.post(reverse("invoices:supplier_expenses", args=[self.supplier.pk]), {"confirme": "1"})
        self.client.post(
            reverse("invoices:supplier_expenses", args=[self.supplier.pk]), {"expenses_only": "1", "confirme": "1"}
        )
        self.assertTrue(all(product.is_expense for product in Product.objects.filter(supplier=self.supplier)))
        self.assertEqual(review_panel_context()["review_total"], 0)

    def test_correcting_a_document_frees_a_product_of_a_supplier_that_sells_goods(self):
        """The flag follows the supplier, so a document corrected by hand -
        which is what the message after unticking asks for - is enough on
        its own."""
        poste = Product.objects.get(supplier=self.supplier)
        Supplier.objects.filter(pk=self.supplier.pk).update(expenses_only=False)
        invoice = Invoice.objects.filter(supplier=self.supplier).first()
        # The line keeps the name it was filed under, so it resolves back to
        # the very product that was flagged - which is what made the advice
        # to "correct them document by document" lead nowhere.
        replace_invoice_lines(
            invoice,
            [ParsedLine(
                raw_name=poste.raw_name, quantity=1, total_volume=D("0"), unit_cost_ht=D("10.00"),
                total_ht=D("10.00"), vat_rate=D("0.20"),
            )],
        )
        poste.refresh_from_db()
        self.assertFalse(poste.is_expense)
        self.assertTrue(poste.needs_review)
        self.assertEqual(review_panel_context()["review_total"], 1)

    def test_nothing_is_shown_when_no_supplier_is_a_charge(self):
        Supplier.objects.filter(pk=self.supplier.pk).update(expenses_only=False)
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(page.context["charge_suppliers"], [])
        self.assertNotContains(page, "Charges et abonnements")
