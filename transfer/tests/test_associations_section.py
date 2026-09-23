"""« Associations produits → articles » (§7.3, §10.2).

What an owner loses if this goes wrong is the review work: hundreds of
products each put under an article with a conversion, by hand. And what they
get if it goes *quietly* wrong is worse - stock booked with a factor of 1
nobody typed, a rent counted as bottles. So every record the import cannot
apply is skipped with its reason, and everything derived (movements,
invoice statuses) is rebuilt by the app's own arithmetic and compared.

Fixtures are invented (the repository is public).
"""

from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from inventory.models import (
    MovementKind,
    Product,
    StockMovement,
    StockTakeLine,
    StockTakeLineSource,
    StockType,
    UnitChoices,
)
from inventory.services import create_stock_movement_for_line, unlink_product
from invoices.importing import redo_as_expenses
from invoices.models import Invoice
from invoices.parsers.base import ParsedInvoice, ParsedLine
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from transfer import registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections.associations import AssociationsSection
from transfer.sections.base import Strategy
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    media_listing,
    round_trip,
)

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
KEY = "associations"


def section():
    return registry.get(KEY)


def build_fixture(test):
    """Two suppliers, four articles, classified products on real lines (one
    measured in litres, one counted with a factor, one refund), one product
    still « à classer », and a charge's poste."""
    test.metro = make_supplier(code="METRO", name="Metro", parser_key="METRO")
    test.shop = make_supplier(code="EPI_TEST", name="Épicerie Test")
    test.charges = make_supplier(code="LOYER_TEST", name="Loyer Test", expenses_only=True)
    test.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
    test.limes = make_stock_type(name="Citrons verts", unit=UnitChoices.UNIT, category="Épicerie", loss_percent=Decimal("0"))
    test.syrup = make_stock_type(name="Sirop de sucre", unit=UnitChoices.LITRE)  # no product yet
    test.beef = make_stock_type(name="Bœuf", unit=UnitChoices.KILOGRAM, category="")
    test.bottle = make_product(test.metro, "VODKA X 70CL", test.vodka, unit=UnitChoices.LITRE, ean="3000000000017")
    test.net = make_product(test.shop, "CITRON VERT FILET", test.limes, unit=UnitChoices.UNIT, stock_equivalent="12")
    test.meat = make_product(test.shop, "BAVETTE", test.beef, unit=UnitChoices.KILOGRAM)
    test.pending = make_product(test.shop, "ARTICLE INCONNU")
    test.rent = make_product(test.charges, "Loyer Test", is_expense=True)
    test.metro_invoice = make_invoice(test.metro, invoice_number="MET-001", status=Invoice.Status.COMPLETE)
    test.shop_invoice = make_invoice(test.shop, invoice_number="EPI-001", status=Invoice.Status.NEEDS_REVIEW)
    test.lines = [
        make_invoice_line(test.metro_invoice, test.bottle, quantity=Decimal("6"), total_volume="4.200", total_ht="57.00"),
        make_invoice_line(test.metro_invoice, test.bottle, quantity=Decimal("-1"), total_volume="-0.700", total_ht="-9.50"),
        make_invoice_line(test.shop_invoice, test.net, quantity=Decimal("2"), total_ht="6.00"),
        make_invoice_line(test.shop_invoice, test.meat, quantity=Decimal("0.350"), total_ht="7.35", unit_cost_ht="21"),
        make_invoice_line(test.shop_invoice, test.pending, quantity=Decimal("1"), total_ht="3.00"),
    ]
    make_invoice_line(make_invoice(test.charges, invoice_number="LOY-001"), test.rent, total_ht="800.00")
    for line in test.lines:
        create_stock_movement_for_line(line)


def own_round_trip(test, strategy):
    """§10.2's round trip on this section alone: the suppliers and the
    invoices stay, the classifications and articles go and come back."""
    before = section().snapshot()
    reader = export_archive({KEY}, closed=False)
    try:
        with test.captureOnCommitCallbacks(execute=True):
            run_clear({KEY}, preview=False, closed=False)
        test.assertEqual(section().count(), {"articles": 0, "produits classés": 0})
        test.assertFalse(StockMovement.objects.exists())
        report = import_archive(reader, {KEY: strategy})
    finally:
        reader.close()
    return before, section().snapshot(), report


def payload(products=(), articles=(), supplier_names=None):
    return {"supplier_names": supplier_names or {}, "articles": list(articles), "products": list(products)}


def import_payload(data, strategy=MERGE, *, preview=False):
    with ArchiveReader(forge({KEY: data})) as reader:
        return import_archive(reader, {KEY: strategy}, preview=preview).section(KEY)


class RoundTripTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_merge_brings_everything_back(self):
        before, after, report = own_round_trip(self, MERGE)

        self.assertEqual(after, before)
        self.assertEqual(len(before["movements"]), 4)  # the refund included, the pending line not
        tally = report.section(KEY).tallies
        self.assertEqual(tally["articles"].created, 4)
        self.assertEqual(tally["produits classés"].updated, 3)  # classified again: filling a blank
        self.assertEqual(report.rebuilt["mouvements de stock"], 4)

    def test_replace_brings_everything_back(self):
        before, after, _report = own_round_trip(self, REPLACE)

        self.assertEqual(after, before)

    def test_documents_without_number_or_date_round_trip(self):
        """Their movements dump None beside a dated invoice's date: the
        snapshot orders them without comparing the two."""
        for invoice_date in (None, self.metro_invoice.invoice_date):
            ticket = make_invoice(self.metro, invoice_number="", invoice_date=None)
            Invoice.objects.filter(pk=ticket.pk).update(invoice_date=invoice_date)
            create_stock_movement_for_line(
                make_invoice_line(ticket, self.bottle, quantity=Decimal("1"), total_volume="0.700", total_ht="9.50")
            )

        before, after, _report = own_round_trip(self, MERGE)

        self.assertEqual(after, before)
        self.assertEqual(len(before["movements"]), 6)

    def test_the_invoice_statuses_come_back(self):
        statuses = dict(Invoice.objects.values_list("invoice_number", "status"))

        own_round_trip(self, MERGE)

        self.assertEqual(dict(Invoice.objects.values_list("invoice_number", "status")), statuses)

    def test_an_article_counted_in_the_products_margin_comes_back_counted(self):
        """Dropped by the round trip, the flag comes back unticked - and the
        paper towels silently leave the products margin's cost, which makes
        the margin look BETTER. Exactly the shape of quietly wrong money this
        archive exists not to produce."""
        StockType.objects.filter(pk=self.syrup.pk).update(count_in_products_margin=True)

        own_round_trip(self, MERGE)

        flags = dict(StockType.objects.values_list("name", "count_in_products_margin"))
        self.assertTrue(flags[self.syrup.name])
        self.assertEqual(sorted(name for name, on in flags.items() if on), [self.syrup.name])

    def test_an_archive_written_before_the_flag_existed_says_nothing_about_it(self):
        """« Not said » is never a conflict and never a change: an older
        archive leaves each article's flag exactly as this database has it."""
        StockType.objects.filter(pk=self.syrup.pk).update(count_in_products_margin=True)

        result = import_payload(
            payload(articles=[{"name": self.syrup.name, "unit": self.syrup.unit}]), REPLACE
        )

        self.syrup.refresh_from_db()
        self.assertTrue(self.syrup.count_in_products_margin)
        self.assertEqual(result.tallies["articles"].unchanged, 1)

    def test_the_articles_keep_their_creation_date(self):
        """auto_now_add writes "now" on insert, bulk_create included."""
        created = dict(StockType.objects.values_list("name", "created_at"))
        StockType.objects.filter(name="Vodka").update(created_at="2024-03-01T09:30:00+00:00")
        created["Vodka"] = StockType.objects.get(name="Vodka").created_at

        own_round_trip(self, MERGE)

        self.assertEqual(dict(StockType.objects.values_list("name", "created_at")), created)

    def test_the_whole_closure_round_trips(self):
        """§10.2 as written, through the suppliers section - once lane A's
        sections are installed."""
        if not registry.is_registered("fournisseurs"):
            self.skipTest("la partie « fournisseurs » n'est pas encore installée")
        for strategy in (MERGE, REPLACE):
            before, after = round_trip({KEY}, strategy)
            self.assertEqual(after[KEY], before[KEY])


class IdempotenceTests(TestCase):
    """Importing a fresh export of the same database changes nothing, and
    says so for every record: this is what catches a Decimal's places or a
    '' against a None."""

    def setUp(self):
        build_fixture(self)

    def check(self, strategy):
        fingerprint = db_fingerprint()
        with export_archive({KEY}, closed=False) as reader:
            report = import_archive(reader, {KEY: strategy})
        mine = report.section(KEY)
        self.assertEqual(mine.conflicts, [])
        self.assertEqual(mine.skipped, [])
        self.assertEqual(mine.tallies["articles"].unchanged, 4)
        self.assertEqual(mine.tallies["produits classés"].unchanged, 3)
        for tally in mine.tallies.values():
            self.assertEqual((tally.created, tally.updated, tally.deleted), (0, 0, 0))
        self.assertEqual(db_fingerprint(), fingerprint)

    def test_merge(self):
        self.check(MERGE)

    def test_replace(self):
        self.check(REPLACE)

    def test_two_products_differing_only_by_case_stay_two(self):
        """The database's constraint is case-sensitive: both exist, each is
        its own record in the file, and neither is « en double »."""
        make_product(self.metro, "Vodka X 70cl", self.vodka, unit=UnitChoices.LITRE, stock_equivalent="2")
        fingerprint = db_fingerprint()
        with export_archive({KEY}, closed=False) as reader:
            mine = import_archive(reader, {KEY: MERGE}).section(KEY)
        self.assertEqual((mine.skipped, mine.conflicts), ([], []))
        self.assertEqual(mine.tallies["produits classés"].unchanged, 4)
        self.assertEqual(db_fingerprint(), fingerprint)


KEG, TWIN = "BIÈRE DU PONT FÛT 20L", "BIÈRE DU PONT FûT 20L"  # the accented capital only


class TwinProductsTests(TestCase):
    """Two products of one supplier whose names differ only by an accented
    capital. SQLite's case-blind comparison is ASCII only, so the app's own
    matcher makes such a pair (a review made one on a scratch copy; the till
    already holds three pairs of that shape), and each is a product of its
    own - its classification, its purchases, its counts.

    Cleared and imported again, the first one created caught the second by
    its folded name: the second's classification was skipped « en double
    dans l'archive » and its invoice line moved onto the first, its purchase
    booked in the other's article at the other's conversion."""

    def setUp(self):
        self.brewery = make_supplier(code="BRASSERIE_TEST", name="Brasserie Test")
        draught = make_stock_type(name="Bière pression", unit=UnitChoices.LITRE)
        kegs = make_stock_type(name="Fûts consignés", unit=UnitChoices.UNIT)
        keg = make_product(self.brewery, KEG, draught, unit=UnitChoices.LITRE)
        twin = make_product(self.brewery, TWIN, kegs, unit=UnitChoices.UNIT, stock_equivalent="2")
        invoice = make_invoice(self.brewery, invoice_number="BRA-001", status=Invoice.Status.COMPLETE)
        for line in (
            make_invoice_line(invoice, keg, quantity=Decimal("2"), total_volume="40.000", total_ht="160.00"),
            make_invoice_line(invoice, twin, quantity=Decimal("3"), total_ht="90.00"),
        ):
            create_stock_movement_for_line(line)
        take = make_stock_take()
        make_stock_take_line(take, keg, counted_quantity="10", unit=UnitChoices.LITRE, value_ht="40.00")
        make_stock_take_line(take, twin, counted_quantity="2", unit=UnitChoices.UNIT, value_ht="60.00")

    def state(self):
        """Each of the brewery's products: its article and conversion, the
        purchases booked from its own lines, and its counts."""
        return sorted(
            (
                product.raw_name,
                product.stock_type.name if product.stock_type else None,
                str(product.stock_equivalent),
                sorted(
                    (str(movement.quantity), str(movement.unit_cost_ht), movement.stock_type.name)
                    for movement in StockMovement.objects.filter(invoice_line__product=product)
                ),
                StockTakeLine.objects.filter(product=product).count(),
            )
            for product in Product.objects.filter(supplier=self.brewery)
        )

    def test_they_stay_two_through_a_clear_and_an_import(self):
        state = self.state()
        self.assertEqual(
            state,
            [
                (KEG, "Bière pression", "1.0000", [("40.000", "4.0000", "Bière pression")], 1),
                (TWIN, "Fûts consignés", "2.0000", [("6.000", "15.0000", "Fûts consignés")], 1),
            ],
        )
        for strategy in (REPLACE, MERGE):
            with self.subTest(strategy=strategy):
                before, after = round_trip({KEY, "factures", "inventaires"}, strategy)

                self.assertEqual(self.state(), state)
                self.assertEqual(after, before)

    def test_its_own_export_merges_as_unchanged(self):
        fingerprint = db_fingerprint()
        with export_archive({"fournisseurs", KEY, "factures", "inventaires"}) as reader:
            mine = import_archive(reader, MERGE).section(KEY)

        self.assertEqual((mine.skipped, mine.conflicts), ([], []))
        self.assertEqual(mine.tallies["produits classés"].unchanged, 2)
        self.assertEqual(db_fingerprint(), fingerprint)


class ChargesSupplierTests(TestCase):
    """A stock item's product stays one when its supplier turns to charges
    (importing.redo_as_expenses: « it is stock after all »), and
    assign_product refuses only a product that is itself a poste
    (`is_expense`). The section refused every product of such a supplier, so
    a round trip lost its classification and its purchases - its invoices
    went back to « À vérifier » - and its own export never merged as
    « inchangé »."""

    def setUp(self):
        self.cellar = make_supplier(code="CAVE_TEST", name="Cave Test")
        champagne = make_stock_type(name="Champagne Test", unit=UnitChoices.LITRE)
        self.bottle = make_product(self.cellar, "CHAMPAGNE BRUT 75CL", champagne, unit=UnitChoices.LITRE)
        invoice = make_invoice(self.cellar, invoice_number="CAV-001", status=Invoice.Status.COMPLETE)
        line = make_invoice_line(invoice, self.bottle, quantity=Decimal("6"), total_volume="4.500", total_ht="120.00")
        create_stock_movement_for_line(line)
        # A count priced from that line: what keeps the document as it was
        # when its supplier turns to charges.
        count = make_stock_take_line(make_stock_take(), self.bottle, counted_quantity="1.5", unit=UnitChoices.LITRE,
                                     value_ht="40.00")
        StockTakeLineSource.objects.create(stock_take_line=count, invoice_line=line, quantity_used=Decimal("1.5"),
                                           unit_cost_ht=Decimal("26.6667"))
        with transaction.atomic():
            self.cellar.expenses_only = True
            self.cellar.save(update_fields=["expenses_only"])
            redo_as_expenses(self.cellar)
        self.bottle.refresh_from_db()
        # What redo_as_expenses leaves: still classified, not a poste, its purchase booked.
        self.assertEqual((self.bottle.stock_type, self.bottle.is_expense), (champagne, False))
        self.assertTrue(StockMovement.objects.filter(invoice_line=line).exists())

    def test_its_own_export_merges_as_unchanged(self):
        fingerprint = db_fingerprint()
        with export_archive({"fournisseurs", KEY, "factures", "inventaires"}) as reader:
            mine = import_archive(reader, MERGE).section(KEY)

        self.assertEqual((mine.skipped, mine.conflicts), ([], []))
        self.assertEqual(mine.tallies["produits classés"].unchanged, 1)
        self.assertEqual(db_fingerprint(), fingerprint)

    def test_a_clear_and_an_import_keep_its_classification_and_its_purchases(self):
        for strategy in (REPLACE, MERGE):
            with self.subTest(strategy=strategy):
                before, after = round_trip({KEY, "factures", "inventaires"}, strategy)

                self.assertEqual(after, before)
                product = Product.objects.get(supplier=self.cellar, raw_name="CHAMPAGNE BRUT 75CL")
                self.assertEqual((product.stock_type.name, product.is_expense), ("Champagne Test", False))
                self.assertEqual(StockMovement.objects.filter(invoice_line__product=product).count(), 1)
                self.assertEqual(Invoice.objects.get(invoice_number="CAV-001").status, Invoice.Status.COMPLETE)


class MergeAndReplaceTests(TestCase):
    """One record changed, one only here, one only in the archive."""

    def setUp(self):
        build_fixture(self)
        self.recipe = make_recipe(name="Mojito")
        self.rum = make_stock_type(name="Rhum ambré", unit=UnitChoices.LITRE)
        make_ingredient(self.recipe, stock_type=self.rum, quantity="0.04")
        self.reader = export_archive({KEY}, closed=False)
        self.addCleanup(self.reader.close)
        # Then this database moves on from the archive:
        self.vodka.loss_percent = Decimal("5")          # changed here
        self.vodka.category = ""                        # a blank the archive can fill
        self.vodka.save()
        self.bottle.stock_equivalent = Decimal("1.5")   # classification changed here
        self.bottle.save()
        self.gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE)  # only here
        self.gin_bottle = make_product(self.metro, "GIN Y 70CL", self.gin, unit=UnitChoices.LITRE)
        gin_line = make_invoice_line(self.metro_invoice, self.gin_bottle, quantity=Decimal("1"), total_volume="0.700",
                                     total_ht="15.00")
        create_stock_movement_for_line(gin_line)
        self.syrup.delete()                             # only in the archive

    def test_merge_keeps_what_differs_and_adds_what_is_missing(self):
        mine = import_archive(self.reader, {KEY: MERGE}).section(KEY)

        self.vodka.refresh_from_db()
        self.bottle.refresh_from_db()
        self.assertEqual(self.vodka.loss_percent, Decimal("5.00"))       # kept
        self.assertEqual(self.vodka.category, "Spiritueux")              # a blank filled
        self.assertEqual(self.bottle.stock_equivalent, Decimal("1.5000"))  # kept
        self.assertTrue(StockType.objects.filter(name="Gin").exists())   # untouched
        self.assertEqual(Product.objects.get(pk=self.gin_bottle.pk).stock_type, self.gin)
        self.assertTrue(StockType.objects.filter(name="Sirop de sucre").exists())  # created
        self.assertEqual(
            mine.conflicts,
            [
                "Article « Vodka » : différent dans l'archive (pertes) — gardé tel quel",
                "Produit « VODKA X 70CL » (Metro) : différent dans l'archive (conversion) — gardé tel quel",
            ],
        )
        self.assertEqual(mine.tallies["articles"].created, 1)
        self.assertEqual(mine.tallies["articles"].updated, 1)  # the category filled
        self.assertEqual(mine.tallies["articles"].deleted, 0)

    def test_replace_makes_the_section_the_archive(self):
        mine = import_archive(self.reader, {KEY: REPLACE}).section(KEY)

        self.vodka.refresh_from_db()
        self.bottle.refresh_from_db()
        self.assertEqual((self.vodka.loss_percent, self.vodka.category), (Decimal("10.00"), "Spiritueux"))
        self.assertEqual(self.bottle.stock_equivalent, Decimal("1.0000"))
        self.assertFalse(StockType.objects.filter(name="Gin").exists())
        self.gin_bottle.refresh_from_db()
        self.assertIsNone(self.gin_bottle.stock_type)  # unlinked, not deleted: it has an invoice line
        self.assertTrue(StockType.objects.filter(name="Sirop de sucre").exists())
        self.assertEqual(mine.conflicts, [])
        self.assertEqual(mine.tallies["articles"].updated, 1)
        self.assertEqual(mine.tallies["articles"].created, 1)
        self.assertEqual(mine.tallies["articles"].deleted, 1)
        # One classification changed, one removed from a product that stays
        # (its invoice line holds it): both are products modified, none goes.
        self.assertEqual(mine.tallies["produits classés"].updated, 2)
        self.assertEqual(mine.tallies["produits classés"].deleted, 0)

    def test_replace_rebooks_the_changed_conversion(self):
        import_archive(self.reader, {KEY: REPLACE})

        movement = StockMovement.objects.get(invoice_line=self.lines[0])
        self.assertEqual(movement.quantity, Decimal("4.200"))  # 4.2 L at a factor of 1 again
        self.assertFalse(StockMovement.objects.filter(invoice_line__product=self.gin_bottle).exists())

    def test_an_article_held_by_a_recipe_is_kept_on_replace_and_said(self):
        # "Rhum ambré" existed at export: take it out of the file.
        with ArchiveReader(forge(self.reader, associations=lambda data: {
            **data, "articles": [a for a in data["articles"] if a["name"] != "Rhum ambré"],
        })) as edited:
            mine = import_archive(edited, {KEY: REPLACE}).section(KEY)

        self.assertTrue(StockType.objects.filter(pk=self.rum.pk).exists())
        self.assertIn("Article « Rhum ambré » : encore utilisé (recette Mojito)", mine.kept)
        self.assertEqual(mine.tallies["articles"].deleted, 1)  # Gin, which nothing holds
        self.assertFalse(StockType.objects.filter(pk=self.gin.pk).exists())

    def test_an_article_with_a_loss_written_down_is_kept(self):
        make_movement(stock_type=self.gin, quantity="-0.5", kind=MovementKind.LOSS)
        self.gin_bottle.stock_type = None
        self.gin_bottle.save()

        mine = import_archive(self.reader, {KEY: REPLACE}).section(KEY)

        self.assertTrue(StockType.objects.filter(pk=self.gin.pk).exists())
        self.assertIn("Article « Gin » : des pertes ou corrections y sont saisies (Inventaires)", mine.kept)


class ClassificationTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_merge_onto_an_unclassified_product_classifies_it_and_completes_its_invoice(self):
        data = payload(
            supplier_names={"EPI_TEST": "Épicerie Test"},
            articles=[{"name": "Divers", "unit": "UNIT", "category": "Épicerie"}],
            products=[{"supplier": "EPI_TEST", "raw_name": "ARTICLE INCONNU", "article": "Divers", "unit": "UNIT",
                       "stock_equivalent": "1.0000"}],
        )
        self.pending.ai_suggestion = {"stock_type_name": "Autre chose"}
        self.pending.save()

        mine = import_payload(data)

        self.pending.refresh_from_db()
        self.assertEqual(self.pending.stock_type.name, "Divers")
        self.assertIsNone(self.pending.ai_suggestion)
        self.assertEqual(mine.tallies["produits classés"].updated, 1)
        self.assertTrue(StockMovement.objects.filter(invoice_line=self.lines[4]).exists())
        self.shop_invoice.refresh_from_db()
        self.assertEqual(self.shop_invoice.status, Invoice.Status.COMPLETE)

    def test_a_product_missing_here_is_created_classified(self):
        data = payload(
            supplier_names={"METRO": "Metro"},
            articles=[{"name": "Vodka", "unit": "L", "category": "Spiritueux", "loss_percent": "10.00"}],
            products=[{"supplier": "METRO", "raw_name": "VODKA Z 1L", "ean": "3000000000024", "article": "Vodka",
                       "unit": "L", "stock_equivalent": "1.0000", "created_at": "2025-03-04T10:15:30.125000+00:00"}],
        )

        mine = import_payload(data)

        product = Product.objects.get(supplier=self.metro, raw_name="VODKA Z 1L")
        self.assertEqual((product.stock_type, product.ean, product.is_expense), (self.vodka, "3000000000024", False))
        self.assertEqual(product.created_at.isoformat(), "2025-03-04T10:15:30.125000+00:00")
        self.assertEqual(mine.tallies["produits classés"].created, 1)
        self.assertIn("1 produit créé sans facture : ses achats compteront dès que ses factures arriveront",
                      mine.notes)

    def test_a_created_product_books_its_movements_when_a_later_invoice_arrives(self):
        """The app's own importer finds the product by its exact name and
        books the purchase at once."""
        from invoices.importing import import_parsed_invoice

        data = payload(
            supplier_names={"METRO": "Metro"},
            articles=[{"name": "Vodka", "unit": "L"}],
            products=[{"supplier": "METRO", "raw_name": "VODKA Z 1L", "article": "Vodka", "unit": "L",
                       "stock_equivalent": "1.0000"}],
        )
        import_payload(data)

        invoice = import_parsed_invoice(self.metro, ParsedInvoice(
            supplier_code="METRO", invoice_number="MET-002", invoice_date=None,
            lines=[ParsedLine(raw_name="VODKA Z 1L", quantity=6, total_volume=Decimal("6.000"),
                              unit_cost_ht=Decimal("12.0000"), total_ht=Decimal("72.00"))],
        ))

        line = invoice.lines.get()
        self.assertEqual(line.product.raw_name, "VODKA Z 1L")
        self.assertEqual(StockMovement.objects.get(invoice_line=line).quantity, Decimal("6.000"))
        self.assertEqual(invoice.status, Invoice.Status.COMPLETE)

    def test_a_created_product_books_its_movements_when_its_invoices_come_in_the_same_run(self):
        """Order 30 < 60: the invoices section finds the product the
        associations created - once lane A's sections are installed."""
        if not registry.is_registered("factures"):
            self.skipTest("la partie « factures » n'est pas encore installée")
        rum = make_stock_type(name="Rhum", unit=UnitChoices.LITRE)
        new = make_product(self.metro, "RHUM W 70CL", rum, unit=UnitChoices.LITRE)
        never_bought = make_product(self.metro, "RHUM V 1L", rum, unit=UnitChoices.LITRE)
        invoice = make_invoice(self.metro, invoice_number="MET-003")
        make_invoice_line(invoice, new, quantity=Decimal("2"), total_volume="1.400", total_ht="30.00")
        reader = export_archive({"factures", KEY}, closed=False)
        self.addCleanup(reader.close)
        invoice.delete()
        new.delete()
        never_bought.delete()

        mine = import_archive(reader, {"factures": MERGE, KEY: MERGE}).section(KEY)

        line = Invoice.objects.get(invoice_number="MET-003").lines.get()
        self.assertEqual(line.product.stock_type.name, "Rhum")
        self.assertEqual(StockMovement.objects.get(invoice_line=line).quantity, Decimal("1.400"))
        # Only the one whose invoices are not in the archive waits for them:
        # said of both, the note read false for the one bought in this run.
        self.assertEqual(mine.tallies["produits classés"].created, 2)
        self.assertIn("1 produit créé sans facture : ses achats compteront dès que ses factures arriveront",
                      mine.notes)

    def test_replace_unlinks_a_product_the_file_lacks_and_its_movements_go(self):
        with export_archive({KEY}, closed=False) as reader, ArchiveReader(forge(reader, associations=lambda data: {
            **data, "products": [p for p in data["products"] if p["raw_name"] != "CITRON VERT FILET"],
        })) as edited:
            mine = import_archive(edited, {KEY: REPLACE}).section(KEY)

        self.net.refresh_from_db()
        self.assertIsNone(self.net.stock_type)
        self.assertEqual(self.net.stock_equivalent, Decimal("12.0000"))  # as unlink_product leaves it
        self.assertFalse(StockMovement.objects.filter(invoice_line__product=self.net).exists())
        self.assertEqual(StockMovement.objects.count(), 3)
        # « Supprimés » counts rows that go: this product stays, unclassified.
        self.assertEqual(mine.tallies["produits classés"].updated, 1)
        self.assertEqual(mine.tallies["produits classés"].deleted, 0)
        self.assertTrue(StockType.objects.filter(pk=self.limes.pk).exists())  # the file still names it

    def test_an_unlinked_products_invoice_waits_again(self):
        self.assertEqual(self.metro_invoice.status, Invoice.Status.COMPLETE)
        with export_archive({KEY}, closed=False) as reader, ArchiveReader(forge(reader, associations=lambda data: {
            **data, "products": [p for p in data["products"] if p["raw_name"] != "VODKA X 70CL"],
        })) as edited:
            import_archive(edited, {KEY: REPLACE})

        self.metro_invoice.refresh_from_db()
        self.assertEqual(self.metro_invoice.status, Invoice.Status.NEEDS_REVIEW)

    def test_replace_removes_a_product_it_had_created_once_unlinked(self):
        """A product nothing was ever bought as, and that the file no longer
        classifies, was only there because an import created it."""
        lonely = make_product(self.metro, "VODKA Z 1L", self.vodka, unit=UnitChoices.LITRE)
        with export_archive({KEY}, closed=False) as reader, ArchiveReader(forge(reader, associations=lambda data: {
            **data, "products": [p for p in data["products"] if p["raw_name"] != "VODKA Z 1L"],
        })) as edited:
            mine = import_archive(edited, {KEY: REPLACE}).section(KEY)

        self.assertFalse(Product.objects.filter(pk=lonely.pk).exists())
        self.assertEqual(mine.tallies["produits sans facture"].deleted, 1)
        # Counted once, as the row that goes - not also as a classification removed.
        self.assertEqual((mine.tallies["produits classés"].updated, mine.tallies["produits classés"].deleted), (0, 0))

    def test_replace_changing_an_articles_unit_says_to_check_its_recipes(self):
        make_ingredient(make_recipe(name="Caïpirinha"), stock_type=self.limes, quantity="1")
        with export_archive({KEY}, closed=False) as reader, ArchiveReader(forge(reader, associations=lambda data: {
            **data,
            "articles": [dict(a, unit="KG") if a["name"] == "Citrons verts" else a for a in data["articles"]],
            "products": [dict(p, unit="KG", stock_equivalent="0.0700") if p["raw_name"] == "CITRON VERT FILET" else p
                         for p in data["products"]],
        })) as edited:
            mine = import_archive(edited, {KEY: REPLACE}).section(KEY)

        self.limes.refresh_from_db()
        self.net.refresh_from_db()
        self.assertEqual((self.limes.unit, self.net.unit, self.net.stock_equivalent),
                         ("KG", "KG", Decimal("0.0700")))
        self.assertIn("« Citrons verts » change d'unité (unités → kilos) : vérifiez les recettes qui l'utilisent",
                      mine.notes)


class RecordChecksTests(TestCase):
    """Each check skips the record with its reason and writes nothing of it."""

    def setUp(self):
        build_fixture(self)

    def product(self, **changes):
        record = {"supplier": "EPI_TEST", "raw_name": "ARTICLE INCONNU", "article": "Citrons verts", "unit": "UNIT",
                  "stock_equivalent": "12.0000"}
        record.update(changes)
        return {key: value for key, value in record.items() if value is not None}

    def run_products(self, *products, articles=(), strategy=MERGE):
        fingerprint = db_fingerprint()
        mine = import_payload(
            payload(products, articles, supplier_names={"EPI_TEST": "Épicerie Test", "LOYER_TEST": "Loyer Test"}),
            strategy,
        )
        return mine, fingerprint

    def assert_skipped(self, reason, *products, articles=()):
        mine, fingerprint = self.run_products(*products, articles=articles)
        self.assertEqual(mine.skipped, [reason])
        self.assertEqual(db_fingerprint(), fingerprint)

    def test_a_factor_of_zero_is_skipped_not_turned_into_one(self):
        self.assert_skipped(
            "Produit « ARTICLE INCONNU » (Épicerie Test) : conversion « 0 » : un nombre positif est attendu",
            self.product(stock_equivalent="0"),
        )

    def test_a_negative_or_unreadable_factor_is_skipped(self):
        label = "Produit « ARTICLE INCONNU » (Épicerie Test) : conversion"
        for factor, reason in (
            ("-1", "« -1 » : un nombre positif est attendu"),
            ("douze", "« douze » : un nombre positif d'au plus 4 décimales est attendu"),
            ("0.00001", "« 0.00001 » : un nombre positif d'au plus 4 décimales est attendu"),
            (0.7, "« 0.7 » : un nombre s'écrit entre guillemets"),  # a float: never guessed at
        ):
            with self.subTest(factor=factor):
                mine, fingerprint = self.run_products(self.product(stock_equivalent=factor))
                self.assertEqual(mine.skipped, [f"{label} {reason}"])
                self.assertEqual(db_fingerprint(), fingerprint)

    def test_a_factor_written_as_a_json_integer_is_a_number(self):
        mine, _ = self.run_products(self.product(stock_equivalent=12))
        self.assertEqual(mine.skipped, [])
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.stock_equivalent, Decimal("12.0000"))

    def test_a_missing_factor_is_skipped(self):
        self.assert_skipped("Produit « ARTICLE INCONNU » (Épicerie Test) : sans conversion",
                            self.product(stock_equivalent=None))

    def test_a_unit_other_than_the_files_article_is_skipped(self):
        self.assert_skipped(
            "Produit « ARTICLE INCONNU » (Épicerie Test) : en litres alors que son article « Citrons verts » "
            "est en unités dans l'archive",
            self.product(unit="L"),
            articles=[{"name": "Citrons verts", "unit": "UNIT", "category": "Épicerie", "loss_percent": "0.00"}],
        )

    def test_an_article_in_another_unit_here_is_skipped(self):
        self.assert_skipped(
            "Produit « ARTICLE INCONNU » (Épicerie Test) : l'article « Citrons verts » est en unités ici, en kilos "
            "dans l'archive : sa conversion ne veut plus rien dire",
            self.product(unit="KG", stock_equivalent="0.0700"),
            articles=[{"name": "Citrons verts", "unit": "KG"}],
        )

    def test_a_charges_poste_is_never_classified(self):
        self.assert_skipped("Produit « Loyer Test » (Loyer Test) : poste de charge : jamais classé",
                            self.product(supplier="LOYER_TEST", raw_name="Loyer Test"))

    def test_a_product_of_a_charges_supplier_missing_here_is_created_as_the_archive_classifies_it(self):
        """What the archive classifies is stock there - a stock item's product
        its supplier kept when it turned to charges - and it is created as
        redo_as_expenses leaves such a product: classified, not a poste."""
        mine, _ = self.run_products(self.product(supplier="LOYER_TEST", raw_name="CAGETTE CITRONS VERTS"))

        self.assertEqual(mine.skipped, [])
        product = Product.objects.get(supplier=self.charges, raw_name="CAGETTE CITRONS VERTS")
        self.assertEqual((product.stock_type, product.is_expense), (self.limes, False))
        self.assertEqual(mine.tallies["produits classés"].created, 1)

    def test_an_expense_product_of_an_ordinary_supplier_is_never_classified(self):
        self.pending.is_expense = True
        self.pending.save()
        self.assert_skipped("Produit « ARTICLE INCONNU » (Épicerie Test) : poste de charge : jamais classé",
                            self.product())

    def test_an_unknown_supplier_is_skipped(self):
        self.assert_skipped("Produit « X » : fournisseur inconnu « NOWHERE »", self.product(supplier="NOWHERE", raw_name="X"))

    def test_a_supplier_known_by_name_only_is_found(self):
        """Codes differ between two databases; the name in supplier_names
        finds it."""
        mine = import_payload(payload(
            [self.product(supplier="EPICERIE_2")], supplier_names={"EPICERIE_2": "ÉPICERIE  test"},
        ))
        self.assertEqual(mine.skipped, [])
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.stock_type, self.limes)

    def test_an_unknown_article_is_skipped(self):
        self.assert_skipped("Produit « ARTICLE INCONNU » (Épicerie Test) : article inconnu « Mangue »",
                            self.product(article="Mangue"))

    def test_a_blank_article_is_skipped(self):
        self.assert_skipped("Produit « ARTICLE INCONNU » (Épicerie Test) : sans article", self.product(article=" "))

    def test_an_article_with_an_impossible_loss_is_skipped(self):
        mine, fingerprint = self.run_products(articles=[{"name": "Mangue", "unit": "KG", "loss_percent": "150.00"}])
        self.assertEqual(mine.skipped, ["Article « Mangue » : pertes « 150.00 » : entre 0 et 100 attendu"])
        self.assertEqual(db_fingerprint(), fingerprint)

    def test_an_article_with_an_unknown_unit_is_skipped(self):
        mine, _ = self.run_products(articles=[{"name": "Mangue", "unit": "BOX"}])
        self.assertEqual(mine.skipped, ["Article « Mangue » : « unit » : valeur inconnue (« BOX »)"])

    def test_a_product_twice_in_the_file_counts_once(self):
        for raw_name in ("ARTICLE INCONNU", "MANGUE FRAÎCHE"):  # one here, one the import creates
            with self.subTest(raw_name=raw_name):
                mine, _ = self.run_products(self.product(raw_name=raw_name),
                                            self.product(raw_name=raw_name, stock_equivalent="6"))
                self.assertEqual(
                    mine.skipped,
                    [f"Produit « {raw_name} » (Épicerie Test) : en double dans l'archive, seul le premier compte"],
                )
                self.assertEqual(Product.objects.get(raw_name=raw_name).stock_equivalent, Decimal("12.0000"))

    def test_an_unknown_field_is_noted_once(self):
        mine, _ = self.run_products(self.product(couleur="vert"), self.product(raw_name="BAVETTE", couleur="rouge",
                                                                                 article="Bœuf", unit="KG",
                                                                                 stock_equivalent="1"))
        self.assertEqual(mine.notes.count("champ inconnu ignoré : produits.couleur"), 1)

    def test_a_list_that_is_not_a_list_refuses_the_archive(self):
        with (
            ArchiveReader(forge({KEY: {"articles": [], "products": {"x": 1}}})) as reader,
            self.assertRaisesMessage(ArchiveError, "associations.json n'a pas de liste « products »"),
        ):
            import_archive(reader, {KEY: MERGE})


class PreviewTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_the_preview_changes_nothing_and_says_what_the_run_does(self):
        with export_archive({KEY}, closed=False) as reader, ArchiveReader(forge(reader, associations=lambda data: {
            **data,
            "articles": [*data["articles"], {"name": "Mangue", "unit": "KG"}],
            "products": [p for p in data["products"] if p["raw_name"] != "BAVETTE"],
        })) as edited:
            fingerprint, media = db_fingerprint(), media_listing()
            preview = import_archive(edited, {KEY: REPLACE}, preview=True)
            self.assertEqual(db_fingerprint(), fingerprint)
            self.assertEqual(media_listing(), media)
            done = import_archive(edited, {KEY: REPLACE})

        self.assertEqual(preview.outcome(), done.outcome())
        self.assertTrue(preview.section(KEY).changes)


class ClearTests(TestCase):
    def setUp(self):
        build_fixture(self)

    @staticmethod
    def state():
        return {
            "products": sorted(Product.objects.values_list("pk", "stock_type_id", "unit", "stock_equivalent",
                                                           "ai_suggestion")),
            "movements": sorted(StockMovement.objects.values_list("invoice_line_id", "quantity", "unit_cost_ht")),
            "statuses": sorted(Invoice.objects.values_list("pk", "status")),
        }

    def test_clear_equals_unlink_product_on_each_product(self):
        with transaction.atomic():
            for product in Product.objects.filter(stock_type__isnull=False):
                unlink_product(product)
            reference = self.state()
            transaction.set_rollback(True)
        self.assertNotEqual(self.state(), reference)

        with self.captureOnCommitCallbacks(execute=True):
            report = run_clear({KEY}, preview=False, closed=False)

        self.assertEqual(self.state(), reference)
        self.assertEqual(section().count(), {"articles": 0, "produits classés": 0})
        mine = report.section(KEY)
        # The three products stay (their lines hold them): modified, not deleted.
        self.assertEqual(mine.tallies["produits classés"].updated, 3)
        self.assertEqual(mine.tallies["produits classés"].deleted, 0)
        self.assertEqual(mine.tallies["mouvements d'achat"].deleted, 4)
        self.assertEqual(mine.tallies["articles"].deleted, 4)

    def test_clear_removes_the_products_nothing_was_bought_as(self):
        lonely = make_product(self.metro, "VODKA Z 1L", self.vodka, unit=UnitChoices.LITRE)

        report = run_clear({KEY}, preview=False, closed=False)

        self.assertFalse(Product.objects.filter(pk=lonely.pk).exists())
        self.assertTrue(Product.objects.filter(pk=self.pending.pk).exists())  # it has a line
        self.assertTrue(Product.objects.filter(pk=self.rent.pk).exists())
        mine = report.section(KEY)
        self.assertEqual(mine.tallies["produits sans facture"].deleted, 1)
        # Each product counted once: the one that goes, and the three that stay unclassified.
        self.assertEqual((mine.tallies["produits classés"].updated, mine.tallies["produits classés"].deleted), (3, 0))

    def test_a_clear_counts_each_product_once(self):
        """The table says what disappears. A full clear read 1 462 products
        « à supprimer » for 794 in the database: the classifications
        removed were counted as deleted, then the same products again as
        « produits sans facture ». « Supprimés » only counts rows that go."""
        if not registry.is_registered("factures"):
            self.skipTest("la partie « factures » n'est pas encore installée")
        products = Product.objects.count()

        with self.captureOnCommitCallbacks(execute=True):
            report = run_clear({KEY, "factures"}, preview=False, closed=False)

        self.assertFalse(Product.objects.exists())
        deleted = sum(
            tally.deleted
            for part in report.sections
            for label, tally in part.tallies.items()
            if label.startswith("produits")
        )
        self.assertEqual(deleted, products)
        self.assertEqual(report.section(KEY).tallies["produits sans facture"].deleted, 3)

    def test_the_clear_preview_changes_nothing(self):
        fingerprint = db_fingerprint()

        preview = run_clear({KEY}, preview=True, closed=False)

        self.assertEqual(db_fingerprint(), fingerprint)
        self.assertTrue(preview.section(KEY).destructive)


class CountAndExportTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_count(self):
        self.assertEqual(AssociationsSection().count(), {"articles": 4, "produits classés": 3})

    def test_the_export_names_suppliers_by_code_and_holds_no_pk(self):
        with export_archive({KEY}, closed=False) as reader:
            data = reader.section(KEY).payload()
            self.assertEqual(reader.section(KEY).counts, {"articles": 4, "produits classés": 3})
        self.assertEqual(data["supplier_names"], {"METRO": "Metro", "EPI_TEST": "Épicerie Test"})
        product = dict(data["products"][0])
        self.assertTrue(product.pop("created_at").endswith("+00:00"))
        self.assertEqual(
            product,
            {"supplier": "EPI_TEST", "raw_name": "BAVETTE", "ean": "", "article": "Bœuf", "unit": "KG",
             "stock_equivalent": "1.0000"},
        )
        limes = next(article for article in data["articles"] if article["name"] == "Citrons verts")
        created_at = limes.pop("created_at")
        self.assertEqual(
            limes,
            {
                "name": "Citrons verts",
                "unit": "UNIT",
                "category": "Épicerie",
                "loss_percent": "0.00",
                "count_in_products_margin": False,
            },
        )
        self.assertTrue(created_at.endswith("+00:00"))
        for record in [*data["articles"], *data["products"]]:
            self.assertNotIn("id", record)
            self.assertNotIn("pk", record)
