"""« Trésorerie » (views.treasury_home and its POST-only routes): the page and
its tabs, the point form read off the page and posted as a browser posts it,
every refusal in French and never a 500, a balance never replaced without
« Remplacer », the adjustment settling a gap and every refusal of it, « Dater
ce point du … », the deletes and the adjustments they leave counting nowhere,
the messages said where the redirect lands, and the statement import's
warning.

Every amount, day and label here is invented."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import unescape
from unittest import mock

from django import forms
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from bank import treasury, views
from bank.forms import NUL_REFUSED, TreasuryAdjustmentForm, TreasuryPointForm
from bank.models import BankTransaction, TreasuryAdjustment, TreasuryCheckpoint
from bank.tests.test_reconcile import debit_row, statement
from bank.treasury import BALANCE_AMBIGUOUS, BALANCE_UNREADABLE, DATE_IMPOSSIBLE, DATE_UNREADABLE
from staff.tests.page_forms import as_post, forms_of
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

#: What separates an amount's thousands on the page (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"
NUL_SAID = "Caractère interdit (NUL) : retapez ce champ."
SEPTEMBER = {"du": "2026-09-01", "au": "2026-09-30"}
#: The page's sections, in the order it draws them.
SECTIONS = ("saisir", "ecarts", "courbe", "points", "ajustements")


def sept(day: int) -> date:
    return date(2026, 9, day)


def text_of(html: str) -> str:
    """Markup as a reader reads it: tags out, entities decoded, whitespace
    folded."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def section(html: str, name: str) -> str:
    """One section of the page, from its id to the next section's (or the
    end of the page's content)."""
    start = html.index(f'id="{name}"')
    later = [html.find(f'id="{other}"', start) for other in SECTIONS[SECTIONS.index(name) + 1 :]]
    ends = [found for found in later if found != -1] or [html.index("</main>")]
    return html[start : min(ends)]


def top_of(html: str) -> str:
    """What the page says above its tabs: the messages said at the top."""
    return html[html.index("<main") : html.index('<nav class="tabs"')]


def point_form_of(html: str):
    """« Saisir un solde »: the one form posting a `solde`."""
    found = [form for form in forms_of(html) if form.method == "post" and "solde" in form.names]
    assert len(found) == 1, f"{len(found)} point forms"
    return found[0]


def forms_to(html: str, target: str, **holding):
    """The POST forms sending to `target`, holding each of `holding`'s
    controls at that value."""
    return [
        form
        for form in forms_of(html)
        if form.method == "post"
        and form.action.split("#")[0] == target
        and all(
            any(control.name == name and control.attrs.get("value", "") == value for control in form.controls)
            for name, value in holding.items()
        )
    ]


class Ledger(TestCase):
    """Lines and points in the database, and the page read as a reader reads
    it. A line is imported « now » unless said: the days before it are
    complete (bank/treasury.py, `complete_through`)."""

    made = 0

    def setUp(self):
        super().setUp()
        #: A client enforcing CSRF, as a browser does - logged in all the same.
        self.browser = self.client_class(enforce_csrf_checks=True)

    def line(self, day, amount, *, account="", imported_at=None):
        self.made += 1
        created = BankTransaction.objects.create(
            operation_date=day,
            label=f"OPERATION EXEMPLE {self.made}",
            amount=Decimal(amount),
            account=account,
            fingerprint=f"tresorerie-vue-{self.made}",
        )
        if imported_at is not None:
            BankTransaction.objects.filter(pk=created.pk).update(imported_at=imported_at)
        return created

    def point(self, day, balance):
        return TreasuryCheckpoint.objects.create(date=day, balance=Decimal(balance))

    def page(self, **params):
        response = self.client.get(reverse("bank:treasury"), params)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "Trésorerie")
        return response

    def html(self, **params) -> str:
        return self.page(**params).content.decode()

    def browse(self, **params) -> str:
        """The page as `self.browser` - a client enforcing CSRF, as a
        browser does - reads it: its forms post with its token."""
        response = self.browser.get(reverse("bank:treasury"), params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def said(self, response) -> list[str]:
        """What the request said - read as a page drawn reads them: dropped
        from the clients' cookies, so the next request says only its own."""
        said = [str(message) for message in get_messages(response.wsgi_request)]
        for client in (self.client, self.browser):
            client.cookies.pop("messages", None)
        return said

    def a_gap(self):
        """01/09: 1 000,00; +300 on the 5th, -100 on the 10th; 15/09:
        1 250,00 - 50,00 more than the operations explain; -30 on the 20th,
        imported since, so the gap is to resolve. The account ends the 20th
        at 1 220,00."""
        self.first = self.point(sept(1), "1000.00")
        self.line(sept(5), "300.00")
        self.line(sept(10), "-100.00")
        self.second = self.point(sept(15), "1250.00")
        self.line(sept(20), "-30.00")

    def read_before(self):
        """01/09: 1 000,00; +100 on the 5th; 15/09: 1 100,00 typed - read
        before that day's -50; -10 on the 20th. The gap is the 15th's
        operations: « Dater ce point du 14/09 »."""
        self.first = self.point(sept(1), "1000.00")
        self.line(sept(5), "100.00")
        self.line(sept(15), "-50.00")
        self.second = self.point(sept(15), "1100.00")
        self.line(sept(20), "-10.00")


class PageTests(Ledger):
    def test_with_no_point_the_page_asks_for_one(self):
        html = self.html()
        self.assertIn("Saisissez le solde du compte à une date : la trésorerie des autres jours en est calculée.", html)
        self.assertNotIn('class="stat-row"', html)
        self.assertIn("Aucun relevé importé", html)
        self.assertIn(f'href="{reverse("bank:bank_home")}?vue=a-traiter&amp;du=', html)
        self.assertIn('aria-current="page">Trésorerie</a>', html)
        self.assertIn('<p class="page-subtitle">Le solde du compte, calculé depuis vos saisies', html)
        self.assertNotIn('id="ecarts"', html)
        self.assertNotIn('id="points"', html)

    def test_the_headline_its_point_and_where_the_statement_stops(self):
        self.a_gap()
        html = self.html()
        self.assertIn('<div class="stat stat-warn">', html)
        self.assertIn("Trésorerie au 20/09/2026", html)
        self.assertIn(f"1{NBSP}220.00 €", html)
        self.assertIn("depuis le solde du 15/09", html)
        self.assertIn("Relevé importé jusqu'au", unescape(html))
        self.assertIn('<span class="stat-value">20/09/2026</span>', html)
        self.assertIn('<a href="#ecarts">1 écart à résoudre</a>', html)
        self.assertNotIn("provisoire", html.lower())

    def test_the_headline_is_amber_only_when_its_own_point_is_unsettled(self):
        """A gap of September does not light a figure carried from a later
        point that agrees."""
        self.a_gap()
        self.point(sept(25), "1220.00")
        self.line(sept(28), "-5.00")
        html = self.html()
        self.assertIn("Trésorerie au 28/09/2026", html)
        self.assertIn("depuis le solde du 25/09", html)
        self.assertNotIn("stat-warn", html)
        self.assertIn('<a href="#ecarts">1 écart à résoudre</a>', html)

    def test_a_provisional_headline_and_curve_say_so(self):
        """The 10th imported on the 10th: that day may be partial, so the
        balance carried to it is « provisoire »."""
        self.point(sept(5), "1000.00")
        self.line(sept(10), "50.00", imported_at=timezone.make_aware(datetime(2026, 9, 10, 11, 0)))
        html = self.html(tout="1")
        self.assertIn("Trésorerie au 10/09/2026", html)
        self.assertIn("depuis le solde du 05/09 · provisoire", html)
        self.assertIn("Provisoire le 10/09 : relevé complet jusqu'au 09/09.", text_of(section(html, "courbe")))
        # Imported since: the day is whole, nothing is provisional.
        BankTransaction.objects.update(imported_at=timezone.now())
        html = self.html(tout="1")
        self.assertNotIn("provisoire", html.lower())

    def test_where_the_statement_stops_and_where_it_is_complete_are_two_words(self):
        """Review C3/C8/C11/C14: a statement exported and imported on the day
        of its last operation - the everyday case - holds part of that day.
        The stat says the last day imported (`last_operation`), the note
        under the curve the last day complete (`complete_through`): one
        phrase for both put two dates under the same words on one page."""
        self.point(sept(5), "1000.00")
        self.line(sept(10), "50.00", imported_at=timezone.make_aware(datetime(2026, 9, 10, 11, 0)))
        page = text_of(self.html(tout="1"))
        self.assertIn("Relevé importé jusqu'au 10/09/2026", page)
        self.assertIn("relevé complet jusqu'au 09/09.", page)
        self.assertNotIn("relevé importé jusqu'au 09/09", page.lower())
        # Nothing imported at all: the note says so, in the same place.
        BankTransaction.objects.all().delete()
        self.point(sept(15), "1100.00")
        self.assertIn(
            "Provisoire du 06/09 au 14/09 : aucun relevé importé.", text_of(section(self.html(tout="1"), "courbe"))
        )

    def test_several_accounts_are_summed_and_said(self):
        self.point(sept(1), "1000.00")
        self.line(sept(5), "10.00", account="****0042")
        self.line(sept(6), "20.00", account="****0077")
        self.assertIn("2 comptes importés : saisissez le total de leurs soldes.", self.html())

    def test_the_curve_and_the_months(self):
        self.a_gap()
        html = self.html(tout="1")
        self.assertIn('data-chart="line"', html)
        self.assertIn('aria-label="Trésorerie"', html)
        table = html[html.index('data-table-label="mois"') :]
        table = table[: table.index("</table>")]
        self.assertIn('data-sort="2026-09-01">Septembre 2026</td>', table)
        self.assertIn('<th class="num">Écart</th>', table)
        self.assertIn('<a href="#ecarts">50.00 €</a>', table)
        self.assertIn('data-sort="300.00">300.00 €</td>', table)
        self.assertIn('data-sort="-130.00">-130.00 €</td>', table)
        self.assertIn("au 20/09", table)
        self.assertNotIn("Ajustements</th>", table)

    def test_the_months_show_the_adjustments_and_no_gap_once_settled(self):
        self.a_gap()
        TreasuryAdjustment.objects.create(date=sept(15), amount=Decimal("50.00"))
        table = section(self.html(tout="1"), "courbe")
        self.assertIn('<th class="num">Ajustements</th>', table)
        self.assertIn('data-sort="50.00">50.00 €</td>', table)
        self.assertNotIn("Écart</th>", table)

    def test_a_suspect_point_is_named_on_both_its_cards(self):
        """10/09 typed 1 300,00 where the operations make 1 100,00: without
        it, its neighbours agree."""
        self.point(sept(1), "1000.00")
        self.line(sept(5), "100.00")
        self.point(sept(10), "1300.00")
        self.line(sept(12), "50.00")
        self.point(sept(15), "1150.00")
        self.line(sept(20), "1.00")
        cards = text_of(section(self.html(), "ecarts"))
        self.assertEqual(cards.count("Sans le point du 10/09, les points voisins concordent."), 2)
        self.assertIn("l'écart est de 200.00 € de plus.", cards)
        self.assertIn("l'écart est de 200.00 € de moins.", cards)

    def suspect_of_september(self):
        """01/09: 1 000,00; +100 on the 5th; 10/09 typed 1 300,00 where the
        operations make 1 100,00; +50 on the 12th; 15/09: 1 150,00; +1 on
        the 20th."""
        self.point(sept(1), "1000.00")
        self.line(sept(5), "100.00")
        self.point(sept(10), "1300.00")
        self.line(sept(12), "50.00")
        self.point(sept(15), "1150.00")
        self.line(sept(20), "1.00")

    def months_table(self, html: str) -> str:
        table = html[html.index('data-table-label="mois"') :]
        return table[: table.index("</table>")]

    def test_a_month_whose_two_gaps_cancel_out_still_shows_them(self):
        """Review C10: one wrong balance between two right ones makes two
        gaps of opposite sign. Summed, the month read nothing - no « Écart »
        column at all - while the page asked two resolutions."""
        self.suspect_of_september()
        html = self.html(tout="1")
        self.assertIn('<a href="#ecarts">2 écarts à résoudre</a>', html)
        table = self.months_table(html)
        self.assertIn('<th class="num">Écart</th>', table)
        self.assertIn('<a href="#ecarts">2 écarts</a>', table)
        self.assertNotIn("en tout", table)  # their sum is 0: nothing beside it
        # Two gaps that do not cancel: the count, and their sum beside it.
        TreasuryCheckpoint.objects.filter(date=sept(10)).update(balance=Decimal("1150.00"))
        TreasuryCheckpoint.objects.filter(date=sept(15)).update(balance=Decimal("1250.00"))
        table = text_of(self.months_table(self.html(tout="1")))
        self.assertIn("2 écarts 100.00 € en tout", table)

    def test_a_suspect_hint_names_the_adjustment_that_goes_with_it(self):
        """Review C2/C12: « Ajouter un ajustement » on the typo's first card
        dated +200 on 10/09; that card agrees, the other is -200. Without
        the point alone its neighbours are 200 apart: the hint names the
        adjustment, and the card offers its « Supprimer »."""
        self.suspect_of_september()
        made = TreasuryAdjustment.objects.create(date=sept(10), amount=Decimal("200.00"))
        cards = section(self.html(), "ecarts")
        said = text_of(cards)
        self.assertIn("Du 10/09/2026 au 15/09/2026", said)
        self.assertNotIn("Du 01/09/2026", said)
        self.assertIn(
            "Sans le point du 10/09 ni l'ajustement du 10/09 (+200.00 €), les points voisins concordent.", said
        )
        self.assertNotIn("Sans le point du 10/09, les points voisins", said)
        self.assertIn("Ajustement du 10/09/2026 : +200.00 € Supprimer", said)
        delete = forms_to(cards, reverse("bank:treasury_adjustment", args=[made.pk]), action="supprimer")
        self.assertEqual(len(delete), 1)
        self.assertEqual(delete[0].attrs["data-confirm"], "Supprimer l'ajustement du 10/09/2026 ?")

    def test_a_suspect_hint_names_every_adjustment_between_its_neighbours_once_a_card(self):
        """Both cards to resolve, an adjustment inside each: both are named
        on both cards, and each card lists each once - its own included."""
        self.suspect_of_september()
        early = TreasuryAdjustment.objects.create(date=sept(5), amount=Decimal("50.00"))
        late = TreasuryAdjustment.objects.create(date=sept(12), amount=Decimal("20.00"))
        cards = section(self.html(), "ecarts")
        said = text_of(cards)
        hint = (
            "Sans le point du 10/09 ni les ajustements du 05/09 (+50.00 €) et du 12/09 (+20.00 €), "
            "les points voisins concordent."
        )
        self.assertEqual(said.count(hint), 2)
        for adjustment in (early, late):
            with self.subTest(adjustment=adjustment.date):
                url = reverse("bank:treasury_adjustment", args=[adjustment.pk])
                self.assertEqual(len(forms_to(cards, url, action="supprimer")), 2)  # one a card
        # Adjustments that cancel out keep nobody apart: the plain hint.
        late.amount = Decimal("-50.00")
        late.save()
        said = text_of(section(self.html(), "ecarts"))
        self.assertEqual(said.count("Sans le point du 10/09, les points voisins concordent."), 2)

    def test_a_pending_gap_shows_no_figure_and_asks_nothing(self):
        """The 15th is past the statement: « relevé à importer », no card,
        no « Écart » column, no figure."""
        self.point(sept(1), "1000.00")
        self.line(sept(5), "100.00")
        self.point(sept(15), "1200.00")
        html = self.html(tout="1")
        self.assertNotIn('id="ecarts"', html)
        self.assertNotIn("Écart</th>", html)
        self.assertNotIn("à résoudre", html)
        self.assertNotIn("100.00 € de plus", html)
        self.assertIn('<span class="muted">relevé à importer</span>', section(html, "points"))
        self.assertIn("relevé à importer", section(html, "courbe"))

    def test_every_form_carries_the_window(self):
        self.a_gap()
        html = self.html(**SEPTEMBER)
        here = f"{reverse('bank:treasury')}?du=2026-09-01&au=2026-09-30"
        self.assertEqual(point_form_of(html).action, f"{here}#saisir")
        posted = [form for form in forms_of(html) if form.method == "post" and "next" in form.names]
        self.assertTrue(posted)
        for form in posted:
            with self.subTest(action=form.action):
                self.assertEqual(form.control("next").value, here)

    def test_the_window_narrows_the_curve_and_the_months_only(self):
        self.a_gap()
        for params in ({"du": "1990-01-01", "au": "1990-12-31"}, {"du": "2030-01-01"}):
            with self.subTest(params=params):
                html = self.html(**params)
                self.assertIn("Rien sur cette période.", html)
                self.assertNotIn('data-chart="line"', html)
                # The headline, the gap and the points are the whole history.
                self.assertIn("Trésorerie au 20/09/2026", html)
                self.assertIn('id="ecarts"', html)
                self.assertIn('data-table-label="soldes saisis"', html)
        self.assertIn('data-sort="2026-09-01">Septembre 2026</td>', self.html(du="0001-01-01"))
        html = self.html(du="2026-09-01", au="2026-09-12")
        self.assertIn("au 12/09", section(html, "courbe"))

    def test_a_date_that_is_no_date_draws_the_form_with_today(self):
        for value in ("hier", "2026-02-30", "\x00"):
            with self.subTest(value=value):
                form = point_form_of(self.html(date=value))
                self.assertEqual(form.control("date").value, timezone.localdate().isoformat())


class PointFormTests(Ledger):
    """« Saisir un solde », read off the page and posted as a browser posts
    it - its CSRF token included."""

    def post(self, values, *, press=("action", "enregistrer"), params=None, html=None):
        if html is None:
            html = self.browse(**(params or {}))
        form = point_form_of(html)
        return self.browser.post(form.action.split("#")[0], as_post(form.submission(press=press, values=values)))

    def test_the_date_and_the_balance_as_the_form_draws_them(self):
        form = point_form_of(self.html())
        today = timezone.localdate().isoformat()
        date_input = form.control("date")
        self.assertEqual((date_input.attrs["type"], date_input.value, date_input.attrs["max"]), ("date", today, today))
        balance = form.control("solde")
        # The iPhone's decimal keypad has no « - »: an overdraft could not be typed.
        self.assertNotIn("inputmode", balance.attrs)
        self.assertEqual((balance.attrs["type"], balance.attrs["placeholder"]), ("text", "1 234,56"))
        self.assertEqual([button.attrs["value"] for button in form.buttons()], ["enregistrer"])

    def test_a_balance_typed_is_saved_and_said_in_its_section(self):
        response = self.post({"date": "2026-09-01", "solde": "-1 234,50"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('bank:treasury')}#saisir")
        point = TreasuryCheckpoint.objects.get()
        self.assertEqual((point.date, point.balance), (sept(1), Decimal("-1234.50")))
        html = self.browser.get(response["Location"]).content.decode()
        self.assertIn(f"Solde du 01/09/2026 enregistré : -1{NBSP}234.50 €.", section(html, "saisir"))
        self.assertNotIn("enregistré", top_of(html))

    def test_every_refusal_is_french_on_its_field_and_writes_nothing(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        impossible = DATE_IMPOSSIBLE.format(first=date(2000, 1, 1), today=timezone.localdate())
        for day, balance, sentence in (
            ("2026-09-01", "abc", BALANCE_UNREADABLE),
            ("2026-09-01", "1.234,567", BALANCE_UNREADABLE),
            ("2026-09-01", "12,3456", BALANCE_UNREADABLE),
            ("2026-09-01", "1e999", BALANCE_UNREADABLE),
            ("2026-09-01", "10000000000", BALANCE_UNREADABLE),
            ("2026-09-01", "", BALANCE_UNREADABLE),
            ("2026-09-01", "12.500", BALANCE_AMBIGUOUS),
            ("2026-09-01", "-1,500", BALANCE_AMBIGUOUS),
            ("2026-09-01", "1\x00", NUL_SAID),
            (tomorrow.isoformat(), "100", impossible),
            ("1999-12-31", "100", impossible),
            ("02/10/2026", "100", DATE_UNREADABLE),
            ("", "100", DATE_UNREADABLE),
            ("2026-09-01\x00", "100", NUL_SAID),
        ):
            with self.subTest(day=day, balance=balance):
                response = self.post({"date": day, "solde": balance}, params=SEPTEMBER)
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, "un solde refusé")
                self.assertIn(sentence, text_of(section(response.content.decode(), "saisir")))
                self.assertFalse(TreasuryCheckpoint.objects.exists())

    def test_the_window_is_kept_through_a_refused_and_an_accepted_post(self):
        # A point already: the window form is drawn beside the curve.
        self.point(date(2026, 8, 1), "10.00")
        refused = self.post({"date": "2026-09-01", "solde": "abc"}, params=SEPTEMBER)
        html = refused.content.decode()
        self.assertIn('name="du" value="2026-09-01"', html)
        self.assertIn('name="au" value="2026-09-30"', html)
        self.assertEqual(point_form_of(html).control("solde").value, "abc")
        accepted = self.post({"date": "2026-09-01", "solde": "100"}, params=SEPTEMBER)
        self.assertEqual(accepted["Location"], f"{reverse('bank:treasury')}?du=2026-09-01&au=2026-09-30#saisir")
        self.assertEqual(TreasuryCheckpoint.objects.get(date=sept(1)).balance, Decimal("100.00"))

    def test_a_date_with_a_balance_is_never_replaced_without_remplacer(self):
        """« Enregistrer » on a date that has another balance - a phone tab
        left open on an old date - writes nothing and offers « Remplacer »."""
        self.point(sept(1), "1000.00")
        refused = self.post({"date": "2026-09-01", "solde": "900"})
        self.assertEqual(refused.status_code, 200)
        self.assertEqual(TreasuryCheckpoint.objects.get().balance, Decimal("1000.00"))
        html = refused.content.decode()
        self.assertIn(f"Le 01/09/2026 a déjà un solde : 1{NBSP}000.00 €.", section(html, "saisir"))
        form = point_form_of(html)
        self.assertEqual([button.attrs["value"] for button in form.buttons()], ["enregistrer", "remplacer"])
        replaced = self.post({}, press=("action", "remplacer"), html=html)
        self.assertEqual(replaced.status_code, 302)
        self.assertEqual(TreasuryCheckpoint.objects.get().balance, Decimal("900.00"))
        self.assertEqual(
            self.said(replaced), [f"Solde du 01/09/2026 enregistré : 900.00 € (remplace 1{NBSP}000.00 €)."]
        )

    def test_a_refusal_on_a_date_that_has_a_balance_says_that_balance_beside_remplacer(self):
        """Review C7: a stale tab posts its old date with a typo in the
        balance. Refused for the typo, the form offered « Remplacer » and
        never said what that date holds - the fixed typo then replaced it."""
        self.point(sept(1), "1000.00")
        for balance, sentence in (
            ("9 800,5O", BALANCE_UNREADABLE),
            ("12.500", BALANCE_AMBIGUOUS),
            ("1\x00", NUL_SAID),
        ):
            with self.subTest(balance=balance):
                refused = self.post({"date": "2026-09-01", "solde": balance})
                self.assertEqual(refused.status_code, 200)
                html = refused.content.decode()
                said = text_of(section(html, "saisir"))
                self.assertIn(sentence, said)
                self.assertIn("Le 01/09/2026 a déjà un solde : 1 000.00 €.", said)
                form = point_form_of(html)
                self.assertEqual([button.attrs["value"] for button in form.buttons()], ["enregistrer", "remplacer"])
                self.assertEqual(TreasuryCheckpoint.objects.get().balance, Decimal("1000.00"))
        # « Remplacer » already pressed - « Corriger », a typo in the amount:
        # still offered, the date not refused.
        html = self.browse(date="2026-09-01")
        refused = self.post({"solde": "1 0l0,00"}, press=("action", "remplacer"), html=html)
        self.assertEqual(refused.status_code, 200)
        html = refused.content.decode()
        said = text_of(section(html, "saisir"))
        self.assertIn(BALANCE_UNREADABLE, said)
        self.assertNotIn("a déjà un solde", said)
        self.assertEqual(
            [button.attrs["value"] for button in point_form_of(html).buttons()], ["enregistrer", "remplacer"]
        )
        # A date with no balance: no « Remplacer », nothing said of it.
        refused = self.post({"date": "2026-09-02", "solde": "abc"})
        html = refused.content.decode()
        self.assertNotIn("a déjà un solde", text_of(section(html, "saisir")))
        self.assertEqual([button.attrs["value"] for button in point_form_of(html).buttons()], ["enregistrer"])
        self.assertEqual(TreasuryCheckpoint.objects.get().balance, Decimal("1000.00"))

    def test_the_same_balance_again_is_already_saved(self):
        point = self.point(sept(1), "1000.00")
        response = self.post({"date": "2026-09-01", "solde": "1 000,00"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.said(response), [f"Solde du 01/09/2026 déjà enregistré : 1{NBSP}000.00 €."])
        self.assertEqual(list(TreasuryCheckpoint.objects.values_list("pk", "balance")), [(point.pk, point.balance)])

    def test_corriger_prefills_the_point_and_offers_remplacer(self):
        self.point(sept(1), "1000.00")
        html = self.browse(date="2026-09-01")
        form = point_form_of(html)
        self.assertEqual((form.control("date").value, form.control("solde").value), ("2026-09-01", "1000.00"))
        self.assertEqual([button.attrs["value"] for button in form.buttons()], ["enregistrer", "remplacer"])
        corrected = self.post({"solde": "1 010,00"}, press=("action", "remplacer"), html=html)
        self.assertEqual(corrected.status_code, 302)
        self.assertEqual(TreasuryCheckpoint.objects.get().balance, Decimal("1010.00"))
        # A date with no point: the date alone, and no « Remplacer ».
        form = point_form_of(self.html(date="2026-09-02"))
        self.assertEqual((form.control("date").value, form.control("solde").value), ("2026-09-02", ""))
        self.assertEqual([button.attrs["value"] for button in form.buttons()], ["enregistrer"])

    def test_remplacer_on_a_date_with_no_point_saves_it(self):
        self.point(sept(1), "1000.00")
        html = self.browse(date="2026-09-01")
        replaced = self.post({"date": "2026-09-05", "solde": "75"}, press=("action", "remplacer"), html=html)
        self.assertEqual(replaced.status_code, 302)
        self.assertEqual(
            list(TreasuryCheckpoint.objects.values_list("date", "balance")),
            [(sept(1), Decimal("1000.00")), (sept(5), Decimal("75.00"))],
        )
        response = self.client.post(
            reverse("bank:treasury"), {"date": "2026-09-06", "solde": "80", "action": "remplacer"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TreasuryCheckpoint.objects.count(), 3)

    def test_no_action_is_enregistrer_and_any_other_is_refused(self):
        response = self.client.post(reverse("bank:treasury"), {"date": "2026-09-01", "solde": "10"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.said(response), ["Solde du 01/09/2026 enregistré : 10.00 €."])
        self.assertEqual(TreasuryCheckpoint.objects.count(), 1)
        for action in ("supprimer", "", "REMPLACER"):
            with self.subTest(action=action):
                response = self.client.post(
                    reverse("bank:treasury"), {"date": "2026-09-02", "solde": "10", "action": action}
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.said(response), ["Action inconnue : rien n'a été modifié."])
                self.assertEqual(TreasuryCheckpoint.objects.count(), 1)

    def test_a_date_another_tab_saved_meanwhile_is_refused_in_french(self):
        """The check found nothing, and the unique date refused the write:
        two tabs. Said on the date, never a 500."""
        self.point(sept(1), "1000.00")
        with mock.patch.object(views, "_point_on", return_value=None):
            response = self.post({"date": "2026-09-01", "solde": "900"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Le 01/09/2026 a déjà un solde : 1{NBSP}000.00 €.", response.content.decode())
        self.assertEqual(TreasuryCheckpoint.objects.get().balance, Decimal("1000.00"))

    def test_a_balance_agreeing_with_the_previous_one_says_so(self):
        self.point(sept(1), "1000.00")
        self.line(sept(5), "200.00")
        self.line(sept(20), "-30.00")
        response = self.post({"date": "2026-09-15", "solde": "1 200"})
        self.assertEqual(
            self.said(response),
            [f"Solde du 15/09/2026 enregistré : 1{NBSP}200.00 € — concorde avec le point du 01/09."],
        )

    def test_a_balance_leaving_a_gap_warns_in_the_gaps(self):
        self.point(sept(1), "1000.00")
        self.line(sept(5), "300.00")
        self.line(sept(10), "-100.00")
        self.line(sept(20), "-30.00")
        response = self.post({"date": "2026-09-15", "solde": "1 250"})
        self.assertEqual(response["Location"], f"{reverse('bank:treasury')}#ecarts")
        html = self.browser.get(response["Location"]).content.decode()
        self.assertIn("Le solde du 15/09 ne concorde pas avec celui du 01/09 : à résoudre.", section(html, "ecarts"))
        self.assertIn('class="message message-warning"', section(html, "ecarts"))

    def test_a_pending_balance_adds_nothing(self):
        self.point(sept(1), "1000.00")
        response = self.post({"date": "2026-09-15", "solde": "1 250"})
        self.assertEqual(response["Location"], f"{reverse('bank:treasury')}#saisir")
        self.assertEqual(self.said(response), [f"Solde du 15/09/2026 enregistré : 1{NBSP}250.00 €."])


class AdjustmentTests(Ledger):
    """« Ajouter un ajustement de ±X € », and everything its POST refuses."""

    def setUp(self):
        super().setUp()
        self.a_gap()

    def card_form(self, html=None):
        found = forms_to(html or self.browse(), self.url)
        self.assertEqual(len(found), 1)
        return found[0]

    @property
    def url(self):
        return reverse("bank:treasury_adjustment_add")

    def send(self, form, **values):
        return self.browser.post(form.action, as_post(form.submission(values=values)))

    def gaps(self):
        return treasury.load().to_resolve

    def test_the_card_says_the_gap_and_its_actions(self):
        html = self.html()
        card = text_of(section(html, "ecarts"))
        for words in (
            "Du 01/09/2026 au 15/09/2026",
            "Les opérations expliquent +200.00 €, l'écart est de 50.00 € de plus.",
            "Relevé manquant entre ces dates ? Importez-le.",
            "Ajouter un ajustement de +50.00 €",
            "Point du 01/09/2026 : 1 000.00 €",
            "Supprimer le point du 15/09",
        ):
            with self.subTest(words=words):
                self.assertIn(words, card)
        form = self.card_form(html)
        self.assertEqual(
            {name: form.control(name).value for name in ("avant", "apres", "ecart")},
            {"avant": str(self.first.pk), "apres": str(self.second.pk), "ecart": "50.00"},
        )
        raison = form.control("raison")
        self.assertEqual((raison.attrs["placeholder"], raison.attrs["maxlength"]), ("Écart non expliqué", "255"))
        # Each delete its own form, asking first and naming its date.
        for point in (self.first, self.second):
            with self.subTest(point=point.date):
                deletes = forms_to(html, reverse("bank:treasury_point", args=[point.pk]), action="supprimer")
                self.assertEqual(len(deletes), 2)  # the card's and the table's
                for delete in deletes:
                    self.assertEqual(delete.attrs["data-confirm"], f"Supprimer le point du {point.date:%d/%m/%Y} ?")
        self.assertIn(f'href="{reverse("bank:treasury")}?date=2026-09-01#saisir"', html)
        self.assertIn('aria-label="Corriger le point du 01/09/2026"', html)
        self.assertIn('aria-label="Supprimer le point du 15/09/2026"', html)

    def test_an_adjustment_settles_the_gap_exactly(self):
        response = self.send(self.card_form(), raison="Espèces déposées")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('bank:treasury')}#ajustements")
        adjustment = TreasuryAdjustment.objects.get()
        self.assertEqual(
            (adjustment.date, adjustment.amount, adjustment.reason), (sept(15), Decimal("50.00"), "Espèces déposées")
        )
        self.assertEqual(self.gaps(), [])
        html = self.browser.get(response["Location"]).content.decode()
        self.assertIn(
            "Ajustement de +50.00 € ajouté au 15/09/2026 : les points du 01/09 et du 15/09 concordent.",
            section(html, "ajustements"),
        )
        self.assertNotIn('id="ecarts"', html)
        row = text_of(section(html, "ajustements"))
        self.assertIn("15/09/2026 +50.00 € Espèces déposées compte Supprimer", row)

    def test_a_double_click_adds_one_adjustment(self):
        form = self.card_form()
        self.assertEqual(len(self.said(self.send(form))), 1)
        second = self.send(form)
        self.assertEqual(TreasuryAdjustment.objects.count(), 1)
        self.assertEqual(self.said(second), ["Ces deux points concordent déjà : rien n'a été ajouté."])
        self.assertEqual(second["Location"], f"{reverse('bank:treasury')}#ecarts")

    def assertRefused(self, response, sentence):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.said(response), [sentence])
        self.assertFalse(TreasuryAdjustment.objects.exists())

    def test_a_point_gone_or_another_between_is_refused(self):
        form = self.card_form()
        middle = self.point(sept(12), "1200.00")
        self.assertRefused(self.send(form), "Un autre point est maintenant entre ces deux-là : rien n'a été ajouté.")
        middle.delete()
        self.second.delete()
        self.assertRefused(self.send(form), "Un des deux points n'existe plus : rien n'a été ajouté.")

    def test_a_gap_that_changed_is_refused(self):
        form = self.card_form()
        for shown in ("49.00", "50", "abc", "", "5e1", "50.000"):
            with self.subTest(shown=shown):
                self.assertRefused(
                    self.send(form, ecart=shown), "L'écart a changé depuis l'affichage : rien n'a été ajouté."
                )
        # A statement brought meanwhile: the gap shown is no longer the gap.
        self.line(sept(12), "20.00")
        self.assertRefused(self.send(form), "L'écart a changé depuis l'affichage : rien n'a été ajouté.")

    def test_ids_that_are_no_point_are_refused(self):
        form = self.card_form()
        for field in ("avant", "apres"):
            for value in ("abc", "²", "999999", "1" * 30, "-1"):
                with self.subTest(field=field, value=value):
                    self.assertRefused(
                        self.send(form, **{field: value}), "Un des deux points n'existe plus : rien n'a été ajouté."
                    )
        swapped = self.send(form, avant=str(self.second.pk), apres=str(self.first.pk))
        self.assertRefused(swapped, "Écart introuvable : rien n'a été ajouté.")

    def test_a_reason_is_refused_in_french(self):
        form = self.card_form()
        self.assertRefused(self.send(form, raison="a\x00b"), NUL_SAID)
        self.assertRefused(self.send(form, raison="x" * 256), "255 caractères au plus (256 ici).")

    def test_a_get_writes_nothing(self):
        response = self.client.get(self.url, {"avant": self.first.pk, "apres": self.second.pk, "ecart": "50.00"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TreasuryAdjustment.objects.exists())

    def test_the_real_operation_imported_after_brings_the_gap_back_the_other_way(self):
        """Adjusted, then the missing +50 imported: the gap is -50 and the
        card lists the adjustment with its « Supprimer » - the fix."""
        self.send(self.card_form())
        self.line(sept(12), "50.00")
        html = self.browse()
        card = text_of(section(html, "ecarts"))
        self.assertIn(
            "Les opérations expliquent +250.00 € et les ajustements +50.00 €, l'écart est de 50.00 € de moins.", card
        )
        self.assertIn("Ajustement du 15/09/2026 : +50.00 € Supprimer", card)
        adjustment = TreasuryAdjustment.objects.get()
        delete = forms_to(html, reverse("bank:treasury_adjustment", args=[adjustment.pk]), action="supprimer")
        self.assertEqual(len(delete), 2)
        self.assertEqual(delete[0].attrs["data-confirm"], "Supprimer l'ajustement du 15/09/2026 ?")
        response = self.browser.post(delete[0].action, as_post(delete[0].submission()))
        self.assertEqual(self.said(response), ["Ajustement de +50.00 € du 15/09/2026 supprimé."])
        self.assertEqual(self.gaps(), [])


class AdjustmentEdgeTests(Ledger):
    def test_a_pending_pair_is_refused(self):
        first, second = self.point(sept(1), "1000.00"), self.point(sept(15), "1100.00")
        self.line(sept(5), "10.00")
        response = self.client.post(
            reverse("bank:treasury_adjustment_add"), {"avant": first.pk, "apres": second.pk, "ecart": "90.00"}
        )
        self.assertEqual(self.said(response), ["Ces deux points attendent un relevé à importer : rien n'a été ajouté."])
        self.assertFalse(TreasuryAdjustment.objects.exists())

    def test_a_gap_wider_than_an_amount_is_refused_never_truncated(self):
        first = self.point(sept(1), "-9999999999.99")
        second = self.point(sept(15), "9999999999.99")
        self.line(sept(20), "1.00")
        html = self.html()
        self.assertIn("Trop grand pour un ajustement : corrigez un des points.", html)
        self.assertEqual(forms_to(html, reverse("bank:treasury_adjustment_add")), [])
        response = self.client.post(
            reverse("bank:treasury_adjustment_add"),
            {"avant": first.pk, "apres": second.pk, "ecart": "19999999999.98"},
        )
        self.assertEqual(
            self.said(response), ["Écart trop grand pour un ajustement : corrigez un des points. Rien n'a été ajouté."]
        )
        self.assertFalse(TreasuryAdjustment.objects.exists())


class PointActionTests(Ledger):
    """« Supprimer » and « Dater ce point du … » on a point."""

    def url(self, point):
        return reverse("bank:treasury_point", args=[point if isinstance(point, int) else point.pk])

    def test_a_point_deleted_from_its_row(self):
        self.a_gap()
        html = self.browse(**SEPTEMBER)
        table = section(html, "points")
        self.assertIn('data-confirm="Supprimer le point du 15/09/2026 ?"', table)
        delete = forms_to(table, self.url(self.second), action="supprimer")[0]
        response = self.browser.post(delete.action, as_post(delete.submission()))
        self.assertEqual(response["Location"], f"{reverse('bank:treasury')}?du=2026-09-01&au=2026-09-30#points")
        self.assertFalse(TreasuryCheckpoint.objects.filter(pk=self.second.pk).exists())
        html = self.browser.get(response["Location"]).content.decode()
        self.assertIn("Point du 15/09/2026 supprimé.", section(html, "points"))

    def test_a_delete_leaving_an_adjustment_outside_says_so_before_and_after(self):
        self.a_gap()
        TreasuryAdjustment.objects.create(date=sept(15), amount=Decimal("50.00"))
        html = self.html()
        self.assertIn(
            'data-confirm="Supprimer le point du 15/09/2026 ? 1 ajustement ne comptera plus."', section(html, "points")
        )
        response = self.client.post(self.url(self.second), {"action": "supprimer", "next": reverse("bank:treasury")})
        self.assertEqual(
            self.said(response),
            ["Point du 15/09/2026 supprimé. 1 ajustement ne compte plus (voir « Ajustements »)."],
        )
        html = self.html()
        self.assertIn("ne compte pas", section(html, "ajustements"))
        # The headline is the first point carried forward, the orphan left out.
        self.assertIn(f"1{NBSP}170.00 €", html)

    def test_a_point_gone_is_said_never_a_404(self):
        for pk in (999999, 10**29):
            with self.subTest(pk=pk):
                for action in ("supprimer", "veille"):
                    response = self.client.post(self.url(pk), {"action": action, "date": "2026-09-14"})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(self.said(response), ["Ce point n'existe plus."])

    def test_an_unknown_action_and_a_get_write_nothing(self):
        point = self.point(sept(1), "1000.00")
        response = self.client.post(self.url(point), {"action": "monter"})
        self.assertEqual(self.said(response), ["Action inconnue : rien n'a été modifié."])
        self.assertEqual(self.client.get(self.url(point), {"action": "supprimer"}).status_code, 302)
        self.assertTrue(TreasuryCheckpoint.objects.filter(pk=point.pk).exists())

    def test_a_point_read_before_its_day_s_operations_is_dated_the_day_before(self):
        self.read_before()
        html = self.browse()
        card = text_of(section(html, "ecarts"))
        self.assertIn("L'écart vaut les opérations du 15/09 : solde lu avant elles ?", card)
        move = forms_to(html, self.url(self.second), action="veille")
        self.assertEqual(len(move), 1)
        self.assertEqual(move[0].control("date").value, "2026-09-14")
        self.assertNotIn("data-confirm", move[0].attrs)
        response = self.browser.post(move[0].action, as_post(move[0].submission()))
        self.assertEqual(self.said(response), ["Point du 15/09/2026 daté du 14/09/2026."])
        self.second.refresh_from_db()
        self.assertEqual(self.second.date, sept(14))
        self.assertEqual(treasury.load().to_resolve, [])
        # A second click moves nothing.
        again = self.browser.post(move[0].action, as_post(move[0].submission()))
        self.assertEqual(self.said(again), ["Ce point a changé depuis l'affichage : rien n'a changé."])
        self.second.refresh_from_db()
        self.assertEqual(self.second.date, sept(14))

    def test_a_move_drawn_before_the_gap_changed_moves_nothing(self):
        """Review C6: the card offered « Dater ce point du 14/09 »; before the
        click a statement brings the +50 of the 12th and the two points agree.
        Posted from the page drawn before, the move would date a right reading
        the wrong day and make a gap of its own."""
        self.read_before()
        move = forms_to(self.browse(), self.url(self.second), action="veille")[0]
        self.line(sept(12), "50.00")
        self.assertEqual(treasury.load().to_resolve, [])
        response = self.browser.post(move.action, as_post(move.submission()))
        self.assertEqual(self.said(response), ["L'écart a changé depuis l'affichage : rien n'a changé."])
        self.second.refresh_from_db()
        self.assertEqual(self.second.date, sept(15))
        self.assertEqual(treasury.load().to_resolve, [])

    def test_a_move_drawn_before_an_adjustment_settled_the_pair_moves_nothing(self):
        """The same page in two tabs: the gap adjusted from one, the point
        then moved from the other."""
        self.read_before()
        html = self.browse()
        move = forms_to(html, self.url(self.second), action="veille")[0]
        settle = forms_to(html, reverse("bank:treasury_adjustment_add"))[0]
        self.said(self.browser.post(settle.action, as_post(settle.submission())))
        self.assertEqual(TreasuryAdjustment.objects.count(), 1)
        response = self.browser.post(move.action, as_post(move.submission()))
        self.assertEqual(self.said(response), ["L'écart a changé depuis l'affichage : rien n'a changé."])
        self.second.refresh_from_db()
        self.assertEqual(self.second.date, sept(15))

    def test_dating_a_point_the_day_before_is_refused_where_it_cannot_be(self):
        self.read_before()
        self.point(sept(14), "1100.00")
        html = self.html()
        self.assertEqual(forms_to(html, self.url(self.second), action="veille"), [])
        response = self.client.post(self.url(self.second), {"action": "veille", "date": "2026-09-14"})
        self.assertEqual(self.said(response), ["Le 14/09/2026 a déjà un solde : rien n'a changé."])
        self.second.refresh_from_db()
        self.assertEqual(self.second.date, sept(15))
        first_day = self.point(date(2000, 1, 1), "10.00")
        response = self.client.post(self.url(first_day), {"action": "veille", "date": "1999-12-31"})
        self.assertEqual(self.said(response), ["Le 31/12/1999 est avant le 01/01/2000 : rien n'a changé."])
        for posted in ("", "hier", "2026-09-13"):
            with self.subTest(posted=posted):
                response = self.client.post(self.url(self.second), {"action": "veille", "date": posted})
                self.assertEqual(self.said(response), ["Ce point a changé depuis l'affichage : rien n'a changé."])


class AdjustmentDeleteTests(Ledger):
    def url(self, adjustment):
        return reverse("bank:treasury_adjustment", args=[adjustment if isinstance(adjustment, int) else adjustment.pk])

    def test_deleting_a_counted_adjustment_brings_its_gap_back_said_in_the_gaps(self):
        self.a_gap()
        adjustment = TreasuryAdjustment.objects.create(date=sept(15), amount=Decimal("50.00"))
        response = self.client.post(self.url(adjustment), {"action": "supprimer", "next": reverse("bank:treasury")})
        self.assertEqual(response["Location"], f"{reverse('bank:treasury')}#ecarts")
        html = self.client.get(response["Location"]).content.decode()
        self.assertIn("Ajustement de +50.00 € du 15/09/2026 supprimé. 1 écart à résoudre.", section(html, "ecarts"))

    def test_the_last_adjustment_deleted_is_said_at_the_top(self):
        self.point(sept(1), "1000.00")
        orphan = TreasuryAdjustment.objects.create(date=sept(20), amount=Decimal("-12.30"), reason="Frais")
        other = TreasuryAdjustment.objects.create(date=sept(21), amount=Decimal("5.00"))
        html = self.html()
        self.assertIn("-12.30 € Frais ne compte pas", text_of(section(html, "ajustements")))
        response = self.client.post(self.url(orphan), {"action": "supprimer", "next": reverse("bank:treasury")})
        self.assertEqual(
            self.said(response), ["Ajustement de -12.30 € du 20/09/2026 supprimé. 1 ajustement ne compte toujours pas."]
        )
        self.assertEqual(response["Location"], f"{reverse('bank:treasury')}#ajustements")
        response = self.client.post(self.url(other), {"action": "supprimer", "next": reverse("bank:treasury")})
        self.assertEqual(response["Location"], reverse("bank:treasury"))
        html = self.client.get(response["Location"]).content.decode()
        self.assertNotIn('id="ajustements"', html)
        self.assertIn("Ajustement de +5.00 € du 21/09/2026 supprimé.", top_of(html))

    def test_one_gone_is_said_never_a_404_and_a_get_writes_nothing(self):
        for pk in (999999, 10**29):
            with self.subTest(pk=pk):
                response = self.client.post(self.url(pk), {"action": "supprimer"})
                self.assertEqual(self.said(response), ["Cet ajustement n'existe plus."])
        adjustment = TreasuryAdjustment.objects.create(date=sept(20), amount=Decimal("1.00"))
        self.assertEqual(self.client.get(self.url(adjustment), {"action": "supprimer"}).status_code, 302)
        response = self.client.post(self.url(adjustment), {"action": "veille"})
        self.assertEqual(self.said(response), ["Action inconnue : rien n'a été modifié."])
        self.assertTrue(TreasuryAdjustment.objects.filter(pk=adjustment.pk).exists())


class MessagesTests(Ledger):
    def test_a_message_from_elsewhere_is_said_at_the_top_once(self):
        self.client.post(reverse("bank:bank_reconcile"))
        html = self.html()
        sentence = "Aucun nouveau rapprochement certain : les suggestions restent à confirmer."
        self.assertEqual(text_of(html).count(sentence), 1)
        self.assertIn(sentence, text_of(top_of(html)))

    def test_every_delete_form_asks_first(self):
        self.a_gap()
        TreasuryAdjustment.objects.create(date=sept(15), amount=Decimal("50.00"))
        TreasuryAdjustment.objects.create(date=sept(25), amount=Decimal("1.00"))
        self.line(sept(12), "50.00")
        html = self.html()
        deletes = [
            form
            for form in forms_of(html)
            if any(button.attrs.get("value") == "supprimer" for button in form.buttons())
        ]
        self.assertEqual(len(deletes), 2 + 2 + 2 + 1)  # the card's points and adjustment, the tables
        for form in deletes:
            with self.subTest(action=form.action):
                self.assertTrue(form.attrs.get("data-confirm", "").startswith("Supprimer "))


class ImportWarningTests(TestCase):
    """The statement import asks too: a gap a statement made real is said
    on Banque, where the import lands."""

    def upload(self, *rows):
        content = statement(*rows)
        return self.client.post(reverse("bank:bank_home"), {"files": SimpleUploadedFile("releve.csv", content)})

    def test_an_import_making_a_gap_real_says_so(self):
        TreasuryCheckpoint.objects.create(date=sept(1), balance=Decimal("1000.00"))
        TreasuryCheckpoint.objects.create(date=sept(15), balance=Decimal("900.00"))
        response = self.upload(debit_row(sept(20), "EXEMPLE", "50,00"))
        said = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn("Trésorerie : 1 écart à résoudre.", said)

    def test_nothing_is_said_without_a_point_nor_when_they_agree(self):
        response = self.upload(debit_row(sept(20), "EXEMPLE", "50,00"))
        said = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(len(said), 1)
        self.assertNotIn("Trésorerie", said[0])
        TreasuryCheckpoint.objects.create(date=sept(21), balance=Decimal("950.00"))
        TreasuryCheckpoint.objects.create(date=sept(23), balance=Decimal("940.00"))
        response = self.upload(debit_row(sept(22), "EXEMPLE", "10,00"), debit_row(sept(25), "EXEMPLE", "1,00"))
        said = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertFalse([one for one in said if "Trésorerie" in one])


class FormTests(SimpleTestCase):
    def test_every_text_field_says_a_nul_in_french(self):
        """Every field of the forms Django checks for a NUL (their
        CharFields) carries the French sentence - one added later included."""
        for form, expected in (
            (TreasuryPointForm(today=date(2026, 10, 2)), ["date", "solde"]),
            (TreasuryAdjustmentForm(), ["raison"]),
        ):
            texts = [name for name, field in form.fields.items() if isinstance(field, forms.CharField)]
            self.assertEqual(texts, expected)
            for name in texts:
                with self.subTest(form=type(form).__name__, field=name):
                    self.assertEqual(form.fields[name].error_messages["null_characters_not_allowed"], NUL_REFUSED)
        self.assertEqual(NUL_REFUSED, NUL_SAID)
