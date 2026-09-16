"""Saving an inventory: what the BROWSER actually posts.

This formset had no payload test at all, which is how it shipped losing
rows. The three shapes that matter, none of which a hand-written happy-path
payload produces:

* **A removed new row leaves a GAP.** The row is taken out of the DOM but
  TOTAL_FORMS is not decremented, so indices arrive as 0, 1, 3 with
  TOTAL_FORMS=4 and index 2 absent entirely.
* **A removed saved row is hidden, not unplugged.** `hidden` doesn't stop an
  input being submitted, so every one of its fields arrives alongside
  DELETE=on.
* **Untouched rows come back exactly as they were rendered**, defaults and
  all.

See CLAUDE.md, "Formsets: test what the browser actually posts".
"""

from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from inventory.forms import product_display_name, stock_type_entry_name
from inventory.models import StockTake, StockTakeLine, UnitChoices
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)


class StockTakePayloadTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(name="Metro")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        self.bottles = [
            self.priced_product(f"BOUTEILLE {n}", self.vodka if n % 2 else self.gin)
            for n in range(1, 7)
        ]

    def priced_product(self, name, stock_type):
        """A product with a purchase behind it, so a count can be valued."""
        product = make_product(
            supplier=self.supplier, raw_name=name, stock_type=stock_type,
            unit=UnitChoices.UNIT, stock_equivalent="0.7",
        )
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 1, 10))
        make_invoice_line(invoice=invoice, product=product, quantity=12, total_ht="120")
        return product

    def many_products(self, count):
        """`count` DISTINCT products - a stock take allows one line per
        product (unique_product_per_stock_take), so a volume test can't just
        repeat the same handful. No purchase history: these tests are about
        the size of the payload, not what a count is worth."""
        return [
            make_product(
                supplier=self.supplier, raw_name=f"ARTICLE {index:04d}",
                stock_type=self.vodka, unit=UnitChoices.UNIT, stock_equivalent="0.7",
            )
            for index in range(count)
        ]

    def payload(self, rows, total_forms=None, initial_forms=0, taken_at="2026-03-31 12:00:00"):
        """`rows` is {index: {field: value}} - a dict, so a test can leave a
        hole exactly where the browser leaves one."""
        data = {
            "taken_at": taken_at,
            "note": "",
            "lines-TOTAL_FORMS": str(total_forms if total_forms is not None else len(rows)),
            "lines-INITIAL_FORMS": str(initial_forms),
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
        }
        for index, row in rows.items():
            for field, value in row.items():
                data[f"lines-{index}-{field}"] = str(value)
        return data

    def row(self, product, quantity="2", unit=UnitChoices.UNIT, **extra):
        return {
            "entry_search": product_display_name(product),
            "counted_quantity": quantity,
            "unit": unit,
            **extra,
        }

    def post(self, data, url=None):
        return self.client.post(url or reverse("inventory:stock_take_create"), data)

    # -- creating ------------------------------------------------------

    def test_a_straightforward_inventory_saves_every_line(self):
        response = self.post(self.payload({index: self.row(p) for index, p in enumerate(self.bottles)}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockTake.objects.get().lines.count(), 6)

    def test_a_removed_row_leaves_a_gap_and_the_rest_still_save(self):
        """The row the user took out is simply absent from the POST, and
        TOTAL_FORMS still counts it. Every other row must survive that."""
        rows = {index: self.row(p) for index, p in enumerate(self.bottles)}
        del rows[2]
        del rows[4]

        response = self.post(self.payload(rows, total_forms=6))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and "form errors")
        self.assertEqual(StockTake.objects.get().lines.count(), 4)

    def test_a_gap_does_not_save_a_blank_line(self):
        rows = {0: self.row(self.bottles[0]), 2: self.row(self.bottles[1])}
        self.post(self.payload(rows, total_forms=3))
        lines = StockTake.objects.get().lines.all()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.product_id or line.stock_type_id for line in lines))

    def test_the_trailing_blank_row_is_not_saved(self):
        """The form always renders one spare row; leaving it untouched must
        not create a line, nor block the save."""
        rows = {0: self.row(self.bottles[0]), 1: {"entry_search": "", "counted_quantity": "", "unit": ""}}
        response = self.post(self.payload(rows))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockTake.objects.get().lines.count(), 1)

    def test_a_stock_type_line_can_be_counted_directly(self):
        rows = {0: {
            "entry_search": stock_type_entry_name(self.vodka),
            "counted_quantity": "3.5",
            "unit": UnitChoices.LITRE,
        }}
        self.post(self.payload(rows))
        line = StockTake.objects.get().lines.get()
        self.assertEqual(line.stock_type, self.vodka)
        self.assertIsNone(line.product_id)

    def test_one_bad_row_does_not_silently_drop_the_others(self):
        """A typo in one name must reject the whole form with the rest of the
        data still on screen - never save the good rows and discard the bad
        one, which reads exactly like the row was never typed."""
        rows = {index: self.row(p) for index, p in enumerate(self.bottles[:3])}
        rows[1]["entry_search"] = "PRODUIT QUI N'EXISTE PAS"

        response = self.post(self.payload(rows))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockTake.objects.count(), 0)
        self.assertContains(response, "Introuvable")
        # ...and the rows the user did type are still in the re-rendered form.
        self.assertContains(response, product_display_name(self.bottles[0]))
        self.assertContains(response, product_display_name(self.bottles[2]))

    # -- editing -------------------------------------------------------

    def existing_take(self):
        take = make_stock_take(taken_at=datetime(2026, 3, 31, 12, 0))
        lines = [
            make_stock_take_line(
                stock_take=take, product=product, counted_quantity="2",
                unit=UnitChoices.UNIT, value_ht="20",
            )
            for product in self.bottles[:3]
        ]
        return take, lines

    def edit_url(self, take):
        return reverse("inventory:stock_take_update", kwargs={"pk": take.pk})

    def test_removing_a_saved_line_actually_deletes_it(self):
        """The browser hides the row rather than unplugging it, so every
        field still arrives - alongside DELETE=on."""
        take, lines = self.existing_take()
        rows = {
            index: self.row(self.bottles[index], id=line.pk)
            for index, line in enumerate(lines)
        }
        rows[1]["DELETE"] = "on"

        response = self.post(
            self.payload(rows, initial_forms=3, taken_at="2026-03-31 12:00:00"), self.edit_url(take)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(take.lines.count(), 2)
        self.assertFalse(StockTakeLine.objects.filter(pk=lines[1].pk).exists())

    def test_reopening_after_a_removal_shows_no_empty_slot(self):
        """The bug as reported: an item is taken out, and reopening the
        inventory shows an empty slot where something used to be.

        The line really was deleted - what was left behind was the formset's
        `extra` row, rendered blank on every load. Next to the items that
        survived it reads as a removal that half-worked, so the form now
        renders exactly the lines that exist and nothing else.
        """
        take, lines = self.existing_take()
        rows = {index: self.row(self.bottles[index], id=line.pk) for index, line in enumerate(lines)}
        rows[1]["DELETE"] = "on"
        self.post(self.payload(rows, initial_forms=3), self.edit_url(take))

        response = self.client.get(self.edit_url(take))
        formset = response.context["formset"]
        self.assertEqual(formset.initial_form_count(), 2)
        self.assertEqual(len(formset.forms), 2)
        self.assertTrue(all(form.initial.get("entry_search") for form in formset.forms))

    def test_an_existing_inventory_renders_only_its_own_lines(self):
        take, lines = self.existing_take()
        formset = self.client.get(self.edit_url(take)).context["formset"]
        self.assertEqual(len(formset.forms), len(lines))

    def test_a_new_inventory_starts_with_no_server_rendered_rows(self):
        """The page adds the first row itself, so nothing on screen is ever a
        line that isn't really in the count."""
        formset = self.client.get(reverse("inventory:stock_take_create")).context["formset"]
        self.assertEqual(len(formset.forms), 0)

    def test_an_untouched_saved_line_keeps_its_valuation(self):
        """Only rows the user actually changed are re-valued - an old count
        must keep reporting what it was worth on the day it was taken."""
        take, lines = self.existing_take()
        rows = {index: self.row(self.bottles[index], id=line.pk) for index, line in enumerate(lines)}
        self.post(self.payload(rows, initial_forms=3), self.edit_url(take))

        lines[0].refresh_from_db()
        self.assertEqual(lines[0].value_ht, Decimal("20"))

    def test_a_big_inventory_is_not_rejected_before_the_view_runs(self):
        """The bug that lost a real inventory.

        Django caps a POST at DATA_UPLOAD_MAX_NUMBER_FIELDS (1000 by
        default), and a stock take posts three fields per row plus the
        management form - so 332 rows was over the line. Over it, the request
        dies with 400 Bad Request in core handling, BEFORE any view runs:
        no form comes back, no error is shown, and every count typed in is
        gone. A bar counts hundreds of things, so this was reachable in one
        ordinary evening's work.

        Deliberately larger than the old cap, and asserting on the row count
        rather than just the status, so shrinking the setting fails here
        rather than in six months on a Sunday night.
        """
        products = self.many_products(400)
        rows = {index: self.row(product) for index, product in enumerate(products)}
        response = self.post(self.payload(rows))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockTake.objects.get().lines.count(), 400)

    def test_editing_a_big_inventory_survives_the_extra_id_field(self):
        """Editing posts an `id` per row on top, so the ceiling is lower
        there - and editing is where a long inventory actually lives."""
        take = make_stock_take(taken_at=datetime(2026, 3, 31, 12, 0))
        products = self.many_products(300)
        lines = [
            make_stock_take_line(
                stock_take=take, product=product, counted_quantity="1",
                unit=UnitChoices.UNIT, value_ht="10",
            )
            for product in products
        ]
        rows = {
            index: self.row(products[index], quantity="3", id=line.pk)
            for index, line in enumerate(lines)
        }

        response = self.post(self.payload(rows, initial_forms=300), self.edit_url(take))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(take.lines.count(), 300)
        self.assertEqual(take.lines.filter(counted_quantity=3).count(), 300)

    def test_adding_a_row_while_removing_another_does_both(self):
        """The realistic edit: one line taken out, one typed in. The new row
        gets a fresh index above every saved one, never a reused hole."""
        take, lines = self.existing_take()
        rows = {index: self.row(self.bottles[index], id=line.pk) for index, line in enumerate(lines)}
        rows[0]["DELETE"] = "on"
        rows[3] = self.row(self.bottles[4], quantity="7")

        response = self.post(self.payload(rows, initial_forms=3), self.edit_url(take))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(take.lines.count(), 3)
        self.assertTrue(take.lines.filter(product=self.bottles[4], counted_quantity=7).exists())
        self.assertFalse(StockTakeLine.objects.filter(pk=lines[0].pk).exists())
