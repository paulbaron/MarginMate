"""The notifications' pages (notifications/views.py): « Notifications »,
« Rappels », « Alertes ». Forms are read off the rendered page and posted as
a browser does (staff/tests/page_forms.py, CSRF enforced); the owner edits,
a member reads. Names, addresses and suppliers are invented."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from html import unescape
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership
from notifications import registry, schedule
from notifications.forms import NEW_REMINDER, NUL_REFUSED, PAGE_REFUSED
from notifications.models import MAX_REMINDERS, TOO_MANY_REMINDERS, Dispatch, EventRule, NotificationSettings, Reminder
from notifications.tests.support import SITE, QuietLogs, make_device, make_login
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from tests.factories import make_supplier
from tests.runner import TEST_TENANT_PK, test_user
from tests.test_ui import PhoneCardsLabelTests

HOME = reverse("notifications:home")
REMINDERS = reverse("notifications:reminders")
EVENTS = reverse("notifications:events")
NIGHT = reverse("notifications:night")
CONSIGNES = registry.RETURNABLES_COMPARISON
GATHER = registry.INVOICES_AUTO_GATHER


def tick(form, name, values):
    """Tick exactly the boxes of `name` whose value is in `values` (the
    form's repeated checkboxes: page_forms ticks one name at a time)."""
    wanted = {str(value) for value in values}
    found = False
    for control in form.controls:
        if control.name == name:
            found = True
            if control.value in wanted:
                control.attrs["checked"] = ""
            else:
                control.attrs.pop("checked", None)
    assert found, f"no box named {name!r}"


def make_reminder(**fields) -> Reminder:
    values = {
        "name": "Vides avant livraison",
        "title": "Consignes",
        "body": "Comptez les vides.",
        "target": "/consignes/#new-pickup",
        "weekdays": "0,2,5",
        "times": "00:00 02:00",
    }
    values.update(fields)
    return Reminder.objects.create(**values)


class PageCase(QuietLogs, TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_class(enforce_csrf_checks=True)

    def get(self, url, status=200):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, url)
        if status == 200:
            content = response.content.decode()
            for marker in ("{#", "#}", "{%", "%}", "{{"):
                self.assertNotIn(marker, content)
        return response

    def html(self, url) -> str:
        return self.get(url).content.decode()

    def text(self, response) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.content.decode())).split())

    def send(self, form, *, press=None, values=None, follow=True):
        data = as_post(form.submission(press=press, values=values))
        return self.client.post(form.action.split("#")[0], data, follow=follow)

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def as_member(self):
        membership = make_login(TEST_TENANT_PK, "serveur@example.invalid")
        self.client.force_login(membership.user)
        return membership


# -- « Notifications » ---------------------------------------------------------------------------------------------


class HomeTests(PageCase):
    def test_the_page_says_what_it_is_and_draws_every_state_hidden_but_one(self):
        response = self.get(HOME)
        html = response.content.decode()
        self.assertContains(response, "<h1>Notifications</h1>")
        self.assertContains(response, "Rappels et alertes sur vos téléphones.")
        states = re.findall(r'<(?:p|div) data-device-state="([a-z-]+)"([^>]*)>', html)
        self.assertEqual(
            [state for state, _ in states],
            ["nojs", "ios-install", "ios-update", "unsupported", "denied-ios", "denied", "activate", "active"],
        )
        self.assertEqual([state for state, attrs in states if "hidden" not in attrs], ["nojs"])
        self.assertIn('id="push-config"', html)
        self.assertIn("notifications.js?v=", html)

    def test_the_island_carries_the_key_and_the_addresses(self):
        from notifications import webpush

        html = self.html(HOME)
        island = re.search(r'<script id="push-config" type="application/json">(.*?)</script>', html).group(1)
        self.assertIn(webpush.vapid_public_key(), island)
        self.assertIn(reverse("notifications:subscribe"), island)
        self.assertIn(reverse("notifications:sync"), island)

    def test_a_dev_server_says_it_sends_nothing(self):
        self.assertContains(self.get(HOME), "Envois désactivés sur ce serveur (serveur de développement).")

    @override_settings(SITE_URL=SITE)
    def test_a_production_server_says_where_its_scheduler_is(self):
        self.assertContains(self.get(HOME), "Le planificateur ne tourne pas : les rappels ne partent pas.")
        settings_row = NotificationSettings.get_solo()
        NotificationSettings.objects.filter(pk=settings_row.pk).update(last_tick_at=timezone.now())
        self.assertContains(self.get(HOME), "Planificateur actif (dernier passage à")
        old = timezone.now() - timedelta(minutes=10)
        NotificationSettings.objects.filter(pk=settings_row.pk).update(last_tick_at=old)
        local = timezone.localtime(old)
        self.assertContains(self.get(HOME), f"Le planificateur ne tourne pas depuis le {local:%d/%m} à {local:%H:%M}")

    def test_a_get_writes_no_settings_row(self):
        self.get(HOME)
        self.get(REMINDERS)
        self.assertFalse(NotificationSettings.objects.exists())

    def test_my_devices_and_only_mine(self):
        mine = Membership.objects.get(user=test_user(), tenant_id=TEST_TENANT_PK)
        make_device(mine, label="Android · Chrome")
        make_device(mine, label="iPhone · application", logged_out_at=timezone.now())
        other = make_login(TEST_TENANT_PK, "collegue@example.invalid")
        make_device(other, label="Windows · Edge")
        text = self.text(self.get(HOME))
        self.assertIn("Android · Chrome", text)
        self.assertIn("iPhone · application à réactiver", text)
        self.assertNotIn("Windows · Edge", text)

    def test_the_rules_in_short(self):
        make_reminder()
        EventRule.objects.create(event=CONSIGNES, outcomes=["differs"], recipient_ids=[1])
        text = self.text(self.get(HOME))
        self.assertIn("1 rappel actif · prochain envoi : Vides avant livraison :", text)
        self.assertIn("Consignes : bon du livreur comparé à la reprise", text)

    def test_the_latest_dispatches_read_as_cards_on_a_phone(self):
        for number, status in enumerate((Dispatch.Status.SENT, Dispatch.Status.MISSED, Dispatch.Status.SKIPPED)):
            Dispatch.objects.create(
                kind=Dispatch.Kind.REMINDER,
                rule_name="Vides avant livraison",
                dedupe_key=f"reminder:1:2027010{number}T2300Z",
                title="Consignes",
                ttl=3600,
                status=status,
                detail="manqué : serveur arrêté" if status == Dispatch.Status.MISSED else "",
            )
        response = self.get(HOME)
        self.assertContains(response, 'data-table-label="Derniers envois"')
        for word in ("envoyé", "manqué", "sauté"):
            self.assertContains(response, word)
        self.assertContains(response, "status-sent")
        cards = PhoneCardsLabelTests.Cards()
        cards.feed(response.content.decode())
        table = next(
            table for table in cards.tables if table["attributes"].get("data-table-label") == "Derniers envois"
        )
        checker = PhoneCardsLabelTests()
        checker.assertLabelled(table, 3)

    def test_the_auto_gathers_link_waits_for_its_page(self):
        with mock.patch("notifications.views._auto_gathers_url", return_value="/invoices/recuperation-auto/"):
            self.assertContains(self.get(HOME), 'href="/invoices/recuperation-auto/"')


# -- « Rappels » ---------------------------------------------------------------------------------------------------


class NewReminderTests(PageCase):
    def new_form(self):
        return form_posting_to(self.html(REMINDERS), REMINDERS)

    def test_the_new_card_has_generic_defaults_and_no_day_ticked(self):
        form = self.new_form()
        self.assertEqual(form.control("nouveau-name").value, NEW_REMINDER["name"])
        self.assertEqual(form.control("nouveau-times").value, "00:00")
        self.assertEqual(form.control("nouveau-skip_if").value, registry.RECENT_PICKUP)
        self.assertNotIn("checked", [c.attrs.get("checked") for c in form.controls if c.name == "nouveau-days"])
        html = self.html(REMINDERS)
        self.assertIn(
            "Livraison le matin ? Cochez le soir d'avant : pour une livraison le mardi, le lundi soir.", unescape(html)
        )
        self.assertIn("samedi soir", html)
        self.assertIn('samedi soir <span class="muted">→ dim. 00:00</span>', unescape(html))

    def test_enter_previews_and_saves_nothing(self):
        form = self.new_form()
        tick(form, "nouveau-days", [0, 2, 5])
        response = self.send(form)  # the first button: the hidden « Aperçu »
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Reminder.objects.exists())
        text = self.text(response)
        self.assertIn("Aperçu : rien n'est enregistré.", text)
        self.assertRegex(text, r"(mar|jeu|dim)\. \d\d/\d\d à 00:00 — nuit de (lundi|mercredi|samedi)")
        self.assertIn("sauté si une reprise a été saisie depuis", text)

    def test_saving_the_new_card(self):
        form = self.new_form()
        tick(form, "nouveau-days", [0, 2, 5])
        response = self.send(form, press=("action", "enregistrer"))
        reminder = Reminder.objects.get()
        self.assertEqual(response.redirect_chain[-1][0], f"{REMINDERS}#rappel-{reminder.pk}")
        self.assertEqual(reminder.weekdays, "0,2,5")
        self.assertEqual(reminder.times, "00:00")
        self.assertEqual(reminder.target, reverse("returnables:home") + "#new-pickup")
        self.assertEqual(reminder.skip_if, registry.RECENT_PICKUP)
        self.assertEqual(reminder.skip_hours, 6)
        self.assertTrue(reminder.all_members)
        self.assertTrue(reminder.is_active)
        self.assertIn(f"Rappel « {NEW_REMINDER['name']} » enregistré.", self.messages_of(response))

    def test_refusals_in_french(self):
        form = self.new_form()
        response = self.send(
            form, press=("action", "enregistrer"), values={"nouveau-times": "25:00", "nouveau-name": ""}
        )
        self.assertEqual(response.status_code, 200)
        text = self.text(response)
        self.assertIn("« 25:00 » n'est pas une heure (00:00 à 23:59).", text)
        self.assertIn("Cochez au moins un jour.", text)
        self.assertIn("Donnez un nom au rappel.", text)
        self.assertFalse(Reminder.objects.exists())

    def test_a_nul_is_refused_in_french(self):
        form = self.new_form()
        tick(form, "nouveau-days", [5])
        response = self.send(form, press=("action", "enregistrer"), values={"nouveau-title": "Con\x00signes"})
        self.assertIn(NUL_REFUSED, self.text(response))
        self.assertFalse(Reminder.objects.exists())

    def test_another_page_is_a_path_of_this_site_only(self):
        for path, saved in (
            ("/banque/?mois=2026-07", True),
            ("https://ailleurs.example/", False),
            ("//ailleurs.example/x", False),
            ("pas-un-chemin", False),
        ):
            with self.subTest(path=path):
                Reminder.objects.all().delete()
                form = self.new_form()
                tick(form, "nouveau-days", [5])
                response = self.send(
                    form,
                    press=("action", "enregistrer"),
                    values={"nouveau-page": registry.OTHER_PAGE, "nouveau-path": path},
                )
                if saved:
                    self.assertEqual(Reminder.objects.get().target, path)
                else:
                    self.assertFalse(Reminder.objects.exists())
                    self.assertIn(PAGE_REFUSED, self.text(response))

    def test_skip_hours_and_the_supplier(self):
        supplier = make_supplier(name="Brasserie Exemple")
        form = self.new_form()
        tick(form, "nouveau-days", [5])
        response = self.send(form, press=("action", "enregistrer"), values={"nouveau-skip_hours": "49"})
        self.assertIn("Un nombre d'heures entre 1 et 48.", self.text(response))
        form = self.new_form()
        tick(form, "nouveau-days", [5])
        self.send(
            form,
            press=("action", "enregistrer"),
            values={"nouveau-skip_hours": "12", "nouveau-skip_supplier": str(supplier.pk)},
        )
        reminder = Reminder.objects.get()
        self.assertEqual((reminder.skip_hours, reminder.skip_supplier_id), (12, supplier.pk))

    def test_no_more_than_fifty(self):
        for number in range(MAX_REMINDERS):
            make_reminder(name=f"Rappel {number}")
        form = self.new_form()
        tick(form, "nouveau-days", [5])
        response = self.send(form, press=("action", "enregistrer"))
        self.assertIn(TOO_MANY_REMINDERS, self.text(response))
        self.assertEqual(Reminder.objects.count(), MAX_REMINDERS)


class ReminderEditTests(PageCase):
    def setUp(self):
        super().setUp()
        self.reminder = make_reminder()
        self.action = reverse("notifications:reminder_edit", args=[self.reminder.pk])

    def edit_form(self):
        return form_posting_to(self.html(REMINDERS), self.action)

    def test_the_card_draws_the_saved_reminder(self):
        form = self.edit_form()
        prefix = f"rappel-{self.reminder.pk}"
        self.assertEqual(form.control(f"{prefix}-page").value, "consignes-reprise")
        ticked = [c.value for c in form.controls if c.name == f"{prefix}-days" and "checked" in c.attrs]
        self.assertEqual(ticked, ["0", "2", "5"])
        html = unescape(self.html(REMINDERS))
        self.assertIn(f'id="rappel-{self.reminder.pk}"', html)
        self.assertIn("— nuit de", html)

    def test_saved_with_its_own_columns_only(self):
        form = self.edit_form()
        prefix = f"rappel-{self.reminder.pk}"
        tick(form, f"{prefix}-days", [6])
        response = self.send(form, press=("action", "enregistrer"), values={f"{prefix}-times": "18h"})
        self.reminder.refresh_from_db()
        self.assertEqual((self.reminder.weekdays, self.reminder.times), ("6", "18:00"))
        self.assertIn("Rappel « Vides avant livraison » enregistré.", self.messages_of(response))

    def test_a_preview_saves_nothing(self):
        form = self.edit_form()
        prefix = f"rappel-{self.reminder.pk}"
        response = self.send(form, values={f"{prefix}-times": "18:00"})
        self.assertEqual(response.status_code, 200)
        self.reminder.refresh_from_db()
        self.assertEqual(self.reminder.times, "00:00 02:00")
        self.assertIn("à 18:00", self.text(response))

    def test_switched_off_by_the_state_wanted(self):
        form = self.edit_form()
        prefix = f"rappel-{self.reminder.pk}"
        self.send(form, press=("action", "enregistrer"), values={f"{prefix}-is_active": False})
        self.reminder.refresh_from_db()
        self.assertFalse(self.reminder.is_active)
        self.assertContains(self.get(REMINDERS), "Inactif : rien ne part.")

    def test_deleted_from_its_danger_zone(self):
        form = form_posting_to(self.html(REMINDERS), reverse("notifications:reminder_delete", args=[self.reminder.pk]))
        response = self.send(form)
        self.assertFalse(Reminder.objects.exists())
        self.assertIn("Rappel « Vides avant livraison » supprimé.", self.messages_of(response))

    def test_a_get_on_its_actions_goes_back_to_its_card(self):
        for name in ("notifications:reminder_edit", "notifications:reminder_delete"):
            response = self.client.get(reverse(name, args=[self.reminder.pk]))
            self.assertRedirects(response, f"{REMINDERS}#rappel-{self.reminder.pk}", fetch_redirect_response=False)
        self.assertTrue(Reminder.objects.exists())

    def test_an_unknown_reminder_is_a_404(self):
        client = self.client_class()
        self.assertEqual(client.post(reverse("notifications:reminder_edit", args=[999])).status_code, 404)


class NightTests(PageCase):
    def night_form(self):
        return form_posting_to(self.html(REMINDERS), NIGHT)

    def test_the_night_is_saved_and_the_next_sends_said_again(self):
        make_reminder()
        response = self.send(self.night_form(), values={"night_ends_at": "4h"})
        self.assertEqual(NotificationSettings.get_solo().night_ends_at, time(4, 0))
        said = self.messages_of(response)[0]
        self.assertTrue(said.startswith("La nuit se termine à 04:00. Prochains envois : Vides avant livraison :"))

    def test_the_calendar_and_its_legend(self):
        self.send(self.night_form(), values={"night_ends_at": "00:00"})
        self.assertEqual(NotificationSettings.get_solo().night_ends_at, time(0, 0))
        html = self.html(REMINDERS)
        self.assertIn("<legend>Jours</legend>", html)

    def test_refused_in_french(self):
        for typed, said in (("02:00", schedule.NIGHT_END_REFUSED), ("6h 7h", "Une seule heure, ex. 06:00.")):
            with self.subTest(typed=typed):
                response = self.send(self.night_form(), values={"night_ends_at": typed})
                self.assertEqual(response.status_code, 200)
                self.assertIn(said, self.text(response))
        self.assertFalse(NotificationSettings.objects.exists())

    def test_only_its_column_is_written(self):
        settings_row = NotificationSettings.get_solo()
        moment = datetime(2027, 1, 5, 23, 0, tzinfo=UTC)
        NotificationSettings.objects.filter(pk=settings_row.pk).update(last_tick_at=moment)
        self.send(self.night_form(), values={"night_ends_at": "05:00"})
        self.assertEqual(NotificationSettings.get_solo().last_tick_at, moment)


# -- « Alertes » ---------------------------------------------------------------------------------------------------


class EventTests(PageCase):
    def event_form(self, key):
        return form_posting_to(self.html(EVENTS), reverse("notifications:event_edit", args=[key]))

    def test_every_event_has_its_card_switched_off_until_saved(self):
        response = self.get(EVENTS)
        for key in registry.EVENTS:
            self.assertContains(response, f'id="alerte-{key}"')
        self.assertContains(response, "désactivée", count=len(registry.EVENTS))
        self.assertContains(response, "Une source en échec n'est signalée qu'une fois par jour.")
        self.assertContains(
            response, "La relève automatique des bons se règle dans Factures › Récupération automatique."
        )

    def test_the_first_save_makes_the_row_for_the_owner(self):
        form = self.event_form(CONSIGNES)
        prefix = f"alerte-{CONSIGNES}"
        self.assertEqual(form.control(f"{prefix}-page").value, "")
        response = self.send(form, values={f"{prefix}-is_active": True})
        rule = EventRule.objects.get(event=CONSIGNES)
        self.assertTrue(rule.is_active)
        self.assertEqual(rule.outcomes, ["match", "differs", "no_pickup", "to_check"])
        self.assertEqual(rule.target, "")
        self.assertFalse(rule.all_members)
        self.assertEqual(rule.recipient_ids, [test_user().pk])
        self.assertIn(
            "Alerte « Consignes : bon du livreur comparé à la reprise » enregistrée.", self.messages_of(response)
        )

    def test_outcomes_and_recipients_with_a_second_member(self):
        member = make_login(TEST_TENANT_PK, "second@example.invalid")
        form = self.event_form(GATHER)
        prefix = f"alerte-{GATHER}"
        tick(form, f"{prefix}-outcomes", ["failed"])
        tick(form, f"{prefix}-recipients", [member.user_id])
        self.send(form, values={f"{prefix}-is_active": True})
        rule = EventRule.objects.get(event=GATHER)
        self.assertEqual(rule.outcomes, ["failed"])
        self.assertEqual(rule.recipient_ids, [member.user_id])

    def test_refusals(self):
        make_login(TEST_TENANT_PK, "second@example.invalid")
        form = self.event_form(GATHER)
        prefix = f"alerte-{GATHER}"
        tick(form, f"{prefix}-outcomes", [])
        tick(form, f"{prefix}-recipients", [])
        response = self.send(form)
        text = self.text(response)
        self.assertIn("Cochez au moins un résultat.", text)
        self.assertIn("Choisissez au moins un destinataire.", text)
        self.assertFalse(EventRule.objects.exists())

    def test_a_recipient_who_is_no_active_member_is_refused(self):
        stranger = make_login(TEST_TENANT_PK, "parti@example.invalid", active=False)
        make_login(TEST_TENANT_PK, "second@example.invalid")
        prefix = f"alerte-{GATHER}"
        form = self.event_form(GATHER)
        data = as_post(form.submission())
        data[f"{prefix}-recipients"] = [str(stranger.user_id)]
        response = self.client.post(form.action.split("#")[0], data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Destinataire inconnu.", self.text(response))
        self.assertFalse(EventRule.objects.exists())

    def test_an_unknown_event_is_a_404(self):
        self.assertEqual(self.client.get(reverse("notifications:event_edit", args=["inconnu"])).status_code, 404)
        self.assertEqual(
            self.client_class().post(reverse("notifications:event_edit", args=["inconnu"])).status_code, 404
        )

    def test_a_get_goes_back_to_its_card(self):
        response = self.client.get(reverse("notifications:event_edit", args=[CONSIGNES]))
        self.assertRedirects(response, f"{EVENTS}#alerte-{CONSIGNES}", fetch_redirect_response=False)


# -- Who may do what -----------------------------------------------------------------------------------------------


class MemberTests(PageCase):
    def setUp(self):
        super().setUp()
        self.reminder = make_reminder()
        self.member = self.as_member()

    def test_a_member_reads_without_forms(self):
        for url in (REMINDERS, EVENTS):
            with self.subTest(url=url):
                response = self.get(url)
                self.assertIn("Seul le propriétaire de l'espace modifie ces réglages.", self.text(response))
                actions = [form.action for form in forms_of(response.content.decode())]
                self.assertEqual([action for action in actions if action.startswith("/notifications/")], [])
        self.assertContains(self.get(REMINDERS), "Vides avant livraison")

    def test_a_member_s_post_is_the_403_page(self):
        for url in (
            REMINDERS,
            NIGHT,
            reverse("notifications:reminder_edit", args=[self.reminder.pk]),
            reverse("notifications:reminder_delete", args=[self.reminder.pk]),
            reverse("notifications:event_edit", args=[CONSIGNES]),
        ):
            with self.subTest(url=url):
                # CSRF not enforced: the refusal must be the owner's rule.
                client = self.client_class()
                client.force_login(self.member.user)
                response = client.post(url, {"night_ends_at": "05:00"})
                self.assertEqual(response.status_code, 403)
        self.assertTrue(Reminder.objects.filter(pk=self.reminder.pk).exists())
        self.assertFalse(EventRule.objects.exists())
        self.assertFalse(NotificationSettings.objects.exists())

    def test_a_member_still_has_the_device_card(self):
        response = self.get(HOME)
        self.assertContains(response, 'id="cet-appareil"')


# -- Every POST route -----------------------------------------------------------------------------------------------


class CsrfTests(QuietLogs, TestCase):
    """Every route that writes refuses a post without the CSRF token."""

    def test_every_post_route_is_protected(self):
        reminder = make_reminder()
        mine = Membership.objects.get(user=test_user(), tenant_id=TEST_TENANT_PK)
        device = make_device(mine)
        client = self.client_class(enforce_csrf_checks=True)
        for url in (
            REMINDERS,
            NIGHT,
            reverse("notifications:reminder_edit", args=[reminder.pk]),
            reverse("notifications:reminder_delete", args=[reminder.pk]),
            reverse("notifications:event_edit", args=[CONSIGNES]),
            reverse("notifications:subscribe"),
            reverse("notifications:sync"),
            reverse("notifications:device_delete", args=[device.pk]),
            reverse("notifications:test"),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.post(url, {}).status_code, 403)
        self.assertTrue(Reminder.objects.filter(pk=reminder.pk).exists())
        self.assertTrue(type(device).objects.filter(pk=device.pk).exists())
