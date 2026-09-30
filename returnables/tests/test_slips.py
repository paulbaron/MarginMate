"""The writer (returnables/slips.py): what becomes of a document handed to
`store_slip`, an upload of several, and « Relire ».

Every PDF is a few hundred bytes built from the invented tickets of
returnables/tests/texts.py; every number, date and name is invented. A slow
motif is simulated (a stand-in pattern raising TimeoutError), never run.
"""

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.files.storage import default_storage
from django.db import IntegrityError
from django.utils import timezone

from accounts import paths
from invoices.models import Invoice
from returnables import patterns, reading, slips
from returnables.models import Slip, SlipLine
from returnables.reading import SlipError
from returnables.slips import (
    CREATED,
    DUPLICATE,
    IGNORED,
    REFUSED,
    RESEND,
    UNEXPECTED,
    file_name,
    reread,
    reread_format,
    store_slip,
    store_uploads,
)
from returnables.tests import texts
from returnables.tests.support import make_format, make_supplier, no_defaults, seeded_format, tiny_pdf
from returnables.tests.test_patterns import SlowPattern
from tests.support import NoNetworkTestCase

UPLOAD = Slip.Origin.UPLOAD
MAIL = Slip.Origin.MAIL


def consignes_files() -> list:
    """Every file under the espace's consignes/ folder."""
    root = Path(paths.media_root()) / "consignes"
    if not root.exists():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def pdf_of(text: str) -> bytes:
    return tiny_pdf(texts.pdf_lines(text))


def stored_lines(slip) -> list:
    return [(line.designation, line.quantity, line.unit_amount, line.amount) for line in slip.lines.all()]


class Upload:
    """What request.FILES hands the upload: a name, a size, read()."""

    def __init__(self, name, content, size=None):
        self.name = name
        self.content = content
        self.size = len(content) if size is None else size
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.content


class FakeBudget:
    """A budget whose remaining time is given, call after call."""

    def __init__(self, *remaining):
        self.values = list(remaining)

    def remaining(self):
        return self.values.pop(0) if self.values else 0.0


class NeverAnInvoiceMixin:
    """The invoice importers replaced by spies that fail if called: a bon is
    never filed as an invoice."""

    def setUp(self):
        super().setUp()
        self.importers = []
        for target in (
            "invoices.receipts.import_document",
            "invoices.importing.parse_and_import",
            "invoices.importing.import_parsed_invoice",
        ):
            patcher = mock.patch(target, side_effect=AssertionError(f"{target} called for a bon"))
            self.importers.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.invoices_before = Invoice.objects.count()

    def assertNoInvoice(self):
        for spy in self.importers:
            spy.assert_not_called()
        self.assertEqual(Invoice.objects.count(), self.invoices_before)


class StoreSlipTests(NeverAnInvoiceMixin, NoNetworkTestCase):
    def test_a_bon_is_stored_with_its_reading_its_lines_and_its_file(self):
        content = pdf_of(texts.NORMAL.text)
        result = store_slip(content, filename="T0000001.pdf", origin=UPLOAD)
        self.assertEqual((result.kind, result.created), (CREATED, True))
        slip = result.slip
        self.assertEqual(result.message, "bon n° 1001 du 14/05/2025 ajouté.")
        self.assertEqual(slip.format, seeded_format())
        self.assertEqual(slip.origin, UPLOAD)
        self.assertEqual(slip.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(slip.original_name, "T0000001.pdf")
        self.assertEqual(slip.number, "1001")
        self.assertEqual(slip.delivery_date, texts.NORMAL.delivery_date)
        self.assertEqual(timezone.localtime(slip.printed_at).replace(tzinfo=None),
                         datetime.combine(*texts.NORMAL.printed))
        self.assertEqual(slip.references, ["610001"])
        self.assertFalse(slip.replaces)
        self.assertEqual(slip.printed_total, Decimal("-175.00"))
        self.assertEqual(slip.read_error, "")
        self.assertIsNotNone(slip.read_at)
        self.assertTrue(slip.checks and all(check["passed"] for check in slip.checks))
        self.assertEqual(set(slip.checks[0]), {"label", "passed", "detail"})
        self.assertEqual(slip.text, reading.pdf_text(content))
        slip.refresh_from_db()
        self.assertEqual(stored_lines(slip), texts.NORMAL.lines)
        self.assertEqual([line.position for line in slip.lines.all()], [1, 2])
        self.assertRegex(slip.file.name, r"^consignes/bons/\d{4}/\d{2}/bon-1001(_\w+)?\.pdf$")
        self.assertTrue(default_storage.exists(slip.file.name))
        with default_storage.open(slip.file.name) as stored:
            self.assertEqual(stored.read(), content)
        self.assertNoInvoice()

    def test_what_a_mail_says_is_cut_to_its_columns_without_control_characters(self):
        result = store_slip(
            pdf_of(texts.NORMAL.text),
            filename="T" * 250 + ".pdf",
            origin=MAIL,
            mail_sender="livreur@example.invalid\x00" + "x" * 400,
            mail_subject="Livraison du 14/05/2025\r\n\x07Tour. : EXEMPLE " + "y" * 400,
            mail_date=timezone.make_aware(datetime(2025, 5, 14, 23, 30)),
        )
        slip = result.slip
        self.assertEqual(slip.origin, MAIL)
        self.assertEqual(len(slip.original_name), 200)
        self.assertEqual(len(slip.mail_sender), 300)
        self.assertEqual(len(slip.mail_subject), 300)
        for text in (slip.mail_sender, slip.mail_subject, slip.original_name):
            self.assertFalse(re.search(r"[\x00-\x1f]", text), text[:40])
        self.assertTrue(slip.mail_subject.startswith("Livraison du 14/05/2025 Tour. : EXEMPLE"))
        self.assertEqual(slip.mail_date, date(2025, 5, 14))

    def test_the_same_bytes_twice_are_one_bon(self):
        content = pdf_of(texts.NORMAL.text)
        first = store_slip(content, filename="a.pdf", origin=UPLOAD)
        files = consignes_files()
        with mock.patch.object(reading, "pdf_text", side_effect=AssertionError("read again")) as extracted:
            again = store_slip(content, filename="b.pdf", origin=MAIL)
        extracted.assert_not_called()
        self.assertEqual((again.kind, again.created, again.slip), (DUPLICATE, False, first.slip))
        self.assertEqual(again.message, "déjà reçu (bon n° 1001 du 14/05/2025).")
        self.assertEqual(Slip.objects.count(), 1)
        self.assertEqual(consignes_files(), files)

    def test_a_resend_is_not_stored_again(self):
        first = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=MAIL)
        files = consignes_files()
        again = store_slip(pdf_of(texts.RESEND.text), filename="b.pdf", origin=MAIL)
        self.assertEqual((again.kind, again.created, again.slip), (RESEND, False, first.slip))
        self.assertEqual(again.message, "bon n° 1001 du 14/05/2025 déjà reçu : ce document en est un renvoi.")
        self.assertEqual(again.reading.printed_at.date(), date(2025, 5, 15))
        self.assertEqual(Slip.objects.count(), 1)
        self.assertEqual(consignes_files(), files)

    def test_the_same_number_on_another_delivery_date_is_another_bon(self):
        store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=MAIL)
        other_day = texts.NORMAL.text.replace("BL No: 610001 du 14/05/2025", "BL No: 610001 du 21/05/2025")
        result = store_slip(pdf_of(other_day), filename="b.pdf", origin=MAIL)
        self.assertEqual(result.kind, CREATED)
        self.assertEqual(result.slip.delivery_date, date(2025, 5, 21))

    def test_the_same_number_with_other_lines_is_another_bon(self):
        store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=MAIL)
        corrected = texts.NORMAL.text.replace(texts.row(texts.KEG, 3, "30.00", "90.00"),
                                              texts.row(texts.KEG, 2, "30.00", "60.00"))
        self.assertEqual(store_slip(pdf_of(corrected), filename="b.pdf", origin=MAIL).kind, CREATED)

    def test_a_bon_naming_no_bon_is_never_taken_for_a_resend(self):
        """No number and no reference: two deliveries of the same kegs on
        one day are two bons."""
        fmt = make_format(name="Sans numéro", number_patterns="", reference_patterns="")
        first = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", fmt=fmt, origin=UPLOAD)
        second = store_slip(pdf_of(texts.RESEND.text), filename="b.pdf", fmt=fmt, origin=UPLOAD)
        self.assertEqual((first.kind, second.kind), (CREATED, CREATED))
        self.assertEqual((first.slip.number, first.slip.references), ("", []))

    def test_a_reading_that_failed_is_stored_so_that_relire_can_fix_it(self):
        fmt = make_format(name="Motif cassé", line_pattern=r"^(?P<designation>.+")
        content = pdf_of(texts.NORMAL.text)
        result = store_slip(content, filename="a.pdf", fmt=fmt, origin=MAIL)
        self.assertEqual((result.kind, result.created), (CREATED, True))
        slip = result.slip
        self.assertTrue(slip.read_error.startswith("Motif de ligne : parenthèse non fermée"), slip.read_error)
        self.assertEqual(slip.checks, [{"label": "Lecture impossible", "passed": False, "detail": result.reading.error}])
        self.assertEqual(slip.lines.count(), 0)
        self.assertEqual((slip.number, slip.delivery_date), ("", None))
        self.assertEqual(slip.text, reading.pdf_text(content))
        self.assertIn("/bon-sans-numero", slip.file.name)
        self.assertTrue(default_storage.exists(slip.file.name))
        self.assertIn("n'a pas pu être lu", result.message)

    def test_a_reading_out_of_time_is_stored_with_its_failed_check(self):
        real = patterns.compile_format

        def slow_lines(fmt, *args, **kwargs):
            motifs = real(fmt, *args, **kwargs)
            motifs["line_pattern"] = [SlowPattern()]
            return motifs

        with mock.patch.object(patterns, "compile_format", side_effect=slow_lines):
            result = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=MAIL)
        self.assertEqual(result.kind, CREATED)
        self.assertIn("est trop lent", result.slip.read_error)
        self.assertEqual([check["label"] for check in result.slip.checks], ["Lecture impossible"])
        self.assertEqual(result.slip.lines.count(), 0)

    def test_a_read_error_is_cut_to_its_column(self):
        long_label = "Motif " + "x" * 400
        failed = reading.SlipReading(error=long_label, checks=[reading.Check("Lecture impossible", False, long_label)])
        with mock.patch.object(reading, "read_slip_text", return_value=failed):
            result = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=MAIL)
        self.assertEqual(len(result.slip.read_error), 300)

    def test_no_format_recognises_it(self):
        files = consignes_files()
        result = store_slip(pdf_of(texts.JUNK), filename="cgv.pdf", origin=UPLOAD)
        self.assertEqual((result.kind, result.slip, result.message), (REFUSED, None, reading.NO_FORMAT))
        self.assertEqual(Slip.objects.count(), 0)
        self.assertEqual(consignes_files(), files)

    def test_several_formats_recognise_it(self):
        make_format(name="Copie du format UBA")
        result = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=UPLOAD)
        self.assertEqual(result.kind, REFUSED)
        self.assertEqual(
            result.message,
            "Plusieurs formats reconnaissent ce document (Copie du format UBA, UBA — bon du livreur) : "
            "choisissez-le dans la liste.",
        )
        self.assertEqual(Slip.objects.count(), 0)

    def test_only_active_formats_with_a_start_motif_recognise_a_document(self):
        no_defaults()
        make_format(name="Inactif", is_active=False)
        make_format(name="Sans début", section_start="")
        result = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=UPLOAD)
        self.assertEqual((result.kind, result.message), (REFUSED, reading.NO_FORMAT))

    def test_a_format_given_is_the_one_used(self):
        other = make_format(name="Autre vendeur", supplier=make_supplier(), is_active=False)
        with mock.patch.object(reading, "detect_format", side_effect=AssertionError("detected")) as detect:
            result = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", fmt=other, origin=UPLOAD)
        detect.assert_not_called()
        self.assertEqual(result.slip.format, other)

    def test_over_five_megabytes_is_refused_before_it_is_read(self):
        content = pdf_of(texts.NORMAL.text)
        with mock.patch.object(reading, "MAX_PDF_BYTES", len(content) - 1), \
                mock.patch.object(reading, "pdf_text", side_effect=AssertionError("read")) as extracted:
            result = store_slip(content, filename="a.pdf", origin=UPLOAD)
        extracted.assert_not_called()
        self.assertEqual((result.kind, result.message), (REFUSED, reading.TOO_HEAVY))

    def test_what_is_not_a_readable_pdf_is_refused_with_the_reading_s_sentence(self):
        self.assertEqual(store_slip(b"pas un PDF", filename="a.pdf", origin=UPLOAD).message, reading.NOT_A_PDF)
        self.assertEqual(store_slip(b"", filename="a.pdf", origin=UPLOAD).message, reading.NOT_A_PDF)
        self.assertEqual(store_slip("texte", filename="a.pdf", origin=UPLOAD).message, reading.NOT_A_PDF)
        with mock.patch.object(reading, "pdf_text", side_effect=SlipError(reading.NO_TEXT)):
            scan = store_slip(b"%PDF-scan", filename="scan.pdf", origin=UPLOAD)
        self.assertEqual((scan.kind, scan.message), (REFUSED, reading.NO_TEXT))
        self.assertEqual(Slip.objects.count(), 0)

    def test_a_text_already_extracted_is_not_extracted_again(self):
        with mock.patch.object(reading, "pdf_text", side_effect=AssertionError("extracted twice")):
            result = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=UPLOAD,
                                text=texts.NORMAL.text.replace("\n", "\r\n") + "\x00")
        self.assertEqual(result.kind, CREATED)
        self.assertEqual(result.slip.text, texts.NORMAL.text)

    def test_the_gather_ignores_an_attachment_that_is_no_bon_of_its_format(self):
        fmt = seeded_format()
        ignored = store_slip(pdf_of(texts.JUNK), filename="cgv.pdf", fmt=fmt, origin=MAIL, skip_non_slips=True)
        self.assertEqual((ignored.kind, ignored.slip), (IGNORED, None))
        self.assertEqual(ignored.message, "pas un bon « UBA — bon du livreur » — ignoré.")
        self.assertEqual(Slip.objects.count(), 0)
        # Chosen by hand, the same document is stored (its checks say what failed).
        kept = store_slip(pdf_of(texts.JUNK), filename="cgv.pdf", fmt=fmt, origin=UPLOAD)
        self.assertEqual(kept.kind, CREATED)
        self.assertFalse(kept.slip.checks[0]["passed"])
        # A reading that FAILED is never taken for « not a bon ».
        broken = make_format(name="Cassé", line_pattern="(")
        failed = store_slip(pdf_of(texts.EMPTY.text), filename="t.pdf", fmt=broken, origin=MAIL, skip_non_slips=True)
        self.assertEqual(failed.kind, CREATED)

    def test_the_origin_is_one_of_the_choices(self):
        with self.assertRaises(ValueError):
            store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin="PORTAIL")


class FileNameTests(NeverAnInvoiceMixin, NoNetworkTestCase):
    """A number is whatever the motif captured: the file stays one flat,
    safe name under consignes/bons/YYYY/MM/."""

    def store_numbered(self, number, **fields):
        fmt = make_format(name=f"Numéros libres {number!r}", number_patterns=r"Ticket No\s*:\s*(?P<numero>.+)$", **fields)
        text = texts.NORMAL.text.replace("Ticket No : 0000001001", f"Ticket No : {number}")
        return store_slip(pdf_of(text), filename="a.pdf", fmt=fmt, origin=UPLOAD).slip

    def assertStoredAs(self, slip, stem):
        folder, name = slip.file.name.rsplit("/", 1)
        self.assertRegex(folder, r"^consignes/bons/\d{4}/\d{2}$")
        self.assertRegex(name, rf"^{re.escape(stem)}(_\w+)?\.pdf$")
        self.assertNotIn("..", slip.file.name)
        self.assertTrue(default_storage.exists(slip.file.name))

    def test_slashes_and_dots_never_make_folders(self):
        self.assertStoredAs(self.store_numbered("12/34"), "bon-1234")
        self.assertStoredAs(self.store_numbered("../x"), "bon-x")
        self.assertStoredAs(self.store_numbered("..\\..\\évasion"), "bon-vasion")

    def test_a_long_number_is_cut_to_twenty_characters(self):
        slip = self.store_numbered("A" * 30 + "-B")
        self.assertEqual(slip.number, "A" * 30 + "-B")
        self.assertStoredAs(slip, "bon-" + "A" * 20)

    def test_without_a_usable_number_the_delivery_date_then_sans_numero(self):
        self.assertStoredAs(self.store_numbered("///"), "bon-20250514")
        fmt = make_format(name="Rien", number_patterns="", date_patterns="rien(?P<date>x)")
        slip = store_slip(pdf_of(texts.EMPTY.text), filename="a.pdf", fmt=fmt, origin=UPLOAD).slip
        self.assertStoredAs(slip, "bon-sans-numero")

    def test_the_helper(self):
        self.assertEqual(file_name("12/34", None), "bon-1234.pdf")
        self.assertEqual(file_name("-", date(2025, 1, 2)), "bon-20250102.pdf")
        self.assertEqual(file_name("", None), "bon-sans-numero.pdf")


class WritingFailureTests(NeverAnInvoiceMixin, NoNetworkTestCase):
    def test_a_failure_after_the_file_is_saved_leaves_no_file_and_no_row(self):
        files = consignes_files()
        with mock.patch.object(SlipLine.objects, "bulk_create", side_effect=RuntimeError("disque plein")):
            with self.assertRaises(RuntimeError):
                store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=UPLOAD)
        self.assertEqual(Slip.objects.count(), 0)
        self.assertEqual(SlipLine.objects.count(), 0)
        self.assertEqual(consignes_files(), files)

    def test_the_same_bytes_stored_at_the_same_moment_answer_already_received(self):
        """The sha check passed for both (an upload during a gather): the
        second insert hits the unique sha256, its file goes, and it answers
        « déjà reçu » with the bon stored first."""
        content = pdf_of(texts.NORMAL.text)
        first = store_slip(content, filename="a.pdf", origin=MAIL).slip
        files = consignes_files()
        with mock.patch.object(slips, "_stored", side_effect=[None, first]), \
                mock.patch.object(slips, "_resend_of", return_value=None):
            result = store_slip(content, filename="b.pdf", origin=UPLOAD)
        self.assertEqual((result.kind, result.created, result.slip), (DUPLICATE, False, first))
        self.assertIn("déjà reçu", result.message)
        self.assertEqual(Slip.objects.count(), 1)
        self.assertEqual(consignes_files(), files)

    def test_another_integrity_error_is_raised_and_its_file_removed(self):
        files = consignes_files()
        with mock.patch.object(slips, "_stored", return_value=None), \
                mock.patch.object(Slip, "save", side_effect=IntegrityError("autre contrainte")):
            with self.assertRaises(IntegrityError):
                store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=UPLOAD)
        self.assertEqual(consignes_files(), files)


class StoreUploadsTests(NeverAnInvoiceMixin, NoNetworkTestCase):
    def test_one_summary_for_the_whole_upload(self):
        normal = pdf_of(texts.NORMAL.text)
        summary = store_uploads(
            [
                Upload("T1.pdf", normal),
                Upload("T2.pdf", pdf_of(texts.EMPTY.text)),
                Upload("T1-copie.pdf", normal),
                Upload("T1-renvoi.pdf", pdf_of(texts.RESEND.text)),
                Upload("cgv.pdf", pdf_of(texts.JUNK)),
            ]
        )
        self.assertEqual(
            summary.message,
            "5 documents : 2 bons ajoutés, 1 déjà reçu, 1 renvoi d'un bon déjà reçu, 1 refusé. "
            "cgv.pdf : Aucun format de bon ne reconnaît ce document.",
        )
        self.assertEqual((summary.created, summary.duplicates, summary.resends, summary.refused), (2, 1, 1, 1))
        self.assertEqual(summary.unrecognised, ["cgv.pdf"])
        self.assertTrue(summary.has_refusals)
        self.assertEqual(Slip.objects.filter(origin=UPLOAD).count(), 2)
        self.assertNoInvoice()

    def test_one_document(self):
        summary = store_uploads([Upload("T1.pdf", pdf_of(texts.NORMAL.text))])
        self.assertEqual(summary.message, "1 document : 1 bon ajouté, 0 déjà reçu, 0 renvoi d'un bon déjà reçu, 0 refusé.")
        self.assertFalse(summary.has_refusals)

    def test_at_most_ten_refusals_are_named(self):
        summary = store_uploads([Upload(f"n{number:02d}.pdf", b"pas un PDF %d" % number) for number in range(12)])
        self.assertEqual(len(summary.refusals), 10)
        self.assertEqual(summary.refusals[0], f"n00.pdf : {reading.NOT_A_PDF}")
        self.assertEqual(summary.more_refused, 2)
        self.assertTrue(summary.message.startswith(
            "12 documents : 0 bon ajouté, 0 déjà reçu, 0 renvoi d'un bon déjà reçu, 12 refusés. n00.pdf : "
        ))
        self.assertTrue(summary.message.endswith("… et 2 autres."))
        self.assertNotIn("n10.pdf", summary.message)

    def test_a_file_too_heavy_is_refused_without_being_read(self):
        heavy = Upload("lourd.pdf", b"", size=reading.MAX_PDF_BYTES + 1)
        summary = store_uploads([heavy])
        self.assertEqual(heavy.reads, 0)
        self.assertEqual(summary.refusals, [f"lourd.pdf : {reading.TOO_HEAVY}"])

    def test_one_file_failing_does_not_stop_the_others(self):
        real = slips.store_slip

        def breaks_on_the_first(content, **kwargs):
            if kwargs["filename"] == "casse.pdf":
                raise RuntimeError("base verrouillée")
            return real(content, **kwargs)

        with mock.patch.object(slips, "store_slip", side_effect=breaks_on_the_first), \
                self.assertLogs("returnables.slips", level="ERROR"):
            summary = store_uploads([Upload("casse.pdf", b"x"), Upload("T1.pdf", pdf_of(texts.NORMAL.text))])
        self.assertEqual(summary.refusals, [f"casse.pdf : {UNEXPECTED}"])
        self.assertEqual(summary.created, 1)

    def test_a_chosen_format_reads_every_document(self):
        other = make_format(name="Choisi à la main", supplier=make_supplier())
        summary = store_uploads([Upload("T1.pdf", pdf_of(texts.NORMAL.text))], fmt=other)
        self.assertEqual(summary.results[0][1].slip.format, other)


class RereadTests(NeverAnInvoiceMixin, NoNetworkTestCase):
    def test_reread_rewrites_the_reading_and_replaces_the_lines(self):
        slip = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=UPLOAD).slip
        first_read = slip.read_at
        fmt = seeded_format()
        fmt.line_pattern = r"^(?P<designation>F.+?)\s+(?P<quantite>\d+)\s+x\s+(?P<prix>[\d.]+)\s+=\s+(?P<montant>[\d.]+)$"
        fmt.save()
        slip = Slip.objects.get(pk=slip.pk)
        result = reread(slip)
        self.assertEqual([line.designation for line in result.lines], [texts.KEG])
        self.assertEqual(stored_lines(slip), [texts.NORMAL.lines[0]])
        slip.refresh_from_db()
        self.assertGreaterEqual(slip.read_at, first_read)
        unread = next(check for check in slip.checks if check["label"] == "Aucune ligne ignorée")
        self.assertFalse(unread["passed"])
        self.assertEqual([line.position for line in slip.lines.all()], [1])

    def test_reread_fixes_a_reading_that_failed(self):
        fmt = make_format(name="Corrigé ensuite", line_pattern="(")
        slip = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", fmt=fmt, origin=MAIL).slip
        self.assertTrue(slip.read_error)
        fmt.line_pattern = texts.UBA_MOTIFS["line_pattern"]
        fmt.save()
        reread(Slip.objects.get(pk=slip.pk))
        slip.refresh_from_db()
        self.assertEqual(slip.read_error, "")
        self.assertEqual((slip.number, slip.references), ("1001", ["610001"]))
        self.assertEqual(stored_lines(slip), texts.NORMAL.lines)

    def test_a_bon_deleted_meanwhile_is_left_alone(self):
        slip = store_slip(pdf_of(texts.NORMAL.text), filename="a.pdf", origin=UPLOAD).slip
        Slip.objects.filter(pk=slip.pk).delete()
        reread(slip)
        self.assertEqual(SlipLine.objects.count(), 0)
        self.assertFalse(Slip.objects.exists())

    def test_reread_format_rereads_every_bon_of_its_format_only(self):
        mine = [store_slip(pdf_of(ticket.text), filename=f"{ticket.name}.pdf", origin=MAIL).slip
                for ticket in (texts.NORMAL, texts.EMPTY, texts.TWO_BLS)]
        other = make_format(name="Autre", supplier=make_supplier())
        theirs = store_slip(pdf_of(texts.MIXED.text), filename="m.pdf", fmt=other, origin=MAIL).slip
        Slip.objects.update(read_at=None)
        self.assertEqual(reread_format(seeded_format()), (3, 0))
        self.assertFalse(Slip.objects.filter(pk__in=[slip.pk for slip in mine], read_at=None).exists())
        self.assertIsNone(Slip.objects.get(pk=theirs.pk).read_at)
        self.assertNoInvoice()

    def test_reread_format_stops_when_a_whole_reading_no_longer_fits(self):
        made = [store_slip(pdf_of(ticket.text), filename=f"{ticket.name}.pdf", origin=MAIL).slip
                for ticket in (texts.NORMAL, texts.EMPTY, texts.TWO_BLS)]
        Slip.objects.update(read_at=None)
        budget = FakeBudget(30.0, patterns.READING_SECONDS, patterns.READING_SECONDS - 0.01)
        self.assertEqual(reread_format(seeded_format(), budget=budget), (2, 1))
        # Newest first: the one left is the oldest.
        self.assertEqual(list(Slip.objects.filter(read_at=None).values_list("pk", flat=True)), [made[0].pk])
        self.assertEqual(reread_format(seeded_format(), budget=FakeBudget()), (0, 3))
