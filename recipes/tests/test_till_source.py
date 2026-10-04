"""One till source key, named once; the sources shown as words.

`recipes.sales.TILL_SOURCE` (« laddition ») is the till's key for EVERY
connector - L'Addition fetched, a file of any till uploaded: the per-recipe
till sales are rebuilt from source-less tables, Marges reads any other key as
typed by hand, and a second till key would count twice and be turned back
into « laddition » by a « Données » round trip (recipes/sales.py). So no
writer may spell a source of its own: the guard below reads the code. And the
key is stored, never shown: « Caisse » and « Saisie à la main » on the page.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from margins import computation
from recipes import forms, sales
from recipes.models import RecipeSale
from recipes.sales import MANUAL_SALE_SOURCE, SOURCE_LABELS, TILL_SOURCE, source_label, sources_named
from tests.factories import make_recipe
from transfer.sections import till_links

#: Calls that write or read a sale by its source: RecipeSale(...),
#: RecipeSale.objects.<anything>(...), record_sales(...) - and any row
#: written with a literal source (`WRITES`), whatever reaches it: a recipe's
#: own manager (`recipe.sales.update_or_create(source=...)`) names no
#: RecipeSale. A `source=` literal anywhere else (a staged archive's
#: « envoi ») is not a sale's.
SALE_CALLS = {"RecipeSale", "record_sales"}
WRITES = {"create", "get_or_create", "update_or_create", "bulk_create"}


def _root_name(node) -> str:
    """The name a call's callee starts from: RecipeSale.objects.filter -> RecipeSale."""
    while isinstance(node, ast.Attribute):
        if node.attr in SALE_CALLS:
            return node.attr
        node = node.value
    while isinstance(node, ast.Call):
        node = node.func
        while isinstance(node, ast.Attribute):
            node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def spelt_sources(tree) -> list[int]:
    """The lines of `tree` where a sale's call spells its source as a
    string literal."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        writes = isinstance(node.func, ast.Attribute) and node.func.attr in WRITES
        if not writes and _root_name(node.func) not in SALE_CALLS:
            continue
        for keyword in node.keywords:
            if keyword.arg == "source" and isinstance(keyword.value, ast.Constant):
                lines.append(node.lineno)
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name == "record_sales" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            lines.append(node.lineno)
    return lines


class OneKeyTests(SimpleTestCase):
    def test_every_reader_of_the_till_rows_names_the_one_key(self):
        self.assertEqual(TILL_SOURCE, "laddition")
        self.assertEqual(computation.TILL_SOURCE, TILL_SOURCE)
        self.assertEqual(till_links.LADDITION, TILL_SOURCE)

    def test_the_manual_key_has_one_definition_re_exported(self):
        self.assertIs(forms.MANUAL_SALE_SOURCE, sales.MANUAL_SALE_SOURCE)
        self.assertEqual(MANUAL_SALE_SOURCE, "manual")

    def test_no_writer_spells_a_source_of_its_own(self):
        found = []
        for path in Path(settings.BASE_DIR).rglob("*.py"):
            parts = set(path.relative_to(settings.BASE_DIR).parts)
            if parts & {".venv", "tests", "migrations", "node_modules"} or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found += [f"{path.relative_to(settings.BASE_DIR)}:{line}" for line in spelt_sources(tree)]
        self.assertEqual(found, [], "a sale's source is TILL_SOURCE or MANUAL_SALE_SOURCE, never a literal")

    def test_the_guard_sees_what_it_is_for(self):
        tree = ast.parse(
            "record_sales(entries, source='laddition')\n"
            "record_sales(entries, 'caisse')\n"
            "RecipeSale(recipe=r, source='api')\n"
            "RecipeSale.objects.update_or_create(recipe=r, source='csv')\n"
            "RecipeSale.objects.filter(source='laddition').exclude(sold_on__in=x)\n"
            "recipe.sales.update_or_create(sold_on=day, source='api')\n"
            "recipe.sales.create(source='csv', quantity=1)\n"
            "anything.objects.get_or_create(source='caisse')\n"
            "_staged(token, path, source='envoi')\n"
            "record_sales(entries, source=TILL_SOURCE)\n"
            "recipe.sales.update_or_create(sold_on=day, source=MANUAL_SALE_SOURCE)\n"
        )
        self.assertEqual(sorted(spelt_sources(tree)), [1, 2, 3, 4, 5, 6, 7, 8])


class LabelsTests(SimpleTestCase):
    def test_the_two_sources_are_words(self):
        self.assertEqual(source_label(TILL_SOURCE), "Caisse")
        self.assertEqual(source_label(MANUAL_SALE_SOURCE), "Saisie à la main")

    def test_a_key_nobody_named_is_shown_as_stored(self):
        self.assertEqual(source_label("api"), "api")
        self.assertEqual(source_label(""), "")

    def test_a_search_names_the_sources_by_their_words(self):
        self.assertEqual(sources_named("caisse"), [TILL_SOURCE])
        self.assertEqual(sources_named("SAISIE"), [MANUAL_SALE_SOURCE])
        self.assertEqual(sources_named("à la main"), [MANUAL_SALE_SOURCE])
        self.assertEqual(sources_named("a la main"), [MANUAL_SALE_SOURCE])
        self.assertEqual(sources_named("Mule"), [])
        self.assertEqual(sources_named("  "), [])
        self.assertEqual(set(SOURCE_LABELS), {TILL_SOURCE, MANUAL_SALE_SOURCE})


class SalesTabWordsTests(TestCase):
    def setUp(self):
        self.mule = make_recipe(name="Mule")
        RecipeSale.objects.create(recipe=self.mule, sold_on=date(2026, 2, 10), quantity=4, source=TILL_SOURCE)
        RecipeSale.objects.create(recipe=self.mule, sold_on=date(2026, 2, 11), quantity=6, source=MANUAL_SALE_SOURCE)

    def page(self, **params):
        return self.client.get(reverse("recipes:sales_list"), params)

    def test_par_origine_and_the_list_say_words_never_the_keys(self):
        response = self.page()
        page = response.content.decode()
        self.assertIn("<td>Caisse</td>", page)
        self.assertIn("<td>Saisie à la main</td>", page)
        self.assertIn('<td class="muted">Caisse</td>', page)
        self.assertIn('<td class="muted">Saisie à la main</td>', page)
        self.assertNotIn(">laddition<", page)
        self.assertNotIn(">manual<", page)
        self.assertIn("origine « Saisie à la main »", page)
        # The rows keep their stored key beside the words.
        self.assertEqual(
            {row["source"]: row["label"] for row in response.context["totals"]},
            {TILL_SOURCE: "Caisse", MANUAL_SALE_SOURCE: "Saisie à la main"},
        )

    def test_a_search_for_the_words_finds_the_rows(self):
        self.assertEqual([sale.quantity for sale in self.page(vente="caisse").context["sales"]], [4])
        self.assertEqual([sale.quantity for sale in self.page(vente="main").context["sales"]], [6])
        # As stored, too: a key is no secret, only no word to show.
        self.assertEqual([sale.quantity for sale in self.page(vente="laddition").context["sales"]], [4])


class WrittenAsNamedEscapesTests(SimpleTestCase):
    """A no-break space, a tab or a byte order mark written as the character
    itself is invisible to whoever reviews the code (CLAUDE.md, « An
    invisible character is written as a named escape »): the till's code -
    every file of the app, and its « Données » section - writes them as
    escapes."""

    INVISIBLE = {chr(code) for code in (0x09, 0xA0, 0x202F, 0x2007, 0x2009, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)}

    def test_no_invisible_character_is_written_as_itself(self):
        base = Path(settings.BASE_DIR)
        files = [path for path in (base / "recipes").rglob("*") if path.suffix in (".py", ".html", ".js")]
        files += [
            base / "transfer" / "sections" / "till_formats.py",
            base / "transfer" / "tests" / "test_till_formats_section.py",
        ]
        found = []
        for path in files:
            for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if self.INVISIBLE & set(text):
                    found.append(f"{path.relative_to(base)}:{number}")
        self.assertEqual(found, [])
