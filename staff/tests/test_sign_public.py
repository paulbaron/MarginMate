"""The employee's pages (/personnel/signer/<token>/…, staff/public_views.py):
the one part of the application reachable without an account. What they
must hold to, each rule a test:

* a link that reaches nothing is a plain French page - 404 unknown, 410
  expired, cancelled or superseded - never a traceback, never a word about
  anything else;
* the month is drawn from the snapshot frozen with the request, and « Voir le
  PDF » is the frozen document itself, inline;
* identification by a one-time code - by e-mail, or handed over by the
  employer - remembered for THIS request in THIS session only;
* the signature: the drawing, the certification, the reservations, each
  refusal drawn back with what was typed, no timestamp no signature;
* nothing else reachable: no other employee, no owner page, no owner's
  message, no token stored anywhere; CSRF on every POST, said in French.

Every POST is read off the rendered page (`page_forms`) through a client
that enforces CSRF, from an invented phone (TEST-NET-3 address). Names and
addresses INVENTED; keys, files and timestamps offline (signing_support)."""

import hashlib
import io
import re
from datetime import date, timedelta
from decimal import Decimal
from html import unescape
from unittest import mock

from django.contrib.sessions.models import Session
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from staff import private_files, public_views, signing
from staff import signature_requests as requests_
from staff.models import Employee, Establishment, SignatureEvent, SignatureRequest, Timesheet, TimesheetDay
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from staff.tests.signing_support import (
    FailingTimestamper,
    OfflineTimestamps,
    SigningTestMixin,
    data_url,
    drawn_signature,
    employer_signature,
)
from staff.tests.support import employee
from staff.timesheet import PostedDay, save_month
from tests import runner
from tests.support import NoNetworkTestCase

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
IP = "203.0.113.7"  # TEST-NET-3: an address that belongs to nobody
PHONE = "Mozilla/5.0 (Linux; Android 14) Essai/1.0"
ADDRESS = "jeanne.dupont@example.invalid"
MAIL = {"EMAIL_HOST": "smtp.example.invalid"}
FAILING_MAIL = {**MAIL, "EMAIL_BACKEND": "staff.tests.test_signature_mail.FailingBackend"}

Status = SignatureRequest.Status
Kind = SignatureEvent.Kind


def png(width=600, height=200, colour=(255, 255, 255, 0)) -> bytes:
    """A PNG with nothing drawn on it, of this size."""
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class PublicCase(SigningTestMixin, NoNetworkTestCase):
    """DUPONT Jeanne's June 2026 - Tuesday 2 worked 9 h « inventaire » - sent
    for signature; her phone holds the link."""

    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True, REMOTE_ADDR=IP, HTTP_USER_AGENT=PHONE)
        Establishment.objects.create(
            pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS"
        )
        self.person = employee()
        self.person.email = ADDRESS
        self.person.save()
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("9"), note="inventaire")])
        self.request, self.token = requests_.create_request(self.person, JUNE)
        self.url = reverse("staff:sign", args=[self.token])

    # -- helpers ----------------------------------------------------------------------------------------------

    def route(self, name, token=None):
        return reverse(name, args=[token or self.token])

    def get(self, url=None, status=200):
        response = self.client.get(url or self.url)
        self.assertEqual(response.status_code, status, url)
        if response["Content-Type"].startswith("text/html"):
            for marker in ("{#", "#}", "{%", "%}", "{{"):
                self.assertNotIn(marker, response.content.decode(), f"unrendered template syntax {marker!r}")
        return response

    def html(self, response) -> str:
        return response.content.decode()

    def text(self, response) -> str:
        """What the page reads as: the tags out, the entities read, the
        spaces single."""
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", self.html(response))).split())

    def form(self, response, name, token=None):
        return form_posting_to(self.html(response), self.route(name, token))

    def post(self, form, values=None, follow=True):
        return self.client.post(form.action, as_post(form.submission(values=values)), follow=follow)

    def kinds(self, request=None) -> list[str]:
        return list((request or self.request).events.order_by("id").values_list("kind", flat=True))

    def identify(self, token=None, request=None):
        """The employer hands a code over; the employee types it on his page."""
        request = request or self.request
        code = requests_.issue_code(request, SignatureRequest.Identification.CODE_HANDED_OVER)
        page = self.get(self.route("staff:sign", token))
        answer = self.post(self.form(page, "staff:sign_check_code", token), {"code": code})
        self.assertEqual(answer.status_code, 200)
        return answer

    def sign(self, page, **values):
        values.setdefault("signature", data_url(drawn_signature()))
        values.setdefault("certification", True)
        return self.post(self.form(page, "staff:sign_submit"), values)

    def refresh(self):
        self.request.refresh_from_db()
        return self.request


# -- The link ---------------------------------------------------------------------------------------------------


class LinkTests(PublicCase):
    def assertPlainPage(self, response, message, status):
        """A French sentence on a page of its own: no traceback, nothing of
        the owner's application, not cached, not indexed."""
        self.assertEqual(response.status_code, status)
        text = self.text(response)
        self.assertIn(message, text)
        html = self.html(response)
        for leak in ("Traceback", 'class="topbar"', "/personnel/1/", "MarginMate", "DUPONT", "BAR EXEMPLE"):
            self.assertNotIn(leak, html)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("noindex", response["X-Robots-Tag"])

    def test_a_link_that_reaches_nothing_is_a_plain_french_404(self):
        for url in (
            "/personnel/signer/inconnu/",
            "/personnel/signer/",
            f"/personnel/signer/{self.token}/autre/chose/",
            f"/personnel/signer/{self.token[:-1]}/",
            f"/personnel/signer/{'x' * 500}/",
            self.route("staff:sign_document", "inconnu"),
            self.route("staff:sign_copy", "inconnu"),
        ):
            with self.subTest(url=url):
                self.assertPlainPage(self.client.get(url), requests_.UNKNOWN_LINK, 404)

    def test_expired_cancelled_and_superseded_links_are_gone_410(self):
        SignatureRequest.objects.filter(pk=self.request.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertPlainPage(self.client.get(self.url), requests_.EXPIRED_LINK, 410)
        self.assertEqual(self.refresh().status, Status.EXPIRED)

        requests_.reopen_month(Timesheet.objects.get(employee=self.person, month=JUNE))
        second, token = requests_.create_request(self.person, JUNE)
        requests_.cancel_request(second, "envoyé par erreur")
        self.assertPlainPage(self.client.get(self.route("staff:sign", token)), requests_.CANCELLED_LINK, 410)

        third, token = requests_.create_request(self.person, JUNE)
        session = {}
        requests_.check_code(third, requests_.issue_code(third, "code_remis"), session)
        requests_.sign_for_employee(third, drawn_signature(), session=session, statement_accepted=True)
        requests_.countersign_request(third, employer_signature())
        requests_.reopen_month(Timesheet.objects.get(employee=self.person, month=JUNE))
        for name in ("staff:sign", "staff:sign_document", "staff:sign_copy"):
            with self.subTest(name=name):
                self.assertPlainPage(self.client.get(self.route(name, token)), requests_.SUPERSEDED_LINK, 410)

    def test_a_post_on_a_link_that_reaches_nothing_is_the_same_page(self):
        page = self.get()
        csrf = forms_of(self.html(page))[0].control("csrfmiddlewaretoken").value
        for name in ("staff:sign_send_code", "staff:sign_check_code", "staff:sign_submit"):
            with self.subTest(name=name):
                answer = self.client.post(self.route(name, "inconnu"), {"csrfmiddlewaretoken": csrf, "code": "123456"})
                self.assertPlainPage(answer, requests_.UNKNOWN_LINK, 404)

    def test_opening_the_link_is_logged_once_per_session(self):
        self.get()
        self.get()
        opened = self.request.events.filter(kind=Kind.LINK_OPENED)
        self.assertEqual(opened.count(), 1)
        self.assertEqual((opened[0].ip, opened[0].user_agent), (IP, PHONE))
        Client(REMOTE_ADDR="198.51.100.4").get(self.url)
        self.assertEqual(opened.count(), 2)


# -- What he reads ----------------------------------------------------------------------------------------------


class PageTests(PublicCase):
    def test_the_month_as_it_was_frozen(self):
        page = self.get()
        text = self.text(page)
        self.assertIn("BAR EXEMPLE", text)
        self.assertIn("12 rue Imaginaire", text)
        self.assertRegex(
            unescape(self.html(page)), r"<h1>\s*Relevé d'heures — Mois de juin 2026 — DUPONT Jeanne\s*</h1>"
        )
        self.assertRegex(self.html(page), r"Mardi 2</th>\s*<td class=\"num\">9</td>\s*<td>inventaire</td>")
        self.assertIn("Total semaine 37,5 h", text)
        self.assertIn("Heures travaillées : 153 h", text)
        self.assertIn(f'href="{self.route("staff:sign_document")}"', self.html(page))
        self.assertIn("Voir le PDF", text)
        # Hours changed in the database behind the lock's back do not change
        # what he is shown: the page is the snapshot taken when it was sent.
        TimesheetDay.objects.filter(timesheet__employee=self.person, date=date(2026, 6, 2)).update(hours=Decimal("10"))
        self.assertRegex(self.html(self.get()), r"Mardi 2</th>\s*<td class=\"num\">9</td>")

    def test_the_view_pdf_link_is_the_frozen_document_shown_inline(self):
        response = self.get(self.route("staff:sign_document"))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response["Content-Disposition"].startswith("inline; filename=\"Releve d'heures DUPONT Jeanne"))
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.content, private_files.read(self.request.uuid, private_files.DOCUMENT))
        self.get(self.route("staff:sign_document"))
        self.assertEqual(self.request.events.filter(kind=Kind.DOWNLOADED).count(), 1)

    def test_a_document_changed_on_disk_is_not_shown(self):
        path = private_files.request_dir(self.request.uuid) / private_files.DOCUMENT
        path.write_bytes(path.read_bytes() + b"%")
        response = self.client.get(self.route("staff:sign_document"))
        self.assertEqual(response.status_code, 500)
        self.assertIn(public_views.ALTERED_DOCUMENT, self.text(response))

    def test_his_copy_before_he_signed_is_a_french_404(self):
        response = self.client.get(self.route("staff:sign_copy"))
        self.assertEqual(response.status_code, 404)
        self.assertIn(public_views.NOT_SIGNED_YET, self.text(response))

    def test_the_notice_says_what_is_recorded_why_and_for_how_long(self):
        self.identify()
        text = self.text(self.get())
        for words in (
            "la date et l'heure",
            "votre adresse IP",
            "votre appareil",
            "le dessin de votre signature",
            "5 ans après la fin du mois",
            "signature électronique simple",
            "RGPD",
        ):
            with self.subTest(words=words):
                self.assertIn(words, text)
        for never in ("qualifiée", "avancée", "équivalente à une signature manuscrite"):
            self.assertNotIn(never, text)


class NothingElseTests(PublicCase):
    def test_no_other_employee_no_owner_page_no_full_address(self):
        other = employee(last_name="Martin", first_name="Paul")
        other.email = "paul.martin@example.invalid"
        other.save()
        save_month(other, JUNE, [PostedDay(date(2026, 6, 3), hours=Decimal("4"), note="secret du voisin")])
        _theirs, their_token = requests_.create_request(other, JUNE)
        for url in (self.url, self.route("staff:sign_copy")):
            response = self.client.get(url)
            html = self.html(response)
            for leak in ("MARTIN", "secret du voisin", "paul.martin", their_token, ADDRESS, 'class="topbar"'):
                with self.subTest(url=url, leak=leak):
                    self.assertNotIn(leak, html)
        # Every address the page names is its own, or a static file.
        html = self.html(self.get())
        own = f"/personnel/signer/{self.token}/"
        for address in re.findall(r'(?:href|action|src)="([^"]*)"', html):
            with self.subTest(address=address):
                self.assertTrue(address.startswith((own, "/static/")), address)

    def test_the_owner_s_messages_never_reach_it(self):
        """The same browser, the owner's page answering with a message not
        read yet: the employee's page does not draw it, and leaves it. The
        owner is logged in on it - his pages want a login (this client,
        the employee's phone, is Django's own and anonymous)."""
        self.client.force_login(runner.test_user())
        owner = reverse("staff:employee", args=[self.person.pk])
        page = self.client.get(owner)
        self.assertEqual(page.status_code, 200)
        form = form_posting_to(page.content.decode(), owner)
        self.client.post(form.action.split("#")[0], as_post(form.submission(values={"first_name": "Jeanne-Marie"})))
        self.assertNotIn("Nom enregistré", self.text(self.get()))
        self.assertIn("Nom enregistré : DUPONT Jeanne-Marie.", self.text(self.client.get(owner)))

    def test_its_headers(self):
        response = self.get()
        self.assertEqual(response["Cache-Control"], "no-store")
        # Not « no-referrer »: the browser would then post `Origin: null`, and
        # Django's CSRF check refuses that (the browser test found it).
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn('<meta name="referrer" content="same-origin">', self.html(response))
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertIn("noindex", response["X-Robots-Tag"])
        policy = response["Content-Security-Policy"]
        for rule in ("default-src 'none'", "script-src 'self'", "frame-ancestors 'none'", "form-action 'self'"):
            self.assertIn(rule, policy)
        self.assertIn('<meta name="robots" content="noindex, nofollow">', self.html(response))
        # No inline script and no inline style: the policy would block them.
        self.assertNotRegex(self.html(response), r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotIn("style=", self.html(response))

    def test_the_token_is_stored_nowhere(self):
        self.identify()
        self.sign(self.get())
        requests_.countersign_request(self.refresh(), employer_signature())
        self.get(self.route("staff:sign_copy"))
        for rows in (
            SignatureRequest.objects.values(),
            SignatureEvent.objects.values(),
            Employee.objects.values(),
        ):
            self.assertNotIn(self.token, str(list(rows)))
        for session in Session.objects.all():
            self.assertNotIn(self.token, str(session.get_decoded()))


# -- Who he is --------------------------------------------------------------------------------------------------


class IdentificationTests(PublicCase):
    CODE = re.compile(r"\b([0-9]{6})\b")

    @override_settings(**MAIL)
    def test_a_code_by_email_then_typed(self):
        page = self.get()
        text = self.text(page)
        self.assertIn("Recevoir un code par e-mail", text)
        self.assertIn("j•••t@example.invalid", text)
        self.assertNotIn(ADDRESS, self.html(page))
        self.assertNotIn(self.route("staff:sign_submit"), self.html(page))
        answer = self.post(self.form(page, "staff:sign_send_code"))
        # Back to « 1. Votre code », under the month - not to its top.
        self.assertEqual(answer.redirect_chain, [(self.url + "#code", 302)])
        self.assertIn("Code envoyé à j•••t@example.invalid : il vaut 15 minutes.", self.text(answer))
        (message,) = mail.outbox
        self.assertEqual(message.to, [ADDRESS])
        code = self.CODE.search(message.body).group(1)
        answer = self.post(self.form(answer, "staff:sign_check_code"), {"code": code})
        self.assertIn(public_views.CODE_VERIFIED, self.text(answer))
        self.form(answer, "staff:sign_submit")
        self.assertEqual(self.kinds()[-2:], [Kind.CODE_SENT, Kind.CODE_VERIFIED])

    def test_without_email_the_code_comes_from_the_employer(self):
        page = self.get()
        self.assertNotIn(self.route("staff:sign_send_code"), self.html(page))
        self.assertIn("Votre employeur vous donne ce code", self.text(page))
        answer = self.identify()
        self.assertIn(public_views.CODE_VERIFIED, self.text(answer))
        self.form(answer, "staff:sign_submit")

    @override_settings(**MAIL)
    def test_a_verified_code_is_said_once(self):
        """Verified, the code is said ONCE - « Code vérifié : vous pouvez
        signer. » in « 1. Votre code », over the frame: the redirect carries
        no notice of its own saying the same thing again (« C'est bien vous :
        vous pouvez signer. » stood under it, seen at 375 px), and the notice
        of the e-mail that brought it is gone."""
        sent = self.post(self.form(self.get(), "staff:sign_send_code"))
        code = self.CODE.search(mail.outbox[-1].body).group(1)
        by_mail = self.post(self.form(sent, "staff:sign_check_code"), {"code": code})
        # Another browser, the employer's code this time.
        self.client = Client(enforce_csrf_checks=True, REMOTE_ADDR=IP, HTTP_USER_AGENT=PHONE)
        handed_over = self.identify()
        for name, answer in (("by e-mail", by_mail), ("handed over", handed_over), ("drawn again", self.get())):
            with self.subTest(name):
                text = self.text(answer)
                self.assertEqual(text.count("vous pouvez signer"), 1, text)
                self.assertIn(public_views.CODE_VERIFIED, text)
                self.assertNotIn("C'est bien vous", text)
                self.assertNotIn("Code envoyé", text)
                self.assertEqual(self.html(answer).count("message-success"), 1)

    def test_a_wrong_code_says_how_many_tries_are_left(self):
        requests_.issue_code(self.request, SignatureRequest.Identification.CODE_HANDED_OVER)
        answer = self.post(self.form(self.get(), "staff:sign_check_code"), {"code": "000000"})
        self.assertIn("Code erroné. Encore 4 essais avec ce code.", self.text(answer))
        self.assertNotIn(self.route("staff:sign_submit"), self.html(answer))
        self.assertEqual(self.kinds()[-1], Kind.CODE_FAILED)

    def test_the_session_changes_its_key_once_he_is_identified(self):
        self.get()
        before = self.client.session.session_key
        self.identify()
        self.assertNotEqual(self.client.session.session_key, before)
        self.assertTrue(requests_.is_identified(self.client.session, self.refresh()))

    def test_a_code_verified_for_one_month_unlocks_no_other(self):
        save_month(self.person, JULY, [])
        july, july_token = requests_.create_request(self.person, JULY)
        self.identify()
        page = self.get(self.route("staff:sign", july_token))
        self.assertNotIn(self.route("staff:sign_submit", july_token), self.html(page))
        csrf = forms_of(self.html(page))[0].control("csrfmiddlewaretoken").value
        answer = self.client.post(
            self.route("staff:sign_submit", july_token),
            {"csrfmiddlewaretoken": csrf, "signature": data_url(drawn_signature()), "certification": "1"},
        )
        self.assertEqual(answer.status_code, 403)
        self.assertIn(public_views.IDENTIFY_FIRST, self.text(answer))
        july.refresh_from_db()
        self.assertEqual(july.status, Status.PENDING)

    @override_settings(**MAIL)
    def test_three_codes_an_hour(self):
        for _ in range(3):
            self.post(self.form(self.get(), "staff:sign_send_code"))
        answer = self.post(self.form(self.get(), "staff:sign_send_code"))
        self.assertIn("Déjà 3 codes demandés dans l'heure", self.text(answer))
        self.assertEqual(len(mail.outbox), 3)

    @override_settings(**FAILING_MAIL)
    def test_a_mail_that_cannot_leave_is_said_plainly(self):
        answer = self.post(self.form(self.get(), "staff:sign_send_code"))
        self.assertIn(public_views.MAIL_FAILED, self.text(answer))
        self.assertEqual(self.kinds()[-1], Kind.MAIL_FAILED)

    @override_settings(**MAIL)
    def test_once_a_code_is_sent_typing_it_comes_first_and_a_new_one_says_it_replaces_it(self):
        """A second tap on « Recevoir un code par e-mail » cancels the code
        on its way, and nothing said so (review, 28/09): the first mail's
        genuine code then read « Code erroné »."""
        answer = self.post(self.form(self.get(), "staff:sign_send_code"))
        html = self.html(answer)
        self.assertLess(html.index('id="code-field"'), html.index(f'action="{self.route("staff:sign_send_code")}"'))
        check = re.search(
            rf'<form[^>]*action="{re.escape(self.route("staff:sign_check_code"))}".*?</form>', html, re.DOTALL
        )
        self.assertRegex(check.group(0), r'<button class="btn" type="submit">Valider le code</button>')
        send = re.search(
            rf'<form[^>]*action="{re.escape(self.route("staff:sign_send_code"))}".*?</form>', html, re.DOTALL
        )
        self.assertRegex(
            send.group(0),
            r'<button class="btn btn-secondary" type="submit">Renvoyer un code \(le précédent ne vaudra plus\)</button>',
        )
        self.assertIn("Il remplace tout code demandé avant.", self.text(answer))
        self.assertIn("Il remplace tout code demandé avant.", mail.outbox[-1].body)

    @override_settings(**MAIL)
    def test_a_code_handed_over_survives_a_visitor_holding_only_the_link(self):
        """Anyone with the link could press « Recevoir un code par e-mail »
        until the employer's code was void and the hour's quota spent
        (review, 28/09)."""
        code = requests_.issue_code(self.request, SignatureRequest.Identification.CODE_HANDED_OVER)
        page = self.get()
        # Not offered while the employer's code waits - and refused when posted all the same.
        self.assertNotIn(self.route("staff:sign_send_code"), self.html(page))
        self.assertIn("Le code que votre employeur vous a donné", self.text(page))
        visitor = Client(enforce_csrf_checks=True, REMOTE_ADDR="198.51.100.4", HTTP_USER_AGENT="Autre/1.0")
        for _ in range(2):
            html = visitor.get(self.url).content.decode()
            csrf = forms_of(html)[0].control("csrfmiddlewaretoken").value
            answer = visitor.post(self.route("staff:sign_send_code"), {"csrfmiddlewaretoken": csrf}, follow=True)
            self.assertIn(requests_.HANDED_OVER_CODE_WAITING, self.text(answer))
        self.assertEqual(mail.outbox, [])
        answer = self.post(self.form(self.get(), "staff:sign_check_code"), {"code": code})
        self.assertIn(public_views.CODE_VERIFIED, self.text(answer))


# -- His signature ----------------------------------------------------------------------------------------------


class SigningTests(PublicCase):
    def test_signed_through_the_page(self):
        page = self.identify()
        form = self.form(page, "staff:sign_submit")
        self.assertEqual(form.control("signature").attrs["type"], "hidden")
        self.assertEqual(form.control("certification").attrs["type"], "checkbox")
        self.assertIn("required", form.control("certification").attrs)
        self.assertIn(
            "Je certifie que ce relevé correspond aux heures que j'ai effectuées en juin 2026.", self.text(page)
        )
        answer = self.sign(page)
        self.assertEqual(answer.redirect_chain, [(self.url, 302)])
        request = self.refresh()
        self.assertEqual(request.status, Status.EMPLOYEE_SIGNED)
        signed = self.kinds()[-1]
        self.assertEqual(signed, Kind.EMPLOYEE_SIGNED)
        event = request.events.get(kind=Kind.EMPLOYEE_SIGNED)
        self.assertEqual((event.ip, event.user_agent), (IP, PHONE))
        text = self.text(answer)
        self.assertIn("Relevé signé le", text)
        self.assertIn("Votre employeur va le contresigner", text)
        self.assertNotIn(self.route("staff:sign_submit"), self.html(answer))
        # His copy, from the page.
        self.assertIn(f'href="{self.route("staff:sign_copy")}"', self.html(answer))
        copy = self.get(self.route("staff:sign_copy"))
        self.assertEqual(copy.content, private_files.read(request.uuid, private_files.EMPLOYEE_SIGNED))
        # Named for what it is - her signature, the employer's still to come -
        # rather than « signé par le salarié » (review, 28/09).
        self.assertIn('juin 2026 signe, avant contreseing.pdf"', copy["Content-Disposition"])
        verification = signing.verify(copy.content)
        self.assertTrue(verification.ok, verification.verdict)

    def test_with_his_reservations(self):
        page = self.identify()
        self.sign(page, avec_reserves=True, reserves="Le 12, j'ai fini à 23 h et non à 22 h.")
        request = self.refresh()
        self.assertEqual(request.reservation, "Le 12, j'ai fini à 23 h et non à 22 h.")
        self.assertIn("avec vos réserves", self.text(self.get()))

    def test_every_refusal_draws_the_page_back_and_signs_nothing(self):
        page = self.identify()
        drawing = data_url(drawn_signature())
        statement = "Je certifie que ce relevé correspond aux heures que j'ai effectuées en juin 2026."
        for values, message, drawing_kept in (
            ({"certification": False}, f"Cochez « {statement} » pour signer.", True),
            ({"signature": data_url(png())}, signing.DRAWING_EMPTY, False),  # « avant de signer »
            ({"signature": "data:image/jpeg;base64,/9j/4AAQ"}, "La signature doit être une image PNG", False),
            ({"signature": data_url(png(1300, 400, (0, 0, 0, 255)))}, "trop grande", False),
            ({"signature": ""}, "La signature n'a pas été reçue", False),
            ({"avec_reserves": True, "reserves": "  "}, public_views.RESERVATION_EMPTY, True),
            ({"reserves": "Le 12, j'ai fini plus tard."}, public_views.RESERVATION_UNTICKED, True),
        ):
            with self.subTest(message=message):
                values = {"signature": drawing, "certification": True, **values}
                answer = self.post(self.form(page, "staff:sign_submit"), values, follow=False)
                self.assertEqual(answer.status_code, 200)
                self.assertIn(message, self.text(answer))
                form = self.form(answer, "staff:sign_submit")
                self.assertEqual(form.control("signature").value, drawing if drawing_kept else "")
                self.assertEqual(form.control("reserves").value.strip(), values.get("reserves", "").strip())
        request = self.refresh()
        self.assertEqual(request.status, Status.PENDING)
        self.assertFalse(private_files.exists(request.uuid, private_files.EMPLOYEE_SIGNED))
        self.assertNotIn(Kind.EMPLOYEE_SIGNED, self.kinds())

    def test_no_timestamp_no_signature(self):
        page = self.identify()
        with OfflineTimestamps(stampers=[FailingTimestamper()]):
            answer = self.sign(page)
        self.assertEqual(answer.status_code, 503)
        self.assertIn(signing.TIMESTAMP_REFUSED, self.text(answer))
        request = self.refresh()
        self.assertEqual(request.status, Status.PENDING)
        self.assertFalse(private_files.exists(request.uuid, private_files.EMPLOYEE_SIGNED))
        self.assertEqual(self.kinds()[-1], Kind.TIMESTAMP_FAILED)
        # Drawn back ready to try again: his drawing is still in the form.
        self.assertTrue(self.form(answer, "staff:sign_submit").control("signature").value.startswith("data:image/png"))

    def test_a_second_post_signs_nothing_more(self):
        page = self.identify()
        self.sign(page)
        answer = self.sign(page)
        self.assertIn(public_views.ALREADY_SIGNED, self.text(answer))
        self.assertEqual(self.kinds().count(Kind.EMPLOYEE_SIGNED), 1)

    def test_nobody_signs_without_the_code(self):
        page = self.get()
        self.assertNotIn(self.route("staff:sign_submit"), self.html(page))
        self.assertIn("Validez d'abord votre code", self.text(page))
        csrf = forms_of(self.html(page))[0].control("csrfmiddlewaretoken").value
        answer = self.client.post(
            self.route("staff:sign_submit"),
            {"csrfmiddlewaretoken": csrf, "signature": data_url(drawn_signature()), "certification": "1"},
        )
        self.assertEqual(answer.status_code, 403)
        self.assertIn(public_views.IDENTIFY_FIRST, self.text(answer))
        self.assertEqual(self.refresh().status, Status.PENDING)

    def test_a_code_typed_hours_ago_signs_nothing(self):
        """A shared phone: he typed his code and closed the tab; whoever
        opens the link from the history later is asked for a code again."""
        page = self.identify()
        self.assertIn(public_views.CODE_VERIFIED, self.text(page))
        later = timezone.now() + requests_.IDENTIFICATION_VALIDITY + timedelta(minutes=1)
        with mock.patch.object(requests_, "_now", lambda now=None: now or later):
            again = self.get()
            self.assertNotIn(public_views.CODE_VERIFIED, self.text(again))
            self.assertNotIn(self.route("staff:sign_submit"), self.html(again))
            answer = self.sign(page)
        self.assertEqual(answer.status_code, 403)
        self.assertIn(public_views.IDENTIFY_FIRST, self.text(answer))
        self.assertEqual(self.refresh().status, Status.PENDING)

    def test_after_countersigning_the_final_copy(self):
        self.sign(self.identify())
        request = requests_.countersign_request(self.refresh(), employer_signature())
        page = self.get()
        text = self.text(page)
        self.assertIn("contresigné par BAR EXEMPLE le", text)
        self.assertIn("Télécharger l'exemplaire final (PDF)", text)
        copy = self.get(self.route("staff:sign_copy"))
        self.assertEqual(hashlib.sha256(copy.content).hexdigest(), request.final_pdf_sha256)
        self.assertIn(
            "filename*=UTF-8''Relev%C3%A9%20d%27heures%20DUPONT%20Jeanne%20juin%202026%20sign%C3%A9.pdf",
            copy["Content-Disposition"],
        )


class SigningErrorPageTests(PublicCase):
    """A signature that cannot be made is a French sentence for him, never
    the server's insides nor a 500 (review, 28/09)."""

    LEAKS = ("Errno", "No such file", "MARGINMATE", "Traceback", ".pem", "private", "Server Error")

    def assertSaysNothingTechnical(self, answer):
        html = self.html(answer)
        for leak in (*self.LEAKS, str(self.private_dir), str(self.private_dir).replace("\\", "\\\\")):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, html)
        self.assertEqual(self.refresh().status, Status.PENDING)
        self.assertFalse(private_files.exists(self.request.uuid, private_files.EMPLOYEE_SIGNED))

    def test_long_accented_names_sign(self):
        """« Autorité interne de » plus a 46-character name holding an « é »
        is 67 bytes, past a certificate's 64: that was an HTTP 500 here."""
        Establishment.objects.filter(pk=Establishment.SINGLETON_PK).update(
            name="Brasserie Imaginaire du Faubourg Saint-Exemple"
        )
        self.person.last_name, self.person.first_name = (
            "De La Fontaine-Saint-Exemple",
            "Marie-Hélène Éléonore Françoise",
        )
        self.person.save()
        answer = self.sign(self.identify())
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.refresh().status, Status.EMPLOYEE_SIGNED)

    def test_a_missing_document(self):
        page = self.identify()
        (private_files.request_dir(self.request.uuid) / private_files.DOCUMENT).unlink()
        with self.assertLogs("staff.signature_requests", "WARNING"):
            answer = self.sign(page)
        self.assertIn(requests_.DOCUMENT_CHANGED, self.text(answer))
        self.assertSaysNothingTechnical(answer)

    def test_keys_that_cannot_be_opened(self):
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase de passe d'essai"):
            signing.authority(Establishment.objects.get(pk=Establishment.SINGLETON_PK))
        page = self.identify()
        with self.assertLogs("staff.signature_requests", "WARNING"):
            answer = self.sign(page)
        self.assertIn(requests_.NOT_SIGNED, self.text(answer))
        self.assertSaysNothingTechnical(answer)

    def test_anything_unforeseen_is_the_same_sentence(self):
        page = self.identify()
        with mock.patch("staff.signing.sign_as_employee", side_effect=RuntimeError("C:\\Users\\quelqu'un\\private")):
            with self.assertLogs("staff.public_views", "ERROR"):
                answer = self.sign(page)
        self.assertEqual(answer.status_code, 500)
        self.assertIn(requests_.NOT_SIGNED, self.text(answer))
        self.assertNotIn("quelqu'un", self.html(answer))
        self.assertSaysNothingTechnical(answer)


# -- Forms and methods ------------------------------------------------------------------------------------------


class SecurityTests(PublicCase):
    ACTIONS = ("staff:sign_send_code", "staff:sign_check_code", "staff:sign_submit")

    def test_every_post_needs_its_csrf_token_and_says_so_in_french(self):
        self.get()
        for name in self.ACTIONS:
            with self.subTest(name=name):
                answer = self.client.post(self.route(name), {"code": "123456", "certification": "1"})
                self.assertEqual(answer.status_code, 403)
                self.assertIn(public_views.PAGE_EXPIRED, self.text(answer))
                self.assertNotIn("DUPONT", self.html(answer))
        self.assertEqual(self.kinds(), [Kind.CREATED, Kind.LINK_OPENED])

    def test_the_owner_s_pages_keep_django_s_own_refusal(self):
        answer = self.client.post(reverse("staff:employee", args=[self.person.pk]), {})
        self.assertEqual(answer.status_code, 403)
        self.assertNotIn(public_views.PAGE_EXPIRED, self.text(answer))

    def test_a_get_on_an_action_goes_back_to_the_page(self):
        for name, anchor in zip(self.ACTIONS, ("#code", "#code", "")):
            with self.subTest(name=name):
                self.assertRedirects(self.client.get(self.route(name)), self.url + anchor)
        self.assertNotIn(Kind.CODE_SENT, self.kinds())
