"""A credit on a charge: a line with a negative amount, typed on the
correction page.

An electricity bill takes the month's subscription back at the rate it was
billed at (5,5 %) and bills it again at the new one (20 %), so its VAT table
prints a 5,5 % row of "-2,76 €" HT, "-0,15 €" of tax - "-2,91 €" TTC - beside
the 20 % one. The ticket reader lost the minus, the bill was filed at
+2,76 € on that rate and its total could not be read, and correcting it was
impossible: the line form refused a count of 1 at a negative amount ("Un
retour a une quantité et un montant négatifs"), a guard that exists because
stock worth less than nothing breaks the FIFO valuation. A charge has no
stock (Supplier.expenses_only), so on a charge the credit is accepted as the
owner types it - one line, at -2,76 - and every figure downstream adds it up
with its sign. On goods the guard stands, and so it does on a charge whose
supplier still has a stock item among its products, and over a supplier's
documents once it leaves charges - or a document once it is moved out of them.

Structurally faithful (two rates, the credit at 5,5 %, the charge filed by
the charge reading as one line per rate), data invented.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bank.reconcile import rounded_total
from inventory.models import StockMovement
from inventory.services import (
    create_stock_movement_for_line,
    link_product_to_stock_type,
)
from inventory.views import charge_suppliers
from invoices.forms import DOCUMENT_RECEIPT, LineCorrectionForm
from invoices.importing import import_parsed_invoice, stop_expenses
from invoices.models import Invoice
from invoices.parsers.base import ParsedInvoice
from invoices.tests.page_posts import page_post
from invoices.tests.test_expense_suppliers import STATEMENT
from invoices.workspace import DOCUMENT_TO_FIX
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_type,
    make_supplier,
)

D = Decimal

# The rent statement of test_expense_suppliers with a deposit given back
# among its postes, as test_charges_postes reads it: 817,00 charged.
REFUNDED_STATEMENT = (
    STATEMENT.replace(
        "PRELV.SEPAau10/11/2025  -  830,00  PROVISIONEAUFROIDE  10,00",
        "PRELV.SEPAau10/11/2025  -  830,00  PROVISIONEAUFROIDE  10,00\nREMBOURSEMENTDEPOTGARANTIE  -  3,00",
    )
    .replace("(B)  820,00", "(B)  817,00")
    .replace("Prélevéle10/12/2025  820,00", "Prélevéle10/12/2025  817,00")
)

# The bill as printed: the credit at 5,5 %, the month at 20 %.
CREDIT_HT, CREDIT_TAX, CREDIT_TTC = D("-3.20"), D("-0.18"), D("-3.38")
MONTH_HT, MONTH_TAX, MONTH_TTC = D("185.40"), D("37.08"), D("222.48")
BILL_TTC = MONTH_TTC + CREDIT_TTC  # 219,10 €
REFUSED_ON_GOODS = "Un retour a une quantité et un montant négatifs, un achat les deux positifs."


class ChargeCreditLineTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="ELEC_X", name="Électricité Exemple", parser_key="", expenses_only=True)
        # As the reader filed it: the minus lost, the 5,5 % row read as a
        # tax-inclusive 3,20 (3,02 HT + 0,18), the total unread.
        self.invoice = import_parsed_invoice(
            self.supplier,
            ParsedInvoice(
                supplier_code="ELEC_X",
                invoice_number="FA-000123",
                invoice_date=timezone.localdate() - timedelta(days=20),
                vat_breakdown=[(D("0.055"), D("3.02"), D("0.18")), (D("0.2"), MONTH_HT, MONTH_TAX)],
            ),
        )
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])

    def post(self, **changes):
        return self.client.post(self.url, page_post(self.client.get(self.url), **changes))

    def credit(self):
        return self.invoice.lines.get(vat_rate=D("0.055"))

    def assert_saved(self, response):
        self.assertEqual(
            response.status_code, 302, response.context and response.context["formset"].errors
        )

    def test_filed_as_the_reader_left_it(self):
        """What the owner opens: the credit as a charge, the total unread."""
        self.assertEqual((self.credit().total_ht, self.credit().printed_ttc), (D("3.02"), D("3.20")))
        self.assertEqual(self.invoice.status, Invoice.Status.NEEDS_REVIEW)
        self.assertTrue(self.invoice.is_receipt)

    def test_a_credit_typed_in_ttc_is_saved_at_a_count_of_one(self):
        self.assert_saved(self.post(**{"form-0-total_ttc": str(CREDIT_TTC), "form-0-amount_source": "ttc"}))
        credit = self.credit()
        self.assertEqual(
            (credit.quantity, credit.total_ht, credit.printed_ttc, credit.unit_cost_ht),
            (1, CREDIT_HT, CREDIT_TTC, CREDIT_HT),
        )

    def test_a_credit_typed_in_ht_works_out_its_ttc(self):
        self.assert_saved(self.post(**{"form-0-total_ht": str(CREDIT_HT), "form-0-amount_source": "ht"}))
        self.assertEqual((self.credit().total_ht, self.credit().printed_ttc), (CREDIT_HT, CREDIT_TTC))

    def test_the_bill_is_worth_what_it_charges_everywhere(self):
        """The document's total, the bank match, the charges fold on Produits
        and the document's own row there: all 219,10 €, the credit off."""
        self.assert_saved(
            self.post(
                **{
                    "form-0-total_ttc": str(CREDIT_TTC),
                    "form-0-amount_source": "ttc",
                    "printed_total_ttc": str(BILL_TTC),
                }
            )
        )
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(invoice.total_ttc, BILL_TTC)
        self.assertEqual(rounded_total(invoice), BILL_TTC)
        [row] = [row for row in charge_suppliers() if row["supplier"] == self.supplier]
        self.assertEqual((row["documents"], row["total_ttc"]), (1, BILL_TTC))
        response = self.client.get(reverse("inventory:charge_supplier_documents", args=[self.supplier.pk]))
        [document] = response.context["rows"]
        self.assertEqual(
            (document["total_ht"], document["total_vat"], document["total_ttc"]),
            (MONTH_HT + CREDIT_HT, MONTH_TAX + CREDIT_TAX, BILL_TTC),
        )

    def test_its_total_typed_settles_the_charge(self):
        """The total the reader could not read, typed beside the lines: the
        charge's own check follows it. Kept from the import, "Total de la
        charge" went on saying the total was never read - over a total just
        typed and lines adding up to it."""
        self.assert_saved(
            self.post(
                **{
                    "form-0-total_ttc": str(CREDIT_TTC),
                    "form-0-amount_source": "ttc",
                    "printed_total_ttc": str(BILL_TTC),
                }
            )
        )
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        checks = {check["label"]: check["passed"] for check in invoice.parse_checks}
        self.assertEqual(
            (checks["Total de la charge"], checks["Somme des lignes = total imprimé"]), (True, True)
        )
        self.assertEqual((invoice.status, invoice.error_message), (Invoice.Status.COMPLETE, ""))
        self.assertFalse(Invoice.objects.filter(DOCUMENT_TO_FIX, pk=invoice.pk).exists())

    def test_without_its_total_it_still_waits(self):
        """Lines corrected, total still unread: a charge is its total, so it
        does not pass for settled (importing.charge_state) - saved, it came
        out "Charge" and left « À corriger »."""
        self.assert_saved(self.post(**{"form-0-total_ttc": str(CREDIT_TTC), "form-0-amount_source": "ttc"}))
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertIsNone(invoice.printed_total_ttc)
        self.assertEqual(invoice.status, Invoice.Status.NEEDS_REVIEW)
        self.assertTrue(Invoice.objects.filter(DOCUMENT_TO_FIX, pk=invoice.pk).exists())

    def test_a_credit_the_charge_reading_filed_saves_untouched(self):
        """The charge reading files a credit the same way: a rent statement's
        deposit given back is a poste of -3,00 at a count of 1. Every row is
        checked on a save, the ones nobody changed included, so two real rent
        statements holding such a poste could not be validated at all."""
        rent = make_supplier(code="LOYER_X", name="Bailleur Exemple", parser_key="", expenses_only=True)
        invoice = import_parsed_invoice(
            rent,
            ParsedInvoice(
                supplier_code="LOYER_X",
                invoice_number="AV-2025-12",
                invoice_date=timezone.localdate() - timedelta(days=10),
                printed_total_ttc=D("830.00"),
                source_text=REFUNDED_STATEMENT,
            ),
        )
        self.assertEqual(
            invoice.lines.filter(total_ht__lt=0).values_list("raw_name", "quantity", "total_ht").get(),
            ("REMBOURSEMENTDEPOTGARANTIE", 1, D("-3.00")),
        )
        url = reverse("invoices:receipt_review", args=[invoice.pk])
        self.assert_saved(self.client.post(url, page_post(self.client.get(url))))
        invoice = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual((invoice.total_ttc, invoice.status), (D("817.00"), Invoice.Status.COMPLETE))
        self.assertEqual(invoice.lines.get(raw_name="REMBOURSEMENTDEPOTGARANTIE").total_ht, D("-3.00"))

    def test_a_negative_count_at_a_positive_amount_is_still_refused(self):
        """The count says credited, the money says charged: whichever was
        meant, the total would be wrong one way."""
        response = self.post(**{"form-0-quantity": "-1", "form-0-total_ttc": "3.38", "form-0-amount_source": "ttc"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "un avoir se saisit avec un montant négatif")
        self.assertEqual(self.credit().total_ht, D("3.02"))


class NegativeAmountRoundingTests(TestCase):
    """A negative amount converts like a positive one, half away from zero,
    both ways: -1,00 HT at 5,5 % is -1,055, so -1,06 TTC as 1,00 is 1,06."""

    def amounts(self, **row):
        data = {"product_name": "Électricité Exemple", "quantity": "1", "vat_rate": "5.5", **row}
        form = LineCorrectionForm(data, document=DOCUMENT_RECEIPT, charge=True)
        self.assertTrue(form.is_valid(), form.errors)
        return form.amounts()

    def test_ht_to_ttc(self):
        for ht, ttc in (("1.00", D("1.06")), ("-1.00", D("-1.06")), ("-2.76", D("-2.91"))):
            with self.subTest(ht=ht):
                amounts = self.amounts(total_ht=ht, amount_source="ht")
                self.assertEqual((amounts["total_ht"], amounts["printed_ttc"]), (D(ht), ttc))

    def test_ttc_to_ht(self):
        for ttc, ht in (("-1.06", D("-1.00")), ("-2.91", D("-2.76")), ("-3.38", D("-3.20"))):
            with self.subTest(ttc=ttc):
                amounts = self.amounts(total_ttc=ttc, amount_source="ttc")
                self.assertEqual((amounts["total_ht"], amounts["printed_ttc"]), (ht, D(ttc)))


class GoodsCreditLineTests(TestCase):
    """The guard stands where there is stock: a shop's ticket still refuses
    a positive count at a negative amount."""

    def test_a_goods_ticket_still_refuses_it(self):
        shop = make_supplier(code="EPICERIE_X", name="Épicerie Exemple", parser_key="")
        invoice = Invoice.objects.create(
            supplier=shop,
            invoice_number="T-0042",
            invoice_date=timezone.localdate() - timedelta(days=3),
            parse_checks=[{"label": "x", "passed": True, "detail": ""}],
        )
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=shop, raw_name="CITRON VERT"), raw_name="CITRON VERT",
            quantity=1, total_ht="3.20", vat_rate=D("0.055"), printed_ttc=D("3.38"),
        )
        url = reverse("invoices:receipt_review", args=[invoice.pk])
        response = self.client.post(
            url, page_post(self.client.get(url), **{"form-0-total_ttc": "-3.38", "form-0-amount_source": "ttc"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, REFUSED_ON_GOODS)
        self.assertEqual(invoice.lines.get().total_ht, D("3.20"))


def negative_costs():
    """Stock worth less than nothing: what the guard is there to stop."""
    return list(StockMovement.objects.filter(unit_cost_ht__lt=0).values_list("quantity", "unit_cost_ht"))


class ChargeWithAStockItemTests(TestCase):
    """A charge supplier can still have stock among its products: ticked
    « charges », a product a stock item claimed stays as it is
    (importing.redo_as_expenses - it is stock after all), and a line on it
    books a stock movement like any line of goods. Relaxed on every row of a
    charge, the guard let a credit typed on that line through: stock at
    -30,00 € the unit."""

    def setUp(self):
        self.supplier = make_supplier(code="GAZ_X", name="Gaz Exemple", parser_key="", expenses_only=True)
        self.bottle = make_product(
            supplier=self.supplier, raw_name="BOUTEILLE GAZ 13KG", stock_type=make_stock_type(name="Gaz en bouteille")
        )
        self.invoice = make_invoice(supplier=self.supplier)
        make_invoice_line(self.invoice, product=self.bottle, quantity=1, total_ht="30.00", vat_rate=D("0.2"))
        self.url = reverse("invoices:invoice_edit_lines", args=[self.invoice.pk])

    def post(self, **changes):
        return self.client.post(self.url, page_post(self.client.get(self.url), **changes))

    def test_a_credit_on_the_stock_item_is_refused(self):
        response = self.post(**{"form-0-total_ht": "-30.00", "form-0-amount_source": "ht"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, REFUSED_ON_GOODS)
        self.assertEqual(self.invoice.lines.get().total_ht, D("30.00"))
        self.assertEqual(negative_costs(), [])

    def test_so_is_a_row_typed_under_its_name(self):
        """Which product a row lands on is only known once it is saved - by
        its name (replace_invoice_lines) - so the guard is the supplier's, not
        the stored line's: a row added under the stock item's name, or a row
        renamed to it, is that stock item."""
        response = self.post(
            **{
                "form-TOTAL_FORMS": "2",
                "form-1-product_name": "BOUTEILLE GAZ 13KG",
                "form-1-quantity": "1",
                "form-1-total_ht": "-30.00",
                "form-1-amount_source": "ht",
                "form-1-vat_rate": "20",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, REFUSED_ON_GOODS)
        self.assertEqual(self.invoice.lines.count(), 1)
        self.assertEqual(negative_costs(), [])

    def test_a_credit_there_is_typed_as_a_return(self):
        """As on goods: a count of -1 at the negative amount, booked back at
        what the bottle cost."""
        response = self.post(**{"form-0-quantity": "-1", "form-0-total_ht": "-30.00", "form-0-amount_source": "ht"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(StockMovement.objects.values_list("quantity", "unit_cost_ht")), [(D("-1"), D("30"))])


class ChargeSwitchedBackToGoodsTests(TestCase):
    """A supplier leaving charges (« changer… » on its page,
    importing.stop_expenses) sells goods again, and the goods guard stands
    again over its documents. A credit typed as a charge takes it - a count
    of 1 at a negative amount - stayed so: the document could not be saved
    untouched any more (the guard refused a row nobody had changed), and
    classifying its poste booked stock at -3,20 € the unit. It becomes a
    return, as goods keep one: the count negative, the amount as typed."""

    def setUp(self):
        self.supplier = make_supplier(code="ELEC_Y", name="Électricité Autre", parser_key="", expenses_only=True)
        self.invoice = import_parsed_invoice(
            self.supplier,
            ParsedInvoice(
                supplier_code="ELEC_Y",
                invoice_number="FA-000456",
                invoice_date=timezone.localdate() - timedelta(days=20),
                vat_breakdown=[(D("0.055"), D("3.02"), D("0.18")), (D("0.2"), MONTH_HT, MONTH_TAX)],
            ),
        )
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])
        typed = {"form-0-total_ttc": str(CREDIT_TTC), "form-0-amount_source": "ttc", "printed_total_ttc": str(BILL_TTC)}
        self.assertEqual(self.client.post(self.url, page_post(self.client.get(self.url), **typed)).status_code, 302)
        self.toggle = reverse("invoices:supplier_expenses", args=[self.supplier.pk])

    def switch_back(self):
        self.client.post(self.toggle, {"confirme": "1"})
        self.supplier.refresh_from_db()
        self.assertFalse(self.supplier.expenses_only)

    def credit(self):
        return self.invoice.lines.get(vat_rate=D("0.055"))

    def test_the_confirmation_says_what_becomes_of_it(self):
        self.assertContains(self.client.get(self.toggle), "1 avoir (une quantité positive à un montant négatif")
        other = make_supplier(code="EAU_Y", name="Eau Exemple", parser_key="", expenses_only=True)
        self.assertNotContains(
            self.client.get(reverse("invoices:supplier_expenses", args=[other.pk])), "un montant négatif"
        )

    def test_the_credit_becomes_a_return(self):
        self.switch_back()
        credit = self.credit()
        self.assertEqual(
            (credit.quantity, credit.total_ht, credit.printed_ttc, credit.unit_cost_ht),
            (-1, CREDIT_HT, CREDIT_TTC, -CREDIT_HT),
        )
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual((invoice.total_ttc, rounded_total(invoice)), (BILL_TTC, BILL_TTC))
        # The month's line is no credit: left as it was.
        self.assertEqual(invoice.lines.get(vat_rate=D("0.2")).quantity, 1)

    def test_the_document_still_saves_untouched(self):
        self.switch_back()
        response = self.client.post(self.url, page_post(self.client.get(self.url)))
        self.assertEqual(response.status_code, 302, response.context and response.context["formset"].errors)
        self.assertEqual((self.credit().quantity, self.credit().total_ht), (-1, CREDIT_HT))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).total_ttc, BILL_TTC)

    def test_classifying_its_poste_books_a_return(self):
        self.switch_back()
        poste = self.credit().product
        link_product_to_stock_type(poste, make_stock_type(name="Abonnement électrique"), poste.unit, D("1"))
        self.assertEqual(
            sorted(StockMovement.objects.filter(invoice_line__invoice=self.invoice).values_list("quantity", "unit_cost_ht")),
            [(D("-1"), -CREDIT_HT), (D("1"), MONTH_HT)],
        )
        self.assertEqual(negative_costs(), [])

    def test_a_movement_already_booked_is_booked_again_as_a_return(self):
        """A stock item among a charge supplier's products books its lines as
        they are - a credit the charge reading filed on it included."""
        bottle = make_product(
            supplier=self.supplier, raw_name="BOUTEILLE GAZ 13KG", stock_type=make_stock_type(name="Gaz en bouteille")
        )
        line = make_invoice_line(make_invoice(supplier=self.supplier), product=bottle, total_ht="-5.00")
        create_stock_movement_for_line(line)
        self.assertEqual(negative_costs(), [(D("1"), D("-5"))])
        stop_expenses(self.supplier)
        self.assertEqual(list(StockMovement.objects.values_list("quantity", "unit_cost_ht")), [(D("-1"), D("5"))])


class CreditMovedToAGoodsSupplierTests(TestCase):
    """The other way a document leaves charges: moved to a supplier of goods
    from its own page (« Changer de fournisseur », receipts.move_documents).
    Its lines were copied as they were, so a typed credit landed on goods at
    a count of 1: the page refused to save it untouched, and classifying its
    product booked stock at -3,20 € the unit. It becomes a return, as it does
    when the supplier itself leaves charges (importing.credit_as_return)."""

    def setUp(self):
        self.charge = make_supplier(code="ELEC_Z", name="Électricité Zed", parser_key="", expenses_only=True)
        self.goods = make_supplier(code="QUINC_Z", name="Quincaillerie Zed", parser_key="")
        self.invoice = import_parsed_invoice(
            self.charge,
            ParsedInvoice(
                supplier_code="ELEC_Z",
                invoice_number="FA-000789",
                invoice_date=timezone.localdate() - timedelta(days=20),
                vat_breakdown=[(D("0.055"), D("3.02"), D("0.18")), (D("0.2"), MONTH_HT, MONTH_TAX)],
            ),
        )
        self.url = reverse("invoices:receipt_review", args=[self.invoice.pk])
        typed = {"form-0-total_ttc": str(CREDIT_TTC), "form-0-amount_source": "ttc", "printed_total_ttc": str(BILL_TTC)}
        self.assertEqual(self.client.post(self.url, page_post(self.client.get(self.url), **typed)).status_code, 302)

    def move_to(self, supplier):
        response = self.client.post(self.url, {"action": "move_shop", "supplier": str(supplier.pk)})
        self.assertEqual(response.status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.supplier, supplier)

    def credit(self):
        return self.invoice.lines.get(vat_rate=D("0.055"))

    def test_the_credit_becomes_a_return(self):
        self.move_to(self.goods)
        credit = self.credit()
        self.assertEqual(
            (credit.quantity, credit.total_ht, credit.printed_ttc, credit.unit_cost_ht),
            (-1, CREDIT_HT, CREDIT_TTC, -CREDIT_HT),
        )
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).total_ttc, BILL_TTC)
        self.assertEqual(self.invoice.lines.get(vat_rate=D("0.2")).quantity, 1)

    def test_it_saves_untouched_on_its_new_supplier(self):
        self.move_to(self.goods)
        response = self.client.post(self.url, page_post(self.client.get(self.url)))
        self.assertEqual(response.status_code, 302, response.context and response.context["formset"].errors)
        self.assertEqual((self.credit().quantity, self.credit().total_ht), (-1, CREDIT_HT))

    def test_classifying_its_product_books_a_return(self):
        self.move_to(self.goods)
        product = self.credit().product
        link_product_to_stock_type(product, make_stock_type(name="Divers Zed"), product.unit, D("1"))
        self.assertEqual(
            sorted(StockMovement.objects.filter(invoice_line__invoice=self.invoice).values_list("quantity", "unit_cost_ht")),
            [(D("-1"), -CREDIT_HT), (D("1"), MONTH_HT)],
        )
        self.assertEqual(negative_costs(), [])

    def test_between_two_suppliers_of_charges_it_stays_a_credit(self):
        """A charge takes a credit at a count of 1: nothing to change."""
        self.move_to(make_supplier(code="ELEC_W", name="Électricité Wes", parser_key="", expenses_only=True))
        self.assertEqual((self.credit().quantity, self.credit().total_ht), (1, CREDIT_HT))
