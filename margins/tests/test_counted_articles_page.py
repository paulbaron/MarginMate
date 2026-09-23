"""« Articles comptés dans la marge produits » on the Marges page, and
`POST /marges/articles/`: what they promise.

The owner ticked « compter dans la marge produits » on each article's own
form and came back to Marges to see what it did - « to avoid going back and
forth », the box is now on the page, a whole category at a time. So:

* **every category, its state in words** (« aucun », « 1 sur 3 », « tous »),
  **« Tout cocher » / « Tout décocher »**, and unfolding, each article with
  its box and what was bought of it over the period - what ticking it adds;
* **a post only ever touches the articles its own form showed.** The
  whole-category buttons touch that category. The article boxes post the
  category, every article the form SHOWED, the ones still ticked - an
  unticked box sends nothing at all - and the ones it DREW ticked, so only
  a box the person changed changes: « unticked » is « drawn ticked, of this
  category, and not sent back », and nothing else. An article of another
  category, one reclassified since the page was drawn, or one another tab
  changed since, is left exactly as it is;
* **an id that is no id, an article gone since, a category nobody carries
  any more is a message, never a 500**;
* **it answers where it was asked**: back on the page with the period AND
  the real margin's selection (`sans`) kept, through a `next` checked like
  every other one, landing on the panel with the message saying what
  changed right there;
* **the products margin moves by what the boxes said**, and an article a
  recipe uses is marked « compté deux fois » where its tick is.

Data invented throughout - no real supplier, article or amount.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from html import unescape
from urllib.parse import urlsplit

from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import connection
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from inventory.models import MovementKind, StockType
from margins.computation import CHARGES_KEY, category_key
from margins.tests.test_page import stat_of, text_of, value_of
from recipes.models import PosProduct, PosProductDailyQuantity
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_type,
    make_supplier,
)

PAGE = "margins:margins_home"
POST = "margins:count_articles"
MARCH = {"du": "2026-03-01", "au": "2026-03-31"}
PANEL = "articles-comptes"


def bought(article, day, total_ht, vat_rate="0.20"):
    invoice = make_invoice(invoice_date=day)
    product = make_product(supplier=invoice.supplier, stock_type=article)
    line = make_invoice_line(
        invoice=invoice, product=product, quantity=1, total_ht=total_ht, vat_rate=Decimal(vat_rate)
    )
    return make_movement(
        stock_type=article,
        quantity="1",
        unit_cost_ht=total_ht,
        invoice_line=line,
        kind=MovementKind.PURCHASE,
        occurred_on=day,
    )


def ticked(*articles) -> list[bool]:
    return [StockType.objects.get(pk=article.pk).count_in_products_margin for article in articles]


def panel_of(html: str) -> str:
    """The panel alone - from its heading to the next section's."""
    start = html.index(f'id="{PANEL}"')
    return html[start : html.index("<h", html.index("</table>", start))]


def category_body(html: str, name: str) -> str:
    """The `<tbody>` of one category of the panel: its row and its
    articles'."""
    at = html.index(f'<input type="hidden" name="categorie" value="{escape(name)}">')
    start = html.rindex("<tbody", 0, at)
    return html[start : html.index("</tbody>", at)]


def article_box(html: str, article) -> str:
    found = re.search(rf'<input type="checkbox"[^>]*name="coche" value="{article.pk}"[^>]*>', html)
    return found.group(0) if found else ""


def state_cell_of(html: str, name: str) -> str:
    """The « Cochés » cell of one category, as it reads."""
    found = re.search(r'<td class="flag-state">(.*?)</td>', category_body(html, name), flags=re.S)
    return text_of(found.group(1)) if found else ""


def state_of(html: str, name: str) -> str:
    """The state of one category in words, alone."""
    found = re.search(r'<span class="flag-state-words">(.*?)</span>', category_body(html, name), flags=re.S)
    return text_of(found.group(1)) if found else ""


def said(response) -> list[str]:
    """What the panel says after a post, followed to the page."""
    return [str(message) for message in response.context["panel_messages"]]


def query_of(url: str) -> QueryDict:
    return QueryDict(urlsplit(url).query)


def next_of(html: str) -> str:
    """The `next` the panel's forms post - as the browser reads it."""
    found = re.search(r'<form id="compter-\d+"[^>]*>.*?name="next" value="([^"]*)"', html, flags=re.S)
    return unescape(found.group(1))


class PanelFixture:
    """March: a till selling 1 000,00 € HT of punch costed 1,00 € a glass,
    and three categories of articles bought.

    Matériel - Perceuse 100,00 · Nappe 50,00 · Tabouret (never bought)
    Consommables - Essuie-tout 30,00 (ticked) · Gobelets 12,50
    Mobilier de salle - Banquette 40,00
    (blank) - Sirop d'églantier 10,00
    Spiritueux - Rhum ambré 200,00, the punch's own rum.
    """

    @classmethod
    def setUpTestData(cls):
        cls.drill = make_stock_type(name="Perceuse sans fil", category="Matériel")
        cls.cloth = make_stock_type(name="Nappe en lin", category="Matériel")
        cls.stool = make_stock_type(name="Tabouret haut", category="Matériel")
        cls.towels = make_stock_type(name="Essuie-tout", category="Consommables", count_in_products_margin=True)
        cls.cups = make_stock_type(name="Gobelets", category="Consommables")
        cls.bench = make_stock_type(name="Banquette", category="Mobilier de salle")
        cls.syrup = make_stock_type(name="Sirop d'églantier", category="")
        cls.rum = make_stock_type(name="Rhum ambré", category="Spiritueux")

        bought(cls.drill, date(2026, 3, 3), "100.00")
        bought(cls.cloth, date(2026, 3, 4), "50.00")
        bought(cls.towels, date(2026, 3, 5), "30.00")
        bought(cls.cups, date(2026, 3, 6), "12.50")
        bought(cls.bench, date(2026, 3, 7), "40.00")
        bought(cls.syrup, date(2026, 3, 8), "10.00")
        bought(cls.rum, date(2026, 3, 9), "200.00")  # one at 200,00 €

        punch = make_recipe(name="Punch du comptoir", selling_price_ttc="12.00", vat_rate="0.20")
        make_ingredient(punch, stock_type=cls.rum, quantity="0.005")  # 1,00 € HT le verre
        pos = PosProduct.objects.create(name="Punch du comptoir", recipe=punch, category="Cocktails")
        PosProductDailyQuantity.objects.create(
            product=pos,
            sold_on=date(2026, 3, 15),
            quantity=100,
            revenue_ttc=Decimal("1200.00"),
            revenue_ht=Decimal("1000.00"),
            revenue_read=True,
        )

    def page(self, **params):
        response = self.client.get(reverse(PAGE), {**MARCH, **params})
        self.assertEqual(response.status_code, 200)
        return response

    def html(self, **params) -> str:
        return self.page(**params).content.decode()

    def post(self, data, follow=False):
        return self.client.post(reverse(POST), data, follow=follow)

    def save(self, category, shown, kept, drawn_ticked=None, **extra):
        """What a category's « Enregistrer » really sends: the category,
        every article the form showed, the boxes still ticked, and the ones
        the page DREW ticked (`etait`). An unticked box sends nothing at all.

        `drawn_ticked` defaults to the page drawn just now - the shown
        articles ticked in the database as the post is made; a stale page
        passes what it drew."""
        if drawn_ticked is None:
            drawn_ticked = [article for article in shown if ticked(article) == [True]]
        return self.post(
            {
                "categorie": category,
                "affiche": [str(article.pk) for article in shown],
                "coche": [str(article.pk) for article in kept],
                "etait": [str(article.pk) for article in drawn_ticked],
                "action": "enregistrer",
                **extra,
            }
        )


class ThePanelTests(PanelFixture, TestCase):
    def test_it_sits_under_the_products_margin(self):
        html = self.html()

        self.assertLess(html.index("<h2>Marge produits</h2>"), html.index(f'id="{PANEL}"'))
        self.assertLess(html.index(f'id="{PANEL}"'), html.index("<h2>Marges par catégorie</h2>"))
        self.assertIn("Articles comptés dans la marge produits", text_of(panel_of(html)))

    def test_every_category_says_its_state_in_words(self):
        StockType.objects.filter(pk=self.bench.pk).update(count_in_products_margin=True)
        html = self.html()

        self.assertEqual(state_of(html, "Matériel"), "aucun")
        self.assertEqual(state_of(html, "Consommables"), "1 sur 2")
        self.assertEqual(state_of(html, "Mobilier de salle"), "tous")
        self.assertEqual(state_of(html, ""), "aucun")
        self.assertIn("Catégorie non renseignée", text_of(category_body(html, "")))

    def test_a_category_says_how_many_of_its_articles_a_recipe_uses(self):
        """Before « Tout cocher » on the spirits: ticked, every one of them
        would be paid for twice."""
        html = self.html()

        self.assertEqual(state_of(html, "Spiritueux"), "aucun")
        self.assertEqual(state_cell_of(html, "Spiritueux"), "aucun 1 sert dans une recette")
        self.assertEqual(state_cell_of(html, "Matériel"), "aucun")

    def test_each_category_offers_to_tick_or_untick_the_whole_of_it(self):
        body = category_body(self.html(), "Consommables")

        self.assertIn('value="cocher"', body)
        self.assertIn('value="decocher"', body)
        self.assertIn("Tout cocher", body)
        self.assertIn("Tout décocher", body)

    def test_a_button_that_would_change_nothing_is_drawn_disabled(self):
        materiel = category_body(self.html(), "Matériel")

        self.assertRegex(materiel, r'<button[^>]*value="decocher"[^>]*disabled')
        self.assertNotRegex(materiel, r'<button[^>]*value="cocher"[^>]*disabled')

    def test_it_says_beside_the_buttons_that_a_newcomer_arrives_unticked(self):
        self.assertIn("arrivera décoché", text_of(panel_of(self.html())))

    def test_each_article_has_its_box_and_what_ticking_it_would_add(self):
        html = self.html()
        materiel = category_body(html, "Matériel")

        self.assertIn(" checked", article_box(html, self.towels))
        self.assertNotIn(" checked", article_box(html, self.cups))
        self.assertIn("Perceuse sans fil", text_of(materiel))
        self.assertIn("100.00 €", text_of(materiel))
        # Never bought over the period: nothing to add, and said so.
        self.assertIn("rien acheté", text_of(materiel))

    def test_it_says_where_it_asks_that_this_is_the_article_s_own_box(self):
        text = text_of(panel_of(self.html()))

        self.assertIn("fiche de l'article", text)
        self.assertIn("compté deux fois", text)

    def test_with_nothing_ticked_the_page_points_to_the_panel(self):
        StockType.objects.update(count_in_products_margin=False)

        text = text_of(self.html())

        self.assertIn("Aucun article coché", text)
        self.assertIn("ci-dessous", text)


class TickingTheWholeCategoryTests(PanelFixture, TestCase):
    def test_tout_cocher_ticks_every_article_of_that_category_and_nothing_else(self):
        response = self.post({"categorie": "Matériel", "action": "cocher", "next": reverse(PAGE)})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticked(self.drill, self.cloth, self.stool), [True, True, True])
        self.assertEqual(ticked(self.towels, self.cups, self.bench, self.syrup), [True, False, False, False])

    def test_tout_decocher_unticks_every_article_of_that_category_and_nothing_else(self):
        StockType.objects.update(count_in_products_margin=True)

        self.post({"categorie": "Consommables", "action": "decocher"})

        self.assertEqual(ticked(self.towels, self.cups), [False, False])
        self.assertEqual(ticked(self.drill, self.bench, self.syrup), [True, True, True])

    def test_the_message_says_what_changed(self):
        response = self.post({"categorie": "Matériel", "action": "cocher"}, follow=True)

        self.assertEqual(said(response), ["3 articles de Matériel comptés dans la marge produits."])

    def test_ticking_what_is_already_ticked_says_nothing_changed(self):
        StockType.objects.filter(category="Matériel").update(count_in_products_margin=True)

        response = self.post({"categorie": "Matériel", "action": "cocher"}, follow=True)

        self.assertEqual(said(response), ["Matériel : rien n'a changé (tous comptés dans la marge produits)."])

    def test_it_ticks_what_the_category_holds_today(self):
        """Classified into Matériel after the page was drawn: « Tout
        cocher » is about the category, and takes it along."""
        ladder = make_stock_type(name="Escabeau", category="Matériel")

        self.post({"categorie": "Matériel", "action": "cocher"})

        self.assertEqual(ticked(ladder), [True])

    def test_the_blank_category_is_a_category_too(self):
        self.post({"categorie": "", "action": "cocher"})

        self.assertEqual(ticked(self.syrup), [True])
        self.assertEqual(ticked(self.drill, self.cups, self.bench), [False, False, False])

    def test_a_category_with_accents_and_spaces_round_trips_through_the_form(self):
        html = self.html()
        body = category_body(html, "Mobilier de salle")
        name = unescape(re.search(r'name="categorie" value="([^"]*)"', body).group(1))

        self.post({"categorie": name, "action": "cocher"})

        self.assertEqual(ticked(self.bench), [True])

    def test_ticking_an_article_a_recipe_uses_warns_that_it_is_now_counted_twice(self):
        """« Tout cocher » ticks what the category holds, recipes or not -
        and says at once what that did."""
        response = self.post({"categorie": "Spiritueux", "action": "cocher"}, follow=True)

        self.assertEqual(ticked(self.rum), [True])
        self.assertEqual(
            said(response),
            [
                "1 article de Spiritueux compté dans la marge produits.",
                "Compté deux fois désormais : Rhum ambré sert dans une recette, qui compte déjà ce qu'elle "
                "en consomme. Décochez-le, ou retirez-le de la recette.",
            ],
        )


class WhatTheBoughtFigureCountsTests(PanelFixture, TestCase):
    def test_it_says_the_bought_figure_leaves_the_invoice_s_duty_out(self):
        """The same article reads 40,00 € here and 45,00 € in « Ce qui a été
        facturé » on an invoice carrying 5,00 € of duty: the products margin
        counts the line's own amount, the breakdown adds the line's share of
        the duty. One page, two figures - said where the second one is."""
        text = text_of(panel_of(self.html()))

        self.assertIn(
            "le montant des lignes elles-mêmes, sans la part des droits de la facture que « Ce qui a été "
            "facturé » leur ajoute",
            text,
        )


class TheArticleBoxesTests(PanelFixture, TestCase):
    """What « Enregistrer » really sends: an unticked box sends NOTHING, so
    « unticked » is read as « shown on the form, of this category, and not
    sent back ticked »."""

    def test_the_boxes_left_ticked_are_ticked_and_the_ones_absent_are_not(self):
        StockType.objects.filter(pk=self.stool.pk).update(count_in_products_margin=True)

        self.save("Matériel", shown=[self.drill, self.cloth, self.stool], kept=[self.drill, self.cloth])

        self.assertEqual(ticked(self.drill, self.cloth, self.stool), [True, True, False])

    def test_an_article_of_another_category_is_left_exactly_as_it_was(self):
        """Essuie-tout is ticked and in Consommables: a post about Matériel
        leaves it ticked, and Gobelets unticked, even named in the post."""
        self.save(
            "Matériel",
            shown=[self.drill, self.cloth, self.stool, self.towels, self.cups],
            kept=[self.drill, self.cups],
        )

        self.assertEqual(ticked(self.drill, self.cloth, self.stool), [True, False, False])
        self.assertEqual(ticked(self.towels, self.cups), [True, False])

    def test_an_article_reclassified_since_the_page_was_drawn_is_left_as_it_was(self):
        """The Essuie-tout moved into Matériel after Matériel's form was
        drawn without it. The form never showed it: its tick stays."""
        StockType.objects.filter(pk=self.towels.pk).update(category="Matériel")

        self.save("Matériel", shown=[self.drill, self.cloth, self.stool], kept=[self.drill])

        self.assertEqual(ticked(self.towels), [True])
        self.assertEqual(ticked(self.drill, self.cloth), [True, False])

    def test_the_message_says_both_what_was_ticked_and_what_was_unticked(self):
        StockType.objects.filter(pk__in=[self.cloth.pk, self.stool.pk]).update(count_in_products_margin=True)

        response = self.client.post(
            reverse(POST),
            {
                "categorie": "Matériel",
                "affiche": [str(self.drill.pk), str(self.cloth.pk), str(self.stool.pk)],
                "coche": [str(self.drill.pk)],
                "etait": [str(self.cloth.pk), str(self.stool.pk)],
                "action": "enregistrer",
            },
            follow=True,
        )

        self.assertEqual(
            said(response),
            [
                "1 article de Matériel compté dans la marge produits.",
                "2 articles de Matériel retirés de la marge produits.",
            ],
        )

    def test_a_stale_page_naming_an_article_deleted_since_is_a_message(self):
        gone = make_stock_type(name="Article supprimé depuis", category="Matériel")
        gone_pk = gone.pk
        gone.delete()

        response = self.post(
            {
                "categorie": "Matériel",
                "affiche": [str(self.drill.pk), str(gone_pk)],
                "coche": [str(self.drill.pk), str(gone_pk)],
                "action": "enregistrer",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ticked(self.drill), [True])
        self.assertIn(
            "1 case ignorée : l'article n'existe plus, ou n'est plus dans Matériel depuis que la page a été "
            "affichée. Rien n'a été changé pour lui.",
            said(response),
        )

    def test_an_id_that_is_no_id_is_a_message_never_a_500(self):
        response = self.post(
            {
                "categorie": "Matériel",
                "affiche": [str(self.drill.pk), "abc", "²", "", "9" * 40],
                "coche": [str(self.drill.pk), "abc", "-1"],
                "action": "enregistrer",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ticked(self.drill), [True])
        self.assertIn("5 cases illisibles ignorées.", said(response))

    def test_a_ticked_box_the_form_never_showed_changes_nothing(self):
        self.save("Matériel", shown=[self.drill], kept=[self.drill, self.cloth])

        self.assertEqual(ticked(self.drill, self.cloth), [True, False])

    def test_a_box_ticked_in_another_tab_is_not_unticked_by_a_stale_page(self):
        """The page drew Consommables with Gobelets unticked; another tab
        ticked it since. This page ticks nothing but leaves Essuie-tout as it
        was and saves: Gobelets was never touched here, and stays ticked."""
        stale_draw = [self.towels]  # Gobelets drawn unticked
        StockType.objects.filter(pk=self.cups.pk).update(count_in_products_margin=True)  # the other tab

        self.save("Consommables", shown=[self.towels, self.cups], kept=[self.towels], drawn_ticked=stale_draw)

        self.assertEqual(ticked(self.towels, self.cups), [True, True])

    def test_a_box_unticked_in_another_tab_is_not_ticked_again_by_a_stale_page(self):
        """The reverse: drawn ticked here, unticked in another tab since. Left
        ticked on this page, it is not ticked back."""
        stale_draw = [self.towels]
        StockType.objects.filter(pk=self.towels.pk).update(count_in_products_margin=False)  # the other tab

        self.save("Consommables", shown=[self.towels, self.cups], kept=[self.towels], drawn_ticked=stale_draw)

        self.assertEqual(ticked(self.towels, self.cups), [False, False])

    def test_a_stale_page_still_applies_what_was_changed_on_it(self):
        """What the person did change on the stale page is applied: Gobelets
        ticked here while Essuie-tout was unticked in another tab."""
        stale_draw = [self.towels]
        StockType.objects.filter(pk=self.towels.pk).update(count_in_products_margin=False)

        self.save(
            "Consommables", shown=[self.towels, self.cups], kept=[self.towels, self.cups], drawn_ticked=stale_draw
        )

        self.assertEqual(ticked(self.towels, self.cups), [False, True])

    def test_the_page_says_which_boxes_it_drew_ticked(self):
        """The `etait` the view reads comes from the page itself: one per box
        drawn ticked, on that box's own form, and none for a box drawn
        unticked."""
        body = category_body(self.html(), "Consommables")
        form = re.search(r'<form id="(compter-\d+)"', body).group(1)

        self.assertRegex(body, rf'<input type="hidden" form="{form}" name="etait" value="{self.towels.pk}">')
        self.assertNotIn(f'name="etait" value="{self.cups.pk}"', body)

    def test_the_ids_are_the_form_s_own_and_need_not_follow_each_other(self):
        """Articles are created and deleted: the ids a category holds are
        never 1, 2, 3."""
        first = make_stock_type(name="Clé à molette", category="Outils")
        make_stock_type(name="Supprimé entre deux", category="Outils").delete()
        make_stock_type(name="Autre supprimé", category="Outils").delete()
        last = make_stock_type(name="Tournevis", category="Outils")

        self.save("Outils", shown=[last, first], kept=[last])

        self.assertEqual(ticked(first, last), [False, True])


class WhatCannotBeUnderstoodTests(PanelFixture, TestCase):
    def test_an_unknown_category_is_a_message_and_changes_nothing(self):
        response = self.post({"categorie": "Catégorie inventée", "action": "cocher"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            said(response),
            [
                "Aucun article n'est dans la catégorie « Catégorie inventée » : rien n'a été modifié. "
                "Ses articles ont peut-être changé de catégorie depuis que la page a été affichée."
            ],
        )
        self.assertEqual(StockType.objects.filter(count_in_products_margin=True).count(), 1)

    def test_a_post_with_no_category_at_all_is_a_message(self):
        response = self.post({"action": "cocher"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockType.objects.filter(count_in_products_margin=True).count(), 1)
        self.assertEqual(said(response), ["Formulaire incomplet, sans catégorie : rien n'a été modifié."])

    def test_an_unknown_action_is_a_message(self):
        response = self.post({"categorie": "Matériel", "action": "tout-casser"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ticked(self.drill, self.cloth, self.stool), [False, False, False])
        self.assertEqual(said(response), ["Action inconnue : rien n'a été modifié."])

    def test_get_changes_nothing_and_goes_back(self):
        response = self.client.get(reverse(POST), {"categorie": "Matériel", "action": "cocher"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticked(self.drill), [False])


class ItAnswersWhereItWasAskedTests(PanelFixture, TestCase):
    def test_the_redirect_keeps_the_period_the_selection_and_depuis_le_debut(self):
        html = self.html(tout="1", sans=[category_key("Matériel"), CHARGES_KEY])
        back = next_of(html)

        response = self.post({"categorie": "Matériel", "action": "cocher", "next": back})

        location = response["Location"]
        self.assertEqual(location, back)
        self.assertEqual(urlsplit(location).path, reverse(PAGE))
        self.assertEqual(urlsplit(location).fragment, PANEL)
        query = query_of(location)
        self.assertEqual(query["du"], MARCH["du"])
        self.assertEqual(query["au"], MARCH["au"])
        self.assertEqual(query["tout"], "1")
        self.assertEqual(query.getlist("sans"), [category_key("Matériel"), CHARGES_KEY])

    def test_every_form_of_the_panel_posts_that_same_next(self):
        html = self.html(sans=[CHARGES_KEY])
        nexts = {unescape(value) for value in re.findall(r'name="next" value="([^"]*)"', panel_of(html))}

        self.assertEqual(len(nexts), 1)
        self.assertEqual(query_of(nexts.pop()).getlist("sans"), [CHARGES_KEY])

    def test_a_next_pointing_off_site_is_refused(self):
        for target in ("https://example.com/marges/", "//example.com/marges/", "javascript:alert(1)"):
            with self.subTest(next=target):
                response = self.post({"categorie": "Matériel", "action": "cocher", "next": target})

                self.assertEqual(response["Location"], f"{reverse(PAGE)}#{PANEL}")

    def test_get_goes_back_to_a_safe_next_only(self):
        self.assertEqual(
            self.client.get(reverse(POST), {"next": "https://example.com/"})["Location"], f"{reverse(PAGE)}#{PANEL}"
        )
        here = f"{reverse(PAGE)}?du=2026-03-01#{PANEL}"
        self.assertEqual(self.client.get(reverse(POST), {"next": here})["Location"], here)

    def test_the_message_is_said_in_the_panel_and_only_there(self):
        """The redirect lands on the panel: said at the top of the page, the
        answer would be two screens above where the question was asked."""
        response = self.post({"categorie": "Matériel", "action": "cocher", "next": next_of(self.html())}, follow=True)
        html = response.content.decode()
        said = "3 articles de Matériel comptés dans la marge produits"

        self.assertIn(said, text_of(panel_of(html)))
        self.assertEqual(text_of(html).count(said), 1)

    def test_a_message_from_elsewhere_stays_at_the_top(self):
        """The page takes the base template's messages over to put its own in
        the panel: every other one must still be said where it always was."""
        storage = CookieStorage(RequestFactory().get("/"))
        self.client.cookies[CookieStorage.cookie_name] = storage._encode(
            [Message(constants.INFO, "Un message venu du stock")]
        )

        html = self.html()

        self.assertIn("Un message venu du stock", html[: html.index("<h1>Marges</h1>")])
        self.assertEqual(html.count("Un message venu du stock"), 1)


class TheProductsMarginMovesTests(PanelFixture, TestCase):
    def test_it_moves_by_what_was_bought_of_the_articles_ticked(self):
        """Matériel held 150,00 € bought over March: ticked, the products
        margin loses exactly that and the real margin does not move."""
        before = self.html()
        self.assertEqual(value_of(stat_of(before, "Marge produits (HT)")), "870.00 €")
        self.assertIn("150.00 €", text_of(category_body(before, "Matériel")))

        self.post({"categorie": "Matériel", "action": "cocher"})
        after = self.html()

        self.assertEqual(value_of(stat_of(after, "Marge produits (HT)")), "720.00 €")
        self.assertEqual(value_of(stat_of(after, "Achats des articles cochés (HT)")), "180.00 €")
        self.assertEqual(value_of(stat_of(after, "Marge réelle (HT)")), value_of(stat_of(before, "Marge réelle (HT)")))

    def test_unticking_gives_it_back(self):
        self.post({"categorie": "Consommables", "action": "decocher"})

        self.assertEqual(value_of(stat_of(self.html(), "Marge produits (HT)")), "900.00 €")


class CountedTwiceTests(PanelFixture, TestCase):
    def test_a_ticked_article_a_recipe_uses_is_marked_where_its_tick_is(self):
        StockType.objects.filter(pk=self.rum.pk).update(count_in_products_margin=True)
        html = self.html()
        body = category_body(html, "Spiritueux")

        row = next(row for row in re.findall(r"<tr[^>]*>.*?</tr>", body, flags=re.S) if "Rhum ambré" in row)
        self.assertIn(" checked", article_box(row, self.rum))
        self.assertIn("compté deux fois", text_of(row))
        # Drawn unfolded: folded, the mark would be out of sight.
        self.assertIn("<details class=\"spend-unfold\" open>", body)
        # And the warning above the figures still names it.
        self.assertIn("Compté deux fois", text_of(html))

    def test_an_unticked_one_says_ticking_it_would_count_it_twice(self):
        row = next(
            row
            for row in re.findall(r"<tr[^>]*>.*?</tr>", category_body(self.html(), "Spiritueux"), flags=re.S)
            if "Rhum ambré" in row
        )

        self.assertIn("dans une recette", text_of(row))
        self.assertNotIn("compté deux fois", text_of(row))


class QueryCountTests(PanelFixture, TestCase):
    def test_three_times_the_articles_cost_the_page_no_more_queries(self):
        with CaptureQueriesContext(connection) as small:
            self.page()

        supplier = make_supplier(name="Grossiste Exemple")
        for index in range(20):
            article = make_stock_type(name=f"Article {index}", category=f"Catégorie {index % 6}")
            invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 3, 20))
            product = make_product(supplier=supplier, stock_type=article)
            line = make_invoice_line(invoice=invoice, product=product, quantity=1, total_ht="4.00")
            make_movement(
                stock_type=article, quantity="1", unit_cost_ht="4.00", invoice_line=line,
                kind=MovementKind.PURCHASE, occurred_on=date(2026, 3, 20),
            )
            if index % 3 == 0:
                StockType.objects.filter(pk=article.pk).update(count_in_products_margin=True)
        with CaptureQueriesContext(connection) as large:
            self.page()

        self.assertEqual(len(large), len(small), "une requête par article s'est glissée dans le panneau")

    def test_a_post_costs_the_same_queries_whatever_the_category_holds(self):
        with CaptureQueriesContext(connection) as small:
            self.post({"categorie": "Mobilier de salle", "action": "cocher"})

        for index in range(30):
            make_stock_type(name=f"Outil {index}", category="Matériel")
        with CaptureQueriesContext(connection) as large:
            self.post({"categorie": "Matériel", "action": "cocher"})

        self.assertEqual(len(large), len(small), "une requête par article s'est glissée dans l'enregistrement")
