"""What a rule may name (notifications/registry.py): the events, the skip
condition and the pages a notification opens."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime, timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import NoReverseMatch, reverse
from django.urls.converters import SlugConverter

from notifications import registry
from returnables.models import Pickup
from tests.factories import make_supplier

#: 02:00 in Paris on Sunday 08/11/2026 (winter time), a reminder's moment.
NOW = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)


class EventsTests(SimpleTestCase):
    def test_the_three_events_in_their_order(self):
        self.assertEqual(
            list(registry.EVENTS), ["returnables-comparison", "invoices-auto-gather", "recipes-auto-sales"]
        )
        self.assertEqual(registry.RETURNABLES_COMPARISON, "returnables-comparison")
        self.assertEqual(registry.INVOICES_AUTO_GATHER, "invoices-auto-gather")
        self.assertEqual(registry.RECIPES_AUTO_SALES, "recipes-auto-sales")

    def test_the_automatic_sales_import(self):
        event = registry.EVENTS["recipes-auto-sales"]
        self.assertEqual(event.label, "Import automatique des ventes")
        self.assertEqual(
            [(outcome.key, outcome.label) for outcome in event.outcomes],
            [("new", "ventes importées"), ("failed", "échec"), ("nothing", "rien de nouveau")],
        )
        self.assertEqual(event.default_outcomes, ("failed",))
        self.assertEqual(event.page_label, "Ventes")

    def test_their_keys_are_url_slugs(self):
        slug = SlugConverter.regex
        for key, event in registry.EVENTS.items():
            with self.subTest(key=key):
                self.assertRegex(key, f"^{slug}$")
                self.assertEqual(event.key, key)

    def test_the_slip_comparison(self):
        event = registry.EVENTS["returnables-comparison"]
        self.assertEqual(event.label, "Consignes : bon du livreur comparé à la reprise")
        self.assertEqual(
            [(outcome.key, outcome.label) for outcome in event.outcomes],
            [
                ("match", "conforme"),
                ("differs", "écart"),
                ("no_pickup", "aucune reprise saisie"),
                ("to_check", "à vérifier"),
            ],
        )
        self.assertEqual(event.default_outcomes, ("match", "differs", "no_pickup", "to_check"))
        self.assertEqual(event.page_label, "le bon")

    def test_the_automatic_gather(self):
        event = registry.EVENTS["invoices-auto-gather"]
        self.assertEqual(event.label, "Récupération automatique")
        self.assertEqual(
            [(outcome.key, outcome.label) for outcome in event.outcomes],
            [("new", "du nouveau"), ("failed", "une source a échoué"), ("nothing", "rien de nouveau")],
        )
        self.assertEqual(event.default_outcomes, ("new", "failed"))
        self.assertEqual(event.page_label, "Factures")

    def test_defaults_are_outcomes_of_their_event(self):
        for event in registry.EVENTS.values():
            with self.subTest(event=event.key):
                self.assertTrue(set(event.default_outcomes) <= set(event.outcome_keys))
                self.assertEqual(len(set(event.outcome_keys)), len(event.outcomes))

    def test_looked_up_by_key(self):
        self.assertIs(registry.event("invoices-auto-gather"), registry.EVENTS["invoices-auto-gather"])
        for key in ("inconnu", "", None, 3, ["returnables-comparison"]):
            with self.subTest(key=key):
                self.assertIsNone(registry.event(key))
        event = registry.EVENTS["returnables-comparison"]
        self.assertEqual(event.outcome_label("differs"), "écart")
        self.assertEqual(event.outcome_label("ancien"), "ancien")


class NoModelAtImportTests(SimpleTestCase):
    def test_the_module_imports_no_app_at_its_top(self):
        """The skip condition imports its model when it runs: the registry is
        read by forms and urls that load before any table is asked."""
        tree = ast.parse(inspect.getsource(registry))
        names = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        apps = ("accounts", "inventory", "invoices", "recipes", "bank", "transfer", "margins", "staff", "returnables")
        self.assertFalse([name for name in names if name.split(".")[0] in apps])


class RecentPickupTests(TestCase):
    """« Ne pas envoyer si une reprise a été enregistrée dans les N dernières
    heures »: a reprise CREATED then, never one edited then."""

    def setUp(self):
        self.condition = registry.SKIP_CONDITIONS["returnables.recent_pickup"]
        self.brasserie = make_supplier(code="BRASSERIE-TEST", name="Brasserie des Essais")
        self.cave = make_supplier(code="CAVE-TEST", name="Cave des Essais")

    def pickup(self, created_at, supplier=None, day=date(2026, 11, 8)):
        pickup = Pickup.objects.create(date=day, supplier=supplier or self.brasserie)
        Pickup.objects.filter(pk=pickup.pk).update(created_at=created_at)
        return pickup

    def test_the_condition(self):
        self.assertEqual(registry.RECENT_PICKUP, "returnables.recent_pickup")
        self.assertEqual(
            self.condition.label,
            "Ne pas envoyer si une reprise de consignes a été enregistrée dans les N dernières heures",
        )
        self.assertEqual(
            registry.skip_choices(),
            [("", "— toujours envoyer —"), ("returnables.recent_pickup", "une reprise de consignes a été enregistrée")],
        )
        self.assertIs(registry.skip_condition("returnables.recent_pickup"), self.condition)
        for key in ("", "inconnue", None):
            with self.subTest(key=key):
                self.assertIsNone(registry.skip_condition(key))

    def test_no_reprise_sends(self):
        self.assertEqual(self.condition.check(NOW, 6), "")

    def test_a_reprise_created_within_the_hours_skips_and_says_when(self):
        self.pickup(NOW - timedelta(minutes=105))
        self.assertEqual(self.condition.check(NOW, 6), "sauté : une reprise a été enregistrée à 00:15")

    def test_the_latest_is_named(self):
        self.pickup(NOW - timedelta(hours=5))
        self.pickup(NOW - timedelta(minutes=105))
        self.assertEqual(self.condition.check(NOW, 6), "sauté : une reprise a été enregistrée à 00:15")

    def test_older_than_the_hours_sends(self):
        self.pickup(NOW - timedelta(hours=6, seconds=1))
        self.assertEqual(self.condition.check(NOW, 6), "")
        self.assertNotEqual(self.condition.check(NOW, 7), "")

    def test_an_old_reprise_edited_tonight_does_not_skip(self):
        """Re-dated (« Mettre la reprise au … ») or given a photo, an old
        reprise has a new updated_at - and must not silence the reminder."""
        old = self.pickup(NOW - timedelta(days=3), day=date(2026, 11, 5))
        Pickup.objects.filter(pk=old.pk).update(date=date(2026, 11, 8), updated_at=NOW - timedelta(minutes=10))
        self.assertEqual(self.condition.check(NOW, 6), "")

    def test_by_one_supplier(self):
        self.pickup(NOW - timedelta(minutes=105), supplier=self.cave)
        self.assertEqual(self.condition.check(NOW, 6, self.brasserie.pk), "")
        self.assertNotEqual(self.condition.check(NOW, 6, self.cave.pk), "")
        self.assertNotEqual(self.condition.check(NOW, 6, None), "")
        self.assertNotEqual(self.condition.check(NOW, 6), "")

    def test_a_reprise_with_no_supplier_counts_for_any(self):
        Pickup.objects.filter(pk=self.pickup(NOW - timedelta(minutes=105)).pk).update(supplier=None)
        self.assertNotEqual(self.condition.check(NOW, 6), "")
        self.assertEqual(self.condition.check(NOW, 6, self.brasserie.pk), "")

    def test_the_preview_line(self):
        since = NOW - timedelta(hours=6)
        self.assertEqual(self.condition.preview(since), "sauté si une reprise a été saisie depuis 20:00")
        self.assertEqual(self.condition.preview(since, "UBA"), "sauté si une reprise UBA a été saisie depuis 20:00")


class PagesTests(SimpleTestCase):
    def test_the_pages_offered(self):
        self.assertEqual(
            [(page.key, page.label, page.url_name, page.fragment) for page in registry.PAGES],
            [
                ("consignes-reprise", "Consignes — nouvelle reprise", "returnables:home", "#new-pickup"),
                ("consignes", "Consignes", "returnables:home", ""),
                ("factures", "Factures", "invoices:invoice_list", ""),
                ("stock", "Stock", "inventory:stock_list", ""),
                ("inventaires", "Inventaires", "inventory:stock_take_list", ""),
                ("banque", "Banque", "bank:bank_home", ""),
                ("recettes", "Recettes & ventes", "recipes:recipe_list", ""),
                ("marges", "Marges", "margins:margins_home", ""),
                ("personnel", "Personnel", "staff:home", ""),
                ("donnees", "Données", "transfer:data_home", ""),
                ("notifications", "Notifications", "notifications:home", ""),
            ],
        )
        self.assertEqual(len({page.key for page in registry.PAGES}), len(registry.PAGES))

    def test_only_the_url_names_that_exist_are_offered(self):
        offered = {page.key: path for page, path in registry.available_pages()}
        for page in registry.PAGES:
            with self.subTest(page=page.key):
                try:
                    expected = reverse(page.url_name) + page.fragment
                except NoReverseMatch:
                    self.assertNotIn(page.key, offered)
                else:
                    self.assertEqual(offered[page.key], expected)
        self.assertEqual(offered["consignes-reprise"], "/consignes/#new-pickup")
        self.assertEqual(offered["consignes"], "/consignes/")
        self.assertEqual(offered["factures"], "/invoices/")

    def test_a_url_name_gone_is_left_out(self):
        pages = (registry.Page("ailleurs", "Ailleurs", "nulle-part:rien"), registry.PAGES[1])
        with mock.patch.object(registry, "PAGES", pages):
            self.assertEqual(registry.page_path(pages[0]), "")
            self.assertEqual([page.key for page, _ in registry.available_pages()], ["consignes"])
            self.assertEqual(registry.page_choices(), [("consignes", "Consignes"), ("autre", "Autre page du site…")])
            self.assertEqual(registry.page_target("ailleurs"), "")

    def test_the_select_ends_with_another_page(self):
        choices = registry.page_choices()
        self.assertEqual(choices[0], ("consignes-reprise", "Consignes — nouvelle reprise"))
        self.assertEqual(choices[-1], (registry.OTHER_PAGE, "Autre page du site…"))
        self.assertEqual(registry.OTHER_PAGE, "autre")

    def test_a_key_to_its_path_and_back(self):
        self.assertEqual(registry.page_target("consignes-reprise"), "/consignes/#new-pickup")
        self.assertEqual(registry.page_target("consignes"), "/consignes/")
        for key in ("autre", "inconnue", "", None):
            with self.subTest(key=key):
                self.assertEqual(registry.page_target(key), "")
        self.assertEqual(registry.page_key_for("/consignes/#new-pickup"), "consignes-reprise")
        self.assertEqual(registry.page_key_for("/consignes/"), "consignes")
        self.assertEqual(registry.page_key_for("/invoices/"), "factures")
        for target in ("/consignes/bons/3/", "/ailleurs/", "", None):
            with self.subTest(target=target):
                self.assertEqual(registry.page_key_for(target), "autre")
