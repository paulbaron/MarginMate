"""Every amount the invoices pages and sentences show has its thousands
grouped by a no-break space (the owner, 01/10/2026: « 10000€ -> 10 000€ »),
and nothing a form, a script or a reader reads back is.

The pages keep their decimal point (« 1 234.56 € », en-us), Metro's warning
its comma (« 1 234,56 € »): only the grouping is new. Data invented.
"""

import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from invoices import einvoice, views
from invoices.filenames import amount_words
from invoices.forms import DOCUMENT_INVOICE, DOCUMENT_RECEIPT, LineCorrectionForm
from invoices.importing import charge_checks
from invoices.models import Invoice, ReceiptBatch, ShopItemPrice, Supplier
from invoices.parsers import ticket_parser_for
from invoices.parsers.base import PdfPage
from invoices.parsers.metro import _reconciliation_warning
from invoices.parsers.receipt_base import missing_item_check, reconcile_quantity
from invoices.receipts import einvoice_checks, lines_check, vat_table_checks
from invoices.tests.einvoice_files import CII_DOCUMENT_CHARGE, CII_TWO_RATES
from invoices.tests.test_document_page import messages_of
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

D = Decimal
NBSP = "\N{NO-BREAK SPACE}"
TWENTY = D("0.2")
FIVE_FIVE = D("0.055")
FAILED = [{"label": "Somme des lignes = total imprimé", "passed": False, "detail": ""}]

FRANPRIX_HEAD = "franprix\nFRANPRIX\n12 RUE INVENTEE\n75000 PARIS\n004211-01\n**DUPLICATA**-\n"
FRANPRIX_FOOT = "15-07-2026 MARDI  13:40\nLORIAN  R1 004211-01 190\n**DUPLICATA**-\n"


def supplier_invoice():
    """A supplier's invoice past a thousand euros: one line of 1 234,56 € HT
    at 20 %, and 1 000,00 € of duty charged outside the lines."""
    invoice = make_invoice(
        supplier=make_supplier(name="Grossiste Exemple"),
        reconciliation_adjustment=D("1000.00"),
        adjustment_vat_rate=TWENTY,
    )
    make_invoice_line(
        invoice=invoice,
        product=make_product(supplier=invoice.supplier, raw_name="FOUR A PIZZA"),
        raw_name="FOUR A PIZZA",
        quantity=1,
        total_ht="1234.56",
        vat_rate=TWENTY,
    )
    return invoice


def big_ticket(**fields):
    """A till receipt still to be checked, one line printed 1 234,56 €."""
    shop = Supplier.objects.get(code="FRANPRIX")
    invoice = make_invoice(supplier=shop, parse_checks=list(FAILED), **fields)
    make_invoice_line(
        invoice=invoice,
        product=make_product(supplier=shop, raw_name="FOUR INVENTE"),
        raw_name="FOUR INVENTE",
        quantity=1,
        total_ht="1170.20",
        vat_rate=FIVE_FIVE,
        printed_ttc=D("1234.56"),
    )
    return invoice


class DocumentPagesTests(TestCase):
    def setUp(self):
        self.invoice = supplier_invoice()

    def test_the_document_page_groups_every_amount(self):
        page = self.client.get(reverse("invoices:invoice_detail", args=[self.invoice.pk])).content.decode()
        self.assertIn(f'<td class="num">1{NBSP}234.56 €</td>', page)  # the line's HT
        self.assertIn(f'<td class="num">1{NBSP}234.5600 €</td>', page)  # its unit cost, four places
        self.assertIn(f'<td class="num">1{NBSP}000.00 €</td>', page)  # the duty
        self.assertIn(f"2{NBSP}234.56 €", page)  # total HT
        self.assertIn(f"2{NBSP}681.47 €", page)  # total TTC: 1 481,47 + 1 200,00
        self.assertNotIn("1234.56 €", page)

    def test_the_documents_list_and_a_row_opened_group_their_totals(self):
        listed = self.client.get(reverse("invoices:invoice_list")).content.decode()
        self.assertIn(f'data-label="Total HT">2{NBSP}234.56 €</td>', listed)
        self.assertIn(f'data-label="Total TTC">2{NBSP}681.47 €</td>', listed)
        opened = self.client.get(reverse("invoices:invoice_preview", args=[self.invoice.pk])).content.decode()
        self.assertIn(f"1{NBSP}234.56 €", opened)
        self.assertIn(f"1{NBSP}481.47 €", opened)

    def test_the_deletion_page_groups_the_total(self):
        page = self.client.get(reverse("invoices:invoice_delete", args=[self.invoice.pk])).content.decode()
        self.assertIn(f"2{NBSP}681.47 €", page)


class QueuePagesTests(TestCase):
    def test_a_ticket_to_check_and_a_document_to_fix_group_their_totals(self):
        big_ticket()
        broken = make_invoice(supplier=make_supplier(name="Fournisseur Cassé"), status=Invoice.Status.ERROR)
        make_invoice_line(invoice=broken, total_ht="4321.00", vat_rate=TWENTY)
        page = self.client.get(reverse("invoices:receipt_queue")).content.decode()
        self.assertIn(f'<span class="amount">1{NBSP}234.56 €</span>', page)
        self.assertIn(f'data-label="Total HT">4{NBSP}321.00 €</td>', page)

    def test_an_import_groups_what_it_read(self):
        ticket = big_ticket()
        batch = ReceiptBatch.objects.create(
            results=[
                {
                    "name": "four.jpg",
                    "status": "ok",
                    "invoice_id": ticket.pk,
                    "shop": "Franprix",
                    "total": "1234.56",
                    "date": "01/01/2026",
                    "receipt": True,
                },
                {
                    "name": "inconnu.jpg",
                    "status": "unrecognised",
                    "message": "Enseigne non reconnue",
                    "header": "EPICERIE INVENTEE",
                    "read_date": "15/07/2026",
                    "read_total": "2345.60",
                    "kept": False,
                },
            ]
        )
        page = self.client.get(reverse("invoices:receipt_batch_status", args=[batch.pk])).content.decode()
        self.assertIn(f'data-label="Total TTC">1{NBSP}234.56 €</td>', page)
        self.assertIn(f'data-label="Total TTC">2{NBSP}345.60 €</td>', page)
        # What the import wrote down stays as it was written: the page groups
        # it, the record does not.
        batch.refresh_from_db()
        self.assertEqual(
            [entry.get("total", entry.get("read_total")) for entry in batch.results], ["1234.56", "2345.60"]
        )


class CorrectionPageTests(TestCase):
    """The correction page: its sentences grouped, its boxes and the figures
    its script computes with left as the server parses them back."""

    def setUp(self):
        self.invoice = supplier_invoice()
        line = self.invoice.lines.get()
        line.quantity = 2
        line.unit_cost_ht = D("617.28")
        line.save()
        Invoice.objects.filter(pk=self.invoice.pk).update(printed_total_ttc=D("2681.47"))
        self.response = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        self.page = self.response.content.decode()

    def test_the_lines_check_is_grouped(self):
        self.assertEqual(
            self.response.context["live_check"]["detail"],
            f"lignes 1{NBSP}481.47 € + 1{NBSP}200.00 € de frais facturés globalement = 2{NBSP}681.47 € "
            f"/ total de la facture 2{NBSP}681.47 € (écart +0.00 €)",
        )
        self.assertIn(f'<span id="document-total">2{NBSP}681.47</span> € TTC', self.page)

    def test_a_price_each_is_grouped(self):
        self.assertEqual(self.response.context["formset"].forms[0].unit_price_hint, "soit 617.28 € HT l'unité")
        form = LineCorrectionForm(
            document=DOCUMENT_INVOICE,
            data={"product_name": "FOUR", "quantity": "2", "total_ht": "2469.12", "vat_rate": "20"},
        )
        self.assertEqual(form.unit_price_hint, f"soit 1{NBSP}234.56 € HT l'unité")
        form = LineCorrectionForm(
            document=DOCUMENT_RECEIPT,
            data={
                "product_name": "FOUR",
                "quantity": "2",
                "total_ttc": "4000.00",
                "discount_ttc": "1531.00",
                "vat_rate": "5.5",
            },
        )
        self.assertEqual(form.unit_price_hint, f"soit 1{NBSP}234.50 € TTC l'unité après remise (2{NBSP}000.00 € avant)")

    def test_what_is_typed_or_computed_with_stays_raw(self):
        self.assertIn('value="1234.56"', self.page)  # the line's HT box
        self.assertIn('value="2681.47"', self.page)  # the total box
        self.assertIn('data-adjustment="1200.00"', self.page)
        self.assertNotIn(f'value="1{NBSP}', self.page)


class EInvoiceAsStatedTests(TestCase):
    """An e-invoice received as XML shows its own text beside the lines
    (« telle qu'elle est déclarée »): its amounts grouped on the page like
    every other figure there, its stored text left as the readers parse it."""

    def test_the_panel_groups_its_amounts(self):
        xml = CII_TWO_RATES.replace(
            "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
            "<ram:GrandTotalAmount>1229.39</ram:GrandTotalAmount>",
        )
        stated = einvoice.read(xml.encode("utf-8")).source_text
        invoice = make_invoice(einvoice_format="CII", source_text=stated)
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[invoice.pk])).content.decode()
        self.assertIn(f"Total TTC 1{NBSP}229.39 €", page)
        self.assertNotIn("Total TTC 1229.39 €", page)
        invoice.refresh_from_db()
        self.assertEqual(invoice.source_text, stated)

    def test_only_the_money_is_grouped(self):
        stated = [
            "Facture électronique (CII)",
            "SIREN 123456782",
            "TVA intracommunautaire FR40123456782",
            "Facture n° 20261001 du 01/10/2026",
            "FOUR 2000 W - 1200 x 1500.0000 = 1800000.00 € HT (20.00 %)",
            "Arrondi -1234.00 €",
            "Total TTC 999.99 €",
        ]
        invoice = Invoice(einvoice_format="CII", source_text="\n".join(stated))
        self.assertEqual(
            views._stated_lines(invoice),
            [
                "Facture électronique (CII)",
                "SIREN 123456782",
                "TVA intracommunautaire FR40123456782",
                "Facture n° 20261001 du 01/10/2026",
                f"FOUR 2000 W - 1200 x 1{NBSP}500.0000 = 1{NBSP}800{NBSP}000.00 € HT (20.00 %)",
                f"Arrondi -1{NBSP}234.00 €",
                "Total TTC 999.99 €",
            ],
        )

    def test_nothing_for_a_document_that_is_no_einvoice(self):
        self.assertEqual(views._stated_lines(Invoice(source_text="Total TTC 1234.56 €")), [])


class KnownPricesTests(TestCase):
    def setUp(self):
        self.ticket = big_ticket()
        self.url = reverse("invoices:receipt_review", args=[self.ticket.pk])

    def test_the_list_and_its_question_group_the_price(self):
        price = ShopItemPrice.objects.create(supplier=self.ticket.supplier, unit_price_ttc=D("1234.50"), label="FOUR")
        page = self.client.get(self.url).content.decode()
        self.assertIn(f'<td class="num">1{NBSP}234.50 €</td>', page)
        self.assertIn(f"Oublier que 1{NBSP}234.50 € désigne « FOUR »", page)
        self.assertIn(f'name="price" value="{price.pk}"', page)
        self.assertEqual(str(price), f"Franprix 1{NBSP}234.50 EUR -> FOUR")

    def test_remembering_and_forgetting_say_the_price_grouped(self):
        posted = {"action": "remember_price", "unit_price_ttc": "1234.50", "label": "FOUR", "valid_from": ""}
        response = self.client.post(self.url, posted)
        self.assertTrue(any(f"Prix retenu : 1{NBSP}234.50 € = FOUR" in said for said in messages_of(response)))
        refused = self.client.post(self.url, posted)
        self.assertContains(refused, f"1{NBSP}234.50 € est déjà retenu chez Franprix")
        price = ShopItemPrice.objects.get()
        self.assertEqual(price.unit_price_ttc, D("1234.50"))
        response = self.client.post(self.url, {"action": "forget_price", "price": price.pk})
        self.assertTrue(any(f"Prix oublié : 1{NBSP}234.50 € = FOUR" in said for said in messages_of(response)))


class LiveChecksScriptTests(SimpleTestCase):
    """The page's script writes the checks' sentences word for word as
    receipts.lines_check and vat_table_checks write them on validation, so
    it groups like the server; and it writes HT and TTC into each other's
    boxes, which the server parses back, so those stay plain."""

    def setUp(self):
        path = Path(settings.BASE_DIR) / "invoices" / "templates" / "invoices" / "document_review.html"
        self.script = path.read_text(encoding="utf-8").split("<script>")[1].split("</script>")[0]

    def test_the_separator_is_the_servers_written_as_a_char_code(self):
        self.assertIn("var THOUSANDS_SEPARATOR = String.fromCharCode(0xa0);", self.script)
        self.assertNotIn("\\u00a0", self.script.lower())
        self.assertNotIn(NBSP, self.script)
        self.assertNotIn("\N{NARROW NO-BREAK SPACE}", self.script)

    def test_every_sentence_is_written_grouped(self):
        statements = self.script.split(";")
        sentences = [statement for statement in statements if "€" in statement and "(" in statement]
        self.assertGreaterEqual(len(sentences), 5)
        for sentence in sentences:
            with self.subTest(sentence=sentence.strip()):
                self.assertNotIn("euros(", sentence)
        for written in re.findall(r"textContent = [^;]+;", self.script):
            with self.subTest(written=written):
                self.assertNotIn("euros(", written)
        self.assertIn("shownTotal.textContent = money(sum)", self.script)
        self.assertIn("return money(Math.sign(value)", self.script)  # « soit … € l'unité »

    def test_the_boxes_are_written_plain(self):
        self.assertIn('ttc.value = htCents === null ? "" : euros(Math.round(htCents * factor));', self.script)
        self.assertIn('ht.value = ttcCents === null ? "" : euros(Math.round(ttcCents / factor));', self.script)
        self.assertNotIn(".value = money(", self.script)


class ChecksWrittenOnValidationTests(TestCase):
    def test_the_lines_against_the_ticket(self):
        ticket = big_ticket(printed_total_ttc=D("2500.00"))
        self.assertEqual(
            lines_check(ticket)["detail"],
            f"lignes 1{NBSP}234.56 € / ticket 2{NBSP}500.00 € (écart +1{NBSP}265.44 €)",
        )

    def test_a_gap_below_zero_and_the_promotions(self):
        ticket = big_ticket(printed_total_ttc=D("100.00"))
        ticket.lines.update(discount_ttc=D("1000.00"))
        self.assertEqual(
            lines_check(ticket)["detail"],
            f"lignes 234.56 € / ticket 100.00 € (écart -134.56 €) - articles 1{NBSP}234.56 € moins "
            f"1{NBSP}000.00 € de remises",
        )

    def test_the_vat_table(self):
        ticket = big_ticket()
        table = [{"rate": TWENTY, "base": D("1500.00"), "vat": D("300.00")}]
        self.assertEqual(
            [check["detail"] for check in vat_table_checks(ticket, table)],
            [
                f"HT 1{NBSP}500.00 € x 20% = 300.00 € / document 300.00 €",
                f"lignes 1{NBSP}170.20 € HT / document 1{NBSP}500.00 € HT (écart +329.80 €)",
            ],
        )

    def test_a_charges_total(self):
        charge = make_invoice(supplier=make_supplier(name="Bailleur Exemple", expenses_only=True))
        (total, *_dated) = charge_checks(charge, D("1850.00"))
        self.assertEqual(total["detail"], f"1{NBSP}850.00 € : le total imprimé sur le document.")


class ReadersSentencesTests(SimpleTestCase):
    """What a reader writes for a person - its checks, its warnings - is
    grouped; what it reads never is."""

    def test_a_tickets_sums_and_its_promotion(self):
        text = (
            FRANPRIX_HEAD + "CONCOMBRE  T1 1.99Eur\n" + "FOUR INVENTE  T1 1000.00Eur\n" * 3 + "SOUS-TOTAL  2001.99Eur\n"
            "TOTAL SANS AVANTAGES  3001.99Eur\n"
            "Detai des renises inmediates :\n"
            "3 pour 2\n"
            "FOUR INVENTE  1000.00Eur\n"
            "TOTAL remise  1000.00Eur\n"
            "TOTAL A PAYER  2001.99Eur\n"
            "CB SANS CONTACT  2001.99Eur\n"
            "rTaux-Ir-Tot.HT-r-Tot.TVA-r-Tot.TTC-\n"
            "5.5%  1897.62  104.37  2001.99\n" + FRANPRIX_FOOT
        )
        parsed = ticket_parser_for("FRANPRIX").parse_pages([PdfPage(text=text)])
        said = {check.label: check.detail for check in parsed.checks}
        self.assertEqual(
            said["Somme des lignes = total imprimé"], f"lignes 2{NBSP}001.99 € / ticket 2{NBSP}001.99 € (écart +0.00 €)"
        )
        self.assertEqual(said["TVA 5.5% cohérente"], f"HT 1{NBSP}897.62 € x 5.5% = 104.37 € / ticket 104.37 €")
        self.assertEqual(said["Remise attribuée"], f"remise 1{NBSP}000.00 € répartie sur : FOUR INVENTE")
        # The amounts read are the figures, whatever the sentences say.
        self.assertEqual(parsed.printed_total_ttc, D("2001.99"))

    def test_a_missing_article_and_a_count_that_does_not_fit(self):
        missing = missing_item_check(D("1500.00"), (D("2500.00"), D("100.00")))
        self.assertEqual(
            missing.detail,
            f"les articles lus font 1{NBSP}500.00 €, le ticket imprime 2{NBSP}500.00 € avant une remise de "
            "100.00 € : un article manque ou est mal lu",
        )
        fixes, problems = [], []
        self.assertEqual(reconcile_quantity("FOUR", 8, D("1250.00"), D("2500.00"), fixes, problems), 2)
        self.assertEqual(reconcile_quantity("PLAN", 3, D("1000.50"), D("2500.00"), fixes, problems), 3)
        self.assertEqual(fixes, [f"FOUR : 8 lu, 2 d'après 2{NBSP}500.00 € / 1{NBSP}250.00 €"])
        self.assertEqual(problems, [f"PLAN : 3 x 1{NBSP}000.50 € ≠ 2{NBSP}500.00 €"])

    def test_metros_warning_keeps_its_comma(self):
        (said,) = _reconciliation_warning(D("1000.00"), D("2234.56"))
        self.assertEqual(
            said,
            f"Les lignes lues font 1{NBSP}000,00 € HT alors que la facture imprime 2{NBSP}234,56 € HT "
            f"(écart 1{NBSP}234,56 €) : une ligne n'a pas été lue. Vérifiez le document.",
        )

    def test_an_einvoices_checks_are_grouped_and_its_text_is_not(self):
        xml = CII_TWO_RATES.replace(
            "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
            "<ram:GrandTotalAmount>1229.39</ram:GrandTotalAmount>",
        )
        parsed = einvoice.read(xml.encode("utf-8"))
        (total,) = [check for check in parsed.checks if check.label == einvoice.TOTAL_CHECK]
        self.assertEqual(
            total.detail,
            f"HT 194.20 € + TVA 35.19 € = 229.39 € / facture 1{NBSP}229.39 € (écart +1{NBSP}000.00 €)",
        )
        self.assertIn(f"(écart +1{NBSP}000.00 €)", " ".join(parsed.warnings))
        # The invoice as text is read again - by identifiers.py, by the
        # charge reading - and stays as a reader expects it.
        self.assertIn("Total TTC 1229.39 €", parsed.source_text)
        self.assertNotIn(NBSP, parsed.source_text)

    def test_an_einvoices_lines_and_vat_table_against_its_base(self):
        xml = CII_TWO_RATES.replace(
            "<ram:TaxBasisTotalAmount>194.20</ram:TaxBasisTotalAmount>",
            "<ram:TaxBasisTotalAmount>1194.20</ram:TaxBasisTotalAmount>",
        ).replace(
            "<ram:GrandTotalAmount>229.39</ram:GrandTotalAmount>",
            "<ram:GrandTotalAmount>1229.39</ram:GrandTotalAmount>",
        )
        said = {check.label: check.detail for check in einvoice.read(xml.encode("utf-8")).checks}
        self.assertEqual(
            said[einvoice.LINES_CHECK],
            f"lignes 194.20 € + frais et remises +0.00 € = 194.20 € / base HT 1{NBSP}194.20 € (écart +1{NBSP}000.00 €)",
        )
        self.assertEqual(
            said[einvoice.VAT_CHECK],
            f"table 194.20 € HT / 35.19 € de TVA - facture 1{NBSP}194.20 € HT / 35.19 € de TVA",
        )

    def test_the_charges_outside_the_lines_of_an_einvoice(self):
        parsed = einvoice.read(CII_DOCUMENT_CHARGE.encode("utf-8"))
        parsed.reconciliation_adjustment = D("1234.50")
        said = [check["detail"] for check in einvoice_checks("Factur-X", parsed.einvoice, parsed)]
        self.assertTrue(any(detail.startswith(f"+1{NBSP}234.50 € HT facturés globalement") for detail in said), said)

    def test_a_figure_too_wide_names_an_amount_grouped_and_a_count_plain(self):
        with self.assertRaises(einvoice.EInvoiceError) as amount:
            einvoice._money(D("12345678901.50"))
        self.assertIn(f"(12{NBSP}345{NBSP}678{NBSP}901.50)", str(amount.exception))
        with self.assertRaises(einvoice.EInvoiceError) as count:
            einvoice._quantity(D("12345678901"))
        self.assertIn("(12345678901.000)", str(count.exception))


class FilenameStaysAsItIsTests(SimpleTestCase):
    """The download name is a file name, not a sentence: never grouped."""

    def test_a_big_total_in_the_file_name(self):
        self.assertEqual(amount_words(D("16568684.50")), "16568684€50")
