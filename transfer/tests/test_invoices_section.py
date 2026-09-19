"""« Factures et tickets » (§7.6, §10.2).

The fixture holds what the real documents hold at their edges: a Metro
invoice with a refund and a weighed line, two byte-identical Monoprix
tickets with no number and no fingerprint (real invoices 262 and 263 -
they must come back as two), a photographed receipt with its preview, its
checks and a VAT table with a negative row, a UBA invoice with no line and
its stored status, a supplier of charges, a stand-in number, and a preview
image stored as '' beside one stored as NULL.

What they guard: a document comes back exactly - fields, lines in their
order, files byte for byte, the purchase movements rebuilt equal - nothing
is read again or learned (no « premier document »), and a stock take's
trail is never broken.

Every name, amount and file below is invented.
"""

import hashlib
import os
import tracemalloc
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase

from bank.models import BankTransaction, InvoicePayment
from inventory import services
from inventory.models import (
    MovementKind,
    Product,
    StockMovement,
    StockTakeLine,
    StockTakeLineSource,
    StockType,
    UnitChoices,
)
from invoices.models import Invoice, InvoiceLine, Supplier, SupplierChange
from invoices.workspace import _changes_to_see
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_take,
    make_supplier,
)
from transfer import archive, keys, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections import invoices as section
from transfer.sections import stock_takes
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
D = Decimal
COMPLETE, NEEDS_REVIEW = Invoice.Status.COMPLETE, Invoice.Status.NEEDS_REVIEW
TWIN = b"%PDF-1.4 ticket Monoprix essai 8,44 EUR - deux fois le meme fichier"


def store(name: str, data: bytes) -> str:
    """Exactly this name: what a previous test left under it goes first."""
    if default_storage.exists(name):
        default_storage.delete(name)
    return default_storage.save(name, ContentFile(data))


def media_names() -> set[str]:
    root = Path(settings.MEDIA_ROOT)
    return {
        path.relative_to(root).as_posix()
        for folder in ("invoices", "receipts")
        for path in (root / folder).rglob("*")
        if path.is_file()
    }


def named_files() -> set[str]:
    """The files the documents name - not whatever else other tests left in
    the shared temp media folder."""
    return {name for pair in Invoice.objects.values_list("source_file", "preview_image") for name in pair if name}


def sha(name: str) -> str:
    return hashlib.sha256(default_storage.open(name, "rb").read()).hexdigest()


def unnumbered(supplier, **fields) -> Invoice:
    """A ticket with no number (make_invoice would give it one)."""
    return Invoice.objects.create(supplier=supplier, invoice_number="", **fields)


def imported(invoice, minute: int) -> Invoice:
    Invoice.objects.filter(pk=invoice.pk).update(imported_at=datetime(2026, 9, 10, 8, minute, 30, 125000, tzinfo=UTC))
    invoice.refresh_from_db()
    return invoice


def payment(invoice, n=1) -> InvoicePayment:
    line = BankTransaction.objects.create(
        account="****0042",
        operation_date=date(2026, 8, 3),
        label=f"PAIEMENT ESSAI {n}",
        amount=D("-12.30"),
        fingerprint=hashlib.sha256(f"essai-{n}".encode()).hexdigest(),
        settled_by_hand=True,
    )
    return InvoicePayment.objects.create(transaction=line, invoice=invoice, method=InvoicePayment.Method.MANUAL)


class MediaMixin:
    """Every file a test stores or imports is removed after it: MEDIA_ROOT is
    one temp folder for the whole run."""

    def setUp(self):
        super().setUp()
        before = media_names()

        def cleanup():
            for name in media_names() - before:
                default_storage.delete(name)

        self.addCleanup(cleanup)


def build_invoices() -> dict:
    metro = Supplier.objects.get(code="METRO")
    monoprix = Supplier.objects.get(code="MONOPRIX")
    uba = Supplier.objects.get(code="UBA")
    water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
    leroy = make_supplier(code="LEROY_ESSAI", name="Leroy Essai")

    vodka = StockType.objects.create(name="Vodka essai", unit=UnitChoices.LITRE)
    ham = StockType.objects.create(name="Jambon essai", unit=UnitChoices.KILOGRAM)
    bottle = make_product(metro, "VODKA ESSAI 70CL", stock_type=vodka, unit=UnitChoices.UNIT, stock_equivalent="0.7")
    weighed = make_product(metro, "JAMBON ESSAI AU POIDS", stock_type=ham, unit=UnitChoices.KILOGRAM)
    napkins = make_product(metro, "SERVIETTES ESSAI", ean="3000000000017")

    invoice = imported(
        make_invoice(
            metro,
            invoice_number="M-0001",
            invoice_date=date(2026, 8, 1),
            source_file=store("invoices/2026/09/metro_essai_0001.pdf", b"%PDF-1.4 metro essai 0001"),
            source_text="METRO ESSAI\nFACTURE M-0001",
            status=NEEDS_REVIEW,
            reconciliation_adjustment=D("0.12"),
        ),
        1,
    )
    lines = [
        make_invoice_line(invoice, bottle, quantity=D("6"), total_ht="57.00", unit_cost_ht="9.5000", taxes=D("1.20"), vat_rate=D("0.2000")),
        make_invoice_line(invoice, bottle, quantity=D("-1"), total_ht="-9.50", unit_cost_ht="9.5000", vat_rate=D("0.2000")),
        make_invoice_line(
            invoice, weighed, quantity=D("0.350"), total_volume="0.350", total_ht="4.20", unit_cost_ht="12.0000",
            vat_rate=D("0.0550"),
        ),
        make_invoice_line(invoice, napkins, quantity=D("2"), total_ht="3.00", unit_cost_ht="1.5000", colisage=2, category="Hygiène"),
    ]
    for line in lines:
        services.create_stock_movement_for_line(line)

    bread = make_product(monoprix, "PAIN ESSAI")
    twins = []
    for minute, name in ((2, "invoices/2026/09/Monoprix_essai.pdf"), (3, "invoices/2026/09/Monoprix_essai_Ab12Cd3.pdf")):
        twin = imported(
            unnumbered(monoprix, invoice_date=date(2025, 2, 26), source_file=store(name, TWIN), status=COMPLETE),
            minute,
        )
        make_invoice_line(twin, bread, quantity=D("2"), total_ht="8.00", unit_cost_ht="4.0000", vat_rate=D("0.0550"))
        twins.append(twin)
    # The ORM writes '' for an empty file field; the real data also holds NULL.
    Invoice.objects.filter(pk=twins[0].pk).update(preview_image=None)
    Invoice.objects.filter(pk=twins[1].pk).update(preview_image="")

    photo = b"\xff\xd8\xff\xe0 ticket essai"
    receipt = imported(
        unnumbered(
            monoprix,
            invoice_date=date(2026, 9, 2),
            source_file=store("invoices/2026/09/ticket_essai.jpg", photo),
            preview_image=store("receipts/2026/09/ticket_essai_apercu.jpg", b"\xff\xd8\xff\xe0 apercu essai"),
            source_sha256=hashlib.sha256(photo).hexdigest(),
            ocr_text="MONOPRIX ESSAI\nPAIN ESSAI 1,20\nTOTAL 117,09",
            ocr_confidence=D("0.87"),
            parse_checks=[{"label": "Somme des lignes = total", "passed": True, "detail": "117,09 €"}],
            vat_breakdown=[["0.055", "-2.76", "-0.15"], ["0.2", "100.00", "20.00"]],
            vat_table_typed=True,
            reviewed_at=datetime(2026, 9, 18, 21, 4, 5, 250000, tzinfo=UTC),
            printed_total_ttc=D("117.09"),
            status=COMPLETE,
        ),
        4,
    )
    make_invoice_line(
        receipt, bread, quantity=D("1"), total_ht="1.14", unit_cost_ht="1.1400", vat_rate=D("0.0550"),
        printed_ttc=D("1.20"), read_as="PAIN ESSA1", discount_ttc=D("0.10"),
    )

    empty = imported(
        make_invoice(
            uba,
            invoice_number="U-0001",
            source_file=store("invoices/2026/09/uba_essai_0001.pdf", b"%PDF-1.4 uba essai"),
            status=NEEDS_REVIEW,
            error_message="Le parseur UBA n'a trouvé aucune ligne dans ce document (essai).",
        ),
        5,
    )

    poste = make_product(water, "Eau Essai", is_expense=True)
    bill = imported(
        make_invoice(water, invoice_number="E-2026-07", source_file=store("invoices/2026/09/eau_essai.pdf", b"%PDF-1.4 eau"), status=COMPLETE),
        6,
    )
    make_invoice_line(bill, poste, quantity=D("1"), total_ht="42.10", unit_cost_ht="42.1000", vat_rate=D("0.0550"), raw_name="Eau Essai")

    stand_in = imported(
        make_invoice(
            leroy,
            invoice_number="20260902-9.68",
            invoice_date=date(2026, 9, 2),
            source_file=store("invoices/2026/09/leroy_essai.pdf", b"%PDF-1.4 leroy essai"),
            status=COMPLETE,
        ),
        7,
    )
    return {"invoice": invoice, "twins": twins, "receipt": receipt, "empty": empty, "bill": bill, "stand_in": stand_in}


class GuardTests(MediaMixin, TestCase):
    def test_every_field_is_exported_or_said_why(self):
        exported = set(section.INVOICE_FIELDS) | set(section.FILE_FIELDS)
        names = {model_field.name for model_field in Invoice._meta.concrete_fields}
        self.assertEqual(names - exported, set(section.NOT_EXPORTED) & names)
        self.assertIn("vat_table_typed", section.INVOICE_FIELDS)
        names = {model_field.name for model_field in InvoiceLine._meta.concrete_fields}
        self.assertEqual(names - set(section.LINE_FIELDS), set(section.NOT_EXPORTED) & names)
        self.assertEqual(set(section.NOT_EXPORTED), {"id", "supplier", "product", "invoice"})

    def test_count(self):
        build_invoices()
        section._SIZES["token"] = None
        count = registry.get("factures").count()
        self.assertEqual(
            {name: count[name] for name in ("documents", "lignes", "fichiers")},
            {"documents": 7, "lignes": 8, "fichiers": 8},
        )
        self.assertEqual(count["Mo de fichiers"], 0)

    def test_an_empty_preview_and_a_null_one_both_export_as_null(self):
        build_invoices()
        self.assertTrue(Invoice.objects.filter(preview_image="").exists())
        self.assertTrue(Invoice.objects.filter(preview_image__isnull=True).exists())
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        records = reader.section("factures").payload()["invoices"]
        receipts = [record for record in records if record["preview_image"] is not None]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(sum(1 for record in records if record["preview_image"] is None), 6)

    def test_the_archive_names_no_primary_key(self):
        build_invoices()
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)

        def names(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from names(item)
            elif isinstance(value, list):
                for item in value:
                    yield from names(item)

        found = set(names(reader.section("factures").payload()))
        self.assertFalse(found & {"id", "pk", "invoice_id", "product_id", "supplier_id"}, found)

    def test_a_file_missing_from_the_disk_is_exported_as_missing_and_said(self):
        metro = Supplier.objects.get(code="METRO")
        make_invoice(metro, invoice_number="M-ABSENT", source_file="invoices/2026/09/absent_essai.pdf")
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        record = reader.section("factures").payload()["invoices"][0]
        self.assertEqual(record["source_file"], {"name": "invoices/2026/09/absent_essai.pdf", "missing": True})
        self.assertIn("fichier absent du disque : invoices/2026/09/absent_essai.pdf", reader.manifest["notes"])

        Invoice.objects.all().delete()
        report = import_archive(reader, MERGE).section("factures")
        invoice = Invoice.objects.get(invoice_number="M-ABSENT")
        self.assertFalse(invoice.source_file)
        self.assertIn(
            "Facture Metro n° M-ABSENT : fichier absent du disque à l'export (« invoices/2026/09/absent_essai.pdf ») "
            "— importée sans",
            report.notes,
        )


class RoundTripTests(MediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.built = build_invoices()
        self.files = {name: sha(name) for name in named_files()}

    def _after_clear(self):
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(InvoiceLine.objects.exists())
        self.assertFalse(StockMovement.objects.filter(kind=MovementKind.PURCHASE).exists())
        self.assertFalse(Product.objects.filter(raw_name__in=["SERVIETTES ESSAI", "PAIN ESSAI", "Eau Essai"]).exists())
        self.assertTrue(Product.objects.filter(raw_name="VODKA ESSAI 70CL").exists())  # classified: the associations'
        self.assertFalse(media_names() & set(self.files))

    def _check(self, strategy):
        before, after = round_trip({"factures"}, strategy, after_clear=self._after_clear)
        self.assertEqual(after, before)
        # Byte for byte, under the same names.
        self.assertEqual({name: sha(name) for name in self.files}, self.files)
        self.assertEqual(Invoice.objects.count(), 7)
        self.assertEqual(StockMovement.objects.filter(kind=MovementKind.PURCHASE).count(), 3)
        return after

    def test_merge(self):
        self._check(MERGE)

    def test_replace(self):
        self._check(REPLACE)

    def test_the_two_identical_tickets_come_back_as_two(self):
        after = self._check(MERGE)
        twins = [document for document in after["factures"]["invoices"] if document["supplier"] == "MONOPRIX" and not document["key"]["sha256"]]
        self.assertEqual(sorted(document["key"]["occurrence"] for document in twins), [0, 1])
        self.assertEqual(len({document["key"]["file_sha256"] for document in twins}), 1)

    def test_fractions_refunds_and_the_negative_vat_row_come_back_exactly(self):
        self._check(REPLACE)
        invoice = Invoice.objects.get(invoice_number="M-0001")
        self.assertEqual(
            [(line.quantity, line.total_ht) for line in invoice.lines.order_by("id")],
            [(D("6.000"), D("57.00")), (D("-1.000"), D("-9.50")), (D("0.350"), D("4.20")), (D("2.000"), D("3.00"))],
        )
        receipt = Invoice.objects.get(source_sha256__gt="")
        self.assertEqual(receipt.vat_breakdown, [["0.055", "-2.76", "-0.15"], ["0.2", "100.00", "20.00"]])
        self.assertTrue(receipt.vat_table_typed)

    def test_the_uba_invoice_with_no_line_keeps_its_stored_status(self):
        self._check(MERGE)
        empty = Invoice.objects.get(invoice_number="U-0001")
        self.assertEqual((empty.lines.count(), empty.status), (0, NEEDS_REVIEW))

    def test_nothing_is_learned_and_nothing_lights_a_view(self):
        SupplierChange.objects.all().delete()
        Supplier.objects.update(ticket_identifiers=[])
        self._check(MERGE)
        self.assertFalse(SupplierChange.objects.exists())
        self.assertEqual(_changes_to_see().count(), 0)
        self.assertFalse(Supplier.objects.exclude(ticket_identifiers=[]).exists())


class IdempotenceTests(MediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_invoices()
        self.reader = export_archive({"factures"}, closed=False)
        self.addCleanup(self.reader.close)

    def test_merge_says_everything_is_unchanged(self):
        report = import_archive(self.reader, MERGE).section("factures")
        self.assertEqual(report.tallies["documents"].unchanged, 7)
        self.assertEqual(report.tallies["lignes"].unchanged, 8)
        self.assertEqual(report.tallies["fichiers"].unchanged, 8)
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))
        self.assertFalse(report.changes)

    def test_replace_changes_nothing(self):
        before, files = db_fingerprint(), {name: sha(name) for name in named_files()}
        run = import_archive(self.reader, REPLACE)
        self.assertFalse(run.section("factures").changes)
        self.assertEqual(run.affected(), set())
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual({name: sha(name) for name in named_files()}, files)


class FileCountTests(MediaMixin, TestCase):
    """Every file and line is counted once, as what happens to it: a document
    updated, or given a file back, keeps what it already had « inchangé ».
    A full « Remplacer » restore of the 19/09 copy counted 1 518 of its
    1 520 files: those of the one document it updated were counted nowhere
    (verifier, 19/09)."""

    def setUp(self):
        super().setUp()
        self.built = build_invoices()
        self.reader = export_archive({"factures"}, closed=False)
        self.addCleanup(self.reader.close)

    def test_a_replaced_document_keeps_its_files_counted(self):
        # The ticket - a photo and its preview - is dated otherwise here.
        Invoice.objects.filter(pk=self.built["receipt"].pk).update(invoice_date=date(2026, 9, 3))
        report = import_archive(self.reader, REPLACE).section("factures")
        self.assertEqual(report.tallies["documents"].updated, 1)
        files = report.tallies["fichiers"]
        self.assertEqual((files.created, files.updated, files.deleted, files.unchanged), (0, 0, 0, 8))
        self.assertEqual(report.tallies["lignes"].unchanged, 8)

    def test_a_file_given_back_leaves_the_rest_of_its_document_counted(self):
        default_storage.delete("receipts/2026/09/ticket_essai_apercu.jpg")
        report = import_archive(self.reader, MERGE).section("factures")
        self.assertEqual(report.tallies["documents"].updated, 1)
        files = report.tallies["fichiers"]
        self.assertEqual((files.created, files.updated, files.deleted, files.unchanged), (1, 0, 0, 7))
        self.assertEqual(report.tallies["lignes"].unchanged, 8)

    def test_a_document_in_conflict_is_counted_by_its_conflict_alone(self):
        # Guard: a document kept as it is, said as a conflict, is not also
        # « inchangé » - neither it nor its lines and files.
        Invoice.objects.filter(pk=self.built["receipt"].pk).update(invoice_date=date(2026, 9, 3))
        report = import_archive(self.reader, MERGE).section("factures")
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.tallies["documents"].unchanged, 6)
        self.assertEqual(report.tallies["fichiers"].unchanged, 6)
        self.assertEqual(report.tallies["lignes"].unchanged, 7)


class MergeAndReplaceTests(MediaMixin, TestCase):
    """Against the archive, this database has: M-0001 dated otherwise, M-0002
    gone, M-0003's file lost from the disk, M-0099 only here (paid by the
    bank), and M-0098 only here, a stock take priced from it."""

    def setUp(self):
        super().setUp()
        metro = Supplier.objects.get(code="METRO")
        vodka = StockType.objects.create(name="Vodka essai", unit=UnitChoices.LITRE)
        self.bottle = make_product(metro, "VODKA ESSAI 70CL", stock_type=vodka, stock_equivalent="0.7")
        for number in ("M-0001", "M-0002", "M-0003"):
            invoice = make_invoice(
                metro, invoice_number=number, source_file=store(f"invoices/2026/09/{number}.pdf", f"%PDF {number}".encode()),
                status=COMPLETE,
            )
            services.create_stock_movement_for_line(make_invoice_line(invoice, self.bottle, quantity=D("2"), total_ht="19.00"))
        self.reader = export_archive({"factures"}, closed=False)
        self.addCleanup(self.reader.close)

        Invoice.objects.filter(invoice_number="M-0001").update(invoice_date=date(2026, 1, 2))
        gone = Invoice.objects.get(invoice_number="M-0002")
        StockMovement.objects.filter(invoice_line__invoice=gone).delete()
        gone.delete()
        default_storage.delete("invoices/2026/09/M-0002.pdf")
        default_storage.delete("invoices/2026/09/M-0003.pdf")
        self.paid = make_invoice(
            metro, invoice_number="M-0099", source_file=store("invoices/2026/09/M-0099.pdf", b"%PDF M-0099"), status=COMPLETE
        )
        make_invoice_line(self.paid, make_product(metro, "PRODUIT ESSAI SEUL"), total_ht="5.00")
        payment(self.paid)
        self.priced = make_invoice(metro, invoice_number="M-0098", invoice_date=date(2025, 12, 20), status=COMPLETE)
        line = make_invoice_line(self.priced, self.bottle, quantity=D("3"), total_ht="28.50")
        take = make_stock_take(datetime(2025, 12, 31, 22, 0, tzinfo=UTC))
        take_line = StockTakeLine.objects.create(
            stock_take=take, product=self.bottle, counted_quantity=D("3"), unit=UnitChoices.UNIT, value_ht=D("28.50")
        )
        StockTakeLineSource.objects.create(stock_take_line=take_line, invoice_line=line, quantity_used=D("3"), unit_cost_ht=D("9.5"))

    def test_merge(self):
        run = import_archive(self.reader, MERGE)
        report = run.section("factures")
        self.assertEqual(
            report.conflicts,
            ["Facture Metro n° M-0001 : différente dans l'archive (date) — gardée telle quelle"],
        )
        self.assertEqual(Invoice.objects.get(invoice_number="M-0001").invoice_date, date(2026, 1, 2))
        self.assertTrue(Invoice.objects.filter(invoice_number="M-0002").exists())
        self.assertTrue(default_storage.exists("invoices/2026/09/M-0002.pdf"))
        restored = Invoice.objects.get(invoice_number="M-0003")
        self.assertEqual(restored.source_file.name, "invoices/2026/09/M-0003.pdf")
        self.assertEqual(sha(restored.source_file.name), hashlib.sha256(b"%PDF M-0003").hexdigest())
        self.assertIn("Facture Metro n° M-0003 : fichier restauré", report.notes)
        documents = report.tallies["documents"]
        self.assertEqual((documents.created, documents.updated, documents.deleted), (1, 1, 0))
        self.assertTrue(Invoice.objects.filter(pk=self.paid.pk).exists())
        self.assertEqual(StockMovement.objects.filter(invoice_line__invoice__invoice_number="M-0002").count(), 1)
        self.assertEqual(run.affected(), {"factures"})

    def test_replace(self):
        with self.captureOnCommitCallbacks(execute=True):
            run = import_archive(self.reader, REPLACE)
        report = run.section("factures")
        self.assertEqual(Invoice.objects.get(invoice_number="M-0001").invoice_date, date(2026, 1, 1))
        self.assertTrue(Invoice.objects.filter(invoice_number="M-0002").exists())
        self.assertFalse(Invoice.objects.filter(pk=self.paid.pk).exists())
        self.assertFalse(default_storage.exists("invoices/2026/09/M-0099.pdf"))
        self.assertFalse(Product.objects.filter(raw_name="PRODUIT ESSAI SEUL").exists())
        documents = report.tallies["documents"]
        self.assertEqual((documents.created, documents.updated, documents.deleted), (1, 2, 1))
        self.assertEqual(
            report.kept, ["Facture Metro n° M-0098 : a servi à valoriser l'inventaire du 31/12/2025"]
        )
        self.assertTrue(Invoice.objects.filter(pk=self.priced.pk).exists())
        bank = run.section("banque")
        self.assertEqual(bank.tallies["paiements"].deleted, 1)
        self.assertEqual(
            bank.notes,
            [
                (
                    "1 paiement de la banque est supprimé avec sa facture ; réimportez ensemble « Factures et "
                    "tickets » et « Banque » de la sauvegarde pour le retrouver"
                )
            ],
        )
        self.assertFalse(InvoicePayment.objects.exists())
        self.assertTrue(BankTransaction.objects.get().settled_by_hand)
        self.assertEqual(run.affected(), {"factures", "banque"})
        self.assertEqual(StockMovement.objects.filter(invoice_line__isnull=True).count(), 0)

    def test_replace_never_removes_a_document_whose_record_is_skipped(self):
        def spoil(payload):
            for record in payload["invoices"]:
                if record["invoice_number"] == "M-0001":
                    record["status"] = "PERDU"
            return payload

        reader = ArchiveReader(forge(self.reader, factures=spoil))
        self.addCleanup(reader.close)
        report = import_archive(reader, REPLACE).section("factures")
        self.assertIn("Facture Metro n° M-0001 : « status » : valeur inconnue (« PERDU »)", report.skipped)
        self.assertEqual(Invoice.objects.get(invoice_number="M-0001").invoice_date, date(2026, 1, 2))

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before, files = db_fingerprint(), media_names()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_names(), files)
        self.assertEqual(callbacks, [])
        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())


class MatchedByFileTests(MediaMixin, TestCase):
    """A stand-in number « YYYYMMDD-total » here, the printed one in the
    archive: the same document, found by its file."""

    def setUp(self):
        super().setUp()
        self.leroy = make_supplier(code="LEROY_ESSAI", name="Leroy Essai")
        self.invoice = make_invoice(
            self.leroy, invoice_number="LM-4411", invoice_date=date(2026, 9, 2),
            source_file=store("invoices/2026/09/leroy_essai.pdf", b"%PDF-1.4 leroy essai"),
        )
        self.reader = export_archive({"factures"}, closed=False)
        self.addCleanup(self.reader.close)
        Invoice.objects.filter(pk=self.invoice.pk).update(invoice_number="20260902-9.68")

    def test_merge_finds_it_and_says_the_numbers_differ(self):
        report = import_archive(self.reader, MERGE).section("factures")
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertIn("rapproché par son fichier : n° 20260902-9.68 ici, n° LM-4411 dans l'archive", report.notes)
        self.assertEqual(
            report.conflicts, ["Facture Leroy Essai n° LM-4411 : différente dans l'archive (numéro) — gardée telle quelle"]
        )
        self.assertEqual(Invoice.objects.get().invoice_number, "20260902-9.68")

    def test_replace_takes_the_archives_number(self):
        import_archive(self.reader, REPLACE)
        self.assertEqual(Invoice.objects.get().invoice_number, "LM-4411")

    def test_two_documents_answering_one_record_is_skipped(self):
        other = make_invoice(self.leroy, invoice_number="LM-4411", invoice_date=date(2026, 9, 3))
        report = import_archive(self.reader, MERGE).section("factures")
        self.assertEqual(len(report.skipped), 1)
        self.assertIn("deux documents différents répondent à cette facture (n° LM-4411 et fichier de Facture Leroy Essai n° 20260902-9.68)", report.skipped[0])
        self.assertEqual(Invoice.objects.count(), 2)
        self.assertEqual(Invoice.objects.get(pk=other.pk).invoice_date, date(2026, 9, 3))

    def test_replace_never_removes_a_document_a_skipped_record_answers_to(self):
        make_invoice(self.leroy, invoice_number="LM-4411", invoice_date=date(2026, 9, 3))
        with self.captureOnCommitCallbacks(execute=True):
            report = import_archive(self.reader, REPLACE).section("factures")
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(Invoice.objects.count(), 2)
        self.assertTrue(default_storage.exists("invoices/2026/09/leroy_essai.pdf"))


class FileTests(MediaMixin, TestCase):
    def test_merge_gives_back_a_lost_preview(self):
        monoprix = Supplier.objects.get(code="MONOPRIX")
        make_invoice(
            monoprix, invoice_number="T-1", source_file=store("invoices/2026/09/t1_essai.jpg", b"JPEG ticket t1"),
            preview_image=store("receipts/2026/09/t1_apercu.jpg", b"JPEG apercu t1"),
        )
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        default_storage.delete("receipts/2026/09/t1_apercu.jpg")
        report = import_archive(reader, MERGE).section("factures")
        self.assertEqual(report.notes, ["Facture Monoprix n° T-1 : photo restaurée"])
        self.assertEqual(sha("receipts/2026/09/t1_apercu.jpg"), hashlib.sha256(b"JPEG apercu t1").hexdigest())

    def test_a_name_taken_by_another_file_gets_an_available_one_and_the_other_stays(self):
        metro = Supplier.objects.get(code="METRO")
        make_invoice(metro, invoice_number="M-7", source_file=store("invoices/2026/09/clash_essai.pdf", b"%PDF contenu A"))
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        Invoice.objects.all().delete()
        store("invoices/2026/09/clash_essai.pdf", b"%PDF contenu B, un autre document")

        import_archive(reader, MERGE)
        invoice = Invoice.objects.get(invoice_number="M-7")
        self.assertNotEqual(invoice.source_file.name, "invoices/2026/09/clash_essai.pdf")
        self.assertTrue(invoice.source_file.name.startswith("invoices/2026/09/clash_essai_"))
        self.assertEqual(sha(invoice.source_file.name), hashlib.sha256(b"%PDF contenu A").hexdigest())
        self.assertEqual(sha("invoices/2026/09/clash_essai.pdf"), hashlib.sha256(b"%PDF contenu B, un autre document").hexdigest())

    def test_the_same_file_already_stored_is_reused(self):
        metro = Supplier.objects.get(code="METRO")
        make_invoice(metro, invoice_number="M-8", source_file=store("invoices/2026/09/deja_essai.pdf", b"%PDF deja"))
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        Invoice.objects.all().delete()
        before = media_names()
        import_archive(reader, MERGE)
        self.assertEqual(Invoice.objects.get().source_file.name, "invoices/2026/09/deja_essai.pdf")
        self.assertEqual(media_names(), before)

    def test_replace_swaps_a_changed_file_and_removes_the_old_one_on_commit(self):
        metro = Supplier.objects.get(code="METRO")
        invoice = make_invoice(metro, invoice_number="M-9", source_file=store("invoices/2026/09/m9_essai.pdf", b"%PDF version 1"))
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        Invoice.objects.filter(pk=invoice.pk).update(source_file=store("invoices/2026/09/m9_autre.pdf", b"%PDF version 2"))
        default_storage.delete("invoices/2026/09/m9_essai.pdf")
        store("invoices/2026/09/m9_essai.pdf", b"%PDF un fichier sans rapport")

        with self.captureOnCommitCallbacks(execute=True):
            report = import_archive(reader, REPLACE).section("factures")
        invoice.refresh_from_db()
        self.assertEqual(sha(invoice.source_file.name), hashlib.sha256(b"%PDF version 1").hexdigest())
        self.assertFalse(default_storage.exists("invoices/2026/09/m9_autre.pdf"))
        self.assertTrue(default_storage.exists("invoices/2026/09/m9_essai.pdf"))
        self.assertEqual(report.tallies["fichiers"].updated, 1)

    def test_a_30_mb_file_is_streamed_both_ways(self):
        metro = Supplier.objects.get(code="METRO")
        name = "invoices/2026/09/gros_essai.pdf"
        path = Path(default_storage.path(name))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.writelines(os.urandom(1024 * 1024) for _ in range(30))
        make_invoice(metro, invoice_number="M-GROS", source_file=name)

        tracemalloc.start()
        try:
            reader = export_archive({"factures"}, closed=False)
            _size, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.addCleanup(reader.close)
        self.assertLess(peak, 10 * 1024 * 1024)
        original = sha(name)
        Invoice.objects.all().delete()
        default_storage.delete(name)

        tracemalloc.start()
        try:
            import_archive(reader, MERGE)
            _size, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 10 * 1024 * 1024)
        self.assertEqual(sha(Invoice.objects.get().source_file.name), original)


class StockTakeTrailTests(MediaMixin, TestCase):
    """A line a count was priced from is updated in place, never deleted."""

    def setUp(self):
        super().setUp()
        metro = Supplier.objects.get(code="METRO")
        vodka = StockType.objects.create(name="Vodka essai", unit=UnitChoices.LITRE)
        self.bottle = make_product(metro, "VODKA ESSAI 70CL", stock_type=vodka, stock_equivalent="0.7")
        self.invoice = make_invoice(metro, invoice_number="M-TAKE", status=COMPLETE)
        self.lines = [
            make_invoice_line(self.invoice, self.bottle, quantity=D("1"), total_ht=amount) for amount in ("10.00", "20.00")
        ]
        self.take = make_stock_take(datetime(2025, 12, 31, 22, 0, tzinfo=UTC))
        self.take_line = StockTakeLine.objects.create(
            stock_take=self.take, product=self.bottle, counted_quantity=D("1"), unit=UnitChoices.UNIT, value_ht=D("20.00")
        )

    def _priced_from(self, line):
        return StockTakeLineSource.objects.create(
            stock_take_line=self.take_line, invoice_line=line, quantity_used=D("1"), unit_cost_ht=D("20")
        )

    def test_the_same_number_of_lines_is_updated_in_place(self):
        self.lines.append(make_invoice_line(self.invoice, self.bottle, quantity=D("1"), total_ht="30.00"))
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        source = self._priced_from(self.lines[1])
        InvoiceLine.objects.filter(pk=self.lines[1].pk).update(total_ht=D("21.00"))

        report = import_archive(reader, REPLACE).section("factures")
        source.refresh_from_db()
        self.assertEqual(source.invoice_line_id, self.lines[1].pk)
        self.assertEqual(InvoiceLine.objects.get(pk=self.lines[1].pk).total_ht, D("20.00"))
        self.assertEqual(report.tallies["lignes"].updated, 1)
        self.assertEqual(report.tallies["lignes"].unchanged, 2)
        self.assertEqual(StockMovement.objects.filter(invoice_line__invoice=self.invoice).count(), 3)

    def test_a_line_to_remove_that_priced_a_count_keeps_the_whole_document(self):
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        third = make_invoice_line(self.invoice, self.bottle, quantity=D("1"), total_ht="30.00")
        self._priced_from(third)
        Invoice.objects.filter(pk=self.invoice.pk).update(invoice_date=date(2026, 3, 3))
        before = db_fingerprint()

        report = import_archive(reader, REPLACE).section("factures")
        self.assertEqual(report.kept, ["Facture Metro n° M-TAKE : sa ligne n°3 a valorisé l'inventaire du 31/12/2025"])
        self.assertEqual(db_fingerprint(), before)

    def _removed_above_a_priced_line(self):
        """[A, B, C] exported, then B taken out on the correction page (it
        priced nothing; its product goes with it) and C priced a count: here
        [A, C], the archive [A, B, C]. Lines are paired by rank."""
        metro = Supplier.objects.get(code="METRO")
        gin = StockType.objects.create(name="Gin essai", unit=UnitChoices.LITRE)
        tonic = make_product(metro, "TONIC ESSAI 20CL")
        self.gin = make_product(metro, "GIN ESSAI 70CL", stock_type=gin, stock_equivalent="0.7")
        invoice = make_invoice(metro, invoice_number="M-RANG", status=COMPLETE)
        make_invoice_line(invoice, self.bottle, quantity=D("1"), total_ht="10.00")
        removed = make_invoice_line(invoice, tonic, quantity=D("24"), total_ht="12.00", raw_name="TONIC ESSAI 20CL")
        priced = make_invoice_line(invoice, self.gin, quantity=D("1"), total_ht="15.00", raw_name="GIN ESSAI 70CL")
        for line in invoice.lines.all():
            services.create_stock_movement_for_line(line)
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        StockMovement.objects.filter(invoice_line=removed).delete()
        removed.delete()
        tonic.delete()
        take_line = StockTakeLine.objects.create(
            stock_take=self.take, product=self.gin, counted_quantity=D("1"), unit=UnitChoices.UNIT, value_ht=D("15.00")
        )
        source = StockTakeLineSource.objects.create(
            stock_take_line=take_line, invoice_line=priced, quantity_used=D("1"), unit_cost_ht=D("15")
        )
        return reader, source

    def test_a_priced_line_the_archive_has_another_product_at_keeps_the_whole_document(self):
        """Rewritten in place, the row the count was priced from became the
        tonic's, the gin came back as a new unprotected row, and the count of
        gin was priced from a tonic purchase - with nothing said."""
        reader, source = self._removed_above_a_priced_line()
        before = db_fingerprint()

        report = import_archive(reader, {"factures": REPLACE}).section("factures")
        self.assertEqual(
            report.kept,
            [
                (
                    "Facture Metro n° M-RANG : sa ligne n°2 a valorisé l'inventaire du 31/12/2025 ; l'archive met "
                    "« TONIC ESSAI 20CL » à sa place"
                )
            ],
        )
        self.assertEqual(db_fingerprint(), before)
        source.refresh_from_db()
        self.assertEqual((source.invoice_line.product, source.invoice_line.raw_name), (self.gin, "GIN ESSAI 70CL"))

    def test_the_preview_keeps_it_too(self):
        reader, _source = self._removed_above_a_priced_line()
        before = db_fingerprint()
        preview = import_archive(reader, {"factures": REPLACE}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(len(preview.section("factures").kept), 1)
        self.assertEqual(preview.outcome(), import_archive(reader, {"factures": REPLACE}).outcome())

    def test_a_priced_line_the_archive_names_otherwise_keeps_the_whole_document(self):
        """The same product under another name at that rank is another
        purchase line: the stock takes' own import tells a source's line by
        its folded name (sections/stock_takes.py), and would no longer find
        it."""
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        self._priced_from(self.lines[1])
        InvoiceLine.objects.filter(pk=self.lines[1].pk).update(raw_name="VODKA ESSAI 70CL PROMO")
        before = db_fingerprint()

        report = import_archive(reader, REPLACE).section("factures")
        self.assertEqual(
            report.kept,
            [
                (
                    "Facture Metro n° M-TAKE : sa ligne n°2 a valorisé l'inventaire du 31/12/2025 ; l'archive met "
                    "« VODKA ESSAI 70CL » à sa place"
                )
            ],
        )
        self.assertEqual(db_fingerprint(), before)

    # .. a count the same run's « Inventaires » « Remplacer » deletes ..........
    #
    # The invoices apply before the counts prune (applies go forward, prunes
    # in reverse). A document kept for a count that same run then deleted:
    # the report said both, and a full « Remplacer » restore left the invoice
    # short of the line the archive has (review, 19/09 copy).

    def _whole(self, inventaires):
        return {"fournisseurs": MERGE, "associations": MERGE, "factures": REPLACE, "inventaires": inventaires}

    def _count_after_the_export(self, invoice_line):
        """A count taken after the export, priced from this line: the
        archive has no such count."""
        take = make_stock_take(datetime(2026, 9, 19, 10, 0, tzinfo=UTC))
        take_line = StockTakeLine.objects.create(
            stock_take=take, product=invoice_line.product, counted_quantity=D("1"), unit=UnitChoices.UNIT,
            value_ht=invoice_line.total_ht,
        )
        StockTakeLineSource.objects.create(
            stock_take_line=take_line, invoice_line=invoice_line, quantity_used=D("1"), unit_cost_ht=invoice_line.total_ht
        )
        return take

    def _shifted_after_the_export(self):
        """Here [B, D] exported with the counts, then B taken out on the
        correction page and a new count priced from D: here [D], the archive
        [B, D] and no count priced from D."""
        metro = Supplier.objects.get(code="METRO")
        tonic = make_product(metro, "TONIC ESSAI 20CL")
        self.gin = make_product(
            metro, "GIN ESSAI 70CL", stock_type=StockType.objects.create(name="Gin essai", unit=UnitChoices.LITRE),
            stock_equivalent="0.7",
        )
        invoice = make_invoice(metro, invoice_number="M-RANG", status=COMPLETE)
        removed = make_invoice_line(invoice, tonic, quantity=D("24"), total_ht="12.00", raw_name="TONIC ESSAI 20CL")
        priced = make_invoice_line(invoice, self.gin, quantity=D("1"), total_ht="15.00", raw_name="GIN ESSAI 70CL")
        for line in invoice.lines.all():
            services.create_stock_movement_for_line(line)
        reader = export_archive(self._whole(REPLACE))
        self.addCleanup(reader.close)
        exported = {key: registry.get(key).snapshot() for key in ("factures", "inventaires")}
        StockMovement.objects.filter(invoice_line=removed).delete()
        removed.delete()
        tonic.delete()
        return reader, exported, self._count_after_the_export(priced)

    def test_a_count_the_same_run_deletes_keeps_nothing_and_the_invoice_comes_back_whole(self):
        reader, exported, take = self._shifted_after_the_export()
        preview = import_archive(reader, self._whole(REPLACE), preview=True)
        run = import_archive(reader, self._whole(REPLACE))
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(run.section("factures").kept, [])
        self.assertEqual(run.section("inventaires").tallies[stock_takes.TAKES].deleted, 1)
        self.assertFalse(type(take).objects.filter(pk=take.pk).exists())
        self.assertEqual(
            list(Invoice.objects.get(invoice_number="M-RANG").lines.order_by("id").values_list("product__raw_name", "raw_name")),
            [("TONIC ESSAI 20CL", "TONIC ESSAI 20CL"), ("GIN ESSAI 70CL", "GIN ESSAI 70CL")],
        )
        # The export, exactly: its lines, their purchases, its counts.
        self.assertEqual({key: registry.get(key).snapshot() for key in exported}, exported)

    def test_a_count_merged_is_still_kept_for_and_said(self):
        reader, _exported, take = self._shifted_after_the_export()
        before = db_fingerprint()
        run = import_archive(reader, self._whole(MERGE))
        self.assertEqual(
            run.section("factures").kept,
            [
                (
                    "Facture Metro n° M-RANG : sa ligne n°1 a valorisé l'inventaire du 19/09/2026 ; l'archive met "
                    "« TONIC ESSAI 20CL » à sa place"
                )
            ],
        )
        self.assertTrue(type(take).objects.filter(pk=take.pk).exists())
        self.assertEqual(db_fingerprint(), before)

    def _added_after_the_export(self):
        """Here [A] exported with the counts, then a line C added and a new
        count priced from C: the archive has neither."""
        metro = Supplier.objects.get(code="METRO")
        self.gin = make_product(
            metro, "GIN ESSAI 70CL", stock_type=StockType.objects.create(name="Gin essai", unit=UnitChoices.LITRE),
            stock_equivalent="0.7",
        )
        invoice = make_invoice(metro, invoice_number="M-AJOUT", status=COMPLETE)
        services.create_stock_movement_for_line(
            make_invoice_line(invoice, self.gin, quantity=D("1"), total_ht="15.00", raw_name="GIN ESSAI 70CL")
        )
        reader = export_archive(self._whole(REPLACE))
        self.addCleanup(reader.close)
        exported = {key: registry.get(key).snapshot() for key in ("factures", "inventaires")}
        added = make_invoice_line(invoice, self.gin, quantity=D("1"), total_ht="16.00", raw_name="GIN ESSAI 70CL")
        services.create_stock_movement_for_line(added)
        return reader, exported, self._count_after_the_export(added), added

    def test_a_line_only_a_count_the_same_run_deletes_priced_goes_after_that_count(self):
        """Deleted in the invoices' apply, the line would still be held by
        that count (PROTECT): it goes in the invoices' prune, which runs
        after the counts'."""
        reader, exported, take, added = self._added_after_the_export()
        preview = import_archive(reader, self._whole(REPLACE), preview=True)
        run = import_archive(reader, self._whole(REPLACE))
        self.assertEqual(preview.outcome(), run.outcome())
        report = run.section("factures")
        self.assertEqual(report.kept, [])
        self.assertEqual(report.tallies["lignes"].deleted, 1)
        self.assertFalse(type(take).objects.filter(pk=take.pk).exists())
        self.assertFalse(InvoiceLine.objects.filter(pk=added.pk).exists())
        self.assertEqual({key: registry.get(key).snapshot() for key in exported}, exported)

    def test_a_line_a_count_of_the_archive_was_moved_onto_stays_and_is_said(self):
        """Only a hand-edited archive does this: its count of 31/12/2025
        names the line its invoice does not have. The line stays - deleting
        it would break that count's trail - and the report says why."""
        reader, _exported, _take, added = self._added_after_the_export()

        def onto_the_added_line(payload):
            for take in payload["stock_takes"]:
                for line in take["lines"]:
                    line["sources"] = [
                        {"invoice": key, "line": 1, "raw_name": "GIN ESSAI 70CL", "quantity_used": "1", "unit_cost_ht": "16.0000"}
                    ]
            return payload

        key = next(
            record["key"] for record in reader.section("factures").payload()["invoices"]
            if record["invoice_number"] == "M-AJOUT"
        )
        forged = ArchiveReader(forge(reader, inventaires=onto_the_added_line))
        self.addCleanup(forged.close)
        preview = import_archive(forged, self._whole(REPLACE), preview=True)
        run = import_archive(forged, self._whole(REPLACE))
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(
            run.section("factures").kept,
            ["Facture Metro n° M-AJOUT : sa ligne n°2, que l'archive n'a pas, a valorisé l'inventaire du 31/12/2025"],
        )
        self.assertTrue(InvoiceLine.objects.filter(pk=added.pk).exists())
        self.assertEqual(StockTakeLineSource.objects.get(stock_take_line=self.take_line).invoice_line_id, added.pk)

    def test_a_priced_line_the_archive_only_spaces_differently_is_updated_in_place(self):
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        source = self._priced_from(self.lines[1])
        InvoiceLine.objects.filter(pk=self.lines[1].pk).update(raw_name="vodka  essai 70cl", total_ht=D("21.00"))

        report = import_archive(reader, REPLACE).section("factures")
        self.assertEqual(report.kept, [])
        source.refresh_from_db()
        self.assertEqual(source.invoice_line_id, self.lines[1].pk)
        self.assertEqual(
            InvoiceLine.objects.filter(pk=self.lines[1].pk).values_list("raw_name", "total_ht").get(),
            ("VODKA ESSAI 70CL", D("20.00")),
        )


class ClassificationTests(MediaMixin, TestCase):
    def test_a_product_classified_there_but_not_here_has_its_invoices_status_worked_out_again(self):
        metro = Supplier.objects.get(code="METRO")
        vodka = StockType.objects.create(name="Vodka essai", unit=UnitChoices.LITRE)
        bottle = make_product(metro, "VODKA ESSAI 70CL", stock_type=vodka, stock_equivalent="0.7")
        make_invoice_line(make_invoice(metro, invoice_number="M-1", status=COMPLETE), bottle)
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        Invoice.objects.all().delete()
        Product.objects.filter(pk=bottle.pk).update(stock_type=None)

        import_archive(reader, MERGE)
        self.assertEqual(Invoice.objects.get().status, NEEDS_REVIEW)
        self.assertFalse(StockMovement.objects.exists())

    def test_a_classified_product_here_books_its_movements(self):
        metro = Supplier.objects.get(code="METRO")
        bottle = make_product(metro, "VODKA ESSAI 70CL")
        make_invoice_line(make_invoice(metro, invoice_number="M-1", status=NEEDS_REVIEW), bottle, quantity=D("6"), total_ht="57.00")
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        Invoice.objects.all().delete()
        vodka = StockType.objects.create(name="Vodka essai", unit=UnitChoices.LITRE)
        Product.objects.filter(pk=bottle.pk).update(stock_type=vodka, stock_equivalent=D("0.7"))

        import_archive(reader, MERGE)
        movement = StockMovement.objects.get()
        self.assertEqual((movement.stock_type_id, movement.quantity), (vodka.pk, D("4.200")))
        self.assertEqual(Invoice.objects.get().status, COMPLETE)

    def test_replace_gives_an_unclassified_product_the_archives_nature(self):
        water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
        poste = make_product(water, "Eau Essai", is_expense=True)
        make_invoice_line(make_invoice(water, invoice_number="E-1"), poste)
        reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        Product.objects.filter(pk=poste.pk).update(is_expense=False)
        Invoice.objects.update(invoice_date=date(2026, 2, 2))
        import_archive(reader, REPLACE)
        self.assertTrue(Product.objects.get(pk=poste.pk).is_expense)


class RefusalTests(MediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_invoices()
        self.reader = export_archive({"factures"}, closed=False)
        self.addCleanup(self.reader.close)
        with self.captureOnCommitCallbacks(execute=True):
            run_clear({"factures"}, preview=False, closed=False)

    def _import(self, change, strategy=MERGE):
        reader = ArchiveReader(forge(self.reader, factures=change))
        self.addCleanup(reader.close)
        return import_archive(reader, strategy).section("factures")

    @staticmethod
    def _edit(number, edit):
        def change(payload):
            for record in payload["invoices"]:
                if record["invoice_number"] == number:
                    edit(record)
            return payload

        return change

    def test_a_file_without_its_list_is_refused_whole(self):
        with self.assertRaisesMessage(ArchiveError, "factures.json n'a pas de liste « invoices »"):
            self._import({"products": []})

    def test_an_unknown_supplier_skips_the_document(self):
        report = self._import(self._edit("M-0001", lambda record: record.update(supplier="INCONNU_ESSAI")))
        self.assertIn("Facture INCONNU_ESSAI n° M-0001 : fournisseur inconnu « INCONNU_ESSAI »", report.skipped)
        self.assertEqual(Invoice.objects.count(), 6)

    def test_a_line_whose_product_is_nowhere_skips_the_whole_document(self):
        def edit(record):
            record["lines"][2]["product"] = ["METRO", "PRODUIT FANTOME"]

        report = self._import(self._edit("M-0001", edit))
        self.assertIn(
            "Facture Metro n° M-0001 : ligne n°3 : produit inconnu « PRODUIT FANTOME »", report.skipped
        )
        self.assertFalse(Invoice.objects.filter(invoice_number="M-0001").exists())
        self.assertFalse(InvoiceLine.objects.filter(raw_name="JAMBON ESSAI AU POIDS").exists())

    def test_an_amount_with_too_many_places_skips_the_document(self):
        report = self._import(self._edit("M-0001", lambda record: record["lines"][0].update(total_ht="57.001")))
        self.assertIn(
            "Facture Metro n° M-0001 : ligne n°1 : « total_ht » : « 57.001 » a plus de 2 décimales",
            report.skipped,
        )

    def test_an_unknown_status_skips_the_document(self):
        report = self._import(self._edit("U-0001", lambda record: record.update(status="PERDU")))
        self.assertIn("Facture UBA n° U-0001 : « status » : valeur inconnue (« PERDU »)", report.skipped)

    def test_a_dangerous_file_name_skips_the_document(self):
        def edit(record):
            record["source_file"]["name"] = "../../config/essai.pdf"

        report = self._import(self._edit("E-2026-07", edit))
        self.assertIn("Facture Eau Essai n° E-2026-07 : nom de fichier refusé (« ../../config/essai.pdf »)", report.skipped)
        self.assertFalse(Invoice.objects.filter(invoice_number="E-2026-07").exists())

    def test_a_file_the_archive_does_not_declare_skips_the_document(self):
        def edit(record):
            record["source_file"]["member"] = "files/invoices/2026/09/jamais_declare.pdf"

        report = self._import(self._edit("E-2026-07", edit))
        self.assertIn(
            "Facture Eau Essai n° E-2026-07 : fichier absent de l'archive (« invoices/2026/09/eau_essai.pdf »)",
            report.skipped,
        )

    def test_checks_that_are_not_checks_skip_the_document(self):
        """Stored, `parse_checks: [1]` took « À vérifier » down: the queue
        asks every check `.get("passed")` (Invoice.failed_checks)."""
        for checks in (
            [1],
            ["Somme des lignes = total"],
            [{"passed": True}],
            [{"label": "Somme des lignes = total", "passed": "oui"}],
            [{"label": 3, "passed": True}],
            [{"label": "Somme des lignes = total", "passed": True, "detail": ["117,09 €"]}],
            {"label": "Somme des lignes = total", "passed": True},
        ):
            with self.subTest(checks=checks):
                report = self._import(self._edit("M-0001", lambda record, checks=checks: record.update(parse_checks=checks)))
                self.assertIn("Facture Metro n° M-0001 : contrôles illisibles", report.skipped)
                self.assertFalse(Invoice.objects.filter(invoice_number="M-0001").exists())

    def test_a_vat_table_the_review_page_cannot_read_skips_the_document(self):
        for table in (
            [1],
            [["0.2", "100.00"]],
            [["0.2", "100.00", "20.00", "120.00"]],
            [[0.2, 100, 20]],
            [["0.2", "cent", "20.00"]],
            [["0.2", "NaN", "20.00"]],
            [{"rate": "0.2", "base": "100.00", "vat": "20.00"}],
            "0.2;100.00;20.00",
        ):
            with self.subTest(table=table):
                report = self._import(self._edit("M-0001", lambda record, table=table: record.update(vat_breakdown=table)))
                self.assertIn("Facture Metro n° M-0001 : table de TVA illisible", report.skipped)
                self.assertFalse(Invoice.objects.filter(invoice_number="M-0001").exists())

    def test_the_checks_and_table_the_application_writes_are_read(self):
        report = self._import(
            self._edit(
                "M-0001",
                lambda record: record.update(
                    parse_checks=[{"label": "Somme des lignes = total", "passed": False, "detail": "1,25 € d'écart"}],
                    vat_breakdown=[["0.055", "-2.76", "-0.15"], ["0.2", "100.00", "20.00"]],
                ),
            )
        )
        self.assertEqual(report.skipped, [])
        invoice = Invoice.objects.get(invoice_number="M-0001")
        self.assertEqual([check["label"] for check in invoice.failed_checks], ["Somme des lignes = total"])

    def test_an_illegible_key_skips_the_document(self):
        report = self._import(self._edit("U-0001", lambda record: record["key"].update(occurrence="0")))
        self.assertIn("Facture UBA n° U-0001 : clé illisible dans l'archive", report.skipped)

    def test_a_document_twice_in_the_archive_is_created_once(self):
        def change(payload):
            payload["invoices"].append(dict(payload["invoices"][-1]))
            return payload

        report = self._import(change)
        self.assertEqual(len(report.skipped), 1)
        self.assertTrue(report.skipped[0].endswith("en double dans l'archive"), report.skipped[0])
        self.assertEqual(Invoice.objects.count(), 7)

    def test_an_unknown_field_is_noted_once(self):
        def change(payload):
            for record in payload["invoices"]:
                record["couleur"] = "vert"
            return payload

        report = self._import(change)
        self.assertEqual(report.notes.count("champ inconnu ignoré : couleur"), 1)
        self.assertEqual(Invoice.objects.count(), 7)


class ClearTests(MediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.built = build_invoices()
        payment(self.built["bill"])
        self.names = named_files()
        store("invoices/2026/09/orphelin_essai.pdf", b"%PDF cite par rien")

    def test_the_registry_clears_the_stock_takes_first(self):
        self.assertIn("inventaires", registry.closure({"factures"}, "clear"))
        self.assertLess(registry.INFO["factures"].order, registry.INFO["inventaires"].order)

    def test_files_go_only_after_the_commit(self):
        with self.captureOnCommitCallbacks() as callbacks:
            run = run_clear({"factures"}, preview=False, closed=False)
        self.assertFalse(Invoice.objects.exists())
        self.assertTrue(self.names <= media_names())
        for callback in callbacks:
            callback()
        self.assertFalse(self.names & media_names())
        self.assertTrue(default_storage.exists("invoices/2026/09/orphelin_essai.pdf"))

        report = run.section("factures")
        self.assertEqual(report.tallies["documents"].deleted, 7)
        self.assertEqual(report.tallies["fichiers"].deleted, 8)
        self.assertIn("Les historiques de récupération et d'import de tickets restent.", report.notes)
        orphans = len({name for name in media_names() if name.startswith(("invoices/", "receipts/"))} - self.names)
        self.assertIn(
            f"{orphans} fichier{'s' if orphans > 1 else ''} que plus rien ne cite dans media/", report.notes[-1]
        )
        self.assertEqual(run.section("banque").tallies["paiements"].deleted, 1)
        self.assertTrue(BankTransaction.objects.exists())
        self.assertFalse(InvoicePayment.objects.exists())
        self.assertTrue(Product.objects.filter(raw_name="VODKA ESSAI 70CL").exists())
        self.assertFalse(Product.objects.filter(raw_name="SERVIETTES ESSAI").exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(registry.get("factures").count()["documents"], 0)

    def test_the_bank_note_says_how_to_get_the_payments_back(self):
        """Importing « Banque » alone from the backup restored none of them:
        every link names a document that is gone. Imported one after the
        other, the bank cannot tell those lines from ones a person unlinked
        (bank.UNDONE_NOTE): the two go back in ONE import. And the preview's
        note is the confirm's word for word - a preview and its confirm on
        the same data have one outcome, which is what tells a stale preview
        apart."""
        payment(self.built["invoice"], n=2)
        preview = run_clear({"factures"}, preview=True, closed=False)
        with self.captureOnCommitCallbacks(execute=True):
            done = run_clear({"factures"}, preview=False, closed=False)
        note = (
            "2 paiements de la banque sont supprimés avec leurs factures ; réimportez ensemble « Factures et "
            "tickets » et « Banque » de la sauvegarde pour les retrouver"
        )
        self.assertEqual(done.section("banque").notes, [note])
        self.assertEqual(preview.section("banque").notes, [note])
        self.assertEqual(done.section("banque").tallies["paiements"].deleted, 2)

    def test_a_failure_half_way_deletes_no_file(self):
        with (
            self.captureOnCommitCallbacks(execute=True) as callbacks,
            mock.patch("transfer.rebuild.rebuild", side_effect=RuntimeError("échec (essai)")),
            self.assertRaises(RuntimeError),
        ):
            run_clear({"factures"}, preview=False, closed=False)
        self.assertEqual(callbacks, [])
        self.assertEqual(Invoice.objects.count(), 7)
        self.assertTrue(self.names <= media_names())

    def test_a_preview_changes_nothing(self):
        before, files = db_fingerprint(), media_names()
        with self.captureOnCommitCallbacks() as callbacks:
            run_clear({"factures"}, preview=True, closed=False)
        self.assertEqual((db_fingerprint(), media_names(), callbacks), (before, files, []))

    def test_a_document_a_count_was_priced_from_stays_and_is_said(self):
        invoice = self.built["invoice"]
        take = make_stock_take(datetime(2025, 12, 31, 22, 0, tzinfo=UTC))
        product = Product.objects.get(raw_name="VODKA ESSAI 70CL")
        take_line = StockTakeLine.objects.create(
            stock_take=take, product=product, counted_quantity=D("1"), unit=UnitChoices.UNIT, value_ht=D("9.50")
        )
        StockTakeLineSource.objects.create(
            stock_take_line=take_line, invoice_line=invoice.lines.first(), quantity_used=D("1"), unit_cost_ht=D("9.5")
        )
        run = run_clear({"factures"}, preview=False, closed=False)
        self.assertEqual(list(Invoice.objects.values_list("pk", flat=True)), [invoice.pk])
        self.assertIn(
            "Facture Metro n° M-0001 : a servi à valoriser l'inventaire du 31/12/2025",
            run.section("factures").kept,
        )


class KeyTests(MediaMixin, TestCase):
    def test_the_keys_come_from_the_file_refs_without_hashing_again(self):
        built = build_invoices()
        with mock.patch.object(keys, "file_sha256", wraps=keys.file_sha256) as hashed:
            reader = export_archive({"factures"}, closed=False)
        self.addCleanup(reader.close)
        hashed.assert_not_called()
        records = reader.section("factures").payload()["invoices"]
        by_number = {record["invoice_number"]: record for record in records}
        self.assertEqual(by_number["M-0001"]["key"]["file_sha256"], sha(built["invoice"].source_file.name))
        self.assertEqual(archive.safe_member_name(by_number["M-0001"]["source_file"]["member"]), True)


class TwinProductsTests(MediaMixin, TestCase):
    """Two UNCLASSIFIED products of one supplier differing by an accented
    capital: the app makes them two (SQLite's case-blind comparison is ASCII
    only), and an import must bring back two. The associations name neither,
    so only this section's resolver decides - and it folded the second onto
    the first once the first was created, moving its line (review, 19/09;
    ImportContext.products)."""

    KEG, TWIN = "BIÈRE DU PONT FÛT 20L", "BIÈRE DU PONT FûT 20L"

    def setUp(self):
        super().setUp()
        self.brewery = make_supplier(code="BRASSERIE_ESSAI", name="Brasserie Essai")
        invoice = make_invoice(self.brewery, invoice_number="BRA-001")
        make_invoice_line(invoice, make_product(self.brewery, self.KEG), quantity=2, total_ht="160.00")
        make_invoice_line(invoice, make_product(self.brewery, self.TWIN), quantity=3, total_ht="90.00")

    def state(self) -> list:
        return sorted(
            (product.raw_name, [str(quantity.normalize()) for quantity in product.invoice_lines.values_list(
                "quantity", flat=True
            )])
            for product in Product.objects.filter(supplier=self.brewery)
        )

    def test_they_stay_two_each_with_its_line_through_a_clear_and_an_import(self):
        before = self.state()
        self.assertEqual(before, [(self.KEG, ["2"]), (self.TWIN, ["3"])])
        reader = export_archive({"fournisseurs", "factures"})
        self.addCleanup(reader.close)
        for strategy in (REPLACE, MERGE):
            with self.subTest(strategy=strategy):
                with self.captureOnCommitCallbacks(execute=True):
                    run_clear({"factures"}, preview=False, closed=False)
                # Gone with their documents: the import has to create both.
                self.assertEqual(self.state(), [])
                import_archive(reader, {"fournisseurs": MERGE, "factures": strategy})
                self.assertEqual(self.state(), before)
