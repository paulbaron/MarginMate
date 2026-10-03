"""The « Récupération automatique » page (/invoices/recuperation-auto/): its
smoke GETs, the owner's forms read off the page and posted as a browser does
(CSRF enforced), the refusals, and a member's read-only view.

Nothing is gathered: a POST here only saves a rule. Data invented.
"""

import re
from datetime import time, timedelta
from html import unescape
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from accounts.access import AREA_KEYS, REFUSED_POST, Access
from accounts.models import Membership
from accounts.tenancy import current_tenant
from invoices import integrations
from invoices.forms import (
    AUTO_END_BEFORE_START,
    AUTO_INVOICES_HOURLY,
    AUTO_NO_DAY,
    AUTO_NO_SOURCE,
    AUTO_SLIPS_HALF_HOURLY,
    AUTO_UNKNOWN_SOURCE,
    NUL_REFUSED,
)
from invoices.models import AutoGather, EmailInvoiceSource, InvoiceType, ScrapeJob
from invoices.views import AUTO_GATHER_CAP, OWNER_ONLY_SETTINGS
from returnables.models import SlipFormat
from returnables.tests.support import SEEDED_FORMAT_NAME
from staff.tests.page_forms import as_post, form_posting_to, page_forms_of
from tests.factories import make_supplier
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

PAGE = "/invoices/recuperation-auto/"


def email_type(name="Cave Exemple - Factures"):
    invoice_type = InvoiceType.objects.create(
        supplier=make_supplier(code="CAVE_X", name="Cave Exemple"),
        name=name,
        source_kind=InvoiceType.SourceKind.EMAIL,
    )
    EmailInvoiceSource.objects.create(invoice_type=invoice_type, sender_pattern="factures@cave.exemple")
    return invoice_type


def portal_type():
    return InvoiceType.objects.create(
        supplier=make_supplier(code="BOX_X", name="Box Exemple", expenses_only=True),
        name="Box Exemple - Factures",
        source_kind=InvoiceType.SourceKind.WEBSITE,
    )


def make_rule(**fields) -> AutoGather:
    values = {
        "name": "Bons exemple",
        "sources": [f"bons-{SlipFormat.objects.get(name=SEEDED_FORMAT_NAME).pk}"],
        "weekdays": "1,4",
        "start_time": time(6, 0),
        "end_time": time(14, 0),
        "every_minutes": 30,
        **fields,
    }
    return AutoGather.objects.create(**values)


class PageCase(TestCase):
    def setUp(self):
        super().setUp()
        # The suite's client, logged in as the espace's owner, CSRF enforced
        # as a browser's is.
        self.client = self.client_class(enforce_csrf_checks=True)
        self.cave = email_type()
        self.box = portal_type()
        self.email_code = f"type-{self.cave.pk}"
        self.portal_code = f"type-{self.box.pk}"
        self.slips_code = f"bons-{SlipFormat.objects.get(name=SEEDED_FORMAT_NAME).pk}"

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
        return form_posting_to(self.html(), reverse("invoices:auto_gather_edit", args=[rule.pk]))

    def send(self, form, change=None, follow=True):
        data = as_post(form.submission())
        for name, value in (change or {}).items():
            data[name] = value if isinstance(value, list) else [value]
            if value is None:
                del data[name]
        return self.client.post(form.action.split("#")[0], data, follow=follow)


class SmokeTests(PageCase):
    def test_an_empty_page(self):
        content = self.html()
        self.assertIn("Récupération automatique", content)
        self.assertIn("Aucune récupération automatique.", content)
        self.assertIn('id="nouvelle-auto"', content)
        self.assertIn("chaque source reprend là où sa dernière récupération réussie", self.text(content).lower())

    def test_a_populated_page(self):
        rule = make_rule(last_result="lancée à 06:00 (récupération n° 3)")
        job = ScrapeJob.objects.create(
            trigger=ScrapeJob.Trigger.AUTOMATIC,
            auto_gather_id=rule.pk,
            status=ScrapeJob.Status.SUCCESS,
            invoices_found=4,
            invoices_created=2,
        )
        ScrapeJob.objects.create(status=ScrapeJob.Status.SUCCESS)  # by hand: not this rule's
        content = self.html()
        text = self.text(content)
        self.assertIn(f'id="auto-{rule.pk}"', content)
        self.assertIn("Prochaines récupérations", text)
        self.assertIn("Dernier passage : lancée à 06:00 (récupération n° 3)", text)
        self.assertIn(f'data-sort="{timezone.localtime(job.started_at):%Y-%m-%d %H:%M}"', content)
        self.assertIn('data-table-label="récupérations automatiques"', content)
        self.assertIn("mar. et ven. · de 06:00 à 14:00, toutes les 30 min", text)

    def test_the_post_only_routes_redirect_on_get_and_write_nothing(self):
        rule = make_rule()
        for name in ("invoices:auto_gather_edit", "invoices:auto_gather_delete"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[rule.pk]))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"{PAGE}#auto-{rule.pk}")
        self.assertTrue(AutoGather.objects.filter(pk=rule.pk).exists())

    def test_an_unknown_rule_is_a_404(self):
        for name in ("invoices:auto_gather_edit", "invoices:auto_gather_delete"):
            form = self.new_form()
            response = self.client.post(reverse(name, args=[999]), as_post(form.submission()))
            self.assertEqual(response.status_code, 404)

    def test_the_new_rule_is_pre_filled_for_the_drivers_slips(self):
        form = self.new_form()
        data = as_post(form.submission())
        self.assertEqual(data["nouveau-name"], ["Bons du livreur"])
        self.assertEqual(data["nouveau-sources"], [self.slips_code])
        self.assertEqual(data["nouveau-weekdays"], ["0", "1", "2", "3", "4", "5"])
        self.assertEqual((data["nouveau-start_time"], data["nouveau-end_time"]), (["06:00"], ["14:00"]))
        self.assertEqual(data["nouveau-every_minutes"], ["30"])
        self.assertEqual(data["nouveau-is_active"], ["on"])
        self.assertIn("Pour les factures : une fois par jour", self.text(self.html()))

    def test_metro_and_a_portal_are_shown_disabled_with_why(self):
        content = self.html()
        self.assertRegex(content, r'value="METRO" disabled')
        self.assertRegex(content, rf'value="{self.portal_code}" disabled')
        self.assertNotRegex(content, rf'value="{self.email_code}"[^>]*disabled')
        text = self.text(content)
        self.assertIn("à la main seulement : son pare-feu bloque les connexions automatiques", text)
        self.assertIn("un portail peut demander un code par SMS", text)

    def test_a_development_server_says_it_never_gathers(self):
        self.assertIn("Serveur de développement", self.html())
        with mock.patch("notifications.webpush.sending_enabled", return_value=True):
            self.assertNotIn("Serveur de développement", self.html())

    def test_reached_from_achats_and_consignes(self):
        for url in (reverse("invoices:invoice_list") + "?ajouter=recuperer", reverse("returnables:home")):
            with self.subTest(url=url):
                self.assertIn(f'href="{PAGE}"', self.html(url))


class CreateTests(PageCase):
    def test_the_new_rule_as_drawn_is_saved_and_starts_from_now(self):
        before = timezone.now()
        response = self.send(self.new_form())
        rule = AutoGather.objects.get()
        self.assertRedirects(response, f"{PAGE}#auto-{rule.pk}", fetch_redirect_response=True)
        self.assertEqual(rule.sources, [self.slips_code])
        self.assertEqual(rule.weekdays, "0,1,2,3,4,5")
        self.assertEqual((rule.start_time, rule.end_time, rule.every_minutes), (time(6), time(14), 30))
        self.assertTrue(rule.is_active)
        self.assertGreaterEqual(rule.last_slot_at, before)
        self.assertEqual(rule.last_result, "")
        self.assertIn("Récupération automatique « Bons du livreur » enregistrée.", self.text(response.content.decode()))

    def test_once_a_day_for_the_invoices(self):
        self.send(
            self.new_form(),
            {
                "nouveau-name": "Factures du matin",
                "nouveau-sources": [self.email_code, self.slips_code],
                "nouveau-start_time": "7h",
                "nouveau-end_time": "07:00",
                "nouveau-every_minutes": "60",
            },
        )
        rule = AutoGather.objects.get()
        self.assertEqual(rule.sources, [self.email_code, self.slips_code])
        self.assertEqual((rule.start_time, rule.end_time), (time(7), time(7)))

    def refused(self, change, sentence):
        response = self.send(self.new_form(), change, follow=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AutoGather.objects.exists())
        self.assertIn(sentence, self.text(response.content.decode()))
        self.assertIn('id="nouvelle-auto"', response.content.decode())
        return response

    def test_an_invoice_source_at_most_hourly(self):
        self.refused({"nouveau-sources": [self.email_code], "nouveau-every_minutes": "30"}, AUTO_INVOICES_HOURLY)

    def test_slips_at_most_every_half_hour(self):
        self.refused({"nouveau-every_minutes": "15"}, AUTO_SLIPS_HALF_HOURLY)

    def test_metro_posted_by_hand_is_refused_with_why(self):
        self.refused({"nouveau-sources": ["METRO", self.slips_code]}, "« Metro » : à la main seulement")

    def test_a_portal_posted_by_hand_is_refused_with_why(self):
        self.refused({"nouveau-sources": [self.portal_code]}, "« Box Exemple - Factures » : à la main seulement")

    def test_an_unknown_source_is_refused(self):
        self.refused({"nouveau-sources": ["type-999"]}, AUTO_UNKNOWN_SOURCE)

    def test_no_source(self):
        self.refused({"nouveau-sources": None}, AUTO_NO_SOURCE)

    def test_no_day(self):
        self.refused({"nouveau-weekdays": None}, AUTO_NO_DAY)

    def test_an_end_before_the_start(self):
        self.refused({"nouveau-start_time": "14:00", "nouveau-end_time": "06:00"}, AUTO_END_BEFORE_START)

    def test_a_time_that_is_none(self):
        self.refused({"nouveau-start_time": "25:00"}, "« 25:00 » n'est pas une heure (00:00 à 23:59).")

    def test_two_times_in_one_field(self):
        self.refused({"nouveau-start_time": "6h 7h"}, "Une seule heure ici")

    def test_a_nul_in_the_name_is_said_in_french(self):
        self.refused({"nouveau-name": "Bons\x00"}, NUL_REFUSED)

    def test_an_unknown_period(self):
        response = self.send(self.new_form(), {"nouveau-every_minutes": "7"}, follow=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AutoGather.objects.exists())

    def test_ten_at_most(self):
        for number in range(AutoGather.MAX_PER_TENANT):
            make_rule(name=f"Règle {number}")
        response = self.send(self.new_form(), follow=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AutoGather.objects.count(), AutoGather.MAX_PER_TENANT)
        self.assertIn(AUTO_GATHER_CAP, self.text(response.content.decode()))


class EditTests(PageCase):
    def setUp(self):
        super().setUp()
        self.past = timezone.now() - timedelta(days=2)
        self.rule = make_rule(last_slot_at=self.past, last_result="manquée : serveur arrêté à 06:00")
        AutoGather.objects.filter(pk=self.rule.pk).update(last_failed_codes=["type-5"])

    def saved(self, change):
        response = self.send(self.edit_form(self.rule), change)
        self.assertRedirects(response, f"{PAGE}#auto-{self.rule.pk}")
        self.rule.refresh_from_db()
        return response

    def test_a_new_name_leaves_the_schedulers_columns_alone(self):
        self.saved({f"auto-{self.rule.pk}-name": "Bons du matin"})
        self.assertEqual(self.rule.name, "Bons du matin")
        self.assertEqual(self.rule.last_slot_at, self.past)
        self.assertEqual(self.rule.last_result, "manquée : serveur arrêté à 06:00")
        self.assertEqual(self.rule.last_failed_codes, ["type-5"])

    def test_new_days_start_from_now(self):
        self.saved({f"auto-{self.rule.pk}-weekdays": ["1", "3"]})
        self.assertEqual(self.rule.weekdays, "1,3")
        self.assertGreater(self.rule.last_slot_at, self.past)

    def test_new_hours_start_from_now(self):
        self.saved({f"auto-{self.rule.pk}-end_time": "12:00"})
        self.assertGreater(self.rule.last_slot_at, self.past)

    def test_switched_off_then_on(self):
        self.saved({f"auto-{self.rule.pk}-is_active": None})
        self.assertFalse(self.rule.is_active)
        self.assertEqual(self.rule.last_slot_at, self.past)
        self.assertIn("inactive", self.html())
        self.saved({f"auto-{self.rule.pk}-is_active": "on"})
        self.assertTrue(self.rule.is_active)
        self.assertGreater(self.rule.last_slot_at, self.past)

    def test_a_refusal_is_drawn_in_its_own_card(self):
        response = self.send(self.edit_form(self.rule), {f"auto-{self.rule.pk}-weekdays": None}, follow=False)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        card = content[content.index(f'id="auto-{self.rule.pk}"') : content.index('id="nouvelle-auto"')]
        self.assertIn(AUTO_NO_DAY, card)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.weekdays, "1,4")

    def test_delete(self):
        ScrapeJob.objects.create(trigger=ScrapeJob.Trigger.AUTOMATIC, auto_gather_id=self.rule.pk)
        form = form_posting_to(self.html(), reverse("invoices:auto_gather_delete", args=[self.rule.pk]))
        response = self.send(form)
        self.assertRedirects(response, PAGE)
        self.assertFalse(AutoGather.objects.exists())
        self.assertEqual(ScrapeJob.objects.count(), 1)
        self.assertIn("Récupération automatique « Bons exemple » supprimée.", self.text(response.content.decode()))


class CsrfTests(PageCase):
    def test_every_post_route_refuses_a_post_without_its_token(self):
        rule = make_rule()
        data = as_post(self.new_form().submission())
        data.pop("csrfmiddlewaretoken")
        for url in (
            PAGE,
            reverse("invoices:auto_gather_edit", args=[rule.pk]),
            reverse("invoices:auto_gather_delete", args=[rule.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(AutoGather.objects.count(), 1)


class MemberTests(PageCase):
    """Behind the gate - which refuses these pages to an employee whatever
    is ticked (GateTests) - the views keep their own check (defence in
    depth): a member reads the rules, his POST is the 403 page."""

    def setUp(self):
        super().setUp()
        self.rule = make_rule()
        self.owner_form = self.new_form()
        self.edit = self.edit_form(self.rule)
        self.delete = form_posting_to(self.html(), reverse("invoices:auto_gather_delete", args=[self.rule.pk]))
        Membership.objects.filter(tenant=current_tenant()).update(role=Membership.Role.MEMBER, pages=sorted(AREA_KEYS))
        # The gate opened, to reach the views' own check.
        self.enterContext(mock.patch.object(Access, "opens", return_value=True))

    def test_a_member_reads_the_rules_without_a_form(self):
        content = self.html()
        self.assertEqual(page_forms_of(content), [])
        self.assertIn(OWNER_ONLY_SETTINGS, self.text(content))
        self.assertIn("Bons exemple", content)
        self.assertIn("Prochaines récupérations", content)

    def test_a_members_post_gets_the_403_page(self):
        for form in (self.owner_form, self.edit, self.delete):
            with self.subTest(action=form.action):
                response = self.client.post(form.action.split("#")[0], as_post(form.submission()))
                self.assertEqual(response.status_code, 403)
        self.assertEqual(list(AutoGather.objects.values_list("name", flat=True)), ["Bons exemple"])


class GateTests(PageCase):
    """« Récupération automatique » is the owner's (accounts/access.py): an
    employee given every area - « Factures » and « Consignes » included -
    is refused the page and its posts by the gate, before any view."""

    def test_an_employee_given_every_area_is_refused_the_page_and_its_posts(self):
        rule = make_rule()
        forms = (
            self.new_form(),
            self.edit_form(rule),
            form_posting_to(self.html(), reverse("invoices:auto_gather_delete", args=[rule.pk])),
        )
        Membership.objects.filter(tenant=current_tenant()).update(role=Membership.Role.MEMBER, pages=sorted(AREA_KEYS))
        self.assertContains(self.client.get(PAGE), "Page non accessible", status_code=403)
        for form in forms:
            with self.subTest(action=form.action):
                response = self.client.post(form.action.split("#")[0], as_post(form.submission()))
                self.assertContains(response, escape(REFUSED_POST), status_code=403)
        self.assertEqual(list(AutoGather.objects.values_list("name", flat=True)), ["Bons exemple"])


class IntegrationsTests(PageCase):
    def test_an_espace_without_the_servers_mailbox_gets_the_sentence_and_no_form(self):
        make_rule()
        form = self.new_form()
        with mock.patch("invoices.views.integrations_allowed", return_value=False):
            content = self.html()
            self.assertEqual(page_forms_of(content), [])
            self.assertIn(integrations.GATHER, unescape(content))
            self.assertNotIn("Bons exemple", content)
            response = self.client.post(PAGE, as_post(form.submission()))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AutoGather.objects.count(), 1)


class AdminTests(TestCase):
    def test_the_admin_shows_a_rule_and_leaves_the_schedulers_columns_alone(self):
        from django.contrib.auth.models import User

        from tests.runner import confirm_password, member_of_the_test_tenant

        rule = make_rule(last_result="lancée à 06:00 (récupération n° 3)")
        confirm_password(
            self.client,
            member_of_the_test_tenant(User.objects.create_superuser("proprio", "proprio@example.invalid", "x")),
        )
        self.assertContains(self.client.get(reverse("admin:invoices_autogather_changelist")), "Bons exemple")
        page = self.client.get(reverse("admin:invoices_autogather_change", args=[rule.pk]))
        self.assertEqual(page.status_code, 200)
        for column in ("last_slot_at", "last_result", "last_failed_codes"):
            with self.subTest(column=column):
                self.assertNotContains(page, f'name="{column}"')
        self.assertContains(page, "lancée à 06:00 (récupération n° 3)")
        self.assertEqual(self.client.get(reverse("admin:invoices_scrapejob_changelist")).status_code, 200)
