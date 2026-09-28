"""The « Personnel » pages, driven the way the owner drives them.

Every POST here is read OFF THE RENDERED PAGE (`page_forms`) and sent
through a client that enforces CSRF, as a browser's does: the grid's
ninety-odd fields, the « Du … au … » selects, the button pressed. A request
written by hand would test the views and let a misnamed field in a
template through (CLAUDE.md « Formsets: test what the browser actually
posts »).

« This month » is pinned to June 2026 (`staff.views.this_month`): June 2026
runs from Monday 1 to Tuesday 30 with no public holiday, and May 2026 holds
four (Friday 1, Friday 8, the Ascension on Thursday 14, Whit Monday 25).

Names and addresses are INVENTED (DUPONT Jeanne, MARTIN Paul, BAR EXEMPLE,
12 rue Imaginaire): the repository is public and a timesheet is personal
data.
"""

import io
import re
from datetime import date
from decimal import Decimal
from html import unescape
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

import pdfplumber
from django.db import connection
from django.template.loader import get_template
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse

from staff.models import ABSENCE_KINDS, Employee, Establishment, Timesheet, TimesheetDay
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from staff.tests.support import employee
from staff.timesheet import apply_range, month_days, save_month

JUNE = date(2026, 6, 1)   # Monday 1 to Tuesday 30, no public holiday
MAY = date(2026, 5, 1)    # holidays on the 1st, 8th, 14th and 25th
JULY = date(2026, 7, 1)
MARCH = date(2026, 3, 1)  # 31 days from a Sunday: six week totals

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "staff"


def month_url(person, month=JUNE):
    return reverse("staff:month", args=[person.pk, month])


def stored(person, month=JUNE):
    """day number → (kind, hours, note) as the database holds them."""
    return {
        row.date.day: (row.kind, row.hours, row.note)
        for row in TimesheetDay.objects.filter(timesheet__employee=person, timesheet__month=month)
    }


class PageTestCase(TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("staff.views.this_month", return_value=JUNE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client(enforce_csrf_checks=True)

    def get(self, url, status=200):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, url)
        if status == 200:
            self.assertRendered(response)
        return response

    def text(self, response) -> str:
        return unescape(response.content.decode())

    def assertRendered(self, response):
        """Django's `{# … #}` is single-line only: a multi-line one prints
        itself onto the page, and still answers 200."""
        content = response.content.decode()
        for marker in ("{#", "#}", "{%", "%}", "{{"):
            self.assertNotIn(marker, content, f"unrendered template syntax {marker!r}")

    def send(self, form, *, press=None, values=None):
        """Submit `form` as the browser would, and follow the redirect."""
        response = self.client.post(
            form.action.split("#")[0], as_post(form.submission(press=press, values=values)), follow=True
        )
        self.assertEqual(response.status_code, 200)
        return response

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def assertSays(self, response, *messages):
        self.assertEqual(self.messages_of(response), list(messages))

    def assertLandedOn(self, response, url):
        self.assertEqual(response.redirect_chain[-1], (url, 302))


# -- /personnel/ ------------------------------------------------------------------------------------------------


class HomePageTests(PageTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("staff:home")

    def add_form(self, response):
        return form_posting_to(response.content.decode(), self.url, holding=("action", "ajouter"))

    def header_form(self, response):
        return form_posting_to(response.content.decode(), self.url, holding=("action", "etablissement"))

    def test_the_empty_page_says_what_to_do_first(self):
        response = self.get(self.url)
        text = self.text(response)
        self.assertIn("Aucun salarié pour l'instant", text)
        self.assertIn("en-tête des fiches", text)
        # Both forms are there, and the header's is open: nothing is saved yet.
        self.add_form(response)
        self.header_form(response)
        self.assertIn('<details class="staff-disclosure" open>', response.content.decode())

    def test_the_employees_active_first_with_their_week_and_this_month(self):
        dupont = employee()
        martin = employee(last_name="Martin", first_name="Paul")
        martin.is_active = False
        martin.save()
        abel = employee(last_name="Abel", first_name="Louise", monday_hours=Decimal("7"))
        save_month(dupont, JUNE, [])
        response = self.get(self.url)
        text = self.text(response)
        self.assertNotIn("Aucun salarié pour l'instant", text)
        # Active first, then by name; the one who left last.
        self.assertLess(text.index("ABEL Louise"), text.index("DUPONT Jeanne"))
        self.assertLess(text.index("DUPONT Jeanne"), text.index("MARTIN Paul"))
        self.assertIn("Ma 7,5 · Me 6 · Je–Sa 7,5 · 36 h / semaine", text)
        self.assertIn("Lu 7 · 7 h / semaine", text)
        rows = [row.employee for row in response.context["rows"]]
        self.assertEqual(rows, [abel, dupont, martin])
        self.assertEqual([row.this_month_saved for row in response.context["rows"]], [False, True, False])
        self.assertIn(f'href="{month_url(dupont)}"', response.content.decode())
        self.assertIn("Fiche juin 2026", text)
        self.assertIn('data-table-label="salariés"', text)

    def test_an_employee_is_added_through_the_page_s_form(self):
        response = self.get(self.url)
        answer = self.send(
            self.add_form(response),
            values={
                "last_name": " Dupont ",
                "first_name": "Jeanne",
                "tuesday_hours": "7",
                "wednesday_hours": "7,5",
                "thursday_hours": "7h30",
                "friday_hours": "8",
                "saturday_hours": "8.25",
            },
        )
        self.assertLandedOn(answer, self.url)
        person = Employee.objects.get()
        self.assertEqual(person.display_name, "DUPONT Jeanne")
        self.assertEqual(
            person.typical_week,
            (Decimal("0"), Decimal("7"), Decimal("7.5"), Decimal("7.5"), Decimal("8"), Decimal("8.25"), Decimal("0")),
        )
        self.assertSays(
            answer, "Salarié ajouté : DUPONT Jeanne, Ma 7 · Me–Je 7,5 · Ve 8 · Sa 8,25 · 38,25 h / semaine."
        )
        self.assertIn("DUPONT Jeanne", self.text(answer))

    def test_an_employee_is_added_with_an_email(self):
        answer = self.send(
            self.add_form(self.get(self.url)),
            values={"last_name": "Dupont", "first_name": "Jeanne", "email": "jeanne.dupont@example.invalid"},
        )
        self.assertLandedOn(answer, self.url)
        self.assertEqual(Employee.objects.get().email, "jeanne.dupont@example.invalid")

    def test_a_refused_employee_is_drawn_back_as_typed_with_french_errors(self):
        response = self.get(self.url)
        form = self.add_form(response)
        answer = self.client.post(
            self.url,
            as_post(
                form.submission(
                    values={"last_name": "  ", "monday_hours": "abc", "tuesday_hours": "25", "wednesday_hours": "1 5"}
                )
            ),
        )
        self.assertEqual(answer.status_code, 200)
        self.assertFalse(Employee.objects.exists())
        text = self.text(answer)
        self.assertIn("Le nom est obligatoire", text)
        self.assertIn("« abc » n'est pas un nombre d'heures", text)
        self.assertIn("« 25 », c'est plus que les 24 h d'une journée.", text)
        # A slip for 1,5 - not 15 h.
        self.assertIn("« 1 5 » : un espace entre deux chiffres", text)
        self.assertIn('value="abc"', answer.content.decode())
        # No total from a week with a figure missing.
        self.assertIn("<output data-week-total>—</output>", answer.content.decode())
        # The header's form was not the one posted: untouched, still there.
        self.header_form(answer)

    def test_a_day_off_is_an_empty_field_that_says_repos(self):
        response = self.get(self.url)
        field = self.add_form(response).control("monday_hours")
        self.assertEqual(field.value, "")
        self.assertEqual(field.attrs["placeholder"], "repos")
        self.assertEqual(field.attrs["inputmode"], "decimal")

    def test_the_header_is_saved_through_its_form(self):
        response = self.get(self.url)
        answer = self.send(
            self.header_form(response),
            values={"name": "BAR EXEMPLE", "address": "12 rue Imaginaire\r\n75000 PARIS\r\n"},
        )
        self.assertLandedOn(answer, self.url)
        establishment = Establishment.current()
        self.assertEqual(establishment.name, "BAR EXEMPLE")
        self.assertEqual(establishment.address_lines, ["12 rue Imaginaire", "75000 PARIS"])
        self.assertEqual(
            self.messages_of(answer), ["En-tête des fiches enregistré : BAR EXEMPLE, 12 rue Imaginaire, 75000 PARIS."]
        )
        # Filled in: shown as it prints, the form folded away - and it posts
        # back what it holds, the address's lines included.
        content = answer.content.decode()
        self.assertIn('<details class="staff-disclosure">', content)
        self.assertIn("<strong>BAR EXEMPLE</strong>", content)
        form = self.header_form(answer)
        self.assertEqual(form.control("address").value, "12 rue Imaginaire\r\n75000 PARIS")

    def test_a_refused_header_is_drawn_back_open_with_its_error(self):
        self.send(self.header_form(self.get(self.url)), values={"name": "BAR EXEMPLE"})
        form = self.header_form(self.get(self.url))
        answer = self.client.post(self.url, as_post(form.submission(values={"name": "x" * 256})))
        self.assertEqual(answer.status_code, 200)
        content = answer.content.decode()
        self.assertIn("Pas plus de 255 caractères.", content)
        self.assertIn('<details class="staff-disclosure" open>', content)
        # The preview is what is saved, not what was typed.
        self.assertIn("<strong>BAR EXEMPLE</strong>", content)
        self.assertEqual(Establishment.current().name, "BAR EXEMPLE")

    def test_an_address_without_a_name_is_said_to_print_nothing(self):
        answer = self.send(self.header_form(self.get(self.url)), values={"address": "12 rue Imaginaire"})
        self.assertEqual(
            self.messages_of(answer),
            ["Adresse enregistrée, mais sans nom d'établissement les fiches s'impriment sans en-tête."],
        )

    def test_an_unknown_action_changes_nothing(self):
        form = self.add_form(self.get(self.url))
        pairs = [(name, "autre" if name == "action" else value) for name, value in form.submission()]
        answer = self.client.post(self.url, as_post(pairs), follow=True)
        self.assertEqual(self.messages_of(answer), ["Action inconnue : rien n'a été modifié."])
        self.assertFalse(Employee.objects.exists())


# -- /personnel/<pk>/ -------------------------------------------------------------------------------------------


class EmployeePageTests(PageTestCase):
    def setUp(self):
        super().setUp()
        self.person = employee()
        self.url = reverse("staff:employee", args=[self.person.pk])

    def edit_form(self, response):
        return form_posting_to(response.content.decode(), self.url)

    def test_the_page_before_any_month_is_saved(self):
        response = self.get(self.url)
        text = self.text(response)
        self.assertIn("DUPONT Jeanne", text)
        self.assertIn("Semaine type : Ma 7,5 · Me 6 · Je–Sa 7,5 · 36 h / semaine", text)
        self.assertIn("ne modifie <strong>jamais</strong> un mois déjà enregistré", text)
        # This month, not saved yet: the typical week's 151,5 h.
        rows = response.context["rows"]
        self.assertEqual([row.sheet.month for row in rows], [JUNE])
        self.assertFalse(rows[0].sheet.saved)
        self.assertIn("Pas encore enregistrée : la semaine type", text)
        self.assertIn("151,5 h", text)
        self.assertIn(f'href="{month_url(self.person)}"', response.content.decode())

    def test_every_saved_month_newest_first_with_its_hours_and_absences(self):
        apply_range(self.person, MAY, date(2026, 5, 5), date(2026, 5, 6), "conges")
        apply_range(self.person, MAY, date(2026, 5, 7), date(2026, 5, 7), "maladie")
        save_month(self.person, JULY, [])
        response = self.get(self.url)
        rows = response.context["rows"]
        # July and May saved, June (this month) listed although it is not.
        self.assertEqual([row.sheet.month for row in rows], [JULY, JUNE, MAY])
        self.assertEqual([row.sheet.saved for row in rows], [True, False, True])
        self.assertEqual(rows[2].absences, "Congés payés 2 jours · Arrêt maladie 1 jour")
        self.assertEqual(rows[0].absences, "")
        text = self.text(response)
        self.assertIn("Congés payés 2 jours · Arrêt maladie 1 jour", text)
        saved_at = Timesheet.objects.get(month=MAY).updated_at
        self.assertIsNotNone(rows[2].saved_at)
        self.assertEqual(rows[2].saved_at, saved_at)
        self.assertIn('data-sort="2026-05"', text)

    def test_the_typical_week_is_changed_through_the_page_and_a_saved_month_keeps_its_hours(self):
        # June saved as the typical week, through its own page.
        june = self.get(month_url(self.person))
        self.send(form_posting_to(june.content.decode(), month_url(self.person)))
        june_before = stored(self.person)

        answer = self.send(self.edit_form(self.get(self.url)), values={"monday_hours": "7", "saturday_hours": ""})
        self.assertLandedOn(answer, self.url)
        self.person.refresh_from_db()
        self.assertEqual(self.person.monday_hours, Decimal("7"))
        self.assertEqual(self.person.saturday_hours, Decimal("0"))
        self.assertEqual(
            self.messages_of(answer),
            [
                "Semaine type enregistrée : Lu 7 · Ma 7,5 · Me 6 · Je–Ve 7,5 · 35,5 h / semaine. Le mois déjà "
                "enregistré garde ses heures et sa semaine type ; les autres suivent la nouvelle semaine type."
            ],
        )
        # The saved month did not move - its days, and the week it is read
        # against: Monday 1 is still a day off as planned, nothing is marked
        # changed, and the page says whose week is whose.
        self.assertEqual(stored(self.person), june_before)
        june_page = self.get(month_url(self.person))
        june = june_page.context["sheet"]
        self.assertEqual(june.days[0].hours, Decimal("0"))   # Monday 1, saved as rest
        self.assertFalse(any(day.differs for day in june.days))
        text = self.text(june_page)
        self.assertIn("Semaine type : 36 h.", text)
        self.assertIn(
            "Cette fiche garde la semaine type avec laquelle elle a été enregistrée, 36 h ; celle de DUPONT Jeanne "
            "est aujourd'hui de 35,5 h.",
            " ".join(text.split()),
        )
        self.assertNotIn("is-changed", june_page.content.decode())
        # July, never saved, is the new week.
        july_page = self.get(month_url(self.person, JULY))
        self.assertFalse(july_page.context["sheet"].saved)
        self.assertEqual(july_page.context["sheet"].days[5].hours, Decimal("7"))  # Monday 6 July
        self.assertNotIn("data-week-changed", july_page.content.decode())

        # « Revenir à la semaine type » is what takes the new week, and says so.
        reset = form_posting_to(june_page.content.decode(), reverse("staff:month_reset", args=[self.person.pk, JUNE]))
        answer = self.send(reset)
        self.assertEqual(
            self.messages_of(answer),
            [
                "Fiche de juin 2026 remise à la semaine type d'aujourd'hui (35,5 h au lieu de 36 h), notes effacées : "
                # Its five Mondays now worked, its four Saturdays now off.
                "9 jours modifiés (lundi 1, samedi 6, lundi 8, samedi 13, lundi 15, samedi 20 et 3 autres)."
            ],
        )
        self.assertEqual(answer.context["sheet"].weekly_hours, Decimal("35.5"))
        self.assertNotIn("data-week-changed", answer.content.decode())

    def test_the_saved_week_is_drawn_as_the_pages_write_hours(self):
        self.person.wednesday_hours = Decimal("6.50")
        self.person.save()
        response = self.get(self.url)
        form = self.edit_form(response)
        self.assertEqual(form.control("monday_hours").value, "")      # a day off: empty, « repos »
        self.assertEqual(form.control("tuesday_hours").value, "7,5")
        self.assertEqual(form.control("wednesday_hours").value, "6,5")
        self.assertIn("<output data-week-total>36,5</output>", response.content.decode())

    def test_the_name_alone(self):
        answer = self.send(self.edit_form(self.get(self.url)), values={"first_name": "Jeanne-Marie"})
        self.assertEqual(self.messages_of(answer), ["Nom enregistré : DUPONT Jeanne-Marie."])

    def test_nothing_changed(self):
        answer = self.send(self.edit_form(self.get(self.url)))
        self.assertEqual(self.messages_of(answer), ["Rien n'a changé."])

    def test_the_email_is_saved_through_the_page_s_form_and_said(self):
        """Where the signing link, the one-time code and the signed copy go:
        a change of it alone is a change, and the page says it - not « Rien
        n'a changé » over an address just saved (signature_requests)."""
        form = self.edit_form(self.get(self.url))
        self.assertEqual(form.control("email").attrs["type"], "email")
        answer = self.send(form, values={"email": " jeanne.dupont@example.invalid "})
        self.assertLandedOn(answer, self.url)
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "jeanne.dupont@example.invalid")
        self.assertEqual(self.messages_of(answer), ["E-mail enregistré : jeanne.dupont@example.invalid."])
        self.assertEqual(self.edit_form(answer).control("email").value, "jeanne.dupont@example.invalid")

        answer = self.send(self.edit_form(answer), values={"email": ""})
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "")
        self.assertEqual(
            self.messages_of(answer),
            ["E-mail effacé : le lien de signature et le code se transmettront sans e-mail."],
        )

    def test_an_address_that_is_no_address_is_refused_in_french(self):
        form = self.edit_form(self.get(self.url))
        answer = self.client.post(self.url, as_post(form.submission(values={"email": "jeanne chez exemple"})))
        self.assertEqual(answer.status_code, 200)
        self.assertIn("« jeanne chez exemple » n'est pas une adresse e-mail.", self.text(answer))
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "")

    def test_a_refused_edit_is_drawn_back_and_the_page_still_shows_the_employee_as_saved(self):
        form = self.edit_form(self.get(self.url))
        answer = self.client.post(
            self.url, as_post(form.submission(values={"last_name": "", "monday_hours": "7h20"}))
        )
        self.assertEqual(answer.status_code, 200)
        text = self.text(answer)
        self.assertIn("Le nom est obligatoire", text)
        self.assertIn("« 7h20 » ne tombe pas juste en centièmes d'heure", text)
        # The header and the months are the employee as saved, not as typed.
        self.assertRegex(answer.content.decode(), r"<h1>\s*DUPONT Jeanne\s")
        self.assertEqual(answer.context["rows"][0].sheet.days[0].typical_hours, Decimal("0"))
        self.person.refresh_from_db()
        self.assertEqual(self.person.last_name, "Dupont")

    def test_deactivated_and_reactivated_never_deleted(self):
        save_month(self.person, MAY, [])
        response = self.get(self.url)
        form = form_posting_to(response.content.decode(), reverse("staff:employee_active", args=[self.person.pk]))
        answer = self.send(form, press=("actif", "0"))
        self.assertLandedOn(answer, self.url)
        self.person.refresh_from_db()
        self.assertFalse(self.person.is_active)
        self.assertEqual(
            self.messages_of(answer),
            [
                "DUPONT Jeanne n'est plus parmi les salariés actifs. Ses fiches de temps sont conservées et restent "
                "consultables ici."
            ],
        )
        self.assertTrue(Timesheet.objects.filter(employee=self.person).exists())
        self.assertIn("Inactif", self.text(answer))
        home = self.get(reverse("staff:home"))
        self.assertIn('class="is-inactive"', home.content.decode())

        # The page now offers the way back.
        form = form_posting_to(answer.content.decode(), reverse("staff:employee_active", args=[self.person.pk]))
        answer = self.send(form, press=("actif", "1"))
        self.person.refresh_from_db()
        self.assertTrue(self.person.is_active)
        self.assertEqual(self.messages_of(answer), ["DUPONT Jeanne est de nouveau parmi les salariés actifs."])

    def test_a_second_click_on_a_stale_page_is_not_a_second_toggle(self):
        form = form_posting_to(
            self.get(self.url).content.decode(), reverse("staff:employee_active", args=[self.person.pk])
        )
        self.send(form, press=("actif", "0"))
        answer = self.send(form, press=("actif", "0"))
        self.person.refresh_from_db()
        self.assertFalse(self.person.is_active)
        self.assertEqual(self.messages_of(answer), ["DUPONT Jeanne : rien n'a changé."])

    def test_deactivating_by_get_or_with_an_unknown_value_changes_nothing(self):
        url = reverse("staff:employee_active", args=[self.person.pk])
        self.assertRedirects(self.client.get(url), self.url)
        form = form_posting_to(self.get(self.url).content.decode(), url)
        token = form.control("csrfmiddlewaretoken").value
        answer = self.client.post(url, {"csrfmiddlewaretoken": token, "actif": "peut-être"}, follow=True)
        self.assertEqual(self.messages_of(answer), ["Action inconnue : rien n'a été modifié."])
        self.assertEqual(self.client.post(url, {"actif": "0"}).status_code, 403)   # no token: refused before the view
        self.person.refresh_from_db()
        self.assertTrue(self.person.is_active)

    def test_another_month_is_opened_from_the_page_s_picker(self):
        form = form_posting_to(
            self.get(self.url).content.decode(), reverse("staff:open_month", args=[self.person.pk]), method="get"
        )
        # The picker opens on this month.
        self.assertEqual(form.submission(), [("mois", "6"), ("annee", "2026")])
        response = self.client.get(form.action, as_post(form.submission(values={"mois": "2", "annee": "2025"})))
        self.assertRedirects(response, month_url(self.person, date(2025, 2, 1)))

    def test_a_month_picked_that_is_no_month_goes_back_saying_so(self):
        url = reverse("staff:open_month", args=[self.person.pk])
        for query in ({"mois": "2", "annee": "1800"}, {"mois": "13", "annee": "2026"}, {"mois": "", "annee": ""},
                      {"mois": "²", "annee": "2026"}, {"mois": "1", "annee": "99999999999999999999999"}):
            with self.subTest(query=query):
                response = self.client.get(url, query, follow=True)
                self.assertLandedOn(response, self.url)
                self.assertEqual(self.messages_of(response), ["Choisissez un mois et une année, de 1900 à 2999."])
        # What an <input type="month"> would send is read too.
        self.assertRedirects(self.client.get(url, {"mois": "2026-02"}), month_url(self.person, date(2026, 2, 1)))


# -- /personnel/<pk>/<yyyy>-<mm>/ -------------------------------------------------------------------------------


class MonthPageTests(PageTestCase):
    def setUp(self):
        super().setUp()
        self.person = employee()
        self.url = month_url(self.person)

    def grid(self, response):
        return form_posting_to(response.content.decode(), self.url)

    def test_an_unsaved_month_is_the_typical_week_and_says_so(self):
        response = self.get(self.url)
        text = self.text(response)
        self.assertIn("<h1>Mois de juin 2026 — DUPONT Jeanne</h1>", text)
        self.assertIn("Pas encore enregistrée : ce sont les heures de la semaine type.", text)
        sheet = response.context["sheet"]
        self.assertFalse(sheet.saved)
        # One row per day, the whole name of the day, the typical week beside it.
        for day in ("Lundi 1", "Mardi 2", "Dimanche 7", "Mardi 30"):
            self.assertIn(f'<span class="day-name">{day}</span>', text)
        self.assertIn("semaine type : 7,5 h", text)
        self.assertIn("semaine type : repos", text)
        # A total after each Sunday, and a partial one closing the month,
        # each beside what the typical week plans for the same days.
        self.assertEqual(text.count("<th scope=\"row\">Total semaine</th>"), 4)
        self.assertEqual(text.count("<th scope=\"row\">Total (semaine incomplète)</th>"), 1)
        self.assertEqual(text.count('<td colspan="2" class="muted">semaine type : 36 h</td>'), 4)
        self.assertRegex(
            text, r'<td class="num" data-live="week">7,5 h</td>\s*<td colspan="2" class="muted">semaine type : 7,5 h</td>'
        )
        self.assertIn("151,5 h", text)
        # « ← mai », « juillet → », the PDF - each asking before it leaves a
        # grid changed and not saved (timesheet.js reads data-leaves-grid).
        self.assertRegex(text, f'href="{month_url(self.person, MAY)}" data-leaves-grid="Des modifications[^"]*">← mai</a>')
        self.assertRegex(
            text, f'href="{month_url(self.person, JULY)}" data-leaves-grid="Des modifications[^"]*">juillet →</a>'
        )
        self.assertIn(f'href="{reverse("staff:month_pdf", args=[self.person.pk, JUNE])}"', text)
        self.assertIn("Télécharger la fiche (PDF)", text)
        # No holiday in June, nothing saved to go back from.
        content = response.content.decode()
        self.assertNotIn(reverse("staff:month_holidays_off", args=[self.person.pk, JUNE]), content)
        self.assertNotIn(reverse("staff:month_reset", args=[self.person.pk, JUNE]), content)
        # The progressive enhancement is loaded - found by staticfiles, or
        # `{% asset %}` would give no version - and knows the absences.
        self.assertIn("js/timesheet.js?v=", content)
        self.assertIn(f'data-absence-kinds="{" ".join(sorted(ABSENCE_KINDS))}"', content)

    def test_the_grid_draws_three_fields_per_day_named_by_date_and_nothing_else(self):
        form = self.grid(self.get(self.url))
        expected = {"csrfmiddlewaretoken"}
        for day in month_days(JUNE):
            expected |= {f"heures-{day.isoformat()}", f"motif-{day.isoformat()}", f"note-{day.isoformat()}"}
        self.assertEqual(sorted(form.names), sorted(expected))
        # What each shows: the typical week's hours, « Repos » on a day off.
        self.assertEqual(form.control("heures-2026-06-02").value, "7,5")
        self.assertEqual(form.control("motif-2026-06-02").value, "travail")
        self.assertEqual(form.control("heures-2026-06-01").value, "")
        self.assertEqual(form.control("motif-2026-06-01").value, "repos")
        # The hours of an absence are NOT disabled by the server: without
        # JavaScript the field is still there to type in when the day goes
        # back to « Travail ».
        self.assertFalse(any(control.disabled for control in form.controls))
        self.assertEqual(len(form.buttons()), 1)

    def test_saving_the_page_as_drawn_stores_the_typical_week(self):
        answer = self.send(self.grid(self.get(self.url)))
        self.assertLandedOn(answer, self.url)
        self.assertSays(answer, "Fiche de juin 2026 enregistrée : c'est la semaine type, sans changement.")
        days = stored(self.person)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[1], ("repos", Decimal("0.00"), ""))
        self.assertEqual(days[2], ("travail", Decimal("7.50"), ""))
        self.assertEqual(days[3], ("travail", Decimal("6.00"), ""))
        self.assertEqual(days[6], ("travail", Decimal("7.50"), ""))
        text = self.text(answer)
        self.assertNotIn("Pas encore enregistrée", text)
        self.assertIn("Enregistrée, modifiée pour la dernière fois le", text)
        # Saved, the month can now go back to the typical week.
        self.assertIn(reverse("staff:month_reset", args=[self.person.pk, JUNE]), answer.content.decode())

    def test_changes_are_saved_and_an_absence_is_zero_hours_whatever_is_posted(self):
        form = self.grid(self.get(self.url))
        answer = self.send(
            form,
            values={
                "heures-2026-06-02": "6h30",
                # « Congés payés » with its hours still in the field, as a
                # page without JavaScript posts it.
                "motif-2026-06-03": "conges",
                "note-2026-06-04": "  arrivé   en retard ",
            },
        )
        days = stored(self.person)
        self.assertEqual(days[2], ("travail", Decimal("6.50"), ""))
        self.assertEqual(days[3], ("conges", Decimal("0.00"), ""))
        self.assertEqual(days[4], ("travail", Decimal("7.50"), "arrivé en retard"))
        self.assertEqual(
            self.messages_of(answer),
            [
                "Fiche de juin 2026 enregistrée : 3 jours modifiés par rapport à la semaine type "
                "(mardi 2, mercredi 3 et jeudi 4)."
            ],
        )
        # Drawn back: the absence has no hours, the summary counts it, the
        # changed days are marked, and the week's total dropped by 7 h (1 h
        # on Tuesday, Wednesday's 6).
        sheet = answer.context["sheet"]
        self.assertEqual(sheet.days[2].hours_input, "")
        self.assertEqual([(count.label, count.days) for count in sheet.summary.absences], [("Congés payés", 1)])
        self.assertEqual(sheet.weeks[0].total, Decimal("29"))
        text = self.text(answer)
        self.assertIn("Congés payés : <strong>1 jour</strong>", text)
        self.assertEqual(text.count('class="day-row is-changed'), 2)   # a note alone is no change

        # A second save says what changed since the first.
        form = self.grid(answer)
        self.assertEqual(form.control("motif-2026-06-03").value, "conges")
        answer = self.send(form, values={"heures-2026-06-05": "9"})
        self.assertEqual(self.messages_of(answer), ["Fiche de juin 2026 enregistrée : 1 jour modifié (vendredi 5)."])
        answer = self.send(self.grid(answer))
        self.assertEqual(self.messages_of(answer), ["Fiche de juin 2026 enregistrée : aucun jour modifié."])

    def test_hours_typed_on_a_day_off_are_a_day_worked_and_the_page_says_so(self):
        answer = self.send(self.grid(self.get(self.url)), values={"heures-2026-06-07": "4"})
        self.assertEqual(stored(self.person)[7], ("travail", Decimal("4.00"), ""))
        self.assertEqual(
            self.messages_of(answer),
            [
                "Fiche de juin 2026 enregistrée : 1 jour modifié par rapport à la semaine type (dimanche 7).",
                "Dimanche 7 : 4 h saisies sur un jour de repos, comptées en Travail.",
            ],
        )

    def test_a_refused_save_writes_nothing_and_draws_back_what_was_typed(self):
        answer = self.client.post(
            self.url,
            as_post(
                self.grid(self.get(self.url)).submission(
                    values={"heures-2026-06-04": "abc", "heures-2026-06-05": "25", "heures-2026-06-02": "6"}
                )
            ),
        )
        self.assertEqual(answer.status_code, 200)
        self.assertRendered(answer)
        self.assertFalse(Timesheet.objects.exists())
        text = self.text(answer)
        self.assertIn("Rien n'a été enregistré.", text)
        self.assertIn("Jeudi 4 : « abc » n'est pas un nombre d'heures", text)
        self.assertIn("Vendredi 5 : « 25 », c'est plus que les 24 h d'une journée.", text)
        form = self.grid(answer)
        # Everything typed comes back as typed, the good figures included.
        self.assertEqual(form.control("heures-2026-06-04").value, "abc")
        self.assertEqual(form.control("heures-2026-06-02").value, "6")
        self.assertEqual(text.count('has-error"'), 2)
        # Corrected on the page drawn back, it saves.
        answer = self.send(form, values={"heures-2026-06-04": "7,5", "heures-2026-06-05": "7h30"})
        self.assertEqual(stored(self.person)[2], ("travail", Decimal("6.00"), ""))
        self.assertSays(
            answer, "Fiche de juin 2026 enregistrée : 1 jour modifié par rapport à la semaine type (mardi 2)."
        )

    def test_a_post_carrying_no_day_saves_nothing(self):
        form = self.grid(self.get(self.url))
        answer = self.client.post(
            self.url, {"csrfmiddlewaretoken": form.control("csrfmiddlewaretoken").value}, follow=True
        )
        self.assertEqual(self.messages_of(answer), ["Aucun jour n'a été envoyé : rien n'a été enregistré."])
        self.assertFalse(Timesheet.objects.exists())

    def test_a_disabled_hours_field_is_not_sent_and_the_absence_stays_at_zero(self):
        """What timesheet.js does to an absence's hours: the field is not
        posted at all."""
        form = self.grid(self.get(self.url))
        form.control("heures-2026-06-09").attrs["disabled"] = ""
        self.send(form, values={"motif-2026-06-09": "maladie"})
        self.assertEqual(stored(self.person)[9], ("maladie", Decimal("0.00"), ""))

    def test_a_saved_month_is_drawn_from_its_own_days(self):
        save_month(self.person, JUNE, [])
        apply_range(self.person, JUNE, date(2026, 6, 17), date(2026, 6, 17), "maladie", note="certificat")
        response = self.get(self.url)
        form = self.grid(response)
        self.assertEqual(form.control("motif-2026-06-17").value, "maladie")
        self.assertEqual(form.control("note-2026-06-17").value, "certificat")
        self.assertEqual(form.control("heures-2026-06-17").value, "")
        text = self.text(response)
        self.assertIn("Arrêt maladie : <strong>1 jour</strong>", text)
        self.assertIn("145,5 h", text)
        # The sick day's 6 h are an absence, not 6 h short.
        flat = " ".join(text.split())
        self.assertIn("dont absences : 6 h", flat)
        self.assertIn("Écart hors absences : 0 h", flat)
        self.assertNotIn("−6 h", flat)

    def test_the_worst_month_six_week_totals(self):
        response = self.get(month_url(self.person, MARCH))
        text = self.text(response)
        self.assertEqual(text.count('<tr class="week-total">'), 6)
        self.assertEqual(len(self.grid_of(response, MARCH).names), 31 * 3 + 1)

    def grid_of(self, response, month):
        return form_posting_to(response.content.decode(), month_url(self.person, month))

    def test_an_inactive_employee_s_month_is_still_editable(self):
        self.person.is_active = False
        self.person.save()
        answer = self.send(self.grid(self.get(self.url)), values={"heures-2026-06-02": "5"})
        self.assertIn("n'est plus parmi les salariés actifs : sa fiche reste modifiable.", self.text(answer))
        self.assertEqual(stored(self.person)[2][1], Decimal("5.00"))

    def test_the_first_and_last_months_an_address_may_name(self):
        first = self.get(month_url(self.person, date(1900, 1, 1)))
        self.assertIsNone(first.context["previous"])
        self.assertNotIn("← décembre", self.text(first))
        self.assertIn("février →", self.text(first))
        last = self.get(month_url(self.person, date(2999, 12, 1)))
        self.assertIsNone(last.context["next"])
        self.assertIn("← novembre", self.text(last))

    def test_the_elision_in_august(self):
        response = self.get(month_url(self.person, date(2026, 8, 1)))
        self.assertIn("<h1>Mois d'août 2026 — DUPONT Jeanne</h1>", self.text(response))
        self.assertIn("Férié : Assomption", self.text(response))
        answer = self.send(self.grid_of(response, date(2026, 8, 1)))
        self.assertSays(answer, "Fiche d'août 2026 enregistrée : c'est la semaine type, sans changement.")


class MonthShortcutTests(PageTestCase):
    """The three small forms beside the grid."""

    def setUp(self):
        super().setUp()
        self.person = employee()
        self.url = month_url(self.person)
        self.range_url = reverse("staff:month_range", args=[self.person.pk, JUNE])

    def range_form(self, month=JUNE):
        response = self.get(month_url(self.person, month))
        return form_posting_to(response.content.decode(), reverse("staff:month_range", args=[self.person.pk, month]))

    # « Du … au … »

    def test_on_leave_from_the_11th_to_the_18th(self):
        form = self.range_form()
        # The days offered are the month's, by name; leave comes first.
        self.assertEqual(form.control("debut").options[1], ("2026-06-01", False))
        self.assertEqual(len(form.control("fin").options), 31)
        self.assertEqual(form.control("motif").value, "conges")
        self.assertIn("required", form.control("debut").attrs)
        # The page says the rule before it is used, not only after.
        page = " ".join(self.text(self.get(self.url)).split())
        self.assertIn("Les jours de repos de la période restent des jours de repos", page)
        self.assertNotIn("jours de repos compris", page)
        answer = self.send(form, values={"debut": "2026-06-11", "fin": "2026-06-18"})
        self.assertLandedOn(answer, self.url)
        days = stored(self.person)
        self.assertEqual([days[day][0] for day in range(11, 19)], ["conges"] * 3 + ["repos"] * 2 + ["conges"] * 3)
        self.assertEqual({days[day][1] for day in range(11, 19)}, {Decimal("0")})
        self.assertEqual(days[10], ("travail", Decimal("6.00"), ""))
        self.assertEqual(days[19], ("travail", Decimal("7.50"), ""))
        # The days off are named: the owner sees why six, not eight.
        self.assertEqual(
            self.messages_of(answer),
            [
                "Congés payés du 11 au 18 juin 2026 : 6 jours modifiés (jeudi 11, vendredi 12, samedi 13, "
                "mardi 16, mercredi 17 et jeudi 18). Jours de repos laissés tels quels : dimanche 14 et lundi 15. "
                "La fiche de juin 2026 est maintenant enregistrée."
            ],
        )
        self.assertIn("Congés payés : <strong>6 jours</strong>", self.text(answer))

    def test_a_range_of_days_off_only_changes_nothing_and_says_why(self):
        answer = self.send(self.range_form(), values={"debut": "2026-06-14", "fin": "2026-06-15"})
        self.assertEqual(
            self.messages_of(answer),
            [
                "Congés payés du 14 au 15 juin 2026 : aucun jour modifié. Jours de repos laissés tels quels : "
                "dimanche 14 et lundi 15."
            ],
        )
        self.assertFalse(Timesheet.objects.exists())

    def test_dates_the_wrong_way_round_are_swapped(self):
        self.send(self.range_form(), values={"debut": "2026-06-11", "fin": "2026-06-10", "motif": "absence"})
        days = stored(self.person)
        self.assertEqual((days[10][0], days[11][0]), ("absence", "absence"))

    def test_worked_hours_on_every_working_day_of_a_range(self):
        answer = self.send(
            self.range_form(),
            values={
                "debut": "2026-06-15", "fin": "2026-06-17", "motif": "travail", "heures": "7h30", "note": "inventaire"
            },
        )
        days = stored(self.person)
        self.assertEqual(days[15], ("repos", Decimal("0.00"), ""))   # a Monday, a day off
        self.assertEqual(days[16], ("travail", Decimal("7.50"), "inventaire"))
        self.assertEqual(days[17], ("travail", Decimal("7.50"), "inventaire"))
        self.assertEqual(
            self.messages_of(answer),
            [
                "Travail, 7,5 h par jour, du 15 au 17 juin 2026 : 2 jours modifiés (mardi 16 et mercredi 17). "
                "Jour de repos laissé tel quel : lundi 15. La fiche de juin 2026 est maintenant enregistrée."
            ],
        )

    def test_back_to_work_without_hours_is_the_typical_week(self):
        apply_range(self.person, JUNE, date(2026, 6, 1), date(2026, 6, 30), "conges")
        answer = self.send(self.range_form(), values={"debut": "2026-06-01", "fin": "2026-06-03", "motif": "travail"})
        days = stored(self.person)
        self.assertEqual(days[1], ("repos", Decimal("0.00"), ""))
        self.assertEqual(days[2], ("travail", Decimal("7.50"), ""))
        # Monday 1 was never on leave - a day off - so two days change, and
        # « Travail » without hours names no day left: it is the typical
        # week, days off included.
        self.assertEqual(
            self.messages_of(answer),
            ["Travail aux heures de la semaine type du 1er au 3 juin 2026 : 2 jours modifiés (mardi 2 et mercredi 3)."],
        )

    def test_hours_given_with_an_absence_are_said_to_be_ignored(self):
        answer = self.send(
            self.range_form(), values={"debut": "2026-06-02", "fin": "2026-06-02", "motif": "maladie", "heures": "4"}
        )
        self.assertEqual(stored(self.person)[2], ("maladie", Decimal("0.00"), ""))
        self.assertEqual(
            self.messages_of(answer)[1],
            "Les 4 h saisies n'ont pas été comptées : un jour de « Arrêt maladie » compte 0 h. Une demi-journée, "
            "c'est « Travail » avec ses heures et une note.",
        )

    def test_what_the_range_refuses_it_says_and_writes_nothing(self):
        form = self.range_form()
        cases = [
            ({"debut": "", "fin": "2026-06-04"}, "« Du … au … » : choisissez le premier et le dernier jour."),
            ({"debut": "2026-06-04", "fin": "2026-06-31"}, "« Du … au … » : choisissez le premier et le dernier jour."),
            ({"debut": "20260604", "fin": "2026-06-04"}, "« Du … au … » : choisissez le premier et le dernier jour."),
            ({"debut": "2026-06-04", "fin": "2026-06-04", "motif": "vacances"}, "« Du … au … » : motif inconnu."),
            ({"debut": "2026-06-04", "fin": "2026-06-04", "motif": "travail", "heures": "-2"},
             "« Du … au … » : les heures ne peuvent pas être négatives."),
            ({"debut": "2026-07-04", "fin": "2026-07-05"}, "Du 04/07/2026 au 05/07/2026 : aucun jour en juin 2026."),
            ({"debut": "2026-06-04", "fin": "2026-06-04", "note": "x" * 300}, "La note dépasse 255 caractères."),
        ]
        pairs = dict(form.submission())
        for values, message in cases:
            with self.subTest(values=values):
                # Posted by hand: a stale or tampered form, since the
                # page's own selects can only send the month's days.
                answer = self.client.post(self.range_url, {**pairs, **values}, follow=True)
                self.assertLandedOn(answer, self.url)
                self.assertEqual(self.messages_of(answer), [f"{message} Rien n'a été modifié."])
                self.assertFalse(Timesheet.objects.exists())

    def test_a_range_running_over_the_month_is_cut_to_it_and_says_so(self):
        pairs = dict(self.range_form().submission())
        answer = self.client.post(self.range_url, {**pairs, "debut": "2026-05-30", "fin": "2026-06-02"}, follow=True)
        days = stored(self.person)
        # Monday 1 is a day off: left as it is.
        self.assertEqual((days[1][0], days[2][0], days[3][0]), ("repos", "conges", "travail"))
        self.assertEqual(
            self.messages_of(answer)[1],
            "La période demandée, du 30 mai au 2 juin 2026, dépasse juin 2026 : seuls ses jours ont été modifiés.",
        )

    # The holidays

    def holidays_form(self, month=MAY):
        response = self.get(month_url(self.person, month))
        return form_posting_to(
            response.content.decode(), reverse("staff:month_holidays_off", args=[self.person.pk, month])
        ), response

    def test_the_holidays_are_listed_and_left_as_the_typical_week_until_asked(self):
        _form, response = self.holidays_form()
        text = self.text(response)
        for label in (
            "1er mai — Fête du Travail", "8 mai — Victoire 1945", "14 mai — Ascension", "25 mai — Lundi de Pentecôte"
        ):
            self.assertIn(label, text)
        self.assertIn("14 mai — Ascension <span class=\"muted\">— Travail, 7,5 h</span>", text)
        self.assertIn("25 mai — Lundi de Pentecôte <span class=\"muted\">— Repos</span>", text)
        self.assertIn("Férié : Ascension", text)
        self.assertIn("Mettre les fériés du mois en Férié chômé", text)
        self.assertIn("celui qui tombe un jour de repos le reste", " ".join(text.split()))

    def test_the_holidays_off_in_one_click(self):
        form, _response = self.holidays_form()
        answer = self.send(form)
        self.assertLandedOn(answer, month_url(self.person, MAY))
        days = stored(self.person, MAY)
        for day in (1, 8, 14):
            self.assertEqual(days[day], ("ferie", Decimal("0.00"), ""))
        # Whit Monday is a Monday, a day off: it stays one, and is said.
        self.assertEqual(days[25], ("repos", Decimal("0.00"), ""))
        self.assertEqual(days[2], ("travail", Decimal("7.50"), ""))
        self.assertEqual(
            self.messages_of(answer),
            [
                "En Férié chômé, 0 h : 1er mai (Fête du Travail), 8 mai (Victoire 1945) et 14 mai (Ascension) — "
                "3 jours modifiés. Jour de repos laissé tel quel : 25 mai (Lundi de Pentecôte). La fiche de mai "
                "2026 est maintenant enregistrée."
            ],
        )
        self.assertIn("Férié chômé : <strong>3 jours</strong>", self.text(answer))
        # Pressed again: nothing to do, and said.
        answer = self.send(form)
        self.assertEqual(
            self.messages_of(answer),
            [
                "Déjà en Férié chômé : 1er mai (Fête du Travail), 8 mai (Victoire 1945) et 14 mai (Ascension). "
                "Jour de repos laissé tel quel : 25 mai (Lundi de Pentecôte). Rien n'a changé."
            ],
        )

    def test_holidays_all_on_days_off_change_nothing_and_say_why(self):
        """April 2026: Easter Monday, a Monday."""
        form, _response = self.holidays_form(date(2026, 4, 1))
        answer = self.send(form)
        self.assertEqual(
            self.messages_of(answer),
            ["Jour de repos laissé tel quel : 6 avril (Lundi de Pâques). Rien n'a été modifié."],
        )
        self.assertFalse(Timesheet.objects.exists())

    def test_a_month_with_no_holiday_writes_nothing(self):
        pairs = dict(self.range_form().submission())
        answer = self.client.post(
            reverse("staff:month_holidays_off", args=[self.person.pk, JUNE]),
            {"csrfmiddlewaretoken": pairs["csrfmiddlewaretoken"]},
            follow=True,
        )
        self.assertEqual(self.messages_of(answer), ["Aucun jour férié en juin 2026 : rien n'a été modifié."])
        self.assertFalse(Timesheet.objects.exists())

    # Back to the typical week

    def test_back_to_the_typical_week_behind_a_confirmation(self):
        grid = form_posting_to(self.get(self.url).content.decode(), self.url)
        self.send(grid, values={"heures-2026-06-02": "5", "motif-2026-06-03": "conges", "note-2026-06-04": "retard"})
        response = self.get(self.url)
        content = response.content.decode()
        reset_url = reverse("staff:month_reset", args=[self.person.pk, JUNE])
        form = form_posting_to(content, reset_url)
        # The button sits inside a closed <details>: a first click opens it,
        # a second one resets - a confirmation that needs no JavaScript.
        details = content[content.index('<details class="staff-disclosure">'):]
        self.assertLess(details.index(reset_url), details.index("</details>"))
        self.assertEqual(form.buttons()[0].attrs["class"], "btn btn-danger")
        answer = self.send(form)
        self.assertLandedOn(answer, self.url)
        days = stored(self.person)
        self.assertEqual(days[2], ("travail", Decimal("7.50"), ""))
        self.assertEqual(days[3], ("travail", Decimal("6.00"), ""))
        self.assertEqual(days[4], ("travail", Decimal("7.50"), ""))
        self.assertEqual(
            self.messages_of(answer),
            [
                "Fiche de juin 2026 remise à la semaine type, notes effacées : 3 jours modifiés "
                "(mardi 2, mercredi 3 et jeudi 4)."
            ],
        )
        answer = self.send(form)
        self.assertSays(answer, "La fiche de juin 2026 était déjà la semaine type : rien n'a changé.")

    def test_back_to_the_typical_week_on_an_unsaved_month_writes_nothing(self):
        pairs = dict(self.range_form().submission())
        answer = self.client.post(
            reverse("staff:month_reset", args=[self.person.pk, JUNE]),
            {"csrfmiddlewaretoken": pairs["csrfmiddlewaretoken"]},
            follow=True,
        )
        self.assertEqual(
            self.messages_of(answer),
            ["La fiche de juin 2026 n'est pas encore enregistrée : elle suit déjà la semaine type."],
        )
        self.assertFalse(Timesheet.objects.exists())

    def test_a_get_on_an_action_goes_back_to_the_month_and_writes_nothing(self):
        for name in ("staff:month_range", "staff:month_holidays_off", "staff:month_reset"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[self.person.pk, MAY]))
                self.assertRedirects(response, month_url(self.person, MAY))
        self.assertFalse(Timesheet.objects.exists())

    def test_every_action_is_refused_without_its_csrf_token(self):
        for name in ("staff:month", "staff:month_range", "staff:month_holidays_off", "staff:month_reset"):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name, args=[self.person.pk, MAY]), {}).status_code, 403)
        self.assertFalse(Timesheet.objects.exists())


class MonthPdfTests(PageTestCase):
    """« Télécharger la fiche (PDF) »: the month's sheet as a file to print
    and have signed, reached from the link the month's page draws."""

    def setUp(self):
        super().setUp()
        self.person = employee()

    def download(self, person=None, month=JUNE):
        """The PDF link read off the month's page, and what it answers."""
        page = self.get(month_url(person or self.person, month)).content.decode()
        links = re.findall(r'<a class="btn" href="([^"]+)"([^>]*)>Télécharger la fiche \(PDF\)</a>', page)
        url = reverse("staff:month_pdf", args=[(person or self.person).pk, month])
        self.assertEqual([href for href, _rest in links], [url])
        # timesheet.js asks before it prints the SAVED month over a grid changed on screen.
        self.assertIn("le PDF imprime la fiche telle qu&#x27;elle est enregistrée", links[0][1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        return response

    def lines(self, response) -> list[str]:
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            self.assertEqual(len(pdf.pages), 1)
            self.assertEqual((round(pdf.pages[0].width), round(pdf.pages[0].height)), (595, 842))  # A4
            return pdf.pages[0].extract_text().splitlines()

    def filenames(self, response) -> tuple[str, str]:
        """(the ASCII fallback, the exact name) of the attachment."""
        header = response["Content-Disposition"]
        header.encode("ascii")  # a header value no encoding can mangle
        match = re.fullmatch(r'attachment; filename="([^"]*)"; filename\*=UTF-8\'\'(\S+)', header)
        self.assertIsNotNone(match, header)
        return match.group(1), unquote(match.group(2), errors="strict")

    def test_the_months_page_links_to_its_sheet_as_a_download(self):
        response = self.download()
        self.assertEqual(
            response["Content-Disposition"],
            "attachment; filename=\"Fiche de temps DUPONT Jeanne juin 2026.pdf\"; "
            "filename*=UTF-8''Fiche%20de%20temps%20DUPONT%20Jeanne%20juin%202026.pdf",
        )
        lines = self.lines(response)
        self.assertIn("Fiche de temps — Mois de juin 2026", lines)
        self.assertIn("Salarié : DUPONT Jeanne", lines)
        self.assertIn("Semaine type : 36 h", lines)
        # The typical week, since nothing is saved: Tuesday 2 at 7,5 h, a
        # Monday off blank, the first week's total, and the signatures.
        self.assertIn("Mardi 2 7,5", lines)
        self.assertIn("Lundi 1", lines)
        self.assertIn("Total semaine 36", lines)
        text = "\n".join(lines)
        self.assertIn("Le salarié", text)
        self.assertIn("L'employeur", text)

    def test_an_accented_month_and_name_keep_their_accents_with_an_ascii_fallback(self):
        person = employee(last_name="Lefèvre", first_name="Élodie")
        fallback, exact = self.filenames(self.download(person, date(2026, 8, 1)))
        self.assertEqual(fallback, "Fiche de temps LEFEVRE Elodie aout 2026.pdf")
        self.assertEqual(exact, "Fiche de temps LEFÈVRE Élodie août 2026.pdf")
        lines = self.lines(self.client.get(reverse("staff:month_pdf", args=[person.pk, date(2026, 8, 1)])))
        self.assertIn("Fiche de temps — Mois d'août 2026", lines)
        self.assertIn("Salarié : LEFÈVRE Élodie", lines)

    def test_a_name_no_file_name_may_hold_makes_a_safe_header(self):
        # A slash would be a folder, a quote would close the header's value,
        # and « 李 » is outside both ASCII and the PDF's cp1252.
        person = employee(last_name='Du/pont "Le Grand"', first_name="Jeanne 李")
        fallback, exact = self.filenames(self.download(person))
        for name in (fallback, exact):
            with self.subTest(name=name):
                self.assertTrue(name.startswith("Fiche de temps DU-PONT -LE GRAND- Jeanne"), name)
                self.assertTrue(name.endswith(" juin 2026.pdf"), name)
                self.assertFalse(set('/\\"') & set(name), name)
        self.assertIn("李", exact)
        self.assertNotIn("李", fallback)

    def test_an_unsaved_month_prints_exactly_what_saving_it_stores_and_the_download_writes_nothing(self):
        before = self.download().content
        # A download is a GET: no month saved, no header row made.
        self.assertFalse(Timesheet.objects.exists())
        self.assertFalse(Establishment.objects.exists())
        page = self.get(month_url(self.person))
        self.send(form_posting_to(page.content.decode(), month_url(self.person)))
        self.assertTrue(Timesheet.objects.filter(employee=self.person, month=JUNE).exists())
        self.assertEqual(self.download().content, before)

    def test_a_saved_month_prints_its_absences_under_the_establishments_header(self):
        Establishment.objects.create(
            pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS"
        )
        apply_range(self.person, JUNE, date(2026, 6, 9), date(2026, 6, 12), "conges")
        lines = self.lines(self.download())
        self.assertEqual(lines[:3], ["BAR EXEMPLE", "12 rue Imaginaire", "75000 PARIS"])
        # Tuesday 9 to Friday 12 on leave: 0 h worked, the label in the note
        # column; the week's total is Saturday's 7,5 h alone.
        for day in ("Mardi 9", "Mercredi 10", "Jeudi 11", "Vendredi 12"):
            self.assertIn(f"{day} Congés payés", lines)
        self.assertIn("Samedi 13 7,5", lines)
        self.assertIn("Total semaine 7,5", lines)

    def test_a_blank_header_prints_none(self):
        Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="", address="12 rue Imaginaire")
        lines = self.lines(self.download())
        self.assertEqual(lines[0], "Fiche de temps — Mois de juin 2026")
        self.assertNotIn("12 rue Imaginaire", "\n".join(lines))

    def test_an_employee_who_left_keeps_their_sheets(self):
        self.person.is_active = False
        self.person.save(update_fields=["is_active"])
        self.assertIn("Salarié : DUPONT Jeanne", self.lines(self.download()))


class QueryCountTests(PageTestCase):
    """Each page reads a fixed number of queries however many employees and
    saved months there are (CLAUDE.md « N+1s hide in per-object
    properties »): a per-row `.timesheets.count` or `sheet.timesheet.employee`
    in a template would not show otherwise. The counts are compared, never
    pinned - the topbar's badges are other apps' queries, and theirs to
    change."""

    def count(self, url):
        # Once first: /personnel/ makes the header's row on its first visit
        # (`Establishment.current()`), three queries that are no list's.
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(queries)

    def saved_year(self, person):
        for number in range(1, 13):
            save_month(person, date(2026, number, 1), [])

    def test_the_list_of_employees(self):
        employee()
        url = reverse("staff:home")
        one = self.count(url)
        for number in range(4):
            self.saved_year(employee(last_name=f"Exemple{number}", first_name="Paul"))
        self.assertEqual(self.count(url), one)

    def test_an_employee_and_their_months(self):
        """This month (June) saved in both, so both lists are saved months only."""
        person = employee()
        save_month(person, JUNE, [])
        url = reverse("staff:employee", args=[person.pk])
        one = self.count(url)
        self.saved_year(person)
        self.saved_year(employee(last_name="Martin", first_name="Paul"))
        self.assertEqual(self.count(url), one)

    def test_a_month_and_its_pdf(self):
        person = employee()
        save_month(person, JUNE, [])
        urls = (month_url(person), reverse("staff:month_pdf", args=[person.pk, JUNE]))
        few = [self.count(url) for url in urls]
        self.saved_year(person)
        self.saved_year(employee(last_name="Martin", first_name="Paul"))
        self.assertEqual([self.count(url) for url in urls], few)


class NotFoundTests(PageTestCase):
    """A bad address is a 404 - a stale bookmark, a hand-typed month - never
    a 500."""

    def setUp(self):
        super().setUp()
        self.person = employee()

    def test_a_month_that_is_no_month(self):
        for month in ("2026-13", "2026-00", "2026-6", "0001-01", "1899-12", "3000-01", "juin", "2026-06-01"):
            with self.subTest(month=month):
                for suffix in ("", "periode/", "feries/", "semaine-type/", "pdf/"):
                    self.get(f"/personnel/{self.person.pk}/{month}/{suffix}", status=404)

    def test_an_unknown_employee(self):
        for pk in ("999", "99999999999999999999999", "0"):
            with self.subTest(pk=pk):
                for suffix in ("", "actif/", "mois/", "2026-06/", "2026-06/periode/", "2026-06/feries/",
                               "2026-06/semaine-type/", "2026-06/pdf/"):
                    self.get(f"/personnel/{pk}/{suffix}", status=404)

    def test_an_unknown_employee_is_a_404_on_post_too(self):
        response = self.get(month_url(self.person))
        token = forms_of(response.content.decode())[0].control("csrfmiddlewaretoken").value
        for suffix in ("", "periode/", "feries/", "semaine-type/"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    self.client.post(f"/personnel/999/2026-06/{suffix}", {"csrfmiddlewaretoken": token}).status_code,
                    404,
                )

    def test_a_month_cannot_be_reversed_into_an_address_it_would_404(self):
        with self.assertRaises(NoReverseMatch):
            reverse("staff:month", args=[self.person.pk, date(1899, 12, 1)])
        self.assertEqual(reverse("staff:month", args=[self.person.pk, "2026-06"]), month_url(self.person))


class TemplateTests(TestCase):
    def test_every_staff_template_loads(self):
        """tests/test_ui.py's syntax check walks a fixed list of apps, staff
        among them since the app was registered; this one also pins which
        templates the app has, so a new one is a decision."""
        names = sorted(path.name for path in TEMPLATES.glob("*.html"))
        self.assertEqual(
            names,
            [
                "_employee_fields.html",
                "_month_table.html",          # a month to read: the employee's page, a month under signature
                "_signature.html",            # the month's « Signature » section
                "_signature_request.html",    # one version sent for signature
                "employee.html",
                "home.html",
                "month.html",
                "public_base.html",           # the employee's pages: NOT base.html, no navigation
                "sign.html",
                "sign_error.html",
                "signature_delete.html",      # « Supprimer… » a version, step 1: what goes, why, two checks
                "signature_delete_confirm.html",   # step 2: « Dernière vérification », one red button
            ],
        )
        for name in names:
            with self.subTest(template=name):
                get_template(f"staff/{name}")
