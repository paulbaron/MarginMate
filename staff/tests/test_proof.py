"""The proof file (« dossier de preuve », `staff/proof.py`): a PDF a person
can read - courts discarded proof files that were unreadable strings of
codes, and ones not visibly tied to their document. Read back through
pdfplumber, as a reader would."""

import datetime as dt
import io
import shutil
from datetime import date

import pdfplumber

from staff import private_files, proof, signing
from staff import signature_requests as requests_
from staff.models import Establishment, SignatureEvent, SignatureRequest
from staff.tests.signing_support import (
    SigningTestMixin,
    countersign_without_a_drawing,
    drawn_signature,
    employer_signature,
)
from staff.tests.support import employee
from staff.timesheet import save_month
from tests.support import NoNetworkTestCase

JUNE = date(2026, 6, 1)
NOW = dt.datetime(2026, 7, 2, 8, 0, tzinfo=dt.UTC)
IP = "203.0.113.7"


def text_of(data: bytes) -> tuple[str, int]:
    with pdfplumber.open(io.BytesIO(data)) as document:
        return "\n".join(page.extract_text() or "" for page in document.pages), len(document.pages)


def flat(text: str) -> str:
    return " ".join(text.split())


def rewrite_countersigned_detail(request, edit):
    """What somebody who can rewrite the database does to the COUNTERSIGNED
    event, working every hash out again: `edit(detail) -> detail`, then every
    event's hash from the first on and the row's head recomputed with
    `requests_.event_hash` - so the chain still adds up, and only what is
    sealed outside the database can tell."""
    previous = requests_.GENESIS
    for event in SignatureEvent.objects.filter(request=request).order_by("id"):
        if event.kind == SignatureEvent.Kind.COUNTERSIGNED:
            event.detail = edit(dict(event.detail or {}))
        digest = requests_.event_hash(
            request.uuid, previous, event.at, event.kind, event.ip, event.user_agent, event.detail
        )
        SignatureEvent.objects.filter(pk=event.pk).update(detail=event.detail, previous_hash=previous, hash=digest)
        previous = digest
    SignatureRequest.objects.filter(pk=request.pk).update(last_event_hash=previous)
    request.refresh_from_db()
    return request


class ProofTests(SigningTestMixin, NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire")
        self.person = employee()
        save_month(self.person, JUNE, [])
        self.request, _token = requests_.create_request(self.person, JUNE, now=NOW, ip=IP, user_agent="Bureau")

    def sign_everything(self, reservation=""):
        session = {}
        code = requests_.issue_code(self.request, SignatureRequest.Identification.CODE_HANDED_OVER, now=NOW)
        requests_.check_code(self.request, code, session, now=NOW, ip=IP, user_agent="Téléphone d'essai")
        requests_.sign_for_employee(
            self.request,
            drawn_signature(),
            session=session,
            statement_accepted=True,
            reservation=reservation,
            now=NOW + dt.timedelta(minutes=1),
            ip=IP,
            user_agent="Téléphone d'essai",
        )
        return requests_.countersign_request(
            self.request, employer_signature(), now=NOW + dt.timedelta(days=1), ip=IP, user_agent="Bureau"
        )

    def test_the_stored_proof_names_the_document_and_its_hash(self):
        data = private_files.read_checked(self.request.uuid, private_files.PROOF, self.request.proof_sha256)
        text, _pages = text_of(data)
        body = flat(text)
        self.assertIn("Dossier de preuve — signature électronique", body)
        self.assertIn(str(self.request.uuid), body)
        self.assertIn(self.request.document_sha256, body)
        self.assertIn("DUPONT Jeanne", body)
        self.assertIn("juin 2026", body)
        self.assertIn("Demande créée, document figé", body)
        self.assertIn("signature électronique simple", body)
        self.assertIn("eIDAS", body)

    def test_after_both_signatures_everything_is_there(self):
        done = self.sign_everything(reservation="Il manque 2 h le samedi 13.")
        text, _pages = text_of(private_files.read(done.uuid, private_files.PROOF))
        body = flat(text)
        for digest in (
            done.document_sha256,
            done.signature_png_sha256,
            done.employee_pdf_sha256,
            done.final_pdf_sha256,
            requests_.employer_drawing_sha256(done),
        ):
            self.assertIn(digest, body)
        self.assertIn("Je certifie que ce relevé correspond aux heures que j'ai effectuées en juin 2026.", body)
        self.assertIn("Il manque 2 h le samedi 13.", body)
        self.assertIn("affiché à l'employeur", body)  # the code handed over, said honestly
        self.assertIn("http://horodatage.test", body)
        self.assertIn("Code vérifié", body)
        self.assertIn("Signé par le salarié", body)
        self.assertIn("Contresigné par l'employeur", body)
        self.assertIn("203.0.113.7", body)
        self.assertIn("Journal intègre", body)
        self.assertIn(done.last_event_hash, body)
        self.assertIn("02/07/2026 à 10:01", body)  # Paris time
        for word in ("qualifiée", "avancée", "manuscrite"):
            self.assertNotIn(word, body)

    def employee_signs(self):
        session = {}
        code = requests_.issue_code(self.request, SignatureRequest.Identification.CODE_HANDED_OVER, now=NOW)
        requests_.check_code(self.request, code, session, now=NOW, ip=IP, user_agent="Téléphone d'essai")
        return requests_.sign_for_employee(
            self.request,
            drawn_signature(),
            session=session,
            statement_accepted=True,
            now=NOW + dt.timedelta(minutes=1),
            ip=IP,
            user_agent="Téléphone d'essai",
        )

    def test_the_employer_s_drawing_is_named_by_its_hash_and_said_sealed(self):
        """« Signature dessinée de l'employeur : SHA-256 … » - the picture
        kept in the request's folder, whose hash the countersignature seals."""
        body = flat(text_of(private_files.read(self.request.uuid, private_files.PROOF))[0])
        self.assertIn("Signature dessinée par l'employeur (employer_signature.png) : — (pas encore)", body)
        done = self.sign_everything()
        digest = requests_.employer_drawing_sha256(done)
        self.assertTrue(digest)
        body = flat(text_of(private_files.read(done.uuid, private_files.PROOF))[0])
        self.assertIn(f"Signature dessinée par l'employeur (employer_signature.png) : {digest}", body)
        self.assertIn(f"Signature dessinée de l'employeur : SHA-256 {digest}", body)
        self.assertIn("scellée dans la contresignature elle-même", body)
        self.assertNotIn("Anomalie", body)

    def test_an_employer_s_drawing_changed_on_disk_is_said(self):
        done = self.sign_everything()
        path = private_files.request_dir(done.uuid) / private_files.EMPLOYER_SIGNATURE_IMAGE
        path.write_bytes(path.read_bytes() + b"\x00")
        body = flat(text_of(proof.proof_pdf(done))[0])
        self.assertIn("Anomalie : le fichier employer_signature.png ne correspond plus à cette empreinte", body)

    def test_a_request_countersigned_before_the_drawing_keeps_its_proof(self):
        """Countersigned the old way (before 28/09): its proof says what it
        said - every hash, the journal sealed - and not a word about a drawing
        of the employer's that never existed."""
        self.employee_signs()
        done = countersign_without_a_drawing(self.request, now=NOW + dt.timedelta(days=1), ip=IP)
        body = flat(text_of(private_files.read(done.uuid, private_files.PROOF))[0])
        for digest in (
            done.document_sha256,
            done.signature_png_sha256,
            done.employee_pdf_sha256,
            done.final_pdf_sha256,
        ):
            self.assertIn(digest, body)
        self.assertIn("Contresigné par l'employeur", body)
        self.assertIn("ceux d'avant la contresignature de l'employeur sont scellés dans le document signé", body)
        self.assertNotIn("de l'employeur (employer_signature.png)", body)
        self.assertNotIn("Signature dessinée de l'employeur", body)
        self.assertNotIn("Anomalie", body)

    def test_a_journal_naming_a_drawing_the_countersignature_does_not_seal_is_an_anomaly(self):
        """Countersigned before 28/09 - its /Reason seals no drawing, and the
        document is READ - then a drawing planted in the journal, every hash
        worked out again. The proof named the journal's hash as the drawing
        and said the countersigned document « n'a pas pu être relu », which
        was false, with no anomaly (review, 28/09)."""
        self.employee_signs()
        done = countersign_without_a_drawing(self.request, now=NOW + dt.timedelta(days=1), ip=IP)
        planted = private_files.write(
            done.uuid,
            private_files.EMPLOYER_SIGNATURE_IMAGE,
            signing.clean_signature_png(employer_signature(colour=(150, 20, 20, 255))),
        )
        done = rewrite_countersigned_detail(done, lambda detail: {**detail, requests_.EMPLOYER_DRAWING: planted})
        self.assertTrue(requests_.verify_event_chain(done).ok)
        final = private_files.read_checked(done.uuid, private_files.FINAL, done.final_pdf_sha256)
        self.assertEqual(signing.signed_reasons(final)[signing.EMPLOYER_FIELD].drawing, "")
        body = flat(text_of(proof.proof_pdf(done))[0])
        self.assertIn(f"Signature dessinée de l'employeur : SHA-256 {planted}", body)
        self.assertIn(
            "Anomalie : la contresignature ne scelle aucune signature dessinée, le journal en nomme une - le journal "
            "a été altéré. L'empreinte ci-dessus est celle du journal.",
            body,
        )
        self.assertNotIn("n'a pas pu être relu", body)

    def test_a_journal_that_lost_the_sealed_drawing_is_an_anomaly(self):
        """The countersignature seals a drawing; the journal no longer names
        it, every hash worked out again. The proof went on printing the seal
        as « scellée » with no anomaly, and the owner's panel quietly stopped
        offering the PNG (review, 28/09)."""
        done = self.sign_everything()
        sealed = requests_.employer_drawing_sha256(done)
        self.assertTrue(sealed)
        done = rewrite_countersigned_detail(
            done, lambda detail: {key: value for key, value in detail.items() if key != requests_.EMPLOYER_DRAWING}
        )
        self.assertEqual(requests_.employer_drawing_sha256(done), "")
        self.assertTrue(requests_.verify_event_chain(done).ok)
        body = flat(text_of(proof.proof_pdf(done))[0])
        self.assertIn(f"Signature dessinée de l'employeur : SHA-256 {sealed}", body)
        self.assertIn(
            "Anomalie : le journal ne nomme pas la signature dessinée que la contresignature a scellée - le journal "
            "a été altéré. L'empreinte ci-dessus est celle de la contresignature.",
            body,
        )
        self.assertNotIn("scellée dans la contresignature elle-même", body)

    def test_his_reservations_are_printed_as_he_signed_them_and_an_edited_row_is_said(self):
        """His words come from his signed document, not from a database
        column anyone with the database can rewrite (review, 28/09: the row
        rewritten to « RAS », the proof printed « Réserves : RAS »)."""
        words = "Le 12, j'ai fini à 23 h 30 et non à 23 h."
        done = self.sign_everything(reservation=words)
        SignatureRequest.objects.filter(pk=done.pk).update(reservation="RAS")
        done.refresh_from_db()
        body = flat(text_of(proof.proof_pdf(done))[0])
        self.assertIn(f"Réserves : {words}", body)
        self.assertNotIn("Réserves : RAS", body)
        self.assertIn("altérées", body)

    def test_a_row_that_no_longer_matches_its_journal_is_said(self):
        done = self.sign_everything()
        SignatureRequest.objects.filter(pk=done.pk).update(identification=SignatureRequest.Identification.CODE_BY_EMAIL)
        done.refresh_from_db()
        body = flat(text_of(proof.proof_pdf(done))[0])
        self.assertIn("ne correspondent plus au journal", body)
        self.assertIn("méthode d'identification", body)

    def test_the_proof_names_the_authority_that_issued_this_document_s_certificates(self):
        """Recorded when he signed, not read from the keys folder today
        (review, 28/09): the folder lost or moved, the next signature makes
        a new authority, and every older proof named it."""
        done = self.sign_everything()
        first = signing.authority_fingerprint()
        shutil.rmtree(self.private_dir / "keys")
        other = employee(last_name="Martin", first_name="Paul")
        save_month(other, JUNE, [])
        second, _token = requests_.create_request(other, JUNE, now=NOW)
        session = {}
        requests_.check_code(second, requests_.issue_code(second, "code_remis", now=NOW), session, now=NOW)
        requests_.sign_for_employee(second, drawn_signature(), session=session, statement_accepted=True, now=NOW)
        now = signing.authority_fingerprint()
        self.assertNotEqual(now, first)
        body = flat(text_of(proof.proof_pdf(done))[0])
        self.assertIn(first, body)
        self.assertIn("n'est plus celle-ci", body)
        self.assertIn(now, body)

    def test_what_the_journal_s_integrity_does_and_does_not_show(self):
        """No more than it shows (review, 28/09): a chain in the database
        does not resist whoever can rewrite the database; what is anchored
        outside it is said as such."""
        done = self.sign_everything()
        body = flat(text_of(private_files.read(done.uuid, private_files.PROOF))[0])
        self.assertIn("ne résiste pas, à elle seule, à qui peut réécrire la base de données", body)
        self.assertIn("scellée dans la signature", body)

    def test_an_altered_log_is_said_in_the_proof(self):
        SignatureEvent.objects.filter(request=self.request).update(user_agent="changé")
        text, _pages = text_of(proof.proof_pdf(self.request))
        self.assertIn("Journal altéré", flat(text))

    def test_a_long_log_runs_onto_more_pages_each_carrying_the_id(self):
        for index in range(80):
            requests_.log_event(
                self.request,
                SignatureEvent.Kind.LINK_OPENED,
                ip=IP,
                user_agent=f"Navigateur d'essai {index}",
                at=NOW + dt.timedelta(seconds=index),
            )
        data = proof.proof_pdf(self.request)
        with pdfplumber.open(io.BytesIO(data)) as document:
            self.assertGreater(len(document.pages), 1)
            for number, page in enumerate(document.pages, start=1):
                footer = flat(page.extract_text())
                self.assertIn(str(self.request.uuid), footer)
                self.assertIn(f"page {number} / {len(document.pages)}", footer)
        self.assertIn("Navigateur d'essai 79", flat(text_of(data)[0]))
