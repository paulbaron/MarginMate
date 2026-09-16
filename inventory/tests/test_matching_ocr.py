"""Matching names read by OCR off a photographed receipt.

A recogniser misreads: "5OOG" for "500G", "BAGUETIE" for "BAGUETTE",
"POMME JULIETX4" for "POMME JULIET X4". Each misreading used to create a
brand-new product, so the same lemons ended up as three products with three
price histories. `ocr_tolerant=True` compares with the same shop's existing
names instead - but it must never give up the guarantee the strict matcher
was hardened for: a different number is a different product.
"""

import math

from django.test import SimpleTestCase, TestCase

from inventory.matching import fold_for_ocr, ocr_edit_cost, resolve_product, resolve_products
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier


class FoldForOcrTests(SimpleTestCase):
    def test_spacing_punctuation_case_and_accents_are_dropped(self):
        self.assertEqual(fold_for_ocr("Pomme Juliet x4"), "POMMEJULIETX4")
        self.assertEqual(fold_for_ocr("AROM.BASILIC"), fold_for_ocr("AROM BASILIC"))
        self.assertEqual(fold_for_ocr("Épices  Mélangées"), "EPICESMELANGEES")


class OcrEditCostTests(SimpleTestCase):
    def test_lookalikes_are_cheap(self):
        self.assertAlmostEqual(ocr_edit_cost("5OOG", "500G"), 0.6)
        self.assertAlmostEqual(ocr_edit_cost("IKG", "1KG"), 0.3)

    def test_a_different_digit_is_impossible(self):
        self.assertEqual(ocr_edit_cost("70CL", "75CL"), math.inf)

    def test_an_added_or_dropped_digit_is_impossible(self):
        self.assertEqual(ocr_edit_cost("1L", "15L"), math.inf)
        self.assertEqual(ocr_edit_cost("X4", "X"), math.inf)

    def test_a_misread_letter_is_one_typo(self):
        self.assertEqual(ocr_edit_cost("BAGUETIE", "BAGUETTE"), 1.0)


class OcrTolerantResolveTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="SHOP", name="Shop")

    def resolve(self, raw_name):
        return resolve_product(self.shop, raw_name, ocr_tolerant=True)

    def assertFinds(self, raw_name, existing):
        product, created = self.resolve(raw_name)
        self.assertEqual(product, existing, f"{raw_name!r} should have found {existing.raw_name!r}")
        self.assertFalse(created)

    def assertCreatesNew(self, raw_name):
        product, created = self.resolve(raw_name)
        self.assertTrue(created, f"{raw_name!r} was attached to {product.raw_name!r}")
        self.assertEqual(product.raw_name, raw_name)
        self.assertTrue(product.needs_review)

    # --- what it is for --------------------------------------------------------

    def test_a_letter_o_read_for_a_zero(self):
        self.assertFinds("CITRON SHT 5OOG", make_product(supplier=self.shop, raw_name="CITRON SHT 500G"))

    def test_a_zero_read_for_a_letter_o(self):
        self.assertFinds("P0MME JULIET X4", make_product(supplier=self.shop, raw_name="POMME JULIET X4"))

    def test_a_dropped_space(self):
        self.assertFinds("POMME JULIETX4", make_product(supplier=self.shop, raw_name="POMME JULIET X4"))

    def test_punctuation_read_differently(self):
        self.assertFinds("4X AROM BASILIC", make_product(supplier=self.shop, raw_name="4X AROM.BASILIC"))

    def test_one_misread_letter_in_a_real_word(self):
        self.assertFinds("BAGUETIE BLANC", make_product(supplier=self.shop, raw_name="BAGUETTE BLANC"))
        self.assertFinds("CRANGE", make_product(supplier=self.shop, raw_name="ORANGE"))

    def test_several_mistakes_on_one_long_name(self):
        """A look-alike and a real typo together, on a name long enough to
        carry both."""
        self.assertFinds(
            "SUCRE R0UX IKG CASS0NADE",
            make_product(supplier=self.shop, raw_name="SUCRE ROUX 1KG CASSONADE"),
        )

    # --- what it must never do -------------------------------------------------

    def test_a_different_weight_is_a_different_product(self):
        make_product(supplier=self.shop, raw_name="CITRON SHT 500G")
        self.assertCreatesNew("CITRON SHT 250G")

    def test_a_different_pack_count_is_a_different_product(self):
        make_product(supplier=self.shop, raw_name="POMME JULIET X4")
        self.assertCreatesNew("POMME JULIET X6")

    def test_a_different_volume_is_a_different_product(self):
        """The case the strict matcher was hardened for, which a forgiving
        matcher must not reopen."""
        make_product(supplier=self.shop, raw_name="RICARD 45D 1L")
        self.assertCreatesNew("RICARD 45D 1.5L")

    def test_an_unnamed_line_never_takes_another_prices_name(self):
        """A till that prints no names is named by price; 0,75 is not 0,70."""
        make_product(supplier=self.shop, raw_name="Article divers (0.70 EUR/u)")
        self.assertCreatesNew("Article divers (0.75 EUR/u)")

    def test_a_short_name_forgives_lookalikes_but_not_typos(self):
        oeuf = make_product(supplier=self.shop, raw_name="OEUF")
        self.assertFinds("0EUF", oeuf)
        make_product(supplier=self.shop, raw_name="THE")
        self.assertCreatesNew("TEE")

    def test_too_many_differences_is_a_new_product(self):
        make_product(supplier=self.shop, raw_name="BAGUETTE BLANC")
        self.assertCreatesNew("BAGUE TRADITION")

    def test_two_equally_close_names_are_not_guessed_between(self):
        make_product(supplier=self.shop, raw_name="BAGUETTE BLANC")
        make_product(supplier=self.shop, raw_name="BAGUETTE BLANK")
        self.assertCreatesNew("BAGUETTE BLANG")

    def test_another_shops_names_are_not_consulted(self):
        other = make_supplier(code="OTHER", name="Other")
        make_product(supplier=other, raw_name="CITRON SHT 500G")
        self.assertCreatesNew("CITRON SHT 5OOG")

    def test_a_digital_invoice_keeps_the_strict_matcher(self):
        """Without the flag nothing changes: a supplier's own PDF has no OCR
        mistakes to forgive, only real products to keep apart."""
        make_product(supplier=self.shop, raw_name="CITRON SHT 500G")
        _product, created = resolve_product(self.shop, "CITRON SHT 5OOG")
        self.assertTrue(created)


class KnownReadingsTests(TestCase):
    """A product is also known by every name its receipts were read as: its
    own name is only whichever reading came first."""

    def setUp(self):
        self.shop = make_supplier(code="SHOP", name="Shop")

    def was_read(self, product, name):
        make_invoice_line(invoice=make_invoice(supplier=product.supplier), product=product, raw_name=name, read_as=name)

    def resolve(self, raw_name):
        return resolve_product(self.shop, raw_name, ocr_tolerant=True)

    def test_a_reading_is_a_name_the_product_is_known_by(self):
        """Two mistakes from BAGUETTE BLAND, one from how it was read once."""
        baguette = make_product(supplier=self.shop, raw_name="BAGUETTE BLAND")
        self.was_read(baguette, "BAGUETTE BLANC")
        self.assertEqual(self.resolve("AGUETTE BLANC"), (baguette, False))

    def test_another_shops_readings_are_not_consulted(self):
        other = make_supplier(code="OTHER", name="Other")
        self.was_read(make_product(supplier=other, raw_name="BAGUETTE BLAND"), "AGUETTE BLANC")
        make_product(supplier=self.shop, raw_name="BAGUETTE BLAND")
        _product, created = self.resolve("AGUETTE BLANC")
        self.assertTrue(created)

    def test_a_reading_as_close_as_another_product_is_not_guessed_between(self):
        plain = make_product(supplier=self.shop, raw_name="BAGUETTE BLANC")
        self.was_read(plain, "BAGUETTE BLAVC")
        make_product(supplier=self.shop, raw_name="BAGUETTE BLAVE")
        _product, created = self.resolve("BAGUETTE BLAVD")
        self.assertTrue(created)

    def test_a_reading_never_bridges_a_different_number(self):
        lemons = make_product(supplier=self.shop, raw_name="CITRON SHT 500G")
        self.was_read(lemons, "CITRON SHT 5OOG")
        _product, created = self.resolve("CITRON SHT 250G")
        self.assertTrue(created)

    def test_a_digital_invoice_ignores_readings(self):
        baguette = make_product(supplier=self.shop, raw_name="BAGUETTE BLAND")
        self.was_read(baguette, "BAGUETTE BLANC")
        _product, created = resolve_product(self.shop, "AGUETTE BLANC")
        self.assertTrue(created)


class ResolveProductsTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="SHOP", name="Shop")

    def test_without_ocr_each_name_stands_alone(self):
        make_product(supplier=self.shop, raw_name="CITRON SHT 500G")
        [(_product, created)] = resolve_products(self.shop, [("CITRON SHT 5OOG", "")])
        self.assertTrue(created)

    def test_a_name_repeated_on_one_document_is_one_product(self):
        for ocr_tolerant in (False, True):
            with self.subTest(ocr_tolerant=ocr_tolerant):
                name = f"MENTHE {'OCR' if ocr_tolerant else 'PDF'}"
                (first, created), (second, created_again) = resolve_products(
                    self.shop, [(name, ""), (name, "")], ocr_tolerant=ocr_tolerant
                )
                self.assertEqual((second, created, created_again), (first, True, False))

    def test_no_line_at_all(self):
        self.assertEqual(resolve_products(self.shop, [], ocr_tolerant=True), [])
