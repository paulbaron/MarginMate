"""The owner's side of the monthly signature, on the month's page
(`staff:month`, its « Signature » section, staff/signature_views.py): send
the saved month, the link shown once with « Copier », the code to hand over,
« Contresigner », « Annuler la demande », « Corriger ce mois », « Vérifier »,
the downloads, the history - and the month read-only while a request holds
it.

Every POST is read OFF THE RENDERED PAGE (`page_forms`) through a client
that enforces CSRF, as the other staff pages' tests do. E-mail goes to
Django's locmem outbox; the timestamps are the offline authority of
staff/tests/signing_support.py; keys and files live in a temp folder. Names
and addresses are INVENTED (DUPONT Jeanne, BAR EXEMPLE, example.invalid)."""

import hashlib
import io
import re
from datetime import date
from decimal import Decimal
from html import unescape

import pdfplumber
from django.contrib.sessions.models import Session
from django.core import mail
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from staff import private_files, signature_requests as requests_, signing
from staff.models import Establishment, SignatureEvent, SignatureRequest, Timesheet
from staff.signature_views import DRAWING
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from staff.tests.signing_support import (
    FailingTimestamper,
    OfflineTimestamps,
    SigningTestMixin,
    blank_canvas,
    countersign_without_a_drawing,
    data_url,
    drawn_signature,
    employer_signature,
)
from staff.tests.support import employee
from staff.tests.test_views import JUNE, MAY, PageTestCase, month_url, stored
from staff.pdf import render_month_pdf
from staff.timesheet import MONTH_LOCKED, PostedDay, month_sheet, save_month
from tests.support import NoNetworkTestCase

JULY = date(2026, 7, 1)
MAIL = {"EMAIL_HOST": "smtp.example.invalid"}   # « configured »: locmem sends nothing anywhere
FAILING_MAIL = {**MAIL, "EMAIL_BACKEND": "staff.tests.test_signature_mail.FailingBackend"}
ADDRESS = "jeanne.dupont@example.invalid"

#: The link as the owner's page shows it, in the field « Copier » copies.
LINK = re.compile(r'id="signature-link"[^>]*value="(https?://[^"]+/personnel/signer/([A-Za-z0-9_-]+)/)"')
#: The code to hand over, as the page shows it once.
CODE = re.compile(r"data-handed-code>([0-9]{3}) ([0-9]{3})<")
FILE_LINK = re.compile(r'href="(/personnel/[0-9]+/2026-06/signature/([0-9]+)/fichier/([a-z-]+)/)"')

Status = SignatureRequest.Status
Kind = SignatureEvent.Kind


class OwnerCase(SigningTestMixin, NoNetworkTestCase, PageTestCase):
    """June 2026 saved - Tuesday 2 worked 9 h « inventaire » - for DUPONT
    Jeanne of BAR EXEMPLE."""

    def setUp(self):
        super().setUp()
        Establishment.objects.create(
            pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS"
        )
        self.person = employee()
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("9"), note="inventaire")])
        self.timesheet = Timesheet.objects.get(employee=self.person, month=JUNE)
        self.url = month_url(self.person)

    def route(self, name, *extra, person=None, month=JUNE):
        return reverse(name, args=[(person or self.person).pk, month, *extra])

    def page(self, url=None):
        return self.get(url or self.url)

    def html(self, response) -> str:
        return response.content.decode()

    def form(self, response, name, *extra, month=JUNE):
        return form_posting_to(self.html(response), self.route(name, *extra, month=month))

    def post_actions(self, response) -> set[str]:
        return {form.action.split("#")[0] for form in forms_of(self.html(response)) if form.method == "post"}

    def request_(self, version=1) -> SignatureRequest:
        return SignatureRequest.objects.get(timesheet=self.timesheet, version=version)

    def kinds(self, request) -> list[str]:
        return list(request.events.order_by("id").values_list("kind", flat=True))

    def create(self):
        return requests_.create_request(self.person, JUNE)

    def employee_signs(self, request, reservation=""):
        session = {}
        code = requests_.issue_code(request, SignatureRequest.Identification.CODE_HANDED_OVER)
        requests_.check_code(request, code, session)
        return requests_.sign_for_employee(
            request, drawn_signature(), session=session, statement_accepted=True, reservation=reservation,
            ip="203.0.113.7", user_agent="Mozilla/5.0 (Linux; Android 14) Essai/1.0",
        )

    def complete(self):
        request, token = self.create()
        request = self.employee_signs(request)
        return requests_.countersign_request(request, employer_signature()), token

    def countersign_form(self, response):
        return self.form(response, "staff:signature_countersign", 1)

    def countersign(self, response, png=None, *, follow=True):
        """« Contresigner » as the pad sends it: the drawing, as a PNG data
        URL, in the form's hidden field."""
        form = self.countersign_form(response)
        values = {DRAWING: data_url(employer_signature() if png is None else png)}
        return self.client.post(form.action.split("#")[0], as_post(form.submission(values=values)), follow=follow)


# -- The month read-only while a request holds it ---------------------------------------------------------------


class LockedMonthTests(OwnerCase):
    def test_a_stale_page_cannot_write_a_month_under_signature(self):
        """Drawn before the month was sent, the grid and its shortcuts still
        post: each is refused with the sentence saying why, and nothing is
        written - never a 500 (« Revenir à la semaine type » raised one)."""
        page = self.page()
        grid = form_posting_to(self.html(page), self.url)
        period = self.form(page, "staff:month_range")
        reset = self.form(page, "staff:month_reset")
        self.create()
        before = stored(self.person)
        for form, values in (
            (grid, {"heures-2026-06-03": "10"}),
            (period, {"debut": "2026-06-09", "fin": "2026-06-10", "motif": "conges"}),
            (reset, None),
        ):
            with self.subTest(action=form.action):
                answer = self.send(form, values=values)
                self.assertLandedOn(answer, self.url)
                self.assertIn(MONTH_LOCKED, " ".join(self.messages_of(answer)))
        self.assertEqual(stored(self.person), before)

    def test_the_holidays_button_of_a_stale_page_is_refused_too(self):
        save_month(self.person, MAY, [])
        page = self.page(month_url(self.person, MAY))
        holidays = self.form(page, "staff:month_holidays_off", month=MAY)
        requests_.create_request(self.person, MAY)
        before = stored(self.person, MAY)
        answer = self.send(holidays)
        self.assertLandedOn(answer, month_url(self.person, MAY))
        self.assertEqual(self.messages_of(answer), [f"{MONTH_LOCKED} Rien n'a été modifié."])
        self.assertEqual(stored(self.person, MAY), before)

    def test_the_page_of_a_month_under_signature_is_read_only(self):
        """No grid, no shortcut: the month as it is being signed, and
        « Corriger ce mois » - the one way back to editing it."""
        self.create()
        response = self.page()
        actions = self.post_actions(response)
        for name in ("staff:month", "staff:month_range", "staff:month_reset", "staff:signature_send"):
            with self.subTest(name=name):
                self.assertNotIn(self.route(name), actions)
        self.assertIn(self.route("staff:month_reopen"), actions)
        html = self.html(response)
        self.assertNotIn('name="heures-2026-06-02"', html)
        text = " ".join(self.text(response).split())
        self.assertIn(
            "En attente de signature (version 1) : le mois ne se modifie plus. Pour le corriger : "
            "« Corriger ce mois », dans la section Signature.",
            text,
        )
        # The days are there to read: Tuesday 2, 9 h, « inventaire ».
        self.assertRegex(html, r"Mardi 2</th>\s*<td class=\"num\">9</td>\s*<td>inventaire</td>")
        # The PDF of the month is still one click away.
        self.assertIn(self.route("staff:month_pdf"), html)

    def test_the_download_stays_the_paper_sheet_while_the_month_is_out_for_signature(self):
        """« Télécharger la fiche (PDF) » is the sheet to sign by hand - its
        « Lu et approuvé » included - while the document frozen for the
        electronic signature words its boxes « Signature électronique … »."""
        request, _token = self.create()
        sheet = month_sheet(self.person, JUNE)
        bar = Establishment.objects.get()
        response = self.client.get(self.route("staff:month_pdf"))
        self.assertEqual(response.content, render_month_pdf(sheet, bar))
        with pdfplumber.open(io.BytesIO(response.content)) as document:
            self.assertIn("Lu et approuvé", document.pages[0].extract_text())
        frozen = private_files.read(request.uuid, private_files.DOCUMENT)
        self.assertTrue(frozen.startswith(render_month_pdf(sheet, bar, electronic=True)))
        self.assertFalse(frozen.startswith(response.content))


# -- Sending ----------------------------------------------------------------------------------------------------


class SendTests(OwnerCase):
    def ticked(self, response):
        form = self.form(response, "staff:signature_send")
        return self.client.post(form.action, as_post(form.submission(values={"transmettre": True})))

    def test_an_unsaved_month_is_never_offered_for_signature(self):
        """A planning is not a record: the page says so, offers nothing, and
        a hand-made post is refused with the same sentence."""
        response = self.page(month_url(self.person, JULY))
        self.assertIn(signing.UNSAVED_MONTH, " ".join(self.text(response).split()))
        self.assertNotIn(self.route("staff:signature_send", month=JULY), self.post_actions(response))
        token = forms_of(self.html(response))[0].control("csrfmiddlewaretoken").value
        answer = self.client.post(
            self.route("staff:signature_send", month=JULY), {"csrfmiddlewaretoken": token, "transmettre": "1"},
            follow=True,
        )
        self.assertLandedOn(answer, month_url(self.person, JULY) + "#signature")
        self.assertEqual(self.messages_of(answer), [signing.UNSAVED_MONTH])
        self.assertFalse(SignatureRequest.objects.exists())

    def test_without_the_establishment_s_name_nothing_is_offered(self):
        Establishment.objects.filter(pk=Establishment.SINGLETON_PK).update(name="")
        response = self.page()
        self.assertIn(signing.NO_ESTABLISHMENT_NAME, " ".join(self.text(response).split()))
        self.assertNotIn(self.route("staff:signature_send"), self.post_actions(response))

    @override_settings(**MAIL)
    def test_sent_by_email_and_the_link_shown_once_with_copier(self):
        self.person.email = ADDRESS
        self.person.save()
        response = self.page()
        self.assertIn(f"Le lien partira par e-mail à {ADDRESS}", " ".join(self.text(response).split()))
        form = self.form(response, "staff:signature_send")
        self.assertNotIn("transmettre", form.names)
        # timesheet.js asks first when the grid holds changes not saved: it is
        # the SAVED month that is frozen.
        self.assertIn("le mois enregistré", form.attrs["data-leaves-grid"])

        answer = self.client.post(form.action, as_post(form.submission()))
        # Drawn in the answer itself: the link is stored nowhere, not even in the session.
        self.assertEqual(answer.status_code, 200)
        self.assertRendered(answer)
        request = self.request_()
        link, token = LINK.search(self.html(answer)).groups()
        self.assertEqual(link, f"http://testserver/personnel/signer/{token}/")
        self.assertEqual(request.token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertRegex(
            self.html(answer), r'<button type="button" class="btn[^"]*" data-copy="signature-link">Copier</button>'
        )
        self.assertEqual(
            self.messages_of(answer),
            [
                f"Mois de juin 2026 envoyé pour signature (version 1, document n° {request.uuid}) : il ne se "
                "modifie plus tant que la demande est en cours."
            ],
        )
        self.assertIn(f"E-mail envoyé à {ADDRESS}.", self.text(answer))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [ADDRESS])
        self.assertIn(link, mail.outbox[0].body)
        self.assertEqual(self.kinds(request), [Kind.CREATED, Kind.LINK_SENT])

        # Once: drawn again, the page has no link - and offers a new one.
        again = self.page()
        self.assertNotIn(token, self.html(again))
        self.assertIsNone(LINK.search(self.html(again)))
        self.form(again, "staff:signature_link", 1)

    def test_without_email_the_owner_ticks_that_he_hands_the_link_over(self):
        response = self.page()
        self.assertIn("Aucun serveur d'e-mail n'est configuré", self.text(response))
        form = self.form(response, "staff:signature_send")
        self.assertEqual(form.control("transmettre").attrs["type"], "checkbox")
        # Not ticked: refused, nothing made.
        answer = self.send(form)
        self.assertLandedOn(answer, self.url + "#signature")
        self.assertEqual(
            self.messages_of(answer),
            [
                "Cochez « Je transmettrai le lien moi-même » pour envoyer ce mois : aucun serveur d'e-mail n'est "
                "configuré, le lien ne peut pas partir par e-mail."
            ],
        )
        self.assertFalse(SignatureRequest.objects.exists())
        # Ticked: made, and the link is on the page to copy.
        answer = self.ticked(response)
        self.assertEqual(answer.status_code, 200)
        self.assertIsNotNone(LINK.search(self.html(answer)))
        self.assertIn("Transmettez ce lien à DUPONT Jeanne vous-même", " ".join(self.text(answer).split()))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.kinds(self.request_()), [Kind.CREATED])

    @override_settings(**MAIL)
    def test_an_employee_without_an_address_is_handed_the_link_too(self):
        response = self.page()
        self.assertIn("DUPONT Jeanne n'a pas d'adresse e-mail", self.text(response))
        answer = self.send(self.form(response, "staff:signature_send"))
        self.assertEqual(
            self.messages_of(answer),
            [
                "Cochez « Je transmettrai le lien moi-même » pour envoyer ce mois : DUPONT Jeanne n'a pas "
                "d'adresse e-mail, le lien ne peut pas partir par e-mail."
            ],
        )
        self.assertEqual(self.ticked(self.page()).status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_the_link_is_stored_nowhere_not_even_in_the_session(self):
        """Only its SHA-256 is kept: not in a row, not in the session (the
        database), not in a cookie - which is why the answer draws it
        rather than redirecting to a page that would have to find it."""
        _link, token = LINK.search(self.html(self.ticked(self.page()))).groups()
        for rows in (SignatureRequest.objects.values(), SignatureEvent.objects.values()):
            self.assertNotIn(token, str(list(rows)))
        for session in Session.objects.all():
            self.assertNotIn(token, str(session.get_decoded()))
        for cookie in self.client.cookies.values():
            self.assertNotIn(token, cookie.value)

    @override_settings(SITE_URL="https://bar.example.invalid")
    def test_the_site_s_address_wins_when_it_is_set(self):
        link, token = LINK.search(self.html(self.ticked(self.page()))).groups()
        self.assertEqual(link, f"https://bar.example.invalid/personnel/signer/{token}/")

    @override_settings(**FAILING_MAIL)
    def test_a_mail_that_fails_is_said_and_the_link_is_still_shown(self):
        self.person.email = ADDRESS
        self.person.save()
        form = self.form(self.page(), "staff:signature_send")
        answer = self.client.post(form.action, as_post(form.submission()))
        self.assertEqual(answer.status_code, 200)
        self.assertIsNotNone(LINK.search(self.html(answer)))
        self.assertIn(f"L'e-mail n'a pas pu être envoyé à {ADDRESS} (lien)", self.text(answer))
        self.assertEqual(self.kinds(self.request_()), [Kind.CREATED, Kind.MAIL_FAILED])

    def test_a_month_already_sent_is_not_sent_twice_from_a_stale_page(self):
        response = self.page()
        self.assertEqual(self.ticked(response).status_code, 200)
        answer = self.send(self.form(response, "staff:signature_send"), values={"transmettre": True})
        self.assertEqual(
            self.messages_of(answer),
            [
                "Le mois de juin 2026 a déjà une demande de signature en cours : « Corriger ce mois » l'annule "
                "avant d'en envoyer une nouvelle version."
            ],
        )
        self.assertEqual(SignatureRequest.objects.count(), 1)


# -- The link and the code, while the employee has not signed ---------------------------------------------------


class LinkAndCodeTests(OwnerCase):
    def test_a_new_link_replaces_the_old_one(self):
        request, old = self.create()
        answer = self.client.post(*self.submitted(self.form(self.page(), "staff:signature_link", 1)))
        self.assertEqual(answer.status_code, 200)
        link, new = LINK.search(self.html(answer)).groups()
        self.assertNotEqual(new, old)
        with self.assertRaises(requests_.LinkError):
            requests_.resolve_link(old)
        self.assertEqual(requests_.resolve_link(new), request)
        self.assertEqual(self.kinds(request)[-1], Kind.LINK_RENEWED)
        self.assertIn("l'ancien ne fonctionne plus", self.text(answer))

    @override_settings(**MAIL)
    def test_a_new_link_is_mailed_when_it_can_be(self):
        self.person.email = ADDRESS
        self.person.save()
        self.create()
        answer = self.client.post(*self.submitted(self.form(self.page(), "staff:signature_link", 1)))
        link, _token = LINK.search(self.html(answer)).groups()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(link, mail.outbox[0].body)

    def submitted(self, form, values=None):
        return form.action, as_post(form.submission(values=values))

    @override_settings(**MAIL)
    def test_a_new_link_is_mailed_as_what_it_is_for(self):
        """Signed but not countersigned: « vérifiez et signez » would be a lie,
        so nothing is mailed. Countersigned: his final copy, the new link with
        it."""
        self.person.email = ADDRESS
        self.person.save()
        request, _token = self.create()
        self.employee_signs(request)
        answer = self.client.post(*self.submitted(self.form(self.page(), "staff:signature_link", 1)))
        self.assertIsNotNone(LINK.search(self.html(answer)))
        self.assertEqual(mail.outbox, [])
        requests_.countersign_request(request, employer_signature())
        mail.outbox.clear()
        answer = self.client.post(*self.submitted(self.form(self.page(), "staff:signature_link", 1)))
        link, _token = LINK.search(self.html(answer)).groups()
        (message,) = mail.outbox
        self.assertIn(link, message.body)
        self.assertEqual(message.attachments[0][0], "Relevé d'heures DUPONT Jeanne juin 2026 signé.pdf")
        self.assertNotIn("signer votre relevé", message.body)

    def test_the_code_to_hand_over_is_shown_once_and_works(self):
        request, _token = self.create()
        page = self.page()
        self.assertIn("Code à transmettre par un autre canal que le lien", self.text(page))
        answer = self.client.post(*self.submitted(self.form(page, "staff:signature_code", 1)))
        self.assertEqual(answer.status_code, 200)
        code = "".join(CODE.search(self.html(answer)).groups())
        self.assertIn("il ne s'affichera plus", self.text(answer))
        request.refresh_from_db()
        self.assertEqual(request.code_method, SignatureRequest.Identification.CODE_HANDED_OVER)
        self.assertEqual(self.kinds(request)[-1], Kind.CODE_GIVEN)
        self.assertNotIn(code, str(list(SignatureRequest.objects.values())))
        # It is the code: the employee's page accepts it.
        requests_.check_code(request, code, {})
        # And it is never drawn again.
        self.assertIsNone(CODE.search(self.html(self.page())))

    def test_three_codes_an_hour_then_a_sentence(self):
        self.create()
        form = self.form(self.page(), "staff:signature_code", 1)
        for _ in range(3):
            self.assertIsNotNone(CODE.search(self.html(self.client.post(*self.submitted(form)))))
        answer = self.send(form)
        self.assertLandedOn(answer, self.url + "#signature")
        self.assertEqual(
            self.messages_of(answer),
            ["Déjà 3 codes demandés dans l'heure : attendez un peu avant d'en demander un autre."],
        )

    @override_settings(**MAIL)
    def test_with_email_the_code_to_hand_over_waits_behind_a_question(self):
        self.person.email = ADDRESS
        self.person.save()
        self.create()
        page = self.page()
        self.assertIn("L'e-mail du code n'arrive pas ?", self.text(page))
        self.form(page, "staff:signature_code", 1)


# -- Countersigning ---------------------------------------------------------------------------------------------


#: The pad of « Contresigner » as signature_pad.js finds it.
PAD = re.compile(
    r'<form method="post" action="(?P<action>[^"]+)"[^>]*\bdata-signature-form\b.*?</form>', re.S
)


class CountersignTests(OwnerCase):
    def test_contresigner_once_the_employee_signed(self):
        request, _token = self.create()
        self.assertNotIn(self.route("staff:signature_countersign", 1), self.post_actions(self.page()))
        self.employee_signs(request, reservation="Le 12, j'ai fini à 23 h.")
        page = self.page()
        text = " ".join(self.text(page).split())
        self.assertIn("Signée, à contresigner", text)
        self.assertIn("avec des réserves", text)
        self.assertIn("Le 12, j'ai fini à 23 h.", text)
        answer = self.countersign(page)
        self.assertLandedOn(answer, self.url + "#signature")
        request.refresh_from_db()
        self.assertEqual(request.status, Status.COMPLETE)
        final = private_files.read_checked(request.uuid, private_files.FINAL, request.final_pdf_sha256)
        self.assertTrue(signing.verify(final).ok)
        digest = requests_.employer_drawing_sha256(request)
        self.assertEqual(
            private_files.read_checked(request.uuid, private_files.EMPLOYER_SIGNATURE_IMAGE, digest),
            signing.clean_signature_png(employer_signature()),
        )
        until = request.expires_at.astimezone(signing.PARIS).strftime("%d/%m/%Y")
        self.assertEqual(
            self.messages_of(answer),
            [
                "Contresigné : le relevé de juin 2026 est signé des deux côtés (version 1).",
                f"Son lien lui donne son exemplaire final jusqu'au {until}.",
            ],
        )
        self.assertIn("Signée et contresignée", self.text(answer))
        self.assertNotIn(self.route("staff:signature_countersign", 1), self.post_actions(answer))

    @override_settings(**MAIL)
    def test_the_final_copy_goes_by_email(self):
        self.person.email = ADDRESS
        self.person.save()
        request, _token = self.create()
        self.employee_signs(request)
        answer = self.countersign(self.page())
        self.assertIn(f"Son exemplaire final est parti par e-mail à {ADDRESS}.", self.messages_of(answer))
        request.refresh_from_db()
        (message,) = mail.outbox
        self.assertEqual(message.to, [ADDRESS])
        (name, content, mimetype), = message.attachments
        self.assertEqual((name, mimetype), ("Relevé d'heures DUPONT Jeanne juin 2026 signé.pdf", "application/pdf"))
        self.assertEqual(hashlib.sha256(content).hexdigest(), request.final_pdf_sha256)
        self.assertEqual(self.kinds(request)[-2:], [Kind.COUNTERSIGNED, Kind.COPY_SENT])

    @override_settings(**FAILING_MAIL)
    def test_a_copy_that_cannot_be_mailed_is_said(self):
        self.person.email = ADDRESS
        self.person.save()
        request, _token = self.create()
        self.employee_signs(request)
        answer = self.countersign(self.page())
        self.assertIn(
            f"L'e-mail n'a pas pu être envoyé à {ADDRESS} (copie signée) : transmettez-le par un autre moyen (SMS, "
            "messagerie, en main propre).",
            self.messages_of(answer),
        )
        request.refresh_from_db()
        self.assertEqual(request.status, Status.COMPLETE)

    def pad(self, response) -> str:
        """The countersignature's form, as HTML."""
        found = [match for match in PAD.finditer(self.html(response))
                 if match.group("action").split("#")[0] == self.route("staff:signature_countersign", 1)]
        self.assertEqual(len(found), 1, "one pad posting to « Contresigner »")
        return found[0].group(0)

    def pad_error(self, response) -> str:
        found = re.search(r'<p class="field-error"[^>]*data-countersign-error[^>]*>(.*?)</p>', self.pad(response), re.S)
        return " ".join(unescape(found.group(1)).split()) if found else ""

    def refused_as_it_stands(self, request):
        """Nothing countersigned, nothing kept, nothing logged but what was."""
        request.refresh_from_db()
        self.assertEqual(request.status, Status.EMPLOYEE_SIGNED)
        self.assertEqual(request.final_pdf_sha256, "")
        for name in (private_files.FINAL, private_files.EMPLOYER_SIGNATURE_IMAGE):
            self.assertFalse(private_files.exists(request.uuid, name))
        self.assertNotIn(Kind.COUNTERSIGNED, self.kinds(request))

    def test_contresigner_opens_a_drawing_pad(self):
        """The owner draws his signature (28/09: « I cannot draw my signature
        as the employer »): the pad of the employee's page, its two buttons,
        the hidden field the drawing is posted in - and its script loaded
        only where there is something to countersign."""
        request, _token = self.create()
        self.assertNotIn("signature_pad.js", self.html(self.page()))
        self.employee_signs(request)
        page = self.page()
        self.assertIn("signature_pad.js", self.html(page))
        pad = self.pad(page)
        # « Contresigner… » opens it: folded until then, the pad under its summary.
        self.assertRegex(
            unescape(self.html(page)),
            r'<details class="[^"]*signature-countersign[^"]*" id="contresigner">\s*'
            r"<summary[^>]*>Contresigner…</summary>\s*<form[^>]*data-signature-form",
        )
        self.assertIn("data-signature-canvas", pad)
        self.assertRegex(pad, r'<button type="button"[^>]*data-signature-undo[^>]*>Annuler le dernier trait</button>')
        self.assertRegex(pad, r'<button type="button"[^>]*data-signature-clear[^>]*>Effacer</button>')
        self.assertRegex(pad, r"data-signature-missing[^>]*hidden")
        drawing = self.countersign_form(page).control(DRAWING)
        self.assertEqual((drawing.kind, drawing.value), ("hidden", ""))
        self.assertIn("data-signature-input", drawing.attrs)

    def test_an_empty_drawing_is_refused_in_french_and_the_pad_stays_open(self):
        """The form sent as the page draws it - nothing drawn, which the
        script stops first - is refused with a French sentence beside the
        pad, the pad still open; nothing is signed."""
        request, _token = self.create()
        self.employee_signs(request)
        form = self.countersign_form(self.page())
        answer = self.client.post(form.action.split("#")[0], as_post(form.submission()))
        self.assertEqual(answer.status_code, 200)
        self.assertRendered(answer)
        self.assertEqual(self.pad_error(answer), signing.EMPLOYER_DRAWING_MISSING)
        self.assertRegex(self.html(answer), r'<details class="[^"]*signature-countersign[^"]*" id="contresigner" open>')
        self.assertEqual(self.countersign_form(answer).control(DRAWING).value, "")
        self.refused_as_it_stands(request)

    def test_a_crafted_drawing_is_refused_never_a_500(self):
        request, _token = self.create()
        self.employee_signs(request)
        page = self.page()
        form = self.countersign_form(page)
        for posted, words in (
            ("data:image/png;base64,!!!", "illisible"),
            ("data:image/gif;base64,R0lGODlhAQABAAAAACw=", "image PNG"),
            ("javascript:alert(1)", "image PNG"),
            (data_url(blank_canvas()), signing.EMPLOYER_DRAWING_EMPTY),
            (data_url(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64), "illisible"),
            (data_url(drawn_signature(width=1300, height=400)), "trop grande"),
            ("data:image/png;base64," + "A" * (signing.MAX_SIGNATURE_BYTES * 2), "trop lourde"),
        ):
            with self.subTest(posted=posted[:40]):
                answer = self.client.post(
                    form.action.split("#")[0], as_post(form.submission(values={DRAWING: posted}))
                )
                self.assertEqual(answer.status_code, 200)
                if words == signing.EMPLOYER_DRAWING_EMPTY:
                    # A dot on the pad (review, 28/09): the owner's own sentence,
                    # « contresigner », whole - never the employee's « signer ».
                    self.assertEqual(self.pad_error(answer), words)
                else:
                    self.assertIn(words, self.pad_error(answer))
                # What was posted is no drawing: it is not drawn back.
                self.assertEqual(self.countersign_form(answer).control(DRAWING).value, "")
        self.refused_as_it_stands(request)

    def test_a_page_drawn_before_the_pad_is_refused_never_a_500(self):
        """A « Contresigner » of a page drawn before 28/09 posts its token
        and nothing else: the answer says to draw, on the page with the pad."""
        request, _token = self.create()
        self.employee_signs(request)
        token = forms_of(self.html(self.page()))[0].control("csrfmiddlewaretoken").value
        answer = self.client.post(self.route("staff:signature_countersign", 1), {"csrfmiddlewaretoken": token})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.pad_error(answer), signing.EMPLOYER_DRAWING_MISSING)
        self.refused_as_it_stands(request)

    def test_no_timestamp_no_countersignature_and_the_drawing_is_kept_on_the_page(self):
        request, _token = self.create()
        self.employee_signs(request)
        page = self.page()
        with OfflineTimestamps(stampers=[FailingTimestamper()]):
            answer = self.countersign(page, follow=False)
        self.assertEqual(answer.status_code, 503)
        self.assertEqual(self.pad_error(answer), signing.TIMESTAMP_REFUSED)
        # Painted back by the pad's script: the owner does not draw it again.
        self.assertEqual(self.countersign_form(answer).control(DRAWING).value, data_url(employer_signature()))
        self.assertEqual(self.kinds(request)[-1], Kind.TIMESTAMP_FAILED)
        self.refused_as_it_stands(request)
        # The same page, a server answering again: countersigned.
        answer = self.countersign(answer)
        self.assertLandedOn(answer, self.url + "#signature")
        request.refresh_from_db()
        self.assertEqual(request.status, Status.COMPLETE)

    def test_a_stale_contresigner_says_why_and_signs_nothing(self):
        request, _token = self.create()
        token = forms_of(self.html(self.page()))[0].control("csrfmiddlewaretoken").value
        answer = self.client.post(
            self.route("staff:signature_countersign", 1), {"csrfmiddlewaretoken": token}, follow=True
        )
        self.assertEqual(self.messages_of(answer), [requests_.COUNTERSIGN_TOO_EARLY])
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)

    def test_countersigned_twice_from_two_tabs_signs_once(self):
        request, _token = self.create()
        self.employee_signs(request)
        page = self.page()
        self.countersign(page)
        answer = self.countersign(page)
        self.assertLandedOn(answer, self.url + "#signature")
        self.assertEqual(self.messages_of(answer), ["Cette demande n'est pas à contresigner."])
        self.assertEqual(self.kinds(request).count(Kind.COUNTERSIGNED), 1)


# -- Cancelling and correcting ----------------------------------------------------------------------------------


class CancelAndCorrectTests(OwnerCase):
    def test_annuler_la_demande(self):
        request, token = self.create()
        answer = self.send(self.form(self.page(), "staff:signature_cancel", 1), values={"motif": "envoyé par erreur"})
        self.assertLandedOn(answer, self.url + "#signature")
        self.assertEqual(
            self.messages_of(answer),
            ["Demande annulée (version 1) : son lien ne fonctionne plus, et le mois se modifie de nouveau."],
        )
        request.refresh_from_db()
        self.assertEqual((request.status, request.cancelled_reason), (Status.CANCELLED, "envoyé par erreur"))
        self.assertTrue(private_files.exists(request.uuid, private_files.DOCUMENT))
        with self.assertRaises(requests_.LinkError) as caught:
            requests_.resolve_link(token)
        self.assertEqual(caught.exception.status, 410)
        # Editable again, and the version stays listed with why it ended.
        form_posting_to(self.html(answer), self.url)
        text = " ".join(self.text(answer).split())
        self.assertIn("Annulée", text)
        self.assertIn("motif : envoyé par erreur", text)

    def test_a_finished_request_is_not_cancelled_but_corrected(self):
        self.complete()
        actions = self.post_actions(self.page())
        self.assertNotIn(self.route("staff:signature_cancel", 1), actions)
        self.assertIn(self.route("staff:month_reopen"), actions)

    def test_corriger_ce_mois_supersedes_a_finished_request_and_keeps_its_files(self):
        request, _token = self.complete()
        answer = self.send(self.form(self.page(), "staff:month_reopen"), values={"motif": "heures du 12 corrigées"})
        self.assertLandedOn(answer, self.url + "#signature")
        self.assertEqual(
            self.messages_of(answer),
            [
                "Mois rouvert : la version 1, signée et contresignée, est remplacée et conservée. Corrigez le mois, "
                "puis envoyez la nouvelle version."
            ],
        )
        request.refresh_from_db()
        self.assertEqual((request.status, request.cancelled_reason), (Status.SUPERSEDED, "heures du 12 corrigées"))
        for name, digest in (
            (private_files.DOCUMENT, request.document_sha256),
            (private_files.EMPLOYEE_SIGNED, request.employee_pdf_sha256),
            (private_files.FINAL, request.final_pdf_sha256),
            (private_files.SIGNATURE_IMAGE, request.signature_png_sha256),
        ):
            with self.subTest(name=name):
                private_files.read_checked(request.uuid, name, digest)

        # The grid is back: correct Wednesday 3, then send the new version.
        corrected = self.send(form_posting_to(self.html(answer), self.url), values={"heures-2026-06-03": "8"})
        form = self.form(corrected, "staff:signature_send")
        sent = self.client.post(form.action, as_post(form.submission(values={"transmettre": True})))
        self.assertEqual(sent.status_code, 200)
        second = self.request_(2)
        self.assertEqual(second.status, Status.PENDING)
        wednesday = second.month_snapshot["weeks"][0]["days"][2]
        self.assertEqual((wednesday["name"], wednesday["hours"]), ("Mercredi 3", "8"))
        # Version 1 stays listed, replaced, with its files still offered.
        page = self.page()
        text = " ".join(self.text(page).split())
        self.assertIn("Remplacée par une nouvelle version", text)
        self.assertIn(("1", "signe"), {(version, slug) for _url, version, slug in FILE_LINK.findall(self.html(page))})

    def test_corriger_ce_mois_cancels_a_waiting_request(self):
        request, _token = self.create()
        answer = self.send(self.form(self.page(), "staff:month_reopen"))
        self.assertEqual(
            self.messages_of(answer),
            [
                "Mois rouvert : la version 1 est annulée, son lien ne fonctionne plus. Corrigez le mois, puis "
                "envoyez la nouvelle version."
            ],
        )
        request.refresh_from_db()
        self.assertEqual((request.status, request.cancelled_reason), (Status.CANCELLED, "mois corrigé par l'employeur"))

    def test_corriger_a_month_nobody_holds_says_so(self):
        self.create()
        form = self.form(self.page(), "staff:month_reopen")
        self.send(form)
        answer = self.send(form)
        self.assertEqual(self.messages_of(answer), ["Ce mois n'est pas en cours de signature : il se modifie déjà."])


# -- Checking and downloading -----------------------------------------------------------------------------------


class VerifyAndDownloadTests(OwnerCase):
    def verify(self):
        form = self.form(self.page(), "staff:signature_verify", 1)
        answer = self.client.post(form.action, as_post(form.submission()))
        self.assertEqual(answer.status_code, 200)
        return answer

    def test_verifier_says_intact_who_signed_and_when(self):
        request, _token = self.complete()
        text = " ".join(self.text(self.verify()).split())
        self.assertIn("Document intact : signé par DUPONT Jeanne, horodaté le", text)
        self.assertIn("contresigné par BAR EXEMPLE", text)
        self.assertIn(signing.ADOBE_UNKNOWN_VALIDITY, text)
        self.assertIn(signing.authority_fingerprint(), text)
        self.assertEqual(self.kinds(request)[-1], Kind.VERIFIED)

    def test_verifier_before_any_signature(self):
        self.create()
        self.assertIn("Aucune signature : c'est le document tel qu'il a été figé", self.text(self.verify()))

    def test_verifier_notices_a_file_changed_on_disk(self):
        request, _token = self.complete()
        path = private_files.request_dir(request.uuid) / private_files.FINAL
        path.write_bytes(path.read_bytes().replace(b"DUPONT", b"DUPOND", 1))
        text = self.text(self.verify())
        self.assertIn("ne correspond plus à l'empreinte enregistrée", text)

    def test_every_file_is_downloaded_from_the_page_and_logged(self):
        request, _token = self.complete()
        page = self.page()
        links = {slug: url for url, version, slug in FILE_LINK.findall(self.html(page)) if version == "1"}
        self.assertEqual(sorted(links), ["dessin", "dessin-employeur", "document", "preuve", "signe", "signe-salarie"])
        # The two drawings side by side, each saying whose it is.
        words = " ".join(unescape(re.sub(r"<[^>]+>", " ", self.html(page))).split())
        self.assertIn("Signature dessinée de DUPONT Jeanne (PNG) Signature dessinée de l'employeur (PNG)", words)
        expected = {
            "document": (private_files.DOCUMENT, "application/pdf"),
            "signe-salarie": (private_files.EMPLOYEE_SIGNED, "application/pdf"),
            "signe": (private_files.FINAL, "application/pdf"),
            "dessin": (private_files.SIGNATURE_IMAGE, "image/png"),
            "dessin-employeur": (private_files.EMPLOYER_SIGNATURE_IMAGE, "image/png"),
            "preuve": (private_files.PROOF, "application/pdf"),
        }
        for slug, (name, content_type) in expected.items():
            with self.subTest(file=slug):
                response = self.client.get(links[slug])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], content_type)
                self.assertTrue(response["Content-Disposition"].startswith("attachment; filename="))
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response.content, private_files.read(request.uuid, name))
        downloaded = [
            event.detail["file"] for event in request.events.filter(kind=Kind.DOWNLOADED).order_by("id")
        ]
        self.assertEqual(sorted(downloaded), sorted(name for name, _type in expected.values()))
        # The proof file is written again as it is downloaded: its own
        # download is in it, and its hash on the row is the one served.
        request.refresh_from_db()
        proof = private_files.read(request.uuid, private_files.PROOF)
        self.assertEqual(hashlib.sha256(proof).hexdigest(), request.proof_sha256)
        with pdfplumber.open(io.BytesIO(proof)) as document:
            text = "\n".join(page.extract_text() for page in document.pages)
        self.assertIn("preuve.pdf", text)

    def test_the_file_names_say_what_they_are(self):
        self.complete()
        response = self.client.get(self.route("staff:signature_file", 1, "signe"))
        self.assertEqual(
            response["Content-Disposition"],
            "attachment; filename=\"Releve d'heures DUPONT Jeanne juin 2026 v1 - signe et contresigne.pdf\"; "
            "filename*=UTF-8''Relev%C3%A9%20d%27heures%20DUPONT%20Jeanne%20juin%202026%20v1%20-%20sign%C3%A9%20et"
            "%20contresign%C3%A9.pdf",
        )
        response = self.client.get(self.route("staff:signature_file", 1, "signe-salarie"))
        self.assertIn(
            "filename=\"Releve d'heures DUPONT Jeanne juin 2026 v1 - signe, avant contreseing.pdf\"",
            response["Content-Disposition"],
        )

    def test_the_employer_s_drawing_changed_on_disk_is_refused_not_served(self):
        request, _token = self.complete()
        path = private_files.request_dir(request.uuid) / private_files.EMPLOYER_SIGNATURE_IMAGE
        path.write_bytes(path.read_bytes() + b"\x00")
        response = self.client.get(self.route("staff:signature_file", 1, "dessin-employeur"), follow=True)
        self.assertLandedOn(response, self.url + "#signature")
        (message,) = self.messages_of(response)
        self.assertIn("ne correspond plus à l'empreinte enregistrée", message)

    def test_a_request_countersigned_before_the_drawing_still_shows_verifies_and_downloads(self):
        """Countersigned the old way (before 28/09, no drawing - one such
        request is on the owner's database): its version draws as it did,
        its five files are offered and served, « Vérifier » says intact, and
        no drawing of the employer's is offered that never existed."""
        request, _token = self.create()
        request = self.employee_signs(request)
        countersign_without_a_drawing(request)
        page = self.page()
        self.assertIn("Signée et contresignée", self.text(page))
        links = {slug: url for url, version, slug in FILE_LINK.findall(self.html(page)) if version == "1"}
        self.assertEqual(sorted(links), ["dessin", "document", "preuve", "signe", "signe-salarie"])
        self.assertNotIn("de l'employeur (PNG)", self.text(page))
        for slug, url in links.items():
            with self.subTest(file=slug):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(self.route("staff:signature_file", 1, "dessin-employeur")).status_code, 404)
        text = " ".join(self.text(self.verify()).split())
        self.assertIn("Document intact : signé par DUPONT Jeanne", text)
        self.assertIn("contresigné par BAR EXEMPLE", text)
        self.assertIn("ceux d'avant la contresignature de l'employeur sont scellés", " ".join(self.text(page).split()))

    def test_a_file_changed_on_disk_is_refused_not_served(self):
        request, _token = self.create()
        path = private_files.request_dir(request.uuid) / private_files.DOCUMENT
        path.write_bytes(path.read_bytes() + b"%")
        response = self.client.get(self.route("staff:signature_file", 1, "document"), follow=True)
        self.assertLandedOn(response, self.url + "#signature")
        (message,) = self.messages_of(response)
        self.assertIn("ne correspond plus à l'empreinte enregistrée", message)

    def test_a_file_gone_from_disk_is_said_without_naming_a_setting(self):
        """The old single mode's STAFF_PRIVATE_DIR exists no more - and a bar
        is never shown the name of a server setting anyway."""
        request, _token = self.create()
        (private_files.request_dir(request.uuid) / private_files.DOCUMENT).unlink()
        response = self.client.get(self.route("staff:signature_file", 1, "document"), follow=True)
        self.assertLandedOn(response, self.url + "#signature")
        (message,) = self.messages_of(response)
        self.assertIn("est introuvable dans le dossier privé de l'espace", message)
        self.assertNotIn("STAFF_PRIVATE_DIR", message)

    def test_what_does_not_exist_is_a_404(self):
        self.create()
        other = employee(last_name="Martin", first_name="Paul")
        save_month(other, JUNE, [])
        for url in (
            self.route("staff:signature_file", 2, "document"),          # no version 2
            self.route("staff:signature_file", 1, "cles"),              # no such file
            self.route("staff:signature_file", 1, "signe"),             # not signed yet
            self.route("staff:signature_file", 1, "document", person=other),   # not his request
            self.route("staff:signature_file", 1, "document", month=JULY),     # not this month's
            self.route("staff:signature_file", 99999999999999999999999, "document"),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)


# -- What the section says --------------------------------------------------------------------------------------


class SectionTests(OwnerCase):
    def test_the_events_are_listed_in_words_with_the_chain_check(self):
        request, _token = self.create()
        requests_.note_link_opened(request, {}, ip="203.0.113.7", user_agent="Mozilla/5.0 Essai")
        text = " ".join(self.text(self.page()).split())
        self.assertIn("Demande créée, document figé", text)
        self.assertIn("Lien ouvert", text)
        self.assertIn("adresse IP 203.0.113.7", text)
        self.assertIn("Journal intègre : 2 événements, chaînés par leurs empreintes.", text)
        self.assertIn(f"Document n° {request.uuid}", text)

    def test_the_keys_line_said_once_and_no_going_online_line_any_more(self):
        text = " ".join(self.text(self.page()).split())
        # « Clé de signature non chiffrée : … », a sentence of its own.
        self.assertEqual(text.lower().count(signing.KEY_WARNING.lower()), 1)
        # The old single mode's « must not go online before the login »:
        # this page is behind the login now, always.
        self.assertNotIn("ne doit pas être mise en ligne", text)
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase d'essai"):
            self.assertNotIn(signing.KEY_WARNING.lower(), " ".join(self.text(self.page()).split()).lower())

    def test_the_retention_and_the_command_that_applies_it_are_said(self):
        """The employee's page promises the deletion after five years;
        nothing runs it by itself, and the owner's page is where he learns
        that it is his to run (review, 28/09)."""
        self.assertNotIn("staff_purge_signatures", self.text(self.page()))
        self.create()
        text = " ".join(self.text(self.page()).split())
        self.assertIn("conservées 5 ans après la fin du mois", text)
        self.assertIn("manage.py staff_purge_signatures (--dry-run d'abord)", text)
        self.assertIn("rien ne le lance automatiquement", text)
        with override_settings(STAFF_SIGNATURE_RETENTION_YEARS=6):
            self.assertIn("conservées 6 ans", self.text(self.page()))

    def section_words(self, response, *, journal=False) -> str:
        """The « Signature » section as it reads - without its journal
        unless asked: the journal's lines are the record's labels (« Lien
        envoyé par e-mail au salarié »), shared with the proof file."""
        html = self.html(response)
        start = html.index('id="signature"')
        section = html[start : html.index("</section>", start)]
        if not journal:
            section = re.sub(r'<details class="staff-disclosure signature-journal">.*?</details>', " ", section, flags=re.S)
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", section)).split())

    @override_settings(**MAIL)
    def test_the_section_speaks_of_her_by_name_never_il(self):
        """Beside « DUPONT Jeanne » the section said « Il demandera son
        code », « Le salarié ne reçoit pas l'e-mail ? », « pour qu'il
        récupère », « Signée par le salarié » (review, 28/09): each sentence
        now uses her name or words that name nobody, at every step."""
        self.person.email = ADDRESS
        self.person.save()
        steps = [("before sending", self.page())]
        request, _token = self.create()
        steps.append(("waiting", self.page()))
        waiting = self.section_words(steps[-1][1])
        self.assertIn("En attente de la signature de DUPONT Jeanne.", waiting)
        self.assertIn("Le code à usage unique se demande par e-mail, sur la page du lien.", waiting)
        self.assertIn("Donnez vous-même un code à DUPONT Jeanne, par un autre canal que le lien.", waiting)
        request = self.employee_signs(request)
        steps.append(("signed by her", self.page()))
        signed = self.section_words(steps[-1][1])
        self.assertIn("Signée, à contresigner", signed)
        self.assertIn("Signé par DUPONT Jeanne (PDF)", signed)
        requests_.countersign_request(request, employer_signature())
        steps.append(("complete", self.page()))
        self.assertIn(
            "pour que DUPONT Jeanne récupère son exemplaire final ; l'ancien cessera de fonctionner.",
            self.section_words(steps[-1][1]),
        )
        for name, page in steps:
            with self.subTest(name):
                words = self.section_words(page)
                self.assertNotIn("salarié", words)
                for masculine in ("Il demandera", "qu'il récupère", "Le salarié ne reçoit", "après lui"):
                    self.assertNotIn(masculine, words)
                journal = self.section_words(page, journal=True)
                self.assertNotIn("salarié identifié", journal)
                self.assertNotIn("identifié par", journal)

    def test_every_action_needs_its_csrf_token(self):
        request, _token = self.create()
        for name, extra in (
            ("staff:signature_send", ()),
            ("staff:signature_link", (1,)),
            ("staff:signature_code", (1,)),
            ("staff:signature_countersign", (1,)),
            ("staff:signature_cancel", (1,)),
            ("staff:signature_verify", (1,)),
            ("staff:month_reopen", ()),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(self.route(name, *extra), {}).status_code, 403)
        request.refresh_from_db()
        self.assertEqual(request.status, Status.PENDING)
        self.assertEqual(self.kinds(request), [Kind.CREATED])

    def test_a_get_on_an_action_goes_back_to_the_month_and_does_nothing(self):
        request, _token = self.create()
        for name, extra in (
            ("staff:signature_send", ()),
            ("staff:signature_link", (1,)),
            ("staff:signature_code", (1,)),
            ("staff:signature_countersign", (1,)),
            ("staff:signature_cancel", (1,)),
            ("staff:signature_verify", (1,)),
            ("staff:month_reopen", ()),
        ):
            with self.subTest(name=name):
                self.assertRedirects(
                    self.client.get(self.route(name, *extra)), self.url + "#signature", fetch_redirect_response=False
                )
        self.assertEqual(self.kinds(request), [Kind.CREATED])


class SignatureQueryCountTests(OwnerCase):
    """The month's page costs the same whatever the number of versions sent
    (CLAUDE.md « N+1s hide in per-object properties »): it read each
    version's events twice and its chain's head once more - 3 queries a
    version (review, 28/09)."""

    def count(self) -> int:
        self.page()
        with CaptureQueriesContext(connection) as queries:
            self.page()
        return len(queries)

    def version(self):
        request, _token = self.create()
        requests_.note_link_opened(request, {}, ip="203.0.113.7", user_agent="Mozilla/5.0 Essai")
        return request

    def test_a_month_with_several_versions(self):
        self.version()
        one = self.count()
        for _ in range(2):
            requests_.reopen_month(self.timesheet)
            self.version()
        self.assertEqual(SignatureRequest.objects.filter(timesheet=self.timesheet).count(), 3)
        self.assertEqual(self.count(), one)
        # And the journal it draws is still checked.
        self.assertEqual(self.text(self.page()).count("Journal intègre"), 3)
