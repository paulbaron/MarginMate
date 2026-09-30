"""The signature's e-mail is capped (security audit LB-4).

The sender is the platform's, shared by every bar, and a bar chooses both
where a mail goes (its employee's address) and words that go in it (its
establishment's name): one « Envoyer pour signature » and twenty « Nouveau
lien » sent 21 mails. Now at most `REQUEST_MAILS_PER_HOUR` (3) link or copy
mails for one request in an hour, and `ESPACE_MAILS_PER_DAY` (60) signature
mails of any kind for the espace in 24 hours - counted from the requests'
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
from django.test import override_settings
from django.utils import timezone

from accounts import paths
from staff import private_files, signature_mail, signature_requests as requests_
from staff.models import SignatureEvent
from staff.signature_views import FINAL_COPY_MISSING
from staff.tests.page_forms import as_post
from staff.tests.test_sign_public import PublicCase
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
class EspaceCapTests(PublicCase):
    def test_sixty_mails_a_day_for_the_whole_espace(self):
        self.assertEqual((signature_mail.ESPACE_MAILS_PER_DAY, signature_mail.REQUEST_MAILS_PER_HOUR), (60, 3))
        with mock.patch.object(signature_mail, "ESPACE_MAILS_PER_DAY", 2):
            self.assertTrue(signature_mail.send_link(self.request, LINK_URL).sent)
            self.assertTrue(signature_mail.send_link(self.request, LINK_URL).sent)
            outcome = signature_mail.send_link(self.request, LINK_URL)
        self.assertFalse(outcome.sent)
        self.assertIn("Déjà 2 e-mails de signature envoyés depuis 24 heures", outcome.message)
        self.assertIn("transmettez-le vous-même", outcome.message)
        self.assertEqual(len(mail.outbox), 2)

    def test_a_day_later_they_go_again(self):
        with mock.patch.object(signature_mail, "ESPACE_MAILS_PER_DAY", 2):
            for _ in range(2):
                signature_mail.send_link(self.request, LINK_URL)
            SignatureEvent.objects.update(at=timezone.now() - dt.timedelta(hours=25))
            self.assertTrue(signature_mail.send_link(self.request, LINK_URL).sent)

    def test_the_code_by_mail_is_refused_before_one_is_issued(self):
        with mock.patch.object(signature_mail, "ESPACE_MAILS_PER_DAY", 1):
            signature_mail.send_link(self.request, LINK_URL)
            with self.assertRaises(requests_.CodeError) as refused:
                signature_mail.send_code(self.request)
        self.assertEqual(str(refused.exception), signature_mail.CODE_CAP_REACHED)
        self.refresh()
        self.assertEqual(self.request.code_hash, "")
        self.assertEqual(len(mail.outbox), 1)

    def test_the_employee_s_page_says_to_ask_the_employer(self):
        with mock.patch.object(signature_mail, "ESPACE_MAILS_PER_DAY", 0):
            answer = self.post(self.form(self.get(), "staff:sign_send_code"))
        self.assertIn(signature_mail.CODE_CAP_REACHED, self.text(answer))
        self.assertEqual(mail.outbox, [])
