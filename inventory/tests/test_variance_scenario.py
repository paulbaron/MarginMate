"""One full worked scenario, shaped like the real bar.

Two recipes with overlapping alternatives - exactly the structure already in
the database:

    Alcool + Soda : Gin OR Vodka OR Whisky OR Rhum   +  a mixer
    Mule          : Vodka OR Gin OR Rhum             +  a mixer

so all four spirits are one pool and the mixers another. The point of the
test is that the answer doesn't depend on which bottle each drink was
actually poured from - which is the one thing that can never be known.
"""

from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from inventory.models import MovementKind, StockMovement, UnitChoices
from inventory.variance import compute_variance
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)

SPIRITS = {"Gin": "20", "Vodka": "18", "Whisky": "40", "Rhum": "15"}
MIXERS = {"Limonade": "2", "Tonic": "3", "Ginger beer": "4"}

# 500 drinks at 0.20 L is 100 L of mixer; from 40 L of each (120 L in
# total) that leaves 20 L when nothing has gone astray.
MIXERS_BALANCED = {"Limonade": "5", "Tonic": "5", "Ginger beer": "10"}
MIXERS_SHORT_BY_5 = {"Limonade": "2", "Tonic": "3", "Ginger beer": "10"}


class BarScenarioTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.supplier = make_supplier()
        cls.stock = {}
        for name, unit_cost in {**SPIRITS, **MIXERS}.items():
            stock_type = make_stock_type(name=name, unit=UnitChoices.LITRE)
            make_product(
                supplier=cls.supplier, raw_name=f"{name.upper()} 70CL", stock_type=stock_type,
                unit=UnitChoices.UNIT, stock_equivalent="0.7",
            )
            StockMovement.objects.create(
                stock_type=stock_type, quantity=Decimal("100"), unit_cost_ht=Decimal(unit_cost),
                kind=MovementKind.PURCHASE, occurred_on=date(2026, 1, 1),
            )
            cls.stock[name] = stock_type

        cls.soda = make_recipe(name="Alcool + Soda", selling_price_ttc="8.50")
        for name in ("Gin", "Vodka", "Whisky", "Rhum"):
            make_ingredient(cls.soda, stock_type=cls.stock[name], quantity="0.04", group=0)
        for name in ("Limonade", "Tonic", "Ginger beer"):
            make_ingredient(cls.soda, stock_type=cls.stock[name], quantity="0.20", group=1)

        cls.mule = make_recipe(name="Mule", selling_price_ttc="8.50")
        for name in ("Vodka", "Gin", "Rhum"):
            make_ingredient(cls.mule, stock_type=cls.stock[name], quantity="0.04", group=0)
        for name in ("Ginger beer", "Limonade"):
            make_ingredient(cls.mule, stock_type=cls.stock[name], quantity="0.20", group=1)

    def count(self, taken_on: int, quantities: dict):
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, taken_on, 12, 0)))
        for name, litres in quantities.items():
            make_stock_take_line(
                stock_take=take, stock_type=self.stock[name],
                counted_quantity=litres, unit=UnitChoices.LITRE,
            )
        return take

    def run_period(self, opening, closing, sales):
        self.count(1, opening)
        closing_take = self.count(31, closing)
        record_sales([(name, date(2026, 3, 15), count) for name, count in sales.items()])
        return compute_variance(closing_take)

    def spirits(self, report):
        return next(pool for pool in report.pools if self.stock["Vodka"] in pool.stock_types)

    def mixers(self, report):
        return next(pool for pool in report.pools if self.stock["Tonic"] in pool.stock_types)

    # ---------------------------------------------------------------
    def test_the_four_spirits_form_one_pool_and_the_mixers_another(self):
        report = self.run_period(
            opening=dict.fromkeys(self.stock, "10"),
            closing=dict.fromkeys(self.stock, "10"),
            sales={},
        )
        self.assertEqual(
            sorted(st.name for st in self.spirits(report).stock_types),
            ["Gin", "Rhum", "Vodka", "Whisky"],
        )
        self.assertEqual(
            sorted(st.name for st in self.mixers(report).stock_types),
            ["Ginger beer", "Limonade", "Tonic"],
        )

    def test_a_night_that_adds_up_reports_nothing_missing(self):
        """500 drinks at 4cl = 20 L of spirit and 100 L of mixer, taken from
        the four spirits in whatever proportion actually happened."""
        report = self.run_period(
            opening=dict.fromkeys(self.stock, "40"),
            closing={
                "Gin": "32", "Vodka": "30", "Whisky": "38", "Rhum": "40",   # 20 L of spirit gone
                **MIXERS_BALANCED,                                          # 100 L of mixer gone
            },
            sales={"Alcool + Soda": 300, "Mule": 200},
        )
        self.assertEqual(self.spirits(report).actual_usage, Decimal("20"))
        self.assertEqual(self.spirits(report).expected_usage_min, Decimal("20"))
        self.assertFalse(self.spirits(report).is_missing)
        self.assertEqual(self.mixers(report).actual_usage, Decimal("100"))
        self.assertEqual(self.mixers(report).expected_usage_min, Decimal("100"))
        self.assertFalse(self.mixers(report).is_missing)
        self.assertFalse(self.mixers(report).is_impossible)

    def test_the_same_total_split_differently_gives_the_same_answer(self):
        """The heart of it: 20 L of spirit went, and it makes no difference
        which bottles it came out of."""
        answers = []
        for closing in (
            {"Gin": "32", "Vodka": "30", "Whisky": "38", "Rhum": "40"},
            {"Gin": "40", "Vodka": "40", "Whisky": "20", "Rhum": "40"},
            {"Gin": "35", "Vodka": "35", "Whisky": "35", "Rhum": "35"},
        ):
            with self.subTest(closing=closing):
                report = self.run_period(
                    opening=dict.fromkeys(self.stock, "40"),
                    closing={**closing, **MIXERS_BALANCED},
                    sales={"Alcool + Soda": 300, "Mule": 200},
                )
                answers.append(self.spirits(report).unexplained_min)
                # Each subtest gets a clean slate from the enclosing rollback
                # only at the end, so tear down what this iteration made.
                report.closing_take.delete()
                report.opening_take.delete()
                from recipes.models import RecipeSale

                RecipeSale.objects.all().delete()
        self.assertEqual(answers, [Decimal("0")] * 3)

    def test_missing_spirit_is_reported_in_bottles_of_the_cheapest(self):
        """25 L of spirit left the shelf but only 20 L was sold. The 5 L
        gap is stated as rum - the cheapest of the four - because that is
        the smallest claim the data supports."""
        report = self.run_period(
            opening=dict.fromkeys(self.stock, "40"),
            closing={
                "Gin": "30", "Vodka": "28", "Whisky": "37", "Rhum": "40",   # 25 L gone
                **MIXERS_BALANCED,
            },
            sales={"Alcool + Soda": 300, "Mule": 200},
        )
        pool = self.spirits(report)
        self.assertEqual(pool.actual_usage, Decimal("25"))
        self.assertEqual(pool.expected_usage_min, Decimal("20"))
        self.assertEqual(pool.unexplained_min, Decimal("5"))

        self.assertEqual(pool.cheapest, self.stock["Rhum"])
        self.assertEqual(pool.cheapest_unit_cost, Decimal("15"))
        # 5 L at 70cl a bottle
        self.assertAlmostEqual(pool.bottles_missing_min, Decimal("5") / Decimal("0.7"), places=6)
        self.assertEqual(pool.value_missing_min, Decimal("75"))  # 5 L x 15

        # Priced as whisky it would be 200 EUR - which is exactly why the
        # headline figure uses the cheapest.
        self.assertLess(pool.value_missing_min, Decimal("5") * Decimal("40"))

    def test_a_broken_bottle_you_logged_is_not_shrinkage(self):
        StockMovement.objects.create(
            stock_type=self.stock["Whisky"], quantity=Decimal("-0.7"),
            unit_cost_ht=Decimal("40"), kind=MovementKind.LOSS,
            note="Bouteille cassée au service", occurred_on=date(2026, 3, 12),
        )
        report = self.run_period(
            opening=dict.fromkeys(self.stock, "40"),
            closing={
                "Gin": "32", "Vodka": "30", "Whisky": "37.3", "Rhum": "40",  # 20.7 L gone
                **MIXERS_BALANCED,
            },
            sales={"Alcool + Soda": 300, "Mule": 200},
        )
        pool = self.spirits(report)
        self.assertEqual(pool.actual_usage, Decimal("20.7"))
        self.assertEqual(pool.known_losses, Decimal("0.7"))
        self.assertEqual(pool.unexplained_min, Decimal("0"))
        self.assertFalse(pool.is_missing)

    def test_pouring_more_than_you_had_is_flagged_as_a_data_error(self):
        report = self.run_period(
            opening=dict.fromkeys(self.stock, "40"),
            closing={
                "Gin": "38", "Vodka": "38", "Whisky": "40", "Rhum": "40",   # only 4 L gone
                **MIXERS_BALANCED,
            },
            sales={"Alcool + Soda": 300, "Mule": 200},  # says 20 L
        )
        pool = self.spirits(report)
        self.assertTrue(pool.is_impossible)
        self.assertIn(pool, report.impossible)
        self.assertNotIn(pool, report.missing)

    def test_the_report_page_renders_the_whole_story(self):
        from django.urls import reverse

        from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

        report = self.run_period(
            opening=dict.fromkeys(self.stock, "40"),
            closing={
                "Gin": "30", "Vodka": "28", "Whisky": "37", "Rhum": "40",
                **MIXERS_BALANCED,
            },
            sales={"Alcool + Soda": 300, "Mule": 200},
        )
        response = self.client.get(
            reverse("inventory:stock_take_variance", kwargs={"pk": report.closing_take.pk})
        )
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "variance report")
        self.assertContains(response, "Gin / Vodka / Whisky / Rhum")
        self.assertContains(response, "75.00")   # the floor, in euros
        self.assertContains(response, "Rhum")    # priced as the cheapest
        self.assertContains(response, "500 ventes")

    def test_the_report_page_warns_when_there_are_no_sales(self):
        from django.urls import reverse

        report = self.run_period(
            opening=dict.fromkeys(self.stock, "40"),
            closing=dict.fromkeys(self.stock, "38"),
            sales={},
        )
        response = self.client.get(
            reverse("inventory:stock_take_variance", kwargs={"pk": report.closing_take.pk})
        )
        self.assertContains(response, "Aucune vente enregistrée")

    def test_the_report_ranks_by_what_the_loss_is_worth(self):
        report = self.run_period(
            opening=dict.fromkeys(self.stock, "40"),
            closing={
                "Gin": "30", "Vodka": "28", "Whisky": "37", "Rhum": "40",   # 5 L of spirit missing
                **MIXERS_SHORT_BY_5,                                        # 5 L of mixer missing
            },
            sales={"Alcool + Soda": 300, "Mule": 200},
        )
        self.assertEqual(
            [pool.label for pool in report.missing],
            [self.spirits(report).label, self.mixers(report).label],
            "the expensive pool should be reported first",
        )
        # 5 L of rum at 15 + 5 L of limonade at 2
        self.assertEqual(report.total_value_missing_min, Decimal("75") + Decimal("10"))
