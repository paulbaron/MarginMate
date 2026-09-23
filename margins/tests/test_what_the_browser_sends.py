"""The Marges page's forms, submitted the way a browser submits them.

Every other test of the two selectors writes its request by hand - the keys,
the ids, which box is ticked. That tests the view, and not the page: a
checkbox tied to the wrong form (`form="compter-…"`), a hidden `affiche` or
`montre` dropped from a row, the selection's hidden fields left out of
« Recalculer », a CSRF token missing - the hand-written request goes on
passing, and the owner's click does nothing, or the wrong thing. This
codebase has shipped that bug three times (CLAUDE.md, « Formsets: test what
the browser actually posts »).

So here the request is READ OFF THE RENDERED PAGE, as a browser builds it:

* a control belongs to the `<form>` around it, or to the one its `form`
  attribute names - which is how an article's box, in a `<tbody>` no form can
  wrap, reaches its category's form;
* an unticked checkbox sends nothing, a disabled control sends nothing, and
  only the button clicked sends its name and value;
* the POST goes through a client that enforces CSRF, as the browser's does.

Also here, three promises the reports made and no test held: categories
sorted as a person reads them (accents and case aside), the links to OTHER
pages carrying the period but not `sans`, and the duty of an invoice whose
every line is a return.

Data invented throughout - no real supplier, article or amount.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.http import QueryDict
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.http import urlencode

from common import DateRange
from inventory.models import StockType
from margins.computation import CHARGES_KEY, NO_CATEGORY, Money, article_key, category_key, margins_for
from margins.tests.test_counted_articles_page import PanelFixture, ticked
from margins.tests.test_page import stat_of, value_of
from margins.tests.test_spend_selection_page import SelectionFixture
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type

PAGE = "margins:margins_home"
MARCH = {"du": "2026-03-01", "au": "2026-03-31"}


class _Forms(HTMLParser):
    """Every form of a page and the controls that belong to it - by nesting,
    or by their `form` attribute, as a browser associates them."""

    CONTROLS = ("input", "button", "select", "textarea")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._current: dict | None = None
        self._loose: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attributes = {name: ("" if value is None else value) for name, value in attrs}
        if tag == "form":
            form = {"attrs": attributes, "controls": []}
            self.forms.append(form)
            if "id" in attributes:
                self._by_id[attributes["id"]] = form
            self._current = form
            return
        if tag not in self.CONTROLS:
            return
        control = {"tag": tag, **attributes}
        if "form" in attributes:
            # Resolved once the whole page is read: the form may come after.
            self._loose.append(control)
        elif self._current is not None:
            self._current["controls"].append(control)

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None

    def close(self):
        super().close()
        for control in self._loose:
            owner = self._by_id.get(control["form"])
            if owner is not None:
                owner["controls"].append(control)


def forms_of(html: str) -> _Forms:
    parser = _Forms()
    parser.feed(html)
    parser.close()
    return parser


def submission(form: dict, *, click: tuple[str, str] | None = None, flip=()) -> list[tuple[str, str]]:
    """What the browser sends for `form` when the button `click` (name,
    value) is pressed - None for the form's only submit button - with the
    checkboxes named in `flip` ((name, value) pairs) toggled first."""
    flip = set(flip)
    pairs = []
    clicked = False
    for control in form["controls"]:
        name = control.get("name")
        if "disabled" in control:
            continue
        kind = control.get("type", "submit" if control["tag"] == "button" else "text")
        if kind in ("submit", "button", "image", "reset"):
            if click is not None and name is not None and (name, control.get("value", "")) == click:
                pairs.append((name, control.get("value", "")))
                clicked = True
            elif click is None:
                clicked = True
            continue
        if not name:
            continue
        value = control.get("value", "on" if kind == "checkbox" else "")
        if kind == "checkbox":
            checked = "checked" in control
            if (name, value) in flip:
                checked = not checked
            if checked:
                pairs.append((name, value))
            continue
        pairs.append((name, value))
    assert clicked, f"no enabled button {click} in this form"
    return pairs


def as_post(pairs) -> dict[str, list[str]]:
    data: dict[str, list[str]] = {}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    return data


def form_with(parser: _Forms, *, name: str, value: str) -> dict:
    """The form holding a control `name`=`value` (a hidden `categorie`)."""
    for form in parser.forms:
        if any(control.get("name") == name and control.get("value") == value for control in form["controls"]):
            return form
    raise AssertionError(f"no form holds {name}={value!r}")


def form_of_class(parser: _Forms, css_class: str) -> dict:
    return next(form for form in parser.forms if css_class in form["attrs"].get("class", "").split())


class ThePanelAsTheBrowserPostsItTests(PanelFixture, TestCase):
    """« Articles comptés dans la marge produits »: a category's form, read
    off the page, posted with CSRF enforced."""

    def setUp(self):
        self.browser = Client(enforce_csrf_checks=True)

    def page(self, **params):
        response = self.browser.get(reverse(PAGE), {**MARCH, **params})
        self.assertEqual(response.status_code, 200)
        return response

    def test_enregistrer_changes_the_boxes_clicked_in_that_category_and_nothing_else(self):
        """Ticked from the page: the tablecloth in Matériel. Also unticked on
        the same screen, in ANOTHER category's rows: the paper towels - a
        browser sends one form, so that click is simply not in the post."""
        parser = forms_of(self.page(sans=[CHARGES_KEY]).content.decode())
        material = form_with(parser, name="categorie", value="Matériel")
        pairs = submission(
            material,
            click=("action", "enregistrer"),
            flip=[("coche", str(self.cloth.pk)), ("coche", str(self.towels.pk))],
        )

        response = self.browser.post(reverse("margins:count_articles"), as_post(pairs))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticked(self.drill, self.cloth, self.stool), [False, True, False])
        self.assertEqual(ticked(self.towels, self.cups), [True, False])
        location = urlsplit(response["Location"])
        self.assertEqual(location.fragment, "articles-comptes")
        self.assertEqual(QueryDict(location.query).getlist("sans"), [CHARGES_KEY])
        self.assertEqual(QueryDict(location.query)["du"], MARCH["du"])

    def test_the_article_boxes_belong_to_their_own_category_s_form(self):
        """Each category's form carries exactly its articles, shown and
        ticked - none of another category's."""
        parser = forms_of(self.page().content.decode())
        for category, members, ticked_now in (
            ("Matériel", [self.drill, self.cloth, self.stool], []),
            ("Consommables", [self.towels, self.cups], [self.towels]),
            ("", [self.syrup], []),
        ):
            with self.subTest(category=category):
                pairs = submission(form_with(parser, name="categorie", value=category), click=("action", "enregistrer"))
                shown = sorted(int(value) for name, value in pairs if name == "affiche")
                kept = sorted(int(value) for name, value in pairs if name == "coche")
                self.assertEqual(shown, sorted(article.pk for article in members))
                self.assertEqual(kept, sorted(article.pk for article in ticked_now))

    def test_saving_untouched_changes_nothing(self):
        parser = forms_of(self.page().content.decode())
        for form in parser.forms:
            if not any(control.get("name") == "categorie" for control in form["controls"]):
                continue
            self.browser.post(
                reverse("margins:count_articles"), as_post(submission(form, click=("action", "enregistrer")))
            )

        self.assertEqual(
            list(StockType.objects.filter(count_in_products_margin=True).values_list("pk", flat=True)), [self.towels.pk]
        )

    def test_a_page_left_open_does_not_undo_another_tab(self):
        """The page is drawn; another tab then ticks Gobelets and unticks
        Essuie-tout. Back on the first page, Consommables' boxes are left as
        drawn and « Enregistrer » is clicked: nothing the person did not
        touch on this page moves back."""
        parser = forms_of(self.page().content.decode())
        StockType.objects.filter(pk=self.cups.pk).update(count_in_products_margin=True)
        StockType.objects.filter(pk=self.towels.pk).update(count_in_products_margin=False)

        pairs = submission(form_with(parser, name="categorie", value="Consommables"), click=("action", "enregistrer"))
        self.browser.post(reverse("margins:count_articles"), as_post(pairs))

        self.assertEqual(ticked(self.towels, self.cups), [False, True])

    def test_tout_cocher_clicked_on_the_page(self):
        parser = forms_of(self.page().content.decode())
        pairs = submission(form_with(parser, name="categorie", value="Consommables"), click=("action", "cocher"))

        response = self.browser.post(reverse("margins:count_articles"), as_post(pairs), follow=True)

        self.assertEqual(ticked(self.towels, self.cups), [True, True])
        self.assertEqual(ticked(self.drill, self.bench, self.syrup, self.rum), [False, False, False, False])
        self.assertIn(
            "1 article de Consommables compté dans la marge produits.",
            [str(message) for message in response.context["panel_messages"]],
        )

    def test_a_button_drawn_disabled_cannot_be_clicked(self):
        """Nothing ticked in Matériel: « Tout décocher » is disabled, and a
        browser sends no disabled button."""
        parser = forms_of(self.page().content.decode())

        with self.assertRaises(AssertionError):
            submission(form_with(parser, name="categorie", value="Matériel"), click=("action", "decocher"))


class RecalculerAsTheBrowserSendsItTests(SelectionFixture, TestCase):
    """« Ce qui a été facturé »: the GET form read off the page."""

    def recalculer(self, html: str, *, untick=()):
        form = form_of_class(forms_of(html), "spend-selection")
        pairs = submission(form, flip=[("garder", key) for key in untick])
        return self.client.get(f"{form['attrs']['action']}?{urlencode(pairs)}")

    def test_unticking_an_article_on_the_page_leaves_it_out(self):
        response = self.recalculer(self.html(), untick=[article_key(self.drill.pk)])

        self.assertEqual(response.status_code, 302)
        query = QueryDict(urlsplit(response["Location"]).query)
        self.assertEqual(query.getlist("sans"), [article_key(self.drill.pk)])
        self.assertEqual(query["du"], MARCH["du"])
        page = self.client.get(response["Location"]).content.decode()
        self.assertEqual(value_of(stat_of(page, "Marge réelle — sans : Perceuse sans fil")), "375.00 €")

    def test_what_is_left_out_and_not_on_the_table_survives_recalculer(self):
        """Consignes were only bought in April: over March they have no row,
        so only the form's hidden fields can carry them through - and
        unticking something else must not put them back."""
        html = self.html(category_key("Consignes"), CHARGES_KEY)

        response = self.recalculer(html, untick=[article_key(self.drill.pk)])

        self.assertEqual(
            QueryDict(urlsplit(response["Location"]).query).getlist("sans"),
            [category_key("Consignes"), CHARGES_KEY, article_key(self.drill.pk)],
        )

    def test_ticking_back_on_the_page_puts_it_back(self):
        html = self.html(category_key("Matériel"))

        response = self.recalculer(html, untick=[category_key("Matériel")])

        self.assertEqual(QueryDict(urlsplit(response["Location"]).query).getlist("sans"), [])

    def test_recalculer_untouched_changes_nothing(self):
        keys = [category_key("Matériel"), article_key(self.rum.pk), category_key("")]
        response = self.recalculer(self.html(*keys))

        # The same keys, in the same order: in the table's order, « sans :
        # Matériel, Rhum » came back « sans : Rhum, Matériel ».
        self.assertEqual(QueryDict(urlsplit(response["Location"]).query).getlist("sans"), keys)

    def test_the_period_form_carries_the_selection(self):
        form = form_of_class(forms_of(self.html(CHARGES_KEY)), "date-range")
        pairs = submission(form)

        self.assertIn(("sans", CHARGES_KEY), pairs)
        self.assertIn(("du", MARCH["du"]), pairs)

    def test_links_to_other_pages_carry_the_period_and_not_the_selection(self):
        """None of those pages reads `sans`: carried there, it would be a
        parameter that means nothing, and travels on from their links."""
        response = self.get(category_key("Matériel"), CHARGES_KEY)

        for name in ("documents_url", "purchases_url", "sales_url"):
            with self.subTest(link=name):
                query = QueryDict(urlsplit(response.context[name]).query)
                self.assertEqual(query["du"], MARCH["du"])
                self.assertEqual(query["au"], MARCH["au"])
                self.assertNotIn("sans", query)


class ReadingOrderTests(TestCase):
    """The panel is a list someone looks a category up in: sorted as a
    person reads, not as SQLite compares bytes (« Épicerie » after « vins »)."""

    def test_categories_and_articles_ignore_accents_and_case(self):
        for name, category in (
            ("Zeste de citron", "Épicerie"),
            ("abricots secs", "Épicerie"),
            ("éponges", "Épicerie"),
            ("Blanche", "Bières"),
            ("Rosé", "vins"),
            ("Ficelle", ""),
        ):
            make_stock_type(name=name, category=category)

        report = margins_for(DateRange(date(2026, 3, 1), date(2026, 3, 31)))

        self.assertEqual([holder.label for holder in report.countable], ["Bières", "Épicerie", "vins", NO_CATEGORY])
        grocery = next(holder for holder in report.countable if holder.name == "Épicerie")
        self.assertEqual([article.name for article in grocery.articles], ["abricots secs", "éponges", "Zeste de citron"])


class DutyOverReturnsOnlyTests(TestCase):
    """An invoice whose every line is a return, with an adjustment on top:
    with no line bought, the lines weigh by their size, and the euro still
    lands once."""

    def test_the_adjustment_is_spread_by_size_when_no_line_is_positive(self):
        keg = make_stock_type(name="Consigne de fût", category="Consignes")
        crate = make_stock_type(name="Consigne de caisse", category="Casiers")
        invoice = make_invoice(invoice_date=date(2026, 3, 4), reconciliation_adjustment=Decimal("4.00"))
        for article, amount in ((keg, "-30.00"), (crate, "-10.00")):
            make_invoice_line(
                invoice=invoice,
                product=make_product(supplier=invoice.supplier, stock_type=article),
                total_ht=amount,
                vat_rate=Decimal("0.20"),
            )

        report = margins_for(DateRange(date(2026, 3, 1), date(2026, 3, 31)))

        places = {group.key: group.money for group in report.spend_groups}
        self.assertEqual(places[category_key("Consignes")].ht, Decimal("-27.00"))
        self.assertEqual(places[category_key("Casiers")].ht, Decimal("-9.00"))
        self.assertEqual(sum(places.values(), start=Money()), report.spend)
