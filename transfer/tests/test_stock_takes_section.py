"""« Inventaires » (§7.9, §10.2).

A count's value was priced once and frozen: an import copies it, it never
prices it again - not even when an invoice line here now says another
price. And a count is imported whole or not at all: one FIFO slice that
cannot be re-pointed at its invoice line, or whose line is no longer the
product it was priced from, refuses the count rather than leaving a hole in
its trail or revaluing it in silence.

Fixtures are invented (the repository is public).
"""

from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from inventory.models import (
    MovementKind,
    StockMovement,
    StockTake,
    StockTakeLine,
    StockTakeLineSource,
    UnitChoices,
)
from invoices.deletion import delete_invoice
from invoices.models import Invoice
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from transfer import registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections.base import Section, Strategy
from transfer.sections.stock_takes import StockTakesSection
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    media_listing,
    round_trip,
)

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
KEY = "inventaires"
END_2025 = datetime(2025, 12, 31, 22, 0, tzinfo=dt_timezone.utc)  # 23:00 in Paris
END_2024 = datetime(2024, 12, 31, 22, 0, tzinfo=dt_timezone.utc)


def section():
    return registry.get(KEY)


def build_fixture(test):
    """Two counts priced from two invoices: a product line with two FIFO
    slices, an article line with a shortfall and no slice; a loss and a
    correction written down."""
    test.metro = make_supplier(code="METRO", name="Metro", parser_key="METRO")
    test.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
    test.limes = make_stock_type(name="Citrons verts", unit=UnitChoices.UNIT)
    test.bottle = make_product(test.metro, "VODKA X 70CL", test.vodka, unit=UnitChoices.LITRE)
    test.gin = make_product(test.metro, "GIN Y 70CL", test.vodka, unit=UnitChoices.LITRE)
    test.older = make_invoice(test.metro, invoice_number="MET-001", invoice_date=date(2025, 11, 2))
    test.newer = make_invoice(test.metro, invoice_number="MET-002", invoice_date=date(2025, 12, 5))
    test.older_lines = [
        make_invoice_line(test.older, test.gin, quantity=Decimal("2"), total_volume="1.400", total_ht="24.00"),
        make_invoice_line(test.older, test.bottle, quantity=Decimal("6"), total_volume="4.200", total_ht="57.00"),
    ]
    test.newer_line = make_invoice_line(test.newer, test.bottle, quantity=Decimal("2"), total_volume="1.400",
                                        total_ht="19.00")
    test.take = make_stock_take(END_2025, note="Inventaire 2025 (import CSV)")
    test.bottle_line = make_stock_take_line(test.take, test.bottle, counted_quantity="3", value_ht="28.50")
    StockTakeLineSource.objects.create(stock_take_line=test.bottle_line, invoice_line=test.newer_line,
                                       quantity_used=Decimal("2"), unit_cost_ht=Decimal("9.5000"))
    StockTakeLineSource.objects.create(stock_take_line=test.bottle_line, invoice_line=test.older_lines[1],
                                       quantity_used=Decimal("1"), unit_cost_ht=Decimal("9.5000"))
    test.limes_line = make_stock_take_line(test.take, stock_type=test.limes, counted_quantity="10.5",
                                           unit=UnitChoices.UNIT, value_ht="3.15", has_shortfall=True,
                                           shortfall_quantity=Decimal("10.5"))
    # The 2024 count was priced from an invoice of its own.
    test.oldest = make_invoice(test.metro, invoice_number="MET-000", invoice_date=date(2024, 11, 5))
    test.oldest_line = make_invoice_line(test.oldest, test.gin, quantity=Decimal("1"), total_volume="0.700",
                                         total_ht="12.00")
    test.first_take = make_stock_take(END_2024, note="Inventaire 2024")
    make_stock_take_line(test.first_take, test.gin, counted_quantity="1", value_ht="12.00")
    StockTakeLineSource.objects.create(stock_take_line=test.first_take.lines.get(), invoice_line=test.oldest_line,
                                       quantity_used=Decimal("1"), unit_cost_ht=Decimal("12.0000"))
    test.loss = make_movement(stock_type=test.vodka, quantity="-0.5", unit_cost_ht="13.5714", kind=MovementKind.LOSS,
                              note="Casse", occurred_on=date(2026, 1, 10))
    test.correction = make_movement(stock_type=test.limes, quantity="4", unit_cost_ht="0.30",
                                    kind=MovementKind.CORRECTION, note="Comptage d'ouverture")


def own_round_trip(test, strategy, *, between=None):
    """§10.2's round trip on this section alone: the invoices and the
    classifications stay, the counts and losses go and come back."""
    before = section().snapshot()
    reader = export_archive({KEY}, closed=False)
    try:
        run_clear({KEY}, preview=False, closed=False)
        test.assertEqual(section().count(), {"inventaires": 0, "lignes comptées": 0, "pertes et corrections": 0})
        if between is not None:
            between()
        report = import_archive(reader, {KEY: strategy})
    finally:
        reader.close()
    return before, section().snapshot(), report


def edited(reader, change) -> ArchiveReader:
    return ArchiveReader(forge(reader, inventaires=change))


class RoundTripTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_merge_brings_everything_back(self):
        before, after, report = own_round_trip(self, MERGE)

        self.assertEqual(after, before)
        self.assertEqual(len(before["stock_takes"]), 2)
        mine = report.section(KEY)
        self.assertEqual(mine.tallies["inventaires"].created, 2)
        self.assertEqual(mine.tallies["lignes comptées"].created, 3)
        self.assertEqual(mine.tallies["pertes et corrections"].created, 2)
        self.assertEqual(mine.skipped, [])

    def test_replace_brings_everything_back(self):
        before, after, _report = own_round_trip(self, REPLACE)

        self.assertEqual(after, before)

    def test_a_frozen_value_is_copied_never_priced_again(self):
        """Even when the invoice line it was priced from now says another
        price here."""
        def reprice():
            self.newer_line.total_ht = Decimal("99.00")
            self.newer_line.save()

        before, after, _report = own_round_trip(self, MERGE, between=reprice)

        self.assertEqual(after, before)
        line = StockTakeLine.objects.get(product=self.bottle)
        self.assertEqual(line.value_ht, Decimal("28.50"))
        self.assertEqual(
            sorted(line.sources.values_list("invoice_line_id", "quantity_used", "unit_cost_ht")),
            [(self.older_lines[1].pk, Decimal("1.0000"), Decimal("9.5000")),
             (self.newer_line.pk, Decimal("2.0000"), Decimal("9.5000"))],
        )

    def test_an_article_line_and_a_product_line_both_come_back(self):
        own_round_trip(self, MERGE)

        take = StockTake.objects.get(taken_at=END_2025)
        self.assertEqual(
            {(line.product_id, line.stock_type_id, line.counted_quantity, line.has_shortfall, line.shortfall_quantity)
             for line in take.lines.all()},
            {(self.bottle.pk, None, Decimal("3.0000"), False, Decimal("0.0000")),
             (None, self.limes.pk, Decimal("10.5000"), True, Decimal("10.5000"))},
        )

    def test_the_counts_keep_their_creation_date_and_the_losses_theirs(self):
        StockTake.objects.filter(pk=self.take.pk).update(created_at=datetime(2026, 1, 3, 8, 15, tzinfo=dt_timezone.utc))
        before = (sorted(StockTake.objects.values_list("taken_at", "created_at")),
                  sorted(StockMovement.objects.filter(kind=MovementKind.LOSS).values_list("created_at", flat=True)))

        own_round_trip(self, MERGE)

        self.assertEqual(
            (sorted(StockTake.objects.values_list("taken_at", "created_at")),
             sorted(StockMovement.objects.filter(kind=MovementKind.LOSS).values_list("created_at", flat=True))),
            before,
        )

    def test_the_same_loss_dated_and_undated_round_trips(self):
        """Two losses alike but for their date: one undated compares None
        with a date, and neither the snapshot nor the key may trip on it."""
        make_movement(stock_type=self.vodka, quantity="-0.5", unit_cost_ht="13.5714", kind=MovementKind.LOSS,
                      note="Casse")

        before, after, report = own_round_trip(self, MERGE)

        self.assertEqual(after, before)
        self.assertEqual(report.section(KEY).tallies["pertes et corrections"].created, 3)

    def test_two_counts_at_the_same_instant_stay_two(self):
        twin = make_stock_take(END_2025, note="Recompté")
        make_stock_take_line(twin, stock_type=self.vodka, counted_quantity="1", unit=UnitChoices.LITRE, value_ht="9")

        before, after, _report = own_round_trip(self, MERGE)

        self.assertEqual(after, before)
        self.assertEqual(StockTake.objects.filter(taken_at=END_2025).count(), 2)

    def test_the_period_picker_lists_the_imported_counts(self):
        own_round_trip(self, MERGE)

        page = self.client.get(reverse("inventory:stock_list"))

        self.assertContains(page, "Inventaire du 31/12/2025 — Inventaire 2025 (import CSV)")
        take = StockTake.objects.get(taken_at=END_2025)
        self.assertContains(page, f'<option value="{take.pk}"')

    def test_the_whole_closure_round_trips(self):
        """§10.2 as written: invoices, classifications and suppliers come
        along - once every section they need is installed."""
        needed = registry.closure({KEY}, "export")
        if not all(registry.is_registered(key) for key in needed):
            self.skipTest("les parties dont dépendent les inventaires ne sont pas encore toutes installées")
        for strategy in (MERGE, REPLACE):
            before, after = round_trip({KEY}, strategy)
            self.assertEqual(after[KEY], before[KEY])


class IdempotenceTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def check(self, strategy):
        fingerprint = db_fingerprint()
        with export_archive({KEY}, closed=False) as reader:
            mine = import_archive(reader, {KEY: strategy}).section(KEY)
        self.assertEqual((mine.conflicts, mine.skipped), ([], []))
        self.assertEqual(mine.tallies["inventaires"].unchanged, 2)
        self.assertEqual(mine.tallies["lignes comptées"].unchanged, 3)
        self.assertEqual(mine.tallies["pertes et corrections"].unchanged, 2)
        for tally in mine.tallies.values():
            self.assertEqual((tally.created, tally.updated, tally.deleted), (0, 0, 0))
        self.assertEqual(db_fingerprint(), fingerprint)

    def test_merge(self):
        self.check(MERGE)

    def test_replace(self):
        self.check(REPLACE)


class MergeAndReplaceTests(TestCase):
    """One count changed, one only here, one only in the archive - and the
    same for the losses."""

    def setUp(self):
        build_fixture(self)
        self.reader = export_archive({KEY}, closed=False)
        self.addCleanup(self.reader.close)
        # Then this database moves on:
        self.take.note = "Inventaire 2025 (corrigé)"
        self.take.save()
        self.bottle_line.value_ht = Decimal("30.00")
        self.bottle_line.save()
        self.first_take.delete()  # only in the archive now
        self.extra = make_stock_take(datetime(2026, 6, 30, 20, 0, tzinfo=dt_timezone.utc), note="Mi-année")  # only here
        make_stock_take_line(self.extra, stock_type=self.limes, counted_quantity="2", value_ht="0.60")
        self.loss.delete()  # only in the archive
        self.spill = make_movement(stock_type=self.vodka, quantity="-0.2", kind=MovementKind.LOSS, note="Renversé")

    def test_merge_keeps_what_differs_and_adds_what_is_missing(self):
        mine = import_archive(self.reader, {KEY: MERGE}).section(KEY)

        self.take.refresh_from_db()
        self.bottle_line.refresh_from_db()
        self.assertEqual((self.take.note, self.bottle_line.value_ht), ("Inventaire 2025 (corrigé)", Decimal("30.00")))
        self.assertTrue(StockTake.objects.filter(pk=self.extra.pk).exists())
        self.assertTrue(StockTake.objects.filter(taken_at=END_2024).exists())
        self.assertEqual(mine.conflicts, ["Inventaire du 31/12/2025 : différent dans l'archive (note, lignes) — gardé tel quel"])
        self.assertEqual(mine.tallies["inventaires"].created, 1)
        self.assertEqual(mine.tallies["pertes et corrections"].created, 1)
        self.assertTrue(StockMovement.objects.filter(pk=self.spill.pk).exists())
        self.assertEqual(StockMovement.objects.filter(kind=MovementKind.LOSS, note="Casse").count(), 1)

    def test_replace_makes_the_section_the_archive(self):
        mine = import_archive(self.reader, {KEY: REPLACE}).section(KEY)

        self.take.refresh_from_db()
        self.bottle_line.refresh_from_db()
        self.assertEqual((self.take.note, self.bottle_line.value_ht), ("Inventaire 2025 (import CSV)", Decimal("28.50")))
        self.assertFalse(StockTake.objects.filter(pk=self.extra.pk).exists())
        self.assertTrue(StockTake.objects.filter(taken_at=END_2024).exists())
        self.assertFalse(StockMovement.objects.filter(pk=self.spill.pk).exists())
        self.assertEqual(StockMovement.objects.filter(kind=MovementKind.LOSS, note="Casse").count(), 1)
        self.assertEqual(mine.conflicts, [])
        tallies = mine.tallies
        self.assertEqual((tallies["inventaires"].created, tallies["inventaires"].updated, tallies["inventaires"].deleted),
                         (1, 1, 1))
        self.assertEqual(tallies["lignes comptées"].updated, 1)
        self.assertEqual(tallies["lignes comptées"].unchanged, 1)
        self.assertEqual((tallies["pertes et corrections"].created, tallies["pertes et corrections"].deleted), (1, 1))

    def test_replace_puts_back_a_lines_sources(self):
        self.bottle_line.sources.filter(invoice_line=self.older_lines[1]).delete()

        import_archive(self.reader, {KEY: REPLACE})

        self.assertEqual(self.bottle_line.sources.count(), 2)

    def test_replace_removes_a_line_the_file_lacks(self):
        make_stock_take_line(self.take, self.gin, counted_quantity="1", value_ht="12.00")

        mine = import_archive(self.reader, {KEY: REPLACE}).section(KEY)

        self.assertFalse(StockTakeLine.objects.filter(stock_take=self.take, product=self.gin).exists())
        self.assertEqual(mine.tallies["lignes comptées"].deleted, 2)  # this one and the extra count's


class RefusalTests(TestCase):
    """A count is imported whole or not at all; each refusal says why."""

    def setUp(self):
        build_fixture(self)
        self.reader = export_archive({KEY}, closed=False)
        self.addCleanup(self.reader.close)
        run_clear({KEY}, preview=False, closed=False)

    def refused(self, reason, change=None, **model_changes):
        reader = edited(self.reader, change) if change else self.reader
        try:
            mine = import_archive(reader, {KEY: MERGE}).section(KEY)
        finally:
            if change:
                reader.close()
        self.assertIn(reason, mine.skipped)
        self.assertFalse(StockTake.objects.filter(taken_at=END_2025).exists())
        self.assertTrue(StockTake.objects.filter(taken_at=END_2024).exists())  # the other count is fine
        return mine

    def test_a_source_whose_line_was_renamed_refuses_its_count(self):
        self.older_lines[1].raw_name = "VODKA X 1L"
        self.older_lines[1].save()

        self.refused(
            "Inventaire du 31/12/2025 : la ligne n°2 de la facture Metro n° MET-001 n'est plus « VODKA X 70CL » : "
            "il ne sera pas revalorisé en silence"
        )
        self.assertFalse(StockTakeLine.objects.filter(stock_type=self.limes).exists())  # nothing of it created

    def test_a_renamed_line_that_only_changed_case_or_spaces_is_the_same(self):
        self.older_lines[1].raw_name = "vodka  x 70cl"
        self.older_lines[1].save()

        mine = import_archive(self.reader, {KEY: MERGE}).section(KEY)

        self.assertEqual(mine.skipped, [])

    def test_a_count_dated_off_the_calendar_refuses_it_rather_than_crashing(self):
        """A hand-edited archive is refused in French, never a 500: a
        moment on the calendar's first or last day has no local time, and
        saying « Inventaire du … » of it used to raise."""
        def off_the_calendar(data):
            data["stock_takes"][1]["taken_at"] = "9999-12-31T23:30:00+00:00"
            return data

        self.refused(
            "Inventaire sans date lisible : « taken_at » : date hors calendrier "
            "(« 9999-12-31T23:30:00+00:00 »)",
            off_the_calendar,
        )

    def test_a_source_whose_invoice_is_absent_refuses_its_count(self):
        self.newer.delete()

        self.refused("Inventaire du 31/12/2025 : facture absente : Metro n° MET-002")

    def test_a_source_past_the_invoices_last_line_refuses_its_count(self):
        def past_the_end(data):
            data["stock_takes"][1]["lines"][0]["sources"][0]["line"] = 7
            return data

        self.refused("Inventaire du 31/12/2025 : la facture Metro n° MET-002 n'a pas de ligne n°8", past_the_end)

    def test_an_unknown_product_refuses_its_count(self):
        def unknown(data):
            data["stock_takes"][1]["lines"][0]["product"] = ["METRO", "RHUM Z 70CL"]
            return data

        self.refused("Inventaire du 31/12/2025 : produit inconnu « RHUM Z 70CL » (Metro)", unknown)

    def test_an_unknown_article_refuses_its_count(self):
        def unknown(data):
            data["stock_takes"][1]["lines"][1]["article"] = "Mangues"
            return data

        self.refused("Inventaire du 31/12/2025 : article inconnu « Mangues »", unknown)

    def test_a_line_counting_both_a_product_and_an_article_refuses_its_count(self):
        def both(data):
            data["stock_takes"][1]["lines"][1]["product"] = ["METRO", "VODKA X 70CL"]
            return data

        self.refused("Inventaire du 31/12/2025 : une ligne compte à la fois un produit et un article", both)

    def test_a_line_counting_nothing_refuses_its_count(self):
        def neither(data):
            data["stock_takes"][1]["lines"][1]["article"] = None
            return data

        self.refused("Inventaire du 31/12/2025 : une ligne ne compte ni produit ni article", neither)

    def test_an_unreadable_value_refuses_its_count(self):
        def unreadable(data):
            data["stock_takes"][1]["lines"][0]["value_ht"] = "28.505"
            return data

        self.refused("Inventaire du 31/12/2025 : « value_ht » : « 28.505 » a plus de 2 décimales", unreadable)

    def test_the_same_product_counted_twice_refuses_its_count(self):
        def twice(data):
            lines = data["stock_takes"][1]["lines"]
            lines.append(dict(lines[0], sources=[]))
            return data

        self.refused("Inventaire du 31/12/2025 : « VODKA X 70CL » est compté deux fois", twice)

    def test_a_purchase_is_never_copied_as_a_loss(self):
        def purchase(data):
            data["movements"][0]["kind"] = "PURCHASE"
            return data

        with edited(self.reader, purchase) as reader:
            mine = import_archive(reader, {KEY: MERGE}).section(KEY)
        self.assertEqual(
            mine.skipped,
            [
                (
                    "Perte ou correction du 10/01/2026 : seules les pertes et les corrections se copient : "
                    "un achat vient de sa facture"
                )
            ],
        )
        self.assertEqual(StockMovement.objects.filter(kind__in=[MovementKind.LOSS, MovementKind.CORRECTION]).count(), 1)

    def test_a_list_that_is_not_a_list_refuses_the_archive(self):
        with (
            edited(self.reader, lambda data: {**data, "movements": {}}) as reader,
            self.assertRaisesMessage(ArchiveError, "inventaires.json n'a pas de liste « movements »"),
        ):
            import_archive(reader, {KEY: MERGE})


KEG, TWIN = "BIÈRE DU PONT FÛT 20L", "BIÈRE DU PONT FûT 20L"  # the accented capital only


class TwinProductsTests(TestCase):
    """Two products of one supplier whose names differ only by an accented
    capital are two products: SQLite's case-blind comparison is ASCII only,
    so the app's own matcher makes such pairs. A count of the one this
    database lacks is never put on the other by its folded name - that
    counted one product's litres as another's, with nothing said."""

    def setUp(self):
        self.brewery = make_supplier(code="BRASSERIE_TEST", name="Brasserie Test")
        self.keg = make_product(self.brewery, KEG, make_stock_type(name="Bière pression", unit=UnitChoices.LITRE),
                                unit=UnitChoices.LITRE)

    def run_archive(self, counted, *, classified=(KEG,)):
        """An archive whose associations classify `classified` and whose one
        count counts `counted`."""
        names = {"BRASSERIE_TEST": "Brasserie Test"}
        with ArchiveReader(forge({
            "associations": {
                "supplier_names": names,
                "articles": [{"name": "Bière pression", "unit": "L"}],
                "products": [
                    {"supplier": "BRASSERIE_TEST", "raw_name": raw_name, "article": "Bière pression", "unit": "L",
                     "stock_equivalent": "1.0000"}
                    for raw_name in classified
                ],
            },
            KEY: {
                "supplier_names": names,
                "stock_takes": [{
                    "taken_at": "2025-12-31T22:00:00+00:00",
                    "note": "",
                    "lines": [{"product": ["BRASSERIE_TEST", counted], "article": None, "counted_quantity": "1.0000",
                               "unit": "L", "value_ht": "80.00", "sources": []}],
                }],
                "movements": [],
            },
        })) as reader:
            return import_archive(reader, {"associations": MERGE, KEY: MERGE}).section(KEY)

    def test_a_count_of_the_twin_this_database_lacks_is_refused_not_put_on_the_other(self):
        mine = self.run_archive(TWIN)

        self.assertEqual(
            mine.skipped, ["Inventaire du 31/12/2025 : produit inconnu « BIÈRE DU PONT FûT 20L » (Brasserie Test)"]
        )
        self.assertFalse(StockTakeLine.objects.filter(product=self.keg).exists())

    def test_a_name_written_in_another_case_still_finds_the_product(self):
        """The folded name stays for what the archive does not name itself:
        the same product, spelt another way in the other database."""
        mine = self.run_archive("Bière du Pont Fût 20L", classified=())

        self.assertEqual(mine.skipped, [])
        self.assertTrue(StockTakeLine.objects.filter(product=self.keg).exists())


class _InvoicesStandIn(Section):
    """Stands for the invoices section in the prune-order test: its prune
    deletes, the app's own way, every invoice the file does not keep - which
    PROTECT refuses while a count still holds one of its lines."""

    key = "factures"

    def count(self):
        return {}

    def snapshot(self):
        return None

    def export(self, out):
        out.write({"keep": list(Invoice.objects.values_list("invoice_number", flat=True))}, {})

    def load(self, src):
        self.keep = src.payload()["keep"]

    def apply(self, ctx, report):
        pass

    def prune(self, ctx, report):
        for invoice in Invoice.objects.exclude(invoice_number__in=self.keep):
            delete_invoice(invoice)
            report.deleted("documents")

    def clear(self, ctx, report):
        pass


class PruneOrderTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_a_pruned_count_releases_its_invoice_for_the_invoices_prune(self):
        """inventaires is pruned before factures (reverse order), in the
        same transaction: the invoice a removed count was priced from can
        go in the same run."""
        with registry.swap({KEY: StockTakesSection, "factures": _InvoicesStandIn}):
            reader = export_archive({KEY, "factures"}, closed=False)
            self.addCleanup(reader.close)
            with ArchiveReader(forge(
                reader,
                inventaires=lambda data: {**data, "stock_takes": data["stock_takes"][1:]},  # the 2024 count goes
                factures={"keep": ["MET-001", "MET-002"]},  # and MET-000, which priced it, with it
            )) as edited_reader:
                report = import_archive(edited_reader, {KEY: REPLACE, "factures": REPLACE})

        self.assertFalse(StockTake.objects.filter(taken_at=END_2024).exists())
        self.assertFalse(Invoice.objects.filter(invoice_number="MET-000").exists())
        self.assertEqual(report.section("factures").tallies["documents"].deleted, 1)

    def test_with_the_real_invoices_section(self):
        if not registry.is_registered("factures"):
            self.skipTest("la partie « factures » n'est pas encore installée")
        reader = export_archive({KEY, "factures"}, closed=False)
        self.addCleanup(reader.close)
        with ArchiveReader(forge(
            reader,
            inventaires=lambda data: {**data, "stock_takes": data["stock_takes"][1:]},
            factures=lambda data: {**data, "invoices": [
                invoice for invoice in data["invoices"] if invoice["invoice_number"] != "MET-000"
            ]},
        )) as edited_reader:
            import_archive(edited_reader, {KEY: REPLACE, "factures": REPLACE})

        self.assertFalse(StockTake.objects.filter(taken_at=END_2024).exists())
        self.assertFalse(Invoice.objects.filter(invoice_number="MET-000").exists())
        self.assertTrue(StockTake.objects.filter(taken_at=END_2025).exists())


class NamedTakesTests(TestCase):
    """The invoices import before the counts, and prune after them: to know
    which counts a « Remplacer » of this section deletes, they ask
    `named_takes`, which pairs the archive's counts with the counts here the
    way `apply` does - by moment, then by rank among the counts of that
    moment. Two rules would sooner or later disagree, and a document was
    kept for a count the same run deleted (review, 19/09)."""

    def setUp(self):
        build_fixture(self)
        self.twin = make_stock_take(END_2025, note="Recompté")
        make_stock_take_line(self.twin, stock_type=self.vodka, counted_quantity="1", unit=UnitChoices.LITRE, value_ht="9")

    def test_it_names_exactly_the_counts_a_replace_keeps(self):
        from transfer.sections.stock_takes import named_takes

        reader = export_archive({KEY}, closed=False)
        self.addCleanup(reader.close)
        make_stock_take(datetime(2026, 9, 19, 10, 0, tzinfo=dt_timezone.utc), note="Après l'export")

        def change(data):
            first, second, twin = data["stock_takes"]
            # The twin's record is unreadable twice over: it names nothing.
            return {**data, "stock_takes": [first, second, "illisible", {**twin, "taken_at": "pas une date"}]}

        forged = edited(reader, change)
        self.addCleanup(forged.close)
        named = named_takes(forged.section(KEY).payload()["stock_takes"])
        self.assertEqual(named, {self.first_take.pk, self.take.pk})

        import_archive(forged, {KEY: REPLACE})
        self.assertEqual(set(StockTake.objects.values_list("pk", flat=True)), named)

    def test_a_list_that_is_not_one_names_nothing(self):
        from transfer.sections.stock_takes import named_takes

        self.assertEqual(named_takes(None), set())


class PreviewAndClearTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_the_preview_changes_nothing_and_says_what_the_run_does(self):
        with export_archive({KEY}, closed=False) as reader, edited(
            reader, lambda data: {**data, "stock_takes": data["stock_takes"][1:], "movements": data["movements"][:1]}
        ) as changed:
            fingerprint, media = db_fingerprint(), media_listing()
            preview = import_archive(changed, {KEY: REPLACE}, preview=True)
            self.assertEqual((db_fingerprint(), media_listing()), (fingerprint, media))
            done = import_archive(changed, {KEY: REPLACE})

        self.assertEqual(preview.outcome(), done.outcome())
        self.assertEqual(done.section(KEY).tallies["inventaires"].deleted, 1)

    def test_clear_leaves_the_purchases(self):
        purchase = make_movement(stock_type=self.vodka, quantity="4.2", invoice_line=self.older_lines[1])

        report = run_clear({KEY}, preview=False, closed=False)

        self.assertEqual(section().count(), {"inventaires": 0, "lignes comptées": 0, "pertes et corrections": 0})
        self.assertFalse(StockTakeLineSource.objects.exists())
        self.assertTrue(StockMovement.objects.filter(pk=purchase.pk).exists())
        tallies = report.section(KEY).tallies
        self.assertEqual((tallies["inventaires"].deleted, tallies["lignes comptées"].deleted,
                          tallies["pertes et corrections"].deleted), (2, 3, 2))

    def test_after_a_clear_the_invoices_can_go(self):
        run_clear({KEY}, preview=False, closed=False)

        delete_invoice(self.older)  # no count holds its lines any more

        self.assertFalse(Invoice.objects.filter(pk=self.older.pk).exists())


class CountAndExportTests(TestCase):
    def setUp(self):
        build_fixture(self)

    def test_count(self):
        self.assertEqual(StockTakesSection().count(), {"inventaires": 2, "lignes comptées": 3, "pertes et corrections": 2})

    def test_the_export_names_invoices_and_lines_by_key_only(self):
        with export_archive({KEY}, closed=False) as reader:
            data = reader.section(KEY).payload()

        self.assertEqual(data["supplier_names"], {"METRO": "Metro"})
        self.assertEqual([take["taken_at"] for take in data["stock_takes"]],
                         ["2024-12-31T22:00:00+00:00", "2025-12-31T22:00:00+00:00"])
        bottle = data["stock_takes"][1]["lines"][0]
        self.assertEqual(bottle["product"], ["METRO", "VODKA X 70CL"])
        self.assertIsNone(bottle["article"])
        self.assertEqual((bottle["value_ht"], bottle["counted_quantity"]), ("28.50", "3.0000"))
        self.assertEqual(
            [(source["invoice"]["number"], source["line"], source["raw_name"], source["quantity_used"])
             for source in bottle["sources"]],
            [("MET-002", 0, "VODKA X 70CL", "2.0000"), ("MET-001", 1, "VODKA X 70CL", "1.0000")],
        )
        self.assertEqual(set(bottle["sources"][0]["invoice"]),
                         {"supplier", "number", "sha256", "file_sha256", "occurrence"})
        self.assertEqual(data["movements"][0]["article"], "Vodka")
        text = str(data)
        for pk in (self.older_lines[1].pk, self.newer_line.pk):
            self.assertNotIn(f"'invoice_line': {pk}", text)
        for take in data["stock_takes"]:
            self.assertNotIn("id", take)
