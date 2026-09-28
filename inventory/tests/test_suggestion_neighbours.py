"""The neighbour source of the « À classer » suggestions: is a product to
classify the same thing as one the owner already classified?

Pure logic, no database: the index is built from hand-made neighbours.
Every name here is invented; the SHAPES are the ones that fooled a
token-set similarity on the real data - two sizes of one cup filed as two
articles, a flavour, a deposit in front of the goods, a short name inside a
long one, a house code one character off, a word that is the prefix of
another word.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.test import SimpleTestCase

from inventory.models import UnitChoices
from inventory.product_matching_rules import (
    ClassifiedNeighbours,
    NameShape,
    Neighbour,
    exact_or_plural,
    fold_name,
    least_confident,
    one_word_apart,
    same_word,
    same_words,
)
from inventory.quantity_extraction import size_signature


@dataclass
class Pending:
    """What `ClassifiedNeighbours.find` reads off a product."""

    raw_name: str
    supplier_id: int = 1
    pk: int = 0


_ARTICLE_IDS: dict[str, int] = {}
_PRODUCT_IDS = iter(range(1000, 10_000))


def classified(raw_name, article, unit=UnitChoices.UNIT, supplier_id=1, stock_equivalent="1", product_id=None):
    return Neighbour(
        product_id=product_id if product_id is not None else next(_PRODUCT_IDS),
        raw_name=raw_name,
        supplier_id=supplier_id,
        supplier_name=f"Fournisseur {supplier_id}",
        stock_type_id=_ARTICLE_IDS.setdefault(article, len(_ARTICLE_IDS) + 1),
        stock_type_name=article,
        stock_type_unit=unit,
        stock_type_category="Consommables",
        stock_equivalent=Decimal(stock_equivalent),
        shape=NameShape.of(raw_name),
    )


class SizeSignatureTests(SimpleTestCase):
    def test_a_size_is_the_same_whatever_unit_it_is_printed_in(self):
        for name in ("COLA 33CL", "COLA 330ML", "COLA 0,33L", "COLA 0.33 L"):
            with self.subTest(name=name):
                self.assertEqual(size_signature(name), {(Decimal("0.33"), "L"): 1})
        self.assertEqual(size_signature("FROMAGE 500G"), size_signature("FROMAGE 0,5KG"))

    def test_counts_dimensions_lengths_and_bare_numbers_are_not_sizes(self):
        self.assertEqual(size_signature("50 TIMBALE PAPIER 25CL X4 20X20 15CM REF7001"), {(Decimal("0.25"), "L"): 1})
        self.assertEqual(size_signature("MPRO 300 PAILLE D8 18CM"), {})

    def test_a_pack_gives_its_item_size_and_a_can_its_catering_weight(self):
        self.assertEqual(size_signature("EAU 24X33CL"), {(Decimal("0.33"), "L"): 1})
        self.assertEqual(size_signature("SUCRE 10GX100"), {(Decimal("0.01"), "KG"): 1})
        self.assertEqual(size_signature("HARICOTS 4/4"), {(Decimal("0.85"), "KG"): 1})

    def test_two_sizes_are_two_entries_and_nothing_is_nothing(self):
        self.assertEqual(size_signature("COFFRET 70CL + 20CL"), {(Decimal("0.7"), "L"): 1, (Decimal("0.2"), "L"): 1})
        self.assertEqual(size_signature(""), {})
        self.assertEqual(size_signature("SAC"), {})


class WordTests(SimpleTestCase):
    def test_a_word_is_itself_an_abbreviation_or_a_plural(self):
        self.assertTrue(same_word("TIMBALE", "TIMBALE"))
        self.assertTrue(same_word("TIMB", "TIMBALE"))
        self.assertTrue(same_word("SACS", "SAC"))
        # Two letters stand for nothing on their own.
        self.assertFalse(same_word("PL", "PLATEAU"))
        self.assertFalse(same_word("FRAISE", "PASSION"))

    def test_one_typo_is_forgiven_on_a_long_word_only_and_never_in_a_code(self):
        self.assertTrue(same_word("PROSECO", "PROSECCO"))
        self.assertFalse(same_word("BRUN", "BRUT"))
        self.assertFalse(same_word("REF7001", "REF7002"))
        self.assertFalse(same_word("1REF7001", "REF7001"))

    def test_exact_or_plural_is_the_same_word_beyond_doubt(self):
        """What a SURE suggestion may rest on: a prefix is a guess about what
        a shortened word stands for, and pairs two words as readily as two
        spellings of one."""
        self.assertTrue(exact_or_plural("SAC", "SAC"))
        self.assertTrue(exact_or_plural("SACS", "SAC"))
        self.assertTrue(exact_or_plural("SAC", "SACS"))
        self.assertFalse(exact_or_plural("TIMB", "TIMBALE"))
        self.assertFalse(exact_or_plural("SAC", "SACHET"))
        self.assertFalse(exact_or_plural("VIN", "VINAIGRE"))
        self.assertFalse(exact_or_plural("PROSECO", "PROSECCO"))
        # A plural needs a word in front of the S.
        self.assertFalse(exact_or_plural("PL", "PLS"))
        self.assertFalse(exact_or_plural("", "S"))

    def test_same_words_pairs_every_word_once(self):
        self.assertTrue(same_words(frozenset({"TIMBALE", "PAPIER"}), frozenset({"PAPIER", "TIMB"})))
        self.assertFalse(same_words(frozenset({"TIMBALE", "PAPIER"}), frozenset({"TIMBALE", "PAPIER", "KRAFT"})))
        # « CONS. » abbreviates « CONSIGNE »: looked up both ways it stood for
        # « CONSIGNE » and for itself, and the goods matched their deposit.
        self.assertFalse(
            same_words(frozenset({"BOUTEILLE", "GAZ", "CONS"}), frozenset({"CONSIGNE", "BOUTEILLE", "GAZ", "CONS"}))
        )
        self.assertFalse(same_words(frozenset(), frozenset()))
        self.assertFalse(same_words(frozenset({"SAC"}), frozenset()))

    def test_same_words_can_be_asked_the_stricter_question(self):
        timbale, timb = frozenset({"TIMBALE", "PAPIER"}), frozenset({"PAPIER", "TIMB"})
        self.assertTrue(same_words(timbale, timb))
        self.assertFalse(same_words(timbale, timb, exact_or_plural))
        self.assertTrue(same_words(frozenset({"TIMBALES", "PAPIER"}), timbale, exact_or_plural))

    def test_one_word_apart_says_which_side_has_it(self):
        self.assertEqual(
            one_word_apart(frozenset({"SIROP", "TESTFRUIT", "BIO"}), frozenset({"SIROP", "TESTFRUIT"})), ("a", "BIO")
        )
        self.assertEqual(
            one_word_apart(frozenset({"SIROP", "TESTFRUIT"}), frozenset({"SIROP", "TESTFRUIT", "BIO"})), ("b", "BIO")
        )
        self.assertIsNone(one_word_apart(frozenset({"SIROP", "TESTFRUIT"}), frozenset({"SIROP", "TESTFRUIT"})))
        self.assertIsNone(
            one_word_apart(frozenset({"SIROP", "TESTFRUIT", "BIO", "GLACE"}), frozenset({"SIROP", "TESTFRUIT"}))
        )
        self.assertIsNone(one_word_apart(frozenset({"SIROP", "BIO"}), frozenset({"SIROP", "TESTFRUIT"})))
        self.assertIsNone(one_word_apart(frozenset({"SIROP"}), frozenset()))

    def test_fold_name_drops_accents_and_case(self):
        self.assertEqual(fold_name("Crème de testfruit"), "CREME DE TESTFRUIT")

    def test_least_confident(self):
        self.assertEqual(least_confident("high", "medium"), "medium")
        self.assertEqual(least_confident("low", "high"), "low")
        self.assertEqual(least_confident("high"), "high")
        self.assertEqual(least_confident(), "low")
        # An unknown level is "low", never itself: nothing unmeasured is
        # approved in bulk, and the panel has a word for "low".
        self.assertEqual(least_confident("high", "bizarre"), "low")
        self.assertEqual(least_confident("bizarre"), "low")


class NameShapeTests(SimpleTestCase):
    def test_what_describes_how_it_was_bought_leaves_the_words(self):
        shape = NameShape.of("MPRO 50 TIMBALE PAPIER SANS COUVERCLE 25CL")
        self.assertEqual(shape.words, frozenset({"TIMBALE", "PAPIER", "SANS", "COUVERCLE"}))
        self.assertEqual(shape.sizes, (((Decimal("0.25"), "L"), 1),))
        self.assertEqual(dict(shape.numbers), {Decimal("50"): 1, Decimal("25"): 1})

    def test_function_words_leave_but_sans_stays(self):
        self.assertEqual(
            NameShape.of("LIMONADE TESTBRAND SANS BULLES 40CL").words,
            frozenset({"LIMONADE", "TESTBRAND", "SANS", "BULLES"}),
        )
        self.assertEqual(NameShape.of("JUS DE LA POMME").words, frozenset({"JUS", "POMME"}))

    def test_accents_junk_and_bare_numbers(self):
        self.assertEqual(NameShape.of("*Crème de testfruit 1L").words, frozenset({"CREME", "TESTFRUIT"}))
        self.assertEqual(NameShape.of("1899 33CL").words, frozenset())
        self.assertEqual(NameShape.of("").words, frozenset())


class FindTests(SimpleTestCase):
    """`ClassifiedNeighbours.find`, one shape of confusion at a time."""

    def test_the_same_words_at_the_same_size_win_over_another_size_of_the_same_cup(self):
        index = ClassifiedNeighbours(
            [
                classified("50 TIMBALE PAPIER BLC 25CL", "Timbales papier", stock_equivalent="50"),
                classified("50 TIMBALE PAPIER BLC 50CL", "Timbales papier pinte", stock_equivalent="50"),
            ]
        )
        match = index.find(Pending("100 TIMBALE PAPIER BLC 25CL"))
        self.assertEqual(match.neighbour.stock_type_name, "Timbales papier")
        self.assertTrue(match.same_sizes)
        self.assertFalse(match.same_numbers)  # another pack: the factor is not copied
        # Another pack of an article counted by the unit: the only wrong
        # articles of the same-words-same-sizes tier were of this shape, so
        # the article's own guard says medium rather than leaning on the
        # factor's.
        self.assertEqual(match.article_confidence, "medium")
        # The same pack is sure.
        match = index.find(Pending("TIMBALE PAPIER BLC 25CL X50"))
        self.assertEqual(match.neighbour.stock_type_name, "Timbales papier")
        self.assertTrue(match.same_numbers)
        self.assertEqual(match.article_confidence, "high")

    def test_another_size_of_a_unit_counted_article_is_low(self):
        """The cup case: the owner files a 25cl cup and a 50cl cup apart, so
        the same words at another size name the wrong article two times in
        three. Offered - the family is right - but low."""
        index = ClassifiedNeighbours([classified("50 TIMBALE PAPIER BLC 50CL", "Timbales papier pinte")])
        match = index.find(Pending("50 TIMBALE PAPIER BLC 25CL"))
        self.assertEqual(match.neighbour.stock_type_name, "Timbales papier pinte")
        self.assertFalse(match.same_sizes)
        self.assertEqual(match.article_confidence, "low")

    def test_two_sizes_as_good_as_each_other_is_no_answer(self):
        index = ClassifiedNeighbours(
            [
                classified("50 TIMBALE PAPIER BLC 25CL", "Timbales papier"),
                classified("50 TIMBALE PAPIER BLC 50CL", "Timbales papier pinte"),
            ]
        )
        self.assertIsNone(index.find(Pending("50 TIMBALE PAPIER BLC 40CL")))

    def test_an_article_tracked_by_volume_absorbs_the_size(self):
        index = ClassifiedNeighbours([classified("VODKA TESTBRAND 70CL", "Vodka premium", unit=UnitChoices.LITRE)])
        match = index.find(Pending("VODKA TESTBRAND 1L"))
        self.assertEqual(match.neighbour.stock_type_name, "Vodka premium")
        self.assertFalse(match.same_sizes)
        self.assertEqual(match.article_confidence, "high")

    def test_a_flavour_is_another_product(self):
        index = ClassifiedNeighbours([classified("BOISSON FRUITEE FRAISE 75CL PET", "Nectar fraise")])
        self.assertIsNone(index.find(Pending("BOISSON FRUITEE PASSION 75CL PET")))

    def test_a_word_apart_is_a_medium_match_that_names_the_word(self):
        index = ClassifiedNeighbours([classified("12 LOUCHE INOX TABLE", "Louches")])
        match = index.find(Pending("LOUCHE INOX"))
        self.assertEqual(match.extra_word, ("neighbour", "TABLE"))
        self.assertFalse(match.one_word_name)
        self.assertEqual(match.article_confidence, "medium")
        match = index.find(Pending("LOUCHE INOX TABLE GRAVEE"))
        self.assertEqual(match.extra_word, ("product", "GRAVEE"))

    def test_a_one_word_name_against_a_two_word_one_is_kept_but_low(self):
        """« LOUCHE » against « LOUCHE INOX »: the one word is the whole of
        what the two have in common, and the loose tier is right less often
        there than elsewhere - kept, named, low."""
        index = ClassifiedNeighbours([classified("LOUCHE INOX", "Louches", stock_equivalent="12")])
        match = index.find(Pending("LOUCHE"))
        self.assertEqual(match.extra_word, ("neighbour", "INOX"))
        self.assertTrue(match.one_word_name)
        self.assertEqual(match.article_confidence, "low")
        match = index.find(Pending("LOUCHE INOX GRAVEE"))
        self.assertFalse(match.one_word_name)
        self.assertEqual(match.article_confidence, "medium")

    def test_a_word_apart_at_the_same_pack_from_the_same_supplier_is_still_medium(self):
        """Measured strictly (the judged product out of the index it would
        have taught), this case is right about nine times in ten, not the
        every-time the lenient benchmark read: the first zero-sugar twin of a
        cola is offered the cola, the deposit its goods. Medium whatever the
        supplier and the pack - the witness ranks higher, it is not made
        sure by it."""
        index = ClassifiedNeighbours(
            [classified("COLA TESTBRAND 33CL X24 VC", "Cola testbrand", stock_equivalent="24")]
        )
        match = index.find(Pending("COLA TESTBRAND ZERO 33CL X24 VC"))
        self.assertEqual(match.extra_word, ("product", "ZERO"))
        self.assertTrue(match.same_numbers)
        self.assertTrue(match.same_supplier)
        self.assertTrue(match.same_sizes)
        self.assertEqual(match.article_confidence, "medium")
        self.assertEqual(least_confident(match.article_confidence, "high"), "medium")

    def test_a_short_name_inside_a_long_one_is_not_it(self):
        index = ClassifiedNeighbours([classified("12 LOUCHE INOX TABLE", "Louches")])
        self.assertIsNone(index.find(Pending("LOUCHE")))

    def test_a_word_that_already_tells_two_articles_apart_is_never_a_word_apart(self):
        deposit = classified("Consigne FUT ALTBRAU 30L", "Consigne fût testbrau")
        # Alone, the loose tier would offer the deposit's article for the beer...
        self.assertEqual(
            ClassifiedNeighbours([deposit]).find(Pending("FUT ALTBRAU 30L")).extra_word, ("neighbour", "CONSIGNE")
        )
        # ...until the owner's own classifications say « CONSIGNE » is the
        # difference between two articles.
        index = ClassifiedNeighbours(
            [
                deposit,
                classified("Consigne FUT TESTBRAU 30L", "Consigne fût testbrau"),
                classified("FUT TESTBRAU 30L", "Bière testbrau fût", unit=UnitChoices.LITRE),
            ]
        )
        self.assertTrue(index.tells_apart("CONSIGNE"))
        self.assertFalse(index.tells_apart("FUT"))
        self.assertIsNone(index.find(Pending("FUT ALTBRAU 30L")))

    def test_a_difference_is_learned_in_every_spelling_of_the_word(self):
        """A deposit printed « CONS. » beside its keg teaches « CONSIGNE »
        too: the next deposit printed in full is not offered the beer."""
        beers = [
            classified("FUT TESTBRAU 30L", "Bière fût", unit=UnitChoices.LITRE),
            classified("FUT ALTBRAU 30L", "Bière fût", unit=UnitChoices.LITRE),
        ]
        # Nothing learned yet: the loose tier offers the beer for a deposit.
        match = ClassifiedNeighbours(beers).find(Pending("CONSIGNE FUT ALTBRAU 30L"))
        self.assertEqual(match.extra_word, ("product", "CONSIGNE"))
        index = ClassifiedNeighbours([*beers, classified("CONS. FUT TESTBRAU 30L", "Consigne fût")])
        self.assertTrue(index.tells_apart("CONS"))
        self.assertTrue(index.tells_apart("CONSIGNE"))
        self.assertFalse(index.tells_apart("FUT"))
        self.assertIsNone(index.find(Pending("CONSIGNE FUT ALTBRAU 30L")))

    def test_the_same_words_under_one_article_teach_nothing(self):
        index = ClassifiedNeighbours([classified("COLA 33CL", "Colas"), classified("COLA ZERO 33CL", "Colas")])
        self.assertFalse(index.tells_apart("ZERO"))
        index = ClassifiedNeighbours([classified("COLA 33CL", "Cola"), classified("COLA ZERO 33CL", "Cola zero")])
        self.assertTrue(index.tells_apart("ZERO"))
        self.assertFalse(index.tells_apart("COLA"))

    def test_a_word_apart_at_another_size_of_a_unit_article_is_no_witness(self):
        index = ClassifiedNeighbours([classified("TIMBALE PAPIER BLC 25CL", "Timbales papier")])
        self.assertIsNone(index.find(Pending("TIMBALE PAPIER BLC KRAFT 50CL")))
        index = ClassifiedNeighbours([classified("SIROP TESTFRUIT 70CL", "Sirop testfruit", unit=UnitChoices.LITRE)])
        self.assertEqual(index.find(Pending("SIROP TESTFRUIT BIO 1L")).article_confidence, "medium")

    def test_a_house_code_one_character_off_is_another_code(self):
        index = ClassifiedNeighbours([classified("REF7001", "Objets test")])
        self.assertIsNone(index.find(Pending("1REF7001")))
        self.assertIsNone(index.find(Pending("REF7002")))

    def test_abbreviations_and_a_typo_still_find_the_article_but_never_as_sure(self):
        """« TIMB PAP » is « TIMBALE PAPIER » to the match; to a sure
        suggestion it is a guess about two shortened words. A plural is the
        same word."""
        index = ClassifiedNeighbours([classified("TIMBALE PAPIER BLC 25CL", "Timbales papier", stock_equivalent="1")])
        match = index.find(Pending("TIMB PAP BLC 25CL"))
        self.assertEqual(match.neighbour.stock_type_name, "Timbales papier")
        self.assertFalse(match.exact_words)
        self.assertFalse(match.words_sure)
        self.assertEqual(match.article_confidence, "medium")
        match = index.find(Pending("TIMBALES PAPIER BLC 25CL"))
        self.assertFalse(match.exact_words)
        self.assertTrue(match.words_sure)
        self.assertEqual(match.article_confidence, "high")
        index = ClassifiedNeighbours(
            [classified("PROSECCO TESTCANTINA 75CL", "Prosecco testcantina", unit=UnitChoices.LITRE)]
        )
        match = index.find(Pending("PROSECO TESTCANTINA 75CL"))
        self.assertIsNotNone(match)
        self.assertFalse(match.words_sure)
        self.assertEqual(match.article_confidence, "medium")

    def test_a_word_that_is_the_prefix_of_another_word_is_matched_but_never_sure(self):
        """While the owner's names print only the long word, the abbreviation
        rule cannot tell « VIN » for « VINAIGRE » from « TIMB » for
        « TIMBALE »: the match stands, at medium, for a person to refuse."""
        index = ClassifiedNeighbours([classified("VINAIGRE TESTCEP 1L", "Vinaigre de test", unit=UnitChoices.LITRE)])
        match = index.find(Pending("VIN TESTCEP 1L"))
        self.assertIsNotNone(match)
        self.assertTrue(match.same_words)
        self.assertFalse(match.words_sure)
        self.assertEqual(match.article_confidence, "medium")
        index = ClassifiedNeighbours([classified("SACHET PAPIER KRAFT", "Sachets kraft")])
        self.assertEqual(index.find(Pending("SAC PAPIER KRAFT")).article_confidence, "medium")

    def test_two_words_the_owner_prints_in_full_under_different_articles_are_two_words(self):
        """Learned, like `tells_apart`: once the classified names print BOTH
        « VIN » and « VINAIGRE », each under articles the other never appears
        under, the prefix is no abbreviation - « VIN TESTCEP » is not offered
        the vinegar, and « SAC » is not « SACHET »."""
        index = ClassifiedNeighbours(
            [
                classified("VINAIGRE TESTCEP 1L", "Vinaigre de test", unit=UnitChoices.LITRE),
                classified("VIN TESTCUVEE 75CL", "Rouge de table", unit=UnitChoices.LITRE),
            ]
        )
        self.assertTrue(index.two_words("VIN", "VINAIGRE"))
        self.assertFalse(index.alike("VIN", "VINAIGRE"))
        self.assertIsNone(index.find(Pending("VIN TESTCEP 1L")))
        # The long word alone still finds its own kind.
        self.assertEqual(index.find(Pending("VINAIGRE TESTCEP 1L")).neighbour.stock_type_name, "Vinaigre de test")
        index = ClassifiedNeighbours(
            [classified("SACHET PAPIER KRAFT", "Sachets kraft"), classified("SAC TESTBAG 50L", "Sacs testbag")]
        )
        self.assertIsNone(index.find(Pending("SAC PAPIER KRAFT")))
        # Nor is such a pair a « word apart » of anything.
        self.assertIsNone(index.find(Pending("SAC PAPIER")))

    def test_an_abbreviation_the_owner_never_spells_out_stays_one(self):
        """« GOB » only ever appears as « GOB » in the classified names, or
        beside « GOBELET » under the same article: nothing refutes it, so it
        goes on standing for the long word - at medium, as any abbreviation."""
        index = ClassifiedNeighbours([classified("GOBELET CARTON BLC 25CL", "Gobelets testcarton")])
        self.assertFalse(index.two_words("GOB", "GOBELET"))
        self.assertTrue(index.alike("GOB", "GOBELET"))
        match = index.find(Pending("50 GOB CARTON BLC 25CL"))
        self.assertEqual(match.neighbour.stock_type_name, "Gobelets testcarton")
        self.assertEqual(match.article_confidence, "medium")
        # Both spellings printed under ONE article vouch for the pair.
        index = ClassifiedNeighbours(
            [
                classified("GOBELET CARTON BLC 25CL", "Gobelets testcarton"),
                classified("50 GOB CARTON BLC 25CL", "Gobelets testcarton"),
                classified("GOB PLASTIQUE 20CL", "Gobelets plastique"),
            ]
        )
        self.assertFalse(index.two_words("GOB", "GOBELET"))
        self.assertIsNotNone(index.find(Pending("GOB CARTON BLC 25CL X100")))
        # A plural is never two words, whatever the articles print.
        index = ClassifiedNeighbours([classified("SAC KRAFT", "Sacs kraft"), classified("SACS PAPIER", "Sacs papier")])
        self.assertTrue(index.alike("SAC", "SACS"))
        self.assertFalse(index.two_words("SAC", "SACS"))

    def test_a_difference_is_not_learned_from_a_word_the_vocabulary_refutes(self):
        """`tells_apart` looks the word up in every spelling `alike` accepts,
        so a refuted prefix pair teaches nothing: « VINAIGRE » beside a name
        without it says nothing about « VIN » once both are the owner's
        words."""
        index = ClassifiedNeighbours(
            [
                classified("VINAIGRE TESTCEP 1L", "Vinaigre de test", unit=UnitChoices.LITRE),
                classified("TESTCEP 1L", "Testcep en litre", unit=UnitChoices.LITRE),
                classified("VIN TESTCUVEE 75CL", "Rouge de table", unit=UnitChoices.LITRE),
            ]
        )
        self.assertTrue(index.tells_apart("VINAIGRE"))
        self.assertFalse(index.tells_apart("VIN"))

    def test_the_sure_spelling_ranks_above_the_abbreviated_one(self):
        index = ClassifiedNeighbours(
            [
                classified("TIMB PAP BLC 25CL", "Timbales papier abrégées"),
                classified("TIMBALES PAPIER BLC 25CL", "Timbales papier"),
            ]
        )
        match = index.find(Pending("TIMBALE PAPIER BLC 25CL"))
        self.assertEqual(match.neighbour.stock_type_name, "Timbales papier")
        self.assertTrue(match.words_sure)

    def test_the_same_supplier_is_the_better_witness_and_two_strangers_are_none(self):
        index = ClassifiedNeighbours(
            [
                classified("AGRUME TESTVERT", "Agrumes testverts", unit=UnitChoices.KILOGRAM, supplier_id=1),
                classified("AGRUME TESTVERT", "Limes cocktail", unit=UnitChoices.KILOGRAM, supplier_id=2),
            ]
        )
        self.assertEqual(
            index.find(Pending("AGRUME TESTVERT", supplier_id=2)).neighbour.stock_type_name, "Limes cocktail"
        )
        self.assertIsNone(index.find(Pending("AGRUME TESTVERT", supplier_id=3)))

    def test_a_name_with_nothing_to_say_matches_nothing(self):
        index = ClassifiedNeighbours([classified("1899", "Bière testbrau"), classified("SAC", "Sacs")])
        self.assertEqual(len(index.neighbours), 1)
        self.assertIsNone(index.find(Pending("")))
        self.assertIsNone(index.find(Pending("1899 33CL")))

    def test_a_product_never_matches_itself(self):
        index = ClassifiedNeighbours([classified("SAC PAPIER", "Sacs", product_id=7)])
        self.assertIsNone(index.find(Pending("SAC PAPIER", pk=7)))
        self.assertIsNotNone(index.find(Pending("SAC PAPIER", pk=8)))
