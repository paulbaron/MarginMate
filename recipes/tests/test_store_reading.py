"""The one writer of a till reading (recipes.tasks.store_reading) and the
payments rule it keeps for a file of payments alone.

Every reader of the till - the job fetching L'Addition, `laddition_import`,
a file uploaded on « Ventes » - writes through `store_reading`, in one order:
the till products and their days, the recipes' sales, then the payments. A
day's payments only beside a day « Ventes » holds: the job held that rule by
writing the lines first; a file of « Encaissements » alone holds it through
`record_payments(beside_sales=True)`, the other days left as they are and
said.

Every name, amount and day is invented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase

from recipes.models import PosDailyPayment, PosProduct, PosProductDailyQuantity, RecipeSale
from recipes.payments import days_with_sales, record_payments
from recipes.pos.laddition_xlsx import DayMoney, DayPayment, ParsedExport
from recipes.sales import TILL_SOURCE
from recipes.tasks import store_reading
from tests.factories import make_recipe

CB, CASH = PosDailyPayment.CARD, PosDailyPayment.CASH


def june(day: int) -> date:
    return date(2026, 6, day)


def payments_only(days: dict) -> ParsedExport:
    """A reading of payments alone: {day: {method: amount}}."""
    export = ParsedExport(payments_read=True, sales_read=False)
    for day, methods in days.items():
        export.payment_days.add(day)
        for method, amount in methods.items():
            export.payments[(day, method)] = DayPayment(Decimal(amount), 1)
    return export


def a_sale_on(day: date, name: str = "Pinte Exemple") -> None:
    product, _ = PosProduct.objects.get_or_create(name=name)
    PosProductDailyQuantity.objects.create(product=product, sold_on=day, quantity=1)


def stored() -> list[tuple]:
    return list(PosDailyPayment.objects.values_list("sold_on", "method", "amount"))


class BesideSalesTests(TestCase):
    def test_only_the_days_with_sales_are_written_the_others_returned(self):
        a_sale_on(june(1))
        export = payments_only({june(1): {CB: "7.50"}, june(2): {CASH: "3.00"}})
        paid = record_payments(export, beside_sales=True)
        self.assertEqual(stored(), [(june(1), CB, Decimal("7.50"))])
        self.assertEqual(paid.days_written, 1)
        self.assertEqual(paid.days_without_sales, [june(2)])

    def test_a_day_that_paid_nothing_is_not_listed(self):
        """Every ticket comped: nothing to write, and no import gives it a
        day in « Ventes » - listed, it would ask for what never comes."""
        export = payments_only({june(3): {}})
        self.assertEqual(record_payments(export, beside_sales=True).days_without_sales, [])

    def test_without_the_rule_every_day_read_is_written_as_before(self):
        export = payments_only({june(2): {CASH: "3.00"}})
        paid = record_payments(export)
        self.assertEqual(stored(), [(june(2), CASH, Decimal("3.00"))])
        self.assertEqual(paid.days_without_sales, [])

    def test_days_with_sales_reads_one_span(self):
        a_sale_on(june(1))
        a_sale_on(june(5), "Soda Exemple")
        self.assertEqual(days_with_sales([june(1), june(2), june(5)]), {june(1), june(5)})
        self.assertEqual(days_with_sales([]), set())
        self.assertEqual(days_with_sales(), {june(1), june(5)})


class StoreReadingTests(TestCase):
    def test_a_reading_is_written_in_the_one_order_and_said_in_french(self):
        recipe = make_recipe(name="Pinte Exemple")
        export = ParsedExport(
            entries=[("Pinte Exemple", june(1), 2), ("Inconnue Exemple", june(1), 1)],
            products={
                "Pinte Exemple": {"quantity": 2, "category": "", "typology": "", "first": june(1), "last": june(1)},
                "Inconnue Exemple": {"quantity": 1, "category": "", "typology": "", "first": june(1), "last": june(1)},
            },
            money={("Pinte Exemple", june(1)): DayMoney(Decimal("15.00"), Decimal("12.50"))},
        )
        export.payments_read = True
        export.payment_days = {june(1)}
        export.payments[(june(1), CB)] = DayPayment(Decimal("15.00"), 2)
        lines = []
        stored_reading = store_reading(export, lines.append)

        self.assertEqual(stored_reading.seen, 2)
        self.assertEqual(stored_reading.unmatched, 1)
        self.assertEqual(
            lines,
            [
                "2 produits de caisse vus.",
                "1 totaux recette/jour enregistrés (1 nouveaux, 0 mis à jour).",
                "1 produits de caisse sans recette - à traiter dans « À lier ».",
                "Paiements enregistrés : 1 jour(s) de caisse remplacé(s), 0 déjà à jour.",
            ],
        )
        self.assertEqual(
            list(RecipeSale.objects.values_list("recipe", "sold_on", "source", "quantity")),
            [(recipe.pk, june(1), TILL_SOURCE, 2)],
        )
        self.assertEqual(stored(), [(june(1), CB, Decimal("15.00"))])

    def test_a_reading_of_payments_alone_writes_and_says_no_sales(self):
        a_sale_on(june(1))
        lines = []
        stored_reading = store_reading(
            payments_only({june(1): {CB: "7.50"}, june(2): {CASH: "3.00"}}),
            lines.append,
            payments_beside_sales=True,
        )
        self.assertEqual(stored_reading.seen, 0)
        self.assertEqual(
            lines,
            [
                "Paiements enregistrés : 1 jour(s) de caisse remplacé(s), 0 déjà à jour.",
                (
                    "1 jour(s) laissé(s) de côté : aucune vente enregistrée ces jours-là. "
                    "Importez d'abord les ventes de ces jours."
                ),
            ],
        )
        self.assertEqual(stored(), [(june(1), CB, Decimal("7.50"))])
        self.assertFalse(RecipeSale.objects.exists())


def sales_of(*entries) -> ParsedExport:
    """A reading of these (name, day, quantity), each a product of its own."""
    export = ParsedExport(entries=list(entries))
    for name, day, quantity in entries:
        export.products[name] = {"quantity": quantity, "category": "", "typology": "", "first": day, "last": day}
    return export


class PartOfADayTests(TestCase):
    """A file holding part of a day - an upload corrects one product - leaves
    the recipes agreeing with the day's till products: `record_sales` sets a
    (recipe, day) to what it is handed, and handed the file alone it set the
    recipe to the corrected product while the day kept its others."""

    def setUp(self):
        self.pinte = make_recipe(name="Pinte Exemple")
        self.pinte.happy_hour_name = "Pinte Exemple HH"
        self.pinte.save()

    def recipe_sales(self) -> list[tuple]:
        return sorted(RecipeSale.objects.values_list("source", "sold_on", "quantity"))

    def day(self) -> dict:
        return dict(PosProductDailyQuantity.objects.values_list("product__name", "quantity"))

    def test_a_product_corrected_alone_keeps_the_recipe_at_the_day_s_total(self):
        store_reading(sales_of(("Pinte Exemple", june(3), 2), ("Pinte Exemple HH", june(3), 3)), lambda line: None)
        store_reading(sales_of(("Pinte Exemple", june(3), 4)), lambda line: None)
        self.assertEqual(self.day(), {"Pinte Exemple": 4, "Pinte Exemple HH": 3})
        self.assertEqual(self.recipe_sales(), [(TILL_SOURCE, june(3), 7)])

    def test_the_recipes_agree_with_a_rebuild_from_the_days(self):
        from recipes.sales import resync_recipe_from_daily_quantities

        store_reading(sales_of(("Pinte Exemple", june(3), 2), ("Pinte Exemple HH", june(3), 3)), lambda line: None)
        store_reading(sales_of(("Pinte Exemple HH", june(3), 1), ("Pinte Exemple", june(4), 5)), lambda line: None)
        written = self.recipe_sales()
        resync_recipe_from_daily_quantities(self.pinte)
        self.assertEqual(written, [(TILL_SOURCE, june(3), 3), (TILL_SOURCE, june(4), 5)])
        self.assertEqual(self.recipe_sales(), written)

    def test_another_day_and_a_sale_typed_by_hand_are_left_alone(self):
        from recipes.sales import MANUAL_SALE_SOURCE

        store_reading(sales_of(("Pinte Exemple", june(2), 6)), lambda line: None)
        RecipeSale.objects.create(recipe=self.pinte, sold_on=june(3), quantity=9, source=MANUAL_SALE_SOURCE)
        store_reading(sales_of(("Pinte Exemple HH", june(3), 1)), lambda line: None)
        self.assertEqual(
            self.recipe_sales(),
            sorted([(MANUAL_SALE_SOURCE, june(3), 9), (TILL_SOURCE, june(2), 6), (TILL_SOURCE, june(3), 1)]),
        )

    def test_only_the_reading_s_own_products_are_said_to_have_no_recipe(self):
        store_reading(sales_of(("Pinte Exemple", june(3), 2), ("Inconnue Exemple", june(3), 1)), lambda line: None)
        lines = []
        stored_reading = store_reading(sales_of(("Pinte Exemple", june(3), 4)), lines.append)
        self.assertEqual(stored_reading.unmatched, 0)
        self.assertNotIn("sans recette", "\n".join(lines))
        self.assertEqual(self.recipe_sales(), [(TILL_SOURCE, june(3), 4)])
