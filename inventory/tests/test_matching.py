"""Tests for resolving an invoice line's raw product name to a Product.

The fuzzy step exists for one narrow job: absorbing formatting drift in the
SAME product's name ("SOBIESKI VODKA 70CL" vs "SOBIESKI VODKA 70 CL") so a
duplicate isn't created. Anything it accepts is linked silently, skipping
the review queue entirely - so a false accept is invisible, and it pollutes
that product's whole price history with another product's costs.

It was accepting far too much. On the real database, 136 invoice lines
(3,732.66 EUR) were attached to a product with a different name, including
"RICARD 45D 1.5L" -> "RICARD 45D 1L" and "RIOBA PUR JUS TOMATE 1L VP" ->
"...25CL VP".
"""

from django.test import TestCase

from inventory.matching import resolve_product
from inventory.models import Product
from tests.factories import make_product, make_supplier


class ResolveProductExactMatchTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier()

    def test_exact_name_match(self):
        existing = make_product(supplier=self.supplier, raw_name="VODKA 70CL")
        product, created = resolve_product(self.supplier, "VODKA 70CL")
        self.assertEqual(product, existing)
        self.assertFalse(created)

    def test_name_match_is_case_and_whitespace_insensitive(self):
        existing = make_product(supplier=self.supplier, raw_name="VODKA 70CL")
        product, created = resolve_product(self.supplier, "  vodka 70cl  ")
        self.assertEqual(product, existing)
        self.assertFalse(created)

    def test_ean_match_wins_over_a_changed_name(self):
        """UBA renames its packaging codes ("FÛT 10/15/20/25/30/50 L" became
        "FÛT 15/16/20/30 L") while keeping the code - the EAN is the
        authority there, and those names score far too low to fuzzy match."""
        existing = make_product(supplier=self.supplier, raw_name="FÛT 15/16/20/30 L", ean="EMB01")
        product, created = resolve_product(self.supplier, "FÛT 10/15/20/25/30/50 L", ean="EMB01")
        self.assertEqual(product, existing)
        self.assertFalse(created)

    def test_a_different_supplier_never_matches(self):
        make_product(supplier=self.supplier, raw_name="VODKA 70CL")
        other = make_supplier()
        product, created = resolve_product(other, "VODKA 70CL")
        self.assertTrue(created)
        self.assertEqual(product.supplier, other)

    def test_an_unknown_product_is_created_for_review(self):
        product, created = resolve_product(self.supplier, "RHUM AGRICOLE 70CL", ean="123")
        self.assertTrue(created)
        self.assertEqual(product.raw_name, "RHUM AGRICOLE 70CL")
        self.assertEqual(product.ean, "123")
        self.assertTrue(product.needs_review)


class FuzzyMatchAcceptsFormattingDriftTests(TestCase):
    """What the fuzzy step is actually for."""

    def setUp(self):
        self.supplier = make_supplier()

    def assert_matches(self, existing_name, incoming_name):
        existing = make_product(supplier=self.supplier, raw_name=existing_name)
        product, created = resolve_product(self.supplier, incoming_name)
        self.assertFalse(created, f"{incoming_name!r} should have matched {existing_name!r}")
        self.assertEqual(product, existing)

    def test_spacing_drift_in_the_size(self):
        self.assert_matches("SOBIESKI VODKA 70CL", "SOBIESKI VODKA 70 CL")

    def test_a_trailing_supplier_suffix(self):
        self.assert_matches("MARGUERITE NEIPA FUT 20L 6°", "MARGUERITE NEIPA FUT 20L 6° MC")

    def test_word_order_drift(self):
        self.assert_matches("VODKA SOBIESKI 70CL", "SOBIESKI VODKA 70CL")


class FuzzyMatchRejectsDifferentProductsTests(TestCase):
    """B3. Every case below is a merge that really happened in the live
    database. They all share one giveaway: a number that differs. A
    different number - size, strength, ageing, pack count - means a
    different product, whatever the string similarity says.
    """

    def setUp(self):
        self.supplier = make_supplier()

    def assert_distinct(self, existing_name, incoming_name):
        make_product(supplier=self.supplier, raw_name=existing_name)
        product, created = resolve_product(self.supplier, incoming_name)
        self.assertTrue(
            created,
            f"{incoming_name!r} was silently merged into {existing_name!r}",
        )
        self.assertEqual(product.raw_name, incoming_name)
        self.assertEqual(Product.objects.filter(supplier=self.supplier).count(), 2)

    def test_different_bottle_size(self):
        self.assert_distinct("RICARD 45D 1L", "RICARD 45D 1.5L")

    def test_different_bottle_size_in_centilitres(self):
        self.assert_distinct("GENEPI 40D 70CL", "GENEPI 40D 50CL")

    def test_wildly_different_size(self):
        self.assert_distinct("RIOBA PUR JUS TOMATE 25CL VP", "RIOBA PUR JUS TOMATE 1L VP")

    def test_different_size_units_entirely(self):
        self.assert_distinct("MPRO NETT MU BACT PIN 5L", "MPRO NETT MU BACT PIN 750ML")

    def test_different_alcohol_strength(self):
        self.assert_distinct("VCE RGE 12D 10L", "VCE RGE 11D 10L")

    def test_barely_different_alcohol_strength(self):
        self.assert_distinct("MARTINI BLC 14.4D 1L", "MARTINI BLC 14.5D 1L")

    def test_different_fat_percentage(self):
        self.assert_distinct("CREME FRAI EP 15% 50CL ROCHAM", "CREME FRAI EP 30% 50CL ROCHAM")

    def test_different_ageing(self):
        self.assert_distinct("COMTE AOP 12M 800GENV MDF", "COMTE AOP 18M 800GENV MDF")

    def test_different_pack_size(self):
        self.assert_distinct("MPRO SAC POUB COULISS 30X100L", "MPRO SAC POUB COULISS 30X50L")

    def test_different_glass_capacity(self):
        self.assert_distinct("MP 12 VERRES BIERE LILITH 58CL", "MP 12 VERRES BIERE LILITH 28CL")

    def test_different_equipment_dimension(self):
        self.assert_distinct("MP BAC GASTRO INOX GN 1/6 H150", "MP BAC GASTRO INOX GN 1/6 H100")

    def test_a_different_recipe_with_identical_numbers_still_needs_the_score(self):
        """"ZERO" is a word, not a number, so the numeric guard can't help
        here - this one is on the similarity score alone."""
        self.assert_distinct(
            "Consigne COCA COLA ZERO 33 CL X 24 VC", "Consigne COCA COLA 33 CL X 24 VC"
        )


class FuzzyMatchScoringTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier()

    def test_an_unrelated_name_is_never_matched(self):
        make_product(supplier=self.supplier, raw_name="SOBIESKI VODKA 70CL")
        _product, created = resolve_product(self.supplier, "COMTE AOP 12M 800GENV MDF")
        self.assertTrue(created)

    def test_matching_against_an_empty_catalogue(self):
        product, created = resolve_product(self.supplier, "VODKA 70CL")
        self.assertTrue(created)
        self.assertEqual(product.raw_name, "VODKA 70CL")

    def test_the_best_of_several_candidates_wins(self):
        make_product(supplier=self.supplier, raw_name="GIN GIBSONS 37.5D 70CL")
        target = make_product(supplier=self.supplier, raw_name="SOBIESKI VODKA 70CL")
        product, created = resolve_product(self.supplier, "SOBIESKI VODKA 70 CL")
        self.assertFalse(created)
        self.assertEqual(product, target)
