"""The name a document's file is downloaded under (invoices/filenames.py),
and the route that serves it under that name (`invoices:invoice_file`):
« Darty 11€55 01_10_2026.pdf », wherever it is saved from."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from invoices.filenames import UniqueNames, amount_words, clean, download_name
from tests.factories import make_invoice, make_invoice_line, make_supplier


def document(name="Darty", total="11.55", day=date(2026, 10, 1), file_name="invoices/2026/10/abc123.pdf"):
    """What `download_name` reads, without a database."""
    return SimpleNamespace(
        supplier=SimpleNamespace(name=name),
        total_ttc=Decimal(total),
        invoice_date=day,
        source_file=SimpleNamespace(name=file_name) if file_name else None,
    )


class DownloadNameTests(SimpleTestCase):
    def test_the_owner_s_two_examples(self):
        self.assertEqual(download_name(document()), "Darty 11€55 01_10_2026.pdf")
        self.assertEqual(download_name(document("Metro", "115.26", date(2026, 8, 25))), "Metro 115€26 25_08_2026.pdf")

    def test_the_total_is_rounded_to_the_cent_and_keeps_its_zeros(self):
        """`total_ttc` is added up from HT lines and carries more places."""
        self.assertEqual(amount_words(Decimal("11.5549")), "11€55")
        self.assertEqual(amount_words(Decimal("11.555")), "11€56")
        self.assertEqual(amount_words(Decimal("7")), "7€00")
        self.assertEqual(amount_words(Decimal("0.05")), "0€05")
        self.assertEqual(amount_words(Decimal("1234.5")), "1234€50")

    def test_a_credit_note_keeps_its_minus(self):
        self.assertEqual(amount_words(Decimal("-11.55")), "-11€55")

    def test_an_undated_document_says_so(self):
        self.assertEqual(download_name(document(day=None)), "Darty 11€55 sans date.pdf")

    def test_a_ticket_s_photo_keeps_its_own_extension(self):
        self.assertEqual(download_name(document(file_name="invoices/2026/10/IMG_01.JPG")), "Darty 11€55 01_10_2026.jpg")

    def test_what_a_file_name_cannot_hold_is_taken_out(self):
        """A slash would be a folder in the zip; Windows refuses the rest."""
        self.assertEqual(clean('A/B: "C" <D>|E?*'), "A B C D E")
        self.assertEqual(clean("  Point final. "), "Point final")
        self.assertEqual(download_name(document(name="Leroy/Merlin")), "Leroy Merlin 11€55 01_10_2026.pdf")

    def test_a_supplier_whose_name_is_nothing_but_forbidden_characters(self):
        self.assertEqual(download_name(document(name="///")), "Fournisseur 11€55 01_10_2026.pdf")


class UniqueNamesTests(SimpleTestCase):
    def test_a_second_name_is_numbered_and_a_third_too(self):
        names = UniqueNames()
        taken = [names.take("Metro 115€26 25_08_2026.pdf") for _ in range(3)]
        self.assertEqual(
            taken,
            ["Metro 115€26 25_08_2026.pdf", "Metro 115€26 25_08_2026 (2).pdf", "Metro 115€26 25_08_2026 (3).pdf"],
        )

    def test_names_differing_by_case_alone_are_one_name_on_windows(self):
        names = UniqueNames()
        self.assertEqual(names.take("metro 1€00 sans date.pdf"), "metro 1€00 sans date.pdf")
        self.assertEqual(names.take("METRO 1€00 sans date.pdf"), "METRO 1€00 sans date (2).pdf")


class InvoiceFileRouteTests(TestCase):
    def setUp(self):
        self.invoice = make_invoice(supplier=make_supplier(name="Darty"), invoice_date=date(2026, 10, 1))
        make_invoice_line(invoice=self.invoice, total_ht="10.00", vat_rate=Decimal("0.20"))
        self.invoice.source_file.save("stocke-sous-un-autre-nom.pdf", ContentFile(b"%PDF-1.4 darty"))
        self.url = reverse("invoices:invoice_file", args=[self.invoice.pk])

    def test_the_pdf_is_shown_under_its_download_name(self):
        """Inline, so the frame and « Voir le PDF » show it - and the browser's
        own « save » takes the name from this header."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 darty")
        self.assertEqual(response["Content-Type"], "application/pdf")
        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith("inline"), disposition)
        self.assertIn("filename*=utf-8''Darty%2012%E2%82%AC00%2001_10_2026.pdf", disposition)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_telecharger_saves_it(self):
        disposition = self.client.get(self.url, {"telecharger": "1"})["Content-Disposition"]
        self.assertTrue(disposition.startswith("attachment"), disposition)
        self.assertIn("Darty%2012%E2%82%AC00%2001_10_2026.pdf", disposition)

    def test_the_document_s_pages_use_it(self):
        detail = self.client.get(reverse("invoices:invoice_detail", args=[self.invoice.pk]))
        self.assertContains(detail, f'href="{self.url}" target="_blank"')
        self.assertContains(detail, f'href="{self.url}?telecharger=1">Télécharger</a>')
        self.assertNotContains(detail, "stocke-sous-un-autre-nom")

    def test_no_file_is_a_404(self):
        bare = make_invoice()
        self.assertEqual(self.client.get(reverse("invoices:invoice_file", args=[bare.pk])).status_code, 404)

    def test_a_file_gone_from_the_disk_is_a_404(self):
        self.invoice.source_file.storage.delete(self.invoice.source_file.name)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_post_is_refused(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_a_download_is_sandboxed_like_any_attachment(self):
        response = self.client.get(self.url, {"telecharger": "1"})
        self.assertEqual(response["Content-Security-Policy"], "sandbox")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])

    def test_an_xml_invoice_is_a_sandboxed_download_named_the_same_way(self):
        xml = make_invoice(supplier=make_supplier(name="Grossiste"), invoice_date=date(2026, 9, 3))
        make_invoice_line(invoice=xml, total_ht="10.00", vat_rate=Decimal("0.20"))
        xml.source_file.save("facture.xml", ContentFile(b"<Invoice/>"))
        response = self.client.get(reverse("invoices:invoice_file", args=[xml.pk]))
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertIn("Grossiste%2012%E2%82%AC00%2003_09_2026.xml", response["Content-Disposition"])
        self.assertEqual(response["Content-Security-Policy"], "sandbox")

    def test_a_stored_name_climbing_out_of_media_is_a_404(self):
        self.invoice.source_file.name = "../../accounts.sqlite3"
        self.invoice.save(update_fields=["source_file"])
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_correction_page_frames_and_offers_it(self):
        page = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk]))
        self.assertContains(page, f'<iframe src="{self.url}"')
        self.assertContains(page, f'href="{self.url}?telecharger=1">Télécharger</a>')
        self.assertNotContains(page, "stocke-sous-un-autre-nom")
