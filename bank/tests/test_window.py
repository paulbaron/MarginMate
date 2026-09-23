"""« Du … au … » on the bank page: what the window narrows, and what carries it.

The failure this guards against is not a 500. It is a page that looks right:
a total counted over every month while the list under it shows one, a tab
saying 12 over a page of 3, or a window that quietly disappears on the next
click - a figure changing with nothing on screen to say why.

So the figures are what is asserted, not the status code: the four stats, the
tab counts, the payee groups, and the window surviving every link and every
round trip the page makes (a tab, « Relancer le rapprochement », an import).
"""

import re
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape
from html import unescape

from bank import reconcile
from bank.models import BankTransaction, IgnoreRule
from bank.tests.test_reconcile import Fixtures, card_row, debit_row, statement
from bank.views import VIEWS
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

#: July 2026, both ends on a day that has an operation: the boundary is the
#: whole point (« au 31 » means the 31st).
JULY = {"du": "2026-07-01", "au": "2026-07-31"}


def transfer_in(day, payer, amount):
    """Money coming in - the « Entrées » tab's rows. card_row and debit_row
    only ever spend, and a window that counted the entrées with the dépenses
    would be wrong in the direction nobody checks."""
    return f"{day:%d/%m/%Y};VIREMENT;VIR RECU;VIR INST RECU /FRM {payer} /REFDO REF;{day:%d/%m/%Y};{amount}"


def _form_fields(response, action):
    """The hidden fields of the first form on the page posting to `action`,
    as the browser would send them - so a test posts what the PAGE says, not
    what the test wishes it said."""
    form = re.search(rf'<form[^>]*action="{re.escape(action)}"(.*?)</form>', response.content.decode(), re.S)
    assert form is not None, f"no form posting to {action} on the page"
    # Unescaped, the way a browser posts an attribute back: the page
    # writes « &amp; » between the parameters and sends « & ».
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(1))
    }


class WindowTests(Fixtures, TestCase):
    def setUp(self):
        self.url = reverse("bank:bank_home")
        self.invoice("METRO", date(2026, 7, 10), "100.00")
        self.load(
            debit_row(date(2026, 6, 30), "VEILLE EXEMPLE", "10,00"),
            debit_row(date(2026, 7, 1), "PREMIER JOUR", "20,00"),
            debit_row(date(2026, 7, 15), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 31), "DERNIER JOUR", "30,00"),
            debit_row(date(2026, 8, 1), "LENDEMAIN EXEMPLE", "40,00"),
            transfer_in(date(2026, 7, 20), "CLIENT EXEMPLE", "500,00"),
            transfer_in(date(2026, 8, 20), "CLIENT EXEMPLE", "600,00"),
        )
        reconcile.reconcile()

    def page(self, **parameters):
        return self.client.get(self.url, {"vue": "toutes", **parameters})

    def payees(self, response):
        return sorted(row.line.counterparty for row in response.context["rows"])

    def test_both_boundary_days_are_in_and_the_rest_is_out(self):
        self.assertEqual(self.payees(self.page(**JULY)), ["DERNIER JOUR", "METRO FRANCE", "PREMIER JOUR"])

    def test_one_end_alone_is_a_window(self):
        self.assertEqual(self.payees(self.page(du="2026-08-01")), ["LENDEMAIN EXEMPLE"])
        self.assertEqual(self.payees(self.page(au="2026-06-30")), ["VEILLE EXEMPLE"])

    def test_the_four_figures_follow_the_window(self):
        stats = self.page(**JULY).context["stats"]
        # 20 + 120 + 30, the 10 of June and the 40 of August left out.
        self.assertEqual((stats["spending_count"], stats["spending_total"]), (3, Decimal("170.00")))
        self.assertEqual((stats["linked_count"], stats["linked_total"]), (1, Decimal("120.00")))
        self.assertEqual((stats["todo_count"], stats["todo_total"]), (2, Decimal("50.00")))
        self.assertEqual((stats["no_invoice_count"], stats["no_invoice_total"]), (0, Decimal("0")))

    def test_without_a_window_everything_is_counted(self):
        stats = self.page().context["stats"]
        self.assertEqual((stats["spending_count"], stats["spending_total"]), (5, Decimal("220.00")))

    def test_the_tab_counts_are_counted_over_the_window_the_list_shows(self):
        counts = {tab["key"]: tab["count"] for tab in self.page(**JULY).context["tabs"]}
        self.assertEqual(counts["toutes"], 3)
        self.assertEqual(counts["a-traiter"], 2)
        self.assertEqual(counts["rapprochees"], 1)
        self.assertEqual(counts["sans-facture"], 0)
        self.assertEqual(counts["entrees"], 1)
        # The bénéficiaires tab counts groups, not lines: the two payments
        # still missing an invoice, each from a payee of its own.
        self.assertEqual(counts["par-beneficiaire"], 2)

    def test_the_payee_groups_follow_the_window(self):
        groups = self.page(vue="par-beneficiaire", **JULY).context["groups"]
        self.assertEqual([(group.name, group.total) for group in groups], [
            ("DERNIER JOUR", Decimal("30.00")),
            ("PREMIER JOUR", Decimal("20.00")),
        ])

    def test_every_tab_link_carries_the_window(self):
        response = self.page(**JULY)
        for tab in response.context["tabs"]:
            with self.subTest(tab=tab["key"]):
                self.assertIn(f"vue={tab['key']}", tab["url"])
                self.assertIn("du=2026-07-01", tab["url"])
                self.assertIn("au=2026-07-31", tab["url"])
        self.assertContains(response, "du=2026-07-01&amp;au=2026-07-31")

    def test_following_a_tab_keeps_the_window(self):
        tabs = {tab["key"]: tab["url"] for tab in self.page(**JULY).context["tabs"]}
        followed = self.client.get(tabs["rapprochees"])
        self.assertEqual(self.payees(followed), ["METRO FRANCE"])
        self.assertEqual(followed.context["stats"]["spending_count"], 3)

    def test_the_page_says_in_words_which_period_it_shows(self):
        self.assertContains(self.page(**JULY), "Du 01/07/2026 au 31/07/2026")
        self.assertContains(self.page(du="2026-07-01"), "Depuis le 01/07/2026")
        self.assertContains(self.page(au="2026-07-31"), "Jusqu'au 31/07/2026")
        self.assertNotContains(self.page(), "date-range-note")

    def test_a_period_with_nothing_in_it_reads_as_empty_dates(self):
        response = self.page(du="2030-01-01", au="2030-12-31")
        self.assertEqual(response.context["stats"]["spending_count"], 0)
        self.assertContains(response, "Aucune opération sur cette période")
        # And never as "no statement imported": that empty state is about an
        # empty database, and would read as a broken page.
        self.assertNotContains(response, "Aucun relevé importé")
        assertNoUnrenderedTemplateSyntax(self, response, "banque, période vide")

    def test_effacer_goes_back_to_the_whole_list_on_the_same_tab(self):
        response = self.page(vue="rapprochees", **JULY)
        self.assertEqual(response.context["clear_window_url"], "/banque/?vue=rapprochees")
        self.assertContains(response, ">Effacer</a>")
        self.assertNotContains(self.page(), ">Effacer</a>")

    def test_an_unreadable_date_is_the_whole_list_not_a_500(self):
        response = self.page(du="n-importe-quoi", au="hier")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["date_window"])
        self.assertEqual(response.context["stats"]["spending_count"], 5)

    def test_a_month_chosen_beats_the_dates(self):
        response = self.page(mois="2026-08", **JULY)
        self.assertEqual(self.payees(response), ["LENDEMAIN EXEMPLE"])
        self.assertFalse(response.context["date_window"])
        # Said and shown: the inputs are given back empty and disabled, and
        # no link carries the dates on, so they cannot come back silently.
        self.assertContains(response, '<input type="date" name="du" value="" disabled>')
        self.assertContains(response, "rend les dates libres")
        # Nothing the page draws carries them on: neither a tab link nor the
        # `next` every form posts (which is why that one is built from what
        # the view read, not from the URL the browser happens to be on).
        self.assertEqual(response.context["page_url"], "/banque/?vue=toutes&mois=2026-08")
        self.assertNotContains(response, "du=2026-07-01")

    def test_a_card_payment_is_windowed_by_the_day_the_bank_booked_it(self):
        """A card is used one day and booked the next, and the two pickers
        have to agree about which period it belongs to: « Mois » filters on
        operation_date, so the window does too. The Date column still shows
        the day the card was used - hence the page saying « dates de passage
        en banque » beside the figures."""
        self.load(card_row(date(2026, 7, 31), "SUPERETTE EXEMPLE 0912", "9,00"))
        self.assertNotIn("SUPERETTE EXEMPLE 0912", self.payees(self.page(**JULY)))
        self.assertIn("SUPERETTE EXEMPLE 0912", self.payees(self.page(du="2026-08-01", au="2026-08-31")))


class WindowRoundTripTests(Fixtures, TestCase):
    """The window has to survive the two POSTs the page makes: both come back
    through `_back(request)` and a `next` the template hands over."""

    def setUp(self):
        self.url = reverse("bank:bank_home")
        self.load(
            debit_row(date(2026, 7, 15), "PREMIER EXEMPLE", "20,00"),
            debit_row(date(2026, 8, 15), "SECOND EXEMPLE", "40,00"),
        )

    def windowed_page(self):
        page = self.client.get(self.url, {"vue": "toutes", **JULY})
        path = page.context["page_url"]
        self.assertEqual(path, "/banque/?vue=toutes&du=2026-07-01&au=2026-07-31")
        # The page really hands that URL over - « Relancer le rapprochement »,
        # the import form and every line action post it as `next`.
        self.assertContains(page, f'name="next" value="{escape(path)}"')
        return path

    def test_the_window_survives_relancer_le_rapprochement(self):
        path = self.windowed_page()
        response = self.client.post(reverse("bank:bank_reconcile"), {"next": path})
        self.assertEqual(response["Location"], path)
        self.assertEqual(self.client.get(response["Location"]).context["stats"]["spending_count"], 1)

    def test_the_window_survives_an_import(self):
        path = self.windowed_page()
        response = self.client.post(
            self.url,
            {
                "next": path,
                "files": [
                    SimpleUploadedFile(
                        "releve.csv", statement(debit_row(date(2026, 7, 20), "NOUVELLE LIGNE", "60,00"))
                    )
                ],
            },
        )
        self.assertEqual(response["Location"], path)
        followed = self.client.get(response["Location"])
        self.assertEqual(followed.context["stats"]["spending_count"], 2)
        self.assertContains(followed, "1 opération(s) importée(s)")

    def test_nothing_chosen_comes_back_to_the_window_too(self):
        path = self.windowed_page()
        response = self.client.post(self.url, {"next": path})
        self.assertEqual(response["Location"], path)
        self.assertContains(self.client.get(response["Location"]), "Choisissez")

    def test_the_window_survives_what_a_person_does_to_a_line(self):
        """« Rattacher », « Pas de facture », « Délier », « Rapprocher
        automatiquement » are the buttons this page is actually used with,
        and only the two POSTs above were covered. A line action dropping the
        window sends the reader back to every month with the figures changed
        and nothing on screen to say why.

        The `next` posted here is the one the page really draws on that form,
        read back out of the HTML: handed the path the test already knows,
        this passes with the hidden field removed from the template - which
        is the bug it is here to catch.
        """
        self.windowed_page()
        line = BankTransaction.objects.get(counterparty="PREMIER EXEMPLE")
        action = reverse("bank:bank_line_action", kwargs={"pk": line.pk})
        posted = _form_fields(self.client.get(self.url, {"vue": "toutes", **JULY}), action)
        response = self.client.post(action, {**posted, "action": "no_invoice"})
        self.assertEqual(response["Location"], "/banque/?vue=toutes&du=2026-07-01&au=2026-07-31")
        followed = self.client.get(response["Location"])
        self.assertEqual(followed.context["stats"]["spending_count"], 1)
        self.assertEqual(followed.context["stats"]["no_invoice_count"], 1)


class WindowChipTests(Fixtures, TestCase):
    """Every figure drawn beside a list is counted over the list's own window.

    The tab counts are asserted against worked-out numbers above; this asks
    the other half of the same question - that each chip equals what its own
    tab actually puts on screen - over a window with something in every tab.
    A count windowed while its list is not (or the reverse) reads as « 12 »
    over a page of 3, which is the shape this class exists to refuse.
    """

    def setUp(self):
        self.url = reverse("bank:bank_home")
        IgnoreRule.objects.create(pattern="URSSAF", description="URSSAF")
        self.invoice("METRO", date(2026, 7, 10), "100.00")
        self.load(
            debit_row(date(2026, 6, 30), "VEILLE EXEMPLE", "10,00"),
            debit_row(date(2026, 7, 1), "PREMIER JOUR", "20,00"),
            debit_row(date(2026, 7, 15), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 20), "URSSAF D ILE", "77,00"),
            debit_row(date(2026, 7, 31), "DERNIER JOUR", "30,00"),
            debit_row(date(2026, 8, 1), "LENDEMAIN EXEMPLE", "40,00"),
            debit_row(date(2026, 8, 2), "URSSAF D ILE", "88,00"),
            transfer_in(date(2026, 7, 20), "CLIENT EXEMPLE", "500,00"),
            transfer_in(date(2026, 8, 20), "CLIENT EXEMPLE", "600,00"),
        )
        reconcile.reconcile()

    def test_each_chip_counts_what_its_own_tab_lists(self):
        for view in VIEWS:
            with self.subTest(vue=view):
                response = self.client.get(self.url, {"vue": view, **JULY})
                chips = {tab["key"]: tab["count"] for tab in response.context["tabs"]}
                # « par bénéficiaire » lists groups, not lines, so the chip
                # beside it counts payees - the one tab where the two differ.
                listed = response.context["groups" if view == "par-beneficiaire" else "rows"]
                self.assertEqual(chips[view], len(listed))

    def test_a_line_a_rule_hides_is_counted_over_the_window_too(self):
        """The « dont N par règle » note under « Pas de facture attendue »:
        one URSSAF debit in July, one in August, and the note is about the
        window, not about every URSSAF payment ever made."""
        stats = self.client.get(self.url, {"vue": "sans-facture", **JULY}).context["stats"]
        self.assertEqual((stats["no_invoice_count"], stats["no_invoice_total"]), (1, Decimal("77.00")))
        self.assertEqual(stats["by_rule_count"], 1)
        unwindowed = self.client.get(self.url, {"vue": "sans-facture"}).context["stats"]
        self.assertEqual((unwindowed["no_invoice_count"], unwindowed["by_rule_count"]), (2, 2))
