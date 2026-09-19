"""« Enseignes et fournisseurs » (§7.1, §10.2).

What these guard, before anything else, is Metro's firewall state: the
`scrape_*` fields are never exported, never written and never reset - by an
import, a replace or a clear - since a restore that put back an older pause
(or none) would let the next gather sign in to a site that blocks it. And a
supplier a reader or a till is keyed on is never deleted.

Every name, code and figure below is invented.
"""

from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from bank.models import CounterpartyAlias
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

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
UTC = dt_timezone.utc
SEEDS = {"METRO", "UBA", "OTHER", "FRANPRIX", "MONOPRIX", "SABBH", "WINGSENG"}
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

    def test_count_leaves_out_the_ai_pseudo_supplier(self):
        build_suppliers()
        self.assertEqual(
            registry.get("fournisseurs").count(),
            {"fournisseurs": Supplier.objects.count() - 1, "prix connus": 4},
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
        # The AI pseudo-supplier is still exported: only the number leaves it out.
        self.assertIn("OTHER", {record["code"] for record in reader.section("fournisseurs").payload()["suppliers"]})


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
            sorted(ShopItemPrice.objects.filter(supplier__code="SABBH", unit_price_ttc="0.70").values_list("label", flat=True)),
            ["Citron", "Pita"],
        )


class IdempotenceTests(TestCase):
    def setUp(self):
        build_suppliers()
        self.reader = export_archive({"fournisseurs"})
        self.addCleanup(self.reader.close)

    def test_merge_says_everything_is_unchanged(self):
        report = import_archive(self.reader, MERGE).section("fournisseurs")
        self.assertEqual(report.tallies["fournisseurs"].unchanged, Supplier.objects.count() - 1)
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
        announced: the AI pseudo-supplier was counted in the report only."""
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                report = import_archive(self.reader, strategy).section("fournisseurs")
                tally = report.tallies["fournisseurs"]
                counted = tally.created + tally.updated + tally.unchanged
                self.assertEqual(counted, registry.get("fournisseurs").count()["fournisseurs"])
                self.assertEqual(counted, self.reader.counts("fournisseurs")["fournisseurs"])
                self.assertNotIn(section.AI_ROW, report.tallies)


class AiPseudoSupplierTests(TestCase):
    """The AI pseudo-supplier is no supplier on the page, so never one of
    the report's « fournisseurs » either - but what happens to it is never
    hidden: a row of its own, which is also what the safety archive is
    chosen from (safety.sections_at_risk)."""

    def setUp(self):
        build_suppliers()
        self.reader = export_archive({"fournisseurs"})
        self.addCleanup(self.reader.close)

    def test_a_replace_changing_it_counts_it_on_its_own_row(self):
        from transfer import safety
        from transfer.archive import ArchiveReader

        def learned(payload):
            for record in payload["suppliers"]:
                if record["code"] == "OTHER":
                    record["ticket_header"] = "EN-TETE ESSAI IA"
            return payload

        with ArchiveReader(forge(self.reader, fournisseurs=learned)) as reader:
            run = import_archive(reader, REPLACE)
        report = run.section("fournisseurs")
        self.assertEqual(Supplier.objects.get(code="OTHER").ticket_header, "EN-TETE ESSAI IA")
        self.assertEqual(report.tallies[section.AI_ROW].updated, 1)
        self.assertEqual(report.tallies["fournisseurs"].updated, 0)
        self.assertEqual(report.tallies["fournisseurs"].unchanged, registry.get("fournisseurs").count()["fournisseurs"])
        self.assertIn("fournisseurs", safety.sections_at_risk(run, strategies={"fournisseurs": REPLACE}))

    def test_a_clear_resetting_it_counts_it_on_its_own_row(self):
        Supplier.objects.filter(code="OTHER").update(ticket_identifiers=["siren:900000004"])
        report = run_clear({"fournisseurs"}, preview=False, closed=False).section("fournisseurs")
        self.assertEqual(Supplier.objects.get(code="OTHER").ticket_identifiers, [])
        self.assertEqual(report.tallies[section.AI_ROW].updated, 1)

    def test_unchanged_it_is_counted_nowhere(self):
        report = import_archive(self.reader, MERGE).section("fournisseurs")
        self.assertNotIn(section.AI_ROW, report.tallies)


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
        self.assertIn(
            "Fournisseur « Fleuriste Essai » : 1 document y est rangé (Factures non remplacées)", report.kept
        )

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
        make_product(supplier=grocer, raw_name="SEL FIN 1KG", stock_type=StockType.objects.create(name="Sel", unit="KG"))
        report = import_archive(self.reader, REPLACE).section("fournisseurs")
        self.assertIn("Fournisseur « Fleuriste Essai » : 1 source récupère pour lui (Sources non remplacées)", report.kept)
        self.assertIn(
            "Fournisseur « Épicerie Essai » : 1 produit classé (Associations non remplacées)", report.kept
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
        poste = make_product(supplier=self.water, raw_name="Eau Essai", is_expense=True)
        make_invoice_line(invoice=make_invoice(supplier=self.water), product=poste, raw_name="Eau Essai")
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

    def test_replace_renames_it_and_its_poste_follows_without_a_word_in_its_history(self):
        import_archive(self.reader, REPLACE)
        self.assertEqual(Supplier.objects.get(pk=self.water.pk).name, "Eau Essai")
        self.assertEqual(list(Product.objects.filter(supplier=self.water).values_list("raw_name", flat=True)), ["Eau Essai"])
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
        self.assertIn("Fournisseur « Eau de la Ville » : nom « Eau Essai » non repris — « Eau Essai » existe déjà", said[0])


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
        self.assertEqual(Supplier.objects.get(code="METRO").ticket_identifiers, ["siren:900000001", "web:metro.example"])
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
                        "key": {"supplier": "LIDL_BIS", "number": "B-1", "sha256": "", "file_sha256": "", "occurrence": 0},
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
        self.assertEqual(run.section("factures").skipped, ["Facture LIDL_BIS n° B-1 : fournisseur inconnu « LIDL_BIS »"])
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
            f"Fournisseur « Metro » : garde son lecteur « METRO » (aucun dans l'archive) — {section.KEPT_BOUND}",
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
        self.assertIn("Prix connu de Sabbh Oriental : « unit_price_ttc » : « 0.705 » a plus de 2 décimales", report.skipped)

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
            supplier=self.bakery, kind=SupplierChange.Kind.FIRST_DOCUMENT, summary="Premier document (essai).",
            needs_review=True,
        )

    def test_clear_keeps_the_code_bound_resets_what_they_learned_and_never_the_firewall_state(self):
        run = run_clear({"fournisseurs"}, preview=False, closed=False)
        self.assertEqual(set(Supplier.objects.values_list("code", flat=True)), BOUND)
        for supplier in Supplier.objects.all():
            self.assertEqual(
                (supplier.ticket_header, supplier.ticket_identifiers, supplier.refused_identifiers, supplier.expenses_only),
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
        self.assertNotIn("Autre (analyse IA)", report.notes[-1])
        self.assertEqual(
            registry.get("fournisseurs").count(), {"fournisseurs": len(BOUND) - 1, "prix connus": 0}
        )

    def test_a_clear_preview_changes_nothing(self):
        before = db_fingerprint()
        run_clear({"fournisseurs"}, preview=True, closed=False)
        self.assertEqual(db_fingerprint(), before)

    def test_a_supplier_something_still_holds_is_kept_and_said(self):
        make_invoice_line(invoice=make_invoice(supplier=self.bakery))
        run = run_clear({"fournisseurs"}, preview=False, closed=False)
        self.assertTrue(Supplier.objects.filter(pk=self.bakery.pk).exists())
        self.assertIn("Fournisseur « Boulangerie Essai » : 1 document y est rangé", run.section("fournisseurs").kept)
