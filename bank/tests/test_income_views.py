"""« Entrées d'argent » and what « Banque » and « Dépenses » say about it.

The page is read through a period with a default, exactly as « Dépenses »
is, so what fails here is what fails there: a link or a form dropping the
period, the figures widening with nothing on screen to say why. And three
things of its own:

* **what it recognises is said on the page** - the payout rule, the two
  deposit types - and so is what it could not read, with the command that
  fills it;
* **a credit is named with the same field and the same form as a
  spending**, the message saying it is an entry, the datalist offering what
  was typed on credits only;
* **« Banque » says what each credit is**, and its « Entrées » stat opens
  this page over the same period - a chosen month as its first and last
  day.

Every payee, amount and date below is invented.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from html import unescape
from urllib.parse import urlsplit

from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from bank.tests.test_income import Fixtures, StatementBeforeTheTill, TillBeforeTheStatement, euros
from bank.views import _build_balance_svg
from common import last_twelve_months
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

JUNE_PARAMS = {"du": "2026-06-01", "au": "2026-06-30"}
HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')
#: The comparison's own heading - what the coverage warnings sit above.
COMPARISON = "Ce que la caisse a encaissé, et ce qui est arrivé sur le compte"


def query_of(url: str) -> QueryDict:
    return QueryDict(urlsplit(url).query)


def said(response) -> str:
    """The page as a reader reads it: entities decoded, whitespace folded."""
    return " ".join(unescape(response.content.decode()).split())


def forms_posting_to(html: str, action: str) -> list[dict]:
    """The hidden fields of every form posting to `action`, unescaped the
    way a browser posts an attribute back."""
    found = re.findall(rf'<form[^>]*action="{re.escape(action)}"(.*?)</form>', html, re.S)
    return [{name: unescape(value) for name, value in HIDDEN.findall(form)} for form in found]


def window_form(html: str) -> str:
    start = html.index('class="date-range"')
    return html[start : html.index("</form>", start)]


class Page(Fixtures):
    """June: two card payouts paying the till's card days, a cash deposit, a
    cheque deposit, a private party paying an event and a transfer nobody
    has named - and May and July around them."""

    def setUp(self):
        super().setUp()
        self.url = reverse("bank:income_home")
        for day, card in ((date(2026, 6, 1), "120.00"), (date(2026, 6, 2), "80.00"), (date(2026, 6, 4), "60.00")):
            self.paid(day, CB=(card, 3), Cash=("15.00", 1))
            self.sold(day, str(Decimal(card) + Decimal("15.00")))
        self.first = self.payout(date(2026, 6, 3), "200.00", "198.60")
        self.second = self.payout(date(2026, 6, 5), "60.00", "59.58")
        self.cash = self.credit(date(2026, 6, 10), "40.00", "VERSEMENT ESPECES", "VERSEMENT ESPECES")
        self.cheque = self.credit(date(2026, 6, 11), "150.00", "REMISE CHEQUES", "REMISE CHEQUES")
        self.party = self.credit(date(2026, 6, 20), "900.00", counterparty="ASSOCIATION EXEMPLE", category="Privatisation")
        self.unnamed = self.credit(date(2026, 6, 22), "35.00", counterparty="PAYEUR INVENTE")
        self.credit(date(2026, 5, 20), "11.00")
        self.credit(date(2026, 7, 20), "13.00")
        rent = self.debit(date(2026, 6, 5), "700.00")
        rent.category = "Loyer inventé"
        rent.save()

    def page(self, **parameters):
        response = self.client.get(self.url, parameters)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"Entrées d'argent {parameters}")
        return response


class IncomePageTests(Page, TestCase):
    def test_the_page_draws_every_part(self):
        response = self.page(**JUNE_PARAMS)
        report = response.context["report"]
        self.assertEqual((report.received_total, report.received_count), (euros("1383.18"), 6))
        for text in (
            "Entrées d'argent",
            "Ce que la caisse a encaissé, et ce qui est arrivé sur le compte",
            "Ventes carte pas encore versées",
            "Versements carte",
            "Mois par mois",
            "Autres entrées",
            "Juin 2026",
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)
        self.assertContains(response, 'data-chart="line"')
        self.assertContains(response, 'data-table-label="versements carte"')

    def test_what_the_page_recognises_is_said_on_it(self):
        response = self.page(**JUNE_PARAMS)
        self.assertContains(response, "TOTAL ENCAISSE &lt;montant&gt; EUROS")
        self.assertContains(response, "VERSEMENT ESPECES")
        self.assertContains(response, "REMISE CHEQUES")

    def test_an_exact_run_is_worded_as_two_equal_amounts(self):
        response = self.page(**JUNE_PARAMS)
        # 120 + 80 on the 1st and 2nd are the 200 of the 3rd, to the cent.
        self.assertContains(response, "même montant au centime que les ventes carte du 01/06/2026 au 02/06/2026")
        self.assertEqual(
            [row.run for row in response.context["report"].payouts],
            [(date(2026, 6, 1), date(2026, 6, 2)), (date(2026, 6, 4), date(2026, 6, 4))],
        )

    def test_the_anchor_and_the_headline_are_said(self):
        response = self.page(**JUNE_PARAMS)
        self.assertContains(response, "compté à partir du 01/06/2026")
        self.assertContains(response, "Pas encore versé au 05/06/2026")

    def test_no_card_payment_read_is_no_balance_and_the_page_says_why(self):
        from recipes.models import PosDailyPayment

        PosDailyPayment.objects.all().delete()
        response = self.page(**JUNE_PARAMS)
        self.assertContains(response, "Pas de solde")
        self.assertContains(response, "manage.py laddition_backfill_payments")
        self.assertNotContains(response, 'data-chart="line"')

    def test_what_the_till_could_not_read_is_said_with_the_command_that_fills_it(self):
        self.sold(date(2026, 6, 6), "50.00")
        self.sold(date(2026, 6, 7), "0", read=False)
        response = self.page(**JUNE_PARAMS)
        self.assertContains(response, "aucun moyen de paiement lu")
        self.assertContains(response, "manage.py laddition_backfill_payments")
        self.assertContains(response, "manage.py laddition_backfill_revenue")

    def test_the_default_period_is_the_last_twelve_months_and_says_so(self):
        response = self.page()
        self.assertEqual(response.context["window"], last_twelve_months())
        self.assertTrue(response.context["is_default"])
        self.assertContains(response, "Les douze derniers mois")

    def test_everything_under_tout(self):
        response = self.page(tout="1", **JUNE_PARAMS)
        self.assertFalse(response.context["window"])
        self.assertEqual(response.context["report"].received_count, 8)
        self.assertContains(response, 'name="du" value="2026-06-01" disabled')
        period = query_of(response.context["period_url"])
        self.assertEqual((period["du"], period.get("tout")), ("2026-06-01", None))

    def test_a_date_that_is_no_date_is_no_500(self):
        self.page(du="hier", au="2026-02-30")

    def test_the_card_caveat_is_said_once(self):
        """The Carte row's note says it; the paragraph under the table said
        it again, word for word, one line further down."""
        self.assertEqual(said(self.page(**JUNE_PARAMS)).count("passent d'un côté ou de l'autre"), 1)

    def test_the_ecart_is_said_to_be_taken_over_the_days_both_sides_cover(self):
        self.assertIn("L'écart est pris sur les jours que les deux côtés couvrent", said(self.page(**JUNE_PARAMS)))

    def test_what_answers_the_question_comes_before_the_long_list_of_payouts(self):
        """« Mois par mois » - does what came in match what was sold - and
        « Autres entrées », the only part with work to do, above the one
        table that grows by a row a day: at the foot of a year of payouts,
        both were thousands of pixels down the page."""
        html = self.page(**JUNE_PARAMS).content.decode()
        order = [
            html.index(marker)
            for marker in (f"<h2>{COMPARISON}</h2>", 'id="solde"', "<h2>Mois par mois</h2>", 'id="autres-entrees"', 'id="versements"')
        ]
        self.assertEqual(order, sorted(order))

    def test_the_payouts_are_listed_newest_first(self):
        """The headline « Pas encore versé au … » is about the latest
        payout: it is the first row, not the last of a year of them."""
        html = self.page(**JUNE_PARAMS).content.decode()
        table = html[html.index('data-table-label="versements carte"') :]
        table = table[: table.index("</table>")]
        self.assertEqual(re.findall(r'<tr>\s*<td data-sort="([\d-]+)">', table), ["2026-06-05", "2026-06-03"])

    def test_every_window_a_query_string_can_carry_on_a_populated_page(self):
        """The smoke test's windows (tests/test_views_smoke.py), here over
        payouts, a balance and a chart - its own fixture has none of them."""
        from tests.test_views_smoke import DateWindowSmokeTests

        for name, query in {**DateWindowSmokeTests.WINDOWS, "june": "du=2026-06-01&au=2026-06-30"}.items():
            with self.subTest(window=name):
                response = self.client.get(f"{self.url}?{query}")
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, query)
                self.assertEqual(self.client.get(f"{self.url}?{query}&tout=1").status_code, 200)


class TheWindowTravelsTests(Page, TestCase):
    def test_every_link_of_the_page_carries_the_period(self):
        response = self.page(**JUNE_PARAMS)
        for name in ("page_url", "all_url", "period_url"):
            with self.subTest(link=name):
                self.assertEqual(query_of(response.context[name])["du"], "2026-06-01")
                self.assertEqual(query_of(response.context[name])["au"], "2026-06-30")
        self.assertEqual(query_of(response.context["all_url"])["tout"], "1")
        self.assertNotIn("tout", query_of(response.context["period_url"]))
        # « Effacer » clears the dates, and the default period comes back.
        self.assertEqual(response.context["clear_url"], reverse("bank:income_home"))

    def test_the_date_form_holds_the_dates_and_nothing_stale(self):
        form = window_form(self.page(**JUNE_PARAMS).content.decode())
        self.assertIn('name="du" value="2026-06-01"', form)
        self.assertNotIn('type="hidden"', form)

    def test_the_other_pages_are_opened_over_the_same_period(self):
        response = self.page(**JUNE_PARAMS)
        for name in ("spending_url", "margins_url", "bank_url"):
            with self.subTest(link=name):
                query = query_of(response.context[name])
                self.assertEqual((query["du"], query["au"]), ("2026-06-01", "2026-06-30"))
        self.assertEqual(query_of(response.context["bank_url"])["vue"], "entrees")
        self.assertContains(response, response.context["spending_url"].replace("&", "&amp;"))

    def test_under_tout_the_other_pages_are_opened_on_everything_too(self):
        """Bare, they would open on THEIR default - a year - while this page
        says « tout l'historique »."""
        response = self.page(tout="1")
        self.assertEqual(query_of(response.context["spending_url"])["tout"], "1")
        self.assertEqual(query_of(response.context["margins_url"])["tout"], "1")

    def test_each_category_form_comes_back_to_this_page_and_period(self):
        html = self.page(**JUNE_PARAMS).content.decode()
        forms = forms_posting_to(html, reverse("bank:bank_line_action", args=[self.unnamed.pk]))
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["action"], "category")
        self.assertTrue(forms[0]["next"].endswith("#autres-entrees"))
        self.assertEqual(query_of(forms[0]["next"].split("#")[0])["du"], "2026-06-01")


class NamingACreditTests(Page, TestCase):
    def post(self, line, value, **parameters):
        back = f"{reverse('bank:income_home')}?du=2026-06-01&au=2026-06-30#autres-entrees"
        return self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]),
            {"action": "category", "categorie": value, "next": back},
            follow=True,
        )

    def test_a_credit_is_named_and_the_message_says_it_is_an_entry(self):
        response = self.post(self.unnamed, "  Apport  ")
        self.unnamed.refresh_from_db()
        self.assertEqual(self.unnamed.category, "Apport")
        self.assertContains(response, "Entrée classée en « Apport ».")
        self.assertNotContains(response, "Dépense classée")
        # Back on the same page, over the same period.
        self.assertEqual(response.context["window"].start, date(2026, 6, 1))
        self.assertEqual(response.context["report"].unnamed_others, 0)

    def test_a_name_taken_off_says_what_the_entry_counts_as(self):
        response = self.post(self.party, "")
        self.party.refresh_from_db()
        self.assertEqual(self.party.category, "")
        self.assertContains(response, "Catégorie retirée : cette entrée compte comme « Sans catégorie ».")

    def test_naming_a_credit_settles_nothing(self):
        self.post(self.unnamed, "Apport")
        self.unnamed.refresh_from_db()
        self.assertFalse(self.unnamed.settled_by_hand)

    def test_the_datalist_offers_what_was_typed_on_credits_and_never_a_spending(self):
        response = self.page(**JUNE_PARAMS)
        self.assertContains(response, '<option value="Privatisation">')
        self.assertNotContains(response, '<option value="Loyer inventé">')

    def test_a_category_cell_prints_its_value_as_text_beside_the_input(self):
        html = self.page(**JUNE_PARAMS).content.decode()
        self.assertIn('<td data-sort="Privatisation">', html)
        self.assertIn("« Privatisation »", html)
        self.assertIn('<td data-sort="Sans catégorie">', html)


class BanqueTests(Page, TestCase):
    def banque(self, **parameters):
        response = self.client.get(reverse("bank:bank_home"), parameters)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"Banque {parameters}")
        return response

    def test_the_entrees_stat_counts_the_windows_credits(self):
        stats = self.banque(**JUNE_PARAMS).context["stats"]
        self.assertEqual((stats["income_count"], stats["income_total"]), (6, euros("1383.18")))
        self.assertEqual(stats["counts"]["entrees"], 6)

    def test_it_opens_entrees_dargent_over_the_same_dates(self):
        response = self.banque(vue="entrees", **JUNE_PARAMS)
        url = response.context["income_url"]
        self.assertEqual(urlsplit(url).path, reverse("bank:income_home"))
        self.assertEqual((query_of(url)["du"], query_of(url)["au"]), ("2026-06-01", "2026-06-30"))
        self.assertContains(response, f'href="{url.replace("&", "&amp;")}">Entrées d\'argent</a>')
        self.assertContains(response, f'href="{url.replace("&", "&amp;")}">face aux ventes</a>')

    def test_a_chosen_month_becomes_its_first_and_last_day(self):
        query = query_of(self.banque(mois="2026-06").context["income_url"])
        self.assertEqual((query["du"], query["au"]), ("2026-06-01", "2026-06-30"))
        self.credit(date(2024, 2, 10), "5.00")
        query = query_of(self.banque(mois="2024-02").context["income_url"])
        self.assertEqual((query["du"], query["au"]), ("2024-02-01", "2024-02-29"))

    def test_every_month_is_everything(self):
        query = query_of(self.banque().context["income_url"])
        self.assertEqual((query.get("du"), query["tout"]), (None, "1"))

    def test_it_opens_depenses_over_its_period_too(self):
        """« Dépenses par catégorie », beside « Entrées d'argent » in the
        header, was a bare link: Dépenses then opened on its own default, a
        year, whatever month Banque was showing."""
        for parameters, expected in (
            ({"mois": "2026-06"}, {"du": "2026-06-01", "au": "2026-06-30"}),
            (JUNE_PARAMS, {"du": "2026-06-01", "au": "2026-06-30"}),
            ({}, {"tout": "1"}),
        ):
            with self.subTest(parameters=parameters):
                response = self.banque(**parameters)
                url = response.context["spending_url"]
                self.assertEqual(urlsplit(url).path, reverse("bank:spending_home"))
                self.assertEqual(dict(query_of(url).items()), expected)
                self.assertContains(response, f'href="{url.replace("&", "&amp;")}">Dépenses par catégorie</a>')

    def test_each_credit_says_what_it_is(self):
        # Unescaped, as a reader sees it: a name is a variable, and its
        # apostrophe is written « &#x27; ».
        html = unescape(" ".join(self.banque(vue="entrees", **JUNE_PARAMS).content.decode().split()))
        self.assertIn("Versement carte : 200.00 € encaissés, 1.40 € de commission", html)
        self.assertIn("Dépôt d'espèces", html)
        self.assertIn("Remise de chèques", html)
        self.assertIn("<span>Privatisation</span>", html)
        self.assertIn("Autre entrée, sans catégorie", html)
        self.assertIn("à classer sur « Entrées d'argent »", html)
        self.assertNotIn(">Entrée d'argent<", html)

    def test_the_entries_are_read_without_a_query_per_row(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as few:
            self.banque(vue="entrees", **JUNE_PARAMS)
        for offset in range(5):
            self.payout(date(2026, 6, 25), "10.00", "9.90")
        with self.assertNumQueries(len(few.captured_queries)):
            self.banque(vue="entrees", **JUNE_PARAMS)


class DepensesLinksHereTests(Page, TestCase):
    def test_the_link_exists_now_and_carries_the_period(self):
        response = self.client.get(reverse("bank:spending_home"), JUNE_PARAMS)
        url = response.context["income_url"]
        self.assertEqual(urlsplit(url).path, reverse("bank:income_home"))
        self.assertEqual(query_of(url)["du"], "2026-06-01")
        self.assertContains(response, f'href="{url.replace("&", "&amp;")}">Entrées d\'argent</a>')

    def test_under_tout_it_opens_everything(self):
        response = self.client.get(reverse("bank:spending_home"), {"tout": "1"})
        self.assertEqual(query_of(response.context["income_url"])["tout"], "1")
        self.assertEqual(query_of(response.context["margins_url"])["tout"], "1")


class CoveragePage:
    def page(self, **parameters):
        response = self.client.get(reverse("bank:income_home"), parameters)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"Entrées d'argent {parameters}")
        return response


class WhereTheStatementStartsTests(CoveragePage, TillBeforeTheStatement, TestCase):
    """The till read since January, the statement since June
    (test_income.py::TillBeforeTheStatement): the page says where each side
    starts, what the statement cannot see, and offers the days both cover.
    Banque's « Entrées » stat opens this page on `tout`, so that is the view
    a reader lands on."""

    def test_each_side_is_said_from_its_first_day_to_its_last(self):
        text = said(self.page(tout="1"))
        self.assertIn("moyens de paiement de la caisse lus du 10/01/2026 au 10/06/2026", text)
        self.assertIn("relevé importé du 01/06/2026 au 15/06/2026", text)

    def test_under_tout_the_page_says_what_the_statement_cannot_see(self):
        response = self.page(tout="1")
        text = said(response)
        self.assertIn(
            "Du 10/01/2026 au 10/05/2026, la caisse a encaissé 600.00 € (dont 500.00 € par carte) sur des jours "
            "que le relevé ne couvre pas : il commence le 01/06/2026.",
            text,
        )
        self.assertIn("L'écart ci-dessous ne les compte pas", text)
        self.assertLess(text.index("que le relevé ne couvre pas"), text.index(COMPARISON))
        covered = response.context["covered_url"]
        self.assertEqual(urlsplit(covered).path, reverse("bank:income_home"))
        self.assertEqual(dict(query_of(covered).items()), {"du": "2026-06-01"})
        self.assertContains(response, f'href="{covered}">Comparer sur les jours couverts des deux côtés</a>')

    def test_banque_opens_it_on_that_warning(self):
        banque = self.client.get(reverse("bank:bank_home"))
        self.assertIn("que le relevé ne couvre pas", said(self.client.get(banque.context["income_url"])))

    def test_a_window_straddling_the_start(self):
        response = self.page(du="2026-04-01", au="2026-06-30")
        self.assertIn("Du 10/04/2026 au 10/05/2026, la caisse a encaissé 240.00 € (dont 200.00 € par carte)", said(response))
        self.assertEqual(dict(query_of(response.context["covered_url"]).items()), {"du": "2026-06-01", "au": "2026-06-30"})
        card = {row.label: row for row in response.context["report"].rows}["Carte"]
        self.assertEqual(card.difference, euros("0.00"))

    def test_a_window_from_the_statements_first_day_says_nothing_of_it(self):
        response = self.page(du="2026-06-01", au="2026-06-30")
        self.assertNotIn("que le relevé ne couvre pas", said(response))
        self.assertEqual(response.context["covered_url"], "")
        card = {row.label: row for row in response.context["report"].rows}["Carte"]
        self.assertEqual(card.difference, euros("0.00"))

    def test_a_window_wholly_before_the_statement_offers_no_days_to_compare(self):
        response = self.page(du="2026-03-01", au="2026-05-31")
        self.assertIn("que le relevé ne couvre pas", said(response))
        self.assertEqual(response.context["covered_url"], "")
        self.assertNotContains(response, "Comparer sur les jours couverts des deux côtés")


class WhereTheTillStartsTests(CoveragePage, StatementBeforeTheTill, TestCase):
    """The mirror: the statement read from before the first till day whose
    payments were read (test_income.py::StatementBeforeTheTill)."""

    def test_what_came_in_before_the_till_is_said(self):
        response = self.page(tout="1")
        text = said(response)
        self.assertIn(
            "Du 05/06/2026 au 06/06/2026, 110.00 € sont arrivés sur le compte (versements carte comptés au brut, "
            "espèces et chèques déposés) avant le premier jour où la caisse a des moyens de paiement lus, le "
            "10/06/2026 : l'écart ci-dessous ne les compte pas.",
            text,
        )
        self.assertLess(text.index("sont arrivés sur le compte (versements"), text.index(COMPARISON))
        self.assertEqual(dict(query_of(response.context["covered_url"]).items()), {"du": "2026-06-10"})


class EmptyTests(TestCase):
    def test_nothing_imported(self):
        response = self.client.get(reverse("bank:income_home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aucun relevé importé")
        assertNoUnrenderedTemplateSyntax(self, response, "Entrées d'argent, base vide")


class BalanceChartTests(SimpleTestCase):
    POINTS = [
        (date(2026, 6, 3), Decimal("-12.50")),
        (date(2026, 6, 5), Decimal("40.00")),
        (date(2026, 6, 9), Decimal("25.25")),
    ]

    def test_fewer_than_two_points_draw_nothing(self):
        self.assertEqual(_build_balance_svg([]), "")
        self.assertEqual(_build_balance_svg(self.POINTS[:1]), "")

    def test_a_point_per_reading_with_its_date_and_figure(self):
        html = _build_balance_svg(self.POINTS)
        self.assertIn('data-chart="line"', html)
        self.assertEqual(html.count('class="chart-point"'), 3)
        self.assertIn('data-label="03/06/2026" data-value="-12.50 €"', html)
        self.assertIn("chart-hover-line", html)

    def test_zero_is_drawn_and_labelled_once(self):
        html = _build_balance_svg(self.POINTS)
        self.assertIn('stroke-dasharray="4 3"', html)
        self.assertEqual(html.count(">0.00 €</text>"), 1)
        self.assertIn(">40.00 €</text>", html)
        self.assertIn(">-12.50 €</text>", html)

    def test_all_positive_does_not_print_zero_twice(self):
        html = _build_balance_svg([(date(2026, 6, 3), Decimal("5")), (date(2026, 6, 4), Decimal("9"))])
        self.assertEqual(html.count(">0.00 €</text>"), 1)

    def test_a_label_that_would_print_over_zero_is_left_out(self):
        """A balance a euro under nothing, on a scale of a thousand: the
        bottom's label and zero's sat on the same line."""
        html = _build_balance_svg([(date(2026, 6, 3), Decimal("-1.00")), (date(2026, 6, 4), Decimal("1000.00"))])
        self.assertEqual(html.count(">0.00 €</text>"), 1)
        self.assertIn(">1000.00 €</text>", html)
        self.assertNotIn(">-1.00 €</text>", html)
        # The point itself still says it.
        self.assertIn('data-value="-1.00 €"', html)

    def test_a_flat_line_at_zero_still_draws(self):
        html = _build_balance_svg([(date(2026, 6, 3), Decimal("0")), (date(2026, 6, 4), Decimal("0"))])
        self.assertEqual(html.count('class="chart-point"'), 2)

    def test_the_label_is_escaped(self):
        html = _build_balance_svg(self.POINTS, label='<img src=x onerror=alert(1)>')
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_every_colour_it_uses_is_defined(self):
        import pathlib

        css = (pathlib.Path(__file__).resolve().parents[2] / "static/css/marginmate.css").read_text(encoding="utf-8")
        for name in set(re.findall(r"var\((--[\w-]+)\)", _build_balance_svg(self.POINTS))):
            with self.subTest(variable=name):
                self.assertIn(name + ":", css)
