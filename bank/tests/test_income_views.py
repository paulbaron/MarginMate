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
  day;
* **« En caisse » is tested as the browser sends it**: every credit of the
  window is one row with its id, in one list; each menu is read off the page
  it drew (staff/tests/page_forms.py) and posted with its CSRF token, and
  the answer lands on that row, whichever list it moved to, over the same
  period. A payer retained moves its other credits with it and is listed
  with « Oublier »; a payout's menu is drawn only on the row asked for; what
  the menu does not offer, a debit and a GET write nothing, and never 500.

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
from django.utils import timezone

from bank import income
from bank.models import BankTransaction, IncomePayer
from bank.tests.test_income import (
    MERCHANT,
    TERMINAL,
    TERMINAL_LABEL,
    Fixtures,
    StatementBeforeTheTill,
    TillBeforeTheStatement,
    euros,
)
from bank.views import _build_balance_svg
from common import last_twelve_months
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

JUNE_PARAMS = {"du": "2026-06-01", "au": "2026-06-30"}
#: What separates an amount's thousands on the page (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"
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
    found = re.findall(rf'<form[^>]*action="{re.escape(action)}"(.*?)</form>', html, re.DOTALL)
    return [{name: unescape(value) for name, value in HIDDEN.findall(form)} for form in found]


def window_form(html: str) -> str:
    start = html.index('class="date-range"')
    return html[start : html.index("</form>", start)]


def table_of(html: str, label: str) -> str:
    """The table `data-table-label` names, from its tag to its end."""
    start = html.rindex("<table", 0, html.index(f'data-table-label="{label}"'))
    return html[start : html.index("</table>", start)]


def row_of(html: str, line) -> str:
    """A credit's row, found by the id « En caisse » answers on."""
    start = html.index(f'<tr id="entree-{line.pk}">')
    return html[start : html.index("</tr>", start)]


def text_of(html: str) -> str:
    """Markup as a reader reads it: tags out, entities decoded, whitespace
    folded."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


#: Every value the « En caisse » menu posts, « Automatique » first - the
#: values `BankTransaction.income_source` and `IncomePayer.source` store.
OFFERED = ["", "card", "cash", "cheque", "credit", "voucher", "other"]


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
        self.party = self.credit(
            date(2026, 6, 20), "900.00", counterparty="ASSOCIATION EXEMPLE", category="Privatisation"
        )
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
        # 120 + 80 on the 1st and 2nd are the 200 of the 3rd, to the cent:
        # the days are given under « Ventes carte au même montant ».
        text = said(response)
        table = text[text.index('data-table-label="versements carte"') :]
        table = table[: table.index("</table>")]
        self.assertIn("<th>Ventes carte au même montant</th>", table)
        self.assertIn("du 01/06/2026 au 02/06/2026", table)
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

    def test_everything_under_all_history(self):
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

    def test_the_gap_is_said_to_be_taken_over_the_days_both_sides_cover(self):
        self.assertIn("L'écart est pris sur les jours que les deux côtés couvrent", said(self.page(**JUNE_PARAMS)))

    def test_what_answers_the_question_comes_before_the_long_list_of_payouts(self):
        """« Mois par mois » - does what came in match what was sold - and
        « Autres entrées », the only part with work to do, above the one
        table that grows by a row a day: at the foot of a year of payouts,
        both were thousands of pixels down the page."""
        html = self.page(**JUNE_PARAMS).content.decode()
        order = [
            html.index(marker)
            for marker in (
                f"<h2>{COMPARISON}</h2>",
                'id="card-balance"',
                "<h2>Mois par mois</h2>",
                'id="autres-entrees"',
                'id="versements"',
            )
        ]
        self.assertEqual(order, sorted(order))

    def test_the_payouts_are_listed_newest_first(self):
        """The headline « Pas encore versé au … » is about the latest
        payout: it is the first row, not the last of a year of them."""
        html = self.page(**JUNE_PARAMS).content.decode()
        table = html[html.index('data-table-label="versements carte"') :]
        table = table[: table.index("</table>")]
        # A row carries its id (`entree-<pk>`, where « En caisse » answers):
        # the order is what is pinned, not the row's attributes.
        self.assertEqual(re.findall(r'<tr[^>]*>\s*<td data-sort="([\d-]+)">', table), ["2026-06-05", "2026-06-03"])

    def test_every_credit_of_the_window_is_one_row_with_its_id_in_one_list(self):
        """« En caisse » answers on `#entree-<pk>`, whichever list the line
        then sits in: a credit listed twice, or in none, is a choice that
        lands nowhere - and May's, July's and the rent are no row at all."""
        response = self.page(**JUNE_PARAMS)
        html = response.content.decode()
        june = [self.first, self.second, self.cash, self.cheque, self.party, self.unnamed]
        self.assertEqual(sorted(int(pk) for pk in re.findall(r'id="entree-(\d+)"', html)), sorted(o.pk for o in june))
        report = response.context["report"]
        lists = {
            "versements carte": [row.entry.line.pk for row in report.payouts],
            "autres entrées": [entry.line.pk for entry in report.others],
            "dépôts et autres moyens de paiement": [entry.line.pk for entry in report.other_means],
        }
        self.assertEqual(sorted(pk for pks in lists.values() for pk in pks), sorted(line.pk for line in june))
        self.assertEqual(lists["versements carte"], [self.first.pk, self.second.pk])
        self.assertEqual(sorted(lists["dépôts et autres moyens de paiement"]), sorted([self.cash.pk, self.cheque.pk]))
        # Each row is drawn in its own list's table, and in no other.
        for label, pks in lists.items():
            with self.subTest(table=label):
                self.assertEqual(
                    sorted(int(pk) for pk in re.findall(r'<tr id="entree-(\d+)">', table_of(html, label))), sorted(pks)
                )

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
        # « Opérations » opens Banque on its default view, as from « Dépenses ».
        self.assertEqual(query_of(response.context["bank_url"])["vue"], "a-traiter")
        # « Dépenses » is reached through its tab (bank/_tabs.html).
        self.assertContains(
            response, f'<a href="{response.context["spending_url"].replace("&", "&amp;")}" class="tab">'
        )

    def test_under_all_history_the_other_pages_are_opened_on_everything_too(self):
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


class InTheTill(Page):
    """« En caisse » as a browser sends it: every form read off the page it
    drew (staff/tests/page_forms.py), posted by a client checking the CSRF
    token the way Django checks a browser's - logged in as the tenant's
    owner on its first request (tests/runner.py), as
    returnables/tests/test_views.py does.

    On top of the page's June: the payer of the transfer nobody named is a
    new terminal the rules do not know. It paid the card days of the 21st
    and the 23rd (and once in May, before the till), while the terminal the
    label recognises paid the 25th on the 26th - so, filed under « Autres
    entrées », its two transfers are a step of 80 € in the balance."""

    def setUp(self):
        super().setUp()
        self.browser = self.client_class(enforce_csrf_checks=True)
        self.paid(date(2026, 6, 21), CB=("35.00", 1))
        self.paid(date(2026, 6, 23), CB=("45.00", 1))
        self.paid(date(2026, 6, 25), CB=("50.00", 2))
        self.later = self.credit(date(2026, 6, 24), "45.00", counterparty="PAYEUR INVENTE")
        self.before = self.credit(date(2026, 5, 24), "20.00", counterparty="PAYEUR INVENTE")
        self.third = self.payout(date(2026, 6, 26), "50.00", "49.65")
        self.page_url = f"{self.url}?du=2026-06-01&au=2026-06-30"

    def june(self, **parameters) -> str:
        response = self.browser.get(self.url, {**JUNE_PARAMS, **parameters})
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"Entrées d'argent {parameters}")
        return response.content.decode()

    def menu(self, line, html: str | None = None):
        """The « En caisse » form of `line`'s row."""
        return form_posting_to(html or self.june(), reverse("bank:income_source", args=[line.pk]))

    def send(self, form, **values):
        """`form` submitted with `values` changed, the redirect followed."""
        response = self.browser.post(form.action, as_post(form.submission(values=values)), follow=True)
        self.assertEqual(response.status_code, 200)
        return response

    def crafted(self, line, **changes):
        """What a page tampered with posts to `line`'s address: a real
        menu's token and `next`, with `changes` - None takes a field out."""
        data = as_post(self.menu(self.cash).submission())
        for name, value in changes.items():
            if value is None:
                data.pop(name, None)
            else:
                data[name] = [value]
        return self.browser.post(reverse("bank:income_source", args=[line.pk]), data, follow=True)

    def said_after(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def written(self):
        """Everything « En caisse » writes."""
        return (
            list(BankTransaction.objects.order_by("pk").values_list("pk", "income_source")),
            list(IncomePayer.objects.order_by("key").values_list("key", "source")),
        )

    def listed(self, response) -> tuple[list, list, list]:
        """The line pks of the payouts, the others and the other means."""
        report = response.context["report"]
        return (
            [row.entry.line.pk for row in report.payouts],
            [entry.line.pk for entry in report.others],
            [entry.line.pk for entry in report.other_means],
        )


class TheMenusTests(InTheTill, TestCase):
    def test_each_menu_offers_every_choice_and_says_who_decided(self):
        """Ticked where retaining is the point - a transfer nothing
        recognised - and unticked on a deposit the bank type names: ticked
        there, one change would move every deposit of that payer."""
        html = self.june()
        cases = {
            self.unnamed: (True, "non reconnue"),
            self.later: (True, "non reconnue"),
            self.party: (True, "non reconnue"),
            self.cash: (False, "type d'opération"),
            self.cheque: (False, "type d'opération"),
        }
        for line, (ticked, who) in cases.items():
            with self.subTest(line=line.label):
                form = self.menu(line, html)
                self.assertEqual([value for value, _selected in form.control("en_caisse").options], OFFERED)
                self.assertEqual(form.control("en_caisse").value, "")
                self.assertEqual("checked" in form.control("retenir").attrs, ticked)
                self.assertEqual(form.control("retenir").value, "1")
                self.assertIn("csrfmiddlewaretoken", form.names)
                # Back to the page and its period, on no list of its own:
                # the view adds the row's id.
                self.assertEqual(form.control("next").value, self.page_url)
                self.assertIn(f'<span class="read-as">{who}</span>', unescape(row_of(html, line)))
        # One menu a row of « Autres entrées » and « Dépôts … », none on a
        # payout's row.
        menus = [form for form in forms_of(html) if form.action.startswith(f"{self.url}operations/")]
        self.assertEqual(len(menus), len(cases))

    def test_a_credit_naming_nobody_has_no_box_and_is_never_retained(self):
        """No counterparty, and a label of digits only: no payer key, so no
        « retenir pour ce payeur » - and a crafted « retenir » chooses for
        the line alone."""
        nobody = BankTransaction.objects.create(
            operation_date=date(2026, 6, 15),
            bank_type="VIREMENT",
            label="000123 / 456",
            amount=euros("10.00"),
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint="sans-payeur",
        )
        form = self.menu(nobody)
        self.assertNotIn("retenir", form.names)
        response = self.crafted(nobody, en_caisse="card", retenir="1")
        self.assertEqual(self.said_after(response), ["Entrée comptée comme « Carte », elle seule."])
        nobody.refresh_from_db()
        self.assertEqual(nobody.income_source, income.CARD)
        self.assertFalse(IncomePayer.objects.exists())

    def test_a_post_without_its_token_is_refused_and_writes_nothing(self):
        before = self.written()
        data = as_post(self.menu(self.unnamed).submission(values={"en_caisse": "card"}))
        del data["csrfmiddlewaretoken"]
        self.assertEqual(
            self.browser.post(reverse("bank:income_source", args=[self.unnamed.pk]), data).status_code, 403
        )
        self.assertEqual(self.written(), before)


class RetainingAPayerTests(InTheTill, TestCase):
    def test_card_with_the_box_ticked_moves_the_payer_into_the_payouts(self):
        """The click that teaches the page a new terminal: its transfers
        are payouts, past and future, and the balance counts them."""
        html = self.june()
        report = self.client.get(self.url, JUNE_PARAMS).context["report"]
        self.assertEqual(
            (report.last_payout.entry.line.pk, report.last_payout.pending), (self.third.pk, euros("80.00"))
        )

        response = self.send(self.menu(self.unnamed, html), en_caisse="card")
        # On the row, in the list it moved to, over the same period.
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.unnamed.pk}", 302)])
        self.assertEqual(response.context["window"].start, date(2026, 6, 1))
        self.assertEqual(
            self.said_after(response),
            [
                "« PAYEUR INVENTE » retenu : ses entrées non reconnues comptent comme « Carte », cette entrée et 2 autre(s)."
            ],
        )
        self.assertEqual(self.written()[1], [("PAYEUR INVENTE", income.CARD)])
        # The line follows its payer: nothing of its own.
        self.unnamed.refresh_from_db()
        self.assertEqual(self.unnamed.income_source, "")

        payouts, others, other_means = self.listed(response)
        self.assertEqual(payouts, [self.first.pk, self.second.pk, self.unnamed.pk, self.later.pk, self.third.pk])
        self.assertEqual(others, [self.party.pk])
        self.assertEqual(sorted(other_means), sorted([self.cash.pk, self.cheque.pk]))
        html = response.content.decode()
        for line in (self.unnamed, self.later):
            with self.subTest(line=line.label):
                self.assertIn(f'<tr id="entree-{line.pk}">', table_of(html, "versements carte"))
                self.assertNotIn(f'id="entree-{line.pk}"', table_of(html, "autres entrées"))

        # The balance counts them: the step is gone.
        report = response.context["report"]
        self.assertEqual(
            [report.balance.pending.get(pk) for pk in payouts[2:]], [euros("0.00"), euros("0.00"), euros("0.00")]
        )
        text = text_of(html)
        self.assertIn("Pas encore versé au 26/06/2026 0.00 €", text)

        # Their gross is the amount received and their commission unknown:
        # left out of the commission and of its rate (2.17 € of the 310.00 €
        # printed), counted, and said - on the stat, the comparison, the
        # month and each row.
        self.assertIn(
            "5 versements : 390.00 € encaissés, 2.17 € de commission (0.70 %), dont 2 sans brut imprimé "
            "(commission inconnue)",
            text,
        )
        self.assertIn(
            "Carte 390.00 € 13 390.00 € brut dont 2 au montant reçu 5 0.00 € 2.17 €",
            text_of(table_of(html, "moyens de paiement")),
        )
        self.assertIn("2 versements de la période sans brut imprimé, comptés au montant reçu", text)
        (month,) = report.months
        self.assertEqual(
            (month.payouts_gross, month.commission, month.net), (euros("390.00"), euros("2.17"), euros("387.83"))
        )
        self.assertEqual(
            text_of(row_of(html, self.unnamed)),
            "22/06/2026 35.00 € montant reçu inconnue 35.00 € du 21/06/2026 0.00 € payeur retenu changer",
        )
        self.assertNotIn("inconnue", text_of(row_of(html, self.first)))

    def test_card_with_the_box_unticked_marks_the_line_alone(self):
        response = self.send(self.menu(self.unnamed), en_caisse="card", retenir=False)
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.unnamed.pk}", 302)])
        self.assertEqual(self.said_after(response), ["Entrée comptée comme « Carte », elle seule."])
        self.unnamed.refresh_from_db()
        self.assertEqual(self.unnamed.income_source, income.CARD)
        self.assertFalse(IncomePayer.objects.exists())
        payouts, others, _other_means = self.listed(response)
        self.assertIn(self.unnamed.pk, payouts)
        self.assertEqual(sorted(others), sorted([self.party.pk, self.later.pk]))
        # The other transfer is still missing from the balance.
        self.assertEqual(response.context["report"].last_payout.pending, euros("45.00"))
        html = response.content.decode()
        self.assertIn("choisi pour cette entrée", text_of(row_of(html, self.unnamed)))
        self.assertNotIn('id="payeurs-retenus"', html)

    def test_a_credit_chosen_on_its_own_keeps_its_choice_beside_its_payer(self):
        self.send(self.menu(self.later), en_caisse="voucher", retenir=False)
        response = self.send(self.menu(self.unnamed), en_caisse="card")
        self.assertEqual(
            self.said_after(response),
            [
                (
                    "« PAYEUR INVENTE » retenu : ses entrées non reconnues comptent comme « Carte », cette entrée et 1 autre(s). "
                    "1 entrée(s) de ce payeur gardent le choix fait pour elles seules."
                )
            ],
        )
        payouts, _others, other_means = self.listed(response)
        self.assertIn(self.unnamed.pk, payouts)
        self.assertIn(self.later.pk, other_means)
        html = response.content.decode()
        self.assertIn("choisi pour cette entrée", text_of(row_of(html, self.later)))
        # The payer decides one credit of June, of two in all.
        self.assertIn("PAYEUR INVENTE Carte 1 sur 2 35.00 €", text_of(table_of(html, "payeurs retenus")))

    def test_automatique_with_the_box_ticked_forgets_the_payer(self):
        """Read off the payout's own menu (« changer »): the payer's choice
        is shown chosen, the box ticked - and « Automatique » sent with it
        hands every credit of that payer back to the rules."""
        self.send(self.menu(self.unnamed), en_caisse="card")
        form = self.menu(self.unnamed, self.june(changer=str(self.unnamed.pk)))
        self.assertEqual(form.control("en_caisse").value, income.CARD)
        self.assertIn("checked", form.control("retenir").attrs)
        response = self.send(form, en_caisse="")
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.unnamed.pk}", 302)])
        self.assertEqual(
            self.said_after(response),
            [
                (
                    "« PAYEUR INVENTE » oublié : cette entrée et 2 autre(s) reviennent à la reconnaissance "
                    "automatique. Celle-ci compte comme « Pas une vente »."
                )
            ],
        )
        self.assertFalse(IncomePayer.objects.exists())
        _payouts, others, _other_means = self.listed(response)
        self.assertEqual(sorted(others), sorted([self.party.pk, self.unnamed.pk, self.later.pk]))

    def test_a_payer_with_no_other_credit_starts_with_this_one(self):
        """The party's association paid nothing else: retained, it decides
        this credit alone for now, and the message says so."""
        form = self.menu(self.party)
        self.assertIn("checked", form.control("retenir").attrs)
        response = self.send(form, en_caisse="credit")
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.party.pk}", 302)])
        self.assertEqual(
            self.said_after(response),
            [
                (
                    "« ASSOCIATION EXEMPLE » retenu : ses entrées non reconnues comptent comme « Avoir », à commencer "
                    "par celle-ci."
                )
            ],
        )
        self.assertEqual(self.written()[1], [("ASSOCIATION EXEMPLE", income.CREDIT)])
        _payouts, others, other_means = self.listed(response)
        self.assertIn(self.party.pk, other_means)
        self.assertNotIn(self.party.pk, others)
        self.assertIn("payeur retenu", text_of(row_of(response.content.decode(), self.party)))


class HandingALineBackTests(InTheTill, TestCase):
    """« Automatique », the box left unticked as it is drawn on a line chosen
    alone: the line's own choice goes, and the message says who decides it
    now - its payer retained, else the rules."""

    def test_to_its_payer_retained(self):
        self.send(self.menu(self.later), en_caisse="voucher", retenir=False)
        self.send(self.menu(self.unnamed), en_caisse="card")
        form = self.menu(self.later)
        self.assertEqual(form.control("en_caisse").value, income.VOUCHER)
        self.assertNotIn("checked", form.control("retenir").attrs)
        response = self.send(form, en_caisse="")
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.later.pk}", 302)])
        self.assertEqual(
            self.said_after(response),
            ["Entrée rendue à son payeur retenu « PAYEUR INVENTE » : elle compte comme « Carte »."],
        )
        self.later.refresh_from_db()
        self.assertEqual(self.later.income_source, "")
        self.assertEqual(self.written()[1], [("PAYEUR INVENTE", income.CARD)])
        payouts, _others, other_means = self.listed(response)
        self.assertIn(self.later.pk, payouts)
        self.assertNotIn(self.later.pk, other_means)
        html = response.content.decode()
        self.assertIn(f'<tr id="entree-{self.later.pk}">', table_of(html, "versements carte"))
        self.assertIn("payeur retenu", text_of(row_of(html, self.later)))

    def test_to_the_rules_where_no_payer_is_retained(self):
        response = self.send(self.menu(self.cash), en_caisse="cheque")
        self.assertEqual(self.said_after(response), ["Entrée comptée comme « Chèque », elle seule."])
        form = self.menu(self.cash, response.content.decode())
        self.assertEqual(form.control("en_caisse").value, income.CHEQUE)
        self.assertNotIn("checked", form.control("retenir").attrs)
        response = self.send(form, en_caisse="")
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.cash.pk}", 302)])
        self.assertEqual(
            self.said_after(response),
            ["Entrée rendue à la reconnaissance automatique : elle compte comme « Espèces »."],
        )
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.income_source, "")
        self.assertEqual(self.written()[1], [])
        _payouts, _others, other_means = self.listed(response)
        self.assertIn(self.cash.pk, other_means)
        self.assertIn("type d'opération", text_of(row_of(response.content.decode(), self.cash)))


class TheBarsOwnNameTests(InTheTill, TestCase):
    """The review's scenario (01/10/2026): the provider prints the bar's own
    name as the payee of its payouts, and a transfer from the bar's other
    account arrives under that same name, in « Autres entrées ». « Pas une
    vente » on it, « retenir pour ce payeur » left ticked as drawn, moved
    every payout of the statement out of the card figures while the payer
    came before the rules."""

    def setUp(self):
        super().setUp()
        self.own_account = self.credit(
            date(2026, 6, 15), "500.00", f"VIR SEPA RECU /FRM {MERCHANT} VIREMENT INTERNE", counterparty=MERCHANT
        )

    def card_side(self, response) -> dict:
        """Everything the card says on the page: the figures, the payouts and
        the running balance, as the report holds them and as they are drawn."""
        report = response.context["report"]
        html = response.content.decode()
        return {
            "figures": (
                report.card_count,
                report.card_gross,
                report.card_net,
                report.card_commission,
                report.card_from_amount,
            ),
            "payouts": [(row.entry.line.pk, row.entry.how, row.pending) for row in report.payouts],
            "balance": (report.balance.anchor, report.balance.pending, report.balance_points),
            "comparison": text_of(table_of(html, "moyens de paiement")),
            "months": text_of(table_of(html, "mois")),
            "payouts_table": text_of(table_of(html, "versements carte")),
            # The balance's own section: its headline, its chart and what it says.
            "headline": text_of(html[html.index('id="card-balance"') : html.index("Un versement carte non reconnu")]),
        }

    def test_no_sale_retained_for_the_payouts_payee_leaves_the_card_figures_alone(self):
        self.assertEqual(
            {income.payer_key(line) for line in (self.first, self.second, self.third, self.own_account)}, {MERCHANT}
        )
        response = self.browser.get(self.url, JUNE_PARAMS)
        before = self.card_side(response)
        self.assertEqual(
            before["payouts"],
            [
                (self.first.pk, income.BY_RULE, euros("0.00")),
                (self.second.pk, income.BY_RULE, euros("0.00")),
                (self.third.pk, income.BY_RULE, euros("80.00")),
            ],
        )
        self.assertIn("Pas encore versé au 26/06/2026 80.00 €", before["headline"])

        form = self.menu(self.own_account, response.content.decode())
        # Drawn ticked: nothing recognised the transfer.
        self.assertIn("checked", form.control("retenir").attrs)
        response = self.send(form, en_caisse="other")
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.own_account.pk}", 302)])
        # No other credit of that payer is one it decides: the payouts are the
        # label's.
        self.assertEqual(
            self.said_after(response),
            [
                (
                    "« BAR EXEMPLE » retenu : ses entrées non reconnues comptent comme « Pas une vente », à commencer "
                    "par celle-ci."
                )
            ],
        )
        self.assertEqual(self.written()[1], [(MERCHANT, income.OTHER)])

        # The card figures, the payouts and the balance have not moved.
        self.assertEqual(self.card_side(response), before)
        # The transfer stays in « Autres entrées », its payer deciding it.
        self.own_account.refresh_from_db()
        self.assertEqual(self.own_account.income_source, "")
        payouts, others, _other_means = self.listed(response)
        self.assertEqual(payouts, [self.first.pk, self.second.pk, self.third.pk])
        self.assertIn(self.own_account.pk, others)
        html = response.content.decode()
        self.assertIn(f'<tr id="entree-{self.own_account.pk}">', table_of(html, "autres entrées"))
        self.assertIn("payeur retenu", text_of(row_of(html, self.own_account)))
        self.assertIn("BAR EXEMPLE Pas une vente 1 500.00 € Oublier", text_of(table_of(html, "payeurs retenus")))


class UnknownCommissionTests(Page, TestCase):
    """A card payout printing no gross - a terminal's transfer said to be
    card - has a commission nobody knows: never 0,00 €, which reads as « no
    fee », on the month, the comparison or the stat (review, 01/10/2026)."""

    JULY = {"du": "2026-07-01", "au": "2026-07-31"}

    def marked(self, day, amount):
        """A transfer of a terminal the rules do not know, said to be card."""
        return self.credit(day, amount, TERMINAL_LABEL, counterparty=TERMINAL, income_source=income.CARD)

    def test_a_month_whose_only_payout_printed_no_gross_has_its_commission_unknown(self):
        self.marked(date(2026, 7, 3), "40.00")
        response = self.page(**self.JULY)
        report = response.context["report"]
        self.assertEqual((report.card_count, report.card_commission, report.card_from_amount), (1, None, 1))
        html = response.content.decode()
        self.assertIn(
            "Juillet 2026 0.00 € 0.00 € 40.00 € inconnue 40.00 € 0.00 € 0.00 €", text_of(table_of(html, "mois"))
        )
        comparison = text_of(table_of(html, "moyens de paiement"))
        self.assertIn("Carte 0.00 € 0 40.00 € brut dont 1 au montant reçu 1 40.00 € inconnue", comparison)
        # The foot's commission is the window's: unknown too.
        self.assertTrue(comparison.endswith("Total 0.00 € 53.00 € net 2 inconnue"), comparison)
        text = said(response)
        self.assertIn("1 versement : 40.00 € encaissés, commission inconnue (brut non imprimé)", text)
        self.assertNotIn("€ de commission", text)

    def test_a_month_mixing_both_says_what_its_commission_leaves_out(self):
        """June's two payouts print their gross (1,40 € and 0,42 € kept);
        a third, marked, prints none: the figure is theirs, and says it
        leaves one out."""
        self.marked(date(2026, 6, 25), "25.00")
        response = self.page(**JUNE_PARAMS)
        report = response.context["report"]
        self.assertEqual((report.card_commission, report.card_from_amount), (euros("1.82"), 1))
        html = response.content.decode()
        month = text_of(table_of(html, "mois"))
        self.assertIn(
            "Juin 2026 305.00 € 260.00 € 285.00 € 1.82 € hors 1 au montant reçu 283.18 € 45.00 € 40.00 €", month
        )
        self.assertNotIn("inconnue", month)
        comparison = text_of(table_of(html, "moyens de paiement"))
        self.assertIn(
            "Carte 260.00 € 9 285.00 € brut dont 1 au montant reçu 3 25.00 € 1.82 € hors 1 au montant reçu",
            comparison,
        )
        self.assertNotIn("inconnue", comparison)
        # Folded as a reader reads it, the no-break space between the
        # thousands is a space; the markup holds the no-break one.
        self.assertTrue(comparison.endswith("Total 305.00 € 1 408.18 € net 7 1.82 €"), comparison)
        self.assertIn(f"1{NBSP}408.18 € net", table_of(html, "moyens de paiement"))
        self.assertIn(
            "3 versements : 285.00 € encaissés, 1.82 € de commission (0.70 %), dont 1 sans brut imprimé "
            "(commission inconnue)",
            said(response),
        )


class ForgettingAPayerTests(InTheTill, TestCase):
    def retain(self) -> str:
        """« Carte » on the transfer nobody named, the box left ticked: the
        page drawn after it."""
        return self.send(self.menu(self.unnamed), en_caisse="card").content.decode()

    def test_the_payers_retained_are_listed_under_the_balance_with_oublier(self):
        html = self.june()
        # The reminder is there before any payer is.
        self.assertIn(
            "Un versement carte non reconnu (autre terminal) ? Choisissez « Carte » sur son virement dans Autres "
            "entrées : « retenir pour ce payeur » compte aussi tous ses autres virements.",
            text_of(html),
        )
        self.assertNotIn('id="payeurs-retenus"', html)
        html = self.retain()
        self.assertLess(html.index('id="card-balance"'), html.index('<h3 id="payeurs-retenus">'))
        # Two credits of June follow it, of three in all.
        self.assertIn(
            "Payeur Compte comme Entrées Montant PAYEUR INVENTE Carte 2 sur 3 80.00 € Oublier",
            text_of(table_of(html, "payeurs retenus")),
        )

    def test_oublier_read_off_the_page_puts_its_credits_back(self):
        html = self.retain()
        payer = IncomePayer.objects.get()
        forget = form_posting_to(html, reverse("bank:income_payer_forget", args=[payer.pk]))
        self.assertEqual(forget.control("next").value, self.page_url)
        self.assertIn("Oublier « PAYEUR INVENTE » ?", forget.attrs["data-confirm"])
        response = self.send(forget)
        # The last payer forgotten, its list is no longer drawn: the answer
        # lands on the section that held it.
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#card-balance", 302)])
        self.assertEqual(
            self.said_after(response),
            ["« PAYEUR INVENTE » oublié : 3 entrée(s) reviennent à la reconnaissance automatique."],
        )
        self.assertFalse(IncomePayer.objects.exists())
        payouts, others, _other_means = self.listed(response)
        self.assertEqual(payouts, [self.first.pk, self.second.pk, self.third.pk])
        self.assertEqual(sorted(others), sorted([self.party.pk, self.unnamed.pk, self.later.pk]))
        self.assertNotIn('id="payeurs-retenus"', response.content.decode())
        self.assertEqual(response.context["report"].last_payout.pending, euros("80.00"))

    def test_oublier_twice_or_on_a_payer_nobody_retained_is_said_never_a_404(self):
        html = self.retain()
        forget = form_posting_to(html, reverse("bank:income_payer_forget", args=[IncomePayer.objects.get().pk]))
        self.send(forget)
        for action in (forget.action, reverse("bank:income_payer_forget", args=[999999])):
            with self.subTest(action=action):
                response = self.browser.post(action, as_post(forget.submission()), follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.redirect_chain, [(f"{self.page_url}#card-balance", 302)])
                self.assertEqual(self.said_after(response), ["Ce payeur n'est plus retenu."])

    def test_oublier_with_another_payer_left_answers_on_the_list(self):
        html = self.retain()
        IncomePayer.objects.create(key="EMETTEUR INVENTE", source=income.VOUCHER)
        forget = form_posting_to(
            html, reverse("bank:income_payer_forget", args=[IncomePayer.objects.get(key="PAYEUR INVENTE").pk])
        )
        response = self.send(forget)
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#payeurs-retenus", 302)])
        self.assertIn('<h3 id="payeurs-retenus">', response.content.decode())

    def test_a_get_forgets_nobody(self):
        self.retain()
        payer = IncomePayer.objects.get()
        response = self.browser.get(reverse("bank:income_payer_forget", args=[payer.pk]), {"next": self.page_url})
        self.assertRedirects(response, self.page_url, fetch_redirect_response=False)
        self.assertTrue(IncomePayer.objects.filter(pk=payer.pk).exists())


class ChangingAPayoutTests(InTheTill, TestCase):
    def test_the_payouts_draw_no_menu_but_a_link_to_change_each(self):
        html = self.june()
        payouts = table_of(html, "versements carte")
        self.assertNotIn("<form", payouts)
        links = [unescape(href) for href in re.findall(r'<a class="income-change" href="([^"]+)">changer</a>', payouts)]
        self.assertEqual(len(links), 3)
        # Newest first, each to its own row over the same period.
        for line, link in zip((self.third, self.second, self.first), links, strict=True):
            with self.subTest(line=line.label):
                self.assertEqual(urlsplit(link).path, self.url)
                self.assertEqual(dict(query_of(link).items()), {**JUNE_PARAMS, "changer": str(line.pk)})
                self.assertEqual(urlsplit(link).fragment, f"entree-{line.pk}")
        # The default period has no query of its own to add it to (a payout
        # of today: inside it whenever this runs).
        recent = self.payout(timezone.localdate(), "10.00", "9.93")
        default = table_of(self.browser.get(self.url).content.decode(), "versements carte")
        self.assertIn(f'href="{self.url}?changer={recent.pk}#entree-{recent.pk}"', default)

    def test_the_row_asked_for_draws_its_menu_and_no_other(self):
        html = self.june(changer=str(self.first.pk))
        self.assertEqual(table_of(html, "versements carte").count("<form"), 1)
        form = self.menu(self.first, html)
        self.assertEqual(form.control("en_caisse").value, "")
        # A payout the label recognises: unticked, or « Pas une vente » on
        # one would move every payout of that provider.
        self.assertNotIn("checked", form.control("retenir").attrs)
        # Back to the page itself: not to the menu, nor to an anchor.
        self.assertEqual(form.control("next").value, self.page_url)
        response = self.send(form, en_caisse="other")
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.first.pk}", 302)])
        self.assertEqual(self.said_after(response), ["Entrée comptée comme « Pas une vente », elle seule."])
        payouts, others, _other_means = self.listed(response)
        self.assertEqual(payouts, [self.second.pk, self.third.pk])
        self.assertIn(self.first.pk, others)
        self.assertNotIn("<form", table_of(response.content.decode(), "versements carte"))

    def test_a_changer_that_is_no_payout_s_id_draws_no_menu(self):
        for value in ("abc", "²", "-3", "", "1.5", "9" * 30, "999999", str(self.cash.pk)):
            with self.subTest(changer=value):
                html = self.june(changer=value)
                self.assertNotIn("<form", table_of(html, "versements carte"))
        # The deposit's own menu stays where it always is.
        self.menu(self.cash, html)


class RefusedChoicesTests(InTheTill, TestCase):
    def test_what_the_menu_does_not_offer_writes_nothing_and_says_so(self):
        before = self.written()
        for value in ("bitcoin", "CARD", " card", "Carte", "credit ", None):
            with self.subTest(value=value):
                response = self.crafted(self.unnamed, en_caisse=value, retenir="1")
                self.assertEqual(response.status_code, 200)
                # Back to the page, on no row: nothing moved.
                self.assertEqual(response.redirect_chain, [(self.page_url, 302)])
                self.assertEqual(self.said_after(response), ["Choix inconnu : rien n'a changé."])
        self.assertEqual(self.written(), before)

    def test_a_debit_or_nothing_received_is_never_counted_in_the_till(self):
        rent = BankTransaction.objects.get(amount__lt=0)
        nothing = BankTransaction.objects.create(
            operation_date=date(2026, 6, 16),
            label="VIR SEPA RECU /FRM CLIENT EXEMPLE ZERO",
            counterparty="CLIENT EXEMPLE",
            amount=euros("0.00"),
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint="rien-recu",
        )
        before = self.written()
        for line in (rent, nothing):
            with self.subTest(amount=line.amount):
                response = self.crafted(line, en_caisse="card", retenir="1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.redirect_chain, [(self.page_url, 302)])
                self.assertEqual(self.said_after(response), ["Seule une entrée d'argent se compte en caisse."])
        self.assertEqual(self.written(), before)

    def test_a_get_writes_nothing(self):
        before = self.written()
        response = self.browser.get(
            reverse("bank:income_source", args=[self.unnamed.pk]),
            {"en_caisse": "card", "retenir": "1", "next": self.page_url},
        )
        self.assertRedirects(response, self.page_url, fetch_redirect_response=False)
        self.assertEqual(self.written(), before)

    def test_a_line_that_is_not_there_is_a_404(self):
        self.assertEqual(self.crafted(BankTransaction(pk=999999), en_caisse="card").status_code, 404)

    def test_a_next_leading_anywhere_else_comes_back_to_the_page(self):
        """`common.safe_next`: « abc » was a 500 (audit LB-5)."""
        for target in ("abc", "https://ailleurs.example/", "//ailleurs.example/"):
            with self.subTest(next=target):
                response = self.crafted(self.unnamed, en_caisse="other", next=target)
                self.assertEqual(response.redirect_chain, [(f"{self.url}#entree-{self.unnamed.pk}", 302)])
        # A `next` carrying an anchor of its own still lands on the row.
        response = self.crafted(self.unnamed, en_caisse="other", next=f"{self.page_url}#autres-entrees")
        self.assertEqual(response.redirect_chain, [(f"{self.page_url}#entree-{self.unnamed.pk}", 302)])


class OtherMeansRowsTests(InTheTill, TestCase):
    def test_a_credit_said_to_be_an_avoir_is_compared_on_a_row_of_its_own(self):
        """The till took no « Avoir » this month, and no credit was one: no
        row. Said on the party's deposit, the row is there with it - and the
        deposit is listed with the other means of payment."""
        response = self.client.get(self.url, JUNE_PARAMS)
        self.assertNotIn(income.CREDIT, [row.key for row in response.context["report"].rows])
        response = self.send(self.menu(self.party), en_caisse="credit", retenir=False)
        self.assertEqual(self.said_after(response), ["Entrée comptée comme « Avoir », elle seule."])
        avoir = {row.key: row for row in response.context["report"].rows}[income.CREDIT]
        self.assertEqual((avoir.label, avoir.bank, avoir.bank_count), ("Avoir", euros("900.00"), 1))
        html = response.content.decode()
        comparison = text_of(table_of(html, "moyens de paiement"))
        self.assertIn("Avoir 0.00 € 0 900.00 € 1 900.00 € —", comparison)
        self.assertIn("Avoir : Réglé par un acompte encaissé avant", text_of(html))
        _payouts, _others, other_means = self.listed(response)
        self.assertIn(self.party.pk, other_means)
        self.assertIn(
            "Acompte (avoir en caisse)",
            text_of(row_of(table_of(html, "dépôts et autres moyens de paiement"), self.party)),
        )

    def test_meal_vouchers_the_account_holds_none_of_are_a_dash_never_0(self):
        """The till took meal vouchers; until a credit is said to be their
        refund, the account side is « — »: 0 would make the whole till
        figure an Écart, accusing a transfer still under « Autres entrées »."""
        self.paid(date(2026, 6, 6), TR=("24.00", 2))
        html = self.june()
        self.assertIn("Titres-restaurant 24.00 € 2 — — — —", text_of(table_of(html, "moyens de paiement")))
        response = self.send(self.menu(self.later, html), en_caisse="voucher", retenir=False)
        self.assertIn(
            "Titres-restaurant 24.00 € 2 45.00 € 1 21.00 € —",
            text_of(table_of(response.content.decode(), "moyens de paiement")),
        )


class BankHomeTests(Page, TestCase):
    def bank(self, **parameters):
        response = self.client.get(reverse("bank:bank_home"), parameters)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, f"Banque {parameters}")
        return response

    def test_the_income_stat_counts_the_windows_credits(self):
        stats = self.bank(**JUNE_PARAMS).context["stats"]
        self.assertEqual((stats["income_count"], stats["income_total"]), (6, euros("1383.18")))
        self.assertEqual(stats["counts"]["entrees"], 6)

    def test_it_opens_income_over_the_same_dates(self):
        response = self.bank(vue="entrees", **JUNE_PARAMS)
        url = response.context["income_url"]
        self.assertEqual(urlsplit(url).path, reverse("bank:income_home"))
        self.assertEqual((query_of(url)["du"], query_of(url)["au"]), ("2026-06-01", "2026-06-30"))
        # Banque's tab (bank/_tabs.html), and the stat's own link.
        self.assertContains(response, f'href="{url.replace("&", "&amp;")}" class="tab">Entrées d\'argent</a>')
        self.assertContains(response, f'href="{url.replace("&", "&amp;")}">face aux ventes</a>')

    def test_a_chosen_month_becomes_its_first_and_last_day(self):
        query = query_of(self.bank(mois="2026-06").context["income_url"])
        self.assertEqual((query["du"], query["au"]), ("2026-06-01", "2026-06-30"))
        self.credit(date(2024, 2, 10), "5.00")
        query = query_of(self.bank(mois="2024-02").context["income_url"])
        self.assertEqual((query["du"], query["au"]), ("2024-02-01", "2024-02-29"))

    def test_every_month_is_everything(self):
        query = query_of(self.bank().context["income_url"])
        self.assertEqual((query.get("du"), query["tout"]), (None, "1"))

    def test_it_opens_spending_over_its_period_too(self):
        """« Dépenses par catégorie », Banque's tab beside « Entrées
        d'argent », was a bare link: Dépenses then opened on its own default,
        a year, whatever month Banque was showing."""
        for parameters, expected in (
            ({"mois": "2026-06"}, {"du": "2026-06-01", "au": "2026-06-30"}),
            (JUNE_PARAMS, {"du": "2026-06-01", "au": "2026-06-30"}),
            ({}, {"tout": "1"}),
        ):
            with self.subTest(parameters=parameters):
                response = self.bank(**parameters)
                url = response.context["spending_url"]
                self.assertEqual(urlsplit(url).path, reverse("bank:spending_home"))
                self.assertEqual(dict(query_of(url).items()), expected)
                self.assertContains(
                    response, f'href="{url.replace("&", "&amp;")}" class="tab">Dépenses par catégorie</a>'
                )

    def test_each_credit_says_what_it_is(self):
        # Unescaped, as a reader sees it: a name is a variable, and its
        # apostrophe is written « &#x27; ».
        html = unescape(" ".join(self.bank(vue="entrees", **JUNE_PARAMS).content.decode().split()))
        self.assertIn("Versement carte : 200.00 € encaissés, 1.40 € de commission", html)
        self.assertIn("Dépôt d'espèces", html)
        self.assertIn("Remise de chèques", html)
        self.assertIn("<span>Privatisation</span>", html)
        self.assertIn("Autre entrée, sans catégorie", html)
        self.assertIn("à classer sur « Entrées d'argent »", html)
        self.assertNotIn(">Entrée d'argent<", html)

    def test_a_payout_printing_no_gross_and_who_decided_are_said(self):
        """The transfer nobody named, counted as card through its payer: its
        label prints no gross, so there is no commission to state - never
        « 0.00 € de commission ». Who decided is said where a person did;
        the rules' own readings are stated once, on « Entrées d'argent »."""
        income.set_source(self.unnamed, income.CARD, remember=True)
        income.set_source(self.party, income.CREDIT, remember=False)
        html = unescape(" ".join(self.bank(vue="entrees", **JUNE_PARAMS).content.decode().split()))
        self.assertIn(
            '<span>Versement carte : brut non imprimé, commission inconnue</span> <span class="read-as">payeur '
            "retenu</span>",
            html,
        )
        self.assertIn(
            '<span>Acompte (avoir en caisse)</span> <span class="read-as">choisi pour cette entrée</span>', html
        )
        self.assertIn("Versement carte : 200.00 € encaissés, 1.40 € de commission", html)
        self.assertNotIn("35.00 € encaissés", html)
        for by_rule in ("libellé « TOTAL ENCAISSE »", "type d'opération", "non reconnue"):
            with self.subTest(how=by_rule):
                self.assertNotIn(f'<span class="read-as">{by_rule}</span>', html)

    def test_the_payers_are_read_once_and_only_where_entries_are_shown(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        IncomePayer.objects.create(key=income.payer_key(self.unnamed), source=income.CARD)
        IncomePayer.objects.create(key="PAYEUR SANS ENTREE", source=income.VOUCHER)

        def payer_queries(**parameters) -> int:
            with CaptureQueriesContext(connection) as queries:
                self.bank(**parameters)
            return sum(1 for query in queries.captured_queries if "bank_incomepayer" in query["sql"])

        self.assertEqual(payer_queries(vue="entrees", **JUNE_PARAMS), 1)
        for parameters in (
            {"vue": "a-traiter", **JUNE_PARAMS},
            {"vue": "toutes", **JUNE_PARAMS},
            # The tab with no credit over its dates has nothing to read them for.
            {"vue": "entrees", "du": "2026-08-01", "au": "2026-08-31"},
        ):
            with self.subTest(parameters=parameters):
                self.assertEqual(payer_queries(**parameters), 0)

    def test_the_entries_are_read_without_a_query_per_row(self):
        """Payouts, payers retained and credits chosen one by one: the payers
        are one query (`income.known_payers`), whatever their number, and
        every row is read against them in Python. Both counts hold a payer
        and a chosen credit, so the payers' query is in each."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        IncomePayer.objects.create(key=income.payer_key(self.unnamed), source=income.CARD)
        income.set_source(self.party, income.CREDIT, remember=False)
        with CaptureQueriesContext(connection) as few:
            self.bank(vue="entrees", **JUNE_PARAMS)
        for offset in range(5):
            self.payout(date(2026, 6, 25), "10.00", "9.90")
            terminal = self.credit(date(2026, 6, 26), "12.00", counterparty=f"TERMINAL INVENTE {offset}")
            IncomePayer.objects.create(key=income.payer_key(terminal), source=income.CARD)
            income.set_source(self.credit(date(2026, 6, 27), "8.00"), income.VOUCHER, remember=False)
        with self.assertNumQueries(len(few.captured_queries)):
            response = self.bank(vue="entrees", **JUNE_PARAMS)
        # And every one of them was read: none fell back to the rules.
        html = response.content.decode()
        self.assertEqual(html.count('<span class="read-as">payeur retenu</span>'), 6)
        self.assertEqual(html.count('<span class="read-as">choisi pour cette entrée</span>'), 6)
        self.assertEqual(html.count("brut non imprimé, commission inconnue"), 6)


class SpendingLinksHereTests(Page, TestCase):
    def test_the_link_exists_now_and_carries_the_period(self):
        response = self.client.get(reverse("bank:spending_home"), JUNE_PARAMS)
        url = response.context["income_url"]
        self.assertEqual(urlsplit(url).path, reverse("bank:income_home"))
        self.assertEqual(query_of(url)["du"], "2026-06-01")
        self.assertContains(response, f'href="{url.replace("&", "&amp;")}" class="tab">Entrées d\'argent</a>')

    def test_under_all_history_it_opens_everything(self):
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

    def test_under_all_history_the_page_says_what_the_statement_cannot_see(self):
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

    def test_the_bank_page_opens_it_on_that_warning(self):
        bank = self.client.get(reverse("bank:bank_home"))
        self.assertIn("que le relevé ne couvre pas", said(self.client.get(bank.context["income_url"])))

    def test_a_window_straddling_the_start(self):
        response = self.page(du="2026-04-01", au="2026-06-30")
        self.assertIn(
            "Du 10/04/2026 au 10/05/2026, la caisse a encaissé 240.00 € (dont 200.00 € par carte)", said(response)
        )
        self.assertEqual(
            dict(query_of(response.context["covered_url"]).items()), {"du": "2026-06-01", "au": "2026-06-30"}
        )
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
            # Meal vouchers and « Avoir » are compared too, once a credit is
            # said to be one (income.COMPARED): the sentence names all five.
            "Du 05/06/2026 au 06/06/2026, 110.00 € sont arrivés sur le compte (carte au brut, espèces, chèques, "
            "titres-restaurant, avoirs) avant le premier jour de paiements lus en caisse, le 10/06/2026 : l'écart "
            "ci-dessous ne les compte pas.",
            text,
        )
        self.assertLess(text.index("sont arrivés sur le compte (carte"), text.index(COMPARISON))
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
        self.assertIn(f">1{NBSP}000.00 €</text>", html)
        self.assertNotIn(">-1.00 €</text>", html)
        # The point itself still says it.
        self.assertIn('data-value="-1.00 €"', html)

    def test_a_figure_over_a_thousand_groups_its_thousands(self):
        """The axis's label and the tooltip charts.js reads off `data-value`
        say the figure the way the table under the chart does; the point's
        position is geometry, never grouped."""
        html = _build_balance_svg([(date(2026, 6, 3), Decimal("-5432.10")), (date(2026, 6, 4), Decimal("12345.67"))])
        self.assertIn(f">12{NBSP}345.67 €</text>", html)
        self.assertIn(f">-5{NBSP}432.10 €</text>", html)
        self.assertIn(f'data-label="03/06/2026" data-value="-5{NBSP}432.10 €"', html)
        self.assertIn(f'data-label="04/06/2026" data-value="12{NBSP}345.67 €"', html)
        self.assertNotIn("12345.67", html)
        self.assertNotIn(NBSP, "".join(re.findall(r'(?:cx|cy|data-x|data-y|points)="[^"]*"', html)))

    def test_a_flat_line_at_zero_still_draws(self):
        html = _build_balance_svg([(date(2026, 6, 3), Decimal("0")), (date(2026, 6, 4), Decimal("0"))])
        self.assertEqual(html.count('class="chart-point"'), 2)

    def test_the_label_is_escaped(self):
        html = _build_balance_svg(self.POINTS, label="<img src=x onerror=alert(1)>")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_every_colour_it_uses_is_defined(self):
        import pathlib

        css = (pathlib.Path(__file__).resolve().parents[2] / "static/css/marginmate.css").read_text(encoding="utf-8")
        for name in set(re.findall(r"var\((--[\w-]+)\)", _build_balance_svg(self.POINTS))):
            with self.subTest(variable=name):
                self.assertIn(name + ":", css)
