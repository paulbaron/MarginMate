"""The one writer of a « facture de vente » and its file (recipes/sale_files.py).

Every sale document with a file enters the database here: « Lire la
facture » (an electronic invoice, read and created at once) and the typed
form (any plain file). Every refusal comes BEFORE the transaction; the file
is saved INSIDE it with its rows; and whatever goes wrong inside, the name
just saved is deleted outside it - Django has no « on rollback ». A file
replaced, removed or deleted with its document goes once the transaction
commits, never before: rolled back, the old file stays with its row.

The file is served by `recipes:sale_document_file` under its download name,
only from `ventes/`, to « Recettes & ventes ».

Every name, SIREN, number, date and amount is invented
(recipes/tests/sale_einvoice_files.py); no real file.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, OperationalError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import paths
from bank.models import BankTransaction
from common import DateRange, file_too_big
from invoices.einvoice import EInvoiceError
from invoices.tests.pdf_files import write_pdf
from margins.computation import margins_for
from recipes.forms import SaleDocumentForm, SaleDocumentLineFormSet, SaleDocumentLineFormSetNew
from recipes.models import SaleDocument, SaleDocumentLine, SaleDocumentPayment, document_to_pay
from recipes.sale_einvoice import read_sale
from recipes.sale_files import (
    ALREADY_A_PURCHASE,
    ALREADY_ADDED,
    BUYER_IS_THE_BAR,
    CREDIT_NOTE_CHECK,
    CREDIT_NOTE_OF,
    DATE_FROM_DELIVERY,
    DATE_UNREADABLE,
    DEPOSIT_SAID,
    DEPOSIT_WORD_SAID,
    EINVOICE_EXTENSION_REFUSED,
    EINVOICE_ON_TYPED_FORM,
    FILE_TAKEN,
    NO_COUNTING,
    NO_EINVOICE_IN_PDF,
    NO_FILE,
    NO_LABEL,
    NUMBER_HELD,
    SALE_BAD_DATE,
    SALE_FILE_ACCEPT,
    SALE_FILE_EXTENSIONS,
    SALE_NO_DATE,
    SELLER_IS_A_SUPPLIER,
    TYPED_DATE_IGNORED,
    DeletedDocument,
    DocumentGone,
    SaleFileRefused,
    delete_document,
    document_from_reading,
    einvoice_on_typed_form,
    read_einvoice_upload,
    save_typed,
    storage_name,
    typed_file_problem,
)
from recipes.sale_payments import read_links
from recipes.tests.sale_einvoice_files import (
    BAR_NAME,
    BAR_SIREN,
    CUSTOMER_NAME,
    CUSTOMER_SIREN,
    SALE_CII,
    SALE_CII_BUYER_IS_THE_BAR,
    SALE_CII_CHARGE_NO_GRAND_TOTAL,
    SALE_CII_CREDIT_NOTE,
    SALE_CII_DELIVERED,
    SALE_CII_DEPOSIT,
    SALE_CII_DEPOSIT_AS_380,
    SALE_CII_DOCTYPE,
    SALE_CII_MINIMUM,
    SALE_CII_NO_DATE,
    SALE_CII_PERIOD,
    SALE_CII_SELLER_IS_A_SUPPLIER,
    SALE_CII_UTF16,
    SALE_CII_YEAR_ONE,
    SALE_NUMBER,
    SALE_UBL,
    SUPPLIER_SIREN,
    factur_x,
)
from tests.factories import (
    make_credit,
    make_invoice,
    make_recipe,
    make_sale_document,
    make_sale_line,
    make_sale_payment,
    make_supplier,
)
from tests.runner import employee_of_the_test_tenant

D = Decimal
COUNTED, TILL, DEPOSIT = (SaleDocument.Counting.COUNTED, SaleDocument.Counting.TILL, SaleDocument.Counting.DEPOSIT)


def as_bytes(xml) -> bytes:
    return xml if isinstance(xml, bytes) else xml.encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pdf_bytes(xml=None) -> bytes:
    """A Factur-X PDF carrying `xml`, or a plain PDF with none."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "facture.pdf")
        if xml is None:
            write_pdf(path, ["Facture de vente", "Total 230,52 EUR"])
        else:
            factur_x(path, xml)
        return Path(path).read_bytes()


def stored_files() -> set[str]:
    """Every file under the test espace's `ventes/`, as stored names."""
    root = Path(paths.media_root())
    folder = root / "ventes"
    if not folder.exists():
        return set()
    return {path.relative_to(root).as_posix() for path in folder.rglob("*") if path.is_file()}


class StoredFilesMixin:
    """What a test leaves under `ventes/` is taken away after it, and
    `new_files()` names what it stored."""

    def setUp(self):
        super().setUp()
        self.files_before = stored_files()
        self.addCleanup(self.remove_new_files)

    def new_files(self) -> set[str]:
        return stored_files() - self.files_before

    def remove_new_files(self):
        root = Path(paths.media_root())
        for name in self.new_files():
            (root / name).unlink(missing_ok=True)


def with_file(document: SaleDocument, name="vente.pdf", content=b"%PDF-1.4 vente exemple") -> SaleDocument:
    document.source_file.save(name, ContentFile(content), save=False)
    document.source_sha256 = sha(content)
    document.save()
    return document


class ReadEInvoiceUploadTests(StoredFilesMixin, TestCase):
    """« Lire la facture », step by step (spec §4.4): nothing written before
    the file is read, checked and dated."""

    def read(self, xml, name="FV-2026-0101.xml", *, typed="", counting=COUNTED):
        return read_einvoice_upload(SimpleUploadedFile(name, as_bytes(xml)), typed_date_text=typed, counting=counting)

    def assertRefused(self, sentence, xml=SALE_CII, name="FV-2026-0101.xml", exception=SaleFileRefused, **kwargs):
        before, files = SaleDocument.objects.count(), stored_files()
        with self.assertRaisesMessage(exception, sentence) as refused:
            self.read(xml, name, **kwargs)
        self.assertEqual(SaleDocument.objects.count(), before)
        self.assertEqual(stored_files(), files)
        return refused.exception

    # -- step 1: the form ------------------------------------------------------------------------------------------

    def test_no_file(self):
        with self.assertRaisesMessage(SaleFileRefused, NO_FILE):
            read_einvoice_upload(None, typed_date_text="", counting=COUNTED)

    def test_an_extension_that_is_no_electronic_invoice(self):
        for name in ("facture.jpg", "facture.docx", "facture", "facture.xml.txt"):
            with self.subTest(name=name):
                self.assertRefused(EINVOICE_EXTENSION_REFUSED, name=name)

    def test_too_big(self):
        upload = SimpleUploadedFile("FV-2026-0101.xml", as_bytes(SALE_CII))
        with mock.patch("common.UPLOAD_MAX_FILE_BYTES", 100):
            sentence = file_too_big(upload)
            self.assertTrue(sentence)
            self.assertRefused(sentence)

    def test_an_unknown_counting(self):
        for counting in ("", "gratuit"):
            with self.subTest(counting=counting):
                self.assertRefused(NO_COUNTING, counting=counting)

    # -- step 2: duplicates by the file -----------------------------------------------------------------------------

    def test_the_same_file_twice(self):
        self.read(SALE_CII)
        self.assertRefused(ALREADY_ADDED.format(document=f"n° {SALE_NUMBER} du 03/09/2026"))

    def test_a_file_already_filed_as_a_purchase(self):
        supplier = make_supplier(name="Grossiste Exemple")
        make_invoice(supplier=supplier, invoice_number="F-12", source_sha256=sha(as_bytes(SALE_CII)))
        self.assertRefused(ALREADY_A_PURCHASE.format(invoice="Grossiste Exemple n° F-12"))

    # -- step 3-4: the XML -------------------------------------------------------------------------------------------

    def test_a_pdf_with_no_electronic_invoice(self):
        self.assertRefused(NO_EINVOICE_IN_PDF.format(name="scan.pdf"), xml=pdf_bytes(), name="scan.pdf")

    def test_an_xml_that_is_no_invoice_is_said_as_einvoice_says_it(self):
        """Never « ne contient pas » : an XML always goes to the reader,
        which says what is wrong with it - the Achats rule."""
        self.assertRefused(
            "Ce fichier XML n'est pas une facture électronique au format EN 16931 (ni CII ni UBL).",
            xml='<?xml version="1.0" encoding="UTF-8"?>\n<menu><plat>Exemple</plat></menu>',
            exception=EInvoiceError,
        )

    def test_utf16_and_doctype_are_said_as_einvoice_says_them(self):
        self.assertRefused("n'est pas encodée en UTF-8", xml=SALE_CII_UTF16, exception=EInvoiceError)
        self.assertRefused("déclare un DOCTYPE ou une entité XML", xml=SALE_CII_DOCTYPE, exception=EInvoiceError)

    # -- step 5: whose invoice it is, and its number ---------------------------------------------------------------

    def test_the_seller_is_one_of_the_bar_s_suppliers(self):
        make_supplier(name="Brasserie du Canal", ticket_identifiers=[f"siren:{SUPPLIER_SIREN}"])
        self.assertRefused(
            SELLER_IS_A_SUPPLIER.format(siren="900 000 019", supplier="Brasserie du Canal"),
            xml=SALE_CII_SELLER_IS_A_SUPPLIER,
        )

    def test_the_buyer_is_the_bar(self):
        make_sale_document(reference="FV-ANCIENNE", seller_siren=BAR_SIREN)
        self.assertRefused(BUYER_IS_THE_BAR.format(siren="800 000 002"), xml=SALE_CII_BUYER_IS_THE_BAR)

    def test_a_number_another_sale_document_holds(self):
        make_sale_document(reference=SALE_NUMBER.lower(), sold_on=date(2026, 3, 5))
        self.assertRefused(NUMBER_HELD.format(number=SALE_NUMBER.lower(), day="05/03/2026"))

    # -- step 6: the date --------------------------------------------------------------------------------------------

    def test_the_delivery_date_first(self):
        outcome = self.read(SALE_CII_DELIVERED)
        self.assertEqual(outcome.document.sold_on, date(2026, 8, 31))
        self.assertEqual(outcome.document.einvoice_issued_on, date(2026, 9, 3))
        self.assertEqual(outcome.document.einvoice_delivered_on, date(2026, 8, 31))
        self.assertEqual(outcome.date_said, DATE_FROM_DELIVERY.format(day="31/08/2026"))

    def test_else_the_start_of_the_billing_period(self):
        outcome = self.read(SALE_CII_PERIOD)
        self.assertEqual(outcome.document.sold_on, date(2026, 8, 15))
        self.assertEqual(outcome.date_said, DATE_FROM_DELIVERY.format(day="15/08/2026"))

    def test_else_the_issue_date_said_nothing_more(self):
        outcome = self.read(SALE_CII)
        self.assertEqual(outcome.document.sold_on, date(2026, 9, 3))
        self.assertEqual(outcome.date_said, "")

    def test_no_date_at_all(self):
        refused = self.assertRefused(SALE_NO_DATE, xml=SALE_CII_NO_DATE)
        self.assertIsNone(refused.typed_date)

    def test_a_date_no_sale_happened_on(self):
        self.assertRefused(SALE_BAD_DATE.format(day="01/01/0001"), xml=SALE_CII_YEAR_ONE)

    def test_the_date_typed_when_the_invoice_gives_none(self):
        outcome = self.read(SALE_CII_NO_DATE, typed="2026-09-01")
        self.assertEqual(outcome.document.sold_on, date(2026, 9, 1))
        self.assertIsNone(outcome.document.einvoice_issued_on)
        outcome = self.read(SALE_CII_YEAR_ONE, typed="2026-09-02")
        self.assertEqual(outcome.document.sold_on, date(2026, 9, 2))
        self.assertEqual(outcome.document.einvoice_issued_on, date(1, 1, 1))

    def test_a_date_typed_that_is_no_date(self):
        """« 2026-02-30 » is not « nothing typed »."""
        refused = self.assertRefused(DATE_UNREADABLE, xml=SALE_CII_NO_DATE, typed="2026-02-30")
        self.assertIsNone(refused.typed_date)

    def test_a_date_typed_outside_2000_today_comes_back_with_its_refusal(self):
        refused = self.assertRefused("Date impossible : entre le 01/01/2000", xml=SALE_CII_NO_DATE, typed="1999-12-31")
        self.assertEqual(refused.typed_date, date(1999, 12, 31))

    def test_a_date_typed_beside_the_invoice_s_own_is_ignored_and_said(self):
        outcome = self.read(SALE_CII, typed="2026-09-01")
        self.assertEqual(outcome.document.sold_on, date(2026, 9, 3))
        self.assertEqual(outcome.date_said, TYPED_DATE_IGNORED.format(day="03/09/2026"))

    # -- the counting ------------------------------------------------------------------------------------------------

    def test_the_card_s_counting(self):
        outcome = self.read(SALE_CII, counting=TILL)
        self.assertEqual(outcome.document.counting, TILL)
        self.assertEqual(outcome.said, [])

    def test_a_deposit_invoice_is_a_deposit_whatever_the_card_said(self):
        outcome = self.read(SALE_CII_DEPOSIT, counting=COUNTED)
        self.assertEqual(outcome.document.counting, DEPOSIT)
        self.assertEqual(outcome.said, [("info", DEPOSIT_SAID)])

    def test_a_credit_note_counts_like_the_invoice_it_corrects(self):
        make_sale_document(reference=SALE_NUMBER.lower(), counting=TILL, sold_on=date(2026, 9, 3))
        outcome = self.read(SALE_CII_CREDIT_NOTE, "AV-2026-0011.xml", counting=COUNTED)
        self.assertEqual(outcome.document.counting, TILL)
        self.assertEqual(
            outcome.said,
            [("info", CREDIT_NOTE_OF.format(number=SALE_NUMBER.lower(), counting="Déjà comptée par la caisse"))],
        )
        self.assertEqual(outcome.document.einvoice_preceding_number, SALE_NUMBER)
        self.assertEqual(outcome.document.total_ttc, D("-230.52"))

    def test_a_credit_note_of_an_invoice_not_held_here_is_said(self):
        outcome = self.read(SALE_CII_CREDIT_NOTE, "AV-2026-0011.xml", counting=COUNTED)
        self.assertEqual(outcome.document.counting, COUNTED)
        self.assertEqual(outcome.said, [("warning", CREDIT_NOTE_CHECK)])

    def test_the_word_acompte_on_an_ordinary_invoice_is_said_never_decided(self):
        outcome = self.read(SALE_CII_DEPOSIT_AS_380, "FV-2026-0102.xml", counting=COUNTED)
        self.assertEqual(outcome.document.counting, COUNTED)
        self.assertEqual(outcome.said, [("warning", DEPOSIT_WORD_SAID)])

    # -- step 9: stored ----------------------------------------------------------------------------------------------

    def test_created_with_its_lines_and_its_file(self):
        data = as_bytes(SALE_CII)
        outcome = self.read(SALE_CII)
        document = SaleDocument.objects.get(pk=outcome.document.pk)
        self.assertEqual(
            (document.reference, document.customer, document.customer_identifier, document.counting),
            (SALE_NUMBER, CUSTOMER_NAME, CUSTOMER_SIREN, COUNTED),
        )
        self.assertEqual((document.stated_total_ttc, document.stated_total_ht), (D("230.52"), D("194.20")))
        self.assertEqual((document.einvoice_format, document.einvoice_type_code), ("CII", "380"))
        self.assertEqual((document.seller_name, document.seller_siren), (BAR_NAME, BAR_SIREN))
        self.assertTrue(document.einvoice_checks)
        self.assertTrue(all(check["passed"] for check in document.einvoice_checks))
        self.assertFalse(outcome.failed_checks)
        self.assertEqual(
            [
                (
                    line.label,
                    line.quantity,
                    line.unit_price_ht,
                    line.total_ht,
                    line.vat_rate,
                    line.rebuilt,
                    line.is_tied,
                )
                for line in document.lines.order_by("id")
            ],
            [
                ("Formule cocktail", D("2"), D("84.50"), D("169.00"), D("0.20"), False, False),
                ("Planche apéritive", D("6"), D("4.20"), D("25.20"), D("0.10"), False, False),
            ],
        )
        # Under the month it was stored in (FileField's upload_to), its own name kept.
        self.assertRegex(document.source_file.name, r"^ventes/\d{4}/\d{2}/FV-2026-0101(_\w+)?\.xml$")
        self.assertEqual(document.source_sha256, sha(data))
        self.assertEqual(document.source_file.open("rb").read(), data)
        document.source_file.close()
        self.assertEqual(self.new_files(), {document.source_file.name})

    def test_a_factur_x_pdf_and_a_ubl(self):
        outcome = self.read(pdf_bytes(SALE_CII), "facture.pdf")
        self.assertEqual(outcome.document.einvoice_format, "Factur-X")
        self.assertTrue(outcome.document.source_file.name.endswith(".pdf"))
        SaleDocument.objects.all().delete()
        outcome = self.read(SALE_UBL, "facture-ubl.xml")
        self.assertEqual(outcome.document.einvoice_format, "UBL")
        self.assertEqual(outcome.document.lines.count(), 2)

    def test_an_invoice_with_no_lines_is_one_rebuilt_line_per_rate(self):
        outcome = self.read(SALE_CII_MINIMUM, "FV-2026-0103.xml")
        lines = list(outcome.document.lines.order_by("id"))
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.rebuilt and not line.is_tied for line in lines))

    def test_the_proposals_and_the_failed_checks_are_counted(self):
        make_recipe(name="Formule cocktail", selling_price_ttc="9.00")
        outcome = self.read(SALE_CII)
        self.assertEqual(outcome.proposals, 1)
        self.assertEqual(SaleDocumentLine.objects.filter(recipe__isnull=False).count(), 0)

    def test_the_temporary_file_is_gone_in_every_outcome(self):
        made = []
        real = tempfile.mkstemp

        def recording(*args, **kwargs):
            handle, path = real(*args, **kwargs)
            made.append(path)
            return handle, path

        with mock.patch("recipes.sale_files.tempfile.mkstemp", side_effect=recording):
            self.read(SALE_CII)
            for xml, name in ((SALE_CII, "FV-2026-0101.xml"), (SALE_CII_DOCTYPE, "x.xml"), (pdf_bytes(), "s.pdf")):
                with self.subTest(name=name), self.assertRaises((SaleFileRefused, EInvoiceError)):
                    self.read(xml, name)
        self.assertEqual(len(made), 4)
        self.assertTrue(all(path.endswith((".xml", ".pdf")) for path in made))
        self.assertEqual([path for path in made if os.path.exists(path)], [])


class NoGrandTotalTests(StoredFilesMixin, TestCase):
    """An electronic invoice whose sender states no grand total (BT-112)
    but a document-level charge: its total is its lines' plus its charge at
    its own rate - one figure for the tab, the bank and « Marges » (the rule
    `models.fallback_total_ttc`), never its lines alone."""

    def test_the_tab_the_bank_and_the_margins_read_one_total(self):
        outcome = read_einvoice_upload(
            SimpleUploadedFile("FV-2026-0115.xml", as_bytes(SALE_CII_CHARGE_NO_GRAND_TOTAL)),
            typed_date_text="",
            counting=COUNTED,
        )
        document = SaleDocument.objects.get(pk=outcome.document.pk)
        self.assertIsNone(document.stated_total_ttc)
        self.assertEqual(document.adjustment_ht, Decimal("12.00"))
        make_sale_payment(document, make_credit("244.92", document.sold_on))

        self.assertEqual(document.total_ttc, Decimal("244.92"))
        self.assertEqual(document.to_pay, Decimal("244.92"))
        self.assertEqual([fact.to_pay for fact in read_links().facts], [Decimal("244.92")])
        report = margins_for(DateRange(document.sold_on, document.sold_on))
        self.assertEqual(report.revenue_documents.ttc, Decimal("244.92"))

    def test_document_to_pay_counts_the_adjustment_at_its_rate(self):
        line = SaleDocumentLine(
            label="Formule", quantity=Decimal("1"), total_ht=Decimal("100.00"), vat_rate=Decimal("0.10")
        )
        self.assertEqual(
            document_to_pay(
                None, None, None, [line], adjustment_ht=Decimal("20.00"), adjustment_vat_rate=Decimal("0.20")
            ),
            Decimal("134.00"),
        )
        self.assertEqual(document_to_pay(None, None, None, [line]), Decimal("110.00"))


class DocumentFromReadingTests(TestCase):
    def test_a_line_stating_no_label_is_named(self):
        """BT-153 is mandatory, but an empty one read as "" would break the
        rule that a line tied to nothing says what it is."""
        reading = read_sale(
            as_bytes(SALE_CII.replace("<ram:Name>Formule cocktail</ram:Name>", "<ram:Name></ram:Name>"))
        )
        _document, lines, _said = document_from_reading(reading, kind="CII", sold_on=date(2026, 9, 3), counting=COUNTED)
        self.assertEqual([line.label for line in lines], [NO_LABEL.format(number=1), "Planche apéritive"])

    def test_a_type_code_wider_than_its_column_is_stored_cut(self):
        """BT-3 is three digits; a sender writing more had it stored whole -
        SQLite says nothing - and « Données » then refused the document for
        good (« plus de 10 caractères »). Cut to the column, after the kind
        was read off the whole code."""
        reading = read_sale(
            as_bytes(
                SALE_CII.replace("<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>380EXEMPLE12</ram:TypeCode>", 1)
            )
        )
        document, _lines, _said = document_from_reading(reading, kind="CII", sold_on=date(2026, 9, 3), counting=COUNTED)
        self.assertEqual(document.einvoice_type_code, "380EXEMPLE")


class TypedFormFileTests(StoredFilesMixin, TestCase):
    """The typed form takes any plain file - and refuses an electronic
    invoice, whose figures would otherwise be typed again by hand: the loss
    receipts.py forbids. An XML einvoice refuses to read is refused there
    too, with einvoice's own sentence."""

    def staged(self, name, data) -> str:
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, name)
        Path(path).write_bytes(data)
        return path

    def test_an_electronic_invoice_is_refused(self):
        for name, data in (
            ("vente.xml", as_bytes(SALE_CII)),
            ("ubl.xml", as_bytes(SALE_UBL)),
            ("vente.pdf", pdf_bytes(SALE_CII)),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    einvoice_on_typed_form(self.staged(name, data), name), EINVOICE_ON_TYPED_FORM.format(name=name)
                )

    def test_an_xml_einvoice_refuses_is_refused_in_its_words(self):
        for name, data, words in (
            ("utf16.xml", SALE_CII_UTF16, "n'est pas encodée en UTF-8"),
            ("doctype.xml", as_bytes(SALE_CII_DOCTYPE), "déclare un DOCTYPE"),
            ("casse.xml", b"<pas du XML", "n'est pas un fichier XML lisible"),
        ):
            with self.subTest(name=name):
                self.assertIn(words, einvoice_on_typed_form(self.staged(name, data), name))

    def test_an_oversized_xml_is_refused(self):
        with mock.patch("invoices.einvoice.MAX_XML_BYTES", 100):
            said = einvoice_on_typed_form(self.staged("grand.xml", as_bytes(SALE_CII)), "grand.xml")
        self.assertEqual(said, "Ce fichier XML est trop volumineux pour être lu comme une facture électronique.")

    def test_another_xml_a_plain_pdf_and_a_photo_are_plain_files(self):
        for name, data in (
            ("menu.xml", b'<?xml version="1.0" encoding="UTF-8"?>\n<menu/>'),
            ("scan.pdf", pdf_bytes()),
            ("photo.jpg", b"\xff\xd8\xff photo"),
        ):
            with self.subTest(name=name):
                self.assertEqual(einvoice_on_typed_form(self.staged(name, data), name), "")

    def test_the_same_file_as_another_document_or_a_purchase(self):
        data = b"%PDF-1.4 facture de vente"
        path = self.staged("vente.pdf", data)
        own = with_file(make_sale_document(reference="FV-OWN"), content=data)
        self.assertEqual(typed_file_problem(path, "vente.pdf", own), "")
        self.assertEqual(
            typed_file_problem(path, "vente.pdf", SaleDocument()), FILE_TAKEN.format(document="n° FV-OWN du 05/03/2026")
        )
        own.delete()
        purchase = make_invoice(supplier=make_supplier(name="Grossiste Exemple"), source_sha256=sha(data))
        type(purchase).objects.filter(pk=purchase.pk).update(invoice_number="")
        self.assertEqual(
            typed_file_problem(path, "vente.pdf", SaleDocument()),
            ALREADY_A_PURCHASE.format(invoice="Grossiste Exemple, sans numéro"),
        )

    def test_a_stem_that_cleans_to_nothing_is_facture(self):
        self.assertEqual(storage_name("€.pdf"), "facture.pdf")
        self.assertEqual(storage_name("€€€.JPG"), "facture.jpg")
        self.assertEqual(storage_name("Facture Mariage 12.PDF"), "Facture_Mariage_12.pdf")
        self.assertEqual(storage_name("C:\\Users\\exemple\\scan.pdf"), "scan.pdf")

    def test_the_extensions_and_what_the_input_offers(self):
        self.assertIn(".heic", SALE_FILE_EXTENSIONS)
        self.assertNotIn(".heic", SALE_FILE_ACCEPT.split(","))
        self.assertEqual(SALE_FILE_ACCEPT.split(","), [ext for ext in SALE_FILE_EXTENSIONS if ext != ".heic"])


def typed_data(lines=(), *, initial=0, **header) -> dict:
    """What the typed form posts: its header (blank but the date and
    « Compte ») and `lines`, each a dict of its fields."""
    data = {
        "sold_on": "2026-03-05",
        "reference": "",
        "customer": "",
        "stated_total_ttc": "",
        "stated_total_ht": "",
        "prepaid_ttc": "",
        "counting": "counted",
        "note": "",
        "lines-TOTAL_FORMS": str(len(lines)),
        "lines-INITIAL_FORMS": str(initial),
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "500",
    }
    data.update(header)
    for index, line in enumerate(lines):
        for key, value in line.items():
            data[f"lines-{index}-{key}"] = str(value)
    return data


class WriterTests(StoredFilesMixin, TestCase):
    """`save_typed` and step 9 of the read: whatever fails inside the
    transaction, the name just saved goes; a file replaced or removed goes
    once the transaction commits, never before."""

    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def staged(self, name, data) -> str:
        path = os.path.join(self.folder, name)
        Path(path).write_bytes(data)
        return path

    def forms(self, document, data):
        form = SaleDocumentForm(data, instance=document)
        formset = SaleDocumentLineFormSet(data, instance=document)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(formset.is_valid(), formset.errors)
        return form, formset

    def a_document_with_a_file(self):
        document = with_file(
            make_sale_document(reference="FV-1", stated_total_ttc="50.00"), "ancien.pdf", b"%PDF ancien"
        )
        return document, document.source_file.name

    def test_any_exception_while_reading_deletes_the_name_just_saved(self):
        failure = IntegrityError("CHECK constraint failed: saledocumentline_untied_has_a_label")
        with (
            mock.patch.object(SaleDocumentLine.objects, "bulk_create", side_effect=failure),
            self.assertRaises(IntegrityError),
        ):
            read_einvoice_upload(
                SimpleUploadedFile("FV-2026-0101.xml", as_bytes(SALE_CII)), typed_date_text="", counting=COUNTED
            )
        self.assertEqual(SaleDocument.objects.count(), 0)
        self.assertEqual(self.new_files(), set())

    def test_two_reads_of_one_file_in_flight_are_one_document(self):
        """The partial unique constraint stops the second: its file goes and
        it is told « Déjà ajoutée »."""
        data = as_bytes(SALE_CII)

        def read_while_another_stores_it(xml):
            make_sale_document(reference="", sold_on=date(2026, 9, 3), source_sha256=sha(data))
            return read_sale(xml)

        with (
            mock.patch("recipes.sale_files.read_sale", side_effect=read_while_another_stores_it),
            self.assertRaisesMessage(SaleFileRefused, ALREADY_ADDED.format(document="sans numéro du 03/09/2026")),
        ):
            read_einvoice_upload(SimpleUploadedFile("FV-2026-0101.xml", data), typed_date_text="", counting=COUNTED)
        self.assertEqual(SaleDocument.objects.filter(source_sha256=sha(data)).count(), 1)
        self.assertEqual(self.new_files(), set())

    def test_any_exception_while_saving_typed_deletes_the_name_just_saved(self):
        document = make_sale_document(reference="FV-2", stated_total_ttc="50.00")
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-2"))
        with (
            mock.patch.object(formset, "save", side_effect=OperationalError("database is locked")),
            self.assertRaises(OperationalError),
        ):
            save_typed(form, formset, staged_path=self.staged("b.pdf", b"%PDF nouveau"), upload_name="b.pdf")
        self.assertEqual(self.new_files(), set())
        document.refresh_from_db()
        self.assertFalse(document.source_file)

    def test_the_same_file_saved_twice_in_flight_is_refused(self):
        data = b"%PDF un seul fichier"
        holder = make_sale_document(reference="FV-3", sold_on=date(2026, 3, 6))
        document = make_sale_document(reference="FV-4", stated_total_ttc="50.00")
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-4"))
        SaleDocument.objects.filter(pk=holder.pk).update(source_sha256=sha(data))
        with self.assertRaisesMessage(SaleFileRefused, FILE_TAKEN.format(document="n° FV-3 du 06/03/2026")):
            save_typed(form, formset, staged_path=self.staged("c.pdf", data), upload_name="c.pdf")
        self.assertEqual(self.new_files(), set())

    def test_a_file_replaced_goes_once_the_transaction_commits(self):
        document, old = self.a_document_with_a_file()
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
        data = b"%PDF-1.4 nouveau"
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            outcome = save_typed(form, formset, staged_path=self.staged("nouveau.pdf", data), upload_name="nouveau.pdf")
        document.refresh_from_db()
        self.assertNotEqual(document.source_file.name, old)
        self.assertEqual(document.source_sha256, sha(data))
        self.assertIn(old, stored_files())
        for callback in callbacks:
            callback()
        self.assertNotIn(old, stored_files())
        self.assertIn(document.source_file.name, stored_files())
        self.assertEqual(outcome.document.pk, document.pk)

    def test_a_file_attached_again_under_its_old_name_stays(self):
        """The row still names a file gone from the disk (a restore, a hand
        deletion): the same file attached again in the same month is saved
        under exactly that name - and the old name's deletion must not take
        the new file with it."""
        document = make_sale_document(reference="FV-1", stated_total_ttc="50.00")
        name = f"{timezone.localdate():ventes/%Y/%m}/x.pdf"
        SaleDocument.objects.filter(pk=document.pk).update(source_file=name, source_sha256=sha(b"%PDF parti"))
        document.refresh_from_db()
        self.assertNotIn(name, stored_files())
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
        with self.captureOnCommitCallbacks(execute=True):
            save_typed(form, formset, staged_path=self.staged("x.pdf", b"%PDF de retour"), upload_name="x.pdf")
        document.refresh_from_db()
        self.assertEqual(document.source_file.name, name)
        self.assertIn(name, stored_files())

    def test_a_save_after_another_tab_deleted_it_is_refused(self):
        """Loaded, then deleted in another tab before this save: neither the
        document nor its lines come back, and the file just staged goes."""
        document = make_sale_document(reference="FV-1", stated_total_ttc="50.00")
        make_sale_line(document, label="Location de salle", unit_price_ttc="50.00")
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
        SaleDocument.objects.filter(pk=document.pk).delete()
        with self.assertRaises(DocumentGone):
            save_typed(form, formset, staged_path=self.staged("n.pdf", b"%PDF nouveau"), upload_name="n.pdf")
        self.assertFalse(SaleDocument.objects.exists())
        self.assertFalse(SaleDocumentLine.objects.exists())
        self.assertEqual(self.new_files(), set())

    def test_a_header_only_save_keeps_the_file_another_tab_put(self):
        document, _old = self.a_document_with_a_file()
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
        other_tab = with_file(SaleDocument.objects.get(pk=document.pk), "autre.pdf", b"%PDF autre onglet")
        put = other_tab.source_file.name
        with self.captureOnCommitCallbacks(execute=True):
            save_typed(form, formset)
        document.refresh_from_db()
        self.assertEqual(document.source_file.name, put)
        self.assertEqual(document.source_sha256, sha(b"%PDF autre onglet"))
        self.assertIn(put, stored_files())

    def test_a_file_removed_goes_once_the_transaction_commits(self):
        document, old = self.a_document_with_a_file()
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            save_typed(form, formset, remove_file=True)
        document.refresh_from_db()
        self.assertFalse(document.source_file)
        self.assertEqual(document.source_sha256, "")
        self.assertIn(old, stored_files())
        for callback in callbacks:
            callback()
        self.assertNotIn(old, stored_files())

    def test_a_save_rolled_back_keeps_the_old_file(self):
        document, old = self.a_document_with_a_file()
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch.object(formset, "save", side_effect=OperationalError("database is locked")),
            self.assertRaises(OperationalError),
        ):
            save_typed(
                form, formset, staged_path=self.staged("nouveau.pdf", b"%PDF nouveau"), upload_name="nouveau.pdf"
            )
        document.refresh_from_db()
        self.assertEqual(document.source_file.name, old)
        self.assertEqual(self.new_files(), {old})

    def test_a_save_rolled_back_leaves_the_page_s_document_naming_its_old_file(self):
        """The page is drawn again from the very instance the save worked on:
        left naming the name just deleted (or no file at all), it offered
        « Voir » on a file that is gone - or hid the one still there."""
        for remove, staged in ((False, "nouveau.pdf"), (True, None)):
            with self.subTest(remove=remove):
                document, old = self.a_document_with_a_file()
                sha_before = document.source_sha256
                form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
                with (
                    mock.patch.object(formset, "save", side_effect=OperationalError("database is locked")),
                    self.assertRaises(OperationalError),
                ):
                    save_typed(
                        form,
                        formset,
                        staged_path=self.staged(staged, b"%PDF nouveau") if staged else None,
                        upload_name=staged or "",
                        remove_file=remove,
                    )
                drawn = (form.instance.source_file.name, form.instance.source_sha256)
                SaleDocument.objects.filter(pk=document.pk).delete()
                self.assertEqual(drawn, (old, sha_before))

    def test_a_new_file_wins_over_retirer_ticked_with_it(self):
        document, old = self.a_document_with_a_file()
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-1"))
        data = b"%PDF-1.4 gagnant"
        with self.captureOnCommitCallbacks(execute=True):
            save_typed(
                form, formset, staged_path=self.staged("gagnant.pdf", data), upload_name="gagnant.pdf", remove_file=True
            )
        document.refresh_from_db()
        self.assertTrue(document.source_file.name.endswith(".pdf"))
        self.assertEqual(document.source_sha256, sha(data))
        self.assertNotIn(old, stored_files())

    def test_a_stem_that_cleans_to_nothing_is_saved_as_facture(self):
        document = make_sale_document(reference="FV-5", stated_total_ttc="50.00")
        form, formset = self.forms(document, typed_data(stated_total_ttc="50.00", reference="FV-5"))
        save_typed(form, formset, staged_path=self.staged("piece.pdf", b"%PDF euro"), upload_name="€.pdf")
        document.refresh_from_db()
        self.assertRegex(document.source_file.name, r"^ventes/\d{4}/\d{2}/facture(_\w+)?\.pdf$")

    def test_a_blank_recipe_price_is_written_on_every_line(self):
        """A typed recipe line's price is the menu's of the day, written when
        the document is saved - every line of it, the forms left unchanged
        included: raising a cocktail's price must never move an old
        invoice's total."""
        mule = make_recipe(name="Mule exemple", selling_price_ttc="8.50")
        preparation = make_recipe(name="Sirop exemple", selling_price_ttc=None)
        document = make_sale_document(reference="FV-6")
        first = make_sale_line(document, recipe=mule, quantity="2")
        second = make_sale_line(document, recipe=mule, quantity="1")
        kept = make_sale_line(document, recipe=preparation, quantity="1")
        free = make_sale_line(document, label="Location de salle", unit_price_ttc="100.00")
        rows = [
            {"id": first.pk, "source": f"recipe:{mule.pk}", "quantity": "2", "label": ""},
            {"id": second.pk, "source": f"recipe:{mule.pk}", "quantity": "3", "label": ""},
            {"id": kept.pk, "source": f"recipe:{preparation.pk}", "quantity": "1", "label": ""},
            {"id": free.pk, "source": "", "quantity": "1", "label": "Location de salle", "unit_price_ttc": "100"},
        ]
        form, formset = self.forms(document, typed_data(rows, initial=4, reference="FV-6"))
        outcome = save_typed(form, formset)
        self.assertEqual(outcome.prices_written, 2)
        prices = dict(document.lines.values_list("pk", "unit_price_ttc"))
        self.assertEqual(prices, {first.pk: D("8.50"), second.pk: D("8.50"), kept.pk: None, free.pk: D("100.00")})
        self.assertEqual(SaleDocumentLine.objects.get(pk=second.pk).quantity, D("3"))
        mule.selling_price_ttc = D("12.00")
        mule.save()
        # 2 + 3 at 8,50 € and the room: 142,50 € - not 160,00 € at the new price.
        self.assertEqual(SaleDocument.objects.get(pk=document.pk).total_ttc, D("142.50"))


class DeletionTests(StoredFilesMixin, TestCase):
    def test_the_file_goes_once_the_deletion_commits(self):
        document = with_file(make_sale_document(reference="FV-7"))
        name = document.source_file.name
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            deleted = delete_document(document)
        self.assertEqual(deleted, DeletedDocument(label="Vente du 05/03/2026 (n° FV-7)", had_file=True, links=0))
        self.assertIn(name, stored_files())
        for callback in callbacks:
            callback()
        self.assertNotIn(name, stored_files())

    def test_its_lines_and_bank_links_go_the_credit_stays(self):
        document = make_sale_document(reference="FV-8")
        make_sale_line(document, label="Location de salle", unit_price_ttc="100.00")
        make_sale_line(document, label="Service", unit_price_ttc="20.00")
        credit = BankTransaction.objects.create(
            operation_date=date(2026, 3, 9),
            label="VIR SEPA EXEMPLE",
            amount=D("120.00"),
            fingerprint="vente-suppression-1",
            settled_by_hand=True,
        )
        SaleDocumentPayment.objects.create(document=document, transaction=credit, method="MANUAL")
        deleted = delete_document(document)
        self.assertEqual(deleted, DeletedDocument(label="Vente du 05/03/2026 (n° FV-8)", had_file=False, links=1))
        self.assertFalse(SaleDocumentLine.objects.exists())
        self.assertFalse(SaleDocumentPayment.objects.exists())
        credit.refresh_from_db()
        self.assertTrue(credit.settled_by_hand)

    def test_a_document_already_gone_answers_none(self):
        document = make_sale_document(reference="FV-9")
        SaleDocument.objects.filter(pk=document.pk).delete()
        self.assertIsNone(delete_document(document))

    def test_the_file_the_row_names_now_is_the_one_deleted(self):
        """Loaded, then given another file in another tab before this
        deletion: the file deleted is the one the row names - never the one
        the stale instance named, leaving the new one on disk named by no
        row."""
        document = with_file(make_sale_document(reference="FV-10"), "premier.pdf", b"%PDF premier")
        other_tab = with_file(SaleDocument.objects.get(pk=document.pk), "second.pdf", b"%PDF second")
        second = other_tab.source_file.name
        with self.captureOnCommitCallbacks(execute=True):
            deleted = delete_document(document)
        self.assertTrue(deleted.had_file)
        self.assertNotIn(second, stored_files())


class FileViewTests(StoredFilesMixin, TestCase):
    """`recipes:sale_document_file`: the file under its download name, a PDF
    or a photo inline (the document's page frames it), anything else a
    sandboxed download - and only a file under `ventes/`."""

    def setUp(self):
        super().setUp()
        self.document = make_sale_document(
            reference="FV-10", customer="Exemple Événements SARL", stated_total_ttc="1500.00"
        )
        with_file(self.document, "stocke-sous-un-autre-nom.pdf", b"%PDF-1.4 vente exemple")
        self.url = reverse("recipes:sale_document_file", args=[self.document.pk])

    def fetch(self, url, **params):
        response = self.client.get(url, params)
        body = b"".join(response.streaming_content) if getattr(response, "streaming", False) else response.content
        response.close()
        return response, body

    def test_a_pdf_is_shown_under_its_download_name(self):
        response, body = self.fetch(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"%PDF-1.4 vente exemple")
        self.assertEqual(response["Content-Type"], "application/pdf")
        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith("inline"), disposition)
        self.assertIn(quote("Vente Exemple Événements SARL 1500€00 05_03_2026.pdf"), disposition)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])

    def test_telecharger_saves_it(self):
        response, _body = self.fetch(self.url, telecharger="1")
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response["Content-Security-Policy"], "sandbox")

    def test_an_xml_is_a_sandboxed_download(self):
        document = make_sale_document(reference="FV-11", customer="Mariage Exemple", stated_total_ttc="10.00")
        with_file(document, "fichier.xml", b"<menu/>")
        response, _body = self.fetch(reverse("recipes:sale_document_file", args=[document.pk]))
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertIn(quote("Vente Mariage Exemple 10€00 05_03_2026.xml"), response["Content-Disposition"])
        self.assertEqual(response["Content-Security-Policy"], "sandbox")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])

    def test_no_file_is_a_404(self):
        bare = make_sale_document(reference="FV-12")
        self.assertEqual(self.client.get(reverse("recipes:sale_document_file", args=[bare.pk])).status_code, 404)

    def test_a_file_outside_ventes_is_a_404(self):
        """A row naming another folder - an older archive, a hand edit - would
        give a purchase's PDF to « Recettes & ventes » through this route."""
        purchase = Path(paths.media_root()) / "invoices" / "2026" / "03" / "achat-exemple.pdf"
        purchase.parent.mkdir(parents=True, exist_ok=True)
        purchase.write_bytes(b"%PDF-1.4 achat")
        self.addCleanup(purchase.unlink, missing_ok=True)
        SaleDocument.objects.filter(pk=self.document.pk).update(source_file="invoices/2026/03/achat-exemple.pdf")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_name_climbing_out_is_a_404(self):
        SaleDocument.objects.filter(pk=self.document.pk).update(source_file="ventes/../../accounts.sqlite3")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_name_going_through_ventes_into_another_folder_is_a_404(self):
        """« ventes/../invoices/… » starts with « ventes/ » and stays inside
        the media folder, so `open_stored` alone opens it: the folder is
        judged on the name resolved, as `accounts.access.areas_of_file`
        judges /fichiers/ - with either separator."""
        purchase = Path(paths.media_root()) / "invoices" / "2026" / "03" / "achat-exemple.pdf"
        purchase.parent.mkdir(parents=True, exist_ok=True)
        purchase.write_bytes(b"%PDF-1.4 achat")
        self.addCleanup(purchase.unlink, missing_ok=True)
        for name in (
            "ventes/../invoices/2026/03/achat-exemple.pdf",
            "ventes\\..\\invoices\\2026\\03\\achat-exemple.pdf",
        ):
            with self.subTest(name=name):
                SaleDocument.objects.filter(pk=self.document.pk).update(source_file=name)
                self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_file_gone_from_the_disk_is_a_404(self):
        self.document.source_file.storage.delete(self.document.source_file.name)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_post_is_refused(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)


class AccessTests(StoredFilesMixin, TestCase):
    """A sale document's file opens to « Recettes & ventes », through its
    route and through /fichiers/ - never to « Banque » or « Consignes »
    alone."""

    def setUp(self):
        super().setUp()
        self.document = with_file(make_sale_document(reference="FV-13"))
        self.urls = (
            reverse("recipes:sale_document_file", args=[self.document.pk]),
            reverse("accounts:media", args=[self.document.source_file.name]),
        )

    def status(self, url) -> int:
        response = self.client.get(url)
        if getattr(response, "streaming", False):
            b"".join(response.streaming_content)
        response.close()
        return response.status_code

    def test_recettes_et_ventes_opens_it(self):
        self.client.force_login(employee_of_the_test_tenant("ventes@example.invalid", ["recipes"]))
        for url in self.urls:
            with self.subTest(url=url):
                self.assertEqual(self.status(url), 200)

    def test_banque_or_consignes_alone_does_not(self):
        for number, area in enumerate(("bank", "returnables")):
            self.client.force_login(employee_of_the_test_tenant(f"autre-{number}@example.invalid", [area]))
            for url in self.urls:
                with self.subTest(area=area, url=url):
                    self.assertEqual(self.status(url), 403)


class BankPassTests(StoredFilesMixin, TestCase):
    """Spec §4.4 step 10: once the document is committed, the automatic bank
    pass (bank.sale_reconcile.reconcile_sales) - its links said, its failure
    too, the document kept: never a 500 after a commit."""

    PAYER = "EXEMPLE EVENEMENTS SARL"

    def credit(self, amount, day):
        return BankTransaction.objects.create(
            operation_date=day,
            bank_type="VIREMENT",
            kind=BankTransaction.Kind.TRANSFER,
            label=f"VIR SEPA RECU /FRM {self.PAYER} /REF {amount}",
            counterparty=self.PAYER,
            amount=D(amount),
            fingerprint=f"passe-vente-{amount}",
        )

    def read(self):
        return read_einvoice_upload(
            SimpleUploadedFile("FV-2026-0101.xml", as_bytes(SALE_CII)), typed_date_text="", counting=COUNTED
        )

    def test_a_read_invoice_is_linked_to_the_credit_that_paid_it(self):
        credit = self.credit("230.52", date(2026, 9, 10))
        outcome = self.read()
        self.assertEqual((outcome.linked, outcome.bank_failed), ([credit], False))
        payment = SaleDocumentPayment.objects.get()
        self.assertEqual((payment.document_id, payment.method), (outcome.document.pk, "AUTO"))

    def test_the_pass_runs_after_the_commit(self):
        seen = []

        def reconcile_sales():
            seen.append(SaleDocument.objects.filter(reference=SALE_NUMBER).exists())
            return []

        with mock.patch("bank.sale_reconcile.reconcile_sales", side_effect=reconcile_sales):
            outcome = self.read()
        self.assertEqual(seen, [True])
        self.assertEqual((outcome.linked, outcome.bank_failed), ([], False))

    def test_a_pass_that_fails_is_said_and_the_document_kept(self):
        with (
            mock.patch("bank.sale_reconcile.reconcile_sales", side_effect=OperationalError("database is locked")),
            self.assertLogs("recipes.sale_files", "ERROR"),
        ):
            outcome = self.read()
        self.assertEqual((outcome.linked, outcome.bank_failed), ([], True))
        document = SaleDocument.objects.get()
        self.assertEqual(self.new_files(), {document.source_file.name})

    def typed(self, **header):
        document = SaleDocument()
        data = typed_data(customer="Exemple Événements SARL", **header)
        form = SaleDocumentForm(data, instance=document)
        formset = SaleDocumentLineFormSetNew(data, instance=document)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(formset.is_valid(), formset.errors)
        return form, formset

    def test_a_typed_document_with_something_to_receive_runs_it(self):
        credit = self.credit("120.00", date(2026, 3, 9))
        outcome = save_typed(*self.typed(stated_total_ttc="120.00"))
        self.assertEqual((outcome.linked, outcome.bank_failed), ([credit], False))

    def test_a_typed_document_with_nothing_to_receive_does_not(self):
        with mock.patch("bank.sale_reconcile.reconcile_sales") as reconcile_sales:
            outcome = save_typed(*self.typed(stated_total_ttc="-20.00"))
        reconcile_sales.assert_not_called()
        self.assertEqual((outcome.linked, outcome.bank_failed), ([], False))

    def test_a_typed_document_s_pass_that_fails(self):
        with (
            mock.patch("bank.sale_reconcile.reconcile_sales", side_effect=OperationalError("database is locked")),
            self.assertLogs("recipes.sale_files", "ERROR"),
        ):
            outcome = save_typed(*self.typed(stated_total_ttc="120.00"))
        self.assertTrue(outcome.bank_failed)
        self.assertTrue(SaleDocument.objects.filter(pk=outcome.document.pk).exists())
