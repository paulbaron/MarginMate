"""Tests for the shared UI treatment.

The searching and sorting themselves are done in the browser by
static/js/datatable.js, which these can't execute. What they CAN pin down is
the contract that script relies on - and every one of these has a failure mode
that looks fine on a page you happen to be looking at and is broken on one you
aren't:

  * a table without `data-table` silently has no search box and no sortable
    headers, and nothing about the page looks wrong;
  * a date cell without `data-sort` sorts as the text "31/03/2026", so
    everything from March lands together regardless of year;
  * a continuation row without `data-child-row` is treated as a row in its own
    right and gets separated from the row it explains the moment you sort.
"""

from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import UnitChoices
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_priced_stock_type,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_supplier,
)


class BaseTemplateTests(TestCase):
    def test_the_table_script_is_loaded_everywhere(self):
        """Every enhanced table depends on it, so it belongs in the base
        template rather than being remembered per page."""
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "js/datatable.js")

    def test_the_stylesheet_is_loaded(self):
        self.assertContains(self.client.get(reverse("inventory:stock_list")), "css/marginmate.css")


class SearchableSortableTableTests(TestCase):
    """Every list page gets a search box and sortable columns."""

    @classmethod
    def setUpTestData(cls):
        supplier = make_supplier(name="Metro")
        stock_type = make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        product = make_product(supplier=supplier, raw_name="VODKA 70CL", stock_type=stock_type)
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 3, 31))
        make_invoice_line(invoice=invoice, product=product, quantity=6, total_ht="72")

        recipe = make_recipe(name="Vodka tonic", selling_price_ttc="8.50")
        make_ingredient(recipe, stock_type=stock_type, quantity="0.04", group=0)
        record_sales([("Vodka tonic", date(2026, 3, 15), 20)])

        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(
            stock_take=take, stock_type=stock_type, counted_quantity="4", unit=UnitChoices.LITRE
        )
        cls.take = take

    def assertEnhancedTable(self, url_name, **kwargs):
        response = self.client.get(reverse(url_name, kwargs=kwargs) if kwargs else reverse(url_name))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "data-table",
            msg_prefix=f"{url_name} has a table with no search or sorting",
        )
        return response

    def test_invoice_list(self):
        self.assertEnhancedTable("invoices:invoice_list")

    def test_invoice_detail(self):
        from invoices.models import Invoice

        self.assertEnhancedTable("invoices:invoice_detail", pk=Invoice.objects.get().pk)

    def test_invoice_type_list(self):
        from tests.factories import make_invoice_type

        make_invoice_type(name="UBA")
        self.assertEnhancedTable("invoices:invoice_type_list")

    def test_recipe_list(self):
        self.assertEnhancedTable("recipes:recipe_list")

    def test_sales_list(self):
        self.assertEnhancedTable("recipes:sales_list")

    def test_pos_product_list(self):
        from recipes.models import PosProduct

        PosProduct.objects.create(name="Mule", total_quantity=12)
        self.assertEnhancedTable("recipes:pos_product_list")

    def test_stock_take_list(self):
        self.assertEnhancedTable("inventory:stock_take_list")

    def test_stock_take_detail(self):
        self.assertEnhancedTable("inventory:stock_take_detail", pk=self.take.pk)

    def test_stock_list_sorts_without_a_second_search_box(self):
        """It already has a server-backed fuzzy search; a second box filtering
        the same rows by a different rule would be worse than none."""
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "data-table-sort-only")
        self.assertContains(response, 'id="stock-search"')


class SortKeyTests(TestCase):
    """Cells whose displayed text doesn't sort correctly must say what does."""

    def test_invoice_dates_carry_an_iso_sort_key(self):
        supplier = make_supplier()
        product = make_product(supplier=supplier)
        for day in (date(2026, 3, 31), date(2025, 4, 1)):
            invoice = make_invoice(supplier=supplier, invoice_date=day)
            make_invoice_line(invoice=invoice, product=product, quantity=1, total_ht="10")

        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(response, 'data-sort="2026-03-31"')
        self.assertContains(response, 'data-sort="2025-04-01"')
        # ...and still READS as a French date.
        self.assertContains(response, "31/03/2026")

    def test_sale_dates_carry_an_iso_sort_key(self):
        make_recipe(name="Mule")
        record_sales([("Mule", date(2026, 3, 5), 4)])
        response = self.client.get(reverse("recipes:sales_list"))
        self.assertContains(response, 'data-sort="2026-03-05"')
        self.assertContains(response, "05/03/2026")


class ChildRowTests(TestCase):
    """Rows that explain the row above them have to travel with it."""

    def test_stock_list_detail_rows_are_marked_as_children(self):
        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "data-child-row")

    def test_the_variance_valuation_note_is_marked_as_a_child(self):
        """"Valorisé en Rhum (…)" belongs to the pool row above it; sorted
        apart, it would sit under an unrelated group and read as its
        valuation."""
        supplier = make_supplier()
        stock_type = make_priced_stock_type(name="Rhum", unit_cost_ht="15", quantity="100")
        make_product(supplier=supplier, stock_type=stock_type, unit=UnitChoices.UNIT, stock_equivalent="0.7")

        opening = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        make_stock_take_line(
            stock_take=opening, stock_type=stock_type, counted_quantity="20", unit=UnitChoices.LITRE
        )
        closing = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(
            stock_take=closing, stock_type=stock_type, counted_quantity="15", unit=UnitChoices.LITRE
        )

        response = self.client.get(reverse("inventory:stock_take_variance", kwargs={"pk": closing.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-child-row")


class PageChromeTests(TestCase):
    def test_list_pages_explain_themselves(self):
        """A subtitle saying what the page is for - this is a tool someone
        comes back to monthly, not daily."""
        for url_name in (
            "inventory:stock_list",
            "invoices:invoice_list",
            "recipes:recipe_list",
            "recipes:sales_list",
            "inventory:stock_take_list",
        ):
            with self.subTest(page=url_name):
                self.assertContains(self.client.get(reverse(url_name)), "page-subtitle")

    def test_the_stock_page_leads_with_its_headline_figures(self):
        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "stat-value")
        self.assertContains(response, "Valeur du stock (HT)")

    def test_an_empty_list_offers_the_next_step(self):
        """An empty state that only says "nothing here" leaves the reader to
        work out what to do about it."""
        response = self.client.get(reverse("recipes:recipe_list"))
        self.assertContains(response, "empty-state")
        self.assertContains(response, reverse("recipes:recipe_create"))

    def test_action_buttons_use_the_shared_class(self):
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(response, 'class="actions"')
        self.assertNotContains(response, 'style="display:flex; gap:0.5rem;"')


class TemplateHygieneTests(TestCase):
    """Static checks over the templates themselves.

    Django's `{# … #}` comment is SINGLE-LINE ONLY. Spread one over several
    lines and Django doesn't treat it as a comment at all - it renders the
    whole thing to the page as visible text. It looks completely normal in the
    editor, which is why it has now shipped twice.
    """

    def template_files(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        return [
            path
            for path in root.rglob("*.html")
            if ".venv" not in path.parts and "staticfiles" not in path.parts
        ]

    def test_no_multiline_hash_comments(self):
        import re

        offenders = []
        for path in self.template_files():
            for match in re.finditer(r"\{#.*?#\}", path.read_text(encoding="utf-8"), re.S):
                if "\n" in match.group(0):
                    offenders.append(f"{path.name}: {match.group(0)[:60]}…")
        self.assertEqual(
            offenders,
            [],
            "Multi-line {# #} renders as visible text - use {% comment %} instead:\n"
            + "\n".join(offenders),
        )

    def test_every_template_is_syntactically_valid(self):
        """A template that only renders on one rarely-visited page still
        fails at import time here rather than in front of someone."""
        from django.template.loader import get_template
        from django.template import TemplateSyntaxError

        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        for path in self.template_files():
            # Name it the way the loader will look it up.
            for base in ("templates", *[f"{app}/templates" for app in ("inventory", "invoices", "recipes")]):
                candidate = root / base
                if candidate in path.parents:
                    name = str(path.relative_to(candidate)).replace("\\", "/")
                    break
            else:
                continue
            with self.subTest(template=name):
                try:
                    get_template(name)
                except TemplateSyntaxError as exc:
                    self.fail(f"{name}: {exc}")


class JobConsoleTests(TestCase):
    """The shared console partial. Both job kinds render through it, so the
    log stops being two near-identical blocks of markup that drift apart."""

    def render(self, **job_kwargs):
        from django.template.loader import render_to_string
        from invoices.models import ScrapeJob

        job = ScrapeJob.objects.create(**job_kwargs)
        job.log = "[+  0.1s] Connexion\n[+  4.2s] 12 175 emails analysés\n"
        job.save(update_fields=["log"])
        return render_to_string(
            "_job_console.html",
            {"log": job.log, "running": job.is_active, "last_line": job.last_log_line,
             "log_lines": job.log_lines, "console_id": "test-log"},
        ), job

    def test_the_log_is_collapsed_behind_a_disclosure(self):
        """A wall of scanner output as the first thing on the page is what
        made these alarming."""
        html, _job = self.render(status="SUCCESS")
        self.assertIn("<details", html)
        self.assertNotIn("<details open", html)
        self.assertIn("Journal technique", html)

    def test_the_full_log_is_still_there(self):
        html, _job = self.render(status="SUCCESS")
        self.assertIn("12 175 emails analysés", html)

    def test_it_counts_the_lines(self):
        html, _job = self.render(status="SUCCESS")
        self.assertIn("2 lignes", html)

    def test_a_running_job_shows_its_last_line_in_plain_sight(self):
        html, _job = self.render(status="RUNNING")
        self.assertIn("job-spinner", html)
        self.assertIn("12 175 emails analysés", html.split("<details")[0])

    def test_a_finished_job_shows_no_spinner(self):
        html, _job = self.render(status="SUCCESS")
        self.assertNotIn("job-spinner", html)

    def test_the_elapsed_prefix_is_stripped_from_the_live_line(self):
        """"[+  4.2s]" is useful in the log and noise on the one line shown
        as a live status, where the spinner already says "running"."""
        _html, job = self.render(status="RUNNING")
        self.assertEqual(job.last_log_line, "12 175 emails analysés")

    def test_nothing_renders_without_a_log(self):
        from django.template.loader import render_to_string

        self.assertEqual(render_to_string("_job_console.html", {"log": ""}).strip(), "")

    def test_both_job_models_answer_the_same_questions(self):
        """The partial is shared, so ScrapeJob and SalesImportJob both have
        to expose is_active / log_lines / last_log_line."""
        from invoices.models import ScrapeJob
        from recipes.models import SalesImportJob

        for model in (ScrapeJob, SalesImportJob):
            with self.subTest(model=model.__name__):
                job = model.objects.create(status="RUNNING")
                job.log = "[+  1.0s] une ligne\n"
                self.assertTrue(job.is_active)
                self.assertEqual(job.log_lines, 1)
                self.assertEqual(job.last_log_line, "une ligne")


class PersistedStateTests(TestCase):
    """Markup that tells ui.js what to remember across a reload."""

    def test_the_invoice_sources_remember_their_ticks(self):
        response = self.client.get(reverse("invoices:invoice_list"))
        html = response.content.decode()
        self.assertIn("data-persist-durable", html)
        self.assertIn('data-persist="sources-open"', html)

    def test_a_source_checkbox_carries_its_own_key(self):
        from tests.factories import make_supplier

        make_supplier(code="METRO", name="Metro", parser_key="METRO", is_scrapable=True)
        html = self.client.get(reverse("invoices:invoice_list")).content.decode()
        self.assertIn('data-persist="source:METRO"', html)


class ChartMarkupTests(TestCase):
    """The charts are server-rendered SVG and readable without JavaScript;
    charts.js only adds the hover readout. It needs the values in the markup
    to do that - the browser's own <title> tooltip is too slow to be useful."""

    def test_the_pie_carries_a_label_and_value_per_slice(self):
        from recipes.views import _build_ingredient_pie_svg
        from decimal import Decimal

        class FakeIngredient:
            source_name = "Vodka"

        html = _build_ingredient_pie_svg(
            [
                {"ingredient": FakeIngredient(), "name": "Vodka", "cost_ht": Decimal("3")},
                {"ingredient": FakeIngredient(), "name": "Gin", "cost_ht": Decimal("1")},
            ]
        )
        self.assertIn('data-chart="pie"', html)
        self.assertIn('data-label="Vodka"', html)
        self.assertIn("75.0 %", html)
        self.assertIn("chart-legend", html)

    def test_a_single_ingredient_is_drawn_as_a_full_circle(self):
        """An arc from a point back to itself collapses to nothing."""
        from recipes.views import _build_ingredient_pie_svg
        from decimal import Decimal

        class FakeIngredient:
            source_name = "Vodka"

        html = _build_ingredient_pie_svg(
            [{"ingredient": FakeIngredient(), "name": "Vodka", "cost_ht": Decimal("3")}]
        )
        self.assertIn("<circle", html)
        self.assertIn("100.0 %", html)

    def test_slice_names_are_escaped(self):
        """Rendered with |safe, and the names come from invoice text."""
        from recipes.views import _build_ingredient_pie_svg
        from decimal import Decimal

        class FakeIngredient:
            source_name = '<img src=x onerror=alert(1)>'

        html = _build_ingredient_pie_svg(
            [{"ingredient": FakeIngredient(), "cost_ht": Decimal("3")}]
        )
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_the_price_chart_carries_a_point_per_reading(self):
        from inventory.views import _build_price_history_svg
        from datetime import date
        from decimal import Decimal

        html = _build_price_history_svg(
            [(date(2026, 1, 1), Decimal("1.50")), (date(2026, 2, 1), Decimal("1.80"))]
        )
        self.assertIn('data-chart="line"', html)
        self.assertEqual(html.count("chart-point"), 2)
        self.assertIn('data-label="01/01/2026"', html)
        self.assertIn("chart-hover-line", html)

    def test_every_css_variable_used_anywhere_is_defined(self):
        """A var() that resolves to nothing doesn't error - it just silently
        draws nothing, which is how the price chart's axes went invisible
        after the stylesheet renamed a token."""
        import glob
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        css = (root / "static/css/marginmate.css").read_text(encoding="utf-8")
        defined = set(re.findall(r"(--[\w-]+)\s*:", css))

        used = {}
        patterns = ("static/css/*.css", "static/js/*.js", "templates/**/*.html",
                    "*/templates/**/*.html", "*/views.py")
        for pattern in patterns:
            for path in glob.glob(str(root / pattern), recursive=True):
                if ".venv" in path:
                    continue
                for name in re.findall(r"var\((--[\w-]+)", pathlib.Path(path).read_text(encoding="utf-8")):
                    used.setdefault(name, set()).add(pathlib.Path(path).name)

        missing = {name: sorted(files) for name, files in used.items() if name not in defined}
        self.assertEqual(missing, {}, f"CSS variables used but never defined: {missing}")

    def test_the_axes_use_a_colour_that_actually_exists(self):
        """They were drawn with var(--panel-border) after the stylesheet
        renamed it, which made them invisible."""
        import pathlib
        from inventory.views import _build_price_history_svg
        from datetime import date
        from decimal import Decimal
        import re

        html = _build_price_history_svg(
            [(date(2026, 1, 1), Decimal("1.50")), (date(2026, 2, 1), Decimal("1.80"))]
        )
        css = (pathlib.Path(__file__).resolve().parent.parent / "static/css/marginmate.css").read_text(
            encoding="utf-8"
        )
        for name in set(re.findall(r"var\((--[\w-]+)\)", html)):
            with self.subTest(variable=name):
                self.assertIn(name + ":", css, f"{name} is used by a chart but not defined in the CSS")

    def test_too_few_points_render_nothing(self):
        from inventory.views import _build_price_history_svg
        from datetime import date
        from decimal import Decimal

        self.assertEqual(_build_price_history_svg([(date(2026, 1, 1), Decimal("1.50"))]), "")


class FormRenderingTests(TestCase):
    """Every form goes through templates/_form_fields.html.

    The field loop used to be copy-pasted into seven templates and had
    drifted: some rendered each field's help text, some didn't - and the ones
    that didn't were the recipe and stock-item forms, where the models define
    the most useful help ("Ex : 0.20 pour 20%").
    """

    FORM_PAGES = [
        ("inventory:stock_type_create", {}),
        ("inventory:stock_take_create", {}),
        ("invoices:invoice_upload", {}),
        ("invoices:invoice_create_manual", {}),
        ("invoices:invoice_type_create", {}),
        ("recipes:recipe_create", {}),
    ]

    def test_every_form_page_uses_the_shared_layout(self):
        for name, kwargs in self.FORM_PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name, kwargs=kwargs)).content.decode()
                self.assertIn("form-grid", html)

    def test_no_template_still_hand_rolls_a_field_loop(self):
        """`{{ field.label_tag }}` was the giveaway of the copy-pasted block.
        Looked for literally rather than by parsing loops: a cleverer check
        kept flagging `{% for error in line_form.errors %}`, which is a
        different job and perfectly fine."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = [
            path.name
            for path in root.rglob("*.html")
            if ".venv" not in path.parts and "label_tag" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], f"These still render fields by hand: {offenders}")

    def test_help_text_reaches_the_page(self):
        html = self.client.get(reverse("recipes:recipe_create")).content.decode()
        self.assertIn("Ex : 0.20 pour 20%", html)
        self.assertIn('class="help"', html)

    def test_required_fields_are_marked(self):
        html = self.client.get(reverse("recipes:recipe_create")).content.decode()
        self.assertIn('class="required"', html)

    def test_a_field_with_an_error_is_flagged(self):
        response = self.client.post(reverse("inventory:stock_type_create"), {"name": "", "unit": ""})
        self.assertContains(response, "has-error")
        self.assertContains(response, "field-error")

    def test_hidden_fields_are_rendered_but_not_labelled(self):
        """A hidden input inside a .form-field would leave an empty labelled
        box on the page."""
        from django import forms
        from django.template.loader import render_to_string

        class Sample(forms.Form):
            visible = forms.CharField()
            secret = forms.CharField(widget=forms.HiddenInput())

        html = render_to_string("_form_fields.html", {"form": Sample()})
        self.assertIn('name="secret"', html)
        self.assertEqual(html.count("form-field "), html.count("form-field form-field-text"))

    def test_excluded_fields_are_left_out(self):
        """The invoice-type page renders its two date fields beside the
        "Tester" button instead of among the pattern fields."""
        from django import forms
        from django.template.loader import render_to_string

        class Sample(forms.Form):
            keep = forms.CharField()
            drop = forms.CharField()

        html = render_to_string("_form_fields.html", {"form": Sample(), "exclude": ["drop"]})
        self.assertIn('name="keep"', html)
        self.assertNotIn('name="drop"', html)


class AssetVersioningTests(TestCase):
    """`{% asset %}` stamps the file's mtime onto its URL.

    Without it, editing the stylesheet and seeing nothing change looks like
    the change didn't work rather than like it didn't load - which cost two
    debugging detours during this very pass.
    """

    def test_the_url_carries_a_version(self):
        from inventory.templatetags.assets import asset

        url = asset("css/marginmate.css")
        self.assertIn("marginmate.css?v=", url)

    def test_a_missing_file_still_yields_a_usable_url(self):
        from inventory.templatetags.assets import asset

        self.assertEqual(asset("css/does-not-exist.css"), "/static/css/does-not-exist.css")

    def test_the_base_template_uses_it_for_every_local_asset(self):
        html = self.client.get(reverse("inventory:stock_list")).content.decode()
        for name in ("marginmate.css", "ui.js", "datatable.js", "charts.js"):
            with self.subTest(asset=name):
                self.assertIn(name + "?v=", html)


class InvoiceDetailTests(TestCase):
    """The reconciliation adjustment exists precisely so an invoice's total
    matches what the supplier billed - and it was invisible on the one page
    that shows that total."""

    def setUp(self):
        from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

        supplier = make_supplier(code="UBA", name="UBA")
        self.invoice = make_invoice(supplier=supplier, invoice_number="VE-1")
        product = make_product(supplier=supplier, raw_name="BIERE 30L")
        make_invoice_line(
            invoice=self.invoice, product=product, quantity=5, total_ht="100.00",
            vat_rate=Decimal("0.20"), taxes=Decimal("3.50"),
        )

    def url(self):
        return reverse("invoices:invoice_detail", kwargs={"pk": self.invoice.pk})

    def test_the_adjustment_is_shown_and_explained(self):
        self.invoice.reconciliation_adjustment = Decimal("0.30")
        self.invoice.save(update_fields=["reconciliation_adjustment"])
        html = self.client.get(self.url()).content.decode()
        self.assertIn("0,30", html.replace(".", ","))
        self.assertIn("Régularisation", html)
        self.assertIn("droits", html)

    def test_no_adjustment_means_no_extra_rows(self):
        html = self.client.get(self.url()).content.decode()
        self.assertNotIn("Régularisation", html)

    def test_vat_is_shown_as_a_percentage_not_a_fraction(self):
        """0.200 read as a currency amount to everyone who saw it."""
        html = self.client.get(self.url()).content.decode()
        self.assertIn("20 %", html)
        self.assertNotIn("0.200", html)

    def test_the_totals_add_up(self):
        self.invoice.reconciliation_adjustment = Decimal("0.30")
        self.invoice.save(update_fields=["reconciliation_adjustment"])
        self.assertEqual(self.invoice.lines_total_ht, Decimal("100.00"))
        self.assertEqual(self.invoice.total_ht, Decimal("100.30"))
        # 100 at 20% = 120, plus the adjustment, which carries no VAT itself.
        self.assertEqual(self.invoice.total_ttc, Decimal("120.30"))

    def test_ttc_mixes_rates_per_line_rather_than_blending_them(self):
        from tests.factories import make_invoice_line, make_product

        food = make_product(supplier=self.invoice.supplier, raw_name="COMTE")
        make_invoice_line(
            invoice=self.invoice, product=food, quantity=1, total_ht="100.00",
            vat_rate=Decimal("0.055"),
        )
        self.assertEqual(self.invoice.total_ttc, Decimal("120.00") + Decimal("105.50"))

    def test_taxes_and_discounts_are_visible_on_the_line(self):
        """They're why the real unit cost differs from the printed price."""
        html = self.client.get(self.url()).content.decode()
        self.assertIn("de taxes", html)

    def test_the_page_links_back_to_the_list(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn(reverse("invoices:invoice_list"), html)


class StockTakeDetailTests(TestCase):
    def setUp(self):
        from tests.factories import make_stock_take, make_stock_take_line, make_stock_type
        from inventory.models import UnitChoices

        self.take = make_stock_take()
        self.stock_type = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        make_stock_take_line(
            stock_take=self.take, stock_type=self.stock_type,
            counted_quantity="4", unit=UnitChoices.LITRE, value_ht="40",
        )

    def url(self):
        return reverse("inventory:stock_take_detail", kwargs={"pk": self.take.pk})

    def test_the_total_is_a_headline_not_a_footnote(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("stat-value", html)
        self.assertIn("Valeur comptée", html)

    def test_a_shortfall_is_counted_and_explained(self):
        from tests.factories import make_stock_take_line, make_stock_type
        from inventory.models import UnitChoices

        make_stock_take_line(
            stock_take=self.take, stock_type=make_stock_type(name="Gin"),
            counted_quantity="9", unit=UnitChoices.LITRE, value_ht="90",
            has_shortfall=True, shortfall_quantity=Decimal("3"),
        )
        response = self.client.get(self.url())
        self.assertEqual(response.context["shortfall_count"], 1)
        self.assertContains(response, "au-delà")

    def test_no_shortfall_means_no_warning(self):
        response = self.client.get(self.url())
        self.assertEqual(response.context["shortfall_count"], 0)
        self.assertNotContains(response, "stat-warn")

    def test_the_page_links_to_its_variance_report(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn(reverse("inventory:stock_take_variance", kwargs={"pk": self.take.pk}), html)


class LayoutClassTests(TestCase):
    """Classes used by templates but never defined in the stylesheet don't
    error - the element just gets no layout, which is how six button groups
    ended up with none."""

    def test_every_class_a_template_uses_for_layout_is_defined(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        css = (root / "static/css/marginmate.css").read_text(encoding="utf-8")
        defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
        # Only the structural ones: utility and state classes are applied by
        # JavaScript or come from Django, and aren't all styled.
        structural = {"actions", "page-header-actions", "stat-row", "stat", "form-grid",
                      "form-field", "table-wrap", "bulk-bar", "job-console", "chart",
                      "explainer", "lead", "breadcrumb"}
        missing = sorted(name for name in structural if name not in defined)
        self.assertEqual(missing, [], f"Structural classes with no CSS rule: {missing}")
