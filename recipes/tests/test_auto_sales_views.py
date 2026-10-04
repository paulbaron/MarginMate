"""The « Import automatique des ventes » page (/recipes/import-auto/): its
smoke GETs, the owner's forms read off the page and posted as a browser does
(CSRF enforced), the French refusals, no import on « Enregistrer », a
member's read-only view, and an espace where the till may not be used.

Nothing is imported: a POST here only saves a rule. Data invented.
"""

import re
from datetime import timedelta
from html import unescape
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from accounts.access import AREA_KEYS, REFUSED_POST, Access
from accounts.models import Membership
from accounts.tenancy import current_tenant
from recipes.auto_sales_views import AUTO_SALES_CAP, OWNER_ONLY_SETTINGS
from recipes.forms import AUTO_SALES_NO_DAY, AUTO_SALES_NUL_REFUSED, AUTO_SALES_TOO_MANY_TIMES
from recipes.integration import refusal
from recipes.models import AutoSalesImport, SalesImportJob
from recipes.tests.till_support import LADDITION_ACCOUNT
from staff.tests.page_forms import as_post, form_posting_to, page_forms_of
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

PAGE = "/recipes/import-auto/"


def make_rule(**fields) -> AutoSalesImport:
    values = {"name": "Ventes exemple", "weekdays": "0,1,2,3,4,5,6", "times": "07:00", **fields}
    return AutoSalesImport.objects.create(**values)


class PageCase(TestCase):
    def setUp(self):
        super().setUp()
        # The suite's client, logged in as the espace's owner, CSRF enforced
        # as a browser's is.
        self.client = self.client_class(enforce_csrf_checks=True)

    def html(self, url=PAGE, status=200) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, status)
        assertNoUnrenderedTemplateSyntax(self, response, url)
        return response.content.decode()

    def text(self, content: str) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", content)).split())

    def new_form(self):
        return form_posting_to(self.html(), PAGE)

    def edit_form(self, rule):
        return form_posting_to(self.html(), reverse("recipes:auto_sales_edit", args=[rule.pk]))

    def send(self, form, change=None, follow=True):
        data = as_post(form.submission())
        for name, value in (change or {}).items():
            data[name] = value if isinstance(value, list) else [value]
            if value is None:
                del data[name]
        with mock.patch("recipes.importing.threading.Thread") as thread:
            response = self.client.post(form.action.split("#")[0], data, follow=follow)
        thread.assert_not_called()
        return response


class SmokeTests(PageCase):
    def test_the_url(self):
        self.assertEqual(reverse("recipes:auto_sales"), PAGE)

    def test_an_empty_page(self):
        content = self.html()
        text = self.text(content)
        self.assertIn("Import automatique des ventes", text)
        self.assertIn("Aucun import automatique.", text)
        self.assertIn('id="nouvel-import"', content)
        self.assertIn(
            "Période : de la dernière journée importée moins 3 jours jusqu'à la dernière nuit terminée "
            "(La nuit se termine à 06:00 : réglage dans Notifications › Rappels",
            text,
        )
        self.assertIn(f'href="{reverse("notifications:reminders")}"', content)
        self.assertIn("L'Addition (caisse) : aucun import réussi pour l'instant.", text)

    def test_a_populated_page(self):
        rule = make_rule(last_result="lancé à 07:00 (import n° 3, du 12/11 au 17/11)", times="07:00 12:00")
        job = SalesImportJob.objects.create(
            trigger=SalesImportJob.Trigger.AUTOMATIC,
            auto_rule_id=rule.pk,
            status=SalesImportJob.Status.SUCCESS,
            range_start=timezone.localdate() - timedelta(days=6),
            range_end=timezone.localdate() - timedelta(days=1),
            recorded=42,
        )
        SalesImportJob.objects.create(status=SalesImportJob.Status.SUCCESS)  # by hand: not this rule's
        content = self.html()
        text = self.text(content)
        self.assertIn(f'id="import-{rule.pk}"', content)
        self.assertIn("L'Addition (caisse) · tous les jours · 07:00 et 12:00", text)
        self.assertIn("Prochains imports", text)
        upcoming = self.client.get(PAGE).context["cards"][0]["upcoming"]
        self.assertEqual(len(upcoming), 5)
        self.assertTrue(all(line.endswith(("à 07:00", "à 12:00")) for line in upcoming), upcoming)
        self.assertIn("Dernier passage : lancé à 07:00 (import n° 3, du 12/11 au 17/11)", text)
        self.assertIn('data-table-label="imports automatiques"', content)
        self.assertIn(f"du {job.range_start:%d/%m/%Y} au {job.range_end:%d/%m/%Y}", text)
        self.assertIn(">42<", content)
        self.assertIn("Un import manqué est rattrapé dans les 12 h.", text)

    def test_a_stored_bad_row_is_said_on_its_card_never_a_500(self):
        for fields, said in (
            ({"weekdays": "9"}, "jours illisibles : corrigez-les"),
            ({"times": "25:00"}, "heures illisibles : corrigez-les"),
            ({"source": "caisse-disparue"}, "source inconnue : choisissez-en une"),
        ):
            with self.subTest(fields=fields):
                rule = make_rule(**fields)
                self.assertIn(said, self.text(self.html()))
                rule.delete()

    def test_the_post_only_routes_redirect_on_get_and_write_nothing(self):
        rule = make_rule()
        before = list(AutoSalesImport.objects.values_list("pk", "name", "last_slot_at", "updated_at"))
        for name in ("recipes:auto_sales_edit", "recipes:auto_sales_delete"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[rule.pk]))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"{PAGE}#import-{rule.pk}")
        self.assertEqual(list(AutoSalesImport.objects.values_list("pk", "name", "last_slot_at", "updated_at")), before)

    def test_an_unknown_rule_is_a_404(self):
        for name in ("recipes:auto_sales_edit", "recipes:auto_sales_delete"):
            form = self.new_form()
            response = self.client.post(reverse(name, args=[999]), as_post(form.submission()))
            self.assertEqual(response.status_code, 404)

    def test_the_new_rule_is_pre_filled_for_yesterday_s_sales(self):
        data = as_post(self.new_form().submission())
        self.assertEqual(data["nouveau-name"], ["Ventes de la veille"])
        self.assertEqual(data["nouveau-source"], ["laddition"])
        self.assertEqual(data["nouveau-weekdays"], [str(day) for day in range(7)])
        self.assertEqual(data["nouveau-times"], ["07:00"])
        self.assertEqual(data["nouveau-is_active"], ["on"])
        self.assertIn("ex. 07:00, ou 07:00 12:00", self.text(self.html()))

    def test_a_development_server_says_it_never_imports(self):
        self.assertIn("Serveur de développement", self.html())
        with mock.patch("notifications.webpush.sending_enabled", return_value=True):
            self.assertNotIn("Serveur de développement", self.html())

    # The link sits in L'Addition's card on the Ventes tab, drawn only where
    # its account is ready (recipes/pos/connectors.py).
    @LADDITION_ACCOUNT
    def test_reached_from_the_sales_tab_the_gathers_and_notifications(self):
        for url in (reverse("recipes:sales_list"), reverse("invoices:auto_gathers"), reverse("notifications:home")):
            with self.subTest(url=url):
                self.assertIn(f'href="{PAGE}"', self.html(url))
        self.assertIn(
            "Les ventes de la caisse ont leur propre import automatique",
            self.text(self.html(reverse("invoices:auto_gathers"))),
        )


class CreateTests(PageCase):
    def test_the_new_rule_as_drawn_is_saved_and_never_runs_at_once(self):
        before = timezone.now()
        response = self.send(self.new_form())
        rule = AutoSalesImport.objects.get()
        self.assertRedirects(response, f"{PAGE}#import-{rule.pk}")
        self.assertEqual((rule.name, rule.source), ("Ventes de la veille", "laddition"))
        self.assertEqual((rule.weekdays, rule.times), ("0,1,2,3,4,5,6", "07:00"))
        self.assertTrue(rule.is_active)
        self.assertGreaterEqual(rule.last_slot_at, before)
        self.assertEqual(rule.last_result, "")
        self.assertFalse(SalesImportJob.objects.exists())
        self.assertIn("Import automatique « Ventes de la veille » enregistré.", self.text(response.content.decode()))

    def test_times_are_normalised(self):
        self.send(self.new_form(), {"nouveau-times": "12h et 7h"})
        self.assertEqual(AutoSalesImport.objects.get().times, "07:00 12:00")

    def refused(self, change, sentence):
        response = self.send(self.new_form(), change, follow=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AutoSalesImport.objects.exists())
        self.assertIn(sentence, self.text(response.content.decode()))
        self.assertIn('id="nouvel-import"', response.content.decode())
        return response

    def test_no_day(self):
        self.refused({"nouveau-weekdays": None}, AUTO_SALES_NO_DAY)

    def test_no_time(self):
        self.refused({"nouveau-times": ""}, "Indiquez au moins une heure.")

    def test_a_time_that_is_none(self):
        self.refused({"nouveau-times": "25:00"}, "« 25:00 » n'est pas une heure (00:00 à 23:59).")

    def test_six_times_at_most(self):
        self.refused({"nouveau-times": "1h 2h 3h 4h 5h 6h 7h"}, AUTO_SALES_TOO_MANY_TIMES)
        self.refused({"nouveau-times": " ".join(f"{hour}h" for hour in range(13))}, AUTO_SALES_TOO_MANY_TIMES)

    def test_an_unknown_source(self):
        self.refused({"nouveau-source": "caisse-disparue"}, "Choix inconnu : rechargez la page.")

    def test_an_unknown_day(self):
        self.refused({"nouveau-weekdays": ["7"]}, "Choix inconnu : rechargez la page.")

    def test_no_name(self):
        self.refused({"nouveau-name": "   "}, "Donnez un nom à cet import.")

    def test_a_nul_is_said_in_french(self):
        for field in ("nouveau-name", "nouveau-times"):
            with self.subTest(field=field):
                self.refused({field: "Ventes\x00"}, AUTO_SALES_NUL_REFUSED)

    def test_five_at_most(self):
        for number in range(AutoSalesImport.MAX_PER_TENANT):
            make_rule(name=f"Règle {number}")
        response = self.send(self.new_form(), follow=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AutoSalesImport.objects.count(), AutoSalesImport.MAX_PER_TENANT)
        self.assertIn(AUTO_SALES_CAP, self.text(response.content.decode()))


class EditTests(PageCase):
    def setUp(self):
        super().setUp()
        self.past = timezone.now() - timedelta(days=2)
        self.rule = make_rule(last_slot_at=self.past, last_result="manquée : serveur arrêté à 07:00")
        AutoSalesImport.objects.filter(pk=self.rule.pk).update(last_failed=timezone.localdate())

    def saved(self, change):
        response = self.send(self.edit_form(self.rule), change)
        self.assertRedirects(response, f"{PAGE}#import-{self.rule.pk}")
        self.rule.refresh_from_db()
        return response

    def test_a_new_name_leaves_the_scheduler_s_columns_alone(self):
        self.saved({f"import-{self.rule.pk}-name": "Ventes du matin"})
        self.assertEqual(self.rule.name, "Ventes du matin")
        self.assertEqual(self.rule.last_slot_at, self.past)
        self.assertEqual(self.rule.last_result, "manquée : serveur arrêté à 07:00")
        self.assertEqual(self.rule.last_failed, timezone.localdate())

    def test_new_days_start_from_now(self):
        self.saved({f"import-{self.rule.pk}-weekdays": ["1", "3"]})
        self.assertEqual(self.rule.weekdays, "1,3")
        self.assertGreater(self.rule.last_slot_at, self.past)

    def test_new_times_start_from_now(self):
        self.saved({f"import-{self.rule.pk}-times": "08:00"})
        self.assertEqual(self.rule.times, "08:00")
        self.assertGreater(self.rule.last_slot_at, self.past)

    def test_the_same_times_written_otherwise_change_nothing(self):
        self.saved({f"import-{self.rule.pk}-times": "7h"})
        self.assertEqual(self.rule.last_slot_at, self.past)

    def test_switched_off_then_on(self):
        self.saved({f"import-{self.rule.pk}-is_active": None})
        self.assertFalse(self.rule.is_active)
        self.assertEqual(self.rule.last_slot_at, self.past)
        self.assertIn("inactif", self.html())
        self.saved({f"import-{self.rule.pk}-is_active": "on"})
        self.assertTrue(self.rule.is_active)
        self.assertGreater(self.rule.last_slot_at, self.past)

    def test_a_refusal_is_drawn_in_its_own_card(self):
        response = self.send(self.edit_form(self.rule), {f"import-{self.rule.pk}-weekdays": None}, follow=False)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        card = content[content.index(f'id="import-{self.rule.pk}"') : content.index('id="nouvel-import"')]
        self.assertIn(AUTO_SALES_NO_DAY, card)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.weekdays, "0,1,2,3,4,5,6")

    def test_delete_keeps_its_imports(self):
        SalesImportJob.objects.create(trigger=SalesImportJob.Trigger.AUTOMATIC, auto_rule_id=self.rule.pk)
        form = form_posting_to(self.html(), reverse("recipes:auto_sales_delete", args=[self.rule.pk]))
        response = self.send(form)
        self.assertRedirects(response, PAGE)
        self.assertFalse(AutoSalesImport.objects.exists())
        self.assertEqual(SalesImportJob.objects.count(), 1)
        self.assertIn("Import automatique « Ventes exemple » supprimé.", self.text(response.content.decode()))


class CsrfTests(PageCase):
    def test_every_post_route_refuses_a_post_without_its_token(self):
        rule = make_rule()
        data = as_post(self.new_form().submission())
        data.pop("csrfmiddlewaretoken")
        for url in (
            PAGE,
            reverse("recipes:auto_sales_edit", args=[rule.pk]),
            reverse("recipes:auto_sales_delete", args=[rule.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(AutoSalesImport.objects.count(), 1)


class MemberTests(PageCase):
    """Behind the gate - which refuses these pages to an employee whatever
    is ticked (GateTests) - the views keep their own check (defence in
    depth): a member reads the rules, his POST is the 403 page."""

    def setUp(self):
        super().setUp()
        self.rule = make_rule()
        self.owner_form = self.new_form()
        self.edit = self.edit_form(self.rule)
        self.delete = form_posting_to(self.html(), reverse("recipes:auto_sales_delete", args=[self.rule.pk]))
        Membership.objects.filter(tenant=current_tenant()).update(role=Membership.Role.MEMBER, pages=sorted(AREA_KEYS))
        # The gate opened, to reach the views' own check.
        self.enterContext(mock.patch.object(Access, "opens", return_value=True))

    def test_a_member_reads_the_rules_without_a_form(self):
        content = self.html()
        self.assertEqual(page_forms_of(content), [])
        self.assertIn(OWNER_ONLY_SETTINGS, self.text(content))
        self.assertIn("Ventes exemple", content)
        self.assertIn("Prochains imports", content)

    def test_a_member_s_post_gets_the_403_page(self):
        for form in (self.owner_form, self.edit, self.delete):
            with self.subTest(action=form.action):
                response = self.client.post(form.action.split("#")[0], as_post(form.submission()))
                self.assertEqual(response.status_code, 403)
        self.assertEqual(list(AutoSalesImport.objects.values_list("name", flat=True)), ["Ventes exemple"])


class GateTests(PageCase):
    """« Import automatique des ventes » is the owner's (accounts/access.py):
    an employee given every area - « Recettes & ventes » included - is
    refused the page and its posts by the gate, before any view."""

    def test_an_employee_given_every_area_is_refused_the_page_and_its_posts(self):
        rule = make_rule()
        forms = (
            self.new_form(),
            self.edit_form(rule),
            form_posting_to(self.html(), reverse("recipes:auto_sales_delete", args=[rule.pk])),
        )
        Membership.objects.filter(tenant=current_tenant()).update(role=Membership.Role.MEMBER, pages=sorted(AREA_KEYS))
        self.assertContains(self.client.get(PAGE), "Page non accessible", status_code=403)
        for form in forms:
            with self.subTest(action=form.action):
                response = self.client.post(form.action.split("#")[0], as_post(form.submission()))
                self.assertContains(response, escape(REFUSED_POST), status_code=403)
        self.assertEqual(list(AutoSalesImport.objects.values_list("name", flat=True)), ["Ventes exemple"])


class UnavailableTests(PageCase):
    def test_where_the_till_may_not_be_used_the_reason_and_no_form(self):
        make_rule()
        form = self.new_form()
        with mock.patch("recipes.integration.integrations_allowed", return_value=False):
            content = self.html()
            self.assertEqual(page_forms_of(content), [])
            self.assertIn(refusal(), unescape(content))
            self.assertNotIn("Ventes exemple", content)
            response = self.client.post(PAGE, as_post(form.submission()))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AutoSalesImport.objects.count(), 1)
