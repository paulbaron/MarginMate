"""The « Du … au … » window on the Ventes tab.

The owner asked to see only what was sold between two dates. Everything the
tab shows about sales is narrowed by it - the sales, the sale documents and
the totals by origin - because a table of all-time totals sitting above a
windowed list is read as the window's figures, and nothing on the page would
say otherwise.

The failures worth testing here are all silent:

* a boundary day dropped (the window is INCLUSIVE at both ends, unlike
  recipes.sales.sales_between, which excludes its start);
* the sale documents windowed AFTER their « 50 most recent » slice, which
  shows an empty February because March is longer than fifty documents;
* a count or a « tout afficher » link counted over the whole table while the
  list beside it is windowed;
* the window lost on the next click (the search, « tout afficher », the tab);
* the import card's own dates - the period to FETCH from the till - wired to
  the window by someone who took the two pairs for one.

Data invented.
"""

import re
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from common import DateRange
from recipes.forms import MANUAL_SALE_SOURCE
from recipes.menu import _window_label
from recipes.models import RecipeSale, SaleDocument
from tests.factories import make_recipe

WINDOW = {"du": "2026-02-01", "au": "2026-02-28"}
WINDOW_QUERY = "du=2026-02-01&au=2026-02-28"


def sale(recipe, day: date, quantity: int = 1, source: str = "laddition") -> RecipeSale:
    return RecipeSale.objects.create(recipe=recipe, sold_on=day, quantity=quantity, source=source)


class WindowLabelTests(TestCase):
    """What the page says it is showing, for each shape of window."""

    def test_both_ends(self):
        label = _window_label(DateRange(date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(label, "Du 01/02/2026 au 28/02/2026")

    def test_one_end_alone(self):
        self.assertEqual(_window_label(DateRange(start=date(2026, 2, 1))), "Depuis le 01/02/2026")
        self.assertEqual(_window_label(DateRange(end=date(2026, 2, 28))), "Jusqu'au 28/02/2026")

    def test_no_window_says_nothing(self):
        self.assertEqual(_window_label(DateRange()), "")


class SalesListWindowTests(TestCase):
    """« Dernières ventes », narrowed on sold_on."""

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        self.days = [
            date(2026, 1, 31),  # the day before: out
            date(2026, 2, 1),   # the opening day: in
            date(2026, 2, 14),  # in
            date(2026, 2, 28),  # the closing day: in, this is the whole point
            date(2026, 3, 1),   # the day after: out
        ]
        for day in self.days:
            sale(self.mule, day)

    def shown(self, **params):
        response = self.client.get(reverse("recipes:sales_list"), params)
        self.assertEqual(response.status_code, 200)
        return response

    def test_both_boundary_days_are_inside_the_window(self):
        response = self.shown(**WINDOW)
        self.assertEqual(
            sorted(row.sold_on for row in response.context["sales"]),
            [date(2026, 2, 1), date(2026, 2, 14), date(2026, 2, 28)],
        )

    def test_one_end_alone_is_a_window(self):
        since = self.shown(du="2026-02-28")
        self.assertEqual(
            sorted(row.sold_on for row in since.context["sales"]), [date(2026, 2, 28), date(2026, 3, 1)]
        )
        until = self.shown(au="2026-02-01")
        self.assertEqual(
            sorted(row.sold_on for row in until.context["sales"]), [date(2026, 1, 31), date(2026, 2, 1)]
        )

    def test_no_window_is_every_sale(self):
        self.assertEqual(len(self.shown().context["sales"]), len(self.days))

    def test_a_date_that_is_no_date_is_the_whole_list_not_a_500(self):
        response = self.shown(du="n-importe-quoi", au="hier")
        self.assertEqual(len(response.context["sales"]), len(self.days))
        self.assertFalse(response.context["date_window"])

    def test_an_empty_window_says_the_dates_are_empty(self):
        response = self.shown(du="2026-06-01", au="2026-06-30")
        self.assertEqual(list(response.context["sales"]), [])
        page = response.content.decode()
        self.assertIn("Du 01/06/2026 au 30/06/2026", page)
        self.assertIn("Aucune vente sur cette période", page)

    def test_the_window_is_said_in_words_beside_the_figures(self):
        page = self.shown(**WINDOW).content.decode()
        self.assertIn("Du 01/02/2026 au 28/02/2026", page)
        self.assertIn('class="muted date-range-note"', page)

    def test_the_date_inputs_hold_the_window_back(self):
        page = self.shown(**WINDOW).content.decode()
        self.assertIn('name="du" value="2026-02-01"', page)
        self.assertIn('name="au" value="2026-02-28"', page)


class SalesCountAndShowAllTests(TestCase):
    """The count and the « tout afficher » line under a window.

    Counted over the whole table, they say « 5 de plus » under a page of one.
    """

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        for day in (date(2026, 2, 3), date(2026, 2, 10), date(2026, 2, 17)):
            sale(self.mule, day)
        for day in (date(2026, 1, 5), date(2026, 3, 5), date(2026, 3, 6)):
            sale(self.mule, day)

    def page(self, **params):
        # Two rows a page rather than three hundred sales: the arithmetic is
        # the same and the test stays readable.
        with patch("recipes.menu.SALES_PAGE_SIZE", 2):
            return self.client.get(reverse("recipes:sales_list"), params)

    def test_the_hidden_count_is_counted_over_the_window(self):
        response = self.page(**WINDOW)
        self.assertEqual(len(response.context["sales"]), 2)
        self.assertEqual(response.context["sales_hidden"], 1)
        self.assertEqual(response.context["sales_found"], 3)

    def test_tout_afficher_carries_the_window(self):
        page = self.page(**WINDOW).content.decode()
        link = re.search(r'href="([^"]*)"[^>]*>\s*tout afficher', page)
        self.assertIsNotNone(link, "the « tout afficher » link is drawn under a window")
        href = link.group(1)
        self.assertIn("ventes=toutes", href)
        self.assertIn("du=2026-02-01", href)
        self.assertIn("au=2026-02-28", href)

    def test_showing_them_all_still_shows_only_the_window(self):
        response = self.page(ventes="toutes", **WINDOW)
        self.assertEqual(len(response.context["sales"]), 3)
        self.assertEqual(response.context["sales_hidden"], 0)

    def test_the_window_form_keeps_the_page_the_reader_was_on(self):
        page = self.page(ventes="toutes", **WINDOW).content.decode()
        self.assertIn('<input type="hidden" name="ventes" value="toutes">', page)


class SalesSearchAndWindowTests(TestCase):
    """A search and a window, together and each surviving the other."""

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        self.spritz = make_recipe(name="Spritz")
        sale(self.mule, date(2026, 2, 10))
        sale(self.spritz, date(2026, 2, 11))
        sale(self.mule, date(2026, 3, 10))

    def test_both_narrow_the_list(self):
        response = self.client.get(reverse("recipes:sales_list"), {"vente": "Mule", **WINDOW})
        self.assertEqual(
            [(row.recipe.name, row.sold_on) for row in response.context["sales"]],
            [("Mule", date(2026, 2, 10))],
        )
        self.assertEqual(response.context["sales_found"], 1)

    def test_the_search_form_carries_the_window(self):
        page = self.client.get(reverse("recipes:sales_list"), {"vente": "Mule", **WINDOW}).content.decode()
        self.assertIn('<input type="hidden" name="du" value="2026-02-01">', page)
        self.assertIn('<input type="hidden" name="au" value="2026-02-28">', page)

    def test_the_window_form_carries_the_search(self):
        page = self.client.get(reverse("recipes:sales_list"), {"vente": "Mule", **WINDOW}).content.decode()
        self.assertIn('<input type="hidden" name="vente" value="Mule">', page)

    def test_clearing_the_search_keeps_the_window(self):
        page = self.client.get(reverse("recipes:sales_list"), {"vente": "Mule", **WINDOW}).content.decode()
        clear = re.search(r'href="([^"]*)"[^>]*>Effacer la recherche', page)
        self.assertIsNotNone(clear)
        self.assertIn("du=2026-02-01", clear.group(1))
        self.assertNotIn("vente=Mule", clear.group(1))


class SaleDocumentsWindowTests(TestCase):
    """« Factures de vente » - narrowed BEFORE the « 50 most recent » slice.

    Narrowed after it, a month with more than fifty documents above the
    window hides every document inside it, and the card reads « aucune
    facture » on a period that has one.
    """

    def setUp(self):
        self.inside = SaleDocument.objects.create(sold_on=date(2026, 2, 14), reference="F-INSIDE")
        for day in range(1, 32):
            SaleDocument.objects.create(sold_on=date(2026, 3, day), reference=f"F-MARS-{day}")
        for day in range(1, 31):
            SaleDocument.objects.create(sold_on=date(2026, 4, day), reference=f"F-AVRIL-{day}")

    def test_the_window_reaches_past_the_slice(self):
        response = self.client.get(reverse("recipes:sales_list"), WINDOW)
        self.assertEqual([doc.reference for doc in response.context["documents"]], ["F-INSIDE"])
        self.assertEqual(response.context["documents_found"], 1)

    def test_without_a_window_the_slice_still_holds(self):
        response = self.client.get(reverse("recipes:sales_list"))
        self.assertEqual(len(response.context["documents"]), 50)
        self.assertEqual(response.context["documents_found"], 62)

    def test_the_boundary_days_are_included(self):
        SaleDocument.objects.create(sold_on=date(2026, 2, 1), reference="F-OUVERTURE")
        SaleDocument.objects.create(sold_on=date(2026, 2, 28), reference="F-CLOTURE")
        SaleDocument.objects.create(sold_on=date(2026, 1, 31), reference="F-VEILLE")
        response = self.client.get(reverse("recipes:sales_list"), WINDOW)
        self.assertEqual(
            sorted(doc.reference for doc in response.context["documents"]),
            ["F-CLOTURE", "F-INSIDE", "F-OUVERTURE"],
        )

    def test_an_empty_window_says_so(self):
        page = self.client.get(reverse("recipes:sales_list"), {"du": "2026-06-01"}).content.decode()
        self.assertIn("Aucune facture de vente sur cette période", page)


class SalesByOriginWindowTests(TestCase):
    """« Par origine » - the same window as the list below it."""

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        sale(self.mule, date(2026, 2, 10), quantity=4, source="laddition")
        sale(self.mule, date(2026, 2, 11), quantity=6, source="manual")
        sale(self.mule, date(2026, 3, 10), quantity=50, source="laddition")
        sale(self.mule, date(2026, 1, 10), quantity=7, source="manual")

    def totals(self, **params):
        response = self.client.get(reverse("recipes:sales_list"), params)
        return {row["source"]: (row["rows"], row["units"]) for row in response.context["totals"]}

    def test_totals_are_the_windows_own(self):
        self.assertEqual(self.totals(**WINDOW), {"laddition": (1, 4), "manual": (1, 6)})

    def test_without_a_window_they_are_everything(self):
        self.assertEqual(self.totals(), {"laddition": (2, 54), "manual": (2, 13)})

    def test_an_empty_window_leaves_the_card_saying_so(self):
        page = self.client.get(reverse("recipes:sales_list"), {"du": "2026-09-01"}).content.decode()
        self.assertIn("Par origine", page)
        self.assertIn("Aucune vente sur cette période", page)

    def test_the_search_does_not_narrow_the_totals_and_the_card_says_so(self):
        """The one figure on this tab that does NOT follow the list below it.

        The search box belongs to « Dernières ventes »; these totals are the
        period's, whatever is typed in it. Left unsaid, « 2 lignes / 54
        unités » sitting above a list of one row reads as one of the two
        being wrong, and there is no way to tell which.
        """
        response = self.client.get(reverse("recipes:sales_list"), {"vente": "Mule", **WINDOW})
        self.assertEqual(len(response.context["sales"]), 2)
        self.assertEqual(
            {row["source"]: row["units"] for row in response.context["totals"]},
            {"laddition": 4, "manual": 6},
        )
        page = response.content.decode()
        self.assertIn("ne s'applique qu'à la liste", page)

    def test_it_says_nothing_about_a_search_nobody_typed(self):
        page = self.client.get(reverse("recipes:sales_list"), WINDOW).content.decode()
        self.assertNotIn("ne s'applique qu'à la liste", page)


class ImportCardIsNotTheWindowTests(TestCase):
    """The import card's « Du … au … » is the period to FETCH from the till.

    Two pairs of dates on one page is exactly how somebody ends up fetching
    three years of sales by accident, so they are checked apart: the window
    must never reach the import card's fields, whatever is asked of it.
    """

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        sale(self.mule, date(2026, 2, 10))
        self.latest = sale(self.mule, date(2026, 8, 20))

    def test_the_import_keeps_its_thirty_day_default_under_a_window(self):
        response = self.client.get(reverse("recipes:sales_list"), WINDOW)
        today = timezone.localdate()
        self.assertEqual(response.context["default_start"], (today - timedelta(days=30)).isoformat())
        self.assertEqual(response.context["default_end"], today.isoformat())
        page = response.content.decode()
        self.assertIn(f'name="start_date" value="{(today - timedelta(days=30)).isoformat()}"', page)
        self.assertIn(f'name="end_date" value="{today.isoformat()}"', page)

    def test_the_last_sale_recorded_is_not_the_windows(self):
        # "Dernière vente enregistrée" says how far the till import has got.
        # Windowed, it would say the import stopped in February.
        response = self.client.get(reverse("recipes:sales_list"), WINDOW)
        self.assertEqual(response.context["last_sale"], self.latest)


class ActionsKeepTheWindowTests(TestCase):
    """What every button drawn on this tab does to the reader's period.

    A window is typed once and clicked away: narrowed to February, one
    « Supprimer » on a hand-typed sale used to bring the page back with all
    8 099 sales in it - nothing saying the period had been dropped, and the
    obvious reading being that the dates do not work. Every action drawn on
    the tab therefore carries the period and comes back to it, and so does
    the sale-document page it sends a reader to.
    """

    def setUp(self):
        self.mule = make_recipe(name="Mule")
        self.typed = sale(self.mule, date(2026, 2, 10), source=MANUAL_SALE_SOURCE)
        self.document = SaleDocument.objects.create(sold_on=date(2026, 2, 14), reference="F-2")

    def assertKeepsTheWindow(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertIn("du=2026-02-01", response["Location"])
        self.assertIn("au=2026-02-28", response["Location"])

    def document_payload(self):
        return {
            "sold_on": "2026-02-14",
            "reference": "F-2",
            "note": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-source": f"recipe:{self.mule.pk}",
            "lines-0-quantity": "2",
            "lines-0-unit_price_ttc": "",
        }

    def test_deleting_a_hand_typed_sale_comes_back_to_the_window(self):
        url = reverse("recipes:sales_delete", kwargs={"pk": self.typed.pk})
        self.assertKeepsTheWindow(self.client.post(f"{url}?{WINDOW_QUERY}"))

    def test_deleting_a_sale_document_comes_back_to_the_window(self):
        url = reverse("recipes:sale_document_delete", kwargs={"pk": self.document.pk})
        self.assertKeepsTheWindow(self.client.post(f"{url}?{WINDOW_QUERY}"))

    def test_adding_a_sale_by_hand_comes_back_to_the_window(self):
        response = self.client.post(
            f"{reverse('recipes:sales_list')}?{WINDOW_QUERY}",
            {"recipe": self.mule.pk, "sold_on": "2026-02-20", "quantity": "3"},
        )
        self.assertKeepsTheWindow(response)

    def test_a_refused_sale_is_redrawn_with_the_window_still_on(self):
        response = self.client.post(
            f"{reverse('recipes:sales_list')}?{WINDOW_QUERY}",
            {"recipe": "", "sold_on": "", "quantity": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["date_window"])
        self.assertEqual([row.sold_on for row in response.context["sales"]], [date(2026, 2, 10)])

    def test_saving_a_sale_document_comes_back_to_the_window(self):
        url = reverse("recipes:sale_document_update", kwargs={"pk": self.document.pk})
        self.assertKeepsTheWindow(self.client.post(f"{url}?{WINDOW_QUERY}", self.document_payload()))

    def test_an_import_comes_back_to_the_window_whether_it_starts_or_not(self):
        url = reverse("recipes:trigger_sales_import")
        with patch("recipes.views.threading.Thread"):
            started = self.client.post(
                f"{url}?{WINDOW_QUERY}", {"start_date": "2026-02-01", "end_date": "2026-02-05"}
            )
        self.assertKeepsTheWindow(started)
        refused = self.client.post(f"{url}?{WINDOW_QUERY}", {"start_date": "", "end_date": ""})
        self.assertKeepsTheWindow(refused)

    def test_the_row_actions_carry_the_window(self):
        page = self.client.get(reverse("recipes:sales_list"), WINDOW).content.decode()
        for name, pk in (
            ("recipes:sales_delete", self.typed.pk),
            ("recipes:sale_document_delete", self.document.pk),
        ):
            target = re.escape(reverse(name, kwargs={"pk": pk}))
            action = re.search(rf'action="({target}[^"]*)"', page)
            self.assertIsNotNone(action, name)
            self.assertIn("du=2026-02-01", action.group(1), name)

    def test_the_sale_document_links_carry_the_window(self):
        page = self.client.get(reverse("recipes:sales_list"), WINDOW).content.decode()
        for name, kwargs in (
            ("recipes:sale_document_create", {}),
            ("recipes:sale_document_update", {"pk": self.document.pk}),
        ):
            target = re.escape(reverse(name, kwargs=kwargs))
            href = re.search(rf'href="({target}[^"]*)"', page)
            self.assertIsNotNone(href, name)
            self.assertIn("du=2026-02-01", href.group(1), name)

    def test_the_manual_sale_form_posts_back_to_the_window(self):
        page = self.client.get(reverse("recipes:sales_list"), WINDOW).content.decode()
        action = re.search(rf'action="({re.escape(reverse("recipes:sales_list"))}[^"]*)" class="inline-form manual-sale"', page)
        self.assertIsNotNone(action)
        self.assertIn("du=2026-02-01", action.group(1))

    def test_the_sale_document_page_leads_back_to_the_window(self):
        url = reverse("recipes:sale_document_update", kwargs={"pk": self.document.pk})
        page = self.client.get(f"{url}?{WINDOW_QUERY}").content.decode()
        back = re.findall(rf'href="({re.escape(reverse("recipes:sales_list"))}[^"]*)"', page)
        self.assertEqual(len(back), 2, "the breadcrumb and « Annuler » both lead back")
        for href in back:
            self.assertIn("du=2026-02-01", href)

    def test_the_search_and_the_page_the_reader_was_on_come_back_too(self):
        url = reverse("recipes:sales_delete", kwargs={"pk": self.typed.pk})
        response = self.client.post(f"{url}?{WINDOW_QUERY}&vente=Mule&ventes=toutes")
        self.assertIn("vente=Mule", response["Location"])
        self.assertIn("ventes=toutes", response["Location"])

    def test_dates_that_are_no_dates_come_back_without_a_500(self):
        # The address is handed back as it was given, junk included: it is
        # read as a window on arrival, where unreadable is simply no window.
        url = reverse("recipes:sales_delete", kwargs={"pk": self.typed.pk})
        response = self.client.post(f"{url}?du=hier&au=2026-02-30", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["date_window"])

    def test_nothing_else_rides_back_on_the_redirect(self):
        # The address these actions are posted to is whatever is in the bar,
        # so only the four parameters this tab reads are given back - a
        # redirect that echoed the query string whole would carry anything.
        url = reverse("recipes:sales_delete", kwargs={"pk": self.typed.pk})
        response = self.client.post(f"{url}?{WINDOW_QUERY}&next=https://exemple.invalid/")
        self.assertNotIn("exemple.invalid", response["Location"])
        self.assertTrue(response["Location"].startswith(reverse("recipes:sales_list")))


class SalesTabLinkTests(TestCase):
    """The tab a reader is already on keeps their window."""

    def test_the_ventes_tab_carries_the_window(self):
        response = self.client.get(reverse("recipes:sales_list"), WINDOW)
        ventes = next(entry for entry in response.context["tabs"] if entry["key"] == "ventes")
        self.assertIn("du=2026-02-01", ventes["url"])
        self.assertIn("au=2026-02-28", ventes["url"])

    def test_the_other_tabs_are_left_alone(self):
        response = self.client.get(reverse("recipes:recipe_list"), WINDOW)
        for entry in response.context["tabs"]:
            self.assertNotIn("du=", entry["url"])
