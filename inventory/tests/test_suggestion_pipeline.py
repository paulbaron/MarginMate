"""The « À classer » suggestion pipeline end to end: which source answers, in
what order, with what confidence; what the product's own invoice line says
against a copied conversion factor; « Approuver les N sûres » and the stored
suggestions it must not trust blindly; and what the panel says about where
each suggestion comes from. Data invented.
"""

from collections import Counter
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from inventory.models import StockType, UnitChoices
from inventory.product_matching_rules import (
    apply_rules_to_pending_products,
    classified_fingerprint,
    is_current,
    suggest_for_product,
)
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type, make_supplier

HTMX = {"HTTP_HX_REQUEST": "true"}


def bought(product, quantity=1, colisage=1, total_volume="0"):
    """One invoice line, what extract_quantity_for_product reads."""
    make_invoice_line(
        invoice=make_invoice(supplier=product.supplier),
        product=product,
        quantity=quantity,
        colisage=colisage,
        total_ht="10",
        total_volume=total_volume,
    )


class SuggestionOrderTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")
        self.vodka = make_stock_type(name="Vodka premium", unit=UnitChoices.LITRE, category="Spiritueux")
        self.classified = make_product(
            supplier=self.supplier, raw_name="VODKA TESTBRAND 70CL", stock_type=self.vodka, stock_equivalent="0.7"
        )
        bought(self.classified, quantity=6)

    def test_a_classified_neighbour_beats_the_rule(self):
        pending = make_product(supplier=self.supplier, raw_name="VODKA TESTBRAND 1L")
        bought(pending, quantity=6)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "neighbour")
        # The rule would say « Vodka »; the owner files this brand elsewhere.
        self.assertEqual(suggestion["stock_type_name"], "Vodka premium")
        self.assertEqual(suggestion["matched_stock_type_id"], self.vodka.pk)
        self.assertFalse(suggestion["is_new_stock_type"])
        self.assertEqual(suggestion["new_stock_type_category"], "Spiritueux")
        self.assertEqual(suggestion["new_stock_type_unit"], UnitChoices.LITRE)
        # Another bottle size: the factor is read off this product's own name.
        self.assertEqual(suggestion["stock_equivalent"], "1")
        self.assertEqual(suggestion["confidence"], "high")
        self.assertIn("VODKA TESTBRAND 70CL", suggestion["reasoning"])
        self.assertIn("Vodka premium", suggestion["reasoning"])
        self.assertIn("conditionnement différent", suggestion["reasoning"])
        self.assertEqual(suggestion["neighbour_product_id"], self.classified.pk)

    def test_the_same_pack_copies_the_neighbours_factor(self):
        cups = make_stock_type(name="Timbales papier", unit=UnitChoices.UNIT, category="Consommables")
        neighbour = make_product(
            supplier=self.supplier, raw_name="50 TIMBALE PAPIER BLC 25CL", stock_type=cups, stock_equivalent="50"
        )
        bought(neighbour)
        pending = make_product(supplier=self.supplier, raw_name="TIMBALE PAPIER BLC 25CL X50")
        bought(pending)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "neighbour")
        self.assertEqual(suggestion["stock_equivalent"], "50")
        self.assertEqual(suggestion["confidence"], "high")
        self.assertIn("conversion reprise", suggestion["reasoning"])

    def test_another_pack_of_a_unit_counted_article_is_never_sure(self):
        cups = make_stock_type(name="Timbales papier", unit=UnitChoices.UNIT, category="Consommables")
        neighbour = make_product(
            supplier=self.supplier, raw_name="50 TIMBALE PAPIER BLC 25CL", stock_type=cups, stock_equivalent="50"
        )
        bought(neighbour)
        pending = make_product(supplier=self.supplier, raw_name="100 TIMBALE PAPIER BLC 25CL")
        bought(pending)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "neighbour")
        self.assertEqual(suggestion["stock_type_name"], "Timbales papier")
        # The name's own count, offered - but how many a line counts is the
        # supplier's convention, so a look is asked for.
        self.assertEqual(suggestion["stock_equivalent"], "100")
        self.assertEqual(suggestion["confidence"], "medium")

    def test_the_first_variant_is_offered_its_base_article_but_never_as_sure(self):
        """A twin nothing has taught apart yet: offered the base article at
        medium with the pack's factor copied - never high, so « Approuver les
        sûres » leaves it to a person. Once the owner files the twin under its
        own article, the word is learned and the twin's next pack is found
        under the twin's article, not the base one."""
        cola = make_stock_type(name="Cola testbrand VC", unit=UnitChoices.UNIT, category="Soft")
        base = make_product(
            supplier=self.supplier, raw_name="COLA TESTBRAND 33CL X24 VC", stock_type=cola, stock_equivalent="24"
        )
        bought(base)
        twin = make_product(supplier=self.supplier, raw_name="COLA TESTBRAND ZERO 33CL X24 VC")
        bought(twin)
        suggestion = suggest_for_product(twin)
        self.assertEqual(suggestion["source"], "neighbour")
        self.assertEqual(suggestion["stock_type_name"], "Cola testbrand VC")
        self.assertEqual(suggestion["stock_equivalent"], "24")
        self.assertEqual(suggestion["confidence"], "medium")
        self.assertIn("à un mot près", suggestion["reasoning"])
        self.assertIn("ZERO", suggestion["reasoning"])

        zero = make_stock_type(name="Cola testbrand zero VC", unit=UnitChoices.UNIT, category="Soft")
        twin.stock_type = zero
        twin.stock_equivalent = Decimal("24")
        twin.save()
        next_pack = make_product(supplier=self.supplier, raw_name="COLA TESTBRAND ZERO 33CL X6 VC")
        bought(next_pack)
        suggestion = suggest_for_product(next_pack)
        self.assertEqual(suggestion["source"], "neighbour")
        self.assertEqual(suggestion["stock_type_name"], "Cola testbrand zero VC")
        self.assertEqual(suggestion["matched_stock_type_id"], zero.pk)
        # Another pack of an article counted by the unit: the factor is read
        # off this name, and how many a line counts is the supplier's
        # convention - offered for a look, never as sure.
        self.assertEqual(suggestion["confidence"], "medium")
        self.assertIn("conversion estimée", suggestion["reasoning"])

    def test_a_neighbour_with_nothing_to_size_the_factor_is_low(self):
        pending = make_product(supplier=self.supplier, raw_name="VODKA TESTBRAND")
        bought(pending)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "neighbour")
        self.assertEqual(suggestion["stock_type_name"], "Vodka premium")
        self.assertEqual(suggestion["confidence"], "low")
        self.assertIn("à vérifier", suggestion["reasoning"])

    def test_the_rule_answers_when_no_neighbour_does_and_is_never_high(self):
        pending = make_product(supplier=self.supplier, raw_name="RHUM TESTBRAND 70CL")
        bought(pending, quantity=6)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "rule")
        self.assertEqual(suggestion["stock_type_name"], "Rhum")
        self.assertTrue(suggestion["is_new_stock_type"])
        self.assertEqual(suggestion["stock_equivalent"], "0.7")
        self.assertEqual(suggestion["confidence"], "medium")
        # Said in French words - the word matched and the article the rule
        # names - never as the regular expression.
        self.assertIn("Règle", suggestion["reasoning"])
        self.assertIn("« RHUM » dans le nom", suggestion["reasoning"])
        self.assertIn("Rhum", suggestion["reasoning"])
        self.assertNotIn("\\", suggestion["reasoning"])
        self.assertNotIn("|", suggestion["reasoning"])

    def test_a_rule_with_alternatives_names_the_word_it_matched(self):
        pending = make_product(supplier=self.supplier, raw_name="PROSECCO TESTCANTINA 75CL")
        bought(pending, quantity=6)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "rule")
        self.assertIn("« PROSECCO » dans le nom", suggestion["reasoning"])
        self.assertNotIn("\\", suggestion["reasoning"])
        self.assertNotIn("(", suggestion["reasoning"].split(" ; ")[0])

    def test_the_raw_name_answers_last_and_is_never_high(self):
        pending = make_product(supplier=self.supplier, raw_name="OBJET INCONNU 500G")
        bought(pending)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "fallback")
        self.assertEqual(suggestion["stock_type_name"], "Objet inconnu")
        self.assertTrue(suggestion["is_new_stock_type"])
        self.assertEqual(suggestion["stock_equivalent"], "0.5")
        self.assertEqual(suggestion["confidence"], "medium")
        self.assertIn("nom de facture repris tel quel", suggestion["reasoning"])

    def test_a_raw_name_that_is_an_existing_article_keeps_that_articles_spelling(self):
        syrup = make_stock_type(name="Sirop Testfruit", unit=UnitChoices.LITRE, category="Soft")
        pending = make_product(supplier=self.supplier, raw_name="SIROP TESTFRUIT 1L")
        bought(pending)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "fallback")
        self.assertEqual(suggestion["stock_type_name"], "Sirop Testfruit")
        self.assertEqual(suggestion["matched_stock_type_id"], syrup.pk)
        self.assertFalse(suggestion["is_new_stock_type"])
        self.assertEqual(suggestion["confidence"], "medium")

    def test_a_product_with_no_line_at_all_still_gets_a_suggestion(self):
        pending = make_product(supplier=self.supplier, raw_name="VODKA TESTBRAND 1L")
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "neighbour")
        self.assertEqual(suggestion["confidence"], "low")

    def test_every_suggestion_is_stamped_with_the_classifications_it_was_made_against(self):
        pending = make_product(supplier=self.supplier, raw_name="VODKA TESTBRAND 1L")
        suggestion = suggest_for_product(pending)
        fingerprint = classified_fingerprint()
        self.assertEqual(suggestion["classified_fingerprint"], fingerprint)
        self.assertTrue(is_current(suggestion, fingerprint))
        # Nothing stored, an empty dict, one made before the stamp existed.
        self.assertFalse(is_current(None, fingerprint))
        self.assertFalse(is_current({}, fingerprint))
        self.assertFalse(is_current({"source": "neighbour", "confidence": "high"}, fingerprint))
        # The neighbour moves to another article: the stamp no longer holds.
        self.classified.stock_type = make_stock_type(name="Vodka ordinaire", unit=UnitChoices.LITRE)
        self.classified.save(update_fields=["stock_type"])
        self.assertFalse(is_current(suggestion, classified_fingerprint()))
        # So does its factor, and so does an article's name.
        moved = classified_fingerprint()
        self.classified.stock_equivalent = Decimal("1")
        self.classified.save(update_fields=["stock_equivalent"])
        self.assertNotEqual(classified_fingerprint(), moved)
        renamed = classified_fingerprint()
        self.vodka.name = "Vodka haut de gamme"
        self.vodka.save(update_fields=["name"])
        self.assertNotEqual(classified_fingerprint(), renamed)

    def test_apply_keeps_a_current_suggestion_and_remakes_every_other(self):
        current = {"source": "kept", "confidence": "high", "classified_fingerprint": classified_fingerprint()}
        with_current = make_product(supplier=self.supplier, raw_name="DEJA VU 1L", ai_suggestion=current)
        # Stored before the classifications moved, or before suggestions
        # carried the stamp at all: made again.
        stale = make_product(
            supplier=self.supplier,
            raw_name="VODKA TESTBRAND 1L",
            ai_suggestion={"source": "kept", "confidence": "high", "classified_fingerprint": "old"},
        )
        unstamped = make_product(
            supplier=self.supplier, raw_name="GIN TESTBRAND 70CL", ai_suggestion={"source": "kept"}
        )
        raw_case = make_product(supplier=self.supplier, raw_name="OBJET INCONNU")
        make_product(supplier=self.supplier, raw_name="LOYER", is_expense=True)
        self.assertEqual(apply_rules_to_pending_products(), Counter({"neighbour": 1, "rule": 1, "fallback": 1}))
        with_current.refresh_from_db()
        self.assertEqual(with_current.ai_suggestion, current)
        for product, source in ((stale, "neighbour"), (unstamped, "rule"), (raw_case, "fallback")):
            product.refresh_from_db()
            self.assertEqual(product.ai_suggestion["source"], source)
            self.assertTrue(is_current(product.ai_suggestion, classified_fingerprint()))
        self.assertEqual(apply_rules_to_pending_products(), Counter())


class LineAgainstCopiedFactorTests(TestCase):
    """The neighbour's factor is copied for the same pack - unless the
    product's OWN invoice line says otherwise. A right article at a wrong
    factor is silently wrong money."""

    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")
        self.other = make_supplier(code="TESTB", name="Fournisseur test")
        self.syrup = make_stock_type(name="Sirop testfruit", unit=UnitChoices.LITRE, category="Soft")

    def test_a_printed_volume_makes_the_factor_one_whatever_the_neighbour_says(self):
        neighbour = make_product(
            supplier=self.supplier, raw_name="SIROP TESTFRUIT 70CL", stock_type=self.syrup, stock_equivalent="0.7"
        )
        bought(neighbour, quantity=6)
        # The same bottle at another supplier, whose line prints the litres:
        # product_base_amount already counts them, so any other factor
        # multiplies litres by a bottle size.
        pending = make_product(supplier=self.other, raw_name="SIROP TESTFRUIT 70CL")
        bought(pending, quantity=6, total_volume="4.2")
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["source"], "neighbour")
        self.assertEqual(suggestion["stock_type_name"], "Sirop testfruit")
        self.assertEqual(suggestion["stock_equivalent"], "1")
        self.assertEqual(suggestion["confidence"], "medium")
        self.assertEqual(suggestion["factor_rule"], "line-contradicts-copy")
        # Both figures, in French.
        self.assertIn("le voisin est à 0.7", suggestion["reasoning"])
        self.assertIn("imprime son volume ou son poids", suggestion["reasoning"])
        self.assertIn(": 1, à vérifier", suggestion["reasoning"])

    def test_a_colisage_already_counting_the_items_makes_the_factor_one(self):
        cups = make_stock_type(name="Timbales papier", unit=UnitChoices.UNIT, category="Consommables")
        neighbour = make_product(
            supplier=self.supplier, raw_name="TIMBALE PAPIER BLC 25CL X50", stock_type=cups, stock_equivalent="50"
        )
        bought(neighbour)
        pending = make_product(supplier=self.other, raw_name="TIMBALE PAPIER BLC 25CL X50")
        bought(pending, quantity=100, colisage=50)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["stock_type_name"], "Timbales papier")
        self.assertEqual(suggestion["stock_equivalent"], "1")
        self.assertEqual(suggestion["confidence"], "medium")
        self.assertIn("le voisin est à 50", suggestion["reasoning"])
        self.assertIn("colis de 50", suggestion["reasoning"])

    def test_a_line_that_agrees_with_the_copy_makes_it_sure(self):
        oil = make_stock_type(name="Huile testolive", unit=UnitChoices.LITRE, category="Epicerie")
        neighbour = make_product(
            supplier=self.supplier, raw_name="HUILE TESTOLIVE 5L", stock_type=oil, stock_equivalent="1"
        )
        bought(neighbour, total_volume="5")
        pending = make_product(supplier=self.other, raw_name="HUILE TESTOLIVE 5L")
        bought(pending, quantity=2, total_volume="10")
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["stock_equivalent"], "1")
        self.assertEqual(suggestion["confidence"], "high")
        self.assertEqual(suggestion["factor_rule"], "copy-confirmed-by-line")
        self.assertIn("conversion reprise (1 produit = 1)", suggestion["reasoning"])
        self.assertIn("imprime son volume ou son poids", suggestion["reasoning"])

    def test_a_line_read_otherwise_keeps_the_copy_at_medium_and_says_both_figures(self):
        # The owner's convention for this bottle is 1; the extractor reads
        # the printed 70CL as 0,7. Neither is repaired: both are said.
        neighbour = make_product(
            supplier=self.supplier, raw_name="SIROP TESTFRUIT 70CL", stock_type=self.syrup, stock_equivalent="1"
        )
        bought(neighbour, quantity=6)
        pending = make_product(supplier=self.other, raw_name="SIROP TESTFRUIT 70CL")
        bought(pending, quantity=6)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["stock_equivalent"], "1")
        self.assertEqual(suggestion["confidence"], "medium")
        self.assertEqual(suggestion["factor_rule"], "copy-read-otherwise")
        self.assertIn("conversion du voisin reprise (1 produit = 1)", suggestion["reasoning"])
        self.assertIn("lue autrement sur cette ligne (0.7", suggestion["reasoning"])
        self.assertIn("à vérifier", suggestion["reasoning"])

    def test_another_pack_with_a_printed_volume_is_settled_by_the_line(self):
        neighbour = make_product(
            supplier=self.supplier, raw_name="SIROP TESTFRUIT 70CL", stock_type=self.syrup, stock_equivalent="0.7"
        )
        bought(neighbour, quantity=6)
        pending = make_product(supplier=self.other, raw_name="SIROP TESTFRUIT 1L")
        bought(pending, quantity=6, total_volume="6")
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["stock_equivalent"], "1")
        self.assertEqual(suggestion["confidence"], "high")
        self.assertEqual(suggestion["factor_rule"], "line-settles")
        self.assertIn("conditionnement différent, mais", suggestion["reasoning"])


class NoNumberPrintedTests(TestCase):
    """Two names printing no number at all have equal (empty) numeric
    signatures: nothing printed says they are one pack."""

    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")
        self.other = make_supplier(code="TESTB", name="Fournisseur test")

    def test_two_names_printing_no_number_are_not_one_pack(self):
        bags = make_stock_type(name="Sacs kraft", unit=UnitChoices.UNIT, category="Consommables")
        neighbour = make_product(
            supplier=self.supplier, raw_name="SACS PAPIER KRAFT", stock_type=bags, stock_equivalent="50"
        )
        bought(neighbour)
        for supplier in (self.supplier, self.other):
            with self.subTest(same_supplier=supplier is self.supplier):
                pending = make_product(supplier=supplier, raw_name="SAC PAPIER KRAFT")
                bought(pending)
                suggestion = suggest_for_product(pending)
                self.assertEqual(suggestion["stock_type_name"], "Sacs kraft")
                # The neighbour's factor is offered - it is the best guess -
                # but never as sure, whoever sold it.
                self.assertEqual(suggestion["stock_equivalent"], "50")
                self.assertEqual(suggestion["confidence"], "medium")
                self.assertEqual(suggestion["factor_rule"], "copy-no-number")
                self.assertIn(
                    "aucun nombre imprimé : conversion du voisin reprise, à vérifier", suggestion["reasoning"]
                )
                self.assertNotIn("même conditionnement", suggestion["reasoning"])

    def test_the_copy_is_sure_when_the_line_reads_the_same(self):
        cola = make_stock_type(name="Cola testbrand VC", unit=UnitChoices.UNIT, category="Soft")
        neighbour = make_product(
            supplier=self.supplier, raw_name="COLA TESTBRAND VC", stock_type=cola, stock_equivalent="1"
        )
        bought(neighbour, quantity=24, total_volume="7.92")
        pending = make_product(supplier=self.other, raw_name="COLA TESTBRAND VC")
        # 24 bottles of 33cl: the line's own arithmetic counts them one by one.
        bought(pending, quantity=24, total_volume="7.92")
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["stock_equivalent"], "1")
        self.assertEqual(suggestion["confidence"], "high")
        self.assertEqual(suggestion["factor_rule"], "copy-no-number-read-agrees")
        self.assertIn("aucun nombre imprimé, mais la ligne se lit de même", suggestion["reasoning"])

    def test_a_number_on_one_side_only_is_another_pack(self):
        bags = make_stock_type(name="Sacs kraft", unit=UnitChoices.UNIT, category="Consommables")
        neighbour = make_product(
            supplier=self.supplier, raw_name="SAC PAPIER KRAFT", stock_type=bags, stock_equivalent="1"
        )
        bought(neighbour)
        pending = make_product(supplier=self.supplier, raw_name="50 SAC PAPIER KRAFT")
        bought(pending)
        suggestion = suggest_for_product(pending)
        self.assertEqual(suggestion["stock_type_name"], "Sacs kraft")
        self.assertEqual(suggestion["stock_equivalent"], "50")
        self.assertEqual(suggestion["confidence"], "medium")
        self.assertIn("conditionnement différent", suggestion["reasoning"])


def suggestion(stock_type, confidence, source="neighbour", stock_equivalent="1"):
    """A stored suggestion as the panel would have drawn it a moment ago:
    stamped with the classifications as they stand, so it is kept as it is."""
    return {
        "source": source,
        "stock_type_name": stock_type.name,
        "new_stock_type_category": stock_type.category,
        "new_stock_type_unit": stock_type.unit,
        "stock_equivalent": stock_equivalent,
        "confidence": confidence,
        "reasoning": f"Même chose que « TEST » (Metro), déjà rangé dans « {stock_type.name} ».",
        "matched_stock_type_id": stock_type.pk,
        "is_new_stock_type": False,
        "classified_fingerprint": classified_fingerprint(),
    }


class ApproveSureSuggestionsTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")
        self.rum = make_stock_type(name="Rhum", unit=UnitChoices.LITRE, category="Spiritueux")
        self.gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE, category="Spiritueux")
        self.url = reverse("inventory:approve_all_suggestions")

    def pending(self, name, stock_type, confidence, **kwargs):
        product = make_product(
            supplier=self.supplier, raw_name=name, ai_suggestion=suggestion(stock_type, confidence, **kwargs)
        )
        bought(product)
        return product

    def message(self, response):
        return str(next(iter(get_messages(response.wsgi_request))))

    def test_only_the_sure_ones_are_linked_whatever_their_ids(self):
        sure_first = self.pending("RHUM A 70CL", self.rum, "high", stock_equivalent="0.7")
        medium = self.pending("RHUM B 70CL", self.rum, "medium")
        make_product(supplier=self.supplier, raw_name="TROU").delete()  # a gap in the ids
        low = self.pending("GIN C 70CL", self.gin, "low")
        sure_last = self.pending("GIN D 1L", self.gin, "high", source="rule")
        response = self.client.post(self.url, {"confiance": "haute"})
        self.assertRedirects(response, reverse("inventory:stock_list"), fetch_redirect_response=False)
        for product in (sure_first, medium, low, sure_last):
            product.refresh_from_db()
        self.assertEqual(sure_first.stock_type, self.rum)
        self.assertEqual(sure_first.stock_equivalent, Decimal("0.7"))
        self.assertEqual(sure_last.stock_type, self.gin)
        self.assertIsNone(medium.stock_type)
        self.assertIsNone(low.stock_type)
        # The ones left keep their suggestion: nothing to make again.
        self.assertEqual(medium.ai_suggestion["confidence"], "medium")
        self.assertIn("2 produit(s) rattaché(s) automatiquement d'après les suggestions sûres", self.message(response))

    def test_a_product_classified_meanwhile_is_left_as_it_was_classified(self):
        product = self.pending("RHUM A 70CL", self.rum, "high")
        # Another tab classified it elsewhere just before the click.
        product.stock_type = self.gin
        product.save(update_fields=["stock_type"])
        self.client.post(self.url, {"confiance": "haute"})
        product.refresh_from_db()
        self.assertEqual(product.stock_type, self.gin)

    def test_an_unknown_scope_approves_nothing(self):
        product = self.pending("RHUM A 70CL", self.rum, "high")
        response = self.client.post(self.url, {"confiance": "moyenne"})
        self.assertRedirects(response, reverse("inventory:stock_list"), fetch_redirect_response=False)
        product.refresh_from_db()
        self.assertIsNone(product.stock_type)
        self.assertIn("Choix inconnu", self.message(response))

    def test_nothing_sure_says_so(self):
        self.pending("RHUM B 70CL", self.rum, "medium")
        response = self.client.post(self.url, {"confiance": "haute"})
        self.assertIn("Aucune suggestion sûre", self.message(response))

    def test_a_factor_whose_movement_no_column_holds_is_left_to_classify(self):
        """0.0001 is a factor; a bottle bought 183.50 EUR is then 1 835 000
        EUR a unit, which a stock movement cannot store."""
        product = make_product(
            supplier=self.supplier,
            raw_name="RHUM X 70CL",
            ai_suggestion=suggestion(self.rum, "high", stock_equivalent="0.0001"),
        )
        make_invoice_line(product=product, quantity=1, total_ht="183.50")
        response = self.client.post(self.url, {"confiance": "haute"})
        product.refresh_from_db()
        self.assertIsNone(product.stock_type)
        self.assertIn("facteur de conversion hors limites", self.message(response))
        self.assertEqual(self.client.get(reverse("inventory:stock_list")).status_code, 200)

    def test_approving_everything_still_takes_every_confidence(self):
        products = [
            self.pending("RHUM A 70CL", self.rum, "high"),
            self.pending("RHUM B 70CL", self.rum, "medium"),
            self.pending("GIN C 70CL", self.gin, "low"),
        ]
        self.client.post(self.url)
        for product in products:
            product.refresh_from_db()
            self.assertIsNotNone(product.stock_type)


class StaleSuggestionTests(TestCase):
    """A stored « haute » is a claim about the classifications at the moment
    the panel was drawn. Once the neighbour it rests on has moved, been
    unlinked, or lost its article, it is made again - when the panel is drawn
    and, before anything is booked, under « Approuver les sûres »."""

    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")
        self.premium = make_stock_type(name="Vodka premium", unit=UnitChoices.LITRE, category="Spiritueux")
        self.neighbour = make_product(
            supplier=self.supplier, raw_name="VODKA TESTBRAND 70CL", stock_type=self.premium, stock_equivalent="0.7"
        )
        bought(self.neighbour, quantity=6)
        self.pending = make_product(supplier=self.supplier, raw_name="VODKA TESTBRAND 1L")
        bought(self.pending, quantity=6)
        self.panel_url = reverse("inventory:review_queue")
        self.approve_url = reverse("inventory:approve_all_suggestions")
        # Drawn once: the suggestion is stored, sure, naming the neighbour's
        # article and the neighbour itself.
        response = self.client.get(self.panel_url, **HTMX)
        self.assertEqual(response.context["sure_count"], 1)
        self.pending.refresh_from_db()
        self.stored = self.pending.ai_suggestion
        self.assertEqual(self.stored["confidence"], "high")
        self.assertEqual(self.stored["matched_stock_type_id"], self.premium.pk)
        self.assertEqual(self.stored["neighbour_product_id"], self.neighbour.pk)

    def message(self, response):
        return str(next(iter(get_messages(response.wsgi_request))))

    def test_a_neighbour_moved_to_another_article_takes_the_suggestion_with_it(self):
        ordinary = make_stock_type(name="Vodka ordinaire", unit=UnitChoices.LITRE, category="Spiritueux")
        self.neighbour.stock_type = ordinary
        self.neighbour.save(update_fields=["stock_type"])
        # The panel redraws it from the classifications as they stand.
        response = self.client.get(self.panel_url, **HTMX)
        page = response.content.decode()
        self.assertIn("Vodka ordinaire", page)
        self.assertNotIn("Vodka premium", page.split('<datalist id="stock-type-datalist">')[0])
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.ai_suggestion["matched_stock_type_id"], ordinary.pk)
        self.assertNotEqual(self.pending.ai_suggestion["classified_fingerprint"], self.stored["classified_fingerprint"])

    def test_approving_the_sure_ones_never_books_the_article_the_neighbour_left(self):
        ordinary = make_stock_type(name="Vodka ordinaire", unit=UnitChoices.LITRE, category="Spiritueux")
        self.neighbour.stock_type = ordinary
        self.neighbour.save(update_fields=["stock_type"])
        # Nobody redrew the panel: the stored « haute » still names Vodka premium.
        response = self.client.post(self.approve_url, {"confiance": "haute"})
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.stock_type, ordinary)
        self.assertEqual(self.pending.stock_equivalent, Decimal("1"))
        self.assertIn("1 produit(s) rattaché(s)", self.message(response))
        self.assertIn("1 suggestion(s) refaite(s) d'abord", self.message(response))

    def test_a_neighbour_unlinked_leaves_the_product_to_classify(self):
        self.neighbour.stock_type = None
        self.neighbour.save(update_fields=["stock_type"])
        response = self.client.post(self.approve_url, {"confiance": "haute"})
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.stock_type)
        # Made again: without the neighbour the rule answers, never as sure.
        self.assertEqual(self.pending.ai_suggestion["source"], "rule")
        self.assertEqual(self.pending.ai_suggestion["confidence"], "medium")
        self.assertIn("Aucune suggestion sûre", self.message(response))
        self.assertIn("1 laissé(s) à classer, leur suggestion refaite n'étant plus sûre", self.message(response))

    def test_a_neighbour_whose_article_is_gone_books_nothing_and_resurrects_nothing(self):
        self.neighbour.stock_type = None
        self.neighbour.save(update_fields=["stock_type"])
        self.premium.delete()
        response = self.client.post(self.approve_url, {"confiance": "haute"})
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.stock_type)
        self.assertFalse(StockType.objects.filter(name__iexact="Vodka premium").exists())
        self.assertIn("Aucune suggestion sûre", self.message(response))

    def test_a_current_suggestion_is_booked_as_shown(self):
        response = self.client.post(self.approve_url, {"confiance": "haute"})
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.stock_type, self.premium)
        self.assertNotIn("refaite", self.message(response))


class PanelSaysWhereSuggestionsComeFromTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="METRO", name="Metro")
        self.rum = make_stock_type(name="Rhum", unit=UnitChoices.LITRE, category="Spiritueux")
        self.url = reverse("inventory:review_queue")

    def panel(self):
        return self.client.get(self.url, **HTMX)

    def test_each_card_names_its_source_and_the_sure_button_counts_the_highs(self):
        high = suggestion(self.rum, "high")
        high["reasoning"] = (
            "Même chose que « RHUM VOISIN 70CL » (Metro), déjà rangé dans « Rhum » ; même conditionnement."
        )
        make_product(supplier=self.supplier, raw_name="RHUM A 70CL", ai_suggestion=high)
        make_product(
            supplier=self.supplier, raw_name="RHUM B 70CL", ai_suggestion=suggestion(self.rum, "medium", source="rule")
        )
        make_product(
            supplier=self.supplier, raw_name="OBJET C", ai_suggestion=suggestion(self.rum, "low", source="fallback")
        )
        response = self.panel()
        page = response.content.decode()
        self.assertIn("🔗 Déjà classé", page)
        self.assertIn("📋 Règle", page)
        self.assertIn("🏷️ Nom brut", page)
        self.assertIn("RHUM VOISIN 70CL", page)
        self.assertIn('data-source="neighbour" data-confidence="high"', page)
        self.assertContains(response, "Approuver la suggestion sûre")
        self.assertContains(response, 'name="confiance" value="haute"')
        self.assertIn("Approuver 1 suggestion(s) de confiance haute", page)
        self.assertIn("1 de confiance moyenne, 1 de confiance basse", page)
        self.assertContains(response, "Approuver les 3 suggestions")
        self.assertEqual(response.context["sure_count"], 1)
        self.assertEqual(response.context["source_counts"], Counter({"neighbour": 1, "rule": 1, "fallback": 1}))

    def test_the_explainer_states_the_three_sources_in_order(self):
        make_product(
            supplier=self.supplier, raw_name="RHUM B 70CL", ai_suggestion=suggestion(self.rum, "medium", source="rule")
        )
        page = self.panel().content.decode()
        self.assertIn("Trois sources, essayées dans cet ordre", page)
        self.assertLess(page.index("Déjà classé</strong>"), page.index("Règle</strong>"))
        self.assertLess(page.index("Règle</strong>"), page.index("Nom brut</strong>"))
        # The template wraps its sentences: read them as a browser does.
        prose = " ".join(page.split())
        # The actual rules, as the code applies them - the conditions for
        # « haute », in the explainer's short form (the owner cut the
        # detailed cases on 01/10/2026; CLAUDE.md keeps them).
        self.assertIn("Confiance haute seulement quand les mots sont identiques (au pluriel près)", prose)
        self.assertIn("la conversion sûre", prose)
        self.assertIn("pour un article compté à l'unité, la taille et le colisage identiques", prose)
        self.assertIn("sinon moyenne ou basse", prose)
        self.assertIn("la plus faible", prose)
        self.assertIn(
            "« Approuver les sûres » ne prend que les hautes ; « Approuver les suggestions » prend tout", prose
        )
        # Nothing sure: no button promising to approve it.
        self.assertNotIn("sûre", page.split("D'où viennent")[0])
        self.assertIn("Approuver la suggestion</button>", page)

    def test_the_sure_button_says_how_many(self):
        for name in ("RHUM A 70CL", "RHUM B 70CL"):
            make_product(supplier=self.supplier, raw_name=name, ai_suggestion=suggestion(self.rum, "high"))
        self.assertContains(self.panel(), "Approuver les 2 sûres")

    def test_a_stored_suggestion_the_classifications_have_outrun_is_not_shown(self):
        """Stamped against classifications that moved since (here: stored
        before the stamp existed), a suggestion is made again before the
        panel shows it - the card says what the pipeline says today."""
        stale = suggestion(self.rum, "high")
        stale["reasoning"] = "Même chose que « RHUM VOISIN 70CL » (Metro), déjà rangé dans « Rhum »."
        del stale["classified_fingerprint"]
        product = make_product(supplier=self.supplier, raw_name="RHUM A 70CL", ai_suggestion=stale)
        bought(product, quantity=6)
        response = self.panel()
        page = response.content.decode()
        self.assertNotIn("RHUM VOISIN 70CL", page)
        self.assertIn("📋 Règle", page)
        self.assertEqual(response.context["sure_count"], 0)
        product.refresh_from_db()
        self.assertEqual(product.ai_suggestion["source"], "rule")
