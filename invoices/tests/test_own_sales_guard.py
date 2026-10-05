"""Achats refuses an electronic invoice the bar issued itself (D10).

The bar's own SIREN is data now: the SELLER its own sales e-invoices state
(`recipes.SaleDocument.seller_siren`). One of those dropped in Achats -
by the folder import, the upload, a gather - would name the bar a supplier
of itself and file the sale as money spent: silently wrong money. It is
refused before anything is filed, in one sentence that names the sale
document that taught the number (the way to undo a sale stored by mistake),
unless a supplier retains that SIREN - then it is that supplier's, and the
sales side refuses it instead.

Every SIREN is one of the house's invented ones
(recipes/tests/sale_einvoice_files.py); every name, number and amount is
invented.
"""

from __future__ import annotations

import os
import shutil
from datetime import date
from unittest import mock

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from accounts import paths
from invoices import einvoice, receipts
from invoices.models import Invoice, InvoiceLine, ScrapeJob, Supplier
from invoices.receipt_batches import STAGING_DIR, run_receipt_batch, stage_batch
from invoices.receipts import OWN_SALES_INVOICE, import_document
from invoices.tasks import _import_document_file, _import_downloaded_file
from invoices.tests.einvoice_files import CII_TWO_RATES
from invoices.tests.test_einvoice_import import seller, write_xml
from recipes.tests.sale_einvoice_files import (
    BAR_SIREN,
    BAR_VAT,
    CUSTOMER_SIREN,
    SALE_CII,
    SUPPLIER_SIREN,
    factur_x,
)
from staff.tests.page_forms import as_post, form_posting_to
from tests.factories import make_sale_document, make_supplier

#: The sale document that taught the bar's SIREN, as the refusal names it.
TAUGHT_BY = "n° FV-2026-0042 du 05/03/2026"
REFUSAL = OWN_SALES_INVOICE.format(siren="800 000 002", sale=TAUGHT_BY)

# SALE_CII with the seller's legal registration (BT-30) left out: its VAT
# number (BT-31) is all that names the bar.
_SELLER_LEGAL_ID = (
    "        <ram:SpecifiedLegalOrganization>\n"
    f'          <ram:ID schemeID="0002">{BAR_SIREN}</ram:ID>\n'
    "        </ram:SpecifiedLegalOrganization>\n"
)
SALE_CII_VAT_ONLY = SALE_CII.replace(_SELLER_LEGAL_ID, "", 1)


def the_bars_sale(**fields):
    """A sale document of the bar's, stating its SIREN as the seller - what
    « Lire la facture » stores from the bar's own electronic invoice."""
    fields.setdefault("seller_siren", BAR_SIREN)
    fields.setdefault("reference", "FV-2026-0042")
    return make_sale_document(sold_on=date(2026, 3, 5), **fields)


def stored_invoice_files() -> set[str]:
    """Every file under the stored invoices' folder (Invoice.source_file)."""
    folder = os.path.join(paths.media_root(), "invoices")
    return {os.path.join(root, name) for root, _dirs, names in os.walk(folder) for name in names}


class TheBarsOwnSalesInvoiceTests(TestCase):
    """Refused, said, nothing filed - whichever way it came in."""

    def setUp(self):
        self.sale = the_bars_sale()

    def test_the_bars_own_sales_invoice_is_refused_by_the_import(self):
        before = stored_invoice_files()
        for name, path in (
            ("facture-vente.xml", write_xml(self, "facture-vente.xml", SALE_CII)),
            ("facture-vente.pdf", factur_x(os.path.join(paths.media_root(), "facture-vente.pdf"), SALE_CII)),
        ):
            with self.subTest(name=name):
                self.addCleanup(lambda path=path: os.path.exists(path) and os.remove(path))
                with self.assertRaises(einvoice.OwnSalesInvoiceError) as refused:
                    import_document(path, display_filename=name)
                self.assertEqual(
                    str(refused.exception),
                    "Facture émise par votre établissement (SIREN 800 000 002, celui de votre facture de vente "
                    "n° FV-2026-0042 du 05/03/2026) : ajoutez-la dans « Recettes & ventes · Ventes », "
                    "ce n'est pas un achat.",
                )
                # One of einvoice's refusals: every handler saying those says it.
                self.assertIsInstance(refused.exception, einvoice.EInvoiceError)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(stored_invoice_files(), before)
        # Nothing learned it either: the bar is no supplier of its own.
        learned = [
            supplier
            for supplier in Supplier.objects.all()
            if f"siren:{BAR_SIREN}" in (supplier.ticket_identifiers or ())
        ]
        self.assertEqual(learned, [])

    def test_named_by_hand_it_is_refused_all_the_same(self):
        """Chosen under a supplier, it would have been filed and taught that
        supplier the bar's own number."""
        supplier = make_supplier(code="BAR_X", name="Bar des tests", parser_key="")
        path = write_xml(self, "facture-vente.xml", SALE_CII)
        with self.assertRaisesMessage(einvoice.OwnSalesInvoiceError, REFUSAL):
            import_document(path, display_filename="facture-vente.xml", supplier=supplier)
        supplier.refresh_from_db()
        self.assertEqual(list(supplier.ticket_identifiers or []), [])
        self.assertFalse(Invoice.objects.exists())

    def test_the_refusal_names_a_sale_with_no_number(self):
        self.sale.delete()
        the_bars_sale(reference="")
        path = write_xml(self, "facture-vente.xml", SALE_CII)
        with self.assertRaises(einvoice.OwnSalesInvoiceError) as refused:
            import_document(path, display_filename="facture-vente.xml")
        self.assertIn("celui de votre facture de vente sans numéro du 05/03/2026", str(refused.exception))

    def test_the_latest_sale_stating_it_is_the_one_named(self):
        """Several of the bar's sales state its SIREN: the refusal names the
        latest sold (the last stored, on the same day) - the one to look at
        first if a sale was stored by mistake."""
        make_sale_document(reference="FV-2026-0007", seller_siren=BAR_SIREN, sold_on=date(2026, 1, 9))
        make_sale_document(reference="FV-2026-0050", seller_siren=BAR_SIREN, sold_on=date(2026, 4, 2))
        make_sale_document(reference="FV-2026-0051", seller_siren=BAR_SIREN, sold_on=date(2026, 4, 2))
        make_sale_document(reference="FV-2026-0099", seller_siren=CUSTOMER_SIREN, sold_on=date(2026, 6, 1))
        path = write_xml(self, "facture-vente.xml", SALE_CII)
        with self.assertRaises(einvoice.OwnSalesInvoiceError) as refused:
            import_document(path, display_filename="facture-vente.xml")
        self.assertIn("celui de votre facture de vente n° FV-2026-0051 du 02/04/2026", str(refused.exception))

    def test_the_folder_import_says_it_on_the_files_line(self):
        seller()
        batch = stage_batch(
            [
                SimpleUploadedFile("facture-vente.xml", SALE_CII.encode("utf-8")),
                SimpleUploadedFile("facture-achat.xml", CII_TWO_RATES.encode("utf-8")),
            ]
        )
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), STAGING_DIR, str(batch.pk)), True)
        batch = run_receipt_batch(batch.pk)
        self.assertEqual([entry["status"] for entry in batch.results], ["error", "ok"])
        self.assertEqual(batch.results[0]["message"], REFUSAL)
        self.assertIn(f"facture-vente.xml : {REFUSAL}", batch.log)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_the_upload_says_it_and_no_shop_to_choose(self):
        # The import card's own form, posted as a browser posts it.
        browser = self.client_class(enforce_csrf_checks=True)
        url = reverse("invoices:invoice_upload")
        card = form_posting_to(browser.get(url).content.decode(), url)
        data = as_post(card.submission(values={"supplier": "new", "new_name": "Bar des tests"}))
        upload = SimpleUploadedFile("facture-vente.xml", SALE_CII.encode("utf-8"))
        response = browser.post(url, {**data, "source_file": upload})
        self.assertRedirects(response, f"{reverse('invoices:invoice_list')}?ajouter=pdf", fetch_redirect_response=False)
        said = [(message.level_tag, message.message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(
            said,
            [
                ("error", f"Échec de l'import. {REFUSAL}"),
                # Made before the file was read, it stays - but there is no
                # document of the bar's to choose it for.
                ("info", "Fournisseur Bar des tests créé, sans ce document."),
            ],
        )
        self.assertFalse(Invoice.objects.exists())
        page = browser.get(response["Location"])
        self.assertContains(page, escape(f"Échec de l'import. {REFUSAL}"))

    def test_a_gather_logs_it_without_a_traceback_and_moves_on(self):
        """Refused for what it is: False (the coverage moves on, a refusal
        fetched again every day would hold the source back), one line in the
        log and no traceback."""
        supplier = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        job = ScrapeJob.objects.create()
        path = write_xml(self, "facture-vente.xml", SALE_CII)
        self.assertIs(_import_document_file(job, supplier, path, chosen_because="Téléchargée par « Exemple »."), False)
        job.refresh_from_db()
        self.assertIn(f"facture-vente.xml : {REFUSAL}", job.log)
        self.assertNotIn("Traceback", job.log)
        self.assertFalse(Invoice.objects.exists())

    def test_a_readers_gather_logs_it_the_same_way(self):
        """Metro's and a mailbox source naming its reader: the file goes
        straight to import_einvoice (tasks._import_downloaded_file)."""
        job = ScrapeJob.objects.create()
        path = write_xml(self, "facture-vente-metro.xml", SALE_CII)
        self.assertIs(_import_downloaded_file(job, Supplier.objects.get(code="METRO"), path), False)
        job.refresh_from_db()
        self.assertIn(f"facture-vente-metro.xml : {REFUSAL}", job.log)
        self.assertNotIn("Traceback", job.log)
        self.assertFalse(Invoice.objects.exists())

    def test_the_vat_number_alone_names_it(self):
        facts = einvoice.read(SALE_CII_VAT_ONLY.encode("utf-8")).einvoice
        assert facts is not None
        self.assertEqual((facts.seller_siren, facts.seller_vat), ("", BAR_VAT))
        path = write_xml(self, "facture-vente.xml", SALE_CII_VAT_ONLY)
        with self.assertRaisesMessage(einvoice.OwnSalesInvoiceError, REFUSAL):
            import_document(path, display_filename="facture-vente.xml")
        self.assertFalse(Invoice.objects.exists())


class WhatIsNotRefusedTests(TestCase):
    """Nothing else in Achats moves."""

    def test_nothing_is_refused_without_a_sale_document_holding_that_siren(self):
        """No sale document states the bar's SIREN (a document typed by hand
        states none, another states another seller): filed as it always was,
        under the supplier named by hand."""
        make_sale_document(reference="FV-TAPEE")
        make_sale_document(reference="FV-AUTRE", seller_siren=CUSTOMER_SIREN)
        supplier = make_supplier(code="BAR_X", name="Bar des tests", parser_key="")
        path = write_xml(self, "facture-vente.xml", SALE_CII)
        invoice = import_document(path, display_filename="facture-vente.xml", supplier=supplier)
        self.assertEqual(invoice.supplier, supplier)

    def test_a_siren_a_supplier_retains_is_that_suppliers(self):
        """A sale document stating a supplier's SIREN as its seller - stored
        before the sales side refused it, or the supplier's number really is
        the bar's: the supplier's invoices still come in."""
        supplier = seller()
        make_sale_document(reference="FV-ERREUR", seller_siren=SUPPLIER_SIREN)
        path = write_xml(self, "facture.xml", CII_TWO_RATES)
        invoice = import_document(path, display_filename="facture.xml")
        self.assertEqual(invoice.supplier, supplier)

    def test_an_ordinary_supplier_einvoice_imports_as_before(self):
        """The bar is the BUYER of every purchase: the guard reads the seller
        only."""
        the_bars_sale()
        supplier = seller()
        path = write_xml(self, "facture.xml", CII_TWO_RATES)
        invoice = import_document(path, display_filename="facture.xml")
        self.assertEqual((invoice.supplier, invoice.einvoice_format), (supplier, "CII"))
        self.assertEqual(InvoiceLine.objects.filter(invoice=invoice).count(), 2)

    def test_one_query_more_per_einvoice(self):
        """The guard costs one indexed query per electronic invoice: the same
        import counted with it, without it (its function answering "" as if
        it were not there), and with it again."""
        the_bars_sale()
        seller()
        path = write_xml(self, "facture.xml", CII_TWO_RATES)
        counts = []
        for guarded in (True, False, True):
            with transaction.atomic(), CaptureQueriesContext(connection) as queries:
                if guarded:
                    invoice = import_document(path, display_filename="facture.xml")
                else:
                    with mock.patch.object(receipts, "own_sales_invoice", return_value=""):
                        invoice = import_document(path, display_filename="facture.xml")
                stored = invoice.source_file.path
                transaction.set_rollback(True)
            self.addCleanup(lambda stored=stored: os.path.exists(stored) and os.remove(stored))
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[2])
        self.assertEqual(counts[0] - counts[1], 1)
