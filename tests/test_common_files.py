"""Three helpers of common.py the documents' files lean on.

* `fits_column`: whether a figure fits the DecimalField that stores it.
  SQLite stores a figure wider than its column without a word, and Django's
  converter then raises decimal.InvalidOperation on every READ of the row -
  the page, the list and even a delete answer 500 (CLAUDE.md « A figure
  wider than the column behind it is refused, at the door »). So the
  question is asked the way that read does it: quantized to the column's
  places, in the column's context. Shared by Achats' lines
  (invoices.importing._fitting) and the sales e-invoice reader
  (recipes/sale_einvoice.py).
* `posted_digest`: what a page sent, but the values that differ each time it
  is sent - the one-time `jeton` of a hand-typed invoice and of a sale
  document (moved from invoices.views, the same digest).
* `delete_stored_files`: each stored file of a row deleted once its
  transaction commits, one already gone passed over.

Invented figures and names.
"""

import hashlib
import tempfile
from decimal import Decimal
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase

from common import delete_stored_files, fits_column, posted_digest
from invoices.models import InvoiceLine
from recipes.models import SaleDocument, SaleDocumentLine

D = Decimal


class FitsColumnTests(SimpleTestCase):
    def test_nothing_fits(self):
        self.assertTrue(fits_column(SaleDocument, "stated_total_ttc", None))

    def test_the_last_amount_that_fits_and_one_past_it(self):
        # (12, 2): ten digits before the point.
        self.assertTrue(fits_column(SaleDocument, "stated_total_ttc", D("9999999999.99")))
        self.assertTrue(fits_column(SaleDocument, "stated_total_ttc", D("-9999999999.99")))
        self.assertFalse(fits_column(SaleDocument, "stated_total_ttc", D("10000000000.00")))
        self.assertFalse(fits_column(SaleDocument, "stated_total_ttc", D("-10000000000")))

    def test_a_figure_rounding_past_the_column_does_not_fit(self):
        self.assertFalse(fits_column(SaleDocument, "stated_total_ttc", D("9999999999.995")))
        self.assertTrue(fits_column(SaleDocument, "stated_total_ttc", D("9999999999.994")))

    def test_more_places_than_the_column_fit_when_rounded(self):
        # (10, 4): six digits before the point.
        self.assertTrue(fits_column(SaleDocumentLine, "quantity", D("1.234567")))
        self.assertTrue(fits_column(SaleDocumentLine, "quantity", D("999999.9999")))
        self.assertFalse(fits_column(SaleDocumentLine, "quantity", D("999999.99995")))
        self.assertFalse(fits_column(SaleDocumentLine, "quantity", D("1234567.5")))

    def test_each_column_its_own_places(self):
        # The same figure fits Achats' quantity (12, 3), not a sale line's (10, 4).
        self.assertTrue(fits_column(InvoiceLine, "quantity", D("1234567.5")))
        self.assertTrue(fits_column(SaleDocumentLine, "vat_rate", D("9.9999")))
        self.assertFalse(fits_column(SaleDocumentLine, "vat_rate", D("10")))

    def test_an_int_and_a_text_figure(self):
        self.assertTrue(fits_column(SaleDocumentLine, "quantity", 3))
        self.assertTrue(fits_column(SaleDocumentLine, "quantity", "2.5"))
        self.assertFalse(fits_column(SaleDocumentLine, "quantity", "beaucoup"))

    def test_an_exponent_does_not_fit(self):
        self.assertFalse(fits_column(SaleDocument, "payable_ttc", D("1E+999999999")))
        self.assertFalse(fits_column(SaleDocument, "payable_ttc", D("Infinity")))
        self.assertTrue(fits_column(SaleDocument, "payable_ttc", D("1E-500")))


class PostedDigestTests(SimpleTestCase):
    """The digest a page's one-time `jeton` is kept with: a page posted again
    as it was is the same submission, another invoice typed on the same page
    is not."""

    def posted(self, data, files=()):
        payload = dict(data)
        for name, upload in files:
            payload[name] = upload
        return RequestFactory().post("/", payload)

    def test_the_digest_invoices_views_gave(self):
        """Fields sorted by name, every value of each, then each file's field,
        name and size - the recipe of invoices.views._posted, unchanged."""
        request = self.posted(
            {"numero": "F-1", "total": ["12,50", "3"], "csrfmiddlewaretoken": "abc", "jeton": "j1"},
            [("fichier", SimpleUploadedFile("facture.pdf", b"%PDF-1.4 exemple"))],
        )
        fields = [("numero", ["F-1"]), ("total", ["12,50", "3"])]
        files = [("fichier", "facture.pdf", 16)]
        expected = hashlib.sha256(repr((fields, files)).encode()).hexdigest()
        self.assertEqual(posted_digest(request), expected)

    def test_the_token_and_the_csrf_value_are_not_what_was_sent(self):
        first = self.posted({"numero": "F-1", "jeton": "j1", "csrfmiddlewaretoken": "a"})
        again = self.posted({"numero": "F-1", "jeton": "j2", "csrfmiddlewaretoken": "b"})
        self.assertEqual(posted_digest(first), posted_digest(again))

    def test_another_value_or_another_file_is_another_submission(self):
        base = posted_digest(self.posted({"numero": "F-1"}))
        self.assertNotEqual(posted_digest(self.posted({"numero": "F-2"})), base)
        with_file = self.posted({"numero": "F-1"}, [("fichier", SimpleUploadedFile("a.pdf", b"12"))])
        other_file = self.posted({"numero": "F-1"}, [("fichier", SimpleUploadedFile("a.pdf", b"123"))])
        self.assertNotEqual(posted_digest(with_file), base)
        self.assertNotEqual(posted_digest(with_file), posted_digest(other_file))


class DeleteStoredFilesTests(SimpleTestCase):
    def test_each_file_is_deleted_and_one_already_gone_is_passed_over(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = FileSystemStorage(location=folder)
            kept = storage.save("ventes/a.pdf", ContentFile(b"a"))
            gone = storage.save("ventes/b.pdf", ContentFile(b"b"))
            storage.delete(gone)
            delete_stored_files([(storage, gone), (storage, kept)])
            self.assertFalse(storage.exists(kept))

    def test_a_file_held_open_on_windows_is_left(self):
        """Windows refuses to delete a file another program holds: the row is
        gone either way, and a stray file costs nothing."""
        storage = mock.Mock()
        storage.delete.side_effect = [PermissionError("held"), None]
        delete_stored_files([(storage, "ventes/a.pdf"), (storage, "ventes/b.pdf")])
        self.assertEqual([call.args[0] for call in storage.delete.call_args_list], ["ventes/a.pdf", "ventes/b.pdf"])
