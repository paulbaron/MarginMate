"""The monthly signature, step by step (`staff/signature_requests.py`):
the request and its frozen month, the secret link (only its hash is kept),
the one-time code, the employee's signature and the employer's, a month
read-only while a request holds it, and « Corriger ce mois ». Invented names
only; keys and files in a temp folder; no network."""

import datetime as dt
import hashlib
import io
import json
from datetime import date
from decimal import Decimal
from unittest import mock

import pdfplumber
from django.template.loader import render_to_string
from django.test import override_settings
from django.utils import timezone

from staff import private_files, signing
from staff import signature_requests as requests_
from staff.models import Establishment, SignatureEvent, SignatureRequest, Timesheet, TimesheetDay
from staff.tests.signing_support import (
    FailingTimestamper,
    OfflineTimestamps,
    SigningTestMixin,
    blank_canvas,
    countersign_without_a_drawing,
    drawn_signature,
    employer_signature,
    png_metadata,
)
from staff.tests.support import employee
from staff.timesheet import PostedDay, read_posted_month, save_month
from tests.support import NoNetworkTestCase

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
NOW = dt.datetime(2026, 7, 2, 8, 0, tzinfo=dt.UTC)
IP = "203.0.113.7"  # TEST-NET-3: an address that belongs to nobody
PHONE = "Mozilla/5.0 (Linux; Android 14) Essai/1.0"

Status = SignatureRequest.Status
Kind = SignatureEvent.Kind
Identification = SignatureRequest.Identification


def pdf_text(data: bytes) -> str:
    """A PDF's text as a reader gets it, spaces single."""
    with pdfplumber.open(io.BytesIO(data)) as document:
        return " ".join(" ".join(page.extract_text() or "" for page in document.pages).split())


class RequestCase(SigningTestMixin, NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.bar = Establishment.objects.create(
            pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS"
        )
        self.person = employee()
        self.person.email = "jeanne.dupont@example.invalid"
        self.person.save()
        save_month(self.person, JUNE, [])
        self.timesheet = Timesheet.objects.get(employee=self.person, month=JUNE)
        self.session = {}

    def create(self, person=None, month=JUNE, now=NOW):
        return requests_.create_request(person or self.person, month, now=now, ip=IP, user_agent="Bureau")

    def identified(self, request, now=NOW, session=None):
        session = self.session if session is None else session
        code = requests_.issue_code(request, SignatureRequest.Identification.CODE_HANDED_OVER, now=now)
        requests_.check_code(request, code, session, now=now, ip=IP, user_agent=PHONE)
        return session

    def employee_signs(self, request, now=NOW, reservation="", session=None):
        session = self.identified(request, now=now, session=session)
        return requests_.sign_for_employee(
            request,
            drawn_signature(),
            session=session,
            statement_accepted=True,
            reservation=reservation,
            now=now + dt.timedelta(minutes=2),
            ip=IP,
            user_agent=PHONE,
        )

    def kinds(self, request):
        return list(request.events.values_list("kind", flat=True))


class CreateTests(RequestCase):
    def test_an_unsaved_month_is_never_sent(self):
        """A planning is not a record: the typical week of a month nobody
        saved cannot be signed."""
        with self.assertRaises(requests_.NotSignable) as caught:
            self.create(month=JULY)
        self.assertIn("pas enregistré", str(caught.exception))
        self.assertFalse(SignatureRequest.objects.exists())

    def test_the_request_freezes_the_month(self):
        request, _token = self.create()
        self.assertEqual(request.version, 1)
        self.assertEqual(request.status, Status.PENDING)
        self.assertEqual(request.created_at, NOW)
        self.assertEqual(request.expires_at, NOW + dt.timedelta(days=14))
        frozen = private_files.read(request.uuid, private_files.DOCUMENT)
        self.assertEqual(hashlib.sha256(frozen).hexdigest(), request.document_sha256)
        self.assertEqual(signing.verify(frozen).unsigned_fields, (signing.EMPLOYEE_FIELD, signing.EMPLOYER_FIELD))
        self.assertEqual(self.kinds(request), [Kind.CREATED])
        self.assertTrue(private_files.exists(request.uuid, private_files.PROOF))
        self.assertEqual(
            hashlib.sha256(private_files.read(request.uuid, private_files.PROOF)).hexdigest(), request.proof_sha256
        )

    def test_the_token_is_never_stored(self):
        request, token = self.create()
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(request.token_hash, hashlib.sha256(token.encode()).hexdigest())
        row = json.dumps(list(SignatureRequest.objects.values()), default=str)
        events = json.dumps(list(SignatureEvent.objects.values()), default=str)
        self.assertNotIn(token, row)
        self.assertNotIn(token, events)

    def test_the_snapshot_is_the_month_as_sent(self):
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("9"), note="inventaire")])
        request, _token = self.create()
        snapshot = request.month_snapshot
        self.assertEqual(snapshot["employee"]["name"], "DUPONT Jeanne")
        self.assertEqual(snapshot["establishment"]["name"], "BAR EXEMPLE")
        self.assertEqual(snapshot["title"], "Mois de juin 2026")
        days = [day for week in snapshot["weeks"] for day in week["days"]]
        self.assertEqual(len(days), 30)
        tuesday = next(day for day in days if day["date"] == "2026-06-02")
        self.assertEqual((tuesday["name"], tuesday["hours"], tuesday["note"]), ("Mardi 2", "9", "inventaire"))
        self.assertEqual(snapshot["weeks"][0]["total"], "37,5")
        self.assertIn(["Heures travaillées", "153 h"], snapshot["summary"]["hours"])

    def test_the_snapshot_does_not_follow_later_edits(self):
        request, _token = self.create()
        requests_.reopen_month(self.timesheet, now=NOW)
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("10"))])
        request.refresh_from_db()
        tuesday = request.month_snapshot["weeks"][0]["days"][1]
        self.assertEqual(tuesday["hours"], "7,5")
        second, _token = self.create()
        self.assertEqual(second.version, 2)
        self.assertEqual(second.month_snapshot["weeks"][0]["days"][1]["hours"], "10")

    def test_one_open_request_per_month(self):
        self.create()
        with self.assertRaises(requests_.RequestStateError):
            self.create()
        self.assertEqual(SignatureRequest.objects.count(), 1)

    def test_no_establishment_name_no_request(self):
        self.bar.name = ""
        self.bar.save()
        with self.assertRaises(requests_.NotSignable) as caught:
            self.create()
        self.assertIn("nom de l'établissement", str(caught.exception))


class LinkTests(RequestCase):
    def test_the_link_finds_its_request_and_nothing_else(self):
        request, token = self.create()
        self.assertEqual(requests_.resolve_link(token, now=NOW).pk, request.pk)
        for wrong in ("", "x", token[:-1], token + "a", "../" + token, "a" * 500, None):
            with self.subTest(token=str(wrong)[:12]):
                with self.assertRaises(requests_.LinkError) as caught:
                    requests_.resolve_link(wrong, now=NOW)
                self.assertEqual(caught.exception.status, 404)

    def test_an_expired_link_is_gone_and_the_request_marked_once(self):
        request, token = self.create()
        after = NOW + dt.timedelta(days=14, seconds=1)
        for _ in range(2):
            with self.assertRaises(requests_.LinkError) as caught:
                requests_.resolve_link(token, now=after)
            self.assertEqual(caught.exception.status, 410)
            self.assertIn("expiré", str(caught.exception))
        request.refresh_from_db()
        self.assertEqual(request.status, Status.EXPIRED)
        self.assertEqual(self.kinds(request).count(Kind.EXPIRED), 1)
        # The last second of the fortnight still works.
        other, token_2 = self.create()
        self.assertEqual(requests_.resolve_link(token_2, now=other.expires_at - dt.timedelta(seconds=1)).pk, other.pk)

    def test_a_cancelled_or_replaced_request_is_gone(self):
        request, token = self.create()
        requests_.cancel_request(request, "erreur de mois", now=NOW)
        with self.assertRaises(requests_.LinkError) as caught:
            requests_.resolve_link(token, now=NOW)
        self.assertEqual(caught.exception.status, 410)
        self.assertIn("annulée", str(caught.exception))

    def test_after_completion_the_link_still_offers_the_final_copy_until_it_expires(self):
        request, token = self.create()
        self.employee_signs(request)
        requests_.countersign_request(
            request, employer_signature(), now=NOW + dt.timedelta(days=13), ip=IP, user_agent="Bureau"
        )
        request.refresh_from_db()
        self.assertEqual(request.status, Status.COMPLETE)
        # Countersigned on the 13th day: the link gets a new fortnight for the copy.
        self.assertEqual(request.expires_at, NOW + dt.timedelta(days=27))
        self.assertEqual(requests_.resolve_link(token, now=NOW + dt.timedelta(days=20)).pk, request.pk)
        with self.assertRaises(requests_.LinkError) as caught:
            requests_.resolve_link(token, now=NOW + dt.timedelta(days=28))
        self.assertEqual(caught.exception.status, 410)
        request.refresh_from_db()
        self.assertEqual(request.status, Status.COMPLETE, "a finished request never becomes « expirée »")

    def test_a_new_link_replaces_the_old_one(self):
        request, old = self.create()
        new = requests_.renew_link(request, now=NOW + dt.timedelta(days=10))
        self.assertNotEqual(new, old)
        with self.assertRaises(requests_.LinkError):
            requests_.resolve_link(old, now=NOW + dt.timedelta(days=10))
        self.assertEqual(requests_.resolve_link(new, now=NOW + dt.timedelta(days=20)).pk, request.pk)
        self.assertIn(Kind.LINK_RENEWED, self.kinds(request))

    def test_the_link_is_absolute(self):
        path = "/personnel/signer/abc/"
        with override_settings(SITE_URL="https://bar.example.invalid"):
            self.assertEqual(requests_.absolute_link(path), "https://bar.example.invalid/personnel/signer/abc/")
        self.assertEqual(
            requests_.absolute_link(path, lambda value: "http://testserver" + value), "http://testserver" + path
        )

    def test_opening_is_logged_once_an_hour_per_device(self):
        """Not once per session: a client that keeps no cookie - a script, a
        link preview - wrote an event at every hit."""
        request, _token = self.create()
        for _ in range(3):
            requests_.note_link_opened(request, ip=IP, user_agent=PHONE, now=NOW)
        requests_.note_link_opened(request, ip="198.51.100.4", user_agent=PHONE, now=NOW)
        requests_.note_link_opened(request, ip=IP, user_agent=PHONE, now=NOW + dt.timedelta(minutes=61))
        self.assertEqual(self.kinds(request).count(Kind.LINK_OPENED), 3)
        opened = request.events.filter(kind=Kind.LINK_OPENED).first()
        self.assertEqual((opened.ip, opened.user_agent), (IP, PHONE))

    def test_ten_openings_an_hour_at_most_whatever_the_devices_say(self):
        request, _token = self.create()
        for number in range(30):
            requests_.note_link_opened(request, ip=IP, user_agent=f"Robot/{number}", now=NOW)
        self.assertEqual(self.kinds(request).count(Kind.LINK_OPENED), 10)
        requests_.note_link_opened(request, ip=IP, user_agent="Robot/0", now=NOW + dt.timedelta(minutes=61))
        self.assertEqual(self.kinds(request).count(Kind.LINK_OPENED), 11)


class LockTests(RequestCase):
    def test_the_month_is_read_only_while_a_request_holds_it(self):
        self.assertFalse(requests_.month_is_locked(self.timesheet, now=NOW))
        request, _token = self.create()
        self.assertTrue(requests_.month_is_locked(self.timesheet, now=NOW))
        self.employee_signs(request)
        self.assertTrue(requests_.month_is_locked(self.timesheet, now=NOW))
        requests_.countersign_request(request, employer_signature(), now=NOW, ip=IP, user_agent="Bureau")
        # Finished, it holds the month for good - its link's expiry changes nothing.
        self.assertTrue(requests_.month_is_locked(self.timesheet, now=NOW + dt.timedelta(days=400)))
        self.assertEqual(requests_.open_request(self.timesheet, now=NOW).pk, request.pk)
        self.assertFalse(requests_.month_is_locked(None))

    def test_an_expired_request_frees_the_month(self):
        self.create()
        self.assertFalse(requests_.month_is_locked(self.timesheet, now=NOW + dt.timedelta(days=15)))

    def test_correcting_a_waiting_month_cancels_its_request_and_keeps_its_files(self):
        request, _token = self.create()
        changed = requests_.reopen_month(self.timesheet, now=NOW, ip=IP, user_agent="Bureau")
        self.assertEqual(changed.pk, request.pk)
        request.refresh_from_db()
        self.assertEqual(request.status, Status.CANCELLED)
        self.assertIn("corrigé", request.cancelled_reason)
        self.assertTrue(private_files.exists(request.uuid, private_files.DOCUMENT))
        self.assertFalse(requests_.month_is_locked(self.timesheet))
        self.assertIn(Kind.CANCELLED, self.kinds(request))
        self.assertEqual(self.create()[0].version, 2)

    def test_correcting_a_finished_month_supersedes_its_request(self):
        request, _token = self.create()
        self.employee_signs(request)
        requests_.countersign_request(request, employer_signature(), now=NOW, ip=IP, user_agent="Bureau")
        requests_.reopen_month(self.timesheet, now=NOW)
        request.refresh_from_db()
        self.assertEqual(request.status, Status.SUPERSEDED)
        for name in (private_files.DOCUMENT, private_files.EMPLOYEE_SIGNED, private_files.FINAL, private_files.PROOF):
            self.assertTrue(private_files.exists(request.uuid, name), name)
        self.assertIn(Kind.SUPERSEDED, self.kinds(request))

    def test_nothing_to_correct_is_none(self):
        self.assertIsNone(requests_.reopen_month(self.timesheet, now=NOW))

    def test_a_held_month_refuses_every_write_whatever_the_page_posts(self):
        """Read-only is enforced where the month is written
        (`timesheet._store`), not only by a form drawn disabled: a stale
        page, or a POST typed by hand, writes nothing either."""
        from staff import timesheet as sheets

        self.create(now=timezone.now())
        before = list(self.timesheet.days.values_list("date", "hours", "kind", "note"))
        writes = {
            "save_month": lambda: sheets.save_month(
                self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("9"))]
            ),
            "apply_range": lambda: sheets.apply_range(self.person, JUNE, date(2026, 6, 2), date(2026, 6, 3), "conges"),
            "reset_to_typical_week": lambda: sheets.reset_to_typical_week(self.person, JUNE),
        }
        for name, write in writes.items():
            with self.subTest(write=name), self.assertRaises(sheets.MonthLocked) as caught:
                write()
            self.assertIn("Corriger ce mois", str(caught.exception))
        self.assertEqual(list(self.timesheet.days.values_list("date", "hours", "kind", "note")), before)
        # Another month of the same employee is not held.
        sheets.save_month(self.person, JULY, [])
        # Corrected, June is written again.
        requests_.reopen_month(self.timesheet)
        sheets.save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("9"))])
        self.assertEqual(self.timesheet.days.get(date=date(2026, 6, 2)).hours, Decimal("9"))

    def test_the_holidays_of_a_held_month_are_not_touched_either(self):
        from staff import timesheet as sheets

        save_month(self.person, date(2026, 5, 1), [])
        requests_.create_request(self.person, date(2026, 5, 1), now=timezone.now())
        with self.assertRaises(sheets.MonthLocked):
            sheets.mark_holidays_off(self.person, date(2026, 5, 1))


class CodeTests(RequestCase):
    def test_a_six_digit_code_kept_as_an_hmac(self):
        request, _token = self.create()
        code = requests_.issue_code(request, SignatureRequest.Identification.CODE_HANDED_OVER, now=NOW)
        self.assertRegex(code, r"^[0-9]{6}$")
        request.refresh_from_db()
        self.assertNotIn(code, request.code_hash)
        self.assertNotEqual(request.code_hash, hashlib.sha256(code.encode()).hexdigest())
        self.assertEqual(request.code_hash, requests_.code_hash(request, code))
        # The code's method waits beside it; nothing is identified until it is typed.
        self.assertEqual(request.code_method, SignatureRequest.Identification.CODE_HANDED_OVER)
        self.assertEqual(request.identification, "")
        self.assertEqual(request.code_sent_at, NOW)
        self.assertIn(Kind.CODE_GIVEN, self.kinds(request))
        events = json.dumps(list(SignatureEvent.objects.values()), default=str)
        self.assertNotIn(code, events)

    def test_the_right_code_identifies_once(self):
        request, _token = self.create()
        code = requests_.issue_code(request, SignatureRequest.Identification.CODE_HANDED_OVER, now=NOW)
        requests_.check_code(request, f" {code[:3]} {code[3:]} ", self.session, now=NOW, ip=IP, user_agent=PHONE)
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW))
        request.refresh_from_db()
        self.assertEqual(request.code_verified_at, NOW)
        self.assertEqual(request.code_hash, "")
        with self.assertRaises(requests_.CodeError):
            requests_.check_code(request, code, {}, now=NOW, ip=IP, user_agent=PHONE)
        self.assertIn(Kind.CODE_VERIFIED, self.kinds(request))

    def test_five_wrong_attempts_and_the_code_is_gone(self):
        request, _token = self.create()
        code = requests_.issue_code(request, SignatureRequest.Identification.CODE_HANDED_OVER, now=NOW)
        wrong = "000000" if code != "000000" else "111111"
        for attempt in range(1, 6):
            with self.assertRaises(requests_.CodeError) as caught:
                requests_.check_code(request, wrong, self.session, now=NOW, ip=IP, user_agent=PHONE)
            if attempt < 5:
                self.assertIn(f"{5 - attempt} essai", str(caught.exception))
        with self.assertRaises(requests_.CodeError) as caught:
            requests_.check_code(request, code, self.session, now=NOW, ip=IP, user_agent=PHONE)
        self.assertIn("nouveau code", str(caught.exception))
        self.assertFalse(requests_.is_identified(self.session, request))
        self.assertEqual(self.kinds(request).count(Kind.CODE_FAILED), 6)

    def test_a_refusal_before_any_comparison_is_logged_once_an_hour_per_device(self):
        """Every code compared is an event; « aucun code en cours », posted
        in a loop, was one each time too."""
        request, _token = self.create()
        for _ in range(20):
            with self.assertRaises(requests_.CodeError) as caught:
                requests_.check_code(request, "123456", self.session, now=NOW, ip=IP, user_agent=PHONE)
            self.assertIn("Aucun code en cours", str(caught.exception))
        failed = request.events.filter(kind=Kind.CODE_FAILED)
        self.assertEqual([event.detail["reason"] for event in failed], ["aucun code en cours"])
        code = requests_.issue_code(request, Identification.CODE_HANDED_OVER, now=NOW)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(7):
            with self.assertRaises(requests_.CodeError):
                requests_.check_code(request, wrong, self.session, now=NOW, ip=IP, user_agent=PHONE)
        reasons = [event.detail["reason"] for event in request.events.filter(kind=Kind.CODE_FAILED)]
        self.assertEqual(sum(reason.startswith("code erroné") for reason in reasons), 5)
        # The fifth used the code up: the last two found none, said already.
        self.assertEqual(reasons.count("aucun code en cours"), 1)
        self.assertEqual(len(reasons), 6)

    def test_a_code_lasts_fifteen_minutes(self):
        request, _token = self.create()
        code = requests_.issue_code(request, SignatureRequest.Identification.CODE_HANDED_OVER, now=NOW)
        with self.assertRaises(requests_.CodeError) as caught:
            requests_.check_code(
                request, code, self.session, now=NOW + dt.timedelta(minutes=15, seconds=1), ip=IP, user_agent=PHONE
            )
        self.assertIn("expiré", str(caught.exception))
        requests_.check_code(
            request,
            requests_.issue_code(request, "code_remis", now=NOW + dt.timedelta(minutes=16)),
            self.session,
            now=NOW + dt.timedelta(minutes=30),
            ip=IP,
            user_agent=PHONE,
        )
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW + dt.timedelta(minutes=30)))

    def test_a_verified_code_identifies_for_an_hour_only(self):
        """Not for the session's two weeks: on a shared phone, whoever
        reopens the link days later must not sign in his name."""
        request, _token = self.create()
        self.identified(request)
        request.refresh_from_db()
        window = requests_.IDENTIFICATION_VALIDITY
        self.assertEqual(window, dt.timedelta(hours=1))
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW + window))
        self.assertFalse(requests_.is_identified(self.session, request, now=NOW + window + dt.timedelta(seconds=1)))
        with self.assertRaises(requests_.IdentificationRequired):
            requests_.sign_for_employee(
                request,
                drawn_signature(),
                session=self.session,
                statement_accepted=True,
                now=NOW + dt.timedelta(hours=2),
                ip=IP,
                user_agent=PHONE,
            )
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)
        # A new code, a new hour.
        self.identified(request, now=NOW + dt.timedelta(hours=2))
        request.refresh_from_db()
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW + dt.timedelta(hours=2, minutes=59)))

    def test_three_codes_an_hour(self):
        request, _token = self.create()
        for minute in (0, 10, 20):
            requests_.issue_code(request, "code_remis", now=NOW + dt.timedelta(minutes=minute))
        with self.assertRaises(requests_.CodeError) as caught:
            requests_.issue_code(request, "code_remis", now=NOW + dt.timedelta(minutes=30))
        self.assertIn("3 codes", str(caught.exception))
        requests_.issue_code(request, "code_remis", now=NOW + dt.timedelta(minutes=61))

    def test_a_new_code_replaces_the_previous_one(self):
        request, _token = self.create()
        first = requests_.issue_code(request, "code_remis", now=NOW)
        second = requests_.issue_code(request, "code_remis", now=NOW)
        if first != second:
            with self.assertRaises(requests_.CodeError):
                requests_.check_code(request, first, self.session, now=NOW, ip=IP, user_agent=PHONE)
        requests_.check_code(request, second, self.session, now=NOW, ip=IP, user_agent=PHONE)

    def test_a_code_verified_for_one_request_unlocks_no_other(self):
        request, _token = self.create()
        other_person = employee(last_name="Martin", first_name="Paul")
        save_month(other_person, JUNE, [])
        other, _token_2 = self.create(person=other_person)
        self.identified(request)
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW))
        self.assertFalse(requests_.is_identified(self.session, other))
        with self.assertRaises(requests_.IdentificationRequired):
            requests_.sign_for_employee(
                other,
                drawn_signature(),
                session=self.session,
                statement_accepted=True,
                now=NOW,
                ip=IP,
                user_agent=PHONE,
            )
        other.refresh_from_db()
        self.assertEqual(other.status, Status.PENDING)

    def test_no_code_for_a_request_that_is_not_waiting(self):
        request, _token = self.create()
        requests_.cancel_request(request, "essai", now=NOW)
        with self.assertRaises(requests_.RequestStateError):
            requests_.issue_code(request, "code_remis", now=NOW)

    def test_an_unknown_method_is_refused(self):
        request, _token = self.create()
        with self.assertRaises(ValueError):
            requests_.issue_code(request, "sms", now=NOW)


class EmployeeSignatureTests(RequestCase):
    def test_signed_stored_hashed_logged_and_verifiable(self):
        request, _token = self.create()
        signed = self.employee_signs(request, reservation="Il manque 2 h le samedi 13.")
        self.assertEqual(signed.status, Status.EMPLOYEE_SIGNED)
        self.assertEqual(signed.employee_signed_at, NOW + dt.timedelta(minutes=2))
        self.assertEqual(signed.reservation, "Il manque 2 h le samedi 13.")
        self.assertEqual(signed.statement_version, requests_.CURRENT_STATEMENT)
        self.assertEqual(signed.employee_timestamp_authority, "http://horodatage.test")
        self.assertIsNotNone(signed.employee_timestamp_at)
        pdf_bytes = private_files.read_checked(signed.uuid, private_files.EMPLOYEE_SIGNED, signed.employee_pdf_sha256)
        private_files.read_checked(signed.uuid, private_files.SIGNATURE_IMAGE, signed.signature_png_sha256)
        self.assertTrue(signing.verify(pdf_bytes).ok)
        self.assertEqual(self.kinds(signed)[-1], Kind.EMPLOYEE_SIGNED)
        event = signed.events.get(kind=Kind.EMPLOYEE_SIGNED)
        self.assertEqual((event.ip, event.user_agent), (IP, PHONE))
        self.assertEqual(event.detail["signed_sha256"], signed.employee_pdf_sha256)
        self.assertEqual(
            hashlib.sha256(private_files.read(signed.uuid, private_files.PROOF)).hexdigest(), signed.proof_sha256
        )

    def test_the_certification_must_be_ticked(self):
        request, _token = self.create()
        session = self.identified(request)
        with self.assertRaises(requests_.RequestError) as caught:
            requests_.sign_for_employee(
                request, drawn_signature(), session=session, statement_accepted=False, now=NOW, ip=IP, user_agent=PHONE
            )
        self.assertIn("Je certifie", str(caught.exception))
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)

    def test_an_empty_drawing_stores_nothing(self):
        import io

        from PIL import Image

        request, _token = self.create()
        session = self.identified(request)
        blank = io.BytesIO()
        Image.new("RGBA", (600, 200), (0, 0, 0, 0)).save(blank, format="PNG")
        with self.assertRaises(signing.SignatureImageError):
            requests_.sign_for_employee(
                request, blank.getvalue(), session=session, statement_accepted=True, now=NOW, ip=IP, user_agent=PHONE
            )
        self.assertFalse(private_files.exists(request.uuid, private_files.SIGNATURE_IMAGE))
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)

    def test_no_timestamp_no_signature_and_nothing_stored(self):
        request, _token = self.create()
        session = self.identified(request)
        with OfflineTimestamps(stampers=[FailingTimestamper("http://a.test"), FailingTimestamper("http://b.test")]):
            with self.assertRaises(signing.TimestampUnavailable) as caught:
                requests_.sign_for_employee(
                    request,
                    drawn_signature(),
                    session=session,
                    statement_accepted=True,
                    now=NOW,
                    ip=IP,
                    user_agent=PHONE,
                )
        self.assertIn("réessayez dans quelques minutes", str(caught.exception))
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)
        self.assertEqual(request.employee_pdf_sha256, "")
        for name in (private_files.SIGNATURE_IMAGE, private_files.EMPLOYEE_SIGNED):
            self.assertFalse(private_files.exists(request.uuid, name))
        failed = request.events.get(kind=Kind.TIMESTAMP_FAILED)
        self.assertEqual(failed.detail["servers"], ["http://a.test", "http://b.test"])
        # And once a server answers again, the same identification signs.
        signed = requests_.sign_for_employee(
            request, drawn_signature(), session=session, statement_accepted=True, now=NOW, ip=IP, user_agent=PHONE
        )
        self.assertEqual(signed.status, Status.EMPLOYEE_SIGNED)

    def test_a_frozen_document_changed_on_disk_is_never_signed(self):
        request, _token = self.create()
        session = self.identified(request)
        path = private_files.request_dir(request.uuid) / private_files.DOCUMENT
        path.write_bytes(path.read_bytes() + b"\n% ajout")
        with (
            self.assertRaises(requests_.RequestError) as caught,
            self.assertLogs("staff.signature_requests", "WARNING"),
        ):
            requests_.sign_for_employee(
                request, drawn_signature(), session=session, statement_accepted=True, now=NOW, ip=IP, user_agent=PHONE
            )
        self.assertEqual(str(caught.exception), requests_.DOCUMENT_CHANGED)
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)

    def test_signed_once_only(self):
        request, _token = self.create()
        self.employee_signs(request)
        with self.assertRaises(requests_.RequestStateError) as caught:
            requests_.sign_for_employee(
                request,
                drawn_signature(),
                session=self.session,
                statement_accepted=True,
                now=NOW,
                ip=IP,
                user_agent=PHONE,
            )
        self.assertIn("déjà signé", str(caught.exception))

    def test_an_expired_request_cannot_be_signed(self):
        request, _token = self.create()
        session = self.identified(request)
        with self.assertRaises(requests_.RequestStateError):
            requests_.sign_for_employee(
                request,
                drawn_signature(),
                session=session,
                statement_accepted=True,
                now=NOW + dt.timedelta(days=15),
                ip=IP,
                user_agent=PHONE,
            )


class CountersignTests(RequestCase):
    def test_countersigned_after_the_employee(self):
        request, _token = self.create()
        with self.assertRaises(requests_.RequestStateError):
            requests_.countersign_request(request, employer_signature(), now=NOW, ip=IP, user_agent="Bureau")
        self.employee_signs(request)
        done = requests_.countersign_request(
            request, employer_signature(), now=NOW + dt.timedelta(days=1), ip=IP, user_agent="Bureau"
        )
        self.assertEqual(done.status, Status.COMPLETE)
        self.assertEqual(done.employer_signed_at, NOW + dt.timedelta(days=1))
        final = private_files.read_checked(done.uuid, private_files.FINAL, done.final_pdf_sha256)
        result = signing.verify(final)
        self.assertTrue(result.ok, result.verdict)
        self.assertEqual(len(result.signatures), 2)
        self.assertEqual(self.kinds(done)[-1], Kind.COUNTERSIGNED)
        with self.assertRaises(requests_.RequestStateError):
            requests_.countersign_request(done, employer_signature(), now=NOW, ip=IP, user_agent="Bureau")

    def test_verify_request_checks_the_latest_file_and_logs_it(self):
        request, _token = self.create()
        self.assertIn("Aucune signature", requests_.verify_request(request, ip=IP, user_agent="Bureau").verdict)
        self.employee_signs(request)
        request.refresh_from_db()
        result = requests_.verify_request(request, ip=IP, user_agent="Bureau")
        self.assertTrue(result.ok)
        self.assertEqual(request.events.filter(kind=Kind.VERIFIED).count(), 2)
        self.assertTrue(request.events.filter(kind=Kind.VERIFIED).last().detail["ok"])

    def test_the_latest_document(self):
        request, _token = self.create()
        self.assertEqual(requests_.latest_document(request)[0], private_files.DOCUMENT)
        self.employee_signs(request)
        request.refresh_from_db()
        self.assertEqual(requests_.latest_document(request)[0], private_files.EMPLOYEE_SIGNED)
        requests_.countersign_request(request, employer_signature(), now=NOW, ip=IP, user_agent="Bureau")
        request.refresh_from_db()
        name, data = requests_.latest_document(request)
        self.assertEqual(name, private_files.FINAL)
        self.assertEqual(hashlib.sha256(data).hexdigest(), request.final_pdf_sha256)


class EmployerDrawingTests(RequestCase):
    """« Contresigner » posts the employer's drawing (the owner, 28/09): the
    drawing is required, goes through the employee's checks, is kept in the
    request's private folder, and its SHA-256 is recorded in the COUNTERSIGNED
    event AND sealed in the countersignature - no new column, so no
    migration of a database the owner had just migrated."""

    def signed(self):
        request, _token = self.create()
        self.employee_signs(request)
        request.refresh_from_db()
        return request

    def countersign(self, request, png):
        return requests_.countersign_request(request, png, now=NOW + dt.timedelta(days=1), ip=IP, user_agent="Bureau")

    def test_kept_hashed_logged_and_sealed(self):
        request = self.signed()
        posted = employer_signature(pnginfo=png_metadata())
        done = self.countersign(request, posted)
        event = done.events.get(kind=Kind.COUNTERSIGNED)
        digest = event.detail[requests_.EMPLOYER_DRAWING]
        kept = private_files.read_checked(done.uuid, private_files.EMPLOYER_SIGNATURE_IMAGE, digest)
        # The picture the employee's checks keep: encoded again, its chunks dropped.
        self.assertEqual(kept, signing.clean_signature_png(posted))
        self.assertNotIn(b"Auteur imaginaire", kept)
        self.assertEqual(hashlib.sha256(kept).hexdigest(), digest)
        self.assertEqual(requests_.employer_drawing_sha256(done), digest)
        # Sealed in the countersignature, under its timestamp.
        final = private_files.read_checked(done.uuid, private_files.FINAL, done.final_pdf_sha256)
        self.assertEqual(signing.signed_reasons(final)[signing.EMPLOYER_FIELD].drawing, digest)
        # The employee's own drawing is untouched, and not the employer's.
        self.assertNotEqual(done.signature_png_sha256, digest)
        private_files.read_checked(done.uuid, private_files.SIGNATURE_IMAGE, done.signature_png_sha256)
        self.assertTrue(requests_.verify_event_chain(done).ok)
        self.assertIn(f"signature dessinée de l'employeur : SHA-256 {digest}", requests_.describe_event(event).details)

    def test_no_drawing_no_countersignature_and_nothing_stored(self):
        request = self.signed()
        before = self.kinds(request)
        for png, words in (
            (None, signing.EMPLOYER_DRAWING_MISSING),
            (b"", signing.EMPLOYER_DRAWING_MISSING),
            (blank_canvas(), signing.EMPLOYER_DRAWING_EMPTY),
            (b"GIF89a pas un PNG", "image PNG"),
            (drawn_signature(width=1201, height=300), "trop grande"),
        ):
            with self.subTest(png=png[:10] if png else png):
                with self.assertRaises(signing.SignatureImageError) as caught:
                    self.countersign(request, png)
                self.assertIn(words, str(caught.exception))
        request.refresh_from_db()
        self.assertEqual(request.status, Status.EMPLOYEE_SIGNED)
        self.assertEqual(request.final_pdf_sha256, "")
        for name in (private_files.FINAL, private_files.EMPLOYER_SIGNATURE_IMAGE):
            self.assertFalse(private_files.exists(request.uuid, name))
        self.assertEqual(self.kinds(request), before)
        self.assertEqual(requests_.employer_drawing_sha256(request), "")

    def test_a_request_not_signed_yet_says_that_first(self):
        """A page drawn before the employee signed posts no drawing: what it
        is told is the step, not the drawing."""
        request, _token = self.create()
        with self.assertRaises(requests_.RequestStateError) as caught:
            self.countersign(request, None)
        self.assertEqual(str(caught.exception), requests_.COUNTERSIGN_TOO_EARLY)

    def test_no_timestamp_no_countersignature_and_no_drawing_kept(self):
        request = self.signed()
        with OfflineTimestamps(stampers=[FailingTimestamper("http://a.test")]):
            with self.assertRaises(signing.TimestampUnavailable):
                self.countersign(request, employer_signature())
        request.refresh_from_db()
        self.assertEqual(request.status, Status.EMPLOYEE_SIGNED)
        self.assertFalse(private_files.exists(request.uuid, private_files.EMPLOYER_SIGNATURE_IMAGE))
        self.assertEqual(request.events.get(kind=Kind.TIMESTAMP_FAILED).detail["step"], "employeur")

    def test_a_request_countersigned_before_the_drawing_still_reads_and_verifies(self):
        """Countersigned the old way (no drawing kept, none in the event nor
        in the /Reason): no drawing to name, and the journal still checks and
        is still sealed."""
        request = self.signed()
        done = countersign_without_a_drawing(request, now=NOW + dt.timedelta(days=1))
        self.assertEqual(done.status, Status.COMPLETE)
        self.assertEqual(requests_.employer_drawing_sha256(done), "")
        self.assertFalse(private_files.exists(done.uuid, private_files.EMPLOYER_SIGNATURE_IMAGE))
        chain = requests_.verify_event_chain(done)
        self.assertTrue(chain.ok, chain.message)
        self.assertIn("ceux d'avant la contresignature de l'employeur sont scellés", chain.message)
        final = private_files.read_checked(done.uuid, private_files.FINAL, done.final_pdf_sha256)
        self.assertTrue(signing.verify(final).ok)
        self.assertEqual(signing.signed_reasons(final)[signing.EMPLOYER_FIELD].drawing, "")
        event = done.events.get(kind=Kind.COUNTERSIGNED)
        self.assertFalse(any("signature dessinée" in line for line in requests_.describe_event(event).details))


class CancelTests(RequestCase):
    def test_cancelled_with_its_reason_and_its_files_kept(self):
        request, _token = self.create()
        requests_.cancel_request(request, "mauvais mois", now=NOW, ip=IP, user_agent="Bureau")
        request.refresh_from_db()
        self.assertEqual(request.status, Status.CANCELLED)
        self.assertEqual(request.cancelled_reason, "mauvais mois")
        self.assertTrue(private_files.exists(request.uuid, private_files.DOCUMENT))
        with self.assertRaises(requests_.RequestStateError):
            requests_.cancel_request(request, "encore", now=NOW)

    def test_a_finished_request_is_corrected_not_cancelled(self):
        request, _token = self.create()
        self.employee_signs(request)
        requests_.countersign_request(request, employer_signature(), now=NOW, ip=IP, user_agent="Bureau")
        with self.assertRaises(requests_.RequestStateError) as caught:
            requests_.cancel_request(request, "non", now=NOW)
        self.assertIn("Corriger ce mois", str(caught.exception))


class DownloadTests(RequestCase):
    def test_a_download_is_an_event(self):
        request, _token = self.create()
        requests_.record_download(request, private_files.DOCUMENT, ip=IP, user_agent=PHONE)
        event = request.events.get(kind=Kind.DOWNLOADED)
        self.assertEqual(event.detail, {"file": "document.pdf"})
        with self.assertRaises(ValueError):
            requests_.record_download(request, "../../keys/authority.key.pem", ip=IP, user_agent=PHONE)


class RequestModelTests(RequestCase):
    def test_the_database_holds_one_open_request_per_month_too(self):
        from django.db import IntegrityError, transaction

        _request, _token = self.create()
        with self.assertRaises(IntegrityError), transaction.atomic():
            SignatureRequest.objects.create(
                timesheet=self.timesheet, version=2, expires_at=NOW, token_hash="f" * 64, document_sha256="0" * 64
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SignatureRequest.objects.create(
                timesheet=self.timesheet,
                version=1,
                status=Status.CANCELLED,
                expires_at=NOW,
                token_hash="e" * 64,
                document_sha256="0" * 64,
            )

    def test_a_month_with_a_request_is_never_deleted(self):
        from django.db.models import ProtectedError

        self.create()
        with self.assertRaises(ProtectedError):
            self.timesheet.delete()

    def test_the_admin_shows_the_evidence_and_edits_none_of_it(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        from tests.runner import confirm_password, member_of_the_test_tenant

        request, _token = self.create()
        # The admin is a superuser's who works in a tenant.
        # The admin asks for the password again (accounts/admin_site.py).
        confirm_password(
            self.client,
            member_of_the_test_tenant(User.objects.create_superuser("proprio", "proprio@example.invalid", "x")),
        )
        event = request.events.first()
        for name, obj in (("signaturerequest", request), ("signatureevent", event)):
            with self.subTest(model=name):
                self.assertEqual(self.client.get(reverse(f"admin:staff_{name}_changelist")).status_code, 200)
                page = self.client.get(reverse(f"admin:staff_{name}_change", args=[obj.pk]))
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, 'name="_save"')
                self.assertEqual(self.client.get(reverse(f"admin:staff_{name}_add")).status_code, 403)
        page = self.client.get(reverse("admin:staff_signaturerequest_change", args=[request.pk]))
        self.assertNotContains(page, request.token_hash)

    def test_now_defaults_to_the_clock(self):
        before = timezone.now()
        request, _token = requests_.create_request(self.person, JUNE)
        self.assertGreaterEqual(request.created_at, before)
        self.assertTrue(requests_.month_is_locked(self.timesheet))
        with mock.patch("staff.signature_requests.timezone.now", return_value=before + dt.timedelta(days=15)):
            self.assertFalse(requests_.month_is_locked(self.timesheet))


# -- Found by the review of 28/09 -------------------------------------------------------------------------------


class IdentificationRecordTests(RequestCase):
    """The method recorded is the one of the code HE verified, never the one
    of the last code issued. Before: `issue_code` wrote `identification` at
    every code, so a code asked for by e-mail from another browser - anyone
    holding the link - after he had typed the one handed over turned the row,
    the signed event and the proof file into « envoyé par e-mail »; and a
    code handed over after an e-mail verification made the proof claim « la
    remise faite par l'employeur »."""

    def sign(self, request, session):
        signed = requests_.sign_for_employee(
            request,
            drawn_signature(),
            session=session,
            statement_accepted=True,
            now=NOW + dt.timedelta(minutes=5),
            ip=IP,
            user_agent=PHONE,
        )
        event = signed.events.get(kind=Kind.EMPLOYEE_SIGNED)
        return (
            signed.identification,
            event.detail["identification"],
            pdf_text(private_files.read(signed.uuid, private_files.PROOF)),
        )

    def test_handed_over_then_a_code_asked_by_email_from_elsewhere(self):
        request, _token = self.create()
        session = self.identified(request)  # handed over, typed on his phone
        requests_.issue_code(
            request,
            Identification.CODE_BY_EMAIL,
            now=NOW + dt.timedelta(minutes=1),
            ip="198.51.100.4",
            user_agent="Autre navigateur",
        )
        row, detail, proof = self.sign(request, session)
        self.assertEqual((row, detail), (Identification.CODE_HANDED_OVER, Identification.CODE_HANDED_OVER))
        self.assertIn("Méthode : code à usage unique affiché à l'employeur", proof)
        self.assertIn("L'identification repose donc sur cette remise", proof)
        self.assertNotIn("Méthode : code à usage unique envoyé par e-mail", proof)

    def test_by_email_then_a_code_handed_over_by_the_owner(self):
        request, _token = self.create()
        code = requests_.issue_code(request, Identification.CODE_BY_EMAIL, now=NOW)
        session = {}
        requests_.check_code(request, code, session, now=NOW, ip=IP, user_agent=PHONE)
        requests_.issue_code(
            request, Identification.CODE_HANDED_OVER, now=NOW + dt.timedelta(minutes=1), user_agent="Bureau"
        )
        row, detail, proof = self.sign(request, session)
        self.assertEqual((row, detail), (Identification.CODE_BY_EMAIL, Identification.CODE_BY_EMAIL))
        self.assertIn("Méthode : code à usage unique envoyé par e-mail", proof)
        self.assertNotIn("L'identification repose donc sur cette remise", proof)

    def test_a_code_issued_identifies_nothing_until_it_is_typed(self):
        request, _token = self.create()
        requests_.issue_code(request, Identification.CODE_HANDED_OVER, now=NOW)
        request.refresh_from_db()
        self.assertEqual((request.identification, request.code_method), ("", Identification.CODE_HANDED_OVER))
        self.assertIn("Méthode : — (pas encore)", pdf_text(requests_.store_proof(request)))

    def test_the_journal_says_which_method_each_verification_used(self):
        request, _token = self.create()
        self.identified(request)
        event = request.events.get(kind=Kind.CODE_VERIFIED)
        self.assertEqual(event.detail, {"method": Identification.CODE_HANDED_OVER})
        self.assertIn(
            "méthode : code à usage unique affiché à l'employeur, qui l'a transmis au salarié par un autre canal que "
            "le lien",
            requests_.describe_event(event).details,
        )


class CodeChannelTests(RequestCase):
    """The two ways of getting a code no longer undo each other. Before,
    they shared one slot and one « 3 codes an hour »: anyone holding only the
    link pressed « Recevoir un code par e-mail » twice, the code the employer
    had handed over read « Code erroné », and the employer was refused
    another for an hour - every hour of the link's fortnight."""

    def test_a_handed_over_code_survives_codes_asked_by_email_meanwhile(self):
        request, _token = self.create()
        code = requests_.issue_code(request, Identification.CODE_HANDED_OVER, now=NOW)
        for minute in (1, 2):
            with self.assertRaises(requests_.CodeError) as caught:
                requests_.issue_code(
                    request, Identification.CODE_BY_EMAIL, now=NOW + dt.timedelta(minutes=minute), ip="198.51.100.4"
                )
            self.assertEqual(str(caught.exception), requests_.HANDED_OVER_CODE_WAITING)
        requests_.check_code(request, code, self.session, now=NOW + dt.timedelta(minutes=3), ip=IP, user_agent=PHONE)
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW + dt.timedelta(minutes=3)))

    def test_a_handed_over_code_that_can_no_longer_be_used_protects_nothing(self):
        request, _token = self.create()
        requests_.issue_code(request, Identification.CODE_HANDED_OVER, now=NOW)
        # Past its 15 minutes, a code by e-mail may take its place.
        requests_.issue_code(request, Identification.CODE_BY_EMAIL, now=NOW + dt.timedelta(minutes=16))
        requests_.issue_code(request, Identification.CODE_HANDED_OVER, now=NOW + dt.timedelta(minutes=17))
        for _ in range(requests_.CODE_MAX_ATTEMPTS):
            with self.assertRaises(requests_.CodeError):
                requests_.check_code(request, "abcdef", self.session, now=NOW + dt.timedelta(minutes=18))
        # Its tries used up, too.
        requests_.issue_code(request, Identification.CODE_BY_EMAIL, now=NOW + dt.timedelta(minutes=19))

    def test_the_owner_may_replace_a_code_sent_by_email(self):
        request, _token = self.create()
        requests_.issue_code(request, Identification.CODE_BY_EMAIL, now=NOW)
        code = requests_.issue_code(request, Identification.CODE_HANDED_OVER, now=NOW + dt.timedelta(minutes=1))
        requests_.check_code(request, code, self.session, now=NOW + dt.timedelta(minutes=2), ip=IP, user_agent=PHONE)
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW + dt.timedelta(minutes=2)))

    def test_the_owner_still_gives_a_code_after_three_asked_by_email(self):
        request, _token = self.create()
        for minute in (0, 1, 2):
            requests_.issue_code(request, Identification.CODE_BY_EMAIL, now=NOW + dt.timedelta(minutes=minute))
        with self.assertRaises(requests_.CodeError) as caught:
            requests_.issue_code(request, Identification.CODE_BY_EMAIL, now=NOW + dt.timedelta(minutes=3))
        self.assertIn("3 codes", str(caught.exception))
        code = requests_.issue_code(request, Identification.CODE_HANDED_OVER, now=NOW + dt.timedelta(minutes=4))
        requests_.check_code(request, code, self.session, now=NOW + dt.timedelta(minutes=5), ip=IP, user_agent=PHONE)
        self.assertTrue(requests_.is_identified(self.session, request, now=NOW + dt.timedelta(minutes=5)))

    def test_the_code_waiting_and_its_method(self):
        request, _token = self.create()
        self.assertEqual(requests_.waiting_code_method(request, now=NOW), "")
        requests_.issue_code(request, Identification.CODE_BY_EMAIL, now=NOW)
        self.assertEqual(requests_.waiting_code_method(request, now=NOW), Identification.CODE_BY_EMAIL)
        self.assertEqual(requests_.waiting_code_method(request, now=NOW + dt.timedelta(minutes=16)), "")


class EmployeeSignatureTextTests(RequestCase):
    """What the employee's page is told when his signature cannot be made:
    a sentence for him, never the server's insides (review, 28/09: the
    private folder's absolute path - the Windows user name in it - and the
    name of the environment variable holding the keys' secret reached the
    public page)."""

    def attempt(self, request, session):
        with self.assertRaises(requests_.RequestError) as caught:
            requests_.sign_for_employee(
                request, drawn_signature(), session=session, statement_accepted=True, now=NOW, ip=IP, user_agent=PHONE
            )
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)
        return str(caught.exception)

    def test_a_missing_document(self):
        request, _token = self.create()
        session = self.identified(request)
        (private_files.request_dir(request.uuid) / private_files.DOCUMENT).unlink()
        with self.assertLogs("staff.signature_requests", "WARNING") as logged:
            message = self.attempt(request, session)
        self.assertEqual(message, requests_.DOCUMENT_CHANGED)
        self.assertNotIn(str(self.private_dir), message)
        # The detail is for whoever reads the server's log.
        self.assertIn(str(request.uuid), " ".join(logged.output))

    def test_keys_that_cannot_be_opened(self):
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase de passe d'essai"):
            signing.authority(self.bar)
        request, _token = self.create()
        session = self.identified(request)
        with self.assertLogs("staff.signature_requests", "WARNING") as logged:
            message = self.attempt(request, session)
        self.assertEqual(message, requests_.NOT_SIGNED)
        self.assertNotIn("MARGINMATE_SIGNING_PASSPHRASE", message)
        self.assertIn("MARGINMATE_SIGNING_PASSPHRASE", " ".join(logged.output))

    def test_his_drawing_refused_is_still_said_to_him(self):
        request, _token = self.create()
        session = self.identified(request)
        with self.assertRaises(signing.SignatureImageError):
            requests_.sign_for_employee(
                request, b"pas une image", session=session, statement_accepted=True, now=NOW, ip=IP, user_agent=PHONE
            )


class SignedFactsTests(RequestCase):
    """What he signed is recorded where the database alone cannot rewrite
    it (review, 28/09): his reservations inside his signature, and in the
    signed event with the text he certified."""

    WORDS = "Le 12, j'ai fini à 23 h 30 et non à 23 h."

    def test_his_reservations_are_in_his_signature_and_in_the_journal(self):
        request, _token = self.create()
        signed = self.employee_signs(request, reservation=self.WORDS)
        document = private_files.read_checked(signed.uuid, private_files.EMPLOYEE_SIGNED, signed.employee_pdf_sha256)
        self.assertEqual(signing.signed_reasons(document)[signing.EMPLOYEE_FIELD].reservation, self.WORDS)
        detail = signed.events.get(kind=Kind.EMPLOYEE_SIGNED).detail
        self.assertEqual(detail["reservation"], self.WORDS)
        self.assertEqual(detail["statement"], requests_.statement_text(signed))
        self.assertEqual(detail["authority_sha256"], signing.authority_fingerprint())


class ChainAnchorTests(RequestCase):
    """The journal up to each signature is sealed in that signature - the
    head of the chain goes into its /Reason, under the timestamp. Before, a
    forger with the database who rewrote an event, every hash after it AND
    the head the request row keeps read « Journal intègre »."""

    def rewrite(self, request, *kinds, ip="198.51.100.66"):
        """The IP of the events of `kinds` rewritten, every hash after them
        worked out again, the head moved: everything but the signed PDFs."""
        previous = requests_.GENESIS
        for event in request.events.order_by("id"):
            new_ip = ip if event.kind in kinds else event.ip
            forged = requests_.event_hash(
                request.uuid, previous, event.at, event.kind, new_ip, event.user_agent, event.detail
            )
            SignatureEvent.objects.filter(pk=event.pk).update(ip=new_ip, previous_hash=previous, hash=forged)
            previous = forged
        SignatureRequest.objects.filter(pk=request.pk).update(last_event_hash=previous)
        request.refresh_from_db()

    def test_an_untouched_signed_journal_says_it_is_sealed(self):
        request, _token = self.create()
        self.employee_signs(request)
        request.refresh_from_db()
        check = requests_.verify_event_chain(request)
        self.assertTrue(check.ok, check.message)
        self.assertIn("scellés dans le document signé", check.message)
        requests_.countersign_request(
            request, employer_signature(), now=NOW + dt.timedelta(days=1), ip=IP, user_agent="Bureau"
        )
        request.refresh_from_db()
        check = requests_.verify_event_chain(request)
        self.assertTrue(check.ok, check.message)
        self.assertIn("contresignature", check.message)

    def test_a_rewrite_that_also_moves_the_head_is_caught(self):
        request, _token = self.create()
        self.employee_signs(request)
        requests_.countersign_request(
            request, employer_signature(), now=NOW + dt.timedelta(days=1), ip=IP, user_agent="Bureau"
        )
        request.refresh_from_db()
        self.rewrite(request, Kind.CODE_VERIFIED, Kind.EMPLOYEE_SIGNED)
        check = requests_.verify_event_chain(request)
        self.assertFalse(check.ok)
        self.assertIn("réécrit", check.message)

    def test_before_the_countersignature_what_precedes_his_signature_is_sealed(self):
        request, _token = self.create()
        self.employee_signs(request)
        request.refresh_from_db()
        self.rewrite(request, Kind.CODE_VERIFIED)
        check = requests_.verify_event_chain(request)
        self.assertFalse(check.ok)
        self.assertIn("signature du salarié", check.message)

    def test_a_signed_file_that_cannot_be_read_is_said(self):
        request, _token = self.create()
        self.employee_signs(request)
        request.refresh_from_db()
        (private_files.request_dir(request.uuid) / private_files.EMPLOYEE_SIGNED).unlink()
        check = requests_.verify_event_chain(request)
        self.assertIn("n'a pas pu être contrôlé", check.message)


class SnapshotAsPrintedTests(RequestCase):
    """What his phone shows is what he signs, to the character (review,
    28/09): a note too long for the column was whole on the page and cut in
    the PDF; a character cp1252 lacks was shown on the page and « ? » in the
    PDF."""

    LONG_NOTE = ("arrivé à 17 h pour la livraison Metro, rangement de la réserve, puis service du soir " * 3)[:190]

    def rows_of(self, data) -> list[str]:
        with pdfplumber.open(io.BytesIO(data)) as document:
            lines = document.pages[0].extract_text().splitlines()
        return lines[lines.index("Jour Heures Motif / note") + 1 : lines.index("Récapitulatif du mois")]

    def test_every_note_reads_as_the_signed_document_prints_it(self):
        save_month(
            self.person,
            JUNE,
            [
                PostedDay(date(2026, 6, 4), hours=Decimal("10"), note=self.LONG_NOTE),
                PostedDay(date(2026, 6, 8), kind="conges", note="pont"),
            ],
        )
        # A character the month's form refuses now, stored before it did.
        TimesheetDay.objects.filter(timesheet=self.timesheet, date=date(2026, 6, 5)).update(note="service 18 h → 2 h")
        request, _token = self.create()
        rows = self.rows_of(private_files.read(request.uuid, private_files.DOCUMENT))
        days = {day["date"]: day for week in request.month_snapshot["weeks"] for day in week["days"]}
        for iso in ("2026-06-04", "2026-06-05", "2026-06-08"):
            day = days[iso]
            with self.subTest(day=iso):
                row = next(line for line in rows if line.startswith(f"{day['name']} "))
                self.assertEqual(row, " ".join(part for part in (day["name"], day["hours"], day["note"]) if part))
        self.assertTrue(days["2026-06-04"]["note"].endswith("…"))
        self.assertEqual(days["2026-06-05"]["note"], "service 18 h ? 2 h")
        self.assertEqual(days["2026-06-08"]["note"], "Congés payés — pont")
        # And the page draws those very words.
        page = render_to_string("staff/_month_table.html", {"month": request.month_snapshot})
        self.assertIn(days["2026-06-04"]["note"], page)
        self.assertNotIn(self.LONG_NOTE, page)
        self.assertIn("service 18 h ? 2 h", page)

    def test_the_month_form_refuses_what_the_sheet_cannot_print(self):
        posted = read_posted_month(JUNE, {"note-2026-06-05": "service 18 h → 2 h 😀"})
        (error,) = posted.errors
        self.assertIn("« → » et « 😀 »", error)
        self.assertIn("ne sait pas écrire", error)
        with self.assertRaises(ValueError) as caught:
            save_month(self.person, JUNE, [PostedDay(date(2026, 6, 5), note="service 18 h → 2 h")])
        self.assertIn("« → »", str(caught.exception))
        # Typography with an exact equivalent is no refusal.
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 5), note="départ\N{NARROW NO-BREAK SPACE}: 22 h")])
        self.assertEqual(read_posted_month(JUNE, {"note-2026-06-05": "l’été… 10 €"}).errors, ())
