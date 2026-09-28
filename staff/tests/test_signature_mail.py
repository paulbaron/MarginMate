"""E-mail for the signatures (`staff/signature_mail.py`): optional, off
unless EMAIL_HOST is set; in tests Django's locmem backend keeps every
message in `mail.outbox` and nothing reaches a server. A send that fails is
a sentence and an event, never a 500."""

import datetime as dt
import re
import smtplib
from datetime import date

from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.test import override_settings

from staff import private_files, signature_mail, signature_requests as requests_
from staff.models import Establishment, SignatureEvent, SignatureRequest
from staff.tests.signing_support import SigningTestMixin, drawn_signature, employer_signature
from staff.tests.support import employee
from staff.timesheet import save_month
from tests.support import NoNetworkTestCase

JUNE = date(2026, 6, 1)
NOW = dt.datetime(2026, 7, 2, 8, 0, tzinfo=dt.timezone.utc)
LINK = "https://bar.example.invalid/personnel/signer/jeton-d-essai/"
Kind = SignatureEvent.Kind
CONFIGURED = override_settings(EMAIL_HOST="smtp.example.invalid", DEFAULT_FROM_EMAIL="bar@example.invalid")


class FailingBackend(BaseEmailBackend):
    """A mail server that refuses the connection."""

    def send_messages(self, messages):
        raise smtplib.SMTPConnectError(421, "service indisponible (essai)")


class MailCase(SigningTestMixin, NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE")
        self.person = employee()
        self.person.email = "jeanne.dupont@example.invalid"
        self.person.save()
        save_month(self.person, JUNE, [])
        self.request, _token = requests_.create_request(self.person, JUNE, now=NOW)

    def kinds(self):
        return list(self.request.events.values_list("kind", flat=True))


@CONFIGURED
class LinkMailTests(MailCase):
    def test_the_link_goes_to_the_employee(self):
        outcome = signature_mail.send_link(self.request, LINK, ip="203.0.113.7", user_agent="Bureau")
        self.assertTrue(outcome.sent, outcome.message)
        self.assertIn("jeanne.dupont@example.invalid", outcome.message)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["jeanne.dupont@example.invalid"])
        self.assertEqual(message.from_email, "bar@example.invalid")
        self.assertEqual(message.content_subtype, "plain")
        self.assertIn("juin 2026", message.subject)
        self.assertIn(LINK, message.body)
        self.assertIn("16/07/2026", message.body)   # valid until
        self.assertIn("réserves", message.body)
        self.assertEqual(self.kinds()[-1], Kind.LINK_SENT)
        self.assertEqual(self.request.events.last().detail, {"to": "jeanne.dupont@example.invalid"})

    def test_a_failed_send_is_said_and_logged(self):
        with override_settings(EMAIL_BACKEND="staff.tests.test_signature_mail.FailingBackend"):
            outcome = signature_mail.send_link(self.request, LINK)
        self.assertFalse(outcome.sent)
        self.assertIn("n'a pas pu être envoyé", outcome.message)
        self.assertIn("autre moyen", outcome.message)
        event = self.request.events.last()
        self.assertEqual(event.kind, Kind.MAIL_FAILED)
        self.assertEqual(event.detail["what"], "lien")

    def test_no_address_no_mail(self):
        self.person.email = ""
        self.person.save()
        self.request.refresh_from_db()
        self.assertFalse(signature_mail.can_email(self.request))
        outcome = signature_mail.send_link(self.request, LINK)
        self.assertFalse(outcome.sent)
        # Said of her by name, as the owner's section says everything else -
        # not « Ce salarié » beside « DUPONT Jeanne ».
        self.assertTrue(outcome.message.startswith("DUPONT Jeanne n'a pas d'adresse e-mail : "), outcome.message)
        self.assertNotIn("salarié", outcome.message)
        self.assertEqual(mail.outbox, [])


class NotConfiguredTests(MailCase):
    def test_off_by_default(self):
        self.assertFalse(signature_mail.mail_configured())
        self.assertFalse(signature_mail.can_email(self.request))
        outcome = signature_mail.send_link(self.request, LINK)
        self.assertFalse(outcome.sent)
        self.assertIn("EMAIL_HOST", outcome.message)
        self.assertEqual(mail.outbox, [])
        self.assertNotIn(Kind.MAIL_FAILED, self.kinds())
        with self.assertRaises(requests_.CodeError):
            signature_mail.send_code(self.request, now=NOW)
        self.request.refresh_from_db()
        self.assertEqual(self.request.code_hash, "")


@CONFIGURED
class CodeMailTests(MailCase):
    def test_the_code_goes_by_mail_and_identifies(self):
        outcome = signature_mail.send_code(self.request, now=NOW, ip="203.0.113.20", user_agent="Téléphone")
        self.assertTrue(outcome.sent)
        body = mail.outbox[0].body
        code = re.search(r"\b([0-9]{6})\b", body).group(1)
        self.assertIn("15 minutes", body)
        self.assertIn("Il remplace tout code demandé avant.", body)
        # The code's method waits beside it; he is identified by e-mail once he types it.
        self.assertEqual(
            (self.request.code_method, self.request.identification), (SignatureRequest.Identification.CODE_BY_EMAIL, "")
        )
        self.assertEqual(self.kinds()[-1], Kind.CODE_SENT)
        self.assertNotIn(code, str(self.request.events.last().detail))
        session = {}
        requests_.check_code(self.request, code, session, now=NOW, ip="203.0.113.20", user_agent="Téléphone")
        self.assertTrue(requests_.is_identified(session, self.request))
        self.assertEqual(self.request.identification, SignatureRequest.Identification.CODE_BY_EMAIL)

    def test_a_code_that_could_not_be_sent_is_withdrawn_and_counted(self):
        with override_settings(EMAIL_BACKEND="staff.tests.test_signature_mail.FailingBackend"):
            for _ in range(3):
                outcome = signature_mail.send_code(self.request, now=NOW)
                self.assertFalse(outcome.sent)
            self.request.refresh_from_db()
            self.assertEqual(self.request.code_hash, "")
            # Three failed sends in the hour: a fourth attempt is refused
            # before anything reaches the mail server again.
            with self.assertRaises(requests_.CodeError):
                signature_mail.send_code(self.request, now=NOW)
        failures = self.request.events.filter(kind=Kind.MAIL_FAILED)
        self.assertEqual([event.detail["what"] for event in failures], ["code"] * 3)


@CONFIGURED
class FinalCopyTests(MailCase):
    def test_the_final_copy_is_attached(self):
        session = {}
        code = requests_.issue_code(self.request, "code_remis", now=NOW)
        requests_.check_code(self.request, code, session, now=NOW)
        with self.assertRaises(requests_.RequestStateError):
            signature_mail.send_final_copy(self.request, LINK)
        requests_.sign_for_employee(self.request, drawn_signature(), session=session, statement_accepted=True, now=NOW)
        done = requests_.countersign_request(self.request, employer_signature(), now=NOW)
        outcome = signature_mail.send_final_copy(done, LINK)
        self.assertTrue(outcome.sent)
        message = mail.outbox[-1]
        (name, content, mimetype), = message.attachments
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(name.endswith(".pdf"))
        self.assertIn("DUPONT Jeanne", name)
        self.assertEqual(content, private_files.read(done.uuid, private_files.FINAL))
        self.assertIn(str(done.uuid), message.body)
        self.assertIn(LINK, message.body)
        self.assertEqual(self.request.events.last().kind, Kind.COPY_SENT)
