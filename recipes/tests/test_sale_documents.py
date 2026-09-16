"""Hand-written sale documents: what was sold off the till.

A line is either a recipe or a stock item sold as itself. Both have to reach
the variance report - a tab settled off the books consumed exactly as much
stock as one rung up on the till - but by different routes: a recipe through
its ingredients, a stock item directly.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase
from django.urls import reverse

from inventory.models import UnitChoices
from inventory.variance import quantities_sold
from recipes.models import SaleDocument, SaleDocumentLine
from recipes.sales import record_sales, sales_between, stock_type_sales_between
from tests.factories import make_ingredient, make_movement, make_recipe, make_stock_type


class SaleDocumentModelTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Mule", selling_price_ttc="8.50")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.document = SaleDocument.objects.create(sold_on=date(2026, 3, 5), reference="T-1")

    def test_a_line_must_be_exactly_one_of_the_two(self):
        line = SaleDocumentLine(document=self.document, quantity=Decimal("1"))
        with self.assertRaises(ValidationError):
            line.clean()
        line.recipe = self.recipe
        line.stock_type = self.vodka
        with self.assertRaises(ValidationError):
            line.clean()

    def test_the_database_refuses_a_line_with_neither(self):
        with self.assertRaises(IntegrityError):
            SaleDocumentLine.objects.create(document=self.document, quantity=Decimal("1"))

    def test_a_recipe_line_falls_back_to_the_recipes_own_price(self):
        line = SaleDocumentLine.objects.create(
            document=self.document, recipe=self.recipe, quantity=Decimal("3")
        )
        self.assertEqual(line.total_ttc, Decimal("25.50"))

    def test_an_explicit_price_wins(self):
        line = SaleDocumentLine.objects.create(
            document=self.document, recipe=self.recipe, quantity=Decimal("3"),
            unit_price_ttc=Decimal("5.00"),
        )
        self.assertEqual(line.total_ttc, Decimal("15.00"))

    def test_a_stock_item_has_no_default_price(self):
        """There is no "price" on a stock item, only a cost."""
        line = SaleDocumentLine.objects.create(
            document=self.document, stock_type=self.vodka, quantity=Decimal("2")
        )
        self.assertEqual(line.total_ttc, Decimal("0"))

    def test_the_document_total_sums_its_lines(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("2"))
        SaleDocumentLine.objects.create(
            document=self.document, stock_type=self.vodka, quantity=Decimal("1"),
            unit_price_ttc=Decimal("20.00"),
        )
        self.assertEqual(self.document.total_ttc, Decimal("37.00"))


class SaleDocumentsReachTheVarianceReportTests(TestCase):
    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.recipe = make_recipe(name="Mule")
        make_ingredient(self.recipe, stock_type=self.vodka, quantity="0.04", group=0)
        self.document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))

    def test_a_recipe_line_counts_as_a_sale_of_that_recipe(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("10"))
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {self.recipe.pk: 10})

    def test_it_adds_to_till_sales_rather_than_replacing_them(self):
        record_sales([("Mule", date(2026, 3, 5), 20)], source="laddition")
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("10"))
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {self.recipe.pk: 30})

    def test_a_stock_line_is_reported_separately(self):
        SaleDocumentLine.objects.create(
            document=self.document, stock_type=self.vodka, quantity=Decimal("0.7")
        )
        self.assertEqual(
            stock_type_sales_between(date(2026, 3, 1), date(2026, 3, 10)),
            {self.vodka.pk: Decimal("0.7")},
        )
        # ...and never as a recipe sale.
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 10)), {})

    def test_the_window_bounds_apply(self):
        SaleDocumentLine.objects.create(document=self.document, recipe=self.recipe, quantity=Decimal("10"))
        self.assertEqual(sales_between(date(2026, 3, 5), date(2026, 3, 10)), {})  # on the opening day
        self.assertEqual(sales_between(date(2026, 3, 1), date(2026, 3, 4)), {})  # after the close


class QuantitiesSoldTests(TestCase):
    """How much of a stock item was sold, for the "Vendu" column on the
    stock page. An ambiguous choice's amount goes to the PRICIEST option that
    still has stock to justify it - the assumption a bar owner would actually
    make eyeballing what left the shelf - rather than split (inventing a
    precision nobody has) or copied onto every alternative (which used to
    make every one of them read "0 sold" with the real number tucked into a
    footnote, indistinguishable from actually nothing having moved).

    These tests are about WHICH alternative wins, so every one of them buys
    far more stock than the sales consume - see ClampedToWhatWasBoughtTests
    for what happens when the shelf runs out, which is a separate question.
    """

    def setUp(self):
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)
        self.lime = make_stock_type(name="Citrons", unit=UnitChoices.KILOGRAM)

    def sell(self, recipe, count):
        record_sales([(recipe.name, date(2026, 3, 5), count)])

    def with_alternatives(self):
        recipe = make_recipe(name="Mule")
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=self.gin, quantity="0.04", group=0)
        return recipe

    def price(self, stock_type, unit_cost_ht, quantity="1000"):
        """A price, and by default plenty of stock at it.

        The average cost is the same whatever the quantity, so buying a lot
        changes nothing these tests are about - it just keeps the shelf out
        of the way, so "which alternative wins" is answered on price alone.
        """
        make_movement(stock_type=stock_type, quantity=quantity, unit_cost_ht=unit_cost_ht)

    def test_an_ingredient_with_no_alternatives_is_exact(self):
        recipe = make_recipe(name="Caipi")
        make_ingredient(recipe, stock_type=self.lime, quantity="0.05", group=0)
        self.sell(recipe, 100)

        sold = quantities_sold()[self.lime.pk]
        self.assertEqual(sold.exact, Decimal("5.00"))
        self.assertEqual(sold.shared, Decimal("0"))
        self.assertFalse(sold.is_ambiguous)
        self.assertEqual(sold.headline, Decimal("5.00"))
        self.assertFalse(sold.headline_is_estimate)

    def test_a_choice_is_credited_whole_to_the_priciest_alternative(self):
        """Not split, and not copied onto every alternative either - one
        item shows the estimate, clearly marked as one."""
        self.price(self.vodka, "15")
        self.price(self.gin, "25")
        self.sell(self.with_alternatives(), 100)

        sold = quantities_sold()
        gin, vodka = sold[self.gin.pk], sold[self.vodka.pk]

        self.assertEqual(gin.shared, Decimal("4.00"))
        self.assertTrue(gin.is_ambiguous)
        self.assertEqual(gin.headline, Decimal("4.00"))
        self.assertTrue(gin.headline_is_estimate)

        # Vodka was offered too, but it's the cheaper alternative - it still
        # gets an entry (its pool/tooltip data has to exist), just no guess.
        self.assertEqual(vodka.exact, Decimal("0"))
        self.assertEqual(vodka.shared, Decimal("0"))
        self.assertEqual(vodka.headline, Decimal("0"))
        self.assertEqual(vodka.pool_partners, 1)

    def test_a_priced_alternative_is_credited_its_OWN_pour_not_the_biggest_one(self):
        """A "double" of the well brand poured next to a single of the good
        stuff is still two alternatives of one choice, just with different
        quantities (see DifferentQuantitiesTests in inventory/tests -
        the same recipe shape). Attributing the POOL's largest possible
        volume to whichever item happens to be priciest overstated the
        premium spirit by the cheap one's bigger pour: 100 sales at 0.04L
        vodka / 0.12L well liquor once showed 12.00L of vodka - three times
        its own true maximum of 4.00L - because only the winning item's
        IDENTITY came from being priciest; the amount still came from
        whichever option poured the most, which was a different one."""
        premium = make_stock_type(name="Vodka Premium", unit=UnitChoices.LITRE)
        well = make_stock_type(name="Well Liquor", unit=UnitChoices.LITRE)
        self.price(premium, "30")
        self.price(well, "5")
        recipe = make_recipe(name="House Special")
        make_ingredient(recipe, stock_type=premium, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=well, quantity="0.12", group=0)
        self.sell(recipe, 100)

        sold = quantities_sold()
        self.assertEqual(sold[premium.pk].shared, Decimal("4.00"))
        self.assertEqual(sold[well.pk].shared, Decimal("0"))

    def test_a_tie_falls_to_the_lowest_id_not_arbitrarily(self):
        """Equal prices can't say which alternative is "priciest". The pick
        still has to be the SAME one every time this runs, or the column
        would flicker between reloads."""
        self.assertLess(self.vodka.pk, self.gin.pk)
        self.price(self.vodka, "20")
        self.price(self.gin, "20")
        self.sell(self.with_alternatives(), 100)

        sold = quantities_sold()
        self.assertEqual(sold[self.vodka.pk].shared, Decimal("4.00"))
        self.assertEqual(sold[self.gin.pk].shared, Decimal("0"))

    def test_a_direct_sale_is_always_exact(self):
        """Sold as itself there is nothing to be ambiguous about, even for an
        item that is an alternative elsewhere."""
        self.with_alternatives()
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))
        SaleDocumentLine.objects.create(document=document, stock_type=self.vodka, quantity=Decimal("0.7"))

        self.assertEqual(quantities_sold()[self.vodka.pk].exact, Decimal("0.7"))

    def test_exact_and_shared_add_up_to_the_headline(self):
        self.price(self.gin, "25")  # the priciest alternative, so the one that gets `shared`
        self.sell(self.with_alternatives(), 100)
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))
        SaleDocumentLine.objects.create(document=document, stock_type=self.gin, quantity=Decimal("1"))

        sold = quantities_sold()[self.gin.pk]
        self.assertEqual(sold.exact, Decimal("1.00"))
        self.assertEqual(sold.shared, Decimal("4.00"))
        self.assertEqual(sold.headline, Decimal("5.00"))

    def test_an_item_that_was_never_sold_is_absent(self):
        self.assertNotIn(self.vodka.pk, quantities_sold())

    def test_custom_unit_costs_are_honoured_over_the_stored_ones(self):
        """StockListView passes in the sums it already computed for its own
        columns, rather than have this run a second identical scan of
        StockMovement - this is what lets it do that."""
        self.price(self.vodka, "15")
        self.price(self.gin, "25")
        self.sell(self.with_alternatives(), 100)

        # Override what's on file: pretend vodka is actually the pricier one.
        overridden = quantities_sold(unit_costs={self.vodka.pk: Decimal("99"), self.gin.pk: Decimal("1")})
        self.assertEqual(overridden[self.vodka.pk].shared, Decimal("4.00"))
        self.assertEqual(overridden[self.gin.pk].shared, Decimal("0"))

    def test_two_independent_choices_in_one_recipe_do_not_cross_wire(self):
        """"Vodka OU gin" AND, separately, "coca OU sprite" - two pools in
        one recipe, each with its own priciest winner. A bug that let one
        group's options leak into the other's would either double-count a
        quantity or crown the wrong item priciest."""
        coca = make_stock_type(name="Coca", unit=UnitChoices.LITRE)
        sprite = make_stock_type(name="Sprite", unit=UnitChoices.LITRE)
        self.price(self.vodka, "15")
        self.price(self.gin, "25")  # priciest spirit
        self.price(coca, "3")
        self.price(sprite, "8")  # priciest mixer

        recipe = make_recipe(name="Long Drink")
        make_ingredient(recipe, stock_type=self.vodka, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=self.gin, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=coca, quantity="0.15", group=1)
        make_ingredient(recipe, stock_type=sprite, quantity="0.15", group=1)
        self.sell(recipe, 100)

        sold = quantities_sold()
        self.assertEqual(sold[self.gin.pk].shared, Decimal("4.00"))
        self.assertEqual(sold[self.vodka.pk].shared, Decimal("0"))
        self.assertEqual(sold[sprite.pk].shared, Decimal("15.00"))
        self.assertEqual(sold[coca.pk].shared, Decimal("0"))

    def test_the_stock_page_renders_the_column(self):
        self.sell(self.with_alternatives(), 100)
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vendu")

    def test_the_estimate_becomes_the_headline_not_a_footnote(self):
        """The whole point of this change: showing "0.00" for a spirit the
        till clearly moved read as "nothing sold". The estimate now IS the
        number, still marked as a guess rather than presented as fact."""
        self.price(self.gin, "25")
        self.sell(self.with_alternatives(), 100)

        response = self.client.get(reverse("inventory:stock_list"))
        rows = [row for category in response.context["categories"] for row in category["rows"]]
        gin_row = next(row for row in rows if row["stock_type"].pk == self.gin.pk)
        self.assertEqual(gin_row["sold"].headline, Decimal("4.00"))
        self.assertTrue(gin_row["sold"].headline_is_estimate)


class QuantitiesSoldWithNestedChoicesTests(TestCase):
    """A choice's options aren't always one stock item each - one option can
    be a sub-recipe that pours several things together (see "OU nests" in
    CLAUDE.md). The priciest-option logic has to work on the option AS A
    WHOLE, crediting every stock item inside the winning one its own
    quantity, not just the one item that happens to be pricier in isolation."""

    def setUp(self):
        self.well_mix = make_stock_type(name="Mélange maison", unit=UnitChoices.LITRE)
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        self.liqueur = make_stock_type(name="Liqueur rare", unit=UnitChoices.LITRE)
        # Plenty of everything: this class is about which OPTION wins, not
        # about running out - see ClampedToWhatWasBoughtTests for that.
        make_movement(stock_type=self.well_mix, quantity="1000", unit_cost_ht="5")
        make_movement(stock_type=self.vodka, quantity="1000", unit_cost_ht="30")
        make_movement(stock_type=self.liqueur, quantity="1000", unit_cost_ht="20")

        # A sub-recipe with NO choice of its own - vodka and liqueur are
        # always poured together, in different groups, so it's one fixed
        # combination rather than an alternative between the two of them.
        self.premium_duo = make_recipe(name="Duo premium", yield_quantity="1")
        make_ingredient(self.premium_duo, stock_type=self.vodka, quantity="0.03", group=0)
        make_ingredient(self.premium_duo, stock_type=self.liqueur, quantity="0.02", group=1)

        self.deluxe = make_recipe(name="Deluxe")
        make_ingredient(self.deluxe, stock_type=self.well_mix, quantity="0.05", group=0)
        make_ingredient(self.deluxe, sub_recipe=self.premium_duo, quantity="1", group=0)

    def test_the_pricier_options_own_items_each_get_their_own_amount(self):
        """0.9€ of vodka + 0.4€ of liqueur (1.3€ total) beats 0.25€ of well
        mix, so the duo wins - and both of ITS ingredients are credited
        their own pour, not a number borrowed from the losing option."""
        record_sales([(self.deluxe.name, date(2026, 3, 5), 100)])

        sold = quantities_sold()
        self.assertEqual(sold[self.vodka.pk].shared, Decimal("3.00"))
        self.assertEqual(sold[self.liqueur.pk].shared, Decimal("2.00"))
        self.assertEqual(sold[self.well_mix.pk].shared, Decimal("0"))

    def test_the_losing_option_still_gets_a_pool_entry(self):
        record_sales([(self.deluxe.name, date(2026, 3, 5), 100)])
        sold = quantities_sold()
        self.assertIn(self.well_mix.pk, sold)
        self.assertEqual(sold[self.well_mix.pk].pool, sold[self.vodka.pk].pool)
        self.assertEqual(sold[self.well_mix.pk].pool, sold[self.liqueur.pk].pool)


class SaleDocumentPageTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Mule", selling_price_ttc="8.50")
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)

    def payload(self, rows):
        data = {
            "sold_on": "2026-03-05",
            "reference": "",
            "note": "",
            "lines-TOTAL_FORMS": str(len(rows)),
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"lines-{index}-{key}"] = str(value)
        return data

    def test_a_document_can_hold_both_kinds_of_line(self):
        response = self.client.post(
            reverse("recipes:sale_document_create"),
            self.payload(
                [
                    {"source": f"recipe:{self.recipe.pk}", "quantity": "3", "unit_price_ttc": ""},
                    {"source": f"stock:{self.vodka.pk}", "quantity": "0.7", "unit_price_ttc": "20"},
                ]
            ),
        )
        self.assertEqual(response.status_code, 302)
        document = SaleDocument.objects.get()
        self.assertEqual(document.lines.filter(recipe__isnull=False).count(), 1)
        self.assertEqual(document.lines.filter(stock_type__isnull=False).count(), 1)

    def test_a_document_with_no_lines_is_refused(self):
        response = self.client.post(reverse("recipes:sale_document_create"), self.payload([]))
        self.assertContains(response, "Ajoutez au moins une ligne")
        self.assertEqual(SaleDocument.objects.count(), 0)

    def test_a_line_with_no_source_is_refused(self):
        response = self.client.post(
            reverse("recipes:sale_document_create"),
            self.payload([{"source": "", "quantity": "3", "unit_price_ttc": ""}]),
        )
        self.assertContains(response, "Choisissez une recette")

    def test_the_page_renders(self):
        self.assertEqual(self.client.get(reverse("recipes:sale_document_create")).status_code, 200)

    def test_the_date_defaults_to_today(self):
        """A ModelForm seeds self.initial from the instance, so setting
        fields["sold_on"].initial is silently ignored and the box renders
        empty - which is a small thing you notice only by looking."""
        from django.utils import timezone

        from recipes.forms import SaleDocumentForm

        self.assertEqual(SaleDocumentForm()["sold_on"].value(), timezone.localdate())

    def test_documents_appear_on_the_ventes_page(self):
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5), reference="T-9")
        SaleDocumentLine.objects.create(document=document, recipe=self.recipe, quantity=Decimal("2"))
        html = self.client.get(reverse("recipes:sales_list")).content.decode()
        self.assertIn("T-9", html)
        self.assertIn("Mule", html)

    def test_a_document_can_be_deleted(self):
        document = SaleDocument.objects.create(sold_on=date(2026, 3, 5))
        SaleDocumentLine.objects.create(document=document, recipe=self.recipe, quantity=Decimal("2"))
        self.client.post(reverse("recipes:sale_document_delete", kwargs={"pk": document.pk}))
        self.assertEqual(SaleDocument.objects.count(), 0)
