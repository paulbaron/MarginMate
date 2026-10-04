"""« Enseignes et fournisseurs » (§7.1, §10.2).

What these guard, before anything else, is Metro's firewall state: the
`scrape_*` fields are never exported, never written and never reset - by an
import, a replace or a clear - since a restore that put back an older pause
(or none) would let the next gather sign in to a site that blocks it. And a
supplier a reader or a till is keyed on is never deleted.

Every name, code and figure below is invented.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from django.test import TestCase

from bank.models import BankTransaction, CounterpartyAlias
from inventory.models import Product
from invoices.models import (
    Invoice,
    InvoiceLine,
    ShopItemPrice,
    Supplier,
    SupplierChange,
)
from invoices.workspace import _changes_to_see
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier
from transfer import registry
from transfer.archive import ArchiveError
from transfer.runner import run_clear
from transfer.sections import suppliers as section
from transfer.sections.base import Strategy
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    round_trip,
)
from transfer.tests.test_bank_section import make_line, pay

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
#: What separates an amount's thousands in the report (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"
SEEDS = {"METRO", "UBA", "FRANPRIX", "MONOPRIX", "SABBH", "WINGSENG"}
BOUND = SEEDS | {"CECINA"}
PAUSE = {
    "scrape_last_login_at": datetime(2026, 9, 17, 14, 2, 11, 120000, tzinfo=UTC),
    "scrape_last_block_at": datetime(2026, 9, 18, 14, 19, 3, tzinfo=UTC),
    "scrape_paused_until": datetime(2026, 10, 2, 14, 19, 3, tzinfo=UTC),
    "scrape_pause_reason": "Page de refus du pare-feu (essai)",
}


def scrape_state(code="METRO") -> dict:
    return dict(Supplier.objects.filter(code=code).values(*PAUSE).get())


def price(supplier, amount, label, valid_from=None, created=None) -> ShopItemPrice:
    item = ShopItemPrice.objects.create(
        supplier=supplier, unit_price_ttc=Decimal(amount), label=label, valid_from=valid_from
    )
    ShopItemPrice.objects.filter(pk=item.pk).update(created_at=created or datetime(2026, 8, 3, 10, 0, tzinfo=UTC))
    return item


def build_suppliers():
    """The code-bound seven plus a CECINA, a shop with a header and figures,
    a supplier of charges, and prices dated and undated at one amount."""
    metro = Supplier.objects.get(code="METRO")
    Supplier.objects.filter(pk=metro.pk).update(ticket_identifiers=["siren:900000001", "web:metro.example"], **PAUSE)
    make_supplier(code="CECINA", name="Cecina Essai", parser_key="CECINA", ticket_identifiers=["siren:900000002"])
    sabbh = Supplier.objects.get(code="SABBH")
    sabbh.ticket_header = "SABBH ORIENTAL ESSAI"
    sabbh.save()
    price(sabbh, "0.70", "Pita", valid_from=date(2026, 8, 1))
    price(sabbh, "0.70", "Citron")
    price(sabbh, "1.20", "Menthe", valid_from=date(2026, 7, 1))
    bakery = make_supplier(
        code="BOULANGERIE_ESSAI",
        name="Boulangerie Essai",
        ticket_header="BOULANGERIE ESSAI",
        ticket_identifiers=["tel:0100000001"],
        refused_identifiers=["tel:0100000009"],
    )
    price(bakery, "1.10", "Baguette")
    make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
    return bakery


class GroupedPriceTests(TestCase):
    """A known price is named in the report as the pages print an amount,
    its thousands grouped by a no-break space (the owner, 01/10/2026)."""

    def test_a_price_over_a_thousand_reads_grouped_in_a_conflict(self):
        sabbh = Supplier.objects.get(code="SABBH")
        price(sabbh, "1234.50", "Fût inventé", valid_from=date(2026, 7, 1))
        reader = export_archive({"fournisseurs"})
        self.addCleanup(reader.close)
        ShopItemPrice.objects.filter(label="Fût inventé").update(label="Fût inventé 30 L")
        report = import_archive(reader, MERGE).section("fournisseurs")
        self.assertIn(
            f"Prix 1{NBSP}234,50 € de {sabbh.name} (depuis le 01/07/2026) : « Fût inventé 30 L » ici, "
            "« Fût inventé » dans l'archive — gardé tel quel",
            report.conflicts,
        )

    def test_below_a_thousand_nothing_is_added(self):
        self.assertEqual(section.money("1.2"), "1,20 €")


class GuardTests(TestCase):
    def test_every_supplier_field_is_exported_or_said_why(self):
        names = {model_field.name for model_field in Supplier._meta.concrete_fields}
        self.assertEqual(names, set(section.SUPPLIER_FIELDS) | set(section.NOT_EXPORTED))
        self.assertFalse(set(section.SUPPLIER_FIELDS) & set(section.NOT_EXPORTED))

    def test_every_price_field_is_exported_or_said_why(self):
        names = {model_field.name for model_field in ShopItemPrice._meta.concrete_fields}
        self.assertEqual(names, set(section.PRICE_FIELDS) | set(section.PRICE_NOT_EXPORTED))

    def test_the_firewall_state_is_never_in_the_archive(self):
        build_suppliers()
        reader = export_archive({"fournisseurs"})
        self.addCleanup(reader.close)
        payload = reader.section("fournisseurs").payload()
        for record in payload["suppliers"]:
            self.assertFalse(set(record) & set(PAUSE), record["code"])
        self.assertNotIn(PAUSE["scrape_pause_reason"], reader.path.read_bytes().decode("latin-1"))

    def test_code_bound(self):
        build_suppliers()
        bound = {supplier.code for supplier in Supplier.objects.all() if section.code_bound(supplier)}
        self.assertEqual(bound, BOUND)

    def test_count_counts_every_supplier(self):
        build_suppliers()
        self.assertEqual(
            registry.get("fournisseurs").count(),
            {"fournisseurs": Supplier.objects.count(), "prix connus": 4},
        )

    def test_the_archive_counts_what_count_counts_under_the_same_labels(self):
        """The import tab puts the two side by side: « archive : 30
        fournisseurs · 5 prix d'articles — ici : 29 fournisseurs · 5 prix
        d'articles sans nom » on identical data read as one supplier more
        in the archive, and one entity under two names."""
        build_suppliers()
        reader = export_archive({"fournisseurs"})
        self.addCleanup(reader.close)
        self.assertEqual(reader.counts("fournisseurs"), registry.get("fournisseurs").count())


class RoundTripTests(TestCase):
    def setUp(self):
        build_suppliers()

    def _after_clear(self):
        self.assertEqual(set(Supplier.objects.values_list("code", flat=True)), BOUND)
        self.assertEqual(ShopItemPrice.objects.count(), 0)

    def test_merge(self):
        before, after = round_trip({"fournisseurs"}, MERGE, after_clear=self._after_clear)
        self.assertEqual(after, before)
        self.assertEqual(scrape_state(), PAUSE)

    def test_replace(self):
        before, after = round_trip({"fournisseurs"}, REPLACE, after_clear=self._after_clear)
        self.assertEqual(after, before)
        self.assertEqual(scrape_state(), PAUSE)

    def test_dated_and_undated_prices_at_one_amount_are_two(self):
        _before, _after = round_trip({"fournisseurs"}, MERGE)
        self.assertEqual(
            sorted(
                ShopItemPrice.objects.filter(supplier__code="SABBH", unit_price_ttc="0.70").values_list(
                    "label", flat=True
                )
            ),
            ["Citron", "Pita"],
        )


class IdempotenceTests(TestCase):
    def setUp(self):
        build_suppliers()
        self.reader = export_archive({"fournisseurs"})
        self.addCleanup(self.reader.close)

    def test_merge_says_everything_is_unchanged(self):
        report = import_archive(self.reader, MERGE).section("fournisseurs")
        self.assertEqual(report.tallies["fournisseurs"].unchanged, Supplier.objects.count())
        self.assertEqual(report.tallies["prix connus"].unchanged, 4)
        self.assertEqual((report.conflicts, report.skipped, report.kept), ([], [], []))
        self.assertFalse(report.changes)

    def test_replace_changes_nothing(self):
        before = db_fingerprint()
        report = import_archive(self.reader, REPLACE).section("fournisseurs")
        self.assertFalse(report.changes)
        self.assertEqual((report.conflicts, report.skipped, report.kept), ([], [], []))
        self.assertEqual(db_fingerprint(), before)

    def test_the_report_counts_what_count_and_the_archive_count(self):
        """« 30 inchangés » for the 29 suppliers the page and the archive
        announced, when a supplier was counted in the report only."""
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                report = import_archive(self.reader, strategy).section("fournisseurs")
                tally = report.tallies["fournisseurs"]
                counted = tally.created + tally.updated + tally.unchanged
                self.assertEqual(counted, registry.get("fournisseurs").count()["fournisseurs"])
                self.assertEqual(counted, self.reader.counts("fournisseurs")["fournisseurs"])


class MergeAndReplaceTests(TestCase):
    """The archive and this database differ by one supplier changed, one
    only here, one only in the archive, and a blank here to fill."""

    def setUp(self):
        self.bakery = build_suppliers()
        make_supplier(code="CAVE_ESSAI", name="Cave Essai", ticket_header="CAVE ESSAI")
        self.reader = export_archive({"fournisseurs"})
        self.addCleanup(self.reader.close)
        Supplier.objects.filter(code="BOULANGERIE_ESSAI").update(is_scrapable=True, ticket_header="")
        Supplier.objects.filter(code="CAVE_ESSAI").delete()
        make_supplier(code="FLEURISTE_ESSAI", name="Fleuriste Essai")
        ShopItemPrice.objects.filter(label="Menthe").update(label="Menthe fraîche")

    def test_merge_adds_what_is_missing_fills_blanks_and_keeps_the_rest(self):
        report = import_archive(self.reader, MERGE).section("fournisseurs")
        bakery = Supplier.objects.get(code="BOULANGERIE_ESSAI")
        self.assertEqual(bakery.ticket_header, "BOULANGERIE ESSAI")  # a blank, filled
        self.assertTrue(bakery.is_scrapable)  # a value, kept
        self.assertTrue(Supplier.objects.filter(code="CAVE_ESSAI").exists())
        self.assertTrue(Supplier.objects.filter(code="FLEURISTE_ESSAI").exists())
        self.assertEqual(report.tallies["fournisseurs"].created, 1)
        self.assertEqual(report.tallies["fournisseurs"].updated, 1)
        self.assertEqual(report.tallies["fournisseurs"].deleted, 0)
        self.assertEqual(
            report.conflicts,
            [
                "Fournisseur « Boulangerie Essai » : différent dans l'archive (récupération automatique) — gardé tel quel",
                (
                    "Prix 1,20 € de Sabbh Oriental (depuis le 01/07/2026) : « Menthe fraîche » ici, « Menthe » dans "
                    "l'archive — gardé tel quel"
                ),
            ],
        )
        self.assertTrue(ShopItemPrice.objects.filter(label="Menthe fraîche").exists())

    def test_replace_makes_the_section_the_archive(self):
        report = import_archive(self.reader, REPLACE).section("fournisseurs")
        bakery = Supplier.objects.get(code="BOULANGERIE_ESSAI")
        self.assertEqual((bakery.ticket_header, bakery.is_scrapable), ("BOULANGERIE ESSAI", False))
        self.assertTrue(Supplier.objects.filter(code="CAVE_ESSAI").exists())
        self.assertFalse(Supplier.objects.filter(code="FLEURISTE_ESSAI").exists())
        tally = report.tallies["fournisseurs"]
        self.assertEqual((tally.created, tally.updated, tally.deleted), (1, 2, 1))
        self.assertEqual(report.tallies["prix connus"].updated, 1)
        self.assertTrue(ShopItemPrice.objects.filter(label="Menthe").exists())
        self.assertEqual(scrape_state(), PAUSE)

    def test_replace_keeps_a_supplier_documents_are_filed_under_and_says_so(self):
        florist = Supplier.objects.get(code="FLEURISTE_ESSAI")
        make_invoice(supplier=florist, invoice_number="F-1")
        report = import_archive(self.reader, REPLACE).section("fournisseurs")
        self.assertTrue(Supplier.objects.filter(code="FLEURISTE_ESSAI").exists())
        self.assertIn("Fournisseur « Fleuriste Essai » : 1 document y est rangé (Factures non remplacées)", report.kept)

    def test_replace_never_deletes_a_code_bound_supplier(self):
        reader = forge(
            self.reader,
            fournisseurs=lambda payload: {
                **payload,
                "suppliers": [record for record in payload["suppliers"] if record["code"] not in BOUND],
            },
        )
        from transfer.archive import ArchiveReader

        with ArchiveReader(reader) as forged:
            report = import_archive(forged, REPLACE).section("fournisseurs")
        self.assertTrue(BOUND <= set(Supplier.objects.values_list("code", flat=True)))
        self.assertIn(f"Fournisseur « Metro » : {section.KEPT_BOUND}", report.kept)
        self.assertEqual(scrape_state(), PAUSE)

    def test_replace_keeps_a_supplier_with_a_source_or_classified_products(self):
        from inventory.models import StockType
        from tests.factories import make_invoice_type

        florist = Supplier.objects.get(code="FLEURISTE_ESSAI")
        make_invoice_type(supplier=florist, name="Fleuriste - Factures")
        grocer = make_supplier(code="EPICERIE_ESSAI", name="Épicerie Essai")
        make_product(
            supplier=grocer, raw_name="SEL FIN 1KG", stock_type=StockType.objects.create(name="Sel", unit="KG")
        )
        report = import_archive(self.reader, REPLACE).section("fournisseurs")
        self.assertIn(
            "Fournisseur « Fleuriste Essai » : 1 source récupère pour lui (Sources non remplacées)", report.kept
        )
        self.assertIn("Fournisseur « Épicerie Essai » : 1 produit classé (Associations non remplacées)", report.kept)

    def test_replace_keeps_a_supplier_returnables_name_and_says_so(self):
        """A slip format and a pickup hold their supplier (PROTECT): kept
        under the false « un de ses produits sert encore » before, since
        `_holders` looked at documents, sources and products only. Each is
        left there by its own section: a slip format by « Types et formats de
        consignes », a pickup by « Consignes »."""
        from returnables.tests.support import make_format, make_pickup

        florist = Supplier.objects.get(code="FLEURISTE_ESSAI")
        make_format(name="Fleuriste Essai — bon", supplier=florist)
        grocer = make_supplier(code="EPICERIE_ESSAI", name="Épicerie Essai")
        make_pickup(supplier=grocer)
        make_pickup(supplier=grocer)
        both = make_supplier(code="CAVISTE_ESSAI", name="Caviste Essai")
        make_format(name="Caviste Essai — bon", supplier=both)
        make_pickup(supplier=both)
        report = import_archive(self.reader, REPLACE).section("fournisseurs")
        self.assertIn(
            "Fournisseur « Fleuriste Essai » : 1 format de bon de consignes est à son nom "
            "(Types et formats de consignes non remplacés)",
            report.kept,
        )
        self.assertIn(
            "Fournisseur « Épicerie Essai » : 2 reprises de consignes sont à son nom (Consignes non remplacées)",
            report.kept,
        )
        self.assertIn(
            "Fournisseur « Caviste Essai » : 1 format de bon de consignes et 1 reprise de consignes sont à son nom "
            "(Types et formats de consignes non remplacés, Consignes non remplacées)",
            report.kept,
        )
        self.assertFalse(any("un de ses produits" in line for line in report.kept), report.kept)
        self.assertEqual(
            Supplier.objects.filter(code__in=["FLEURISTE_ESSAI", "EPICERIE_ESSAI", "CAVISTE_ESSAI"]).count(), 3
        )

    def test_replace_deletes_a_suppliers_unused_products_and_says_its_payee_names_go(self):
        florist = Supplier.objects.get(code="FLEURISTE_ESSAI")
        make_product(supplier=florist, raw_name="ROSES")
        CounterpartyAlias.objects.create(supplier=florist, name="FLEURISTE ESSAI SARL")
        run = import_archive(self.reader, REPLACE)
        self.assertFalse(Supplier.objects.filter(code="FLEURISTE_ESSAI").exists())
        self.assertFalse(Product.objects.filter(raw_name="ROSES").exists())
        self.assertEqual(run.section("banque").tallies["noms de payeurs appris"].deleted, 1)
        self.assertIn("banque", run.affected())

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())


class RenameTests(TestCase):
    def setUp(self):
        self.water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
        charge_item = make_product(supplier=self.water, raw_name="Eau Essai", is_expense=True)
        make_invoice_line(invoice=make_invoice(supplier=self.water), product=charge_item, raw_name="Eau Essai")
        self.reader = export_archive({"fournisseurs"})
        self.addCleanup(self.reader.close)
        Supplier.objects.filter(pk=self.water.pk).update(name="Eau de la Ville")
        Product.objects.filter(supplier=self.water).update(raw_name="Eau de la Ville")
        InvoiceLine.objects.filter(invoice__supplier=self.water).update(raw_name="Eau de la Ville")

    def test_merge_says_the_name_differs(self):
        report = import_archive(self.reader, MERGE).section("fournisseurs")
        self.assertIn(
            "Fournisseur « Eau de la Ville » : différent dans l'archive (nom) — gardé tel quel", report.conflicts
        )
        self.assertEqual(Supplier.objects.get(pk=self.water.pk).name, "Eau de la Ville")

    def test_replace_renames_it_and_its_charge_item_follows_without_a_word_in_its_history(self):
        import_archive(self.reader, REPLACE)
        self.assertEqual(Supplier.objects.get(pk=self.water.pk).name, "Eau Essai")
        self.assertEqual(
            list(Product.objects.filter(supplier=self.water).values_list("raw_name", flat=True)), ["Eau Essai"]
        )
        self.assertEqual(
            list(InvoiceLine.objects.filter(invoice__supplier=self.water).values_list("raw_name", flat=True)),
            ["Eau Essai"],
        )
        self.assertFalse(SupplierChange.objects.exists())

    def test_replace_keeps_a_name_another_supplier_holds_and_says_so(self):
        make_supplier(code="AUTRE_EAU", name="Eau Essai")
        report = import_archive(self.reader, REPLACE).section("fournisseurs")
        self.assertEqual(Supplier.objects.get(pk=self.water.pk).name, "Eau de la Ville")
        said = [note for note in report.notes if "non repris" in note]
        self.assertEqual(len(said), 1)
        self.assertIn(
            "Fournisseur « Eau de la Ville » : nom « Eau Essai » non repris — « Eau Essai » existe déjà", said[0]
        )


class FreshDatabaseTests(TestCase):
    """The other computer's archive onto a database holding only the seven
    suppliers the migrations install."""

    def setUp(self):
        build_suppliers()
        Supplier.objects.filter(code="UBA").update(name="U.B.A.", ticket_identifiers=["siren:900000003"])
        self.reader = export_archive({"fournisseurs"})
        self.addCleanup(self.reader.close)
        ShopItemPrice.objects.all().delete()
        Supplier.objects.exclude(code__in=SEEDS).delete()
        Supplier.objects.filter(code="UBA").update(name="UBA")
        Supplier.objects.update(ticket_header="", ticket_identifiers=[])

    def test_merge_creates_the_others_fills_the_seeds_and_says_a_renamed_one_differs(self):
        report = import_archive(self.reader, MERGE).section("fournisseurs")
        self.assertEqual(report.tallies["fournisseurs"].created, 3)
        self.assertEqual(Supplier.objects.get(code="SABBH").ticket_header, "SABBH ORIENTAL ESSAI")
        self.assertEqual(
            Supplier.objects.get(code="METRO").ticket_identifiers, ["siren:900000001", "web:metro.example"]
        )
        self.assertEqual(Supplier.objects.get(code="UBA").ticket_identifiers, ["siren:900000003"])
        self.assertEqual(Supplier.objects.get(code="UBA").name, "UBA")
        self.assertIn("Fournisseur « UBA » : différent dans l'archive (nom) — gardé tel quel", report.conflicts)
        self.assertEqual(ShopItemPrice.objects.count(), 4)

    def test_replace_renames_the_seed(self):
        import_archive(self.reader, REPLACE)
        self.assertEqual(Supplier.objects.get(code="UBA").name, "U.B.A.")
        self.assertEqual(Supplier.objects.get(code="UBA").ticket_identifiers, ["siren:900000003"])


class CodesAndNamesTests(TestCase):
    def test_the_same_supplier_under_another_code_is_found_by_name_and_its_documents_follow(self):
        lidl = make_supplier(code="LIDL", name="Lidl Essai")
        product = make_product(supplier=lidl, raw_name="EAU GAZEUSE")
        make_invoice_line(invoice=make_invoice(supplier=lidl, invoice_number="L-77"), product=product)
        reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(reader.close)
        Invoice.objects.all().delete()
        Product.objects.all().delete()
        Supplier.objects.filter(pk=lidl.pk).update(code="LIDL_2")

        run = import_archive(reader, MERGE)
        self.assertIn(
            "« Lidl Essai » : code LIDL_2 ici, LIDL dans l'archive — rapprochés par le nom",
            run.section("fournisseurs").notes,
        )
        self.assertFalse(Supplier.objects.filter(code="LIDL").exists())
        invoice = Invoice.objects.get(invoice_number="L-77")
        self.assertEqual(invoice.supplier.code, "LIDL_2")
        self.assertEqual(invoice.lines.get().product.supplier.code, "LIDL_2")

    def test_a_name_another_record_already_answers_to_is_skipped_with_its_documents(self):
        make_supplier(code="LIDL", name="Lidl Essai")
        payloads = {
            "fournisseurs": {
                "supplier_names": {"LIDL": "Lidl Express", "LIDL_BIS": "Lidl Essai"},
                "suppliers": [
                    {"code": "LIDL", "name": "Lidl Express"},
                    {"code": "LIDL_BIS", "name": "Lidl Essai"},
                ],
            },
            "factures": {
                "supplier_names": {"LIDL_BIS": "Lidl Essai"},
                "products": [],
                "invoices": [
                    {
                        "key": {
                            "supplier": "LIDL_BIS",
                            "number": "B-1",
                            "sha256": "",
                            "file_sha256": "",
                            "occurrence": 0,
                        },
                        "supplier": "LIDL_BIS",
                        "invoice_number": "B-1",
                        "lines": [],
                    }
                ],
            },
        }
        from transfer.archive import ArchiveReader

        with ArchiveReader(forge(payloads)) as reader:
            run = import_archive(reader, MERGE)
        self.assertEqual(len(run.section("fournisseurs").skipped), 1)
        self.assertIn("répond déjà au code LIDL", run.section("fournisseurs").skipped[0])
        self.assertEqual(
            run.section("factures").skipped, ["Facture LIDL_BIS n° B-1 : fournisseur inconnu « LIDL_BIS »"]
        )
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(Supplier.objects.filter(name__startswith="Lidl").count(), 1)

    def test_a_refused_code_resolves_to_nothing_whatever_its_name_or_code_say(self):
        from transfer.keys import SupplierResolver

        lidl = make_supplier(code="LIDL", name="Lidl Essai")
        resolver = SupplierResolver()
        resolver.remember({"LIDL": "Lidl Essai", "LIDL_BIS": "Lidl Essai"})
        self.assertEqual((resolver.resolve("LIDL"), resolver.resolve("LIDL_BIS")), (lidl, lidl))
        resolver.refuse("LIDL_BIS")
        resolver.refuse("LIDL")
        self.assertEqual((resolver.resolve("LIDL"), resolver.resolve("LIDL_BIS", "Lidl Essai")), (None, None))


class NatureTests(TestCase):
    """Passing a supplier to charges, or back, re-reads its documents from
    its page; an import flipping the flag alone would leave them filed the
    other way."""

    def setUp(self):
        self.shop = make_supplier(code="QUINCAILLERIE_ESSAI", name="Quincaillerie Essai", expenses_only=True)
        make_invoice(supplier=self.shop, invoice_number="Q-1")
        self.reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(self.reader.close)
        Supplier.objects.filter(pk=self.shop.pk).update(expenses_only=False)

    def test_refused_while_its_documents_stay(self):
        report = import_archive(self.reader, {"fournisseurs": REPLACE}).section("fournisseurs")
        self.assertFalse(Supplier.objects.get(pk=self.shop.pk).expenses_only)
        self.assertIn(section.CHARGES_KEPT.format(name="Quincaillerie Essai"), report.notes)

    def test_accepted_when_its_documents_are_replaced_too(self):
        import_archive(self.reader, REPLACE)
        self.assertTrue(Supplier.objects.get(pk=self.shop.pk).expenses_only)


class ParserTests(TestCase):
    def test_merge_fills_a_blank_parser_only_with_one_this_application_has(self):
        make_supplier(code="CAVE_ESSAI", name="Cave Essai", parser_key="")
        payloads = {
            "fournisseurs": {
                "suppliers": [
                    {"code": "CAVE_ESSAI", "name": "Cave Essai", "parser_key": "LECTEUR_FUTUR"},
                    {"code": "CAVE_DEUX", "name": "Cave Deux", "parser_key": "LECTEUR_FUTUR"},
                ]
            }
        }
        from transfer.archive import ArchiveReader

        with ArchiveReader(forge(payloads)) as reader:
            report = import_archive(reader, MERGE).section("fournisseurs")
        self.assertEqual(Supplier.objects.get(code="CAVE_ESSAI").parser_key, "")
        self.assertIn(
            "Fournisseur « Cave Essai » : lecteur inconnu ici (« LECTEUR_FUTUR » dans l'archive) — gardé tel quel",
            report.conflicts,
        )
        self.assertEqual(Supplier.objects.get(code="CAVE_DEUX").parser_key, "")
        self.assertIn("Fournisseur « Cave Deux » : lecteur inconnu ici (« LECTEUR_FUTUR ») — laissé vide", report.notes)

    def test_replace_never_changes_a_code_bound_suppliers_parser(self):
        payloads = {"fournisseurs": {"suppliers": [{"code": "METRO", "name": "Metro", "parser_key": ""}]}}
        from transfer.archive import ArchiveReader

        with ArchiveReader(forge(payloads)) as reader:
            report = import_archive(reader, REPLACE).section("fournisseurs")
        self.assertEqual(Supplier.objects.get(code="METRO").parser_key, "METRO")
        self.assertIn(
            f"Fournisseur « Metro » : garde son lecteur « Metro » (le lecteur générique dans l'archive) — "
            f"{section.KEPT_BOUND}",
            report.notes,
        )

    def test_the_readers_are_named_as_the_source_form_names_them(self):
        """Never by the registry's key (« CECINA »)."""
        payloads = {"fournisseurs": {"suppliers": [{"code": "METRO", "name": "Metro", "parser_key": "CECINA"}]}}
        from transfer.archive import ArchiveReader

        with ArchiveReader(forge(payloads)) as reader:
            report = import_archive(reader, REPLACE).section("fournisseurs")
        self.assertIn(
            "Fournisseur « Metro » : garde son lecteur « Metro » (« Cecina (Vignerons de Cessenon) » dans "
            f"l'archive) — {section.KEPT_BOUND}",
            report.notes,
        )


class RefusalTests(TestCase):
    def setUp(self):
        build_suppliers()
        self.reader = export_archive({"fournisseurs"})
        self.addCleanup(self.reader.close)

    def _forged(self, change):
        from transfer.archive import ArchiveReader

        reader = ArchiveReader(forge(self.reader, fournisseurs=change))
        self.addCleanup(reader.close)
        return reader

    def test_a_file_without_its_list_is_refused_whole(self):
        reader = self._forged({"fournisseurs": []})
        with self.assertRaisesMessage(ArchiveError, "fournisseurs.json n'a pas de liste « suppliers »"):
            import_archive(reader, MERGE)

    def test_identifiers_that_are_not_a_list_of_texts_skip_the_record(self):
        def change(payload):
            for record in payload["suppliers"]:
                if record["code"] == "BOULANGERIE_ESSAI":
                    record["ticket_identifiers"] = "tel:0100000001"
            return payload

        Supplier.objects.filter(code="BOULANGERIE_ESSAI").delete()
        report = import_archive(self._forged(change), MERGE).section("fournisseurs")
        self.assertIn(
            "Fournisseur « Boulangerie Essai » : « ticket_identifiers » : liste de textes attendue", report.skipped
        )
        self.assertFalse(Supplier.objects.filter(code="BOULANGERIE_ESSAI").exists())

    def test_the_firewall_state_in_a_hand_edited_archive_is_ignored(self):
        def change(payload):
            for record in payload["suppliers"]:
                if record["code"] == "METRO":
                    record["scrape_paused_until"] = None
                    record["scrape_last_block_at"] = None
            return payload

        report = import_archive(self._forged(change), REPLACE).section("fournisseurs")
        self.assertEqual(scrape_state(), PAUSE)
        self.assertIn("champ inconnu ignoré : scrape_last_block_at", report.notes)
        self.assertIn("champ inconnu ignoré : scrape_paused_until", report.notes)

    def test_a_bad_price_is_skipped_alone(self):
        def change(payload):
            for record in payload["suppliers"]:
                if record["code"] == "SABBH":
                    record["item_prices"][0]["unit_price_ttc"] = "0.705"
            return payload

        ShopItemPrice.objects.all().delete()
        report = import_archive(self._forged(change), MERGE).section("fournisseurs")
        self.assertEqual(ShopItemPrice.objects.count(), 3)
        self.assertIn(
            "Prix connu de Sabbh Oriental : « unit_price_ttc » : « 0.705 » a plus de 2 décimales", report.skipped
        )

    def test_a_record_without_a_code_or_a_name_is_skipped(self):
        def change(payload):
            payload["suppliers"].append({"name": "Sans Code"})
            payload["suppliers"].append({"code": "SANS_NOM", "name": " "})
            return payload

        report = import_archive(self._forged(change), MERGE).section("fournisseurs")
        self.assertIn("Un fournisseur de l'archive n'a pas de code.", report.skipped)
        self.assertIn("Fournisseur de code « SANS_NOM » : sans nom", report.skipped)
        self.assertFalse(Supplier.objects.filter(code="SANS_NOM").exists())


class ClearTests(TestCase):
    def setUp(self):
        self.bakery = build_suppliers()
        CounterpartyAlias.objects.create(supplier=self.bakery, name="BOULANGERIE ESSAI SARL")
        SupplierChange.objects.create(
            supplier=self.bakery,
            kind=SupplierChange.Kind.FIRST_DOCUMENT,
            summary="Premier document (essai).",
            needs_review=True,
        )

    def test_clear_keeps_the_code_bound_resets_what_they_learned_and_never_the_firewall_state(self):
        run = run_clear({"fournisseurs"}, preview=False, closed=False)
        self.assertEqual(set(Supplier.objects.values_list("code", flat=True)), BOUND)
        for supplier in Supplier.objects.all():
            self.assertEqual(
                (
                    supplier.ticket_header,
                    supplier.ticket_identifiers,
                    supplier.refused_identifiers,
                    supplier.expenses_only,
                ),
                ("", [], [], False),
                supplier.code,
            )
        self.assertEqual(scrape_state(), PAUSE)
        metro = Supplier.objects.get(code="METRO")
        self.assertEqual((metro.name, metro.parser_key, metro.is_scrapable), ("Metro", "METRO", True))
        self.assertEqual(Supplier.objects.get(code="CECINA").name, "Cecina Essai")
        self.assertFalse(ShopItemPrice.objects.exists())
        self.assertFalse(SupplierChange.objects.exists())
        self.assertEqual(_changes_to_see().count(), 0)
        self.assertFalse(CounterpartyAlias.objects.exists())
        report = run.section("fournisseurs")
        self.assertEqual(report.tallies["fournisseurs"].deleted, 2)
        self.assertEqual(run.section("banque").tallies["noms de payeurs appris"].deleted, 1)
        self.assertIn("1 changement de l'historique des fournisseurs effacé", report.notes)
        self.assertTrue(any(note.endswith("leurs identifiants appris sont remis à zéro") for note in report.notes))
        self.assertIn("Metro", report.notes[-1])
        self.assertEqual(registry.get("fournisseurs").count(), {"fournisseurs": len(BOUND), "prix connus": 0})

    def test_a_clear_preview_changes_nothing(self):
        before = db_fingerprint()
        run_clear({"fournisseurs"}, preview=True, closed=False)
        self.assertEqual(db_fingerprint(), before)

    def test_a_supplier_something_still_holds_is_kept_and_said(self):
        make_invoice_line(invoice=make_invoice(supplier=self.bakery))
        run = run_clear({"fournisseurs"}, preview=False, closed=False)
        self.assertTrue(Supplier.objects.filter(pk=self.bakery.pk).exists())
        self.assertIn("Fournisseur « Boulangerie Essai » : 1 document y est rangé", run.section("fournisseurs").kept)

    def test_a_supplier_a_pickup_holds_is_kept_with_the_true_reason(self):
        """Cleared alone (closed=False: the page clears « Consignes » with
        it), a supplier a pickup holds is kept (PROTECT) - and said so, not
        « un de ses produits sert encore »."""
        from returnables.tests.support import make_pickup

        make_pickup(supplier=self.bakery)
        run = run_clear({"fournisseurs"}, preview=False, closed=False)
        self.assertTrue(Supplier.objects.filter(pk=self.bakery.pk).exists())
        self.assertIn(
            "Fournisseur « Boulangerie Essai » : 1 reprise de consignes est à son nom", run.section("fournisseurs").kept
        )

    def test_cleared_with_returnables_the_supplier_goes(self):
        """What the page does: « Types et formats de consignes » and
        « Consignes » require the suppliers, so they are cleared first and
        hold nothing any more."""
        from returnables.tests.support import make_format, make_pickup

        make_pickup(supplier=self.bakery)
        make_format(name="Boulangerie Essai — bon", supplier=self.bakery)
        self.assertLessEqual({"consignes", "types_consignes"}, registry.closure({"fournisseurs"}, "clear"))
        run_clear({"fournisseurs", "types_consignes", "consignes"}, preview=False, closed=False)
        self.assertFalse(Supplier.objects.filter(pk=self.bakery.pk).exists())


#: The AI reading's pseudo-supplier as invoices/0002 seeded it, which every
#: archive written before 04/10/2026 carries.
OLD_AI = {"code": "OTHER", "name": "Autre (analyse IA)", "parser_key": "LLM", "is_scrapable": False}


class OlderArchiveTests(TestCase):
    """An archive written before 04/10/2026 still carries « Autre (analyse
    IA) » (OTHER, reader LLM), which invoices/0037 removed with the reading:
    it is never created again as such. Left out when nothing the run imports
    is filed under it; otherwise an ordinary supplier, its reader key empty -
    so nothing of the archive is lost. Every other record imports as
    before."""

    def old_archive(self, keys, *, filed=False, priced=False, payee=False, paid=False):
        """Export `keys` from this database holding the pseudo-supplier as
        0002 seeded it, as an older version did, then take it away with
        everything naming it, as 0037 does in the database imported into.
        Under it: a document when `filed`, a known price when `priced`, a
        payee name learnt when `payee`, and when `paid` a document a bank
        line pays."""
        ai = Supplier.objects.create(**OLD_AI)
        if filed or paid:
            invoice = make_invoice(supplier=ai, invoice_number="IA-ESSAI-1")
        if priced:
            price(ai, "0.70", "Article essai")
        if payee:
            CounterpartyAlias.objects.create(supplier=ai, name="PAYEUR IA ESSAI")
        if paid:
            pay(make_line(date(2026, 8, 4), "PAYEUR IA ESSAI", "-12.00"), invoice)
        reader = export_archive(keys)
        self.addCleanup(reader.close)
        BankTransaction.objects.all().delete()
        Invoice.objects.filter(supplier=ai).delete()
        ai.delete()
        records = {record["code"]: record for record in reader.section("fournisseurs").payload()["suppliers"]}
        self.assertEqual(records["OTHER"]["parser_key"], "LLM")
        return reader

    def assert_no_ai_supplier(self):
        self.assertFalse(Supplier.objects.filter(code="OTHER").exists())
        self.assertFalse(Supplier.objects.filter(parser_key="LLM").exists())

    def test_merged_with_nothing_filed_under_it_it_is_left_out_and_nothing_else_moves(self):
        reader = self.old_archive({"fournisseurs"})
        before = db_fingerprint()
        report = import_archive(reader, MERGE).section("fournisseurs")
        self.assert_no_ai_supplier()
        self.assertEqual(db_fingerprint(), before)
        self.assertIn(section.AI_LEFT_OUT.format(name="Autre (analyse IA)"), report.notes)
        self.assertEqual(report.tallies["fournisseurs"].unchanged, Supplier.objects.count())
        self.assertEqual((report.conflicts, report.skipped, report.kept), ([], [], []))

    def test_replaced_with_nothing_filed_under_it_it_is_left_out_and_nothing_else_moves(self):
        reader = self.old_archive({"fournisseurs"})
        before = db_fingerprint()
        report = import_archive(reader, REPLACE).section("fournisseurs")
        self.assert_no_ai_supplier()
        self.assertEqual(db_fingerprint(), before)
        self.assertIn(section.AI_LEFT_OUT.format(name="Autre (analyse IA)"), report.notes)
        self.assertFalse(report.changes)

    def test_a_document_filed_under_it_brings_it_as_an_ordinary_supplier_merged(self):
        reader = self.old_archive({"fournisseurs", "factures"}, filed=True)
        run = import_archive(reader, MERGE)
        self.assert_ordinary_with_its_document(run)

    def test_a_document_filed_under_it_brings_it_as_an_ordinary_supplier_replaced(self):
        reader = self.old_archive({"fournisseurs", "factures"}, filed=True)
        run = import_archive(reader, REPLACE)
        self.assert_ordinary_with_its_document(run)

    def assert_ordinary_with_its_document(self, run):
        report = run.section("fournisseurs")
        kept = Supplier.objects.get(code="OTHER")
        self.assertEqual((kept.name, kept.parser_key, kept.is_scrapable), ("Autre (analyse IA)", "", False))
        self.assertEqual(Invoice.objects.get(invoice_number="IA-ESSAI-1").supplier, kept)
        self.assertIn(section.AI_ORDINARY.format(name="Autre (analyse IA)"), report.notes)
        self.assertEqual(report.tallies["fournisseurs"].created, 1)
        self.assertFalse(Supplier.objects.filter(parser_key="LLM").exists())
        self.assertEqual((report.conflicts, report.skipped), ([], []))

    def test_into_an_espace_that_kept_it_it_stays_an_ordinary_supplier(self):
        """Something named it here, so 0037 kept it with its reader key
        emptied: the archive's key is no conflict, and is never written."""
        reader = self.old_archive({"fournisseurs", "factures"}, filed=True)
        kept = Supplier.objects.create(**{**OLD_AI, "parser_key": ""})
        make_invoice(supplier=kept, invoice_number="ICI-1")
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                run = import_archive(reader, {"fournisseurs": strategy, "factures": MERGE})
                report = run.section("fournisseurs")
                self.assertEqual(Supplier.objects.get(pk=kept.pk).parser_key, "")
                self.assertEqual(report.conflicts, [])
                self.assertEqual(
                    set(Invoice.objects.filter(supplier=kept).values_list("invoice_number", flat=True)),
                    {"ICI-1", "IA-ESSAI-1"},
                )

    def test_its_own_known_prices_bring_it_as_an_ordinary_supplier(self):
        """Its prices travel in its own record: left out, they were dropped
        with no line saying so - where 0037 keeps a supplier a price
        names."""
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                reader = self.old_archive({"fournisseurs"}, priced=True)
                report = import_archive(reader, strategy).section("fournisseurs")
                kept = Supplier.objects.get(code="OTHER")
                self.assertEqual((kept.name, kept.parser_key), ("Autre (analyse IA)", ""))
                self.assertEqual(
                    list(kept.item_prices.values_list("unit_price_ttc", "label")), [(Decimal("0.70"), "Article essai")]
                )
                self.assertIn(section.AI_ORDINARY.format(name="Autre (analyse IA)"), report.notes)
                self.assertEqual((report.skipped, report.conflicts), ([], []))
                kept.delete()

    def test_a_payee_name_learnt_for_it_brings_it_as_an_ordinary_supplier(self):
        reader = self.old_archive({"fournisseurs", "banque"}, payee=True)
        run = import_archive(reader, MERGE)
        kept = Supplier.objects.get(code="OTHER")
        self.assertEqual(kept.parser_key, "")
        self.assertTrue(CounterpartyAlias.objects.filter(supplier=kept, name="PAYEUR IA ESSAI").exists())
        self.assertIn(section.AI_ORDINARY.format(name="Autre (analyse IA)"), run.section("fournisseurs").notes)

    def test_a_payment_of_its_document_without_the_documents_leaves_it_out(self):
        """« Banque » names the suppliers of the documents its lines pay, and
        those documents come with « Factures » only: with « Factures » left
        out, the payment brings nothing that would need the supplier - it
        came back empty, under « des données de l'import y sont rangées »."""
        reader = self.old_archive({"fournisseurs", "factures", "banque"}, paid=True)
        self.assertIn("OTHER", reader.section("banque").payload()["supplier_names"])
        run = import_archive(reader, {"fournisseurs": MERGE, "banque": MERGE})
        self.assert_no_ai_supplier()
        self.assertIn(section.AI_LEFT_OUT.format(name="Autre (analyse IA)"), run.section("fournisseurs").notes)
        self.assertEqual(BankTransaction.objects.count(), 1)

    def test_with_its_documents_the_payment_follows_them(self):
        reader = self.old_archive({"fournisseurs", "factures", "banque"}, paid=True)
        import_archive(reader, MERGE)
        kept = Supplier.objects.get(code="OTHER")
        self.assertEqual(kept.parser_key, "")
        self.assertEqual(list(BankTransaction.objects.values_list("payments__invoice__supplier", flat=True)), [kept.pk])

    def test_replace_never_prunes_the_one_0037_kept_nor_what_names_it(self):
        """0037 kept it here, a payee name naming it: an older archive's
        record of it, with nothing filed under it, is that supplier's. Left
        out, « Remplacer » took it for one the archive does not have and
        deleted it - its payee name with it (CASCADE)."""
        reader = self.old_archive({"fournisseurs"})
        kept = Supplier.objects.create(**{**OLD_AI, "parser_key": ""})
        CounterpartyAlias.objects.create(supplier=kept, name="PAYEUR IA ESSAI")
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                report = import_archive(reader, strategy).section("fournisseurs")
                self.assertEqual(Supplier.objects.get(pk=kept.pk).parser_key, "")
                self.assertTrue(CounterpartyAlias.objects.filter(supplier=kept).exists())
                self.assertEqual(report.tallies["fournisseurs"].deleted, 0)
                self.assertEqual((report.conflicts, report.kept), ([], []))
                self.assertFalse([note for note in report.notes if "analyse IA a été retirée" in note])

    def test_no_new_shop_takes_its_code(self):
        """So OTHER here is always the supplier 0037 kept, and an archive's
        record of it is matched by its code."""
        from invoices.receipts import RETIRED_CODES

        self.assertIn(section.RETIRED_AI_CODE, RETIRED_CODES)

    def test_what_another_section_holds_is_read_never_trusted(self):
        """Read by hand off sections an archive may forge: a payee name's
        code that is no text, a table that is no table, are nothing filed
        under it - never a 500 on the preview."""
        from types import SimpleNamespace

        payloads = {
            "fournisseurs": {},
            "banque": {"aliases": [{"supplier": ["OTHER"]}, "OTHER", {"name": "X"}], "supplier_names": {"OTHER": "x"}},
            "factures": {"supplier_names": ["OTHER"]},
        }
        ctx = SimpleNamespace(
            strategies=dict.fromkeys(payloads, MERGE),
            reader=SimpleNamespace(section=lambda key: SimpleNamespace(payload=lambda: payloads[key])),
        )
        record = {**OLD_AI, "item_prices": "illisible"}
        self.assertFalse(section._filed_under_by_this_run(ctx, record))
        payloads["banque"]["aliases"].append({"supplier": "OTHER", "name": "PAYEUR"})
        self.assertTrue(section._filed_under_by_this_run(ctx, record))

    def test_its_documents_not_imported_in_this_run_leave_it_out(self):
        """« Fournisseurs » alone: what the archive filed under it is not
        brought, and a later import of the documents takes the suppliers
        with them (they require « Fournisseurs »)."""
        reader = self.old_archive({"fournisseurs", "factures"}, filed=True)
        report = import_archive(reader, {"fournisseurs": MERGE}).section("fournisseurs")
        self.assert_no_ai_supplier()
        self.assertIn(section.AI_LEFT_OUT.format(name="Autre (analyse IA)"), report.notes)

    def test_an_archive_written_since_carries_it_as_any_supplier(self):
        """Kept as an ordinary supplier by 0037, it is exported as one: no
        reader key, no note."""
        payloads = {"fournisseurs": {"suppliers": [{**OLD_AI, "parser_key": ""}]}}
        from transfer.archive import ArchiveReader

        with ArchiveReader(forge(payloads)) as reader:
            report = import_archive(reader, MERGE).section("fournisseurs")
        self.assertEqual(Supplier.objects.get(code="OTHER").parser_key, "")
        self.assertFalse([note for note in report.notes if "analyse IA a été retirée" in note])
