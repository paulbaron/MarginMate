"""« Supprimer… »: deleting a version of a month sent for signature - signed
or not - from the owner's « Signature » section, in TWO steps, and the ONE
function that deletes (staff/signature_deletion.py), which
`manage.py staff_purge_signatures` uses too.

The owner (28/09): « I would like to be able to delete signed time sheets
(with double verifications as this can be dangerous) ». A signed sheet and
its proof file are the employer's evidence of the hours, so each rule here
is a way the deletion could happen without being meant:

1. Step 1 says what goes and why it is dangerous; it deletes nothing, and
   passes only with the box ticked AND the phrase typed.
2. Step 2 (« Dernière vérification ») deletes, and only with the token step
   1 signed: ten minutes, this version, and nothing changed meanwhile.

Every request is read OFF THE RENDERED PAGE (`page_forms`) through a client
that enforces CSRF. Files are removed once the deletion is committed
(`transaction.on_commit`), which a TestCase runs only inside
`captureOnCommitCallbacks(execute=True)`. Names are INVENTED (DUPONT
Jeanne, BAR EXEMPLE)."""

import datetime as dt
import json
import re
import time
from datetime import date, timedelta
from html import unescape
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.core import signing as django_signing
from django.db import transaction
from django.utils import timezone

from staff import private_files, signature_deletion as deletion, signature_requests as requests_
from staff.models import SignatureEvent, SignatureRequest, Timesheet, TimesheetDay
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from staff.tests.support import employee
from staff.tests.test_signature_pages import OwnerCase
from staff.tests.test_views import JUNE, JULY
from staff.timesheet import month_sheet, save_month

AUGUST = date(2026, 8, 1)
Status = SignatureRequest.Status
Kind = SignatureEvent.Kind

#: What step 1 asks to be typed for June 2026.
PHRASE = "supprimer juin 2026"
#: Every file a finished request keeps, as step 1 names them.
ALL_FILES = (
    private_files.DOCUMENT,
    private_files.SIGNATURE_IMAGE,
    private_files.EMPLOYEE_SIGNED,
    private_files.EMPLOYER_SIGNATURE_IMAGE,
    private_files.FINAL,
    private_files.PROOF,
)


def words_of(fragment: str) -> str:
    """A fragment of HTML as the words it shows."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def blocks(tag: str, html: str, *, css_class: str | None = None) -> list[str]:
    """Every `<tag>…</tag>` of `html` (not nested in one of its kind), those
    carrying `css_class` among their classes when it is given."""
    found = []
    for attrs, inner in re.findall(rf"<{tag}\b([^>]*)>(.*?)</{tag}>", html, re.S):
        classes = re.search(r'class="([^"]*)"', attrs)
        if css_class is None or (classes and css_class in classes.group(1).split()):
            found.append(inner)
    return found


def form_html(html: str, action: str) -> str:
    """The inside of the one `<form>` of the page sending to `action`."""
    found = [
        inner
        for attrs, inner in re.findall(r"<form\b([^>]*)>(.*?)</form>", html, re.S)
        if (target := re.search(r'action="([^"]*)"', attrs)) and unescape(target.group(1)) == action
    ]
    assert len(found) == 1, f"{len(found)} forms sending to {action!r}"
    return found[0]


class DeletionCase(OwnerCase):
    def step1_url(self, version=1, month=JUNE):
        return self.route("staff:signature_delete", version, month=month)

    def step2_url(self, version=1, month=JUNE):
        return self.route("staff:signature_delete_confirm", version, month=month)

    def step1(self, version=1, month=JUNE):
        return self.get(self.step1_url(version, month))

    def step1_form(self, response, version=1, month=JUNE):
        return form_posting_to(self.html(response), self.step1_url(version, month))

    def through_step1(self, version=1, *, phrase=PHRASE, understood=True, hours=None, month=JUNE):
        """Step 1 read off its page and posted as the browser does it -
        the answer, not followed. `hours` ticks (True) or leaves (False) the
        hours option, which must then be on the page."""
        form = self.step1_form(self.step1(version, month), version, month)
        values = {deletion.PHRASE_FIELD: phrase, deletion.UNDERSTOOD_FIELD: understood}
        if hours is not None:
            values[deletion.HOURS_FIELD] = hours
        return self.client.post(form.action, as_post(form.submission(values=values)))

    def step2_page(self, version=1, month=JUNE, **step1):
        answer = self.through_step1(version, month=month, **step1)
        self.assertEqual(answer.status_code, 302, "step 1 refused")
        self.assertEqual(urlsplit(answer["Location"]).path, self.step2_url(version, month))
        return self.get(answer["Location"])

    def step2_form(self, page, version=1, month=JUNE):
        return form_posting_to(self.html(page), self.step2_url(version, month))

    def token_of(self, page, version=1, month=JUNE) -> str:
        return self.step2_form(page, version, month).control(deletion.TOKEN_FIELD).value

    def confirm(self, page, version=1, month=JUNE, *, values=None, post_to=None):
        """Step 2's one button, pressed; files removed once committed."""
        form = self.step2_form(page, version, month)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                post_to or form.action, as_post(form.submission(values=values)), follow=True
            )

    def delete(self, version=1, month=JUNE, **step1):
        return self.confirm(self.step2_page(version, month, **step1), version, month)

    def assertKept(self, request, events=None):
        """The version, its journal and its files, all still there."""
        self.assertTrue(SignatureRequest.objects.filter(pk=request.pk).exists())
        if events is not None:
            self.assertEqual(SignatureEvent.objects.filter(request_id=request.pk).count(), events)
        self.assertTrue(private_files.exists(request.uuid, private_files.DOCUMENT))
        self.assertTrue(private_files.exists(request.uuid, private_files.PROOF))
        self.assertEqual(private_files.read_deletion_records(), [])

    def assertGone(self, request):
        self.assertFalse(SignatureRequest.objects.filter(pk=request.pk).exists())
        self.assertFalse(SignatureEvent.objects.filter(request_id=request.pk).exists())
        self.assertFalse(private_files.request_dir(request.uuid).exists())

    def words(self, response) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", self.html(response))).split())


# -- The panel ----------------------------------------------------------------------------------------------------


class PanelTests(DeletionCase):
    def delete_links(self, response) -> dict[str, str]:
        """{step 1's address: the link's words} for every « Supprimer… »."""
        return {
            unescape(href): " ".join(unescape(text).split())
            for href, text in re.findall(r'<a [^>]*href="([^"]*/supprimer/)"[^>]*>(.*?)</a>', self.html(response))
        }

    def test_every_version_in_every_state_offers_supprimer(self):
        """June: v1 finished then replaced, v2 cancelled, v3 expired, v4
        signed by her; July finished; August waiting - each version, current
        or earlier, has its own discreet « Supprimer… »."""
        first, _token = self.complete()
        requests_.reopen_month(self.timesheet, "heures corrigées")
        second, _token = self.create()
        requests_.cancel_request(second, "envoyé par erreur")
        requests_.create_request(self.person, JUNE, now=timezone.now() - timedelta(days=30))
        fourth, _token = self.create()
        self.employee_signs(fourth)
        for month in (JULY, AUGUST):
            save_month(self.person, month, [])
        july, _token = requests_.create_request(self.person, JULY)
        requests_.countersign_request(self.employee_signs(july), self._employer())
        requests_.create_request(self.person, AUGUST)
        statuses = {
            (request.timesheet.month, request.version): request.status
            for request in SignatureRequest.objects.select_related("timesheet")
        }
        self.assertEqual(
            sorted(statuses.values()),
            sorted([Status.SUPERSEDED, Status.CANCELLED, Status.EXPIRED, Status.EMPLOYEE_SIGNED, Status.COMPLETE,
                    Status.PENDING]),
        )
        for month, versions in ((JUNE, (1, 2, 3, 4)), (JULY, (1,)), (AUGUST, (1,))):
            with self.subTest(month=month):
                links = self.delete_links(self.page(self.route("staff:month", month=month)))
                self.assertEqual(links, {self.step1_url(version, month): "Supprimer…" for version in versions})

    def _employer(self):
        from staff.tests.signing_support import employer_signature

        return employer_signature()


# -- Step 1: what goes, why it is dangerous, and the two checks ---------------------------------------------------


class StepOneTests(DeletionCase):
    def test_step_one_says_what_will_be_destroyed_and_why(self):
        request, _token = self.complete()
        request.refresh_from_db()
        events = request.events.count()
        text = self.words(self.step1())
        for expected in (
            "Supprimer la version 1 de juin 2026",
            "Étape 1 sur 2",
            "Signée et contresignée",
            f"Document n° {request.uuid}",
            "envoyée le",
            "Signé par DUPONT Jeanne le",
            "Contresigné le",
            f"Journal : {events} événements",
            "Dossier de preuve (PDF)",
            # Why it is dangerous: the evidence, the periods, final.
            "la preuve, pour l'employeur, des heures effectuées",
            "1 an pour l'inspection du travail",
            "3 ans pour une réclamation de salaire",
            "5 ans recommandés par cette application",
            "La suppression est définitive",
            "Je comprends que la suppression est définitive",
            f"« {PHRASE} »",
            # The trace kept, said before anything is deleted.
            "deletions.log",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        for name in ALL_FILES:
            with self.subTest(file=name):
                self.assertIn(name, text)
        self.assertKept(request, events)

    def test_a_waiting_version_says_its_link_will_reach_nothing(self):
        self.create()
        text = self.words(self.step1())
        self.assertIn("En attente de signature", text)
        self.assertIn("lien valable jusqu'au", text)
        self.assertIn("son lien de signature ne mènera plus à rien", text)
        self.assertIn("le mois redeviendra modifiable", text)

    def test_a_get_never_deletes(self):
        """The fields in a GET's query, or step 2 opened with a valid token:
        a page, never a deletion."""
        request, _token = self.complete()
        events = request.events.count()
        self.get(f"{self.step1_url()}?{deletion.UNDERSTOOD_FIELD}=1&{deletion.PHRASE_FIELD}=supprimer+juin+2026")
        page = self.step2_page()
        self.get(f"{self.step2_url()}?{deletion.TOKEN_FIELD}={self.token_of(page)}&confirmer=1")
        self.assertKept(request, events)

    def test_step_one_alone_deletes_nothing(self):
        request, _token = self.complete()
        events = request.events.count()
        answer = self.through_step1()
        self.assertEqual(answer.status_code, 302)
        self.assertTrue(parse_qs(urlsplit(answer["Location"]).query)[deletion.TOKEN_FIELD][0])
        self.assertKept(request, events)

    def test_the_box_unticked_is_refused(self):
        request, _token = self.complete()
        answer = self.through_step1(understood=False)
        self.assertEqual(answer.status_code, 200)
        text = self.words(answer)
        self.assertIn("Rien n'a été supprimé.", text)
        self.assertIn("Cochez « Je comprends que la suppression est définitive » pour continuer.", text)
        self.assertKept(request)

    def test_a_wrong_phrase_is_refused(self):
        request, _token = self.complete()
        for typed in ("", "supprimer", "supprimer mai 2026", "supprimer juin 2025", "supprimer juin 2026 !",
                      "supprimerjuin 2026", "effacer juin 2026"):
            with self.subTest(typed=typed):
                answer = self.through_step1(phrase=typed)
                self.assertEqual(answer.status_code, 200)
                self.assertIn(f"Tapez exactement « {PHRASE} » pour continuer.", self.words(answer))
        self.assertKept(request)

    def test_both_missing_both_said(self):
        self.complete()
        text = self.words(self.through_step1(phrase="non", understood=False))
        self.assertIn("Cochez « Je comprends", text)
        self.assertIn("Tapez exactement", text)

    def test_the_phrase_is_compared_trimmed_and_case_folded(self):
        self.complete()
        for typed in ("  SUPPRIMER Juin 2026  ", "Supprimer  juin 2026", "supprimer juin 2026\t"):
            with self.subTest(typed=typed):
                self.assertEqual(self.through_step1(phrase=typed).status_code, 302)

    def test_the_phrase_names_the_month_with_its_accent(self):
        save_month(self.person, AUGUST, [])
        requests_.create_request(self.person, AUGUST)
        self.assertIn("« supprimer août 2026 »", self.words(self.step1(month=AUGUST)))
        self.assertEqual(self.through_step1(month=AUGUST, phrase="SUPPRIMER AOÛT 2026").status_code, 302)

    def test_a_refused_step_one_is_drawn_back_as_posted(self):
        self.complete()
        answer = self.through_step1(phrase="supprimer mai", understood=True, hours=True)
        form = self.step1_form(answer)
        self.assertEqual(form.control(deletion.PHRASE_FIELD).value, "supprimer mai")
        self.assertIn("checked", form.control(deletion.UNDERSTOOD_FIELD).attrs)
        self.assertIn("checked", form.control(deletion.HOURS_FIELD).attrs)

    def test_an_unknown_version_is_a_404(self):
        self.create()
        self.assertEqual(self.client.get(self.step1_url(2)).status_code, 404)
        self.assertEqual(self.client.get(self.step1_url(1, month=JULY)).status_code, 404)


class HoursOptionTests(DeletionCase):
    def test_offered_unticked_on_the_month_s_only_version(self):
        self.complete()
        page = self.step1()
        box = self.step1_form(page).control(deletion.HOURS_FIELD)
        self.assertEqual(box.kind, "checkbox")
        self.assertNotIn("checked", box.attrs)
        self.assertIn("Supprimer aussi les heures enregistrées du mois", self.words(page))

    def test_not_offered_while_another_version_remains(self):
        first, _token = self.create()
        requests_.cancel_request(first)
        second, _token = self.create()
        for version in (1, 2):
            with self.subTest(version=version):
                page = self.step1(version)
                self.assertNotIn(deletion.HOURS_FIELD, self.step1_form(page, version).names)
                self.assertIn("Les heures enregistrées du mois restent", self.words(page))
                # A crafted post asking for them anyway is refused.
                form = self.step1_form(page, version)
                pairs = form.submission(values={deletion.PHRASE_FIELD: PHRASE, deletion.UNDERSTOOD_FIELD: True})
                answer = self.client.post(form.action, as_post([*pairs, (deletion.HOURS_FIELD, "1")]))
                self.assertEqual(answer.status_code, 200)
                self.assertIn("d'autres versions de juin 2026 en dépendent", self.words(answer))
        self.assertKept(first)
        self.assertKept(second)

    def test_offered_once_the_other_versions_are_gone(self):
        first, _token = self.create()
        requests_.cancel_request(first)
        self.create()
        self.delete(1)
        self.assertIn(deletion.HOURS_FIELD, self.step1_form(self.step1(2), 2).names)

    def test_the_hours_go_too_and_the_month_is_the_typical_week_again(self):
        request, _token = self.complete()
        page = self.step2_page(hours=True)
        self.assertIn("les heures enregistrées du mois (30 jours)", self.words(page))
        answer = self.confirm(page)
        self.assertGone(request)
        self.assertFalse(Timesheet.objects.filter(employee=self.person, month=JUNE).exists())
        self.assertFalse(TimesheetDay.objects.filter(timesheet__employee=self.person, timesheet__month=JUNE).exists())
        self.assertFalse(month_sheet(self.person, JUNE).saved)
        self.assertIn("Pas encore enregistrée : ce sont les heures de la semaine type.", self.words(answer))
        (record,) = private_files.read_deletion_records()
        self.assertIs(record["hours_deleted"], True)

    def test_without_the_option_the_hours_stay(self):
        request, _token = self.complete()
        answer = self.delete(hours=False)
        self.assertGone(request)
        self.assertEqual(TimesheetDay.objects.filter(timesheet=self.timesheet).count(), 30)
        self.assertTrue(month_sheet(self.person, JUNE).saved)
        (record,) = private_files.read_deletion_records()
        self.assertIs(record["hours_deleted"], False)
        # The month is editable again: its grid is back.
        form_posting_to(self.html(answer), self.url)

    def test_the_hours_option_is_not_one_of_the_two_checks(self):
        """« Deux vérifications » heads the box and the phrase, nothing else.
        The hours option sat first under that heading, drawn exactly like
        « Je comprends… »: two boxes under « two checks », and an owner
        ticking « both » also erased the month's saved hours - the most
        destructive choice on the page. It comes after the phrase now, in a
        fieldset of its own that says it is an option, and its help says
        those days are the working-time record itself."""
        self.complete()
        form = form_html(self.html(self.step1()), self.step1_url())
        hours_control = f'name="{deletion.HOURS_FIELD}"'
        heading = form.index("Deux vérifications")
        understood = form.index(f'name="{deletion.UNDERSTOOD_FIELD}"')
        phrase = form.index(f'name="{deletion.PHRASE_FIELD}"')
        self.assertLess(heading, understood)
        self.assertNotIn(hours_control, form[heading:understood], "the option sits among the two checks")
        self.assertEqual(form.count(hours_control), 1)
        self.assertGreater(form.index(hours_control), phrase, "the option comes before the phrase")
        (fieldset,) = [block for block in blocks("fieldset", form) if hours_control in block]
        legend = re.search(r"<legend\b[^>]*>(.*?)</legend>", fieldset, re.S)
        self.assertIsNotNone(legend, "the option's fieldset has no legend")
        self.assertTrue(words_of(legend.group(1)).startswith("Option"), words_of(legend.group(1)))
        self.assertIn("D3171-16", words_of(fieldset))
        for check in (deletion.UNDERSTOOD_FIELD, deletion.PHRASE_FIELD):
            with self.subTest(check=check):
                self.assertNotIn(f'name="{check}"', fieldset)

    def test_step_two_says_the_hours_apart_as_a_danger(self):
        """Ticked, the month's hours are not one more item of the list: they
        are said apart, in the page's red, beside the one red button."""
        self.complete()
        html = self.html(self.step2_page(hours=True))
        (facts,) = blocks("ul", html, css_class="deletion-facts")
        self.assertNotIn("heures enregistrées", words_of(facts))
        alerts = [words_of(block) for block in blocks("p", html, css_class="message-error")]
        self.assertTrue(
            any("les heures enregistrées du mois (30 jours)" in alert for alert in alerts), alerts
        )
        self.assertTrue(any("relevé du temps de travail" in alert for alert in alerts), alerts)

    def test_a_version_sent_between_the_steps_takes_the_option_back(self):
        """Step 1 of the only version, the hours ticked; then the month is
        sent again. Step 2 would have deleted a month another version rests
        on: refused, nothing deleted."""
        first, _token = self.create()
        requests_.cancel_request(first)
        page = self.step2_page(hours=True)
        second, _token = self.create()
        answer = self.confirm(page)
        self.assertIn("d'autres versions de juin 2026 en dépendent", " ".join(self.messages_of(answer)))
        self.assertKept(first)
        self.assertKept(second)
        self.assertTrue(Timesheet.objects.filter(pk=self.timesheet.pk).exists())


# -- Step 2: the last check, then the deletion ----------------------------------------------------------------


class StepTwoTests(DeletionCase):
    def test_step_two_is_a_last_check_with_one_red_button(self):
        request, _token = self.complete()
        events = request.events.count()
        page = self.step2_page()
        text = self.words(page)
        for expected in (
            "Dernière vérification",
            "Étape 2 sur 2",
            "rien n'est encore supprimé",
            "la version 1 de juin 2026 de DUPONT Jeanne",
            "Signée et contresignée",
            "ses 6 fichiers",
            f"son journal de {events} événements",
            "Les heures enregistrées du mois restent",
            "deletions.log",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        form = self.step2_form(page)
        (button,) = form.buttons()
        self.assertIn("btn-destroy", button.attrs.get("class", ""))
        self.assertEqual(self.html(page).count("btn-destroy"), 1)
        self.assertEqual([form.action for form in forms_of(self.html(page)) if form.method == "post"],
                         [self.step2_url()])
        self.assertEqual(form.names, ["csrfmiddlewaretoken", deletion.TOKEN_FIELD])
        self.assertKept(request, events)

    def test_both_steps_delete_the_version_its_journal_and_its_files(self):
        request, token = self.complete()
        request.refresh_from_db()
        answer = self.delete()
        self.assertLandedOn(answer, self.url + "#signature")
        (message,) = self.messages_of(answer)
        self.assertIn("Version 1 de juin 2026 supprimée", message)
        self.assertIn("6 fichiers", message)
        self.assertIn("deletions.log", message)
        self.assertGone(request)
        # The month is editable again, and the version is no longer listed.
        form_posting_to(self.html(answer), self.url)
        self.assertNotIn(f"Document n° {request.uuid}", self.words(answer))
        self.assertNotIn(self.step1_url(), self.html(answer))
        # Its public link answers as a link that never existed.
        public = self.client.get(f"/personnel/signer/{token}/")
        self.assertEqual(public.status_code, 404)
        self.assertIn(requests_.UNKNOWN_LINK, unescape(public.content.decode()))

    def test_the_tombstone_line(self):
        request, _token = self.complete()
        request.refresh_from_db()
        events = request.events.count()
        self.delete()
        raw = (self.private_dir / private_files.DELETIONS_LOG).read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(raw.count("\n"), 1)
        record = json.loads(raw)
        self.assertEqual(record, private_files.read_deletion_records()[0])
        expected = {
            "how": "page",
            "employee": "DUPONT Jeanne",
            "month": "2026-06",
            "version": 1,
            "uuid": str(request.uuid),
            "status": Status.COMPLETE,
            "document_sha256": request.document_sha256,
            "final_pdf_sha256": request.final_pdf_sha256,
            "ip": "127.0.0.1",
            "hours_deleted": False,
        }
        self.assertEqual({key: record[key] for key in expected}, expected)
        when = dt.datetime.fromisoformat(record["when"])
        self.assertIsNotNone(when.tzinfo)
        self.assertLess(abs(timezone.now() - when), timedelta(minutes=1))
        self.assertEqual(sorted(record["files"]), sorted(ALL_FILES))
        self.assertEqual(record["events"], events)

    def test_every_status_can_be_deleted(self):
        """Waiting, signed by her, cancelled: each version goes through the
        two steps (finished and replaced are the other tests'). Each was the
        month's only one, so the next sent is a version 1 again - its
        document n° is new."""
        waiting, _token = self.create()
        self.delete()
        self.assertGone(waiting)
        signed, _token = self.create()
        self.assertEqual(signed.version, 1)
        self.assertNotEqual(signed.uuid, waiting.uuid)
        self.employee_signs(signed)
        self.delete()
        self.assertGone(signed)
        cancelled, _token = self.create()
        requests_.cancel_request(cancelled)
        self.delete()
        self.assertGone(cancelled)
        self.assertEqual(
            [record["status"] for record in private_files.read_deletion_records()],
            [Status.PENDING, Status.EMPLOYEE_SIGNED, Status.CANCELLED],
        )

    def test_an_earlier_version_goes_alone(self):
        first, _token = self.complete()
        requests_.reopen_month(self.timesheet)
        second, _token = self.create()
        events = second.events.count()
        self.delete(1)
        self.assertGone(first)
        self.assertKept_(second, events)
        second.refresh_from_db()
        self.assertEqual(second.status, Status.PENDING)
        self.assertTrue(requests_.month_is_locked(self.timesheet))

    def assertKept_(self, request, events):
        self.assertTrue(SignatureRequest.objects.filter(pk=request.pk).exists())
        self.assertEqual(SignatureEvent.objects.filter(request_id=request.pk).count(), events)
        self.assertTrue(private_files.exists(request.uuid, private_files.DOCUMENT))

    def test_pressed_twice_deletes_once(self):
        request, _token = self.complete()
        page = self.step2_page()
        self.confirm(page)
        again = self.confirm(page)
        self.assertLandedOn(again, self.url + "#signature")
        self.assertEqual(
            self.messages_of(again), ["Cette version n'existe pas ou plus : elle a peut-être déjà été supprimée."]
        )
        self.assertEqual(len(private_files.read_deletion_records()), 1)


class StepTwoRefusalTests(DeletionCase):
    """Each refusal: a French sentence, the owner sent back to step 1,
    nothing deleted - never a 500."""

    def refused(self, answer, request, sentence):
        self.assertEqual(answer.status_code, 200)
        self.assertLandedOn(answer, self.step1_url(request.version))
        self.assertIn(sentence, " ".join(self.messages_of(answer)))
        self.assertIn("Rien n'a été supprimé.", " ".join(self.messages_of(answer)))
        self.assertKept(request)

    def test_without_a_token(self):
        request, _token = self.complete()
        page = self.step2_page()
        for value in ("", "   "):
            with self.subTest(value=value):
                self.refused(self.confirm(page, values={deletion.TOKEN_FIELD: value}), request, deletion.TOKEN_INVALID)
        # The field left out altogether.
        csrf = self.step2_form(page).control("csrfmiddlewaretoken").value
        answer = self.client.post(self.step2_url(), {"csrfmiddlewaretoken": csrf}, follow=True)
        self.refused(answer, request, deletion.TOKEN_INVALID)

    def test_a_tampered_or_foreign_token(self):
        request, _token = self.complete()
        page = self.step2_page()
        genuine = self.token_of(page)
        payload = django_signing.loads(genuine, salt=deletion.TOKEN_SALT)
        for name, value in (
            ("garbage", "n'importe quoi"),
            ("a letter changed", genuine[:-1] + ("A" if genuine[-1] != "A" else "B")),
            ("no salt", django_signing.dumps(payload)),
            ("another salt", django_signing.dumps(payload, salt="staff.autre-chose")),
            ("too long", "x" * 5000),
            ("not text", "é\x00☃"),
        ):
            with self.subTest(name):
                self.refused(self.confirm(page, values={deletion.TOKEN_FIELD: value}), request, deletion.TOKEN_INVALID)

    def test_the_token_expires_after_ten_minutes(self):
        request, _token = self.complete()
        page = self.step2_page()
        # Read after the token was signed: its timestamp is no later.
        issued = time.time()
        late = SimpleNamespace(time=lambda: issued + 601)
        with mock.patch("django.core.signing.time", new=late):
            answer = self.confirm(page)
        self.refused(answer, request, deletion.TOKEN_EXPIRED)
        # Nine and a half minutes is still in time.
        in_time = SimpleNamespace(time=lambda: issued + 570)
        with mock.patch("django.core.signing.time", new=in_time):
            self.confirm(page)
        self.assertGone(request)

    def test_the_expired_token_is_refused_on_the_page_too(self):
        request, _token = self.complete()
        page = self.step2_page()
        issued = time.time()
        late = SimpleNamespace(time=lambda: issued + 601)
        with mock.patch("django.core.signing.time", new=late):
            answer = self.client.get(f"{self.step2_url()}?{deletion.TOKEN_FIELD}={self.token_of(page)}", follow=True)
        self.refused(answer, request, deletion.TOKEN_EXPIRED)

    def test_the_token_is_bound_to_its_version(self):
        """Version 1's confirmation posted to version 2's step 2 - the same
        month, the same employee: refused."""
        first, _token = self.create()
        requests_.cancel_request(first)
        second, _token = self.create()
        page = self.step2_page(1)
        answer = self.confirm(page, post_to=self.step2_url(2))
        self.refused(answer, second, deletion.TOKEN_OTHER)
        self.assertKept(first)
        # And another employee's month.
        other = employee(last_name="Martin", first_name="Paul")
        save_month(other, JUNE, [])
        requests_.create_request(other, JUNE)
        other_url = self.route("staff:signature_delete_confirm", 1, person=other)
        answer = self.confirm(page, post_to=other_url)
        self.assertEqual(answer.redirect_chain[-1], (self.route("staff:signature_delete", 1, person=other), 302))
        self.assertIn(deletion.TOKEN_OTHER, " ".join(self.messages_of(answer)))
        self.assertEqual(SignatureRequest.objects.count(), 3)

    def test_a_status_change_between_the_steps(self):
        """Waiting at step 1, signed by her before step 2: what the owner
        read is no longer what would go."""
        request, _token = self.create()
        page = self.step2_page()
        self.employee_signs(request)
        request.refresh_from_db()
        self.refused(self.confirm(page), request, deletion.CHANGED)

    def test_a_status_change_is_refused_on_the_page_too(self):
        """Step 2's page, opened again after the change: sent back to step
        1, which shows the version as it now is."""
        request, _token = self.create()
        page = self.step2_page()
        self.employee_signs(request)
        request.refresh_from_db()
        answer = self.client.get(f"{self.step2_url()}?{deletion.TOKEN_FIELD}={self.token_of(page)}", follow=True)
        self.refused(answer, request, deletion.CHANGED)
        self.assertIn("Signée, à contresigner", self.words(answer))

    def test_a_countersignature_between_the_steps(self):
        request, _token = self.create()
        request = self.employee_signs(request)
        page = self.step2_page()
        requests_.countersign_request(request, self._employer())
        request.refresh_from_db()
        self.refused(self.confirm(page), request, deletion.CHANGED)

    def test_expired_between_the_steps(self):
        request, _token = self.create()
        page = self.step2_page()
        SignatureRequest.objects.filter(pk=request.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        answer = self.confirm(page)
        request.refresh_from_db()
        self.assertEqual(request.status, Status.EXPIRED)
        self.refused(answer, request, deletion.CHANGED)

    def test_a_version_that_does_not_exist(self):
        self.create()
        csrf = self.step2_form(self.step2_page()).control("csrfmiddlewaretoken").value
        for url in (self.step2_url(7), self.route("staff:signature_delete_confirm", 99999999999999999999999)):
            with self.subTest(url=url):
                answer = self.client.post(url, {"csrfmiddlewaretoken": csrf, deletion.TOKEN_FIELD: "x"}, follow=True)
                self.assertLandedOn(answer, self.url + "#signature")
                self.assertEqual(
                    self.messages_of(answer),
                    ["Cette version n'existe pas ou plus : elle a peut-être déjà été supprimée."],
                )
                self.assertEqual(self.client.get(url, follow=True).status_code, 200)
        self.assertEqual(SignatureRequest.objects.count(), 1)

    def test_a_trace_that_cannot_be_written_deletes_nothing(self):
        request, _token = self.complete()
        page = self.step2_page()
        with mock.patch("staff.private_files.append_deletion_record", side_effect=OSError("disque plein (test)")):
            answer = self.confirm(page)
        self.refused(answer, request, deletion.TRACE_FAILED)

    def test_files_that_cannot_be_removed_are_said(self):
        """A file held open (Windows) once the rows are committed: said as
        a warning beside the success, never a 500. On the server the view
        runs outside any transaction, so the deletion's commit - and the
        removal it triggers - comes before the page answers; a TestCase
        holds every commit until the test ends, so here the callback runs
        at once, as it would there."""
        request, _token = self.complete()
        page = self.step2_page()
        with (
            mock.patch("staff.private_files.delete_request_files", side_effect=OSError("fichier ouvert (test)")),
            mock.patch("staff.signature_deletion.transaction.on_commit", new=lambda callback, **kwargs: callback()),
        ):
            answer = self.confirm(page)
        self.assertFalse(SignatureRequest.objects.filter(pk=request.pk).exists())
        messages = self.messages_of(answer)
        self.assertIn("Version 1 de juin 2026 supprimée", messages[0])
        self.assertIn(f"signatures/{request.uuid}", messages[1])
        self.assertNotIn(str(self.private_dir), " ".join(messages))
        self.assertEqual(len(private_files.read_deletion_records()), 1)

    def _employer(self):
        from staff.tests.signing_support import employer_signature

        return employer_signature()


class ProtectionTests(DeletionCase):
    def test_both_steps_need_their_csrf_token(self):
        request, _token = self.complete()
        page = self.step2_page()
        token = self.token_of(page)
        self.assertEqual(
            self.client.post(
                self.step1_url(), {deletion.PHRASE_FIELD: PHRASE, deletion.UNDERSTOOD_FIELD: "1"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post(self.step2_url(), {deletion.TOKEN_FIELD: token}).status_code, 403)
        self.assertKept(request)

    def test_smoke_both_steps_in_every_state(self):
        """Step 1 and step 2 of a version waiting, cancelled, signed by her,
        finished, replaced: each draws (200, every template tag rendered),
        and a GET of either leaves every version where it was."""

        def both(version, hours=None):
            self.step1(version)
            self.step2_page(version, hours=hours)

        request, _token = self.create()
        both(1, hours=False)                  # waiting, the month's only version
        requests_.cancel_request(request)
        both(1, hours=True)                   # cancelled
        signed, _token = self.create()
        signed = self.employee_signs(signed)
        both(2)                               # signed by her
        requests_.countersign_request(signed, self._employer())
        both(2)                               # finished
        requests_.reopen_month(self.timesheet)
        both(2)                               # replaced
        self.assertEqual(SignatureRequest.objects.count(), 2)
        self.assertEqual(private_files.read_deletion_records(), [])

    def _employer(self):
        from staff.tests.signing_support import employer_signature

        return employer_signature()


# -- The one function -------------------------------------------------------------------------------------------


class SharedFunctionTests(DeletionCase):
    def test_rows_events_files_and_the_tombstone(self):
        request, _token = self.complete()
        with self.captureOnCommitCallbacks(execute=True):
            outcome = deletion.delete_signature_request(request, how=deletion.PURGE)
        self.assertGone(request)
        self.assertEqual(sorted(outcome.files), sorted(ALL_FILES))
        self.assertEqual(outcome.files_error, "")
        (record,) = private_files.read_deletion_records()
        self.assertEqual((record["how"], record["ip"]), ("purge", None))

    def test_the_files_go_only_once_committed(self):
        """Rolled back, the rows are back - and so must the files be: they
        are removed on commit, never before."""
        request, _token = self.complete()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    deletion.delete_signature_request(request, how=deletion.PAGE)
                    raise RuntimeError("rollback (test)")
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertTrue(SignatureRequest.objects.filter(pk=request.pk).exists())
        for name in ALL_FILES:
            self.assertTrue(private_files.exists(request.uuid, name), name)

    def test_what_it_refuses(self):
        first, _token = self.create()
        requests_.cancel_request(first)
        second, _token = self.create()
        with self.assertRaisesMessage(deletion.DeletionRefused, "d'autres versions de juin 2026 en dépendent"):
            deletion.delete_signature_request(first, how=deletion.PAGE, with_hours=True)
        with self.assertRaisesMessage(deletion.DeletionRefused, deletion.CHANGED):
            deletion.delete_signature_request(second, how=deletion.PAGE, expected={"status": Status.COMPLETE})
        with self.assertRaises(ValueError):
            deletion.delete_signature_request(second, how="à la main")
        self.assertTrue(SignatureRequest.objects.filter(pk__in=[first.pk, second.pk]).count() == 2)
        self.assertEqual(private_files.read_deletion_records(), [])
        with self.captureOnCommitCallbacks(execute=True):
            deletion.delete_signature_request(second, how=deletion.PAGE)
        with self.assertRaisesMessage(deletion.DeletionRefused, deletion.GONE):
            deletion.delete_signature_request(second, how=deletion.PAGE)

    def test_the_log_is_one_json_line_per_deletion_whatever_the_name(self):
        """An invented name with letters beyond Latin-1 and a line separator:
        still one line, read back whole."""
        self.person.last_name = "Żółć Ünal"
        self.person.save()
        first, _token = self.create()
        requests_.cancel_request(first)
        self.create()
        with self.captureOnCommitCallbacks(execute=True):
            deletion.delete_signature_request(first, how=deletion.PAGE)
        raw = (self.private_dir / private_files.DELETIONS_LOG).read_bytes()
        self.assertEqual(raw.count(b"\n"), 1)
        (record,) = private_files.read_deletion_records()
        self.assertEqual(record["employee"], "ŻÓŁĆ ÜNAL Jeanne")

    def test_the_log_skips_a_line_it_cannot_read(self):
        path = private_files.private_dir() / private_files.DELETIONS_LOG
        path.write_text('{"how": "page"}\nceci n\'est pas du JSON\n[1, 2]\n{"how": "purge"}\n', encoding="utf-8")
        self.assertEqual(private_files.read_deletion_records(), [{"how": "page"}, {"how": "purge"}])


class PageFormsTests(DeletionCase):
    def test_the_panel_link_leads_to_step_one_s_form(self):
        """From the month's page, as the owner clicks: the link, then step
        1's one form - which posts to itself, not to step 2."""
        self.complete()
        page = self.page()
        (href,) = re.findall(r'href="([^"]*/supprimer/)"', self.html(page))
        step1 = self.get(unescape(href))
        posting = [form for form in forms_of(self.html(step1)) if form.method == "post"]
        self.assertEqual([form.action for form in posting], [self.step1_url()])
