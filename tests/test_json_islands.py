"""Data handed to a page's script travels in a JSON island built by Django's
`json_script`, never by `json.dumps` printed with `|safe` (security audit
XSS-1 / LB-2, 29/09/2026).

`json.dumps` leaves « < » as it is. The inventory form printed every product
and article name that way inside ``<script type="application/json">``: a
name holding « </script> » closed the element early and the rest of it ran
as the page's own markup, in the bar's logged-in session. Product names come
from documents outsiders write (a supplier's PDF, an e-invoice, a photo read
by OCR), articles from a « Données » archive too. `json_script` writes « < »,
« > » and « & » as \\u escapes, which JSON.parse reads back unchanged.

Here: every spot, with a name holding the payload - the page carries no
live element and the island reads back to the same data - and a sweep of the
templates, so the pattern does not come back.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from inventory.forms import product_display_name, stock_take_entry_lookup, stock_type_entry_name
from inventory.models import UnitChoices
from tests.factories import (
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)

#: What a name planted in a document would carry.
PAYLOAD = "</script><img src=x onerror=alert(document.domain)>"


class _Elements(HTMLParser):
    """Every start tag of a page, as a browser's tokenizer sees it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def live_handlers(page: str) -> list[tuple[str, dict]]:
    """The elements of `page` carrying an inline event handler (on…=)."""
    parser = _Elements()
    parser.feed(page)
    return [(tag, attrs) for tag, attrs in parser.tags if any(name.startswith("on") for name in attrs)]


def island(page: str, element_id: str):
    """The JSON island `element_id` of `page`, parsed as the page's script
    parses it (JSON.parse of the element's text)."""
    found = re.search(rf'<script id="{re.escape(element_id)}" type="application/json">(.*?)</script>', page, re.DOTALL)
    assert found, f"no island {element_id!r} on the page"
    return json.loads(found.group(1))


class NoLiveMarkupMixin:
    def assertNoBreakout(self, page: str):
        self.assertNotIn("</script><img", page)
        self.assertEqual(live_handlers(page), [])


class StockTakeFormTests(NoLiveMarkupMixin, TestCase):
    """The inventory form (inventory/templates/inventory/stock_take_form.html):
    `entry-data` holds every name that can be counted - a product's raw
    name WITH its supplier's name, and each article's - and `saved-values`
    what the saved lines are worth."""

    def setUp(self):
        self.supplier = make_supplier(name=f"Grossiste {PAYLOAD}")
        self.article = make_stock_type(name=f"Rhum{PAYLOAD}", unit=UnitChoices.LITRE)
        self.product = make_product(supplier=self.supplier, raw_name=f"SIROP {PAYLOAD} 1L", stock_type=self.article)

    def test_a_new_inventory_carries_the_names_as_data_only(self):
        page = self.client.get(reverse("inventory:stock_take_create")).content.decode()
        self.assertNoBreakout(page)
        entries = island(page, "entry-data")
        self.assertEqual(set(entries), set(stock_take_entry_lookup()))
        self.assertIn(product_display_name(self.product), entries)
        self.assertIn(stock_type_entry_name(self.article), entries)
        self.assertEqual(entries[product_display_name(self.product)]["kind"], "product")

    def test_an_inventory_opened_again_carries_its_saved_values_as_data_only(self):
        stock_take = make_stock_take()
        line = make_stock_take_line(stock_take=stock_take, product=self.product, counted_quantity="2", value_ht="12.34")
        page = self.client.get(reverse("inventory:stock_take_update", args=[stock_take.pk])).content.decode()
        self.assertNoBreakout(page)
        self.assertEqual(island(page, "saved-values"), {str(line.pk): "12.34"})
        self.assertIn(product_display_name(self.product), island(page, "entry-data"))


class RecipeFormTests(NoLiveMarkupMixin, TestCase):
    """The recipe form's `ingredient-units-data` (recipes/templates/recipes/
    recipe_form.html): the unit shown beside an ingredient's quantity. Its
    labels are the units' own today, but it was an island written by hand;
    it is `json_script`'s now, like every other."""

    def test_the_units_are_data_only(self):
        units = {"stock:1": f"Litre{PAYLOAD}", "recipe:2": "Kilogramme"}
        with mock.patch("recipes.views.ingredient_unit_map", return_value=units):
            page = self.client.get(reverse("recipes:recipe_create")).content.decode()
        self.assertNoBreakout(page)
        self.assertEqual(island(page, "ingredient-units-data"), units)


def template_sources():
    base = Path(settings.BASE_DIR)
    for pattern in ("*/templates/**/*.html", "templates/**/*.html"):
        for path in base.glob(pattern):
            yield path.relative_to(base).as_posix(), path.read_text(encoding="utf-8")


class SweepTests(SimpleTestCase):
    """The rule, over every template of the application."""

    def test_no_island_is_written_by_hand(self):
        """A `<script type="application/json">` in a template is one built
        by hand - `json_script` writes its own."""
        found = [
            name
            for name, source in template_sources()
            if re.search(r"<script\b[^>]*type=[\"']application/(?:ld\+)?json", source)
        ]
        self.assertEqual(found, [])

    def test_nothing_is_printed_raw_inside_a_script(self):
        """`|safe` (or `{% autoescape off %}`) inside a <script> element
        prints markup straight into the script: a « </script> » in the
        value ends it."""
        found = []
        for name, source in template_sources():
            for script in re.findall(r"<script\b[^>]*>(.*?)</script>", source, re.DOTALL):
                if re.search(r"\|\s*safe\b|autoescape\s+off", script):
                    found.append(name)
        self.assertEqual(found, [])

    def test_no_view_hands_a_template_a_json_string(self):
        """`json.dumps` is for a header (HX-Trigger), a file or a hash:
        never a value of a template's context."""
        base = Path(settings.BASE_DIR)
        found = sorted(
            (path.relative_to(base).as_posix(), key)
            for path in base.glob("*/views.py")
            for key in re.findall(r'"([a-z_]+)":\s*json\.dumps\(', path.read_text(encoding="utf-8"))
        )
        # The one left: transfer's picker hands each checkbox the names of
        # the sections that tick it, in a data- attribute - autoescaped, and
        # the names are the application's own (transfer.registry), not a
        # bar's. An attribute, not a script.
        self.assertEqual(found, [("transfer/views.py", "forced_by")])
