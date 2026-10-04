"""The signature's e-mail is capped (security audit LB-4).

The sender is the platform's, shared by every bar, and a bar chooses both
where a mail goes (its employee's address) and words that go in it (its
establishment's name): one « Envoyer pour signature » and twenty « Nouveau
lien » sent 21 mails. Now at most `REQUEST_MAILS_PER_HOUR` (3) link or copy
mails for one request in an hour, and `TENANT_MAILS_PER_DAY` (60) signature
mails of any kind for the tenant in 24 hours - counted from the requests'
own events, failed sends included. Over either nothing is sent: the owner's
page shows the link « à transmettre vous-même », the employee's page says to
ask the employer for the code.

E-mail goes to Django's locmem outbox; nothing reaches a server. Names and
addresses INVENTED."""

import datetime as dt
import re
from html import unescape
from unittest import mock

from django.core import mail
from django.core.mail import EmailMessage
from django.test import override_settings
from django.utils import timezone

from accounts import paths
from staff import private_files, signature_mail
from staff import signature_requests as requests_
from staff.models import SignatureEvent, SignatureRequest
from staff.signature_views import FINAL_COPY_MISSING
from staff.tests.page_forms import as_post
from staff.tests.test_sign_public import IP, PublicCase
from staff.tests.test_signature_mail import CONFIGURED, NOW, MailCase
from staff.tests.test_signature_pages import ADDRESS, LINK, MAIL, OwnerCase

Kind = SignatureEvent.Kind
LINK_URL = "https://bar.example.invalid/personnel/signer/jeton-d-essai/"


def _text(response) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.content.decode())).split())


@override_settings(**MAIL)
class RequestCapPageTests(OwnerCase):
    def setUp(self):
        super().setUp()
        self.person.email = ADDRESS
        self.person.save()

    def new_link(self):
        form = self.form(self.page(), "staff:signature_link", 1)
        return self.client.post(form.action, as_post(form.submission()))

    def test_three_mails_an_hour_for_a_request_then_the_link_is_handed_over(self):
        form = self.form(self.page(), "staff:signature_send")
        self.assertEqual(self.client.post(form.action, as_post(form.submission())).status_code, 200)
        for _ in range(2):
            self.assertIn("E-mail envoyé à", _text(self.new_link()))
        self.assertEqual(len(mail.outbox), 3)

        answer = self.new_link()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(len(mail.outbox), 3)
        text = _text(answer)
        self.assertIn("Déjà 3 e-mails envoyés pour cette demande dans l'heure", text)
        self.assertIn("transmettez-le vous-même", text)
        # The new link is on the page all the same, and it works.
        _link, token = LINK.search(answer.content.decode()).groups()
        self.assertEqual(requests_.resolve_link(token), self.request_())
        self.assertEqual(self.kinds(self.request_()).count(Kind.LINK_SENT), 3)

    def test_an_hour_later_it_goes_again(self):
        request, _token = self.create()
        for _ in range(3):
            self.assertTrue(signature_mail.send_link(request, LINK_URL).sent)
        self.assertFalse(signature_mail.send_link(request, LINK_URL).sent)
        # The events as they would be an hour and a minute later (an update
        # of the rows' times - the chain is not what this test is about).
        SignatureEvent.objects.filter(request=request).update(at=timezone.now() - dt.timedelta(minutes=61))
        self.assertTrue(signature_mail.send_link(request, LINK_URL).sent)

    def test_a_failed_send_counts_so_a_broken_server_is_not_hammered(self):
        request, _token = self.create()
        with override_settings(EMAIL_BACKEND="staff.tests.test_signature_mail.FailingBackend"):
            for _ in range(3):
                self.assertFalse(signature_mail.send_link(request, LINK_URL).sent)
        outcome = signature_mail.send_link(request, LINK_URL)
        self.assertFalse(outcome.sent)
        self.assertIn("transmettez-le vous-même", outcome.message)
        self.assertEqual(self.kinds(request).count(Kind.MAIL_FAILED), 3)

    def test_the_final_copy_mailed_again_and_again_is_capped_too(self):
        request, _token = self.complete()
        for _ in range(3):
            self.assertTrue(signature_mail.send_final_copy(request).sent)
        outcome = signature_mail.send_final_copy(request)
        self.assertFalse(outcome.sent)
        self.assertIn("la copie signée n'a pas été envoyé", outcome.message)
        self.assertEqual(len(mail.outbox), 3)

    def test_a_copy_gone_from_disk_is_said_without_its_path(self):
        """A FileNotFoundError's words are the file's full path on the server
        (security audit LB-3): the owner's panel says it in its own words."""
        request, _token = self.complete()
        final = private_files.request_dir(request.uuid) / private_files.FINAL
        final.unlink()
        answer = self.new_link()
        self.assertEqual(answer.status_code, 200)
        text = _text(answer)
        self.assertIn(f"L'e-mail n'est pas parti : {FINAL_COPY_MISSING}", text)
        self.assertNotIn(str(paths.private_dir()), answer.content.decode())
        self.assertNotIn(str(final), text)
        self.assertIsNotNone(LINK.search(answer.content.decode()))


@override_settings(**MAIL)
class TenantCapTests(PublicCase):
    def test_sixty_mails_a_day_for_the_whole_tenant(self):
        self.assertEqual((signature_mail.TENANT_MAILS_PER_DAY, signature_mail.REQUEST_MAILS_PER_HOUR), (60, 3))
        with mock.patch.object(signature_mail, "TENANT_MAILS_PER_DAY", 2):
            self.assertTrue(signature_mail.send_link(self.request, LINK_URL).sent)
            self.assertTrue(signature_mail.send_link(self.request, LINK_URL).sent)
            outcome = signature_mail.send_link(self.request, LINK_URL)
        self.assertFalse(outcome.sent)
        self.assertIn("Déjà 2 e-mails de signature envoyés depuis 24 heures", outcome.message)
        self.assertIn("transmettez-le vous-même", outcome.message)
        self.assertEqual(len(mail.outbox), 2)

    def test_a_day_later_they_go_again(self):
        with mock.patch.object(signature_mail, "TENANT_MAILS_PER_DAY", 2):
            for _ in range(2):
                signature_mail.send_link(self.request, LINK_URL)
            SignatureEvent.objects.update(at=timezone.now() - dt.timedelta(hours=25))
            self.assertTrue(signature_mail.send_link(self.request, LINK_URL).sent)

    def test_the_code_by_mail_is_refused_before_one_is_issued(self):
        with mock.patch.object(signature_mail, "TENANT_MAILS_PER_DAY", 1):
            signature_mail.send_link(self.request, LINK_URL)
            with self.assertRaises(requests_.CodeError) as refused:
                signature_mail.send_code(self.request)
        self.assertEqual(str(refused.exception), signature_mail.CODE_CAP_REACHED)
        self.refresh()
        self.assertEqual(self.request.code_hash, "")
        self.assertEqual(len(mail.outbox), 1)

    def test_the_employee_s_page_says_to_ask_the_employer(self):
        with mock.patch.object(signature_mail, "TENANT_MAILS_PER_DAY", 0):
            answer = self.post(self.form(self.get(), "staff:sign_send_code"))
        self.assertIn(signature_mail.CODE_CAP_REACHED, self.text(answer))
        self.assertEqual(mail.outbox, [])


@CONFIGURED
class OneLinkCodeTests(MailCase):
    """One link holder alone - « Recevoir un code par e-mail » three times an
    hour - used up the bar's sixty mails of the day, so every other
    employee's link and copy were refused for 24 hours; and each new code's
    five tries made some 300 guesses a day for the link's fortnight."""

    def test_six_codes_a_day_by_mail_for_one_request(self):
        for hour in range(2):
            for minute in (0, 10, 20):
                signature_mail.send_code(self.request, now=NOW + dt.timedelta(hours=hour, minutes=minute))
        self.assertEqual(len(mail.outbox), 6)
        with self.assertRaises(requests_.CodeError) as refused:
            signature_mail.send_code(self.request, now=NOW + dt.timedelta(hours=2))
        self.assertEqual(str(refused.exception), signature_mail.CODE_CAP_REACHED)
        self.assertEqual(len(mail.outbox), 6)
        self.request.refresh_from_db()
        self.assertEqual(self.request.code_sent_at, NOW + dt.timedelta(hours=1, minutes=20))
        # The rest of the tenant's day is the others'.
        self.assertEqual(signature_mail.cap_reached(self.request, "le lien", now=NOW + dt.timedelta(hours=2)), "")
        # A day after the first, one more.
        self.assertTrue(signature_mail.send_code(self.request, now=NOW + dt.timedelta(hours=24, minutes=1)).sent)

    def test_wrong_codes_with_one_link_stop_the_codes_by_mail_until_a_new_link(self):
        self.assertEqual(requests_.CODE_FAILURES_PER_LINK, 30)
        by_mail = SignatureRequest.Identification.CODE_BY_EMAIL
        with mock.patch.object(requests_, "CODE_FAILURES_PER_LINK", 3):
            code = requests_.issue_code(self.request, by_mail, now=NOW)
            wrong = "000000" if code != "000000" else "111111"
            for _ in range(3):
                with self.assertRaises(requests_.CodeError):
                    requests_.check_code(self.request, wrong, {}, now=NOW, ip=IP)
            # Even the right one: the guesses stop here.
            session = {}
            with self.assertRaises(requests_.CodeError) as refused:
                requests_.check_code(self.request, code, session, now=NOW, ip=IP)
            self.assertEqual(str(refused.exception), requests_.TOO_MANY_WRONG_CODES)
            with self.assertRaises(requests_.CodeError) as refused:
                signature_mail.send_code(self.request, now=NOW + dt.timedelta(minutes=1))
            self.assertEqual(str(refused.exception), requests_.TOO_MANY_WRONG_CODES)
            self.assertEqual(mail.outbox, [])
            # The code the employer hands over still works.
            handed = requests_.issue_code(
                self.request, SignatureRequest.Identification.CODE_HANDED_OVER, now=NOW + dt.timedelta(minutes=2)
            )
            requests_.check_code(self.request, handed, session, now=NOW + dt.timedelta(minutes=2), ip=IP)
            self.assertTrue(requests_.is_identified(session, self.request, now=NOW + dt.timedelta(minutes=2)))
            # « Nouveau lien » starts the count again.
            requests_.renew_link(self.request, now=NOW + dt.timedelta(minutes=3))
            self.assertTrue(signature_mail.send_code(self.request, now=NOW + dt.timedelta(minutes=4)).sent)

    def test_a_code_asked_while_one_is_on_its_way_is_refused(self):
        """Two posts at once both passed the counts, which read the
        `code_sent` logged once the mail has left."""
        meanwhile = []
        real_send = EmailMessage.send

        def slow_send(message, fail_silently=False):
            if not meanwhile:
                meanwhile.append("")
                try:
                    signature_mail.send_code(self.request, now=NOW)
                except requests_.CodeError as refused:
                    meanwhile[0] = str(refused)
                else:
                    meanwhile[0] = "envoyé"
            return real_send(message, fail_silently=fail_silently)

        with mock.patch.object(EmailMessage, "send", slow_send):
            self.assertTrue(signature_mail.send_code(self.request, now=NOW).sent)
        self.assertEqual(meanwhile, [signature_mail.CODE_ON_ITS_WAY])
        self.assertEqual(len(mail.outbox), 1)
        # Once it has left, the next one goes.
        self.assertTrue(signature_mail.send_code(self.request, now=NOW + dt.timedelta(minutes=1)).sent)
