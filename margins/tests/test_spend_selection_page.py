"""« Marge réelle — sans : … » on the page: what it promises.

The owner wants the real margin without the equipment, or without the
charges - **and the global one still in sight**. So:

* **two figures, never one replacing the other.** « Marge réelle » is the
  same with or without a selection; « Marge réelle — sans : Matériel,
  Charges » appears beside it only when something is left out, naming it in
  words, with « tout remettre »;
* **the breakdown is the selector.** « Ce qui a été facturé » lists the
  charges (by supplier) and every article category (by article), each with
  what it cost and its share, each ticked; unticking and « Recalculer »
  leaves it out. The form is a GET, so what it sends is what a browser
  sends: an unticked box sends nothing at all, and the view reads « left
  out » as « shown on the form and not sent back », never as « not sent »;
* **the revenue never moves**, and the page says why where the question is
  asked;
* **the selection is part of the address**, like the period: every link and
  form of the page carries `sans` as it carries `du`/`au`, and a key the
  page does not know is dropped, never a 500.

Data invented throughout - no real supplier, article or amount.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

from django.db import connection
from django.http import QueryDict
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape
from django.utils.http import urlencode

from margins.computation import CHARGES_KEY, TO_CLASSIFY_KEY, article_key, category_key, supplier_key
from margins.tests.test_page import stat_of, text_of, value_of
from recipes.models import PosProduct, PosProductDailyQuantity
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type, make_supplier

PAGE = "margins:margins_home"
MARCH = {"du": "2026-03-01", "au": "2026-03-31"}
MATERIAL = category_key("Matériel")


def line(invoice, article, total_ht, vat_rate="0.20"):
    product = make_product(supplier=invoice.supplier, stock_type=article)
    return make_invoice_line(invoice=invoice, product=product, total_ht=total_ht, vat_rate=Decimal(vat_rate))


def box(html: str, key: str) -> str:
    """The « Garder » checkbox of `key` - the whole tag, so a test can ask
    whether it is ticked."""
    found = re.search(rf'<input type="checkbox" name="garder" value="{re.escape(escape(key))}"[^>]*>', html)
    return found.group(0) if found else ""


def group_body(html: str, key: str) -> str:
    """The `<tbody>` holding the group `key`: its row and its members'."""
    at = html.index(f'name="montre" value="{escape(key)}"')
    start = html.rindex("<tbody", 0, at)
    return html[start : html.index("</tbody>", at)]


def query_of(url: str) -> QueryDict:
    return QueryDict(urlsplit(url).query)


class SelectionFixture:
    """March: 425,00 € of goods on one invoice, 300,00 € of rent, and the
    till took 1 000,00 € HT (1 200,00 € TTC).

    Matériel 150 (perceuse 100, nappe 50) · Spiritueux 200 · Mobilier de
    salle 40 · no category 10 · à classer 25 · Charges 300 (Bailleur).
    Marge réelle 275,00 € HT, 330,00 € TTC.
    """

    @classmethod
    def setUpTestData(cls):
        goods = make_supplier(name="Grossiste Exemple")
        cls.landlord = make_supplier(name="Bailleur Exemple", expenses_only=True)
        cls.drill = make_stock_type(name="Perceuse sans fil", category="Matériel")
        cls.cloth = make_stock_type(name="Nappe en lin", category="Matériel")
        cls.rum = make_stock_type(name="Rhum ambré", category="Spiritueux")
        cls.stool = make_stock_type(name="Tabouret haut", category="Mobilier de salle")
        cls.syrup = make_stock_type(name="Sirop d'églantier", category="")
        cls.keg = make_stock_type(name="Fût consigné", category="Consignes")

        invoice = make_invoice(supplier=goods, invoice_date=date(2026, 3, 5))
        line(invoice, cls.drill, "100.00")
        line(invoice, cls.cloth, "50.00")
        line(invoice, cls.rum, "200.00")
        line(invoice, cls.stool, "40.00")
        line(invoice, cls.syrup, "10.00")
        line(invoice, None, "25.00")
        line(make_invoice(supplier=cls.landlord, invoice_date=date(2026, 3, 1)), None, "300.00")
        line(make_invoice(supplier=goods, invoice_date=date(2026, 4, 2)), cls.keg, "80.00")

        product = PosProduct.objects.create(name="Pinte du comptoir", category="Bières")
        PosProductDailyQuantity.objects.create(
            product=product,
            sold_on=date(2026, 3, 12),
            quantity=100,
            revenue_ttc=Decimal("1200.00"),
            revenue_ht=Decimal("1000.00"),
            revenue_read=True,
        )

    def get(self, *left_out, **extra):
        params = {**MARCH, **extra}
        if left_out:
            params["sans"] = list(left_out)
        response = self.client.get(reverse(PAGE), params)
        self.assertEqual(response.status_code, 200)
        return response

    def html(self, *left_out, **extra) -> str:
        return self.get(*left_out, **extra).content.decode()


class TwoMarginsTests(SelectionFixture, TestCase):
    def test_with_nothing_left_out_there_is_one_real_margin(self):
        html = self.html()

        self.assertEqual(value_of(stat_of(html, "Marge réelle (HT)")), "275.00 €")
        self.assertNotIn("sans :", html)
        self.assertNotIn("tout remettre", html)

    def test_left_out_a_second_margin_appears_beside_the_global_one(self):
        html = self.html(MATERIAL, CHARGES_KEY)

        self.assertEqual(value_of(stat_of(html, "Marge réelle (HT)")), "275.00 €")
        second = stat_of(html, "Marge réelle — sans : Matériel, Charges")
        self.assertEqual(value_of(second), "725.00 €")
        self.assertIn("72.5 % de l'encaissé", text_of(second))
        self.assertIn("marge TTC 870.00 €", text_of(second))

    def test_it_says_what_it_left_out_and_how_much(self):
        text = text_of(self.html(MATERIAL, CHARGES_KEY))

        self.assertIn("Matériel 150.00 € HT", text)
        self.assertIn("Charges 300.00 € HT", text)
        self.assertIn("450.00 € HT laissés de côté sur 725.00 € facturés", text)

    def test_tout_remettre_is_the_same_period_with_nothing_left_out(self):
        response = self.get(MATERIAL)

        self.assertEqual(response.context["reset_url"], f"{reverse(PAGE)}?{urlencode(MARCH)}")
        self.assertContains(response, escape(response.context["reset_url"]))
        self.assertContains(response, "tout remettre")

    def test_each_thing_left_out_can_be_put_back_on_its_own(self):
        response = self.get(MATERIAL, CHARGES_KEY)

        urls = [row.put_back_url for row in response.context["exclusions"]]
        self.assertEqual(query_of(urls[0]).getlist("sans"), [CHARGES_KEY])
        self.assertEqual(query_of(urls[1]).getlist("sans"), [MATERIAL])
        for url in urls:
            self.assertEqual(query_of(url)["du"], MARCH["du"])
            self.assertContains(response, escape(url))

    def test_the_revenue_does_not_move_and_the_page_says_why(self):
        everything = self.html()
        without = self.html(MATERIAL, CHARGES_KEY, TO_CLASSIFY_KEY)

        self.assertEqual(value_of(stat_of(without, "Encaissé (HT)")), value_of(stat_of(everything, "Encaissé (HT)")))
        self.assertEqual(value_of(stat_of(without, "Encaissé (HT)")), "1000.00 €")
        self.assertIn("on retire une dépense, pas une vente", text_of(everything))

    def test_something_left_out_with_nothing_in_the_window_is_named_as_such(self):
        """The consignes were only bought in April: over March the question
        is still the owner's, and the page answers it rather than hiding it."""
        html = self.html(category_key("Consignes"))

        self.assertEqual(value_of(stat_of(html, "Marge réelle — sans : Consignes")), "275.00 €")
        self.assertIn("Consignes : rien de facturé sur cette période", text_of(html))

    def test_something_bought_and_given_back_is_0_00_not_nothing_invoiced(self):
        """A keg bought and returned in March takes out 0,00 € - but it WAS
        invoiced, and « rien de facturé » under it would be false."""
        invoice = make_invoice(invoice_date=date(2026, 3, 20))
        line(invoice, self.keg, "30.00")
        line(invoice, self.keg, "-30.00")

        text = text_of(self.html(category_key("Consignes")))

        self.assertIn("Consignes 0.00 € HT", text)
        self.assertNotIn("rien de facturé sur cette période", text)

    def test_putting_back_a_category_puts_back_what_its_name_hid(self):
        """Perceuse unticked inside Matériel, Matériel left out too: the label
        says « sans : Matériel, Charges », so « remettre » on Matériel puts
        ALL of Matériel back. It left Perceuse out, and only then named it."""
        drill = article_key(self.drill.pk)
        response = self.get(MATERIAL, drill, CHARGES_KEY)

        rows = {row.exclusion.key: row.put_back_url for row in response.context["exclusions"]}
        self.assertEqual(list(rows), [MATERIAL, CHARGES_KEY])
        self.assertEqual(query_of(rows[MATERIAL]).getlist("sans"), [CHARGES_KEY])
        self.assertEqual(query_of(rows[CHARGES_KEY]).getlist("sans"), [MATERIAL, drill])

    def test_an_article_inside_a_category_left_out_is_not_named_twice(self):
        html = self.html(MATERIAL, article_key(self.drill.pk))

        self.assertTrue(stat_of(html, "Marge réelle — sans : Matériel<"))
        self.assertEqual(value_of(stat_of(html, "Marge réelle — sans : Matériel")), "425.00 €")


class TheBreakdownIsTheSelectorTests(SelectionFixture, TestCase):
    def test_it_lists_what_was_invoiced_with_its_share(self):
        text = text_of(self.html())

        self.assertIn("Ce qui a été facturé", text)
        self.assertIn("Charges 1 fournisseur 300.00 € 41.4 %", text)
        self.assertIn("Matériel 2 articles 150.00 € 20.7 %", text)
        self.assertIn("Perceuse sans fil 100.00 € 13.8 %", text)
        self.assertIn("Total facturé 725.00 € 100 %", text)

    def test_every_box_is_ticked_by_default(self):
        html = self.html()

        for key in (CHARGES_KEY, supplier_key(self.landlord.pk), MATERIAL, article_key(self.drill.pk), TO_CLASSIFY_KEY):
            with self.subTest(key=key):
                self.assertIn(" checked", box(html, key))

    def test_what_is_left_out_is_unticked_and_nothing_else(self):
        html = self.html(MATERIAL, article_key(self.rum.pk))

        self.assertNotIn(" checked", box(html, MATERIAL))
        self.assertNotIn(" checked", box(html, article_key(self.rum.pk)))
        # The articles of a category left out keep their own box: ticking
        # the category back brings them back with it.
        self.assertIn(" checked", box(html, article_key(self.drill.pk)))
        self.assertIn(" checked", box(html, CHARGES_KEY))

    def test_a_category_holding_an_unticked_article_is_drawn_unfolded(self):
        """Folded, the only box unticked in it would be out of sight - and
        the category's own box, still ticked, would say nothing is out."""
        html = self.html(article_key(self.rum.pk))

        self.assertIn('<details class="spend-unfold" open>', group_body(html, category_key("Spiritueux")))
        self.assertIn('<details class="spend-unfold">', group_body(html, MATERIAL))

    def test_it_says_nothing_is_saved_and_where_the_selection_lives(self):
        """Two tables of boxes by article category on one page: this one is a
        view, the panel's is saved. The column says « Garder », not
        « Compter », and the page says the selection lives in the address -
        the menu brings back the margin of everything."""
        html = self.html()
        form = html[html.index('class="spend-selection"') :]
        form = form[: form.index("</form>")]

        self.assertIn(">Garder</th>", form)
        self.assertIn(">Dépense</th>", form)
        self.assertNotIn(">Compter</th>", form)
        self.assertIn("Rien n'est enregistré : la sélection vit dans l'adresse de la page", text_of(form))

    def test_recalculer_is_a_get_form_carrying_the_period(self):
        html = self.html(tout="1")
        form = html[html.index('class="spend-selection"') :]
        form = form[: form.index("</form>")]

        self.assertIn('method="get"', html[html.index('class="spend-selection"') - 40 : html.index('class="spend-selection"')])
        self.assertIn('<input type="hidden" name="du" value="2026-03-01">', form)
        self.assertIn('<input type="hidden" name="au" value="2026-03-31">', form)
        self.assertIn('<input type="hidden" name="tout" value="1">', form)
        self.assertIn("Recalculer", form)


class WhatTheFormSendsTests(SelectionFixture, TestCase):
    """The payload a browser really sends: every row's key under `montre`,
    and under `garder` only the boxes still ticked - an unticked box sends
    nothing at all. The answer is a redirect to the clean address."""

    def submit(self, pairs):
        return self.client.get(f"{reverse(PAGE)}?{urlencode(pairs)}")

    def expected(self, *left_out, **extra):
        return f"{reverse(PAGE)}?{urlencode([*MARCH.items(), *extra.items(), *(('sans', key) for key in left_out)])}"

    def rows(self, unticked=()):
        """What the form holds, in document order, with `unticked` left
        unticked."""
        pairs = []
        for key in (
            CHARGES_KEY,
            supplier_key(self.landlord.pk),
            MATERIAL,
            article_key(self.drill.pk),
            article_key(self.cloth.pk),
            TO_CLASSIFY_KEY,
        ):
            pairs.append(("montre", key))
            if key not in unticked:
                pairs.append(("garder", key))
        return pairs

    def test_unticking_leaves_out_exactly_what_was_unticked(self):
        response = self.submit([*MARCH.items(), *self.rows(unticked=(MATERIAL, article_key(self.cloth.pk)))])

        self.assertRedirects(response, self.expected(MATERIAL, article_key(self.cloth.pk)))

    def test_everything_ticked_back_is_nothing_left_out(self):
        response = self.submit([*MARCH.items(), ("sans", MATERIAL), *self.rows()])

        self.assertRedirects(response, self.expected())

    def test_what_the_form_did_not_show_keeps_its_state(self):
        """Consignes had nothing in March, so no row: left out before, it
        stays out - only a row the form showed can be ticked back."""
        response = self.submit([*MARCH.items(), ("sans", category_key("Consignes")), *self.rows(unticked=(CHARGES_KEY,))])

        self.assertRedirects(response, self.expected(category_key("Consignes"), CHARGES_KEY))

    def test_a_stale_page_posting_an_article_since_gone_is_no_500(self):
        gone = make_stock_type(name="Article supprimé depuis", category="Matériel")
        gone_key = article_key(gone.pk)
        gone.delete()
        pairs = [*MARCH.items(), *self.rows(unticked=(CHARGES_KEY,)), ("montre", gone_key)]

        response = self.submit(pairs)

        self.assertRedirects(response, self.expected(CHARGES_KEY))

    def test_a_ticked_box_the_form_never_showed_changes_nothing(self):
        response = self.submit([*MARCH.items(), *self.rows(unticked=(MATERIAL,)), ("garder", CHARGES_KEY), ("garder", "rien")])

        self.assertRedirects(response, self.expected(MATERIAL))

    def test_recalculer_keeps_the_order_the_keys_were_asked_in(self):
        """« sans : Nappe, Matériel » stays so after a « Recalculer » that
        changed nothing - in table order it read « Matériel, Nappe »."""
        cloth = article_key(self.cloth.pk)
        pairs = [*MARCH.items(), ("sans", cloth), ("sans", MATERIAL), *self.rows(unticked=(MATERIAL, cloth))]

        response = self.submit(pairs)

        # Compared as written: `assertRedirects` sorts the query, and the
        # order is the point.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(query_of(response["Location"]).getlist("sans"), [cloth, MATERIAL])

    def test_what_is_newly_left_out_comes_after_what_already_was(self):
        cloth = article_key(self.cloth.pk)
        pairs = [*MARCH.items(), ("sans", cloth), *self.rows(unticked=(MATERIAL, cloth, CHARGES_KEY))]

        response = self.submit(pairs)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(query_of(response["Location"]).getlist("sans"), [cloth, CHARGES_KEY, MATERIAL])

    def test_depuis_le_debut_survives_recalculer(self):
        response = self.submit([*MARCH.items(), ("tout", "1"), *self.rows(unticked=(TO_CLASSIFY_KEY,))])

        self.assertRedirects(response, self.expected(TO_CLASSIFY_KEY, tout="1"))


class KeysInTheAddressTests(SelectionFixture, TestCase):
    def test_a_category_with_accents_spaces_or_no_name_at_all_round_trips(self):
        keys = [MATERIAL, category_key("Mobilier de salle"), category_key("")]
        response = self.get(*keys)
        html = response.content.decode()

        self.assertEqual(query_of(response.context["here_url"]).getlist("sans"), keys)
        second = stat_of(html, "Marge réelle — sans : Matériel, Mobilier de salle, Catégorie non renseignée")
        self.assertEqual(value_of(second), "475.00 €")
        # The same keys, as the window form and the table give them back.
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(f'<input type="hidden" name="sans" value="{escape(key)}">', html)
                self.assertTrue(box(html, key))

    def test_a_key_the_page_does_not_know_is_ignored(self):
        for key in ("", "rien", "article:abc", "article:²", "article:99999", "fournisseur:-1", "categorie:Inconnue au bataillon"):
            with self.subTest(key=key):
                response = self.get(key)
                self.assertNotContains(response, "sans :")
                self.assertEqual(query_of(response.context["here_url"]).getlist("sans"), [])

    def test_a_garbled_key_beside_a_good_one_leaves_the_good_one(self):
        response = self.get("article:x", CHARGES_KEY)

        self.assertEqual(query_of(response.context["here_url"]).getlist("sans"), [CHARGES_KEY])
        self.assertEqual(value_of(stat_of(response.content.decode(), "Marge réelle — sans : Charges")), "575.00 €")

    def test_every_link_and_form_carries_the_selection_as_it_carries_the_period(self):
        response = self.get(MATERIAL, CHARGES_KEY)
        html = response.content.decode()
        both = [MATERIAL, CHARGES_KEY]

        # `here_url` is what a form answering back to this page puts in its
        # `next`; `period_url` is drawn under « depuis le début » (below).
        for name in ("here_url", "all_url", "period_url"):
            with self.subTest(link=name):
                query = query_of(response.context[name])
                self.assertEqual(query["du"], MARCH["du"])
                self.assertEqual(query["au"], MARCH["au"])
                self.assertEqual(query.getlist("sans"), both)
        self.assertEqual(query_of(response.context["all_url"])["tout"], "1")
        self.assertIn(escape(response.context["all_url"]), html)
        # « Effacer » clears the dates, not the selection.
        self.assertEqual(query_of(response.context["clear_url"]).getlist("sans"), both)
        self.assertNotIn("du", query_of(response.context["clear_url"]))

        window_form = html[html.index('class="date-range"') :]
        window_form = window_form[: window_form.index("</form>")]
        for key in both:
            self.assertIn(f'<input type="hidden" name="sans" value="{escape(key)}">', window_form)

    def test_under_depuis_le_debut_the_links_keep_the_selection_too(self):
        """All of the history holds April's keg too: 1 000 € against 505 €
        of goods once the rent is left out."""
        response = self.get(CHARGES_KEY, tout="1")
        html = response.content.decode()

        self.assertEqual(query_of(response.context["period_url"]).getlist("sans"), [CHARGES_KEY])
        self.assertIn(escape(response.context["period_url"]), html)
        self.assertEqual(query_of(response.context["reset_url"])["tout"], "1")
        self.assertEqual(value_of(stat_of(html, "Marge réelle — sans : Charges")), "495.00 €")


class QueryCountTests(SelectionFixture, TestCase):
    """The breakdown reads every line of every invoice in the window: one
    prefetch, whatever their number."""

    def test_three_times_the_invoices_and_the_keys_cost_no_more_queries(self):
        keys = [MATERIAL, article_key(self.rum.pk), supplier_key(self.landlord.pk)]
        with CaptureQueriesContext(connection) as small:
            self.get(*keys)

        for index in range(8):
            article = make_stock_type(name=f"Article {index}", category=f"Catégorie {index}")
            invoice = make_invoice(invoice_date=date(2026, 3, 20), reconciliation_adjustment=Decimal("1.00"))
            line(invoice, article, "10.00")
            line(invoice, None, "5.00")
            charge = make_supplier(name=f"Charge {index}", expenses_only=True)
            line(make_invoice(supplier=charge, invoice_date=date(2026, 3, 21)), None, "12.00")
            keys.append(category_key(f"Catégorie {index}"))
            keys.append(supplier_key(charge.pk))
        with CaptureQueriesContext(connection) as large:
            self.get(*keys)

        self.assertEqual(len(large), len(small), "une requête par facture ou par ligne s'est glissée dans la page")
