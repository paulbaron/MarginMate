"""The cryptography of the monthly signature (`staff/signing.py`): the
internal authority and its certificates, the passphrase, the frozen
document with its two empty fields, the employee's signature and the
employer's countersignature, both timestamped - and `verify`, which is what
a court would be shown. Every key, certificate and PDF here is generated in
a temp folder; every name is invented; no test reaches a timestamp server
(`signing_support.OfflineTimestamps`)."""

import datetime as dt
import hashlib
import io
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast
from unittest import mock

import pdfplumber
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.test import override_settings
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields

from staff import pdf, signing
from staff.models import Establishment
from staff.pdf import render_month_pdf
from staff.tests.signing_support import (
    FAKE_TSA_NAME,
    FailingTimestamper,
    FakeTimestampAuthority,
    OfflineTimestamps,
    SigningTestMixin,
    blank_canvas,
    drawn_signature,
    employer_signature,
    png_metadata,
)
from staff.tests.support import employee
from staff.timesheet import month_sheet, save_month
from tests.support import NoNetworkTestCase

JUNE = date(2026, 6, 1)
#: 08:24 UTC on 2 July 2026 is 10:24 in Paris (summer time).
WHEN = dt.datetime(2026, 7, 2, 8, 24, tzinfo=dt.UTC)
LATER = dt.datetime(2026, 7, 3, 16, 5, tzinfo=dt.UTC)
DOCUMENT_ID = "0f8e3a52-1111-4222-8333-444455556666"


def field_dictionary(pdf_bytes, name):
    reader = PdfFileReader(io.BytesIO(pdf_bytes))
    for reference in reader.root["/AcroForm"]["/Fields"]:
        field = reference.get_object()
        if str(field.get("/T")) == name:
            return field
    raise AssertionError(f"no field {name}")


def appearance(pdf_bytes, name) -> bytes:
    """The content stream of a field's stamp (its /AP /N), decoded."""
    field = field_dictionary(pdf_bytes, name)
    widget = field["/Kids"][0].get_object() if "/Kids" in field else field
    return widget["/AP"]["/N"].get_object().data


def page_text(pdf_bytes) -> str:
    """What the page itself prints, as a reader extracts it (the stamps are
    the fields' appearances, not part of it)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as document:
        return document.pages[0].extract_text()


def printed(text) -> bytes:
    """`text` as the stamp writes it into its content stream."""
    return pdf._pdf_string(pdf.printable(text)).encode("ascii")


_SHOWN = re.compile(rb"\(((?:[^()\\]|\\.)*)\) Tj")
_ESCAPE = re.compile(rb"\\([0-7]{3}|.)")


def stamp_text(pdf_bytes, name) -> str:
    """What a field's stamp prints, its pieces joined by a space and read
    back from cp1252 - a name wrapped over two lines reads whole again."""

    def unescape(match):
        value = match.group(1)
        return bytes([int(value, 8)]) if len(value) == 3 else value

    pieces = [_ESCAPE.sub(unescape, raw).decode("cp1252") for raw in _SHOWN.findall(appearance(pdf_bytes, name))]
    return " ".join(" ".join(pieces).split())


_DRAWN = re.compile(rb"q 1 0 0 1 0 (-?[0-9.]+) cm q\s+([0-9.]+) 0 0 ([0-9.]+) 0 0 cm /\S+ Do Q")
_TEXT = re.compile(rb"BT /F[0-9]+ ([0-9.]+) Tf (-?[0-9.]+) (-?[0-9.]+) Td")


@dataclass(frozen=True)
class StampGeometry:
    """A field's stamp as its appearance draws it, in points from its foot:
    the box (/BBox), each drawn picture (x, y, width, height), and each piece
    of text (size, x, baseline)."""

    width: float
    height: float
    drawings: tuple
    texts: tuple

    @property
    def text_top(self) -> float:
        return max(baseline + size for size, _x, baseline in self.texts)

    @property
    def baselines(self) -> list[float]:
        return sorted({baseline for _size, _x, baseline in self.texts})


def stamp_geometry(pdf_bytes, name) -> StampGeometry:
    field = field_dictionary(pdf_bytes, name)
    widget = field["/Kids"][0].get_object() if "/Kids" in field else field
    stream = widget["/AP"]["/N"].get_object()
    left, bottom, right, top = (float(value) for value in stream["/BBox"])
    data = stream.data
    return StampGeometry(
        width=abs(right - left),
        height=abs(top - bottom),
        drawings=tuple((0.0, float(y), float(w), float(h)) for y, w, h in _DRAWN.findall(data)),
        texts=tuple((float(size), float(x), float(y)) for size, x, y in _TEXT.findall(data)),
    )


def reason_of(pdf_bytes, field_name) -> str:
    """The /Reason of a signature, read back with pyHanko: part of what the
    signature and its timestamp cover."""
    reader = PdfFileReader(io.BytesIO(pdf_bytes))
    for embedded in reader.embedded_signatures:
        if embedded.field_name == field_name:
            return str(embedded.sig_object["/Reason"])
    raise AssertionError(f"no signature {field_name}")


class SigningCase(SigningTestMixin, NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.bar = Establishment.objects.create(
            pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS"
        )
        self.person = employee()
        save_month(self.person, JUNE, [])
        self.sheet = month_sheet(self.person, JUNE)

    def frozen(self):
        return signing.freeze(self.sheet, self.bar)

    def signed_by_employee(self, reservation="", when=WHEN):
        return signing.sign_as_employee(
            self.frozen(),
            self.person,
            drawn_signature(),
            when,
            document_id=DOCUMENT_ID,
            reservation=reservation,
            establishment=self.bar,
        )

    def countersigned(self, reservation=""):
        employee_signed = self.signed_by_employee(reservation)
        return signing.countersign(employee_signed.pdf, self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID)


class AuthorityTests(SigningCase):
    def test_the_first_signature_makes_the_authority_and_the_employee_the_countersignature_the_employer(self):
        keys = self.private_dir / "keys"
        self.assertFalse(keys.exists())
        identity = signing.employee_identity(self.person, self.bar)
        for stem in ("authority", f"employees/{self.person.pk}"):
            with self.subTest(stem=stem):
                self.assertTrue((keys / f"{stem}.key.pem").is_file())
                self.assertTrue((keys / f"{stem}.cert.pem").is_file())
        self.assertFalse((keys / "employer.key.pem").exists())
        employer = signing.employer_identity(self.bar)
        self.assertTrue((keys / "employer.key.pem").is_file())
        self.assertEqual(employer.name, "BAR EXEMPLE")
        employer.certificate.verify_directly_issued_by(signing.authority(self.bar).certificate)
        certificate = identity.certificate
        self.assertEqual(certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, "DUPONT Jeanne")
        self.assertEqual(certificate.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value, "BAR EXEMPLE")
        authority = signing.authority(self.bar)
        certificate.verify_directly_issued_by(authority.certificate)
        self.assertEqual(
            authority.certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value,
            "Autorité interne de BAR EXEMPLE",
        )

    def test_ec_p256_sha256_ten_years(self):
        for identity in (
            signing.authority(self.bar),
            signing.employer_identity(self.bar),
            signing.employee_identity(self.person, self.bar),
        ):
            with self.subTest(name=identity.name):
                self.assertIsInstance(identity.key, ec.EllipticCurvePrivateKey)
                self.assertIsInstance(identity.key.curve, ec.SECP256R1)
                self.assertEqual(identity.certificate.signature_hash_algorithm.name, "sha256")
                validity = identity.certificate.not_valid_after_utc - identity.certificate.not_valid_before_utc
                self.assertGreaterEqual(validity, dt.timedelta(days=3650))
        self.assertTrue(
            signing.authority(self.bar).certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        )
        self.assertFalse(
            signing.employee_identity(self.person, self.bar)
            .certificate.extensions.get_extension_for_class(x509.BasicConstraints)
            .value.ca
        )

    def test_made_once_then_reused(self):
        first = signing.authority(self.bar).sha256
        employee_first = signing.employee_identity(self.person, self.bar).sha256
        self.assertEqual(signing.authority(self.bar).sha256, first)
        self.assertEqual(signing.employee_identity(self.person, self.bar).sha256, employee_first)
        self.assertEqual(signing.authority_fingerprint(), first)

    def test_each_signature_says_which_authority_issued_its_certificate(self):
        """Recorded at signing time (the request's events keep it): the
        authority on disk today may be another one by the time the proof is
        read again."""
        employee_signed = self.signed_by_employee()
        final = signing.countersign(employee_signed.pdf, self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID)
        authority = signing.authority(self.bar).sha256
        self.assertEqual((employee_signed.issuer_sha256, final.issuer_sha256), (authority, authority))

    def test_a_renamed_employee_gets_a_certificate_in_his_new_name(self):
        before = signing.employee_identity(self.person, self.bar)
        self.person.last_name = "Durand"
        self.person.save()
        after = signing.employee_identity(self.person, self.bar)
        self.assertNotEqual(before.sha256, after.sha256)
        self.assertEqual(after.name, "DURAND Jeanne")
        # The old one is kept, not overwritten: it signed documents.
        archived = list((self.private_dir / "keys" / "archive").glob("*.cert.pem"))
        self.assertEqual(len(archived), 1)

    def test_no_establishment_name_no_certificate(self):
        nameless = Establishment(name="  ")
        self.assertIn("nom de l'établissement", signing.setup_problem(nameless))
        with self.assertRaises(signing.SigningSetupError):
            signing.employee_identity(self.person, nameless)
        self.assertEqual(signing.setup_problem(self.bar), "")


class PassphraseTests(SigningCase):
    def key_text(self, stem="authority"):
        return (self.private_dir / "keys" / f"{stem}.key.pem").read_text()

    def test_without_a_passphrase_stored_in_clear_and_said(self):
        signing.employee_identity(self.person, self.bar)
        self.assertIn("-----BEGIN PRIVATE KEY-----", self.key_text())
        self.assertEqual(
            signing.key_warning(),
            "clé de signature non chiffrée : définir MARGINMATE_SIGNING_PASSPHRASE avant la mise en ligne",
        )

    def test_with_a_passphrase_encrypted_and_nothing_to_say(self):
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase de passe d'essai"):
            identity = signing.employee_identity(self.person, self.bar)
            signing.employer_identity(self.bar)
            for stem in ("authority", "employer", f"employees/{self.person.pk}"):
                self.assertIn("-----BEGIN ENCRYPTED PRIVATE KEY-----", self.key_text(stem))
            self.assertEqual(signing.key_warning(), "")
            self.assertEqual(signing.employee_identity(self.person, self.bar).sha256, identity.sha256)

    def test_keys_made_in_clear_are_encrypted_once_a_passphrase_is_set(self):
        signing.employee_identity(self.person, self.bar)
        signing.employer_identity(self.bar)
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase de passe d'essai"):
            self.assertIn("encore non chiffrée", signing.key_warning())
            # The next signature - here the authority it goes through - encrypts them all.
            signing.authority(self.bar)
            for stem in ("authority", "employer", f"employees/{self.person.pk}"):
                self.assertIn("ENCRYPTED", self.key_text(stem))
            self.assertEqual(signing.key_warning(), "")

    def test_an_encrypted_key_without_its_passphrase_is_a_french_refusal(self):
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase de passe d'essai"):
            signing.authority(self.bar)
        with self.assertRaises(signing.SigningKeyError) as caught:
            signing.authority(self.bar)
        self.assertIn("MARGINMATE_SIGNING_PASSPHRASE", str(caught.exception))
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="une autre phrase"):
            with self.assertRaises(signing.SigningKeyError) as caught:
                signing.authority(self.bar)
        self.assertIn("ne s'ouvre pas", str(caught.exception))
        # Nothing was overwritten by the refusals.
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase de passe d'essai"):
            signing.authority(self.bar)


class FreezeTests(SigningCase):
    def test_an_unsaved_month_is_never_frozen(self):
        """A planning is not a record: the typical week of a month nobody
        saved must never reach a signature."""
        july = month_sheet(self.person, date(2026, 7, 1))
        self.assertFalse(july.saved)
        with self.assertRaises(signing.DocumentError) as caught:
            signing.freeze(july, self.bar)
        self.assertIn("pas enregistré", str(caught.exception))

    def test_an_establishment_with_no_name_freezes_nothing(self):
        """Nobody to countersign, and a sheet with no header: refused before
        the employee is asked for anything."""
        with self.assertRaises(signing.SigningSetupError):
            signing.freeze(self.sheet, Establishment(name=""))

    def test_both_fields_are_there_empty_where_the_boxes_are(self):
        frozen = self.frozen()
        reader = PdfFileReader(io.BytesIO(frozen))
        self.assertEqual(len(reader.embedded_signatures), 0)
        self.assertEqual(
            signing.FIELD_BOXES,
            (
                (signing.EMPLOYEE_FIELD, pdf.ELECTRONIC_SIGNATURE_BOXES[0]),
                (signing.EMPLOYER_FIELD, pdf.ELECTRONIC_SIGNATURE_BOXES[1]),
            ),
        )
        for name, box in signing.FIELD_BOXES:
            with self.subTest(field=name):
                field = field_dictionary(frozen, name)
                self.assertEqual(str(field["/FT"]), "/Sig")
                self.assertNotIn("/V", field)
                rect = [float(value) for value in field["/Rect"]]
                for got, wanted in zip(rect, box.stamp_rect):
                    self.assertAlmostEqual(got, wanted, places=2)

    def test_the_frozen_document_is_the_timesheet_plus_its_fields(self):
        """Its first revision is, byte for byte, the PDF « Personnel »
        prints for the month with the boxes worded for an electronic
        signature: the fields are an incremental update."""
        rendered = render_month_pdf(self.sheet, self.bar, electronic=True)
        self.assertTrue(self.frozen().startswith(rendered))

    def test_the_frozen_document_asks_for_no_handwritten_read_and_approved(self):
        """The stamp goes where the paper sheet asks for a date, a signature
        and « Lu et approuvé » written by hand: the frozen document says what
        goes there instead. The download keeps the paper words."""
        frozen = self.frozen()
        text = page_text(frozen)
        self.assertIn(pdf.EMPLOYEE_ELECTRONIC_NOTE, text)
        self.assertIn(pdf.EMPLOYER_ELECTRONIC_NOTE, text)
        self.assertNotIn("Lu et approuvé", text)
        self.assertNotIn("Date et signature", text)
        self.assertFalse(frozen.startswith(render_month_pdf(self.sheet, self.bar)))
        self.assertIn("Lu et approuvé", page_text(render_month_pdf(self.sheet, self.bar)))

    def test_a_document_frozen_with_the_paper_words_still_signs(self):
        """Documents frozen before 28/09 print the paper instructions and
        are never drawn again: signed and countersigned where their own
        fields are, they verify, and still say what they said."""
        rendered = render_month_pdf(self.sheet, self.bar)
        writer = IncrementalPdfFileWriter(io.BytesIO(rendered))
        for name, box in ((signing.EMPLOYEE_FIELD, pdf.EMPLOYEE_BOX), (signing.EMPLOYER_FIELD, pdf.EMPLOYER_BOX)):
            fields.append_signature_field(
                writer, fields.SigFieldSpec(sig_field_name=name, on_page=0, box=box.stamp_rect)
            )
        output = io.BytesIO()
        writer.write(output)
        old = output.getvalue()
        employee_signed = signing.sign_as_employee(
            old,
            self.person,
            drawn_signature(),
            WHEN,
            document_id=DOCUMENT_ID,
            establishment=self.bar,
        )
        final = signing.countersign(employee_signed.pdf, self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID)
        self.assertTrue(final.pdf.startswith(old))
        result = signing.verify(final.pdf)
        self.assertTrue(result.ok, result.verdict)
        self.assertIn("Lu et approuvé", page_text(final.pdf))
        self.assertIn("DUPONT Jeanne", stamp_text(final.pdf, signing.EMPLOYEE_FIELD))


class SignatureTests(SigningCase):
    def test_signed_countersigned_and_verified(self):
        final = self.countersigned()
        result = signing.verify(final.pdf)
        self.assertEqual(result.error, "")
        self.assertTrue(result.ok, result.verdict)
        self.assertEqual([check.field for check in result.signatures], [signing.EMPLOYEE_FIELD, signing.EMPLOYER_FIELD])
        employee_check, employer_check = result.signatures
        self.assertEqual(employee_check.signer, "DUPONT Jeanne")
        self.assertEqual(employer_check.signer, "BAR EXEMPLE")
        for check in result.signatures:
            with self.subTest(field=check.field):
                self.assertTrue(check.intact)
                self.assertTrue(check.valid)
                self.assertTrue(check.trusted)
                self.assertIsNotNone(check.timestamp)
                self.assertTrue(check.timestamp_trusted)
                self.assertEqual(check.timestamp_authority, FAKE_TSA_NAME)
                self.assertEqual(check.problems, ())
        # Signing the employer's field, created with the document, is form
        # filling as far as the employee's signature is concerned.
        self.assertEqual(employee_check.modification, "FORM_FILLING")
        self.assertEqual(result.unsigned_fields, ())
        self.assertIn("intact", result.verdict)
        self.assertIn("DUPONT Jeanne", result.verdict)
        for word in ("qualifiée", "avancée", "manuscrite"):
            self.assertNotIn(word, result.verdict)

    def test_the_employee_alone_is_verified_too_and_the_employer_field_waits(self):
        signed = self.signed_by_employee()
        result = signing.verify(signed.pdf)
        self.assertTrue(result.ok, result.verdict)
        self.assertEqual(len(result.signatures), 1)
        self.assertEqual(result.unsigned_fields, (signing.EMPLOYER_FIELD,))
        self.assertIn("contresignature", result.verdict)

    def test_the_timestamp_is_recorded(self):
        fixed = dt.datetime(2026, 7, 2, 8, 24, 30, tzinfo=dt.UTC)
        with OfflineTimestamps(fixed_dt=fixed):
            signed = self.signed_by_employee()
        self.assertEqual(signed.timestamp, fixed)
        self.assertEqual(signed.authority, "http://horodatage.test")

    def test_the_stamp_says_who_when_in_paris_and_which_document(self):
        signed = self.countersigned()
        stamp = appearance(signed.pdf, signing.EMPLOYEE_FIELD)
        self.assertIn(printed("Signé électroniquement par DUPONT Jeanne"), stamp)
        self.assertIn(printed("le 02/07/2026 à 10:24 (heure de Paris)"), stamp)
        self.assertIn(printed(f"Document n° {DOCUMENT_ID}"), stamp)
        self.assertIn(b"/F1", stamp)  # Helvetica, the sheet's own font
        self.assertIn(b" Do", stamp)  # the drawn signature
        self.assertNotIn(printed("réserves"), stamp)
        employer = appearance(signed.pdf, signing.EMPLOYER_FIELD)
        self.assertIn(printed("Contresigné électroniquement par BAR EXEMPLE"), employer)
        self.assertIn(printed("le 03/07/2026 à 18:05 (heure de Paris)"), employer)
        self.assertIn(printed(f"Document n° {DOCUMENT_ID}"), employer)

    def test_winter_time_is_paris_time_too(self):
        signed = self.signed_by_employee(when=dt.datetime(2026, 1, 5, 23, 30, tzinfo=dt.UTC))
        self.assertIn(printed("le 06/01/2026 à 00:30 (heure de Paris)"), appearance(signed.pdf, signing.EMPLOYEE_FIELD))

    def test_a_signature_with_reservations_says_so_on_the_document(self):
        signed = self.signed_by_employee(reservation="Il manque 2 h le samedi 13.")
        self.assertIn(printed("avec réserves"), appearance(signed.pdf, signing.EMPLOYEE_FIELD))

    def test_the_employer_signs_after_the_employee_only_and_each_once(self):
        with self.assertRaises(signing.DocumentError) as caught:
            signing.countersign(self.frozen(), self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID)
        self.assertIn("salarié", str(caught.exception))
        signed = self.signed_by_employee()
        with self.assertRaises(signing.DocumentError):
            signing.sign_as_employee(
                signed.pdf, self.person, drawn_signature(), WHEN, document_id=DOCUMENT_ID, establishment=self.bar
            )
        final = signing.countersign(signed.pdf, self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID)
        with self.assertRaises(signing.DocumentError):
            signing.countersign(final.pdf, self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID)

    def test_a_file_that_is_no_pdf_is_a_document_error_not_a_timestamp_one(self):
        with self.assertRaises(signing.DocumentError):
            signing.sign_as_employee(
                b"pas un PDF", self.person, drawn_signature(), WHEN, document_id=DOCUMENT_ID, establishment=self.bar
            )


class TimestampTests(SigningCase):
    def test_the_next_server_is_tried_when_one_does_not_answer(self):
        down = FailingTimestamper("http://premier-en-panne.test")
        working = FakeTimestampAuthority.shared().timestamper()
        signed = signing.sign_as_employee(
            self.frozen(),
            self.person,
            drawn_signature(),
            WHEN,
            document_id=DOCUMENT_ID,
            establishment=self.bar,
            stampers=[down, working],
        )
        self.assertGreaterEqual(down.calls, 1)
        self.assertEqual(signed.authority, "http://horodatage.test")
        self.assertTrue(signing.verify(signed.pdf).ok)

    def test_when_none_answers_the_signature_is_refused(self):
        stampers = [FailingTimestamper("http://premier.test"), FailingTimestamper("http://second.test")]
        with self.assertRaises(signing.TimestampUnavailable) as caught:
            signing.sign_as_employee(
                self.frozen(),
                self.person,
                drawn_signature(),
                WHEN,
                document_id=DOCUMENT_ID,
                establishment=self.bar,
                stampers=stampers,
            )
        self.assertEqual(
            str(caught.exception), "Le service d'horodatage ne répond pas, réessayez dans quelques minutes."
        )
        self.assertEqual(
            [url for url, _reason in caught.exception.failures], ["http://premier.test", "http://second.test"]
        )
        self.assertTrue(all(stamper.calls for stamper in stampers))

    def test_no_server_configured_is_refused_the_same_way(self):
        """settings_test has STAFF_TIMESTAMP_URLS = []: a test that forgot to
        inject its timestamper is refused, it never reaches DigiCert."""
        self.timestamps.stop()
        with self.assertRaises(signing.TimestampUnavailable):
            self.signed_by_employee()

    def test_the_real_servers_are_http_timestampers_on_the_settings(self):
        self.timestamps.stop()
        with override_settings(STAFF_TIMESTAMP_URLS=["http://un.test", "http://deux.test"]):
            stampers = signing.timestampers()
        self.assertEqual([stamper.url for stamper in stampers], ["http://un.test", "http://deux.test"])

    def test_a_real_server_is_never_reached_from_a_test(self):
        """tests.support.NoNetworkTestCase: an HTTP timestamper that slipped
        through fails loudly - it is not quietly taken for a server down."""
        self.timestamps.stop()
        with override_settings(STAFF_TIMESTAMP_URLS=["http://timestamp.digicert.com"]):
            with self.assertRaises(AssertionError) as caught:
                self.signed_by_employee()
        self.assertIn("timestamp server", str(caught.exception))


class VerifyTests(SigningCase):
    def test_a_byte_changed_after_signing_is_seen(self):
        final = self.countersigned().pdf
        # The document's title, in the first revision - covered by both signatures.
        position = final.index(b"/Title <FEFF0046") + len(b"/Title <FEFF004")
        tampered = final[:position] + b"7" + final[position + 1 :]
        self.assertEqual(len(tampered), len(final))
        result = signing.verify(tampered)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "")
        self.assertTrue(result.signatures)
        self.assertFalse(any(check.intact for check in result.signatures))
        self.assertIn("modifié", result.verdict)

    def test_a_revision_added_after_the_last_signature_is_seen(self):
        final = self.countersigned().pdf
        writer = IncrementalPdfFileWriter(io.BytesIO(final))
        extra = generic.StreamObject(stream_data=b"BT /F1 12 Tf 100 300 Td (35 h) Tj ET")
        writer.add_stream_to_page(0, writer.add_object(extra))
        output = io.BytesIO()
        writer.write(output)
        result = signing.verify(output.getvalue())
        self.assertFalse(result.ok)
        self.assertTrue(all(check.intact for check in result.signatures))
        self.assertIn("après", result.verdict)

    def test_another_authority_is_valid_but_not_trusted(self):
        final = self.countersigned().pdf
        elsewhere = Path(tempfile.mkdtemp(prefix="marginmate-other-install-"))
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        with override_settings(TENANTS_ROOT=elsewhere):
            signing.authority(Establishment(name="AUTRE BAR"))
            result = signing.verify(final)
        self.assertFalse(result.ok)
        self.assertTrue(all(check.intact and check.valid for check in result.signatures))
        self.assertFalse(any(check.trusted for check in result.signatures))
        self.assertIn("autorité", result.verdict)

    def test_losing_the_keys_does_not_make_a_signature_unverifiable(self):
        """Each signature embeds its certificates: with the private folder
        gone, the document still proves it is intact and whom it names."""
        final = self.countersigned().pdf
        shutil.rmtree(self.private_dir / "keys")
        result = signing.verify(final)
        self.assertTrue(all(check.intact and check.valid for check in result.signatures))
        self.assertEqual([check.signer for check in result.signatures], ["DUPONT Jeanne", "BAR EXEMPLE"])
        self.assertTrue(all(check.timestamp_trusted for check in result.signatures))

    def test_after_the_keys_are_lost_the_verdict_still_says_intact_and_whom(self):
        """A new authority made after the keys folder was lost did not issue
        these certificates: said as such - « clés perdues ou remplacées ? » -
        and never as « not issued by this installation » alone, with the
        signed content still said intact and who signed (review, 28/09)."""
        final = self.countersigned().pdf
        shutil.rmtree(self.private_dir / "keys")
        signing.authority(self.bar)  # the next signature makes a new one
        result = signing.verify(final)
        self.assertFalse(result.ok)
        self.assertTrue(all(check.intact and check.valid for check in result.signatures))
        self.assertIn(
            "ne se rattache à aucune autorité connue de cette installation (clés perdues ou remplacées ?)",
            result.verdict,
        )
        self.assertIn("intact", result.verdict)
        self.assertIn("signé par DUPONT Jeanne", result.verdict)
        self.assertIn("contresigné par BAR EXEMPLE", result.verdict)

    def test_an_unknown_timestamp_authority_is_said(self):
        final = self.countersigned().pdf
        with mock.patch("staff.signing.timestamp_trust_roots", new=list):
            result = signing.verify(final)
        self.assertFalse(result.ok)
        self.assertTrue(all(check.intact for check in result.signatures))
        self.assertFalse(any(check.timestamp_trusted for check in result.signatures))
        self.assertIn("horodatage", result.verdict)

    def test_the_frozen_document_has_no_signature_yet(self):
        result = signing.verify(self.frozen())
        self.assertFalse(result.ok)
        self.assertEqual(result.signatures, ())
        self.assertEqual(result.unsigned_fields, (signing.EMPLOYEE_FIELD, signing.EMPLOYER_FIELD))
        self.assertIn("Aucune signature", result.verdict)

    def test_bytes_that_are_no_pdf_are_a_sentence_not_a_traceback(self):
        for data in (b"", b"%PDF-1.4 tronque", b"\x00" * 50):
            with self.subTest(data=data[:10]):
                result = signing.verify(data)
                self.assertFalse(result.ok)
                self.assertTrue(result.error)
                self.assertEqual(result.verdict, result.error)


# -- Long names, and letters cp1252 lacks (review, 28/09) --------------------------------------------------------

#: 46 characters: with « Autorité interne de » in front, 67 bytes of UTF-8.
LONG_BAR = "Brasserie Imaginaire du Faubourg Saint-Exemple"
#: « DE LA FONTAINE-SAINT-EXEMPLE Marie-Hélène Éléonore Françoise »: 60
#: characters, 65 bytes - past what a certificate's common name may hold.
LONG_LAST, LONG_FIRST = "De La Fontaine-Saint-Exemple", "Marie-Hélène Éléonore Françoise"


class LongNameTests(SigningCase):
    """X.520 caps a certificate's common name at 64 BYTES of UTF-8, and
    `cryptography` raises a plain ValueError past it - not a SigningError, so
    it escaped every page: an establishment named in 44 characters or more
    with one accent in « Autorité interne de … », or an employee whose name
    is long and accented, could not sign at all, and the employee's page
    answered an HTTP 500."""

    def long_names(self):
        self.bar.name = LONG_BAR
        self.bar.save()
        self.person.last_name, self.person.first_name = LONG_LAST, LONG_FIRST
        self.person.save()

    def common_name(self, common, organisation=LONG_BAR) -> str:
        # A common name's value is a str; only an X.500 unique identifier's is bytes.
        return cast(str, signing._name(common, organisation).get_attributes_for_oid(NameOID.COMMON_NAME)[0].value)

    def test_a_name_is_cut_on_its_bytes_between_two_words(self):
        authority = f"Autorité interne de {LONG_BAR}"
        self.assertGreater(len(authority.encode()), signing.NAME_LIMIT)
        self.assertEqual(self.common_name(authority), "Autorité interne de Brasserie Imaginaire du Faubourg")
        employee_name = f"{LONG_LAST.upper()} {LONG_FIRST}"
        self.assertEqual((len(employee_name), len(employee_name.encode())), (60, 65))
        self.assertEqual(self.common_name(employee_name), "DE LA FONTAINE-SAINT-EXEMPLE Marie-Hélène Éléonore")
        # One word past the limit is cut inside it - never inside a character.
        self.assertEqual(signing._limited("é" * 40), "é" * 32)
        self.assertEqual(signing._limited("  BAR   EXEMPLE "), "BAR EXEMPLE")

    def test_signed_countersigned_and_verified_with_long_accented_names(self):
        self.long_names()
        final = self.countersigned()
        result = signing.verify(final.pdf)
        self.assertTrue(result.ok, result.verdict)

    def test_an_establishment_renamed_long_after_the_employee_signed_still_countersigns(self):
        signed = self.signed_by_employee()
        self.bar.name = "Brasserie Imaginaire du Faubourg Saint-Exemple, Café-Théâtre Éphémère"
        self.bar.save()
        final = signing.countersign(signed.pdf, self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID)
        result = signing.verify(final.pdf)
        self.assertTrue(result.ok, result.verdict)


class StampNameTests(SigningCase):
    """The stamp names who signed, whole (review, 28/09): sharing one 7 pt
    line with « Signé électroniquement par », a long name was cut with
    « … » at the box's edge - the employer's past about 27 capitals - and a
    letter cp1252 lacks printed « ? » (« WÓJCIK ?ukasz »), while the
    certificate and the phone page carried the real name."""

    def rename(self, last, first):
        self.person.last_name, self.person.first_name = last, first
        self.person.save()

    def test_a_long_name_is_stamped_whole(self):
        for last, first in (("De La Tour d'Auvergne-Lauraguais", "Marie-Christine"), (LONG_LAST, LONG_FIRST)):
            with self.subTest(last=last):
                self.rename(last, first)
                signed = self.signed_by_employee(reservation="Il manque 2 h le samedi 13.")
                text = stamp_text(signed.pdf, signing.EMPLOYEE_FIELD)
                self.assertIn(f"Signé électroniquement par {self.person.display_name}", text)
                self.assertNotIn("…", text)
                self.assertIn("le 02/07/2026 à 10:24 (heure de Paris) – avec réserves", text)
                self.assertIn(f"Document n° {DOCUMENT_ID}", text)
                self.assertTrue(signing.verify(signed.pdf).ok)

    def test_a_long_establishment_is_countersigned_whole(self):
        self.bar.name = "BRASSERIE DE LA GARE ET DU COMMERCE REUNIS"
        self.bar.save()
        final = self.countersigned()
        text = stamp_text(final.pdf, signing.EMPLOYER_FIELD)
        self.assertIn("Contresigné électroniquement par BRASSERIE DE LA GARE ET DU COMMERCE REUNIS", text)
        self.assertNotIn("…", text)
        self.assertIn("le 03/07/2026 à 18:05 (heure de Paris)", text)

    def test_a_short_name_keeps_its_one_line(self):
        signed = self.signed_by_employee()
        self.assertIn(
            printed("Signé électroniquement par DUPONT Jeanne"), appearance(signed.pdf, signing.EMPLOYEE_FIELD)
        )

    def test_letters_cp1252_lacks_are_written_as_their_nearest(self):
        for (last, first), shown in (
            (("Wójcik", "Łukasz"), "WÓJCIK Lukasz"),
            (("Nguyễn", "Thị Đào"), "NGUYÊN Thi Dào"),
        ):
            with self.subTest(shown=shown):
                self.rename(last, first)
                signed = self.signed_by_employee()
                text = stamp_text(signed.pdf, signing.EMPLOYEE_FIELD)
                self.assertIn(f"Signé électroniquement par {shown}", text)
                self.assertNotIn("?", text)
                # The certificate keeps the name as it is written.
                self.assertEqual(signing.verify(signed.pdf).signatures[0].signer, self.person.display_name)


class ReasonTests(SigningCase):
    """What each signature covers besides the page: the employee's
    reservations in his own words, and the journal's last hash when he (or
    the employer) signed (review, 28/09: the reservations lived only in a
    database column - rewritten there, nothing noticed - and the journal was
    anchored nowhere). The signature dictionary's /Reason is inside the
    signed byte range, and so under the timestamp."""

    JOURNAL = "ab" * 32

    def test_his_reservations_are_inside_his_signature(self):
        words = "Le 12, j'ai fini à 23 h 30 et non à 23 h.\r\nEt le 13 → pareil."
        signed = signing.sign_as_employee(
            self.frozen(),
            self.person,
            drawn_signature(),
            WHEN,
            document_id=DOCUMENT_ID,
            establishment=self.bar,
            reservation=words,
            journal=self.JOURNAL,
        )
        reason = reason_of(signed.pdf, signing.EMPLOYEE_FIELD)
        self.assertIn(words, reason)
        self.assertIn(self.JOURNAL, reason)
        read = signing.signed_reasons(signed.pdf)[signing.EMPLOYEE_FIELD]
        self.assertEqual((read.reservation, read.journal), (words, self.JOURNAL))
        self.assertTrue(signing.verify(signed.pdf).ok)

    def test_without_reservations_the_reason_says_so(self):
        signed = self.signed_by_employee()
        read = signing.signed_reasons(signed.pdf)[signing.EMPLOYEE_FIELD]
        self.assertEqual((read.reservation, read.journal), ("", ""))
        self.assertIn("certifié exact", read.text)

    def test_the_countersignature_carries_the_journal_too(self):
        employee_signed = self.signed_by_employee()
        final = signing.countersign(
            employee_signed.pdf, self.bar, employer_signature(), LATER, document_id=DOCUMENT_ID, journal=self.JOURNAL
        )
        reasons = signing.signed_reasons(final.pdf)
        self.assertEqual(reasons[signing.EMPLOYER_FIELD].journal, self.JOURNAL)
        self.assertIsNone(reasons[signing.EMPLOYER_FIELD].reservation)
        self.assertEqual(reasons[signing.EMPLOYEE_FIELD].reservation, "")
        self.assertEqual(signing.signed_reasons(b"pas un PDF"), {})


class EmployerDrawingTests(SigningCase):
    """The employer countersigns with his own drawn signature (the owner,
    28/09: « I cannot draw my signature as the employer »): required, checked
    as the employee's is, drawn in « L'employeur » by the one layout both
    stamps share, and its SHA-256 sealed in the countersignature's /Reason."""

    JOURNAL = "cd" * 32

    def test_no_drawing_no_countersignature(self):
        signed = self.signed_by_employee()
        for png, words in (
            (None, signing.EMPLOYER_DRAWING_MISSING),
            (b"", signing.EMPLOYER_DRAWING_MISSING),
            # The owner's own words: he countersigns (review, 28/09).
            (blank_canvas(), signing.EMPLOYER_DRAWING_EMPTY),
            (b"pas une image", "image PNG"),
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "illisible"),
            (drawn_signature(width=1300, height=400), "trop grande"),
        ):
            with self.subTest(png=png[:12] if png else png):
                stamper = FailingTimestamper()
                with self.assertRaises(signing.SignatureImageError) as caught:
                    signing.countersign(signed.pdf, self.bar, png, LATER, document_id=DOCUMENT_ID, stampers=[stamper])
                self.assertIn(words, str(caught.exception))
                # Refused before anything was signed or timestamped.
                self.assertEqual(stamper.calls, 0)

    def test_the_drawing_kept_is_the_one_checked_and_its_hash_is_sealed(self):
        """Decoded and encoded again, the chunks a canvas never writes
        dropped - the employee's checks - and what the countersignature seals
        is the SHA-256 of exactly that picture, the one the request keeps."""
        employee_signed = self.signed_by_employee()
        posted = employer_signature(pnginfo=png_metadata())
        self.assertIn(b"Auteur imaginaire", posted)
        final = signing.countersign(
            employee_signed.pdf, self.bar, posted, LATER, document_id=DOCUMENT_ID, journal=self.JOURNAL
        )
        self.assertEqual(final.drawing, signing.clean_signature_png(posted))
        self.assertNotIn(b"Auteur imaginaire", final.drawing)
        self.assertEqual(final.drawing_sha256, hashlib.sha256(final.drawing).hexdigest())
        reasons = signing.signed_reasons(final.pdf)
        employer = reasons[signing.EMPLOYER_FIELD]
        self.assertEqual(
            (employer.drawing, employer.journal, employer.reservation), (final.drawing_sha256, self.JOURNAL, None)
        )
        self.assertIn(
            f"signature dessinée SHA-256 {final.drawing_sha256}", reason_of(final.pdf, signing.EMPLOYER_FIELD)
        )
        # The employee's signature seals no employer's drawing.
        self.assertEqual(reasons[signing.EMPLOYEE_FIELD].drawing, "")
        self.assertTrue(signing.verify(final.pdf).ok)

    def test_his_drawing_is_stamped_above_his_lines_inside_the_box(self):
        final = self.countersigned()
        stamp = stamp_geometry(final.pdf, signing.EMPLOYER_FIELD)
        ((x, y, width, height),) = stamp.drawings
        self.assertGreater(y, stamp.text_top)
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + width, stamp.width)
        self.assertLessEqual(y + height, stamp.height)
        self.assertGreater(height, 20)
        text = stamp_text(final.pdf, signing.EMPLOYER_FIELD)
        self.assertIn("Contresigné électroniquement par BAR EXEMPLE", text)
        self.assertIn("le 03/07/2026 à 18:05 (heure de Paris)", text)
        self.assertIn(f"Document n° {DOCUMENT_ID}", text)

    def test_both_stamps_share_one_layout(self):
        """The same drawing comes out at the same size and place in either
        box, over lines of the same sizes on the same baselines."""
        same = drawn_signature()
        employee_signed = signing.sign_as_employee(
            self.frozen(),
            self.person,
            same,
            WHEN,
            document_id=DOCUMENT_ID,
            establishment=self.bar,
        )
        final = signing.countersign(employee_signed.pdf, self.bar, same, LATER, document_id=DOCUMENT_ID)
        employee = stamp_geometry(final.pdf, signing.EMPLOYEE_FIELD)
        employer = stamp_geometry(final.pdf, signing.EMPLOYER_FIELD)
        self.assertEqual((employee.width, employee.height), (employer.width, employer.height))
        self.assertEqual(len(employee.drawings), 1)
        self.assertEqual(employee.drawings, employer.drawings)
        self.assertEqual(employee.baselines, employer.baselines)
        for stamp in (employee, employer):
            self.assertEqual({size for size, _x, _baseline in stamp.texts}, {signing.STAMP_TEXT_SIZE})

    def test_a_long_establishment_is_named_whole_under_its_drawing(self):
        for name in (
            "BRASSERIE DE LA GARE ET DU COMMERCE REUNIS",
            "Brasserie Imaginaire du Faubourg Saint-Exemple, Café-Théâtre Éphémère et Associés Réunis de la Place",
        ):
            with self.subTest(name=name):
                self.bar.name = name
                self.bar.save()
                final = self.countersigned()
                text = stamp_text(final.pdf, signing.EMPLOYER_FIELD)
                self.assertIn(f"Contresigné électroniquement par {name}", text)
                self.assertNotIn("…", text)
                stamp = stamp_geometry(final.pdf, signing.EMPLOYER_FIELD)
                ((x, y, width, height),) = stamp.drawings
                self.assertGreater(y, stamp.text_top)
                self.assertLessEqual(x + width, stamp.width)
                self.assertLessEqual(y + height, stamp.height)
                self.assertGreater(height, 5)
                self.assertTrue(signing.verify(final.pdf).ok)

    def test_a_countersignature_made_before_the_drawing_reads_as_it_did(self):
        """Documents countersigned before 28/09 state no drawing: read back,
        their /Reason says just what it said."""
        for text, journal in (
            (signing.REASON_EMPLOYER, ""),
            (f"{signing.REASON_EMPLOYER} — journal {self.JOURNAL}", self.JOURNAL),
        ):
            with self.subTest(text=text):
                read = signing.read_reason(text)
                self.assertEqual((read.reservation, read.journal, read.drawing), (None, journal, ""))
        # His own words never pass for the employer's drawing.
        words = f"signature dessinée SHA-256 {'ab' * 32}"
        read = signing.read_reason(signing.employee_reason(words, self.JOURNAL))
        self.assertEqual((read.reservation, read.drawing, read.journal), (words, "", self.JOURNAL))
