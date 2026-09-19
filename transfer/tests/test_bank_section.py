"""« Banque » (§7.7, §10.2): the bank's lines, their links to invoices, the
ignore rules and the payee names learnt.

What these guard is the owner's decisions: a link made by hand, a line
unlinked, a line declared « pas de facture ». Nothing rebuilds them, so a
round trip must bring every one back exactly, a merge must never overwrite
one, and a link whose invoice is not here must be said, never guessed.

Every name, amount and label below is invented.
"""

import hashlib
import itertools
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from bank import reconcile
from bank.models import BankTransaction, CounterpartyAlias, IgnoreRule, InvoicePayment
from invoices.deletion import delete_invoice
from invoices.models import Invoice, Supplier
from tests.factories import make_invoice, make_supplier
from transfer import keys, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections import bank as section
from transfer.sections.bank import (
    ALIASES,
    ENTITIES,
    OPERATIONS,
    PAYMENTS,
    RECONCILE_NOTE,
    RULES,
    BankSection,
)
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
CARD, DEBIT, TRANSFER = BankTransaction.Kind.CARD, BankTransaction.Kind.DEBIT, BankTransaction.Kind.TRANSFER
MANUAL, AUTO = InvoicePayment.Method.MANUAL, InvoicePayment.Method.AUTO
UTC = dt_timezone.utc

_counter = itertools.count(1)


def make_line(day, counterparty, amount, *, kind=CARD, settled=False, no_invoice=False) -> BankTransaction:
    """A statement line as bank.reconcile.import_statement leaves it, with an
    import moment in the past: a round trip that forgot to restore it would
    show today's instead."""
    n = next(_counter)
    line = BankTransaction.objects.create(
        account="****0042",
        operation_date=day,
        value_date=day,
        card_date=day - timedelta(days=2) if kind == CARD else None,
        bank_type="FACTURE CARTE" if kind == CARD else "PRLV SEPA",
        kind=kind,
        label=f"OPERATION {day:%d%m%y} {counterparty} REF{n:04d}",
        counterparty=counterparty,
        amount=Decimal(amount),
        fingerprint=hashlib.sha256(f"releve-essai|{n}".encode()).hexdigest(),
        no_invoice=no_invoice,
        settled_by_hand=settled,
    )
    BankTransaction.objects.filter(pk=line.pk).update(imported_at=datetime(2026, 8, 1, 9, 0, n % 60, 250000, tzinfo=UTC))
    line.refresh_from_db()
    return line


def pay(line, invoice, method=MANUAL) -> InvoicePayment:
    payment = InvoicePayment.objects.create(transaction=line, invoice=invoice, method=method)
    InvoicePayment.objects.filter(pk=payment.pk).update(created_at=datetime(2026, 8, 3, 18, 45, 12, tzinfo=UTC))
    payment.refresh_from_db()
    return payment


def make_rule(pattern, description="", is_active=True) -> IgnoreRule:
    rule = IgnoreRule.objects.create(pattern=pattern, description=description, is_active=is_active)
    IgnoreRule.objects.filter(pk=rule.pk).update(created_at=datetime(2026, 7, 20, 8, 30, tzinfo=UTC))
    rule.refresh_from_db()
    return rule


def wipe_bank() -> None:
    InvoicePayment.objects.all().delete()
    BankTransaction.objects.all().delete()
    CounterpartyAlias.objects.all().delete()
    IgnoreRule.objects.all().delete()


def links() -> set[tuple[str, str, str]]:
    """(line fingerprint, invoice number, method) for every payment."""
    return set(
        InvoicePayment.objects.values_list("transaction__fingerprint", "invoice__invoice_number", "method")
    )


def bank_report(run):
    return run.section("banque")


def tally(run, entity):
    return bank_report(run).tallies[entity]


class BankData:
    """Two suppliers, four invoices, and one line for every decision the bank
    page lets a person take."""

    def setUp(self):
        super().setUp()
        self.grocer = make_supplier(code="EPICERIE", name="Épicerie des Lilas")
        self.hardware = make_supplier(code="QUINCAILLE", name="Quincaillerie du Nord")
        self.invoice_a = make_invoice(supplier=self.grocer, invoice_number="T-101", invoice_date=date(2026, 7, 1))
        self.invoice_b = make_invoice(supplier=self.hardware, invoice_number="Q-2002", invoice_date=date(2026, 6, 20))
        self.invoice_c = make_invoice(supplier=self.grocer, invoice_number="T-102", invoice_date=date(2026, 7, 14))
        self.invoice_d = make_invoice(supplier=self.grocer, invoice_number="T-103", invoice_date=date(2026, 7, 14))
        # Linked by hand.
        self.manual = make_line(date(2026, 7, 2), "EPICERIE LILAS", "-12.30", settled=True)
        pay(self.manual, self.invoice_a, MANUAL)
        # Linked by the automatic pass: nobody touched it.
        self.auto = make_line(date(2026, 7, 9), "QUINCAILLERIE NORD", "-120.35", kind=DEBIT)
        pay(self.auto, self.invoice_b, AUTO)
        # A person unlinked it: settled, and no payment.
        self.unlinked = make_line(date(2026, 7, 10), "BOULANGERIE", "-3.40", settled=True)
        # « Pas de facture ».
        self.no_invoice = make_line(date(2026, 7, 5), "URSSAF", "-450.00", kind=DEBIT, settled=True, no_invoice=True)
        # Money in.
        self.income = make_line(date(2026, 7, 31), "CLIENT SOIREE", "500.00", kind=TRANSFER)
        # One debit for two deliveries.
        self.double = make_line(date(2026, 7, 15), "EPICERIE LILAS", "-40.00", settled=True)
        pay(self.double, self.invoice_c)
        pay(self.double, self.invoice_d)
        CounterpartyAlias.objects.create(supplier=self.hardware, name="QNORD")
        make_rule("URSSAF", "Cotisations")
        make_rule("PRET LOCAL", "Prêt du local", is_active=False)

    def export(self) -> ArchiveReader:
        reader = export_archive({"banque"})
        self.addCleanup(reader.close)
        return reader

    def forged(self, change) -> ArchiveReader:
        """This database's export with banque.json changed by `change`."""
        reader = ArchiveReader(forge(self.export(), banque=change))
        self.addCleanup(reader.close)
        return reader

    def record(self, payload, line) -> dict:
        return next(item for item in payload["transactions"] if item["fingerprint"] == line.fingerprint)


class ContractTests(TestCase):
    def test_every_field_is_exported_or_said_why_not(self):
        # A field added to one of these models later cannot be left out of
        # the archive in silence.
        for model, exported in section.EXPORTED.items():
            with self.subTest(model=model.__name__):
                concrete = {field.name for field in model._meta.concrete_fields}
                self.assertEqual(concrete, set(exported) | set(section.NOT_EXPORTED[model]))
                self.assertFalse(set(exported) & set(section.NOT_EXPORTED[model]))

    def test_the_section_is_registered_under_its_key(self):
        self.assertIsInstance(registry.get("banque"), BankSection)

    def test_count_of_an_empty_bank(self):
        self.assertEqual(BankSection().count(), dict.fromkeys(ENTITIES, 0))


class ExportTests(BankData, TestCase):
    def test_counts(self):
        expected = {OPERATIONS: 6, PAYMENTS: 4, RULES: 2, ALIASES: 1}
        self.assertEqual(BankSection().count(), expected)
        self.assertEqual(self.export().section("banque").counts, expected)

    def test_every_line_keeps_the_decisions_taken_on_it(self):
        payload = self.export().section("banque").payload()
        self.assertEqual(
            [(item["counterparty"], item["settled_by_hand"], item["no_invoice"]) for item in payload["transactions"]],
            [
                ("EPICERIE LILAS", True, False),
                ("QUINCAILLERIE NORD", False, False),
                ("BOULANGERIE", True, False),
                ("URSSAF", True, True),
                ("CLIENT SOIREE", False, False),
                ("EPICERIE LILAS", True, False),
            ],
        )
        manual = self.record(payload, self.manual)
        self.assertEqual(manual["amount"], "-12.30")
        self.assertEqual(manual["card_date"], "2026-06-30")
        self.assertEqual(manual["imported_at"], self.manual.imported_at.astimezone(UTC).isoformat())
        self.assertEqual(
            manual["payments"],
            [
                {
                    "invoice": {"supplier": "EPICERIE", "number": "T-101", "sha256": "", "file_sha256": "", "occurrence": 0},
                    "method": "MANUAL",
                    "created_at": "2026-08-03T18:45:12+00:00",
                }
            ],
        )
        self.assertEqual(self.record(payload, self.auto)["payments"][0]["method"], "AUTO")
        self.assertEqual(payload["aliases"], [{"supplier": "QUINCAILLE", "name": "QNORD"}])
        self.assertEqual(
            payload["rules"],
            [
                {"pattern": "URSSAF", "description": "Cotisations", "is_active": True, "created_at": "2026-07-20T08:30:00+00:00"},
                {"pattern": "PRET LOCAL", "description": "Prêt du local", "is_active": False, "created_at": "2026-07-20T08:30:00+00:00"},
            ],
        )

    def test_invoices_are_named_by_key_only_never_by_pk(self):
        payload = self.export().section("banque").payload()

        def names(value):
            if isinstance(value, dict):
                for name, item in value.items():
                    yield name
                    yield from names(item)
            elif isinstance(value, list):
                for item in value:
                    yield from names(item)

        found = set(names(payload))
        self.assertFalse({"id", "pk"} & found)
        self.assertFalse({name for name in found if name.endswith("_id")})
        for record in payload["transactions"]:
            for payment in record["payments"]:
                self.assertEqual(set(payment["invoice"]), set(keys.KEY_FIELDS))

    def test_the_suppliers_it_names_come_with_their_names(self):
        payload = self.export().section("banque").payload()
        self.assertEqual(payload["supplier_names"], {"EPICERIE": "Épicerie des Lilas", "QUINCAILLE": "Quincaillerie du Nord"})


class RoundTripTests(BankData, TestCase):
    def assert_empty(self):
        self.assertEqual(BankSection().count(), dict.fromkeys(ENTITIES, 0))

    def assert_decisions_back(self):
        self.assertEqual(
            links(),
            {
                (self.manual.fingerprint, "T-101", MANUAL),
                (self.auto.fingerprint, "Q-2002", AUTO),
                (self.double.fingerprint, "T-102", MANUAL),
                (self.double.fingerprint, "T-103", MANUAL),
            },
        )
        unlinked = BankTransaction.objects.get(fingerprint=self.unlinked.fingerprint)
        self.assertTrue(unlinked.settled_by_hand)
        self.assertFalse(unlinked.payments.exists())
        self.assertTrue(BankTransaction.objects.get(fingerprint=self.no_invoice.fingerprint).no_invoice)
        # Restored after the insert, not the moment of the import.
        self.assertEqual(BankTransaction.objects.get(fingerprint=self.manual.fingerprint).imported_at, self.manual.imported_at)
        self.assertEqual(
            set(InvoicePayment.objects.values_list("created_at", flat=True)),
            {datetime(2026, 8, 3, 18, 45, 12, tzinfo=UTC)},
        )

    def test_merge(self):
        before, after = round_trip({"banque"}, MERGE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_decisions_back()

    def test_replace(self):
        before, after = round_trip({"banque"}, REPLACE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_decisions_back()

    def test_lines_of_one_day_keep_their_order(self):
        # The page sorts by date, then id: two lines of one morning must come
        # back in the statement's order.
        first = make_line(date(2026, 7, 20), "BOULANGERIE", "-1.20")
        second = make_line(date(2026, 7, 20), "BOULANGERIE", "-1.20")
        round_trip({"banque"}, MERGE)
        self.assertEqual(
            list(BankTransaction.objects.filter(operation_date=date(2026, 7, 20)).values_list("fingerprint", flat=True)),
            [second.fingerprint, first.fingerprint],
        )


class IdempotenceTests(BankData, TestCase):
    def assert_nothing_moves(self, run):
        report = bank_report(run)
        expected = {OPERATIONS: 6, PAYMENTS: 4, RULES: 2, ALIASES: 1}
        for entity, number in expected.items():
            with self.subTest(entity=entity):
                counted = report.tallies[entity]
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, number))
        self.assertEqual((report.conflicts, report.skipped, report.kept), ([], [], []))
        self.assertNotIn(RECONCILE_NOTE, report.notes)
        self.assertFalse(run.affected())

    def test_merging_its_own_export_changes_nothing(self):
        before = db_fingerprint()
        self.assert_nothing_moves(import_archive(self.export(), MERGE))
        self.assertEqual(db_fingerprint(), before)

    def test_replacing_with_its_own_export_changes_nothing(self):
        before = db_fingerprint()
        self.assert_nothing_moves(import_archive(self.export(), REPLACE))
        self.assertEqual(db_fingerprint(), before)

    def test_the_same_records_made_at_other_moments_are_unchanged(self):
        # The same statement imported on another computer a day later: the
        # moments differ, the records do not (§6.4). A record equal on every
        # compared field is not written, not even its moment.
        reader = self.export()
        BankTransaction.objects.update(imported_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
        InvoicePayment.objects.update(created_at=datetime(2026, 9, 1, 12, 1, tzinfo=UTC))
        IgnoreRule.objects.update(created_at=datetime(2026, 9, 1, 12, 2, tzinfo=UTC))
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                self.assert_nothing_moves(import_archive(reader, strategy))
                self.assertEqual(db_fingerprint(), before)


class MergeAndReplaceTests(BankData, TestCase):
    """The database moved on after the export: of each kind, one record
    changed, one only here, one only in the archive."""

    def setUp(self):
        super().setUp()
        self.before = BankSection().snapshot()
        self.reader = self.export()
        # Lines: one changed, one only here, one only in the archive. The
        # changed one has no link: a line whose record differs keeps its
        # links as they are, and the link only in the archive (below) is on
        # a line equal to the archive's.
        BankTransaction.objects.filter(pk=self.no_invoice.pk).update(counterparty="URSSAF PARIS")
        self.extra = make_line(date(2026, 8, 1), "LIBRAIRIE", "-8.90")
        self.income.delete()
        # Links: one changed (its method), one only here, one only in the archive.
        InvoicePayment.objects.filter(invoice=self.invoice_a).update(method=AUTO)
        self.invoice_e = make_invoice(supplier=self.grocer, invoice_number="T-104", invoice_date=date(2026, 7, 9))
        pay(self.unlinked, self.invoice_e)
        InvoicePayment.objects.filter(invoice=self.invoice_b).delete()
        # Rules and names.
        IgnoreRule.objects.filter(pattern="URSSAF").update(description="URSSAF trimestre")
        make_rule("LOYER", "Loyer")
        IgnoreRule.objects.filter(pattern="PRET LOCAL").delete()
        CounterpartyAlias.objects.create(supplier=self.grocer, name="EPI LILAS")
        CounterpartyAlias.objects.filter(name="QNORD").delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        operations = tally(run, OPERATIONS)
        self.assertEqual((operations.created, operations.updated, operations.deleted, operations.unchanged), (1, 0, 0, 4))
        payments = tally(run, PAYMENTS)
        self.assertEqual((payments.created, payments.updated, payments.deleted, payments.unchanged), (1, 0, 0, 2))
        rules = tally(run, RULES)
        self.assertEqual((rules.created, rules.updated, rules.deleted, rules.unchanged), (1, 0, 0, 0))
        aliases = tally(run, ALIASES)
        self.assertEqual((aliases.created, aliases.deleted), (1, 0))
        self.assertEqual(
            bank_report(run).conflicts,
            [
                "Règle « URSSAF » : différente dans l'archive (nom) — gardée telle quelle",
                (
                    "Opération du 05/07/2026 (URSSAF PARIS, -450,00 €) : différente dans l'archive "
                    "(bénéficiaire) — gardée telle quelle"
                ),
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : le lien vers Épicerie des Lilas n° T-101 du "
                    "01/07/2026 est « Automatique » ici, « À la main » dans l'archive — gardé tel quel"
                ),
            ],
        )
        # Kept as they are here.
        self.assertEqual(BankTransaction.objects.get(pk=self.no_invoice.pk).counterparty, "URSSAF PARIS")
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_a).method, AUTO)
        self.assertEqual(IgnoreRule.objects.get(pattern="URSSAF").description, "URSSAF trimestre")
        # Only here: untouched.
        self.assertTrue(BankTransaction.objects.filter(pk=self.extra.pk).exists())
        self.assertTrue(InvoicePayment.objects.filter(invoice=self.invoice_e, transaction=self.unlinked).exists())
        self.assertTrue(IgnoreRule.objects.filter(pattern="LOYER").exists())
        self.assertTrue(CounterpartyAlias.objects.filter(name="EPI LILAS").exists())
        # Only in the archive: created, with its import moment.
        self.assertEqual(BankTransaction.objects.get(fingerprint=self.income.fingerprint).imported_at, self.income.imported_at)
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_b).method, AUTO)
        self.assertTrue(IgnoreRule.objects.filter(pattern="PRET LOCAL", is_active=False).exists())
        self.assertTrue(CounterpartyAlias.objects.filter(name="QNORD", supplier=self.hardware).exists())
        self.assertIn(RECONCILE_NOTE, bank_report(run).notes)
        # A merge updates and deletes nothing: no safety export is needed.
        self.assertFalse(run.affected())

    def test_replace_makes_the_bank_exactly_the_archive(self):
        run = import_archive(self.reader, REPLACE)
        operations = tally(run, OPERATIONS)
        self.assertEqual((operations.created, operations.updated, operations.deleted, operations.unchanged), (1, 1, 1, 4))
        payments = tally(run, PAYMENTS)
        self.assertEqual((payments.created, payments.updated, payments.deleted, payments.unchanged), (1, 1, 1, 2))
        rules = tally(run, RULES)
        self.assertEqual((rules.created, rules.updated, rules.deleted, rules.unchanged), (1, 1, 1, 0))
        aliases = tally(run, ALIASES)
        self.assertEqual((aliases.created, aliases.deleted, aliases.unchanged), (1, 1, 0))
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(BankSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {"banque"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before, files = db_fingerprint(), media_listing()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)
        self.assertEqual(callbacks, [])
        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertNotEqual(db_fingerprint(), before)

    def test_a_merge_preview_says_what_the_merge_does(self):
        before = db_fingerprint()
        preview = import_archive(self.reader, MERGE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(preview.outcome(), import_archive(self.reader, MERGE).outcome())


class LinkTests(BankData, TestCase):
    """How a link of the archive meets the links this database has."""

    def test_merge_onto_a_line_paying_another_invoice_here_is_a_conflict(self):
        reader = self.export()
        InvoicePayment.objects.filter(transaction=self.manual).delete()
        other = make_invoice(supplier=self.grocer, invoice_number="T-150", invoice_date=date(2026, 7, 1))
        pay(self.manual, other)
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : paie ici Épicerie des Lilas n° T-150 du "
                    "01/07/2026, dans l'archive Épicerie des Lilas n° T-101 du 01/07/2026 — gardée telle quelle"
                )
            ],
        )
        self.assertEqual(list(self.manual.payments.values_list("invoice__invoice_number", flat=True)), ["T-150"])
        self.assertFalse(InvoicePayment.objects.filter(invoice=self.invoice_a).exists())

    def test_an_invoice_paid_here_by_another_line_is_a_conflict(self):
        reader = self.export()
        InvoicePayment.objects.filter(invoice=self.invoice_a).update(transaction=self.unlinked)
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : la facture Épicerie des Lilas n° T-101 du "
                    "01/07/2026 est déjà payée par l'opération du 10/07/2026 — lien ignoré"
                )
            ],
        )
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_a).transaction, self.unlinked)

    def test_a_missing_link_of_a_line_nobody_settled_is_added(self):
        # A line the automatic pass linked and no person touched: its link
        # missing here is a blank, the archive's fills it.
        reader = self.export()
        InvoicePayment.objects.filter(invoice=self.invoice_b).delete()
        run = import_archive(reader, MERGE)
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(tally(run, PAYMENTS).created, 1)
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_b).transaction, self.auto)

    def test_a_link_undone_by_hand_after_the_export_is_not_made_again(self):
        # Unlinked on the bank page, the line stays « réglée à la main » with
        # no payment: its record equals the archive's but for the link, and
        # merging the export put the link back with nothing said.
        reader = self.export()
        reconcile.unlink(self.manual)
        run = import_archive(reader, MERGE)
        self.assertFalse(self.manual.payments.exists())
        self.assertFalse(InvoicePayment.objects.filter(invoice=self.invoice_a).exists())
        self.assertEqual(tally(run, PAYMENTS).created, 0)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : réglée à la main ici sans payer Épicerie "
                    "des Lilas n° T-101 du 01/07/2026, qu'elle paie dans l'archive — gardée telle quelle"
                )
            ],
        )
        # Nothing on the line tells an undone link from one lost with its
        # invoice: the report says both, and the way back for the second.
        self.assertIn(section.UNDONE_NOTE, bank_report(run).notes)

    def test_a_line_whose_record_differs_keeps_its_links_as_they_are(self):
        # Linked by the automatic pass in the archive, unlinked by hand here:
        # « réglée à la main » differs, a conflict « gardée telle quelle » -
        # and the archive's link came back all the same, under a « réglée à
        # la main » the automatic pass never revisits: a state neither
        # database held, the invoice paid by a line a person said did not.
        reader = self.export()
        reconcile.unlink(self.auto)
        run = import_archive(reader, MERGE)
        self.auto.refresh_from_db()
        self.assertTrue(self.auto.settled_by_hand)
        self.assertFalse(self.auto.payments.exists())
        self.assertEqual(tally(run, PAYMENTS).created, 0)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 09/07/2026 (QUINCAILLERIE NORD, -120,35 €) : différente dans l'archive "
                    "(« réglée à la main ») — gardée telle quelle, sans le lien de l'archive vers Quincaillerie du "
                    "Nord n° Q-2002 du 20/06/2026"
                )
            ],
        )

    def test_a_link_left_off_a_line_paying_several_is_not_made_again(self):
        # The bank page unlinks a line whole: linked again to one delivery
        # only, the line says it no longer pays the other.
        reader = self.export()
        reconcile.unlink(self.double)
        reconcile.link(self.double, [self.invoice_c])
        run = import_archive(reader, MERGE)
        self.assertEqual(list(self.double.payments.values_list("invoice__invoice_number", flat=True)), ["T-102"])
        self.assertFalse(InvoicePayment.objects.filter(invoice=self.invoice_d).exists())
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 15/07/2026 (EPICERIE LILAS, -40,00 €) : réglée à la main ici sans payer Épicerie "
                    "des Lilas n° T-103 du 14/07/2026, qu'elle paie dans l'archive — gardée telle quelle"
                )
            ],
        )

    def test_a_merge_never_undoes_a_pas_de_facture_taken_here(self):
        reader = self.export()
        reconcile.mark_no_invoice(self.manual)
        run = import_archive(reader, MERGE)
        # One conflict: a line kept as it is keeps its links as they are,
        # and the one it does not get is named there.
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : différente dans l'archive "
                    "(« pas de facture ») — gardée telle quelle, sans le lien de l'archive vers Épicerie des Lilas "
                    "n° T-101 du 01/07/2026"
                ),
            ],
        )
        self.manual.refresh_from_db()
        self.assertTrue(self.manual.no_invoice)
        self.assertFalse(self.manual.payments.exists())
        # « Remplacer » is asked for: the archive's decision comes back.
        import_archive(reader, REPLACE)
        self.manual.refresh_from_db()
        self.assertFalse(self.manual.no_invoice)
        self.assertEqual(self.manual.payments.get().invoice, self.invoice_a)

    def test_a_merge_never_resets_a_line_settled_by_hand(self):
        reader = self.export()
        BankTransaction.objects.filter(pk=self.auto.pk).update(settled_by_hand=True)
        run = import_archive(reader, MERGE)
        self.assertIn(
            "Opération du 09/07/2026 (QUINCAILLERIE NORD, -120,35 €) : différente dans l'archive "
            "(« réglée à la main ») — gardée telle quelle",
            bank_report(run).conflicts,
        )
        self.assertTrue(BankTransaction.objects.get(pk=self.auto.pk).settled_by_hand)

    def test_replace_moves_a_link_between_two_lines(self):
        reader = self.export()
        InvoicePayment.objects.filter(invoice=self.invoice_a).update(transaction=self.unlinked, method=AUTO)
        run = import_archive(reader, REPLACE)
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_a).transaction, self.manual)
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_a).method, MANUAL)
        self.assertFalse(self.unlinked.payments.exists())
        payments = tally(run, PAYMENTS)
        # By key (line, invoice): the one here goes, the archive's comes.
        self.assertEqual((payments.created, payments.updated, payments.deleted, payments.unchanged), (1, 0, 1, 3))

    def test_replace_moves_a_link_away_from_a_line_it_deletes(self):
        reader = self.export()
        InvoicePayment.objects.filter(invoice=self.invoice_a).delete()
        stray = make_line(date(2026, 7, 3), "EPICERIE LILAS", "-12.30")
        pay(stray, self.invoice_a, AUTO)
        run = import_archive(reader, REPLACE)
        self.assertFalse(BankTransaction.objects.filter(pk=stray.pk).exists())
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_a).transaction, self.manual)
        self.assertEqual(tally(run, OPERATIONS).deleted, 1)
        self.assertEqual((tally(run, PAYMENTS).created, tally(run, PAYMENTS).deleted), (1, 1))

    def test_a_link_to_an_invoice_absent_here_is_skipped_and_said(self):
        reader = self.export()
        wipe_bank()
        self.invoice_a.delete()
        run = import_archive(reader, MERGE)
        self.assertIn(
            "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : facture absente : Épicerie des Lilas n° T-101",
            bank_report(run).skipped,
        )
        # The line itself comes, decision included; its other lines' links too.
        manual = BankTransaction.objects.get(fingerprint=self.manual.fingerprint)
        self.assertTrue(manual.settled_by_hand)
        self.assertFalse(manual.payments.exists())
        self.assertEqual(InvoicePayment.objects.count(), 3)

    def test_codes_that_differ_between_the_databases_are_matched_by_name(self):
        reader = self.export()
        wipe_bank()
        Supplier.objects.filter(pk=self.grocer.pk).update(code="EPICERIE_2")
        Supplier.objects.filter(pk=self.hardware.pk).update(code="QUINCAILLE_2")
        run = import_archive(reader, MERGE)
        self.assertEqual(bank_report(run).skipped, [])
        self.assertEqual(InvoicePayment.objects.count(), 4)
        self.assertEqual(self.manual.fingerprint, InvoicePayment.objects.get(invoice=self.invoice_a).transaction.fingerprint)
        self.assertTrue(CounterpartyAlias.objects.filter(supplier=self.hardware, name="QNORD").exists())

    def test_two_invoices_answering_one_key_skip_the_link(self):
        by_file = make_invoice(supplier=self.grocer, source_sha256="ab" * 32, invoice_date=date(2026, 7, 1))
        # A document without a number (the factory always gives one).
        Invoice.objects.filter(pk=by_file.pk).update(invoice_number="")
        key = {"supplier": "EPICERIE", "number": "T-101", "sha256": "ab" * 32, "file_sha256": "", "occurrence": 0}

        def change(payload):
            self.record(payload, self.manual)["payments"][0]["invoice"] = key
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertIn(
            "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : deux documents différents répondent à la facture "
            f"Épicerie des Lilas n° T-101 (n° T-101 et le fichier de Épicerie des Lilas n° {by_file.pk} du 01/07/2026) "
            "— lien ignoré",
            bank_report(run).skipped,
        )
        self.assertFalse(InvoicePayment.objects.filter(invoice__in=[self.invoice_a, by_file]).exists())

    def test_an_invoice_found_by_its_file_is_linked_and_said(self):
        # The number differs between the databases (a stand-in rewritten on
        # one side); the stored sha finds the document.
        self.invoice_a.source_sha256 = "cd" * 32
        self.invoice_a.save(update_fields=["source_sha256"])
        reader = self.export()
        wipe_bank()
        self.invoice_a.invoice_number = "20260701-12.30"
        self.invoice_a.save(update_fields=["invoice_number"])
        run = import_archive(reader, MERGE)
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_a).transaction.fingerprint, self.manual.fingerprint)
        self.assertIn("rapproché par son fichier : n° 20260701-12.30 ici, n° T-101 dans l'archive", bank_report(run).notes)


class InvoicesGoneTests(BankData, TestCase):
    """Deleting invoices cascades their payments; the lines stay « réglées à
    la main » (as deletion.delete_invoice leaves them). Imported with their
    invoices, in one run, the bank puts the links back. Imported on its own
    once the invoices are back, it cannot tell a link lost with its invoice
    from one a person undid: the lines settled by hand are left to a person,
    and said."""

    def setUp(self):
        super().setUp()
        if not (registry.is_registered("factures") and registry.is_registered("fournisseurs")):
            self.skipTest("les parties « factures » et « fournisseurs » (voie A) ne sont pas encore installées")

    def export_with_invoices(self) -> ArchiveReader:
        reader = export_archive({"fournisseurs", "factures", "banque"})
        self.addCleanup(reader.close)
        return reader

    def expected_links(self):
        return {
            (self.manual.fingerprint, "T-101", MANUAL),
            (self.auto.fingerprint, "Q-2002", AUTO),
            (self.double.fingerprint, "T-102", MANUAL),
            (self.double.fingerprint, "T-103", MANUAL),
        }

    def test_links_lost_with_their_invoices_come_back_with_them(self):
        reader = self.export_with_invoices()
        for invoice in (self.invoice_a, self.invoice_b, self.invoice_c, self.invoice_d):
            with self.captureOnCommitCallbacks(execute=True):
                delete_invoice(invoice)
        self.assertEqual(InvoicePayment.objects.count(), 0)
        self.assertEqual(BankTransaction.objects.count(), 6)
        self.manual.refresh_from_db()
        self.assertTrue(self.manual.settled_by_hand)

        run = import_archive(reader, MERGE)
        self.assertEqual(links(), self.expected_links())
        self.assertEqual(tally(run, PAYMENTS).created, 4)
        self.assertEqual(tally(run, OPERATIONS).unchanged, 6)
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertNotIn(section.UNDONE_NOTE, bank_report(run).notes)

    def test_a_link_lost_with_its_invoice_comes_back_beside_the_others(self):
        # One debit for two deliveries, one of them deleted: the line still
        # pays the other, and gets the second back with its invoice.
        reader = self.export_with_invoices()
        with self.captureOnCommitCallbacks(execute=True):
            delete_invoice(self.invoice_d)
        run = import_archive(reader, MERGE)
        self.assertEqual(links(), self.expected_links())
        self.assertEqual(tally(run, PAYMENTS).created, 1)
        self.assertEqual(bank_report(run).conflicts, [])

    def test_clearing_the_invoices_section_then_importing_back(self):
        reader = self.export_with_invoices()
        with self.captureOnCommitCallbacks(execute=True):
            cleared = run_clear({"factures"}, preview=False, closed=False)
        self.assertEqual(InvoicePayment.objects.count(), 0)
        self.assertEqual(BankTransaction.objects.count(), 6)
        self.manual.refresh_from_db()
        self.assertTrue(self.manual.settled_by_hand)
        self.assertEqual(tally(cleared, PAYMENTS).deleted, 4)

        run = import_archive(reader, MERGE)
        self.assertEqual(links(), self.expected_links())
        self.assertEqual(bank_report(run).conflicts, [])

    def test_invoices_imported_back_on_their_own_leave_the_links_to_a_person(self):
        # « Effacer factures », then the invoices imported back alone, then
        # the bank: the invoices were here when the bank's run started, and
        # a line settled by hand without its link looks exactly like one a
        # person unlinked.
        reader = self.export_with_invoices()
        with self.captureOnCommitCallbacks(execute=True):
            run_clear({"factures"}, preview=False, closed=False)
        import_archive(reader, {"fournisseurs": MERGE, "factures": MERGE})
        run = import_archive(reader, {"banque": MERGE})
        # Linked by the automatic pass and never settled: a blank, filled.
        self.assertEqual(links(), {(self.auto.fingerprint, "Q-2002", AUTO)})
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : réglée à la main ici sans payer Épicerie "
                    "des Lilas n° T-101 du 01/07/2026, qu'elle paie dans l'archive — gardée telle quelle"
                ),
                (
                    "Opération du 15/07/2026 (EPICERIE LILAS, -40,00 €) : réglée à la main ici sans payer Épicerie "
                    "des Lilas n° T-102 du 14/07/2026, qu'elle paie dans l'archive — gardée telle quelle"
                ),
                (
                    "Opération du 15/07/2026 (EPICERIE LILAS, -40,00 €) : réglée à la main ici sans payer Épicerie "
                    "des Lilas n° T-103 du 14/07/2026, qu'elle paie dans l'archive — gardée telle quelle"
                ),
            ],
        )
        self.assertIn(section.UNDONE_NOTE, bank_report(run).notes)
        # The way back the note gives: « Remplacer » makes the bank's links
        # the archive's.
        import_archive(reader, {"banque": REPLACE})
        self.assertEqual(links(), self.expected_links())


class CheckTests(BankData, TestCase):
    """One per « Checks » item of §7.7, and the archive's structure."""

    def test_a_rule_matching_every_label_is_refused(self):
        def change(payload):
            payload["rules"].append({"pattern": "URSSAF|", "description": "trop large", "is_active": True})
            return payload

        reader = self.forged(change)
        run = import_archive(reader, MERGE)
        self.assertIn(
            "Règle « URSSAF| » : Ce motif correspond à n'importe quelle opération : précisez-le.",
            bank_report(run).skipped,
        )
        self.assertFalse(IgnoreRule.objects.filter(pattern="URSSAF|").exists())

    def test_a_rule_that_is_no_regular_expression_is_refused(self):
        def change(payload):
            payload["rules"].append({"pattern": "LOYER (", "description": "", "is_active": True})
            return payload

        run = import_archive(self.forged(change), MERGE)
        (skipped,) = bank_report(run).skipped
        self.assertTrue(skipped.startswith("Règle « LOYER ( » : Expression régulière invalide"), skipped)
        self.assertFalse(IgnoreRule.objects.filter(pattern="LOYER (").exists())

    def test_a_rule_without_a_pattern_is_refused(self):
        def change(payload):
            payload["rules"].append({"pattern": "  ", "description": "vide"})
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(bank_report(run).skipped, ["Règle sans motif"])

    def test_a_name_learnt_for_an_unknown_supplier_is_skipped(self):
        def change(payload):
            payload["aliases"].append({"supplier": "NULLEPART", "name": "SUMUP LE KIOSQUE"})
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(
            bank_report(run).skipped, ["Nom de payeur « SUMUP LE KIOSQUE » : fournisseur inconnu (« NULLEPART »)"]
        )
        self.assertFalse(CounterpartyAlias.objects.filter(name="SUMUP LE KIOSQUE").exists())

    def test_a_line_with_an_amount_that_is_no_number_is_skipped_and_the_others_come(self):
        def change(payload):
            self.record(payload, self.manual)["amount"] = "-12,30"
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        (skipped,) = bank_report(run).skipped
        self.assertIn("« amount » : « -12,30 » n'est pas un nombre", skipped)
        self.assertEqual(BankTransaction.objects.count(), 5)
        self.assertFalse(BankTransaction.objects.filter(fingerprint=self.manual.fingerprint).exists())

    def test_a_new_line_without_its_date_is_skipped(self):
        def change(payload):
            del self.record(payload, self.income)["operation_date"]
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped, [f"Opération {self.income.fingerprint[:12]}… : « operation_date » : valeur manquante"]
        )

    def test_a_line_without_fingerprint_is_skipped(self):
        def change(payload):
            del self.record(payload, self.income)["fingerprint"]
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(bank_report(run).skipped, ["Opération sans empreinte lisible : empreinte manquante"])
        self.assertEqual(BankTransaction.objects.count(), 5)

    def test_a_link_with_an_unknown_method_is_skipped(self):
        def change(payload):
            self.record(payload, self.manual)["payments"][0]["method"] = "CHEQUE"
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped,
            [
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : lien vers Épicerie des Lilas n° T-101 du "
                    "01/07/2026 : « method » : valeur inconnue (« CHEQUE »)"
                )
            ],
        )
        self.assertFalse(InvoicePayment.objects.filter(invoice=self.invoice_a).exists())

    def test_a_link_to_an_unreadable_key_is_skipped(self):
        def change(payload):
            self.record(payload, self.manual)["payments"][0]["invoice"] = {"supplier": "EPICERIE", "number": ["T-101"]}
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped,
            ["Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : lien vers une facture illisible"],
        )

    def test_the_same_line_twice_in_the_archive_is_created_once(self):
        def change(payload):
            payload["transactions"].append(dict(self.record(payload, self.income)))
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(bank_report(run).skipped, [f"Opération {self.income.fingerprint[:12]}… : en double dans l'archive"])
        self.assertEqual(BankTransaction.objects.count(), 6)

    def test_an_unknown_field_is_said_once(self):
        def change(payload):
            for record in payload["transactions"]:
                record["humeur"] = "calme"
            payload["couleur"] = "bleu"
            return payload

        run = import_archive(self.forged(change), MERGE)
        notes = bank_report(run).notes
        self.assertEqual(notes.count("champ inconnu ignoré : opérations › humeur"), 1)
        self.assertIn("champ inconnu ignoré : banque.json › couleur", notes)

    def test_an_archive_without_its_lists_is_refused_before_anything_is_written(self):
        for name, value in (("transactions", {}), ("rules", None), ("aliases", ["QNORD"])):
            with self.subTest(name=name):

                def change(payload, name=name, value=value):
                    payload[name] = value
                    return payload

                reader = self.forged(change)
                wipe_bank()
                before = db_fingerprint()
                with self.assertRaises(ArchiveError):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)

    def test_links_that_are_no_list_refuse_the_archive(self):
        def change(payload):
            self.record(payload, self.manual)["payments"] = {"invoice": "T-101"}
            return payload

        with self.assertRaisesMessage(ArchiveError, "les liens d'une opération ne sont pas une liste"):
            import_archive(self.forged(change), MERGE)

    def test_replace_keeps_a_line_it_cannot_read_links_included(self):
        def change(payload):
            self.record(payload, self.manual)["amount"] = "beaucoup"
            return payload

        reader = self.forged(change)
        run = import_archive(reader, REPLACE)
        self.assertTrue(BankTransaction.objects.filter(pk=self.manual.pk).exists())
        self.assertEqual(self.manual.payments.get().invoice, self.invoice_a)
        self.assertEqual(tally(run, OPERATIONS).deleted, 0)
        self.assertEqual(len(bank_report(run).skipped), 1)

    def test_replace_keeps_the_links_of_a_line_whose_record_does_not_say_them(self):
        def change(payload):
            del self.record(payload, self.manual)["payments"]
            return payload

        reader = self.forged(change)
        run = import_archive(reader, REPLACE)
        self.assertEqual(self.manual.payments.get().invoice, self.invoice_a)
        self.assertEqual(tally(run, PAYMENTS).deleted, 0)


class ImportMakesNoDecisionTests(BankData, TestCase):
    def test_no_automatic_matching_runs(self):
        reader = self.export()
        wipe_bank()
        with mock.patch("bank.reconcile.reconcile") as matching:
            run = import_archive(reader, MERGE)
        matching.assert_not_called()
        self.assertEqual(InvoicePayment.objects.count(), 4)
        self.assertIn(RECONCILE_NOTE, bank_report(run).notes)


class ClearTests(BankData, TestCase):
    def test_a_preview_changes_nothing(self):
        before = db_fingerprint()
        preview = run_clear({"banque"}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        confirmed = run_clear({"banque"}, preview=False)
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_everything_of_the_bank_goes_and_nothing_else(self):
        run = run_clear({"banque"}, preview=False)
        self.assertEqual(BankSection().count(), dict.fromkeys(ENTITIES, 0))
        deleted = {entity: tally(run, entity).deleted for entity in ENTITIES}
        self.assertEqual(deleted, {OPERATIONS: 6, PAYMENTS: 4, RULES: 2, ALIASES: 1})
        self.assertIn(section.CLEAR_NOTE, bank_report(run).notes)
        self.assertEqual(run.affected(), {"banque"})
        # The invoices and suppliers the bank pointed at are not the bank's.
        self.assertEqual(self.grocer.invoices.count(), 3)
        self.assertTrue(Supplier.objects.filter(pk=self.hardware.pk).exists())

    def test_clearing_the_bank_clears_only_the_bank(self):
        self.assertEqual(registry.closure({"banque"}, "clear"), {"banque"})
