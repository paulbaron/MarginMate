"""« Dépenses »: leaving categories out of the pie.

The owner (27/09): the VAT paid over is no money the bar spent on anything,
and with it in the pie every other wedge reads smaller than it is - « I want
to see the proportions of the other spending without it ». So a category can be
left out of THE PIE, and of nothing else:

* **the table and the total do not move.** The page's argument is that its
  categories add back up to what left the account, to the cent; a left-out
  category stays a row, marked, with no share, and
  `total == drawn_total + given_back + left_out_total` whatever is left out;
* **the shares and « Autres » are worked out over what remains**, and a
  left-out category is never a wedge nor folded into « Autres »;
* **it is a view carried in the address** (`?sans=`, by name, repeated), the
  « Marges » precedent: the table is the selector, a GET form whose every row
  sends its name under `montre` and, ticked, under `garder` - left out is
  shown and not kept (`common.left_out_from`, one definition for both
  pages), and the answer is a redirect to the clean address;
* **every link and form of the page carries it**, like the period and the
  kind; the links to other pages carry the window alone;
* **a name from a query string is only a name**: cleaned, once, in the order
  asked, never a 500 - and one with nothing in the window is kept and said.

Every name, payee and amount below is invented.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from html import unescape
from urllib.parse import urlsplit

from django.db import connection
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.http import urlencode

from bank import spending
from bank.models import BankTransaction, IgnoreRule, InvoicePayment
from common import KEPT_PARAM, LEFT_OUT_PARAM, SHOWN_PARAM, DateRange, left_out_from
from inventory.models import UnitChoices
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type, make_supplier
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

JUNE = DateRange(date(2026, 6, 1), date(2026, 6, 30))
#: What separates an amount's thousands on the page (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"
JUNE_PARAMS = {"du": "2026-06-01", "au": "2026-06-30"}
PAGE = "bank:spending_home"
FORM_ID = "pie-choice"
VAT = "TVA"
RENT = "Loyer inventé"
WORKS = "Travaux inventés"


def query_of(url: str) -> QueryDict:
    return QueryDict(urlsplit(url).query)


def euros(value) -> Decimal:
    return Decimal(value)


class Fixtures:
    """June: 500,00 € of VAT, 300,00 € of rent, 200,00 € of works - a
    thousand euros out, half of it the VAT. Without it the rent is 60 % of
    what the bar spent and the works 40 %."""

    def setUp(self):
        super().setUp()
        self.url = reverse(PAGE)
        self.counter = 0
        self.debit(date(2026, 6, 3), "IMPOTS INVENTES", "500.00", category=VAT)
        self.debit(date(2026, 6, 5), "BAILLEUR INVENTE", "300.00", category=RENT)
        self.debit(date(2026, 6, 8), "ARTISAN INVENTE", "200.00", category=WORKS)

    def debit(self, day, payee, amount, **kwargs):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            label=f"PRLV SEPA {payee} REF/{self.counter:04d}",
            counterparty=payee,
            amount=-euros(amount),
            kind=BankTransaction.Kind.DEBIT,
            fingerprint=f"sans-{self.counter}",
            **kwargs,
        )

    def credit(self, day, payee, amount, **kwargs):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            label=f"VIR SEPA RECU {payee}",
            counterparty=payee,
            amount=euros(amount),
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint=f"sans-credit-{self.counter}",
            **kwargs,
        )

    def given_back(self, day, amount_ht, category="Consignes inventées"):
        """A 60,00 € debit whose invoice gives back kegs for `amount_ht` HT
        beside 200,00 € HT of beer: the kegs' category comes out negative
        over the month. -150 HT is -180 TTC against 240 TTC of beer - the
        invoice is the debit to the cent, so nothing lands elsewhere."""
        supplier = make_supplier(code=f"GROS{self.counter}", name=f"Grossiste inventé {self.counter}")
        keg = make_stock_type(name=f"Fût inventé {self.counter}", unit=UnitChoices.UNIT, category=category)
        beer = make_stock_type(name=f"Blonde inventée {self.counter}", unit=UnitChoices.LITRE, category="Bières")
        document = make_invoice(supplier=supplier, invoice_date=day)
        for article, total_ht in ((keg, amount_ht), (beer, "200.00")):
            make_invoice_line(
                invoice=document,
                product=make_product(supplier=supplier, stock_type=article),
                total_ht=total_ht,
                vat_rate=Decimal("0.20"),
            )
        line = self.debit(day, supplier.name.upper(), "60.00")
        InvoicePayment.objects.create(transaction=line, invoice=document, method=InvoicePayment.Method.MANUAL)
        return line

    def report(self, *left_out, kind=""):
        return spending.spending_for(JUNE, kind=kind, left_out=left_out)

    def page(self, *left_out, **extra):
        params = {**JUNE_PARAMS, **extra}
        if left_out:
            params[LEFT_OUT_PARAM] = list(left_out)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response


# -- the one reading of a « Recalculer » ---------------------------------------


class LeftOutFromTests(SimpleTestCase):
    """`common.left_out_from`: what a « Recalculer » leaves out, read as
    « shown and not kept » - the rule « Marges » had alone, now shared."""

    def read(self, pairs, key=None):
        return left_out_from(QueryDict(urlencode(pairs)), key=key)

    def rows(self, *names, unticked=()):
        pairs = []
        for name in names:
            pairs.append((SHOWN_PARAM, name))
            if name not in unticked:
                pairs.append((KEPT_PARAM, name))
        return pairs

    def test_the_parameters_are_the_ones_the_margins_page_already_spells(self):
        self.assertEqual((LEFT_OUT_PARAM, SHOWN_PARAM, KEPT_PARAM), ("sans", "montre", "garder"))

    def test_nothing_asked_is_nothing_left_out(self):
        self.assertEqual(self.read([]), [])

    def test_unticking_leaves_out_exactly_what_was_unticked(self):
        self.assertEqual(self.read(self.rows(VAT, RENT, WORKS, unticked=(VAT,))), [VAT])

    def test_everything_ticked_back_is_nothing_left_out(self):
        self.assertEqual(self.read([(LEFT_OUT_PARAM, VAT), *self.rows(VAT, RENT)]), [])

    def test_a_name_the_form_did_not_show_keeps_its_state(self):
        """No row for it on this period: left out before, it stays out."""
        pairs = [(LEFT_OUT_PARAM, "Frais inventés"), *self.rows(VAT, RENT, unticked=(RENT,))]
        self.assertEqual(self.read(pairs), ["Frais inventés", RENT])

    def test_a_ticked_box_the_form_never_showed_changes_nothing(self):
        pairs = [*self.rows(VAT, RENT, unticked=(VAT,)), (KEPT_PARAM, "Autre chose"), (KEPT_PARAM, VAT)]
        # A second `garder` of a SHOWN row does count: the box was ticked.
        self.assertEqual(self.read(pairs), [])
        self.assertEqual(self.read([*self.rows(RENT, unticked=(RENT,)), (KEPT_PARAM, "Autre chose")]), [RENT])

    def test_the_order_asked_is_kept_and_what_is_new_comes_after(self):
        """« sans : Travaux, TVA » stays so after a « Recalculer » that
        changed nothing - in table order it would read « TVA, Travaux »."""
        pairs = [
            (LEFT_OUT_PARAM, WORKS),
            (LEFT_OUT_PARAM, VAT),
            *self.rows(VAT, RENT, WORKS, unticked=(VAT, WORKS, RENT)),
        ]
        self.assertEqual(self.read(pairs), [WORKS, VAT, RENT])

    def test_the_key_makes_two_spellings_of_one_row_one_row(self):
        """« TVA » in the address with spaces round it and « TVA » ticked on
        the form: compared raw they are two things, and the one the person
        just ticked back would stay out of the pie."""
        pairs = [(LEFT_OUT_PARAM, "  TVA "), *self.rows(VAT, RENT)]
        self.assertEqual(self.read(pairs), ["  TVA "])
        self.assertEqual(self.read(pairs, key=spending.clean_category), [])


class LeftOutNamesTests(SimpleTestCase):
    """`spending.left_out_names`: a name from a query string, as the page
    reads it."""

    def test_names_are_cleaned_once_each_in_the_order_asked(self):
        self.assertEqual(
            spending.left_out_names([" TVA ", RENT, "TVA", "  ", "", "Loyer\x00 inventé", WORKS]),
            [VAT, RENT, WORKS],
        )

    def test_a_name_wider_than_the_column_is_cut_like_a_stored_one(self):
        self.assertEqual(spending.left_out_names(["T" * 400]), ["T" * spending.CATEGORY_MAX])

    def test_what_is_no_string_is_nothing(self):
        self.assertEqual(spending.left_out_names([None, 12, VAT]), [VAT])


# -- the report --------------------------------------------------------------


class ThePieWithoutTests(Fixtures, TestCase):
    def test_a_left_out_category_stays_in_the_table_and_in_the_total(self):
        report = self.report(VAT)
        rows = {one.name: one for one in report.categories}
        self.assertEqual(report.total, euros("1000.00"))
        self.assertEqual(rows[VAT].amount, euros("500.00"))
        self.assertTrue(rows[VAT].left_out)
        self.assertIsNone(rows[VAT].share)
        self.assertFalse(rows[RENT].left_out)
        self.assertEqual(report.left_out, [VAT])
        self.assertEqual(report.left_out_total, euros("500.00"))

    def test_the_others_shares_are_worked_out_over_what_remains(self):
        everything = {one.name: one.share for one in self.report().categories}
        without = {one.name: one.share for one in self.report(VAT).categories}
        self.assertEqual((everything[RENT], everything[WORKS]), (Decimal("30.0"), Decimal("20.0")))
        self.assertEqual((without[RENT], without[WORKS]), (Decimal("60.0"), Decimal("40.0")))

    def test_a_left_out_category_is_no_wedge_and_the_wedges_make_a_hundred(self):
        report = self.report(VAT)
        self.assertEqual([piece.name for piece in report.slices], [RENT, WORKS])
        self.assertEqual(report.drawn_total, euros("500.00"))
        self.assertEqual(sum(piece.amount for piece in report.slices), report.drawn_total)
        self.assertEqual(sum(piece.share for piece in report.slices), Decimal("100"))

    def test_the_three_totals_add_back_up_to_what_left_the_account(self):
        """What the pie draws, what the invoices gave back, what the reader
        left out: every category is exactly one of the three - a left-out
        one whatever its sign."""
        self.given_back(date(2026, 6, 12), "-150.00")
        self.given_back(date(2026, 6, 14), "-150.00", category="Retours inventés")
        for left_out in (
            (),
            (VAT,),
            (VAT, "Consignes inventées"),
            ("Consignes inventées",),
            (VAT, RENT, WORKS, "Bières"),
        ):
            with self.subTest(left_out=left_out):
                report = self.report(*left_out)
                self.assertEqual(report.total, report.drawn_total + report.given_back + report.left_out_total)
                self.assertEqual(report.total, sum(one.amount for one in report.categories))

    def test_leaving_out_a_negative_category_moves_no_figure_about_the_money(self):
        """The stat « Déduit par les factures » reads `deducted`, which the
        pie cannot move; `given_back` is only what the pie's sentence still
        has to account for once the left-out ones are counted apart."""
        self.given_back(date(2026, 6, 12), "-150.00")
        everything = self.report()
        without = self.report("Consignes inventées")
        self.assertEqual(everything.deducted, euros("-180.00"))
        self.assertEqual(without.deducted, everything.deducted)
        self.assertEqual(without.given_back, euros("0"))
        self.assertEqual(without.left_out_total, euros("-180.00"))
        self.assertEqual(without.drawn_total, everything.drawn_total)

    def test_the_others_slice_is_worked_out_over_what_remains(self):
        """990, 5, 3 and 2: the two smallest are thinner than half a per
        cent and fold into « Autres ». Leave the big one out and each of the
        three is a sizeable share of what remains, with a wedge of its own."""
        BankTransaction.objects.all().delete()
        for payee, amount, name in (
            ("A", "990.00", "Gros poste"),
            ("B", "5.00", "Poste B"),
            ("C", "3.00", "Poste C"),
            ("D", "2.00", "Poste D"),
        ):
            self.debit(date(2026, 6, 3), payee, amount, category=name)
        self.assertIn(spending.OTHERS, [piece.name for piece in self.report().slices])
        without = self.report("Gros poste")
        self.assertEqual([piece.name for piece in without.slices], ["Poste B", "Poste C", "Poste D"])
        self.assertEqual([piece.share for piece in without.slices], [Decimal("50.0"), Decimal("30.0"), Decimal("20.0")])

    def test_a_left_out_category_is_never_folded_into_the_others_slice(self):
        """Poste D left out: it is not in the pie at all, not hidden inside
        « Autres » - which here holds nothing any more, the one thin
        category left being named rather than folded alone."""
        BankTransaction.objects.all().delete()
        for payee, amount, name in (
            ("A", "990.00", "Gros poste"),
            ("B", "5.00", "Poste B"),
            ("C", "3.00", "Poste C"),
            ("D", "2.00", "Poste D"),
        ):
            self.debit(date(2026, 6, 3), payee, amount, category=name)
        report = self.report("Poste D")
        names = [piece.name for piece in report.slices]
        self.assertNotIn("Poste D", names)
        self.assertNotIn(spending.OTHERS, names)
        self.assertEqual(sum(piece.held for piece in report.slices), 0)
        self.assertEqual(sum(piece.share for piece in report.slices), Decimal("100"))

    def test_uncategorised_may_be_left_out_like_any_other(self):
        self.debit(date(2026, 6, 9), "PAYEUR INVENTE", "100.00")
        self.assertEqual(self.report().slices[0].name, spending.NO_CATEGORY)
        report = self.report(spending.NO_CATEGORY)
        self.assertNotIn(spending.NO_CATEGORY, [piece.name for piece in report.slices])
        # Still the table's first row, and still the stat's figure.
        self.assertEqual(report.categories[0].name, spending.NO_CATEGORY)
        self.assertEqual(report.unsaid_total, euros("100.00"))

    def test_everything_left_out_draws_nothing_and_says_so(self):
        report = self.report(VAT, RENT, WORKS)
        self.assertEqual((report.slices, report.drawn_total), ([], euros("0")))
        self.assertTrue(report.nothing_left_to_draw)
        self.assertFalse(self.report().nothing_left_to_draw)
        self.assertEqual(report.total, euros("1000.00"))

    def test_a_name_with_nothing_in_the_window_is_kept(self):
        """A view about names: « sans TVA » over a month nothing was paid
        for it is still the reader's question over the next one."""
        report = self.report("Frais inventés", VAT)
        self.assertEqual(report.left_out, ["Frais inventés", VAT])
        self.assertEqual(report.left_out_total, euros("500.00"))
        self.assertNotIn("Frais inventés", [one.name for one in report.categories])

    def test_a_name_is_compared_once_cleaned(self):
        report = self.report("  TVA ", "TVA")
        self.assertEqual(report.left_out, [VAT])
        self.assertTrue(next(one for one in report.categories if one.name == VAT).left_out)

    def test_nothing_but_the_pie_moves(self):
        """The stats, the table's amounts, the work list and its chips are
        read off the whole window, whatever the pie leaves out."""
        IgnoreRule.objects.create(pattern="ARTISAN", description="Artisan", category=WORKS)
        self.debit(date(2026, 6, 9), "PAYEUR INVENTE", "100.00")
        everything = self.report()
        without = self.report(VAT, spending.NO_CATEGORY)
        self.assertEqual(without.total, everything.total)
        self.assertEqual(without.operations, everything.operations)
        self.assertEqual(
            [(one.name, one.amount, one.operations, one.by_rule) for one in without.categories],
            [(one.name, one.amount, one.operations, one.by_rule) for one in everything.categories],
        )
        self.assertEqual(without.unsaid_total, everything.unsaid_total)
        self.assertEqual(without.uninvoiced, everything.uninvoiced)
        self.assertEqual(without.deducted, everything.deducted)
        self.assertEqual(without.counts, everything.counts)
        self.assertEqual([one.line.pk for one in without.listed], [one.line.pk for one in everything.listed])

    def test_leaving_out_costs_no_query(self):
        with CaptureQueriesContext(connection) as everything:
            self.report()
        with self.assertNumQueries(len(everything.captured_queries)):
            self.report(VAT, RENT, "Frais inventés")


class KnownCategoriesTests(Fixtures, TestCase):
    def test_a_category_typed_on_a_credit_is_not_offered_for_a_spending(self):
        """« Entrées d'argent » names credits with the same field: an income
        word offered here would file a spending under it."""
        self.credit(date(2026, 6, 10), "CLIENT INVENTE", "800.00", category="Privatisation inventée")
        names = spending.known_categories()
        self.assertIn(VAT, names)
        self.assertNotIn("Privatisation inventée", names)


# -- the page ----------------------------------------------------------------


def selection_payload(html: str, unticked=(), ticked=()) -> list[tuple[str, str]]:
    """What a browser sends when « Recalculer le camembert » is pressed after
    unticking `unticked` and ticking `ticked`: the table's inputs that join
    the form through their `form` attribute, then the form's own hidden
    fields - in document order, each attribute unescaped as a browser posts
    it, and a box sent only when it is ticked."""
    pairs = []
    for kind, name, value, checked in re.findall(
        rf'<input type="(hidden|checkbox)" name="(montre|garder)" value="([^"]*)" form="{FORM_ID}"( checked)?',
        html,
    ):
        value = unescape(value)
        if kind == "checkbox" and not ((checked and value not in unticked) or value in ticked):
            continue
        pairs.append((name, value))
    start = html.index(f'id="{FORM_ID}"')
    form = html[start : html.index("</form>", start)]
    pairs += [
        (name, unescape(value))
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
    ]
    return pairs


def window_form(html: str) -> str:
    start = html.index('class="date-range"')
    return html[start : html.index("</form>", start)]


#: What only the pie itself draws (views._build_spending_pie_svg).
PIE = 'data-chart="pie"'


def stats_of(html: str) -> list[str]:
    """Every stat's label, value and note, in order, as the page prints them."""
    return [
        " ".join(text.split())
        for text in re.findall(r'<span class="stat-(?:label|value|note)">(.*?)</span>', html, re.DOTALL)
    ]


class ThePageSaysWhatIsLeftOutTests(Fixtures, TestCase):
    def test_with_nothing_left_out_nothing_is_said(self):
        html = self.page().content.decode()
        self.assertNotIn("Camembert sans", html)
        self.assertNotIn("hors camembert", html)
        self.assertIn('aria-label="Répartition des dépenses par catégorie"', html)

    def test_the_line_above_the_pie_names_what_is_out_and_how_much(self):
        response = self.page(VAT)
        html = response.content.decode()
        assertNoUnrenderedTemplateSyntax(self, response, "Dépenses sans TVA")
        self.assertIn("Camembert sans :", html)
        self.assertIn("TVA 500.00 €", html)
        self.assertIn("500.00 € hors du camembert sur", html)
        self.assertIn(f"1{NBSP}000.00 € sortis du compte", html)
        self.assertIn("tout remettre", html)
        self.assertLess(html.index("Camembert sans :"), html.index(PIE))

    def test_the_pie_is_labelled_without_what_it_leaves_out(self):
        html = self.page(VAT, WORKS).content.decode()
        self.assertIn('aria-label="Répartition des dépenses par catégorie, sans : TVA, Travaux inventés"', html)
        self.assertIn("100.0 %", html)

    def test_a_name_that_reaches_the_svg_is_escaped(self):
        self.debit(date(2026, 6, 9), "PAYEUR INVENTE", "10.00", category="<b>Divers</b>")
        html = self.page("<b>Divers</b>").content.decode()
        self.assertNotIn("<b>Divers</b>", html)
        self.assertIn("sans : &lt;b&gt;Divers&lt;/b&gt;", html)

    def test_a_name_with_nothing_on_the_period_is_said_so(self):
        html = self.page("Frais inventés").content.decode()
        self.assertIn("Frais inventés : rien sur cette période", html)

    def test_the_reconciling_sentence_adds_the_three_up(self):
        self.given_back(date(2026, 6, 12), "-150.00")
        html = self.page(VAT).content.decode()
        # 1000 + 60 out; 500 of VAT out of the pie; the keg's -180 given
        # back; 740 drawn (rent, works, and the invoice's 240 of beer).
        # Folded on the template's own white space only: `\s` would fold the
        # no-break space between the thousands too.
        self.assertIn(
            f"Le camembert dessine 740.00 € des 1{NBSP}060.00 € sortis du compte : les 500.00 € des catégories "
            "laissées hors du camembert et les -180.00 € déduits par les factures rattachées",
            re.sub(r"[ \t\r\n]+", " ", html),
        )

    def test_the_stats_do_not_move(self):
        """Left out of the pie, the VAT and the kegs given back are still
        what left the account and what the invoices deducted."""
        self.given_back(date(2026, 6, 12), "-150.00")
        self.debit(date(2026, 6, 9), "PAYEUR INVENTE", "100.00")
        everything = stats_of(self.page().content.decode())
        without = stats_of(self.page(VAT, "Consignes inventées", spending.NO_CATEGORY).content.decode())
        self.assertEqual(without, everything)
        self.assertIn("Déduit par les factures", without)
        self.assertIn("-180.00 €", without)

    def test_everything_left_out_draws_no_pie_and_says_why(self):
        html = self.page(VAT, RENT, WORKS).content.decode()
        self.assertNotIn(PIE, html)
        self.assertIn("Toutes les catégories de la période sont hors du camembert", html)

    def test_the_page_says_nothing_is_saved_and_where_the_selection_lives(self):
        html = self.page().content.decode()
        self.assertIn(">Dans le camembert</th>", html)
        self.assertIn("la sélection vit dans l'adresse de la page", unescape(html))
        self.assertIn("Recalculer le camembert", html)


class TheTableIsTheSelectorTests(Fixtures, TestCase):
    def box(self, html, name):
        found = re.search(
            rf'<input type="checkbox" name="garder" value="{re.escape(name)}" form="{FORM_ID}"[^>]*>', html
        )
        return found.group(0) if found else ""

    def cell(self, html, name):
        at = html.index(f'name="montre" value="{name}"')
        start = html.rindex("<td", 0, at)
        return html[start : html.index("</td>", at)]

    def test_every_box_is_ticked_by_default(self):
        html = self.page().content.decode()
        for name in (VAT, RENT, WORKS):
            with self.subTest(name=name):
                self.assertIn(" checked", self.box(html, name))
                self.assertIn('data-sort="1"', self.cell(html, name))

    def test_what_is_left_out_is_unticked_and_says_so_in_text(self):
        """An input is invisible to the table's search and sort
        (static/js/datatable.js reads textContent): the state is a
        `data-sort` and a word."""
        html = self.page(VAT).content.decode()
        self.assertNotIn(" checked", self.box(html, VAT))
        self.assertIn('data-sort="0"', self.cell(html, VAT))
        self.assertIn("hors camembert", self.cell(html, VAT))
        self.assertIn(" checked", self.box(html, RENT))
        self.assertNotIn("hors camembert", self.cell(html, RENT))

    def test_the_table_still_has_its_search_and_sort(self):
        self.assertContains(self.page(VAT), 'data-table-label="catégories"')

    def test_recalculate_carries_the_period_the_kind_and_what_is_out(self):
        response = self.page(VAT, "Frais inventés", tout="1", classement=spending.BY_HAND)
        html = response.content.decode()
        start = html.index(f'id="{FORM_ID}"')
        self.assertIn('method="get"', html[html.rindex("<form", 0, start) : start])
        pairs = selection_payload(html)
        for pair in (
            ("du", "2026-06-01"),
            ("au", "2026-06-30"),
            ("tout", "1"),
            ("classement", spending.BY_HAND),
            (LEFT_OUT_PARAM, VAT),
            (LEFT_OUT_PARAM, "Frais inventés"),
        ):
            with self.subTest(pair=pair):
                self.assertIn(pair, pairs)


class WhatTheFormSendsTests(Fixtures, TestCase):
    """The payload a browser really sends, read off the page it drew, and
    the redirect to the clean address it gets back."""

    def submit(self, pairs):
        return self.client.get(f"{self.url}?{urlencode(pairs)}")

    def test_unticking_leaves_out_what_was_unticked(self):
        html = self.page().content.decode()
        response = self.submit(selection_payload(html, unticked=(VAT,)))
        self.assertRedirects(response, f"{self.url}?{urlencode([*JUNE_PARAMS.items(), (LEFT_OUT_PARAM, VAT)])}")
        followed = self.client.get(response["Location"])
        self.assertEqual(followed.context["report"].left_out, [VAT])

    def test_ticking_everything_back_is_the_whole_pie(self):
        html = self.page(VAT).content.decode()
        # Pressed as drawn, nothing moves: TVA stays out.
        self.assertEqual(query_of(self.submit(selection_payload(html))["Location"]).getlist(LEFT_OUT_PARAM), [VAT])
        response = self.submit(selection_payload(html, ticked=(VAT,)))
        self.assertRedirects(response, f"{self.url}?{urlencode(JUNE_PARAMS)}")

    def test_a_name_with_no_row_on_this_period_stays_out(self):
        html = self.page("Frais inventés").content.decode()
        response = self.submit(selection_payload(html, unticked=(RENT,)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(query_of(response["Location"]).getlist(LEFT_OUT_PARAM), ["Frais inventés", RENT])

    def test_the_order_asked_is_kept(self):
        html = self.page(WORKS, VAT).content.decode()
        response = self.submit(selection_payload(html, unticked=(WORKS, VAT)))
        self.assertEqual(query_of(response["Location"]).getlist(LEFT_OUT_PARAM), [WORKS, VAT])

    def test_the_period_and_the_kind_survive_recalculate(self):
        html = self.page(tout="1", classement=spending.BY_HAND).content.decode()
        location = self.submit(selection_payload(html, unticked=(VAT,)))["Location"]
        query = query_of(location)
        self.assertEqual((query["du"], query["au"], query["tout"]), ("2026-06-01", "2026-06-30", "1"))
        self.assertEqual(query["classement"], spending.BY_HAND)
        self.assertEqual(query.getlist(LEFT_OUT_PARAM), [VAT])

    def test_a_kind_nobody_can_use_does_not_travel(self):
        pairs = [*JUNE_PARAMS.items(), ("classement", "<script>"), (SHOWN_PARAM, VAT)]
        self.assertNotIn("classement", query_of(self.submit(pairs)["Location"]))

    def test_a_garbled_name_is_cleaned_or_dropped_never_a_500(self):
        pairs = [*JUNE_PARAMS.items(), (LEFT_OUT_PARAM, "T\x00VA"), (LEFT_OUT_PARAM, "   "), (SHOWN_PARAM, RENT)]
        response = self.submit(pairs)
        self.assertEqual(query_of(response["Location"]).getlist(LEFT_OUT_PARAM), [VAT, RENT])


class TheSelectionTravelsTests(Fixtures, TestCase):
    def test_a_garbled_name_in_the_address_is_no_500_and_does_not_travel(self):
        for value in ("\x00", "   ", "T" * 400):
            with self.subTest(value=value[:5]):
                response = self.page(value)
                assertNoUnrenderedTemplateSyntax(self, response, "Dépenses sans un nom illisible")
                self.assertNotIn(value, query_of(response.context["page_url"]).getlist(LEFT_OUT_PARAM))

    def test_every_link_and_form_carries_the_selection_as_it_carries_the_period(self):
        response = self.page(VAT, WORKS, classement=spending.UNSAID)
        both = [VAT, WORKS]
        for name in ("page_url", "all_url", "period_url"):
            with self.subTest(link=name):
                query = query_of(response.context[name])
                self.assertEqual(query.getlist(LEFT_OUT_PARAM), both)
                self.assertEqual(query["du"], JUNE_PARAMS["du"])
                self.assertEqual(query["classement"], spending.UNSAID)
        self.assertEqual(query_of(response.context["all_url"])["tout"], "1")
        # « Effacer » clears the dates, not the selection.
        clear = query_of(response.context["clear_url"])
        self.assertEqual(clear.getlist(LEFT_OUT_PARAM), both)
        self.assertNotIn("du", clear)
        for chip in response.context["kind_chips"]:
            with self.subTest(chip=chip["key"]):
                self.assertEqual(query_of(chip["url"]).getlist(LEFT_OUT_PARAM), both)
                self.assertEqual(query_of(chip["url"])["du"], JUNE_PARAMS["du"])

    def test_the_window_form_carries_the_kind_and_the_selection_beside_its_dates(self):
        html = self.page(VAT, classement=spending.UNSAID).content.decode()
        form = window_form(html)
        self.assertIn(f'<input type="hidden" name="sans" value="{VAT}">', form)
        self.assertIn(f'<input type="hidden" name="classement" value="{spending.UNSAID}">', form)
        self.assertNotIn('<input type="hidden" name="du"', form)

    def test_a_category_typed_comes_back_with_the_pie_it_was_typed_under(self):
        line = self.debit(date(2026, 6, 9), "PAYEUR INVENTE", "10.00")
        response = self.page(VAT)
        self.assertEqual(query_of(response.context["page_url"]).getlist(LEFT_OUT_PARAM), [VAT])
        self.assertContains(response, "sans=TVA#a-classer")
        answer = self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]),
            {"action": "category", "categorie": "Divers", "next": f"{response.context['page_url']}#a-classer"},
            follow=True,
        )
        self.assertEqual(answer.context["report"].left_out, [VAT])

    def test_each_name_can_be_put_back_on_its_own(self):
        response = self.page(VAT, "Frais inventés", WORKS)
        rows = {row.name: row for row in response.context["left_out_rows"]}
        self.assertEqual(list(rows), [VAT, "Frais inventés", WORKS])
        self.assertEqual(query_of(rows[VAT].put_back_url).getlist(LEFT_OUT_PARAM), ["Frais inventés", WORKS])
        self.assertEqual(query_of(rows[WORKS].put_back_url)["du"], JUNE_PARAMS["du"])
        self.assertIsNone(rows["Frais inventés"].category)
        self.assertEqual(rows[VAT].category.amount, euros("500.00"))

    def test_putting_everything_back_is_the_same_page_with_nothing_left_out(self):
        response = self.page(VAT, classement=spending.BY_HAND)
        reset = query_of(response.context["reset_url"])
        self.assertEqual(reset.getlist(LEFT_OUT_PARAM), [])
        self.assertEqual((reset["du"], reset["classement"]), (JUNE_PARAMS["du"], spending.BY_HAND))

    def test_links_to_other_pages_carry_the_window_alone(self):
        response = self.page(VAT, classement=spending.BY_HAND)
        margins = query_of(response.context["margins_url"])
        self.assertEqual((margins["du"], margins["au"]), (JUNE_PARAMS["du"], JUNE_PARAMS["au"]))
        self.assertNotIn(LEFT_OUT_PARAM, margins)
        self.assertNotIn("classement", margins)
        self.assertContains(response, response.context["margins_url"].replace("&", "&amp;"))
        income = response.context["income_url"]
        if income:
            self.assertNotIn(LEFT_OUT_PARAM, query_of(income))
            self.assertEqual(query_of(income)["du"], JUNE_PARAMS["du"])
            self.assertContains(response, f'href="{income.replace("&", "&amp;")}" class="tab">Entrées d\'argent</a>')
        else:
            self.assertNotContains(response, ">Entrées d'argent</a>")
        # « Rapprocher les factures », the « une facture manque » stat and the
        # empty state all open Banque: over the same period, like the others.
        bank = query_of(response.context["bank_url"])
        self.assertEqual((bank["du"], bank["au"]), (JUNE_PARAMS["du"], JUNE_PARAMS["au"]))
        self.assertNotIn(LEFT_OUT_PARAM, bank)
        self.assertNotIn("classement", bank)
        self.assertContains(response, response.context["bank_url"].replace("&", "&amp;"))

    def test_over_the_whole_history_the_bank_page_opens_on_everything(self):
        """Banque has no default period: with no dates it is every month,
        which is what « tout » asked for."""
        response = self.page(tout="1")
        bank = query_of(response.context["bank_url"])
        self.assertNotIn("du", bank)
        self.assertNotIn("au", bank)

    def test_over_the_default_period_the_bank_page_opens_on_that_year(self):
        response = self.client.get(self.url)
        window = response.context["window"]
        bank = query_of(response.context["bank_url"])
        self.assertEqual((bank["du"], bank["au"]), (window.start.isoformat(), window.end.isoformat()))


class ANameStoredUncleanTests(Fixtures, TestCase):
    """A category stored in a shape `clean_category` does not produce - a
    double space, typed on a rule before its form cleaned what it stored -
    is left out all the same.

    The names in `?sans=` arrive cleaned (`left_out_names`), so the names
    they are compared with have to be cleaned too: compared as stored, the
    reader unticked the row, pressed « Recalculer », and got back a pie
    still drawing it, under a line saying it had nothing on this period
    while the table beside it showed its money. Invented names and amounts.
    """

    STORED = "Frais  bancaires"
    CLEAN = "Frais bancaires"

    def setUp(self):
        super().setUp()
        # Created as an older rule was stored: the form used to trim the
        # ends of what was typed, and nothing else.
        IgnoreRule.objects.create(pattern="BANQUE INVENTEE", description="Frais", category=self.STORED)
        self.debit(date(2026, 6, 10), "BANQUE INVENTEE", "12.00")

    def row(self, report):
        return next(one for one in report.categories if one.name == self.STORED)

    def test_the_report_leaves_it_out_whichever_spelling_asks(self):
        for asked in (self.STORED, self.CLEAN):
            with self.subTest(asked=asked):
                report = self.report(asked)
                self.assertTrue(self.row(report).left_out)
                self.assertNotIn(self.STORED, [piece.name for piece in report.slices])
                self.assertEqual(report.left_out_total, euros("12.00"))
                self.assertEqual(report.total, report.drawn_total + report.given_back + report.left_out_total)

    def test_unticked_on_the_page_it_leaves_the_pie(self):
        html = self.page().content.decode()
        response = self.client.get(f"{self.url}?{urlencode(selection_payload(html, unticked=(self.STORED,)))}")
        self.assertEqual(query_of(response["Location"]).getlist(LEFT_OUT_PARAM), [self.CLEAN])
        followed = self.client.get(response["Location"])
        page = followed.content.decode()
        self.assertEqual(followed.context["report"].left_out, [self.CLEAN])
        self.assertTrue(self.row(followed.context["report"]).left_out)
        self.assertNotIn(f'data-label="{self.STORED}"', page)
        self.assertNotIn("rien sur cette période", page)
        self.assertIn(f"{self.CLEAN} 12.00 €", page)

    def test_two_spellings_of_one_name_go_out_together_and_are_said_as_one_sum(self):
        """« Frais bancaires » typed on a line, « Frais  bancaires » on a
        rule: one key, as `left_out_from` already reads them, so both leave
        the pie - and the « sans : » sentence says what they hold together,
        not one of the two amounts."""
        self.debit(date(2026, 6, 11), "GUICHET INVENTE", "5.00", category=self.CLEAN)
        report = self.report(self.CLEAN)
        self.assertEqual(
            sorted((one.name, one.left_out) for one in report.categories if one.name in (self.STORED, self.CLEAN)),
            sorted([(self.CLEAN, True), (self.STORED, True)]),
        )
        rows = self.page(self.CLEAN).context["left_out_rows"]
        self.assertEqual([(row.name, row.category.amount) for row in rows], [(self.CLEAN, euros("17.00"))])

    def test_a_rule_typed_today_stores_its_category_clean(self):
        """The rules page cleans a category as every other place that
        stores one does (`rule_action`, `set_category`)."""
        self.client.post(
            reverse("bank:rule_list"),
            {
                "pattern": "AUTRE BANQUE INVENTEE",
                "description": "Autres frais",
                "category": " Frais \N{NO-BREAK SPACE} bancaires\N{ZERO WIDTH SPACE} ",
            },
        )
        self.assertEqual(IgnoreRule.objects.get(pattern="AUTRE BANQUE INVENTEE").category, self.CLEAN)
