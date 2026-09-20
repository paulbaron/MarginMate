"""Every section at once (§10.4): the owner's case, a whole database moved.

Each section's own tests round-trip that section, the others standing in or
left where they are. Here all nine are real and move together - export
everything, clear everything, import everything - so what crosses sections
has to survive the trip too: an invoice line a stock take was priced from,
a bank payment to a ticket known only by its file (the second of two
byte-identical ones), a supplier's code named by six sections' records, and
the purchase movements and the till's sales per recipe rebuilt from what
came back.

Every name, amount and file below is invented.
"""

import shutil
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from bank.models import BankTransaction, CounterpartyAlias, IgnoreRule, InvoicePayment
from inventory.models import (
    MovementKind,
    Product,
    StockMovement,
    StockTakeLineSource,
    StockType,
    UnitChoices,
)
from inventory.services import update_product_conversion
from invoices.models import ShopItemPrice, Supplier
from tests.factories import make_movement, make_stock_take, make_stock_take_line
from transfer import registry, safety
from transfer.archive import ArchiveReader
from transfer.registry import INFO
from transfer.runner import run_clear
from transfer.sections.base import Strategy
from transfer.sections.suppliers import code_bound
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    import_archive,
    media_listing,
    round_trip,
)
from transfer.tests.test_bank_section import make_line, make_rule, pay
from transfer.tests.test_invoices_section import (
    MediaMixin,
    build_invoices,
    media_names,
    named_files,
    sha,
)
from transfer.tests.test_sales_section import build_sales
from transfer.tests.test_sources_section import as_restored, email_source, portal_source
from transfer.tests.test_views import shown_preview

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
ALL = set(INFO)
UTC = dt_timezone.utc
D = Decimal
PAUSED_UNTIL = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
BLOCKED_AT = datetime(2026, 9, 18, 21, 40, 12, tzinfo=UTC)


def build_everything() -> dict:
    """One bar's worth of every section, each pointing at the others the way
    the real data does."""
    built = build_invoices()  # documents, lines and files (lane A's fixture)
    # The page keeps a product in its article's unit (inventory.views.
    # assign_product), and the associations section refuses one that is not:
    # lane A's fixture counts the vodka in bottles against an article in litres.
    update_product_conversion(
        Product.objects.get(raw_name="VODKA ESSAI 70CL"), unit=UnitChoices.LITRE, stock_equivalent=D("0.7")
    )
    build_sales()  # recipes, till days and links, sales typed in, sale documents (lane C's)

    # Suppliers: Metro paused by its firewall, known prices, what names a shop.
    Supplier.objects.filter(code="METRO").update(
        scrape_paused_until=PAUSED_UNTIL, scrape_last_block_at=BLOCKED_AT, scrape_pause_reason="Refus du pare-feu (essai)"
    )
    sabbh = Supplier.objects.get(code="SABBH")
    ShopItemPrice.objects.create(supplier=sabbh, unit_price_ttc=D("0.70"), label="Pita essai")
    ShopItemPrice.objects.create(supplier=sabbh, unit_price_ttc=D("0.70"), label="Pita essai", valid_from=date(2026, 8, 1))
    leroy = Supplier.objects.get(code="LEROY_ESSAI")
    Supplier.objects.filter(pk=leroy.pk).update(
        ticket_header="LEROY ESSAI", ticket_identifiers=["web:leroy-essai.example"], refused_identifiers=["tel:0100000000"]
    )
    water = Supplier.objects.get(code="EAU_ESSAI")

    # Sources: a mailbox search and a customer portal.
    email_source(leroy, "Leroy Essai - Factures", r"(?i)factures@leroy-essai\.example", subject="(?i)facture")
    portal_source(water, "Eau Essai", "EAU_ESSAI")

    # A stock take priced from a numbered invoice's line and from the second
    # of the two identical tickets, an article counted short, a loss.
    bottle_line = built["invoice"].lines.order_by("id").first()
    twin_line = built["twins"][1].lines.get()
    take = make_stock_take(datetime(2026, 8, 31, 22, 0, tzinfo=UTC), note="Inventaire d'essai")
    counted = make_stock_take_line(take, bottle_line.product, counted_quantity="3", value_ht="28.50")
    StockTakeLineSource.objects.create(
        stock_take_line=counted, invoice_line=bottle_line, quantity_used=D("3"), unit_cost_ht=D("9.5000")
    )
    bread = make_stock_take_line(take, twin_line.product, counted_quantity="2", value_ht="8.00")
    StockTakeLineSource.objects.create(
        stock_take_line=bread, invoice_line=twin_line, quantity_used=D("2"), unit_cost_ht=D("4.0000")
    )
    make_stock_take_line(
        take, stock_type=StockType.objects.get(name="Gin"), counted_quantity="0.7", unit=UnitChoices.LITRE,
        value_ht="0", has_shortfall=True, shortfall_quantity=D("0.7"),
    )
    make_movement(
        stock_type=StockType.objects.get(name="Vodka essai"), quantity="-0.5", unit_cost_ht="13.5714",
        kind=MovementKind.LOSS, note="Casse (essai)", occurred_on=date(2026, 8, 20),
    )

    # The bank: a payment by number, one by the twin's file, one by the
    # receipt's stored fingerprint, a « pas de facture », a payee name learned.
    pay(make_line(date(2026, 8, 3), "METRO ESSAI", "-50.70", settled=True), built["invoice"])
    pay(make_line(date(2026, 2, 27), "MONOPRIX ESSAI", "-8.44"), built["twins"][1], InvoicePayment.Method.AUTO)
    pay(make_line(date(2026, 9, 3), "MONOPRIX ESSAI", "-117.09", settled=True), built["receipt"])
    make_line(date(2026, 8, 5), "URSSAF", "-450.00", kind=BankTransaction.Kind.DEBIT, settled=True, no_invoice=True)
    CounterpartyAlias.objects.create(supplier=leroy, name="LEROY ESS")
    make_rule("URSSAF", "Cotisations")

    # Every product created on a day of its own: a round trip that forgot
    # to restore the moment would show the import's instead.
    for minute, pk in enumerate(Product.objects.order_by("pk").values_list("pk", flat=True)):
        Product.objects.filter(pk=pk).update(created_at=datetime(2026, 3, 1 + minute, 9, minute, 30, 125000, tzinfo=UTC))
    return built


def product_moments() -> dict:
    return {
        (code, raw_name): moment
        for code, raw_name, moment in Product.objects.values_list("supplier__code", "raw_name", "created_at")
    }


def snapshots() -> dict:
    return {key: registry.get(key).snapshot() for key in registry.ordered(ALL)}


def metro_pause() -> tuple:
    return tuple(
        Supplier.objects.filter(code="METRO").values_list(
            "scrape_last_login_at", "scrape_last_block_at", "scrape_paused_until", "scrape_pause_reason"
        ).get()
    )


class FixtureTests(MediaMixin, TestCase):
    def test_every_section_holds_something(self):
        build_everything()
        for key in registry.ordered(ALL):
            with self.subTest(section=key):
                self.assertTrue(any(registry.get(key).count().values()), key)


class WholeArchiveTests(MediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_everything()
        self.files = {name: sha(name) for name in named_files()}
        self.code_bound = {supplier.code for supplier in Supplier.objects.all() if code_bound(supplier)}
        self.pause = metro_pause()

    def assertSameSnapshots(self, after, before):
        self.assertEqual(list(after), list(before))
        for key in before:
            with self.subTest(section=key):
                self.assertEqual(after[key], before[key])

    def _everything_cleared(self):
        for key in registry.ordered(ALL - {"fournisseurs"}):
            counts = registry.get(key).count()
            self.assertFalse(any(counts.values()), (key, counts))
        self.assertEqual(set(Supplier.objects.values_list("code", flat=True)), self.code_bound)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(media_names() & set(self.files))
        self.assertEqual(metro_pause(), self.pause)

    def _check(self, strategy):
        before, after = round_trip(ALL, strategy, after_clear=self._everything_cleared)
        # A portal comes back inactive: an import never switches one on
        # (sections/sources.py) - where it signs in is the owner's to check.
        before["sources"] = as_restored(before["sources"])
        self.assertSameSnapshots(after, before)
        # Byte for byte, under the same names.
        self.assertEqual({name: sha(name) for name in named_files()}, self.files)
        # Never exported, never reset.
        self.assertEqual(metro_pause(), self.pause)

    def test_export_clear_and_replace_gives_everything_back(self):
        self._check(REPLACE)

    def test_export_clear_and_merge_gives_everything_back(self):
        self._check(MERGE)

    def test_the_derived_data_is_rebuilt_equal(self):
        """Said on its own, since it is what the snapshots are made from:
        three purchase movements (a refund among them), the loss, and the
        till's sales per recipe."""
        movements = sorted(
            StockMovement.objects.values_list("stock_type__name", "kind", "quantity", "unit_cost_ht", "invoice_line__raw_name")
        )
        self._check(REPLACE)
        self.assertEqual(
            sorted(
                StockMovement.objects.values_list(
                    "stock_type__name", "kind", "quantity", "unit_cost_ht", "invoice_line__raw_name"
                )
            ),
            movements,
        )
        self.assertEqual(len(movements), 4)

    def test_products_keep_the_moment_they_were_created(self):
        """Neither section's snapshot holds it (§6.4 never compares it), and
        both create products: the associations the classified ones, the
        invoices the others. On the real data all 794 came back dated from
        the import."""
        moments = product_moments()
        self.assertEqual(len(moments), 5)
        self._check(REPLACE)
        self.assertEqual(product_moments(), moments)

    def test_clearing_everything_recounts_only_what_is_left(self):
        """« Recalculé » says what was rebuilt: the links' clear asks for
        every recipe's till sales, and the recipes' clear deleted them all -
        read as six, the report announced sales rebuilt for recipes gone."""
        with self.captureOnCommitCallbacks(execute=True):
            run = run_clear(ALL, preview=False)
        self.assertEqual(
            run.rebuilt, {"mouvements de stock": 0, "statuts de factures": 0, "ventes par recette": 0, "produits caisse": 0}
        )

    def _own_export(self, strategy):
        reader = export_archive(ALL)
        self.addCleanup(reader.close)
        before, files = db_fingerprint(), media_listing()
        run = import_archive(reader, strategy)
        for report in run.sections:
            with self.subTest(section=report.key):
                self.assertFalse(report.changes, report.to_json())
                self.assertEqual((report.conflicts, report.skipped, report.kept), ([], [], []))
                self.assertTrue(report.tallies, report.key)
        self.assertEqual(run.affected(), set())
        self.assertEqual(set(run.rebuilt.values()), {0})
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)

    def test_merging_its_own_export_changes_nothing(self):
        self._own_export(MERGE)

    def test_replacing_with_its_own_export_changes_nothing(self):
        self._own_export(REPLACE)

    def test_the_preview_of_the_whole_import_is_the_import(self):
        reader = export_archive(ALL)
        self.addCleanup(reader.close)
        with self.captureOnCommitCallbacks(execute=True):
            run_clear(ALL, preview=False)
        cleared, files = db_fingerprint(), media_listing()
        preview = import_archive(reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), cleared)
        self.assertEqual(media_listing(), files)
        done = import_archive(reader, REPLACE)
        self.assertEqual(
            [report.to_json() for report in preview.sections], [report.to_json() for report in done.sections]
        )
        self.assertEqual(preview.rebuilt, done.rebuilt)
        self.assertEqual(done.rebuilt["mouvements de stock"], 3)


def empty_backups():
    shutil.rmtree(settings.DATA_BACKUP_DIR, ignore_errors=True)


class ClearFromThePageTests(MediaMixin, TransactionTestCase):
    """« Effacer Enseignes et fournisseurs », typed EFFACER, for real: the
    transaction commits and the files go, as on the owner's computer."""

    # The suppliers the migrations seed must still be there for the tests
    # that run after this one.
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        empty_backups()
        self.addCleanup(empty_backups)
        build_everything()
        # Its own backup (backups/, where only safety.before writes) puts
        # the portals back as they were - as_restored is for an archive from
        # anywhere else. Restored inactive, the owner's five portals were
        # left out of the next gather with nothing saying why (20/09).
        self.before = snapshots()
        self.files = {name: sha(name) for name in named_files()}
        self.code_bound = {supplier.code for supplier in Supplier.objects.all() if code_bound(supplier)}
        self.pause = metro_pause()
        self.bank = (BankTransaction.objects.count(), IgnoreRule.objects.count())

    def test_clearing_the_suppliers_clears_all_but_the_bank_and_the_backup_brings_it_back(self):
        url = reverse("transfer:data_clear")
        cleared = registry.closure({"fournisseurs"}, "clear")
        self.assertEqual(cleared, ALL - {"banque"})
        posted = {"sections": sorted(cleared)}
        self.client.post(url, {**posted, "action": "previsualiser"})
        # The confirm names the preview its page shows (views.SHOWN_PREVIEW).
        apercu = shown_preview(self.client.get(url))
        response = self.client.post(url, {**posted, "action": "effacer", "confirmation": " effacer ", "apercu": apercu})
        self.assertRedirects(response, url + "?rapport=1", fetch_redirect_response=False)

        # Everything but the bank is empty; the bank lost its payments and
        # its payee names, not its lines or its rules.
        for key in registry.ordered(cleared - {"fournisseurs"}):
            counts = registry.get(key).count()
            with self.subTest(section=key):
                self.assertFalse(any(counts.values()), counts)
        self.assertEqual((BankTransaction.objects.count(), IgnoreRule.objects.count()), self.bank)
        self.assertFalse(InvoicePayment.objects.exists())
        self.assertFalse(CounterpartyAlias.objects.exists())
        # The suppliers with a reader or a till of their own stay, Metro
        # still paused.
        self.assertEqual(set(Supplier.objects.values_list("code", flat=True)), self.code_bound)
        self.assertEqual(metro_pause(), self.pause)
        self.assertFalse(media_names() & set(self.files))

        # Both backups, and the archive holds the bank too: it lost rows.
        backups = safety.list_backups()
        self.assertEqual(sorted(backup.kind for backup in backups), ["sqlite", "zip"])
        archive = next(backup for backup in backups if backup.kind == "zip")
        with ArchiveReader(archive.path) as reader:
            self.assertEqual(reader.sections, ALL)
            import_archive(reader, REPLACE)
        after = snapshots()
        for key in self.before:
            with self.subTest(section=key):
                self.assertEqual(after[key], self.before[key])
        self.assertEqual({name: sha(name) for name in named_files()}, self.files)
        self.assertEqual(metro_pause(), self.pause)
