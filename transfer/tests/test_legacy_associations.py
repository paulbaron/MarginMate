"""The old « Exporter les associations » file still imports (§4.6), through
the same preview and rules as an archive.

The old import had four silent behaviours this one must not keep: it
matched a supplier by its exact name only, ignored the product's own unit,
dropped a blank article without a word, and turned a factor of 0 into 1 -
stock booked at a conversion nobody typed. The file layout below is the old
view's own (`inventory/views.py` before 19/09), with invented names.
"""

import json
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product, StockMovement, StockType, UnitChoices
from invoices.models import Invoice
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_type,
    make_supplier,
)
from transfer import legacy, registry, staging
from transfer.archive import ArchiveReader
from transfer.sections.base import Strategy
from transfer.tests.support import (
    FAKES,
    db_fingerprint,
    import_archive,
    new_archive_path,
)

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE


def entry(supplier, raw_name, article, unit, factor, *, category="", article_unit=None):
    return {
        "supplier": supplier,
        "raw_name": raw_name,
        "product_unit": unit,
        "stock_equivalent": factor,
        "stock_type_name": article,
        "stock_type_category": category,
        "stock_type_unit": article_unit or unit,
    }


OLD_FILE = {
    "version": 1,
    "products": [
        entry("Metro", "VODKA X 70CL", "Vodka", "L", "1.0000", category="Spiritueux"),
        # Supplier names were written as the old database spelled them: matched folded.
        entry("épicerie  TEST", "BAVETTE", "Bœuf", "KG", "1.0000", category="Boucherie"),
        entry("Épicerie Test", "CITRON VERT FILET", "Citrons verts", "UNIT", "12.0000", category="Épicerie"),
        # The product's own unit disagrees with its article's: the old import
        # linked it in the article's unit whatever it said.
        entry("Épicerie Test", "SUCRE 1KG", "Sucre", "UNIT", "1.0000", category="Épicerie", article_unit="KG"),
        entry("Épicerie Test", "SACHET MYSTÈRE", "", "UNIT", "1.0000"),
        entry("Épicerie Test", "MENTHE BOTTE", "Menthe", "UNIT", "0.0000", category="Épicerie"),
        entry("Fournisseur Disparu", "RHUM Q 70CL", "Vodka", "L", "1.0000", category="Spiritueux"),
    ],
}


def old_file_bytes(payload=OLD_FILE, *, bom=False) -> bytes:
    """As the old view wrote it: indented, UTF-8, accents unescaped."""
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def reader_of(payload=OLD_FILE) -> ArchiveReader:
    path = new_archive_path("legacy")
    legacy.to_archive(payload, path)
    return ArchiveReader(path)


class ConversionTests(TestCase):
    def test_the_archive_holds_only_the_associations(self):
        with reader_of() as reader:
            self.assertEqual(reader.sections, {"associations"})
            self.assertEqual(reader.reason, "ancien export d'associations")
            self.assertEqual(reader.section("associations").counts, {"articles": 5, "produits classés": 7})
            data = reader.section("associations").payload()

        self.assertEqual(data["supplier_names"], {"~1": "Metro", "~2": "épicerie  TEST", "~3": "Fournisseur Disparu"})
        self.assertEqual(
            data["articles"],
            [
                {"name": "Vodka", "unit": "L", "category": "Spiritueux"},
                {"name": "Bœuf", "unit": "KG", "category": "Boucherie"},
                {"name": "Citrons verts", "unit": "UNIT", "category": "Épicerie"},
                {"name": "Sucre", "unit": "KG", "category": "Épicerie"},
                {"name": "Menthe", "unit": "UNIT", "category": "Épicerie"},
            ],
        )
        self.assertEqual(
            data["products"][2],
            {"supplier": "~2", "raw_name": "CITRON VERT FILET", "article": "Citrons verts", "unit": "UNIT",
             "stock_equivalent": "12.0000"},
        )
        # Nothing repaired on the way: a blank article and a 0 factor reach the section as they were.
        self.assertEqual(data["products"][4]["article"], "")
        self.assertEqual(data["products"][5]["stock_equivalent"], "0.0000")

    def test_a_hand_edited_number_keeps_its_digits(self):
        payload = {"version": 1, "products": [entry("Metro", "VODKA X 70CL", "Vodka", "L", 0.7)]}
        with reader_of(payload) as reader:
            self.assertEqual(reader.section("associations").payload()["products"][0]["stock_equivalent"], "0.7")

    def test_an_unreadable_entry_is_carried_for_the_section_to_refuse(self):
        payload = {"version": 1, "products": ["pas un produit", {"supplier": 3, "raw_name": "X"}]}
        with reader_of(payload) as reader:
            products = reader.section("associations").payload()["products"]
        self.assertEqual(products, ["pas un produit", {"supplier": None, "raw_name": "X", "article": ""}])


class ImportTests(TestCase):
    def setUp(self):
        self.metro = make_supplier(code="METRO", name="Metro", parser_key="METRO")
        self.shop = make_supplier(code="EPI_TEST", name="Épicerie Test")
        self.meat = make_stock_type(name="Viande", unit=UnitChoices.KILOGRAM)
        self.vodka_bottle = make_product(self.metro, "VODKA X 70CL")  # « à classer »
        self.bavette = make_product(self.shop, "BAVETTE", self.meat, unit=UnitChoices.KILOGRAM)
        self.sugar = make_product(self.shop, "SUCRE 1KG")
        self.invoice = make_invoice(self.metro, invoice_number="MET-001", status=Invoice.Status.NEEDS_REVIEW)
        self.line = make_invoice_line(self.invoice, self.vodka_bottle, quantity=Decimal("6"), total_volume="4.200",
                                      total_ht="57.00")

    def run_old_file(self, strategy=MERGE, payload=OLD_FILE):
        with reader_of(payload) as reader:
            return import_archive(reader, {"associations": strategy}).section("associations")

    def test_the_old_file_imports(self):
        mine = self.run_old_file()

        # The supplier matched by name, the product classified, its purchase booked.
        self.vodka_bottle.refresh_from_db()
        self.assertEqual((self.vodka_bottle.stock_type.name, self.vodka_bottle.unit), ("Vodka", "L"))
        self.assertEqual(StockMovement.objects.get(invoice_line=self.line).quantity, Decimal("4.200"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.COMPLETE)
        # A product missing here is created now (the old import left it out).
        lime = Product.objects.get(supplier=self.shop, raw_name="CITRON VERT FILET")
        self.assertEqual((lime.stock_type.name, lime.unit, lime.stock_equivalent),
                         ("Citrons verts", "UNIT", Decimal("12.0000")))
        self.assertEqual(mine.tallies["produits classés"].created, 1)
        self.assertEqual(mine.tallies["produits classés"].updated, 1)
        # loss_percent was never in the old file: the new articles take the default.
        self.assertEqual(StockType.objects.get(name="Vodka").loss_percent, Decimal("10.00"))

    def test_what_cannot_be_applied_is_said_not_repaired(self):
        mine = self.run_old_file()

        self.assertEqual(
            mine.skipped,
            [
                (
                    "Produit « SUCRE 1KG » (Épicerie Test) : en unités alors que son article « Sucre » est en "
                    "kilos dans l'archive"
                ),
                "Produit « SACHET MYSTÈRE » (Épicerie Test) : sans article",
                "Produit « MENTHE BOTTE » (Épicerie Test) : conversion « 0.0000 » : un nombre positif est attendu",
                "Produit « RHUM Q 70CL » : fournisseur inconnu « Fournisseur Disparu »",
            ],
        )
        self.sugar.refresh_from_db()
        self.assertIsNone(self.sugar.stock_type)  # not linked in kilos behind the owner's back
        self.assertFalse(Product.objects.filter(raw_name__in=["SACHET MYSTÈRE", "MENTHE BOTTE"]).exists())

    def test_the_product_unit_and_factor_are_the_files(self):
        """Read from the file, not recomputed from the article (the unit
        that disagrees with its article is refused above, not overridden)."""
        payload = {"version": 1, "products": [entry("Metro", "VODKA X 70CL", "Vodka", "L", "0.7000")]}
        self.run_old_file(payload=payload)
        self.vodka_bottle.refresh_from_db()
        self.assertEqual((self.vodka_bottle.unit, self.vodka_bottle.stock_equivalent), ("L", Decimal("0.7000")))

    def test_a_conflict_is_counted_and_never_overwritten_by_fusionner(self):
        mine = self.run_old_file()

        self.bavette.refresh_from_db()
        self.assertEqual(self.bavette.stock_type, self.meat)
        self.assertEqual(
            mine.conflicts, ["Produit « BAVETTE » (Épicerie Test) : différent dans l'archive (article) — gardé tel quel"]
        )

    def test_remplacer_is_allowed(self):
        self.run_old_file(REPLACE)

        self.bavette.refresh_from_db()
        self.assertEqual(self.bavette.stock_type.name, "Bœuf")

    def test_two_products_differing_by_an_accented_capital_are_both_created(self):
        """Two products of the old database (SQLite's case-blind comparison
        is ASCII only, so its matcher made them two): created here as two,
        never the second caught by the first's folded name and skipped « en
        double »."""
        payload = {"version": 1, "products": [
            entry("Épicerie Test", "SIROP D'ÉRABLE 1L", "Sirop", "L", "1.0000"),
            entry("Épicerie Test", "SIROP D'éRABLE 1L", "Sirop", "L", "1.0000"),
        ]}

        mine = self.run_old_file(payload=payload)

        self.assertEqual(mine.skipped, [])
        self.assertEqual(mine.tallies["produits classés"].created, 2)
        syrups = Product.objects.filter(supplier=self.shop, raw_name__startswith="SIROP")
        self.assertEqual(sorted(syrups.values_list("raw_name", flat=True)), ["SIROP D'ÉRABLE 1L", "SIROP D'éRABLE 1L"])

    def test_importing_it_twice_changes_nothing_the_second_time(self):
        self.run_old_file()
        fingerprint = db_fingerprint()

        mine = self.run_old_file()

        self.assertEqual(db_fingerprint(), fingerprint)
        self.assertEqual(mine.tallies["produits classés"].created, 0)
        self.assertEqual(mine.tallies["produits classés"].updated, 0)
        self.assertEqual(mine.tallies["produits classés"].unchanged, 2)


class StagingTests(TestCase):
    def test_the_old_file_is_staged_as_an_archive(self):
        for bom in (False, True):
            with self.subTest(bom=bom):
                stage = staging.stage_upload(
                    SimpleUploadedFile("marginmate-associations.json", old_file_bytes(bom=bom))
                )
                self.addCleanup(staging.discard, stage)
                self.assertTrue(stage.legacy)
                self.assertEqual(stage.sections, {"associations"})
                with stage.open() as reader:
                    self.assertEqual(len(reader.section("associations").payload()["products"]), 7)


class PageTests(TestCase):
    """Upload, then preview, through the page: what the owner will do with
    the file they kept."""

    def setUp(self):
        registered = registry.registered()
        # The page offers a section only once what it requires is installed;
        # a stand-in for the suppliers until lane A's lands (unused here: the
        # old file holds no supplier part).
        swap = registry.swap({**registered, "fournisseurs": registered.get("fournisseurs", FAKES["fournisseurs"])})
        swap.__enter__()
        self.addCleanup(swap.__exit__, None, None, None)
        make_supplier(code="EPI_TEST", name="Épicerie Test")

    def test_upload_and_preview(self):
        response = self.client.post(
            reverse("transfer:data_import"),
            {"archive": SimpleUploadedFile("marginmate-associations.json", old_file_bytes())},
        )
        stage_url = response["Location"]
        self.addCleanup(lambda: staging.discard(staging.get(stage_url.rstrip("/").split("/")[-1])))
        fingerprint = db_fingerprint()

        response = self.client.post(
            stage_url, {"action": "previsualiser", "sections": ["associations"], "strategie-associations": "fusionner"}
        )
        self.assertRedirects(response, stage_url, fetch_redirect_response=False)
        page = self.client.get(stage_url)

        self.assertEqual(db_fingerprint(), fingerprint)
        self.assertContains(page, "Ancien fichier « marginmate-associations.json »")
        self.assertContains(page, "Associations produits → articles › produits classés")
        self.assertContains(page, "Produit « SACHET MYSTÈRE » (Épicerie Test) : sans article")
        self.assertContains(page, "conversion « 0.0000 » : un nombre positif est attendu")
        self.assertContains(page, 'value="importer"')
