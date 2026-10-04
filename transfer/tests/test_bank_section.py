"""« Banque » (§7.7, §10.2): the bank's lines, their links to invoices, the
payee names learnt, the payers retained on « Entrées d'argent », and the
treasury's points and adjustments (« Trésorerie »).

What these guard is the owner's decisions: a link made by hand, a line
unlinked, a line declared « pas de facture », what a credit is in the till
(« En caisse », on the line or for its payer), the balances he typed and the
adjustments he made. Nothing rebuilds them, so a round trip must bring every
one back exactly, a merge must never overwrite one, and a link whose invoice
is not here must be said, never guessed.

How the bank's statements are read - the formats, the recognition rules,
the ignore rules - is « Règles de la banque » since 02/10/2026
(test_bank_rules_section.py). What is left here of them is the boundary: an
archive of « Banque » neither holds nor touches them, and one written before
that day, whose banque.json still held them, imports its lines as it always
did (`archive.CARVED`).

Every name, amount and label below is invented.
"""

import hashlib
import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps
from django.test import TestCase

from bank import reconcile
from bank.income import income_for
from bank.models import (
    BankTransaction,
    CounterpartyAlias,
    IgnoreRule,
    IncomePayer,
    IncomeSource,
    InvoicePayment,
    OperationRule,
    StatementFormat,
    TreasuryAdjustment,
    TreasuryCheckpoint,
)
from common import DateRange
from invoices.deletion import delete_invoice
from invoices.models import Invoice, Supplier
from tests.factories import make_invoice, make_supplier
from transfer import archive, codec, keys, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections import bank as section
from transfer.sections import bank_rules
from transfer.sections.bank import (
    ADJUSTMENTS,
    ALIASES,
    CHECKPOINTS,
    ENTITIES,
    OPERATIONS,
    PAYERS,
    PAYMENTS,
    RECONCILE_NOTE,
    TREASURY_CLEAR_NOTE,
    BankSection,
)
from transfer.sections.bank_rules import BankRulesSection
from transfer.sections.base import Strategy
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    media_listing,
    round_trip,
)
from transfer.tests.test_bank_rules_section import DJANGO_ENGLISH, RulesData, wipe_rules

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
CARD, DEBIT, TRANSFER = BankTransaction.Kind.CARD, BankTransaction.Kind.DEBIT, BankTransaction.Kind.TRANSFER
MANUAL, AUTO = InvoicePayment.Method.MANUAL, InvoicePayment.Method.AUTO
# What a credit is in the till (« En caisse »).
AUTOMATIC, TILL_CARD, TILL_CASH = IncomeSource.AUTOMATIC, IncomeSource.CARD, IncomeSource.CASH
TILL_CHEQUE, TILL_CREDIT, VOUCHER, NOT_A_SALE = (
    IncomeSource.CHEQUE,
    IncomeSource.CREDIT,
    IncomeSource.VOUCHER,
    IncomeSource.OTHER,
)
PAYER_MOMENT = datetime(2026, 7, 28, 11, 5, 30, 125000, tzinfo=UTC)
#: What separates an amount's thousands in the report (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"
#: The lists of banque.json that are « Règles de la banque »'s now.
RULE_LISTS = set(bank_rules.TOP_LEVEL)
#: When the treasury's points and adjustments of BankData were typed.
CHECKPOINT_MOMENT = datetime(2026, 8, 2, 19, 20, 10, 375000, tzinfo=UTC)
ADJUSTMENT_MOMENT = datetime(2026, 8, 2, 19, 25, 40, 625000, tzinfo=UTC)
#: A day the tests hold « today » to, after every day BankData dates.
TODAY = date(2026, 9, 30)
#: What « today » the import reads (`_apply_checkpoints`, `_apply_adjustments`).
TODAY_READ = "transfer.sections.bank.timezone.localdate"

_counter = itertools.count(1)


def make_line(
    day, counterparty, amount, *, kind=CARD, settled=False, no_invoice=False, income_source=AUTOMATIC
) -> BankTransaction:
    """A statement line as bank.reconcile.import_statement leaves it, with an
    import moment in the past: a round trip that forgot to restore it would
    show today's instead. `income_source` is what a person said a credit is
    in the till."""
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
        income_source=income_source,
    )
    BankTransaction.objects.filter(pk=line.pk).update(
        imported_at=datetime(2026, 8, 1, 9, 0, n % 60, 250000, tzinfo=UTC)
    )
    line.refresh_from_db()
    return line


def pay(line, invoice, method=MANUAL) -> InvoicePayment:
    payment = InvoicePayment.objects.create(transaction=line, invoice=invoice, method=method)
    InvoicePayment.objects.filter(pk=payment.pk).update(created_at=datetime(2026, 8, 3, 18, 45, 12, tzinfo=UTC))
    payment.refresh_from_db()
    return payment


def make_payer(key, source) -> IncomePayer:
    """A payer retained on « Entrées d'argent » (`key` in the form
    `bank.income.payer_key` makes), made at a moment in the past: a round
    trip that forgot to restore it would show today's instead."""
    payer = IncomePayer.objects.create(key=key, source=source)
    IncomePayer.objects.filter(pk=payer.pk).update(created_at=PAYER_MOMENT)
    payer.refresh_from_db()
    return payer


def make_checkpoint(day, balance, moment=CHECKPOINT_MOMENT) -> TreasuryCheckpoint:
    """A balance typed on « Trésorerie » (checked by the model as the page's
    save is), made at a moment in the past: a round trip that forgot to
    restore it would show today's instead."""
    checkpoint = TreasuryCheckpoint(date=day, balance=Decimal(balance))
    checkpoint.full_clean()
    checkpoint.save()
    TreasuryCheckpoint.objects.filter(pk=checkpoint.pk).update(created_at=moment)
    checkpoint.refresh_from_db()
    return checkpoint


def make_adjustment(day, amount, reason="", moment=ADJUSTMENT_MOMENT) -> TreasuryAdjustment:
    """An adjustment made on « Trésorerie », its reference drawn by the
    model as the page's is, made at a moment in the past."""
    adjustment = TreasuryAdjustment(date=day, amount=Decimal(amount), reason=reason)
    adjustment.full_clean()
    adjustment.save()
    TreasuryAdjustment.objects.filter(pk=adjustment.pk).update(created_at=moment)
    adjustment.refresh_from_db()
    return adjustment


def wipe_bank() -> None:
    """Everything the section holds, as its clear leaves it: an archive
    importing into it has to bring it back. The formats and the rules stay:
    they are « Règles de la banque »'s."""
    InvoicePayment.objects.all().delete()
    BankTransaction.objects.all().delete()
    CounterpartyAlias.objects.all().delete()
    IncomePayer.objects.all().delete()
    TreasuryCheckpoint.objects.all().delete()
    TreasuryAdjustment.objects.all().delete()


def without_treasury(snapshot: dict) -> dict:
    """A « Banque » snapshot less the treasury's lists: what an archive
    written before bank/0008 can bring back."""
    return {name: rows for name, rows in snapshot.items() if not name.startswith("treasury_")}


def checkpoints() -> dict:
    """day → balance of every treasury point."""
    return dict(TreasuryCheckpoint.objects.values_list("date", "balance"))


def adjustments() -> dict:
    """reference → (day, amount, reason) of every treasury adjustment."""
    return {
        reference: (day, amount, reason)
        for reference, day, amount, reason in TreasuryAdjustment.objects.values_list(
            "reference", "date", "amount", "reason"
        )
    }


def links() -> set[tuple[str, str, str]]:
    """(line fingerprint, invoice number, method) for every payment."""
    return set(InvoicePayment.objects.values_list("transaction__fingerprint", "invoice__invoice_number", "method"))


def payers() -> dict[str, str]:
    """key → source of every payer retained."""
    return dict(IncomePayer.objects.values_list("key", "source"))


def bank_report(run):
    return run.section("banque")


def tally(run, entity):
    return bank_report(run).tallies[entity]


class BankData:
    """Two suppliers, four invoices, one line for every decision the bank
    page lets a person take, the two ways « Entrées d'argent » lets one say
    what a credit is in the till - on the line, and for its payer - and two
    balances typed on « Trésorerie », one of them an overdraft, with two
    adjustments, one without a reason."""

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
        # Money in: a private party's deposit, said to be an « Avoir » of the
        # till on its own line.
        self.income = make_line(date(2026, 7, 31), "CLIENT SOIREE", "500.00", kind=TRANSFER, income_source=TILL_CREDIT)
        # One debit for two deliveries.
        self.double = make_line(date(2026, 7, 15), "EPICERIE LILAS", "-40.00", settled=True)
        pay(self.double, self.invoice_c)
        pay(self.double, self.invoice_d)
        CounterpartyAlias.objects.create(supplier=self.hardware, name="QNORD")
        # Two payers retained: a payment terminal whose payouts print no
        # « TOTAL ENCAISSE », and a contribution that is no sale.
        make_payer("TERMINAL EXEMPLE", TILL_CARD)
        make_payer("ASSOCIATION EXEMPLE", NOT_A_SALE)
        # « Trésorerie »: two balances read on the bank's site, and two
        # adjustments settling them. An import works no gap out between them.
        self.june = make_checkpoint(date(2026, 6, 30), "2450.00")
        self.july = make_checkpoint(date(2026, 7, 31), "-120.40")
        self.fees = make_adjustment(date(2026, 7, 31), "-35.00", "Frais non relevés")
        self.found = make_adjustment(date(2026, 7, 15), "12.30")

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

    def checkpoint_record(self, payload, day) -> dict:
        return next(item for item in payload["treasury_checkpoints"] if item["date"] == day.isoformat())

    def adjustment_record(self, payload, adjustment) -> dict:
        return next(item for item in payload["treasury_adjustments"] if item["reference"] == adjustment.reference)


class ContractTests(TestCase):
    def test_every_field_is_exported_or_said_why_not(self):
        # A field added to one of these models later cannot be left out of
        # the archive in silence.
        for model, exported in section.EXPORTED.items():
            with self.subTest(model=model.__name__):
                concrete = {field.name for field in model._meta.concrete_fields}
                self.assertEqual(concrete, set(exported) | set(section.NOT_EXPORTED[model]))
                self.assertFalse(set(exported) & set(section.NOT_EXPORTED[model]))

    def test_every_model_of_the_bank_is_in_exactly_one_of_the_two_sections(self):
        """The guard above only sees the models EXPORTED names: a model added
        to bank/ and left out of the archive passed it in silence, and an
        « Effacer » then a restore lost its rows for good. Each is carried by
        « Banque » or by « Règles de la banque », never both, never neither -
        the treasury's points and adjustments, a person's decisions about
        money, by « Banque »."""
        ours, theirs = set(section.EXPORTED), set(bank_rules.EXPORTED)
        self.assertFalse(ours & theirs)
        self.assertEqual(ours | theirs, set(apps.get_app_config("bank").get_models()))
        self.assertLessEqual({TreasuryCheckpoint, TreasuryAdjustment}, ours)
        self.assertEqual(set(section.NOT_EXPORTED), ours)
        self.assertEqual(set(bank_rules.NOT_EXPORTED), theirs)

    def test_the_section_is_registered_under_its_key(self):
        self.assertIsInstance(registry.get("banque"), BankSection)

    def test_count_of_an_empty_bank(self):
        # Nothing imported, nothing decided - and nothing seeded either: the
        # format bank/0007 seeds and the rules bank/0006 seeds are « Règles
        # de la banque »'s.
        self.assertEqual(BankSection().count(), dict.fromkeys(ENTITIES, 0))

    def test_it_counts_its_lines_and_names_and_no_rule(self):
        # No label an older archive gave the rules: this version's
        # « Banque » exported alone would be read as carrying them
        # (`archive.carved`).
        self.assertEqual(ENTITIES, (OPERATIONS, PAYMENTS, ALIASES, PAYERS, CHECKPOINTS, ADJUSTMENTS))
        self.assertEqual(len(set(ENTITIES)), len(ENTITIES))
        self.assertFalse({old for _label, old in archive.CARVED["regles_banque"].counts} & set(ENTITIES))

    def test_the_treasury_has_counts_of_its_own(self):
        self.assertEqual((CHECKPOINTS, ADJUSTMENTS), ("points de trésorerie", "ajustements de trésorerie"))
        self.assertIn("treasury_checkpoints", section.TOP_LEVEL)
        self.assertIn("treasury_adjustments", section.TOP_LEVEL)

    def test_no_list_and_no_model_of_the_rules_is_its_own(self):
        self.assertFalse(RULE_LISTS & set(section.TOP_LEVEL))
        self.assertFalse(set(bank_rules.EXPORTED) & set(section.EXPORTED))


class ExportTests(BankData, TestCase):
    def test_counts(self):
        expected = {OPERATIONS: 6, PAYMENTS: 4, ALIASES: 1, PAYERS: 2, CHECKPOINTS: 2, ADJUSTMENTS: 2}
        self.assertEqual(BankSection().count(), expected)
        self.assertEqual(self.export().section("banque").counts, expected)

    def test_the_treasury_points_are_exported_by_day(self):
        # Signed (an overdraft), the moment they were typed, nothing else.
        payload = self.export().section("banque").payload()
        self.assertEqual(
            payload["treasury_checkpoints"],
            [
                {"date": "2026-06-30", "balance": "2450.00", "created_at": "2026-08-02T19:20:10.375000+00:00"},
                {"date": "2026-07-31", "balance": "-120.40", "created_at": "2026-08-02T19:20:10.375000+00:00"},
            ],
        )

    def test_the_treasury_adjustments_are_exported_by_day_with_their_reference(self):
        # By day: an export of the same rows is the same file in every
        # database. The reference is the key, never the id.
        payload = self.export().section("banque").payload()
        self.assertEqual(
            payload["treasury_adjustments"],
            [
                {
                    "reference": self.found.reference,
                    "date": "2026-07-15",
                    "amount": "12.30",
                    "reason": "",
                    "created_at": "2026-08-02T19:25:40.625000+00:00",
                },
                {
                    "reference": self.fees.reference,
                    "date": "2026-07-31",
                    "amount": "-35.00",
                    "reason": "Frais non relevés",
                    "created_at": "2026-08-02T19:25:40.625000+00:00",
                },
            ],
        )
        self.assertRegex(self.fees.reference, r"^[0-9a-f]{16}$")

    def test_no_treasury_point_or_adjustment_is_an_empty_list_never_a_missing_one(self):
        # Absent, the lists read as an archive written before bank/0008
        # (« not said »), and « Remplacer » would keep this database's.
        TreasuryCheckpoint.objects.all().delete()
        TreasuryAdjustment.objects.all().delete()
        reader = self.export()
        payload = reader.section("banque").payload()
        self.assertEqual((payload["treasury_checkpoints"], payload["treasury_adjustments"]), ([], []))
        self.assertEqual(
            (reader.section("banque").counts[CHECKPOINTS], reader.section("banque").counts[ADJUSTMENTS]), (0, 0)
        )

    def test_it_holds_the_lines_and_names_and_never_the_rules(self):
        # Exported alone, « Banque » takes no format and no rule of this
        # database: they are « Règles de la banque »'s, which it only
        # recommends.
        payload = self.export().section("banque").payload()
        self.assertEqual(set(payload), set(section.TOP_LEVEL))
        self.assertFalse(RULE_LISTS & set(payload))

    def test_every_line_says_what_it_is_in_the_till(self):
        # « Automatique » is said too: blank, the line follows its payer or
        # the rules.
        payload = self.export().section("banque").payload()
        self.assertEqual(
            [(item["counterparty"], item["income_source"]) for item in payload["transactions"]],
            [
                ("EPICERIE LILAS", ""),
                ("QUINCAILLERIE NORD", ""),
                ("BOULANGERIE", ""),
                ("URSSAF", ""),
                ("CLIENT SOIREE", "credit"),
                ("EPICERIE LILAS", ""),
            ],
        )

    def test_the_payers_retained_are_exported_by_key(self):
        payload = self.export().section("banque").payload()
        self.assertEqual(
            payload["income_payers"],
            [
                {"key": "ASSOCIATION EXEMPLE", "source": "other", "created_at": "2026-07-28T11:05:30.125000+00:00"},
                {"key": "TERMINAL EXEMPLE", "source": "card", "created_at": "2026-07-28T11:05:30.125000+00:00"},
            ],
        )

    def test_no_payer_is_an_empty_list_never_a_missing_one(self):
        # Exported with nothing retained, the list is said empty: absent, an
        # archive reads as written before payers existed (« not said »).
        IncomePayer.objects.all().delete()
        payload = self.export().section("banque").payload()
        self.assertEqual(payload["income_payers"], [])
        self.assertEqual(self.export().section("banque").counts[PAYERS], 0)

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
                    "invoice": {
                        "supplier": "EPICERIE",
                        "number": "T-101",
                        "sha256": "",
                        "file_sha256": "",
                        "occurrence": 0,
                    },
                    "method": "MANUAL",
                    "created_at": "2026-08-03T18:45:12+00:00",
                }
            ],
        )
        self.assertEqual(self.record(payload, self.auto)["payments"][0]["method"], "AUTO")
        self.assertEqual(payload["aliases"], [{"supplier": "QUINCAILLE", "name": "QNORD"}])

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
                # Not one of them was typed by hand without a file: no moment.
                self.assertEqual(set(payment["invoice"]), set(keys.KEY_FIELDS) - {"moment"})

    def test_the_suppliers_it_names_come_with_their_names(self):
        payload = self.export().section("banque").payload()
        self.assertEqual(
            payload["supplier_names"], {"EPICERIE": "Épicerie des Lilas", "QUINCAILLE": "Quincaillerie du Nord"}
        )


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
        # What a credit is in the till, on its line and for its payer.
        self.assertEqual(
            dict(BankTransaction.objects.exclude(income_source="").values_list("fingerprint", "income_source")),
            {self.income.fingerprint: TILL_CREDIT},
        )
        self.assertEqual(payers(), {"TERMINAL EXEMPLE": TILL_CARD, "ASSOCIATION EXEMPLE": NOT_A_SALE})
        # Restored after the insert, not the moment of the import.
        self.assertEqual(
            BankTransaction.objects.get(fingerprint=self.manual.fingerprint).imported_at, self.manual.imported_at
        )
        self.assertEqual(
            set(InvoicePayment.objects.values_list("created_at", flat=True)),
            {datetime(2026, 8, 3, 18, 45, 12, tzinfo=UTC)},
        )
        self.assertEqual(set(IncomePayer.objects.values_list("created_at", flat=True)), {PAYER_MOMENT})

    def assert_treasury_back(self):
        # The balances typed, the overdraft signed; each adjustment under its
        # own reference, never one drawn anew; each with the moment it was
        # made, not the import's.
        self.assertEqual(checkpoints(), {date(2026, 6, 30): Decimal("2450.00"), date(2026, 7, 31): Decimal("-120.40")})
        self.assertEqual(
            adjustments(),
            {
                self.fees.reference: (date(2026, 7, 31), Decimal("-35.00"), "Frais non relevés"),
                self.found.reference: (date(2026, 7, 15), Decimal("12.30"), ""),
            },
        )
        self.assertEqual(set(TreasuryCheckpoint.objects.values_list("created_at", flat=True)), {CHECKPOINT_MOMENT})
        self.assertEqual(set(TreasuryAdjustment.objects.values_list("created_at", flat=True)), {ADJUSTMENT_MOMENT})

    def test_merge(self):
        before, after = round_trip({"banque"}, MERGE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_decisions_back()
        self.assert_treasury_back()

    def test_replace(self):
        before, after = round_trip({"banque"}, REPLACE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_decisions_back()
        self.assert_treasury_back()

    def test_lines_of_one_day_keep_their_order(self):
        # The page sorts by date, then id: two lines of one morning must come
        # back in the statement's order.
        first = make_line(date(2026, 7, 20), "BOULANGERIE", "-1.20")
        second = make_line(date(2026, 7, 20), "BOULANGERIE", "-1.20")
        round_trip({"banque"}, MERGE)
        self.assertEqual(
            list(
                BankTransaction.objects.filter(operation_date=date(2026, 7, 20)).values_list("fingerprint", flat=True)
            ),
            [second.fingerprint, first.fingerprint],
        )


class IdempotenceTests(BankData, TestCase):
    def assert_nothing_moves(self, run):
        report = bank_report(run)
        expected = {OPERATIONS: 6, PAYMENTS: 4, ALIASES: 1, PAYERS: 2, CHECKPOINTS: 2, ADJUSTMENTS: 2}
        self.assertEqual(set(expected), set(ENTITIES))
        for entity, number in expected.items():
            with self.subTest(entity=entity):
                counted = report.tallies[entity]
                self.assertEqual(
                    (counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, number)
                )
        self.assertEqual(list(report.tallies), list(ENTITIES))
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
        IncomePayer.objects.update(created_at=datetime(2026, 9, 1, 12, 3, tzinfo=UTC))
        TreasuryCheckpoint.objects.update(created_at=datetime(2026, 9, 1, 12, 6, tzinfo=UTC))
        TreasuryAdjustment.objects.update(created_at=datetime(2026, 9, 1, 12, 7, tzinfo=UTC))
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
        # Names.
        CounterpartyAlias.objects.create(supplier=self.grocer, name="EPI LILAS")
        CounterpartyAlias.objects.filter(name="QNORD").delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        operations = tally(run, OPERATIONS)
        self.assertEqual(
            (operations.created, operations.updated, operations.deleted, operations.unchanged), (1, 0, 0, 4)
        )
        payments = tally(run, PAYMENTS)
        self.assertEqual((payments.created, payments.updated, payments.deleted, payments.unchanged), (1, 0, 0, 2))
        aliases = tally(run, ALIASES)
        self.assertEqual((aliases.created, aliases.deleted), (1, 0))
        self.assertEqual(
            bank_report(run).conflicts,
            [
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
        # Only here: untouched.
        self.assertTrue(BankTransaction.objects.filter(pk=self.extra.pk).exists())
        self.assertTrue(InvoicePayment.objects.filter(invoice=self.invoice_e, transaction=self.unlinked).exists())
        self.assertTrue(CounterpartyAlias.objects.filter(name="EPI LILAS").exists())
        # Only in the archive: created, with its import moment.
        self.assertEqual(
            BankTransaction.objects.get(fingerprint=self.income.fingerprint).imported_at, self.income.imported_at
        )
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_b).method, AUTO)
        self.assertTrue(CounterpartyAlias.objects.filter(name="QNORD", supplier=self.hardware).exists())
        self.assertIn(RECONCILE_NOTE, bank_report(run).notes)
        # A merge updates and deletes nothing: no safety export is needed.
        self.assertFalse(run.affected())

    def test_replace_makes_the_bank_exactly_the_archive(self):
        run = import_archive(self.reader, REPLACE)
        operations = tally(run, OPERATIONS)
        self.assertEqual(
            (operations.created, operations.updated, operations.deleted, operations.unchanged), (1, 1, 1, 4)
        )
        payments = tally(run, PAYMENTS)
        self.assertEqual((payments.created, payments.updated, payments.deleted, payments.unchanged), (1, 1, 1, 2))
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


class GroupedAmountTests(TestCase):
    """The report names a line by its amount as Banque shows it, its
    thousands grouped by a no-break space (the owner, 01/10/2026); the
    archive keeps the figure as the statement gave it."""

    def test_the_report_groups_the_thousands_and_the_archive_does_not(self):
        line = make_line(date(2026, 7, 5), "BAILLEUR", "-12345.67", kind=DEBIT, settled=True, no_invoice=True)
        reader = export_archive({"banque"})
        self.addCleanup(reader.close)
        payload = reader.section("banque").payload()
        record = next(item for item in payload["transactions"] if item["fingerprint"] == line.fingerprint)
        self.assertEqual(record["amount"], "-12345.67")
        BankTransaction.objects.filter(pk=line.pk).update(counterparty="BAILLEUR PARIS")
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    f"Opération du 05/07/2026 (BAILLEUR PARIS, -12{NBSP}345,67 €) : différente dans l'archive "
                    "(bénéficiaire) — gardée telle quelle"
                )
            ],
        )

    def test_below_a_thousand_nothing_is_added(self):
        self.assertEqual(section._euros(Decimal("-12.30")), "-12,30 €")
        self.assertEqual(section._euros(Decimal("1000")), f"1{NBSP}000,00 €")


class CategoryTests(BankData, TestCase):
    """What a spending was FOR, said on its line, is a decision like any
    other here: a statement imported again brings the line back and not one
    word of what a person said about it. (What an ignore rule says of its
    payments travels with the rule: test_bank_rules_section.py.)
    """

    def setUp(self):
        super().setUp()
        BankTransaction.objects.filter(pk=self.unlinked.pk).update(category="Travaux")

    def line(self) -> BankTransaction:
        return BankTransaction.objects.get(fingerprint=self.unlinked.fingerprint)

    def test_a_category_typed_on_a_line_comes_back_from_the_archive(self):
        reader = self.export()
        wipe_bank()
        import_archive(reader, MERGE)
        self.assertEqual(self.line().category, "Travaux")

    def test_a_category_changed_here_is_a_conflict_kept_whole(self):
        """Merging an archive must never rewrite what somebody decided since
        - and a category is exactly that kind of decision."""
        reader = self.export()
        BankTransaction.objects.filter(pk=self.unlinked.pk).update(category="Entretien")
        run = import_archive(reader, MERGE)
        self.assertEqual(self.line().category, "Entretien")
        self.assertIn("catégorie", " ".join(bank_report(run).conflicts))


class IncomeSourceTests(BankData, TestCase):
    """What a credit is in the till (« En caisse »), said on its own line: a
    decision like a category - a statement imported again brings the line
    back and not one word of it - so it rides with the line, and a merge
    never changes it."""

    INCOME = "Opération du 31/07/2026 (CLIENT SOIREE, 500,00 €)"

    def line(self) -> BankTransaction:
        return BankTransaction.objects.get(fingerprint=self.income.fingerprint)

    def test_what_a_credit_is_comes_back_from_the_archive(self):
        reader = self.export()
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(self.line().income_source, TILL_CREDIT)
        self.assertEqual((bank_report(run).conflicts, bank_report(run).skipped), ([], []))

    def test_a_credit_said_otherwise_here_is_a_conflict_kept_whole(self):
        reader = self.export()
        BankTransaction.objects.filter(pk=self.income.pk).update(income_source=VOUCHER)
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [f"{self.INCOME} : différente dans l'archive (« en caisse ») — gardée telle quelle"],
        )
        self.assertEqual(self.line().income_source, VOUCHER)
        operations = tally(run, OPERATIONS)
        self.assertEqual(
            (operations.created, operations.updated, operations.deleted, operations.unchanged), (0, 0, 0, 5)
        )
        self.assertFalse(run.affected())

    def test_replace_puts_the_archives_word_back(self):
        reader = self.export()
        BankTransaction.objects.filter(pk=self.income.pk).update(income_source=VOUCHER)
        run = import_archive(reader, REPLACE)
        self.assertEqual(self.line().income_source, TILL_CREDIT)
        operations = tally(run, OPERATIONS)
        self.assertEqual(
            (operations.created, operations.updated, operations.deleted, operations.unchanged), (0, 1, 0, 5)
        )
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(run.affected(), {"banque"})

    def test_a_credit_said_here_after_the_export_is_not_undone_by_a_merge(self):
        # The archive says « Automatique »: blank is a value it states, and
        # the merge does not read it as « nothing said ».
        refund = make_line(date(2026, 7, 20), "ASSOCIATION EXEMPLE", "120.00", kind=TRANSFER)
        reader = self.export()
        BankTransaction.objects.filter(pk=refund.pk).update(income_source=NOT_A_SALE)
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 20/07/2026 (ASSOCIATION EXEMPLE, 120,00 €) : différente dans l'archive "
                    "(« en caisse ») — gardée telle quelle"
                )
            ],
        )
        refund.refresh_from_db()
        self.assertEqual(refund.income_source, NOT_A_SALE)
        # « Remplacer » is asked for: the archive's « Automatique » comes back.
        import_archive(reader, REPLACE)
        refund.refresh_from_db()
        self.assertEqual(refund.income_source, AUTOMATIC)

    def test_a_new_line_saying_an_unknown_source_is_skipped_with_its_reason(self):
        def change(payload):
            self.record(payload, self.income)["income_source"] = "ticket"
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped,
            [f"Opération {self.income.fingerprint[:12]}… : « income_source » : valeur inconnue (« ticket »)"],
        )
        self.assertEqual(BankTransaction.objects.count(), 5)
        self.assertFalse(BankTransaction.objects.filter(fingerprint=self.income.fingerprint).exists())

    def test_a_line_here_whose_record_says_no_source_is_kept_whole(self):
        # « Remplacer » included: a record that cannot be read says nothing
        # about the line, which is neither rewritten nor pruned.
        reasons = {
            "ticket": "« income_source » : valeur inconnue (« ticket »)",
            "CREDIT": "« income_source » : valeur inconnue (« CREDIT »)",
            None: "« income_source » : valeur manquante",
            3: "« income_source » : texte attendu (« 3 »)",
        }
        for value, reason in reasons.items():
            with self.subTest(value=value):

                def change(payload, value=value):
                    self.record(payload, self.income)["income_source"] = value
                    return payload

                run = import_archive(self.forged(change), REPLACE)
                self.assertEqual(bank_report(run).skipped, [f"{self.INCOME} : {reason}"])
                self.assertEqual(self.line().income_source, TILL_CREDIT)
                self.assertEqual(tally(run, OPERATIONS).deleted, 0)


class PayerTests(BankData, TestCase):
    """The payers retained moved on after the export: one said otherwise
    here, one only here, one only in the archive. Merged like a rule: one
    changed here is a conflict, kept."""

    def setUp(self):
        super().setUp()
        self.before = BankSection().snapshot()
        self.reader = self.export()
        IncomePayer.objects.filter(key="TERMINAL EXEMPLE").update(source=TILL_CASH)
        make_payer("CAISSE EXEMPLE", TILL_CHEQUE)
        IncomePayer.objects.filter(key="ASSOCIATION EXEMPLE").delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        counted = tally(run, PAYERS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 0, 0, 0))
        self.assertEqual(
            bank_report(run).conflicts,
            ["Payeur retenu « TERMINAL EXEMPLE » : « Espèces » ici, « Carte » dans l'archive — gardé tel quel"],
        )
        self.assertEqual(
            payers(), {"TERMINAL EXEMPLE": TILL_CASH, "CAISSE EXEMPLE": TILL_CHEQUE, "ASSOCIATION EXEMPLE": NOT_A_SALE}
        )
        # Only in the archive: created, with the moment it was retained.
        self.assertEqual(IncomePayer.objects.get(key="ASSOCIATION EXEMPLE").created_at, PAYER_MOMENT)
        # Nothing else of the bank moved.
        self.assertEqual(tally(run, OPERATIONS).unchanged, 6)
        # A merge updates and deletes nothing: no safety export is needed.
        self.assertFalse(run.affected())

    def test_replace_makes_the_payers_exactly_the_archives(self):
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, PAYERS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 1, 1, 0))
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(payers(), {"TERMINAL EXEMPLE": TILL_CARD, "ASSOCIATION EXEMPLE": NOT_A_SALE})
        self.assertEqual(BankSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {"banque"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_an_empty_list_forgets_every_payer_under_replace_only(self):
        # Said empty, the archive retains nobody: « Remplacer » forgets them
        # all, « Fusionner » adds nothing and forgets nothing.
        def change(payload):
            payload["income_payers"] = []
            return payload

        reader = self.forged(change)
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, PAYERS).deleted, 0)
        self.assertEqual(set(payers()), {"TERMINAL EXEMPLE", "CAISSE EXEMPLE"})
        run = import_archive(reader, REPLACE)
        self.assertEqual(tally(run, PAYERS).deleted, 2)
        self.assertEqual(payers(), {})


class PayerCreatedByTheRunTests(BankData, TestCase):
    """A payer a merge creates brings the choices its lines held beside it in
    the archive (`_took_its_payers_choice`): there they were one decision -
    « every transfer of this terminal is a card payout, but this one » - and
    the payer alone would decide the line the archive kept apart from it, a
    state neither database held. Only onto a line saying nothing here: a
    choice made here, or a payer already retained here, is a decision this
    database holds."""

    REFUND = "Opération du 22/07/2026 (TERMINAL EXEMPLE, 35,00 €)"

    def setUp(self):
        super().setUp()
        # A fee refund from the payment terminal: its label prints no « TOTAL
        # ENCAISSE », so no rule recognises it, and in the archive it is kept
        # « Pas une vente » beside its payer « Carte ».
        self.refund = make_line(date(2026, 7, 22), "TERMINAL EXEMPLE", "35.00", kind=TRANSFER, income_source=NOT_A_SALE)
        self.reader = self.export()
        # Here, neither was ever said: no payer retained, nothing on the line.
        IncomePayer.objects.filter(key="TERMINAL EXEMPLE").delete()
        BankTransaction.objects.filter(pk=self.refund.pk).update(income_source=AUTOMATIC)

    def line(self) -> BankTransaction:
        return BankTransaction.objects.get(pk=self.refund.pk)

    def test_a_line_saying_nothing_here_takes_its_choice_with_its_payer(self):
        before = db_fingerprint()
        preview = import_archive(self.reader, MERGE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(self.line().income_source, NOT_A_SALE)
        self.assertEqual(payers(), {"TERMINAL EXEMPLE": TILL_CARD, "ASSOCIATION EXEMPLE": NOT_A_SALE})
        operations = tally(run, OPERATIONS)
        self.assertEqual(
            (operations.created, operations.updated, operations.deleted, operations.unchanged), (0, 1, 0, 6)
        )
        self.assertEqual((bank_report(run).conflicts, bank_report(run).skipped), ([], []))
        # Counted « modifiée »: the safety archive takes the bank.
        self.assertEqual(run.affected(), {"banque"})
        # Read as « Entrées d'argent » reads it: « Pas une vente », in no
        # payout.
        report = income_for(DateRange())
        self.assertNotIn(self.refund.pk, [row.entry.line.pk for row in report.payouts])
        self.assertIn(self.refund.pk, [entry.line.pk for entry in report.others])
        # Left saying nothing, the payer alone made it a card payout.
        BankTransaction.objects.filter(pk=self.refund.pk).update(income_source=AUTOMATIC)
        self.assertIn(self.refund.pk, [row.entry.line.pk for row in income_for(DateRange()).payouts])

    def test_a_line_that_also_differs_otherwise_is_a_conflict_that_takes_it_all_the_same(self):
        BankTransaction.objects.filter(pk=self.refund.pk).update(category="Remboursements")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    f"{self.REFUND} : différente dans l'archive (catégorie) — gardée telle quelle, son choix "
                    "« en caisse » repris avec son payeur"
                )
            ],
        )
        line = self.line()
        self.assertEqual((line.income_source, line.category), (NOT_A_SALE, "Remboursements"))

    def test_a_payer_already_retained_here_brings_nothing(self):
        make_payer("TERMINAL EXEMPLE", TILL_CARD)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [f"{self.REFUND} : différente dans l'archive (« en caisse ») — gardée telle quelle"],
        )
        self.assertEqual(self.line().income_source, AUTOMATIC)
        self.assertEqual(tally(run, PAYERS).created, 0)

    def test_a_choice_made_here_stays_a_conflict(self):
        BankTransaction.objects.filter(pk=self.refund.pk).update(income_source=VOUCHER)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [f"{self.REFUND} : différente dans l'archive (« en caisse ») — gardée telle quelle"],
        )
        self.assertEqual(self.line().income_source, VOUCHER)
        # The payer comes all the same.
        self.assertEqual(payers()["TERMINAL EXEMPLE"], TILL_CARD)

    def test_replace_writes_the_archives_line_as_before(self):
        BankTransaction.objects.filter(pk=self.refund.pk).update(category="Remboursements")
        run = import_archive(self.reader, REPLACE)
        self.assertEqual(bank_report(run).conflicts, [])
        line = self.line()
        self.assertEqual((line.income_source, line.category), (NOT_A_SALE, ""))
        operations = tally(run, OPERATIONS)
        self.assertEqual(
            (operations.created, operations.updated, operations.deleted, operations.unchanged), (0, 1, 0, 6)
        )


class CheckpointTests(BankData, TestCase):
    """The balances typed moved on after the export: one said otherwise
    here, one only here, one only in the archive. Merged like a payer: one
    changed here is a conflict, kept, both balances said."""

    def setUp(self):
        super().setUp()
        self.before = BankSection().snapshot()
        self.reader = self.export()
        TreasuryCheckpoint.objects.filter(pk=self.july.pk).update(balance=Decimal("-1120.40"))
        make_checkpoint(date(2026, 8, 31), "310.00")
        self.june.delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        counted = tally(run, CHECKPOINTS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 0, 0, 0))
        self.assertEqual(
            bank_report(run).conflicts,
            [f"Point de trésorerie du 31/07/2026 : -1{NBSP}120,40 € ici, -120,40 € dans l'archive — gardé tel quel"],
        )
        self.assertEqual(
            checkpoints(),
            {
                date(2026, 6, 30): Decimal("2450.00"),
                date(2026, 7, 31): Decimal("-1120.40"),
                date(2026, 8, 31): Decimal("310.00"),
            },
        )
        # Only in the archive: created, with the moment it was typed.
        self.assertEqual(TreasuryCheckpoint.objects.get(date=date(2026, 6, 30)).created_at, CHECKPOINT_MOMENT)
        # Nothing else of the bank moved.
        self.assertEqual(tally(run, ADJUSTMENTS).unchanged, 2)
        # A merge updates and deletes nothing: no safety export is needed.
        self.assertFalse(run.affected())

    def test_replace_makes_the_points_exactly_the_archives(self):
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, CHECKPOINTS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 1, 1, 0))
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(checkpoints(), {date(2026, 6, 30): Decimal("2450.00"), date(2026, 7, 31): Decimal("-120.40")})
        self.assertEqual(BankSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {"banque"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_an_empty_list_forgets_every_point_under_replace_only(self):
        # Said empty, the archive holds no balance: « Remplacer » forgets
        # them all, « Fusionner » adds nothing and forgets nothing. The
        # adjustments the archive still names stay: with no point they count
        # nowhere, which « Trésorerie » says.
        def change(payload):
            payload["treasury_checkpoints"] = []
            return payload

        reader = self.forged(change)
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, CHECKPOINTS).deleted, 0)
        self.assertEqual(set(checkpoints()), {date(2026, 7, 31), date(2026, 8, 31)})
        run = import_archive(reader, REPLACE)
        self.assertEqual(tally(run, CHECKPOINTS).deleted, 2)
        self.assertEqual(checkpoints(), {})
        self.assertEqual(set(adjustments()), {self.fees.reference, self.found.reference})


class AdjustmentTests(BankData, TestCase):
    """The adjustments moved on after the export: one changed here, one only
    here, one only in the archive. Merged like a point: one changed here is
    a conflict, kept - and named as « Trésorerie » shows it here."""

    def setUp(self):
        super().setUp()
        self.before = BankSection().snapshot()
        self.reader = self.export()
        TreasuryAdjustment.objects.filter(pk=self.fees.pk).update(amount=Decimal("-53.00"))
        self.extra = make_adjustment(date(2026, 8, 31), "8.00", "Dépôt non relevé")
        self.found.delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        counted = tally(run, ADJUSTMENTS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 0, 0, 0))
        self.assertEqual(
            bank_report(run).conflicts,
            ["Ajustement de trésorerie du 31/07/2026 (-53,00 €) : différent dans l'archive (montant) — gardé tel quel"],
        )
        self.assertEqual(
            adjustments(),
            {
                self.fees.reference: (date(2026, 7, 31), Decimal("-53.00"), "Frais non relevés"),
                self.extra.reference: (date(2026, 8, 31), Decimal("8.00"), "Dépôt non relevé"),
                # Only in the archive: created under its own reference, with
                # the moment it was made.
                self.found.reference: (date(2026, 7, 15), Decimal("12.30"), ""),
            },
        )
        self.assertEqual(TreasuryAdjustment.objects.get(reference=self.found.reference).created_at, ADJUSTMENT_MOMENT)
        self.assertEqual(tally(run, CHECKPOINTS).unchanged, 2)
        self.assertFalse(run.affected())

    def test_replace_makes_the_adjustments_exactly_the_archives(self):
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, ADJUSTMENTS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 1, 1, 0))
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(set(adjustments()), {self.fees.reference, self.found.reference})
        self.assertEqual(BankSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {"banque"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        confirmed = import_archive(self.reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_an_empty_list_forgets_every_adjustment_under_replace_only(self):
        def change(payload):
            payload["treasury_adjustments"] = []
            return payload

        reader = self.forged(change)
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, ADJUSTMENTS).deleted, 0)
        self.assertEqual(set(adjustments()), {self.fees.reference, self.extra.reference})
        run = import_archive(reader, REPLACE)
        self.assertEqual(tally(run, ADJUSTMENTS).deleted, 2)
        self.assertEqual(adjustments(), {})
        self.assertEqual(len(checkpoints()), 2)

    def test_an_adjustment_whose_day_and_reason_were_corrected_is_the_same_one(self):
        # Keyed by its reference, never by what it says: corrected here, it
        # is a conflict under « Fusionner » - never a second adjustment beside
        # it, counted twice - and written over in place under « Remplacer ».
        TreasuryAdjustment.objects.filter(pk=self.fees.pk).update(
            date=date(2026, 7, 30), amount=Decimal("-35.00"), reason="Frais de tenue de compte"
        )
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                "Ajustement de trésorerie du 30/07/2026 (-35,00 €) : différent dans l'archive (date, raison) — gardé tel quel"
            ],
        )
        self.assertEqual(TreasuryAdjustment.objects.filter(amount=Decimal("-35.00")).count(), 1)
        run = import_archive(self.reader, REPLACE)
        self.assertEqual(tally(run, ADJUSTMENTS).updated, 1)
        fees = TreasuryAdjustment.objects.get(pk=self.fees.pk)
        self.assertEqual((fees.date, fees.reason), (date(2026, 7, 31), "Frais non relevés"))


def written_before_treasury(payload) -> dict:
    """banque.json as an archive written before bank/0008 holds it: no
    treasury points nor adjustments."""
    del payload["treasury_checkpoints"]
    del payload["treasury_adjustments"]
    return payload


def written_before_payers(payload) -> dict:
    """banque.json as an archive written before « En caisse » holds it: no
    payers retained, no line saying what it is in the till - nor the
    treasury, which came after them."""
    del payload["income_payers"]
    for record in payload["transactions"]:
        del record["income_source"]
    return written_before_treasury(payload)


class OldArchiveTests(BankData, TestCase):
    """An archive written before « En caisse » says nothing of it - which is
    not « Automatique » everywhere, nor « forget every payer »."""

    def test_after_a_wipe_it_merges_cleanly_every_credit_automatic(self):
        reader = self.forged(written_before_payers)
        wipe_bank()
        run = import_archive(reader, MERGE)
        report = bank_report(run)
        self.assertEqual((report.conflicts, report.skipped), ([], []))
        self.assertEqual([note for note in report.notes if "champ inconnu" in note], [])
        self.assertEqual(BankTransaction.objects.count(), 6)
        self.assertEqual(set(BankTransaction.objects.values_list("income_source", flat=True)), {AUTOMATIC})
        self.assertEqual(payers(), {})
        counted = tally(run, PAYERS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 0))
        # Nor any treasury point or adjustment.
        self.assertEqual((checkpoints(), adjustments()), ({}, {}))

    def test_replace_keeps_this_databases_payers_and_marks(self):
        reader = self.forged(written_before_payers)
        before = db_fingerprint()
        preview = import_archive(reader, REPLACE, preview=True)
        confirmed = import_archive(reader, REPLACE)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        counted = tally(confirmed, PAYERS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 0))
        operations = tally(confirmed, OPERATIONS)
        self.assertEqual((operations.updated, operations.deleted, operations.unchanged), (0, 0, 6))
        self.assertEqual(bank_report(confirmed).conflicts, [])
        self.assertEqual(payers(), {"TERMINAL EXEMPLE": TILL_CARD, "ASSOCIATION EXEMPLE": NOT_A_SALE})
        self.assertEqual(BankTransaction.objects.get(pk=self.income.pk).income_source, TILL_CREDIT)
        self.assertEqual(db_fingerprint(), before)
        self.assertFalse(confirmed.affected())

    def test_merge_keeps_them_too(self):
        before = db_fingerprint()
        run = import_archive(self.forged(written_before_payers), MERGE)
        self.assertEqual((bank_report(run).conflicts, bank_report(run).skipped), ([], []))
        self.assertEqual(db_fingerprint(), before)


class OldTreasuryArchiveTests(BankData, TestCase):
    """An archive written before bank/0008 says nothing of the treasury's
    points and adjustments - which is not « forget every balance typed »:
    merged or replaced, this database's stay as they are, and nothing is
    said about them."""

    def setUp(self):
        super().setUp()
        self.reader = self.forged(written_before_treasury)
        # Moved on since, of each: one changed, one added, one deleted.
        TreasuryCheckpoint.objects.filter(pk=self.july.pk).update(balance=Decimal("-20.40"))
        make_checkpoint(date(2026, 8, 31), "310.00")
        self.june.delete()
        TreasuryAdjustment.objects.filter(pk=self.fees.pk).update(reason="Frais bancaires")
        make_adjustment(date(2026, 8, 31), "8.00")
        self.found.delete()

    def test_neither_strategy_touches_the_treasury_here(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                preview = import_archive(self.reader, strategy, preview=True)
                run = import_archive(self.reader, strategy)
                self.assertEqual(preview.outcome(), run.outcome())
                report = bank_report(run)
                self.assertEqual((report.conflicts, report.skipped), ([], []))
                self.assertEqual([note for note in report.notes if "champ inconnu" in note], [])
                for entity in (CHECKPOINTS, ADJUSTMENTS):
                    counted = tally(run, entity)
                    self.assertEqual(
                        (counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 0)
                    )
                self.assertEqual(db_fingerprint(), before)
                self.assertFalse(run.affected())

    def test_after_a_wipe_nothing_is_made(self):
        wipe_bank()
        run = import_archive(self.reader, MERGE)
        self.assertEqual((checkpoints(), adjustments()), ({}, {}))
        self.assertEqual((tally(run, CHECKPOINTS).created, tally(run, ADJUSTMENTS).created), (0, 0))
        self.assertEqual(tally(run, OPERATIONS).created, 6)


class RulesAndLinesTests(BankData, RulesData, TestCase):
    """The boundary between « Banque » and « Règles de la banque ». An
    archive written before that section existed - every safety backup taken
    until 02/10/2026 - holds the formats and the rules in banque.json, beside
    the lines (`written_by_the_old_code`). « Banque » reads that file without
    them (`archive.CARVED`): its lines come as they always did, nothing is
    said of lists it no longer knows, and importing it alone - « Remplacer »
    included - leaves the rules here as they are. What the rules section
    makes of the same file is test_bank_rules_section.py's."""

    def setUp(self):
        super().setUp()
        self.lines_before = without_treasury(BankSection().snapshot())
        self.rules_before = BankRulesSection().snapshot()

    def assert_nothing_said_of_the_rules(self, report):
        self.assertEqual([note for note in report.notes if "champ inconnu" in note], [])
        self.assertEqual(list(report.tallies), list(ENTITIES))

    def test_the_lines_of_an_older_archive_come_and_nothing_is_said_of_the_rules(self):
        reader = self.older()
        self.assertEqual(reader.sections, {"banque", bank_rules.KEY})
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                wipe_bank()
                run = import_archive(reader, {"banque": strategy})
                report = bank_report(run)
                self.assertEqual((report.conflicts, report.skipped), ([], []))
                self.assert_nothing_said_of_the_rules(report)
                self.assertEqual((tally(run, OPERATIONS).created, tally(run, PAYMENTS).created), (6, 4))
                self.assertEqual(without_treasury(BankSection().snapshot()), self.lines_before)
                # The code that wrote it had no treasury: none comes, « not said ».
                self.assertEqual((checkpoints(), adjustments()), ({}, {}))
                self.assertIsNone(run.section(bank_rules.KEY))

    def test_the_lines_of_an_older_archive_alone_leave_the_rules_here_as_they_are(self):
        # The rules moved on since: of each kind, one changed, one added, one
        # deleted. Imported alone, « Banque » neither restores nor prunes
        # them - not even under « Remplacer », whose prune is the lines'.
        reader = self.older()
        self.move_on()
        rules = BankRulesSection().snapshot()
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                run = import_archive(reader, {"banque": strategy})
                self.assertEqual(BankRulesSection().snapshot(), rules)
                self.assertEqual(db_fingerprint(), before)
                self.assertIsNone(run.section(bank_rules.KEY))
                self.assertFalse(run.affected())

    def test_both_from_an_older_archive_bring_the_whole_bank_back(self):
        reader = self.older()
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                wipe_bank()
                wipe_rules()
                run = import_archive(reader, strategy)
                # Every section it offers, the rules first (`registry.INFO`'s order).
                self.assertEqual([report.key for report in run.sections], [bank_rules.KEY, "banque"])
                self.assert_nothing_said_of_the_rules(bank_report(run))
                self.assertEqual(without_treasury(BankSection().snapshot()), self.lines_before)
                # The code that wrote it had no treasury: none comes, « not said ».
                self.assertEqual((checkpoints(), adjustments()), ({}, {}))
                self.assertEqual(BankRulesSection().snapshot(), self.rules_before)
                # And imported again, every record « inchangé ».
                before = db_fingerprint()
                again = import_archive(reader, strategy)
                self.assertEqual(db_fingerprint(), before)
                self.assertFalse(again.affected())

    def test_rules_left_in_banque_json_beside_their_own_file_are_said_and_ignored(self):
        # A hand-edited archive of this version: « Banque » never reads them,
        # « Règles de la banque » reads its own file.
        current = export_archive({"banque", bank_rules.KEY})
        self.addCleanup(current.close)
        rules = current.section(bank_rules.KEY).payload()

        def change(payload):
            payload.update(rules)
            return payload

        reader = ArchiveReader(forge(current, banque=change))
        self.addCleanup(reader.close)
        self.assertEqual(reader.sections, {"banque", bank_rules.KEY})
        IgnoreRule.objects.all().delete()
        run = import_archive(reader, {"banque": MERGE})
        notes = bank_report(run).notes
        for name in sorted(RULE_LISTS):
            self.assertIn(f"champ inconnu ignoré : banque.json › {name}", notes)
        self.assertFalse(IgnoreRule.objects.exists())


class PayerCheckTests(BankData, TestCase):
    """What the archive says of the payers retained, read before it is
    trusted: a key `bank.income.payer_key` could make, a source of the
    menu."""

    def test_payers_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        for value in ({}, "TERMINAL EXEMPLE", ["TERMINAL EXEMPLE"], [{"key": "BAR EXEMPLE", "source": "card"}, 3]):
            with self.subTest(value=value):

                def change(payload, value=value):
                    payload["income_payers"] = value
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                with self.assertRaisesMessage(ArchiveError, "« income_payers » n'est pas une liste d'objets"):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)

    def test_a_payer_it_cannot_read_is_skipped_with_its_reason_and_the_others_come(self):
        def change(payload):
            terminal = next(item for item in payload["income_payers"] if item["key"] == "TERMINAL EXEMPLE")
            payload["income_payers"] += [
                {"key": "  ", "source": "card"},
                {"source": "card"},
                {"key": 7, "source": "card"},
                dict(terminal, source="cash"),
                {"key": "Terminal exemple", "source": "card"},
                {"key": "TERMINAL-EXEMPLE", "source": "card"},
                {"key": "BAR EXEMPLE", "source": "ticket"},
                {"key": "BUVETTE EXEMPLE", "source": ""},
                {"key": "CLUB EXEMPLE"},
                {"key": "A" * 256, "source": "card"},
            ]
            return payload

        reader = self.forged(change)
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped,
            [
                "Payeur retenu sans nom",
                "Payeur retenu sans nom",
                "Payeur retenu sans nom",
                "Payeur retenu « TERMINAL EXEMPLE » : en double dans l'archive",
                "Payeur retenu « Terminal exemple » : nom illisible",
                "Payeur retenu « TERMINAL-EXEMPLE » : nom illisible",
                "Payeur retenu « BAR EXEMPLE » : « source » : valeur inconnue (« ticket »)",
                # « Automatique » is no choice to retain: a payer always says something.
                "Payeur retenu « BUVETTE EXEMPLE » : « source » : valeur inconnue («  »)",
                "Payeur retenu « CLUB EXEMPLE » : « source » : valeur manquante",
                f"Payeur retenu « {'A' * 256} » : « key » : plus de 255 caractères",
            ],
        )
        # The first of two is the one kept.
        self.assertEqual(payers(), {"TERMINAL EXEMPLE": TILL_CARD, "ASSOCIATION EXEMPLE": NOT_A_SALE})
        self.assertEqual(tally(run, PAYERS).created, 2)

    def test_replace_keeps_a_payer_whose_record_it_cannot_read(self):
        def change(payload):
            for item in payload["income_payers"]:
                if item["key"] == "TERMINAL EXEMPLE":
                    item["source"] = "ticket"
            return payload

        run = import_archive(self.forged(change), REPLACE)
        self.assertEqual(
            bank_report(run).skipped,
            ["Payeur retenu « TERMINAL EXEMPLE » : « source » : valeur inconnue (« ticket »)"],
        )
        self.assertEqual(payers(), {"TERMINAL EXEMPLE": TILL_CARD, "ASSOCIATION EXEMPLE": NOT_A_SALE})
        counted = tally(run, PAYERS)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, 1))

    def test_an_unknown_field_of_a_payer_is_said_once(self):
        def change(payload):
            for item in payload["income_payers"]:
                item["humeur"] = "calme"
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(bank_report(run).notes.count("champ inconnu ignoré : payeurs retenus › humeur"), 1)
        self.assertEqual(bank_report(run).skipped, [])


class TreasuryCheckData(BankData):
    """A forged archive's treasury lists, imported with « today » held to
    `TODAY`: what the bound on the day says depends on it."""

    def adding(self, name, *records) -> ArchiveReader:
        def change(payload):
            payload[name] += list(records)
            return payload

        return self.forged(change)

    def imported(self, reader, strategy, *, today=TODAY, preview=False):
        with mock.patch(TODAY_READ, return_value=today):
            return import_archive(reader, strategy, preview=preview)

    def assert_no_list_refuses_the_archive(self, name, values):
        for value in values:
            with self.subTest(value=value):

                def change(payload, value=value):
                    payload[name] = value
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                with self.assertRaisesMessage(ArchiveError, f"« {name} » n'est pas une liste d'objets"):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)


class CheckpointCheckTests(TreasuryCheckData, TestCase):
    """What the archive says of the treasury points, read before it is
    trusted: a day it can read, between 01/01/2000 and today, once; a
    balance as the codec reads money; every point written checked by the
    model, said in French."""

    def test_points_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        self.assert_no_list_refuses_the_archive(
            "treasury_checkpoints", ({}, "2026-07-31", ["2026-07-31"], [{"date": "2026-08-31", "balance": "1.00"}, 3])
        )

    def test_a_point_it_cannot_read_is_skipped_with_its_reason_and_the_others_come(self):
        unread = "Point de trésorerie sans date lisible : "
        cases = [
            ({"balance": "10.00"}, f"{unread}« date » : valeur manquante"),
            ({"date": "31/08/2026", "balance": "10.00"}, f"{unread}« date » : date illisible (« 31/08/2026 »)"),
            ({"date": 20260831, "balance": "10.00"}, f"{unread}« date » : date illisible (« 20260831 »)"),
            # A day is one point: the first of two is the one kept.
            ({"date": "2026-06-30", "balance": "1.00"}, "Point de trésorerie du 30/06/2026 : en double dans l'archive"),
            (
                {"date": "2026-09-01", "balance": 10.5},
                "Point de trésorerie du 01/09/2026 : « balance » : un nombre s'écrit entre guillemets (« 10.5 »)",
            ),
            (
                {"date": "2026-09-02", "balance": "1,50"},
                "Point de trésorerie du 02/09/2026 : « balance » : « 1,50 » n'est pas un nombre",
            ),
            (
                {"date": "2026-09-03", "balance": "1.505"},
                "Point de trésorerie du 03/09/2026 : « balance » : « 1.505 » a plus de 2 décimales",
            ),
            (
                {"date": "2026-09-04", "balance": "10000000000.00"},
                "Point de trésorerie du 04/09/2026 : « balance » : « 10000000000.00 » a trop de chiffres",
            ),
            (
                {"date": "2026-09-05", "balance": "NaN"},
                "Point de trésorerie du 05/09/2026 : « balance » : « NaN » n'est pas un nombre",
            ),
            ({"date": "2026-09-06"}, "Point de trésorerie du 06/09/2026 : « balance » : valeur manquante"),
            (
                {"date": "2026-09-07", "balance": "5.00", "created_at": "hier"},
                "Point de trésorerie du 07/09/2026 : « created_at » : date illisible (« hier »)",
            ),
            # Not after today: a point dated tomorrow moved « Trésorerie au … »
            # past today. Nor before a statement's first day, nor at the
            # calendar's ends - each its record's reason, never a 500.
            (
                {"date": "2026-10-01", "balance": "5.00"},
                (
                    "Point de trésorerie du 01/10/2026 : « date » : date hors limites (« 2026-10-01 ») : "
                    "entre le 01/01/2000 et le 30/09/2026"
                ),
            ),
            (
                {"date": "1999-12-31", "balance": "5.00"},
                (
                    "Point de trésorerie du 31/12/1999 : « date » : date hors limites (« 1999-12-31 ») : "
                    "entre le 01/01/2000 et le 30/09/2026"
                ),
            ),
            (
                {"date": "9999-12-31", "balance": "5.00"},
                (
                    "Point de trésorerie du 31/12/9999 : « date » : date hors limites (« 9999-12-31 ») : "
                    "entre le 01/01/2000 et le 30/09/2026"
                ),
            ),
            (
                {"date": "0001-01-01", "balance": "5.00"},
                (
                    "Point de trésorerie du 01/01/0001 : « date » : date hors limites (« 0001-01-01 ») : "
                    "entre le 01/01/2000 et le 30/09/2026"
                ),
            ),
            # At both ends of the bound, an account at nothing, an overdraft
            # as wide as the column: taken.
            ({"date": "2000-01-01", "balance": "0.00"}, None),
            ({"date": "2026-09-30", "balance": "-9999999999.99"}, None),
        ]
        reader = self.adding("treasury_checkpoints", *(record for record, _reason in cases))
        wipe_bank()
        run = self.imported(reader, MERGE)
        skipped = bank_report(run).skipped
        self.assertEqual(skipped, [reason for _record, reason in cases if reason is not None])
        for reason in skipped:
            self.assertNotRegex(reason, DJANGO_ENGLISH)
        self.assertEqual(tally(run, CHECKPOINTS).created, 4)
        self.assertEqual(
            checkpoints(),
            {
                date(2000, 1, 1): Decimal("0.00"),
                date(2026, 6, 30): Decimal("2450.00"),
                date(2026, 7, 31): Decimal("-120.40"),
                date(2026, 9, 30): Decimal("-9999999999.99"),
            },
        )

    def test_replace_keeps_a_point_whose_record_it_cannot_read(self):
        def change(payload):
            self.checkpoint_record(payload, self.july.date)["balance"] = "1,5"
            return payload

        run = self.imported(self.forged(change), REPLACE)
        self.assertEqual(
            bank_report(run).skipped, ["Point de trésorerie du 31/07/2026 : « balance » : « 1,5 » n'est pas un nombre"]
        )
        self.assertEqual(TreasuryCheckpoint.objects.get(pk=self.july.pk).balance, Decimal("-120.40"))
        counted = tally(run, CHECKPOINTS)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, 1))

    def test_a_point_past_today_is_still_named_and_never_pruned(self):
        # Refused, it is named all the same: « Remplacer » never deletes a
        # point the archive holds, even one it could not take.
        run = self.imported(self.export(), REPLACE, today=date(2026, 7, 30))
        self.assertEqual(
            bank_report(run).skipped,
            [
                (
                    "Point de trésorerie du 31/07/2026 : « date » : date hors limites (« 2026-07-31 ») : "
                    "entre le 01/01/2000 et le 30/07/2026"
                )
            ],
        )
        self.assertEqual(tally(run, CHECKPOINTS).deleted, 0)
        self.assertEqual(TreasuryCheckpoint.objects.get(pk=self.july.pk).balance, Decimal("-120.40"))

    def test_an_unknown_field_of_a_point_is_said_once(self):
        def change(payload):
            for item in payload["treasury_checkpoints"]:
                item["humeur"] = "calme"
            return payload

        run = self.imported(self.forged(change), MERGE)
        self.assertEqual(bank_report(run).notes.count("champ inconnu ignoré : points de trésorerie › humeur"), 1)
        self.assertEqual(bank_report(run).skipped, [])


class AdjustmentCheckTests(TreasuryCheckData, TestCase):
    """What the archive says of the treasury adjustments, read before it is
    trusted: a reference it can read, once, and never one drawn here; a day
    between 01/01/2000 and today; an amount as the codec reads money; every
    adjustment written checked by the model, said in French."""

    def test_adjustments_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        self.assert_no_list_refuses_the_archive(
            "treasury_adjustments", ({}, "-35.00", [self.fees.reference], [{"reference": "a" * 16}, None])
        )

    def test_an_adjustment_it_cannot_read_is_skipped_with_its_reason_and_the_others_come(self):
        unread = "Ajustement de trésorerie sans référence lisible : "
        cases = [
            ({"date": "2026-09-01", "amount": "1.00"}, f"{unread}référence manquante"),
            ({"reference": "  ", "date": "2026-09-01", "amount": "1.00"}, f"{unread}référence manquante"),
            ({"reference": 1234, "date": "2026-09-01", "amount": "1.00"}, f"{unread}référence manquante"),
            (
                {"reference": "0" * 17, "date": "2026-09-01", "amount": "1.00"},
                f"{unread}« reference » : plus de 16 caractères",
            ),
            # One reference is one adjustment: the first of two is the one kept.
            (
                {"reference": self.fees.reference, "date": "2026-09-02", "amount": "2.00"},
                "Ajustement de trésorerie du 02/09/2026 (2,00 €) : en double dans l'archive",
            ),
            (
                {"reference": "c000000000000001", "amount": "1.00"},
                "Ajustement de trésorerie sans date lisible : « date » : valeur manquante",
            ),
            (
                {"reference": "c000000000000002", "date": "02/09/2026", "amount": "1.00"},
                "Ajustement de trésorerie sans date lisible : « date » : date illisible (« 02/09/2026 »)",
            ),
            (
                {"reference": "c000000000000003", "date": "2026-09-03"},
                "Ajustement de trésorerie du 03/09/2026 : « amount » : valeur manquante",
            ),
            (
                {"reference": "c000000000000004", "date": "2026-09-04", "amount": 4.5},
                "Ajustement de trésorerie du 04/09/2026 : « amount » : un nombre s'écrit entre guillemets (« 4.5 »)",
            ),
            (
                {"reference": "c000000000000005", "date": "2026-09-05", "amount": "4.555"},
                "Ajustement de trésorerie du 05/09/2026 : « amount » : « 4.555 » a plus de 2 décimales",
            ),
            # The model's own check, in French: an adjustment of 0 € changes
            # nothing.
            (
                {"reference": "c000000000000006", "date": "2026-09-06", "amount": "0.00"},
                (
                    "Ajustement de trésorerie du 06/09/2026 (0,00 €) : montant : un ajustement de 0 € ne change rien : "
                    "tapez un montant"
                ),
            ),
            (
                {"reference": "c000000000000007", "date": "2026-10-01", "amount": "7.00"},
                (
                    "Ajustement de trésorerie du 01/10/2026 (7,00 €) : « date » : date hors limites (« 2026-10-01 ») : "
                    "entre le 01/01/2000 et le 30/09/2026"
                ),
            ),
            (
                {"reference": "c000000000000008", "date": "1999-12-31", "amount": "8.00"},
                (
                    "Ajustement de trésorerie du 31/12/1999 (8,00 €) : « date » : date hors limites (« 1999-12-31 ») : "
                    "entre le 01/01/2000 et le 30/09/2026"
                ),
            ),
            (
                {"reference": "c000000000000009", "date": "2026-09-09", "amount": "9.00", "reason": "R" * 256},
                "Ajustement de trésorerie du 09/09/2026 (9,00 €) : « reason » : plus de 255 caractères",
            ),
            # At both ends of the bound, a reason left blank, an amount as
            # wide as the column: taken.
            ({"reference": "c000000000000010", "date": "2000-01-01", "amount": "-9999999999.99"}, None),
            ({"reference": "c000000000000011", "date": "2026-09-30", "amount": "0.01", "reason": ""}, None),
        ]
        reader = self.adding("treasury_adjustments", *(record for record, _reason in cases))
        wipe_bank()
        run = self.imported(reader, MERGE)
        skipped = bank_report(run).skipped
        self.assertEqual(skipped, [reason for _record, reason in cases if reason is not None])
        for reason in skipped:
            self.assertNotRegex(reason, DJANGO_ENGLISH)
        self.assertEqual(tally(run, ADJUSTMENTS).created, 4)
        # Each under the reference the archive gives it.
        self.assertEqual(
            adjustments(),
            {
                self.fees.reference: (date(2026, 7, 31), Decimal("-35.00"), "Frais non relevés"),
                self.found.reference: (date(2026, 7, 15), Decimal("12.30"), ""),
                "c000000000000010": (date(2000, 1, 1), Decimal("-9999999999.99"), ""),
                "c000000000000011": (date(2026, 9, 30), Decimal("0.01"), ""),
            },
        )

    def test_an_adjustment_without_a_reference_is_never_given_one(self):
        """A reference drawn by the preview and another by the confirm would
        make the two differ (`runner.NotAsPreviewed`), and one drawn at all
        would make an adjustment the archive cannot name again: skipped.
        Nothing is drawn either for the one it creates beside it."""

        def change(payload):
            del self.adjustment_record(payload, self.found)["reference"]
            return payload

        reader = self.forged(change)
        wipe_bank()
        with mock.patch("bank.models.secrets") as drawn:
            preview = self.imported(reader, MERGE, preview=True)
            run = self.imported(reader, MERGE)
            drawn.token_hex.assert_not_called()
            # What would have been seen: a new adjustment draws its own.
            TreasuryAdjustment(date=date(2026, 9, 1), amount=Decimal("1.00"))
            drawn.token_hex.assert_called_once_with(8)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(
            bank_report(run).skipped, ["Ajustement de trésorerie sans référence lisible : référence manquante"]
        )
        self.assertEqual(list(adjustments()), [self.fees.reference])

    def test_replace_checks_what_it_writes_and_keeps_the_adjustment_when_refused(self):
        # Named as « Trésorerie » shows it here; kept as it is, never pruned.
        for fields, reason in (
            ({"amount": "0.00"}, "montant : un ajustement de 0 € ne change rien : tapez un montant"),
            (
                {"date": "2026-10-01"},
                "« date » : date hors limites (« 2026-10-01 ») : entre le 01/01/2000 et le 30/09/2026",
            ),
            ({"amount": "1,5"}, "« amount » : « 1,5 » n'est pas un nombre"),
        ):
            with self.subTest(fields=fields):

                def change(payload, fields=fields):
                    self.adjustment_record(payload, self.fees).update(fields)
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                preview = self.imported(reader, REPLACE, preview=True)
                run = self.imported(reader, REPLACE)
                self.assertEqual(preview.outcome(), run.outcome())
                self.assertEqual(
                    bank_report(run).skipped, [f"Ajustement de trésorerie du 31/07/2026 (-35,00 €) : {reason}"]
                )
                self.assertEqual(db_fingerprint(), before)
                counted = tally(run, ADJUSTMENTS)
                self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, 1))

    def test_an_unknown_field_of_an_adjustment_is_said_once(self):
        def change(payload):
            for item in payload["treasury_adjustments"]:
                item["humeur"] = "calme"
            return payload

        run = self.imported(self.forged(change), MERGE)
        self.assertEqual(bank_report(run).notes.count("champ inconnu ignoré : ajustements de trésorerie › humeur"), 1)
        self.assertEqual(bank_report(run).skipped, [])


class TreasuryModelCheckTests(TestCase):
    """`_check_treasury`: the model's refusals in its own French, after the
    field they name, and Django's - which an archive cannot reach through
    the codec, but which are English - never said: the field is named
    instead."""

    def refusal(self, row) -> str:
        with self.assertRaises(codec.FieldValueError) as caught:
            section._check_treasury(row)
        reason = str(caught.exception)
        self.assertNotRegex(reason, DJANGO_ENGLISH)
        return reason

    def test_the_models_own_refusals_are_said_after_their_field(self):
        self.assertEqual(
            self.refusal(TreasuryAdjustment(reference="d" * 16, date=date(2026, 9, 1), amount=Decimal("0.00"))),
            "montant : un ajustement de 0 € ne change rien : tapez un montant",
        )
        self.assertEqual(
            self.refusal(TreasuryCheckpoint(date=date(2100, 1, 1), balance=Decimal("1.00"))),
            "date : date hors limites : entre le 01/01/2000 et le 31/12/2099",
        )
        self.assertEqual(
            self.refusal(TreasuryCheckpoint(date=date(2026, 9, 1), balance=Decimal("10000000000.00"))),
            f"solde : solde hors limites : 9{NBSP}999{NBSP}999{NBSP}999.99 € au plus, en plus ou en moins",
        )

    def test_djangos_own_refusals_name_the_field_never_in_english(self):
        make_checkpoint(date(2026, 9, 1), "1.00")
        self.assertEqual(
            self.refusal(TreasuryCheckpoint(date=date(2026, 9, 1), balance=Decimal("2.00"))),
            "« date » : valeur refusée",
        )
        taken = make_adjustment(date(2026, 9, 1), "1.00")
        self.assertEqual(
            self.refusal(TreasuryAdjustment(reference=taken.reference, date=date(2026, 9, 2), amount=Decimal("2.00"))),
            "« reference » : valeur refusée",
        )
        # Wider than the column, past what `clean` measures (a third
        # decimal): Django's validator, said in French.
        self.assertEqual(
            self.refusal(TreasuryCheckpoint(date=date(2026, 9, 2), balance=Decimal("1.005"))),
            "« balance » : valeur refusée",
        )


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

    def test_a_line_settled_here_does_not_take_an_invoice_another_line_pays(self):
        """An invoice is no longer paid once - but what holds the link back
        here is this LINE's own decision: settled by hand without it, so a
        person took it off. The invoice paying elsewhere says nothing."""
        reader = self.export()
        InvoicePayment.objects.filter(invoice=self.invoice_a).update(transaction=self.unlinked)
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [
                (
                    "Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : réglée à la main ici sans payer Épicerie "
                    "des Lilas n° T-101 du 01/07/2026, qu'elle paie dans l'archive — gardée telle quelle"
                )
            ],
        )
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_a).transaction, self.unlinked)

    def test_a_line_nobody_settled_takes_its_link_although_another_line_pays_it(self):
        # The automatic pass linked this one and no person touched it; here
        # the invoice is paid by a second line. Refused, an invoice settled
        # in two goes could never be restored at all.
        reader = self.export()
        InvoicePayment.objects.filter(invoice=self.invoice_b).update(transaction=self.unlinked)
        run = import_archive(reader, MERGE)
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(
            set(InvoicePayment.objects.filter(invoice=self.invoice_b).values_list("transaction_id", flat=True)),
            {self.auto.pk, self.unlinked.pk},
        )

    def test_an_invoice_paid_by_two_lines_comes_back_on_both(self):
        pay(self.unlinked, self.invoice_c)
        reader = self.export()
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.invoice_c).count(), 2)

    def test_replace_puts_an_invoice_paid_by_two_lines_back_on_both(self):
        pay(self.unlinked, self.invoice_c)
        reader = self.export()
        wipe_bank()
        run = import_archive(reader, REPLACE)
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.invoice_c).count(), 2)

    def test_merging_an_export_of_an_invoice_paid_twice_changes_nothing(self):
        # Both links are already here: « inchangé ». Keyed by the invoice
        # alone rather than by the pair, the second payment answered for the
        # first and the round trip reported a conflict about nothing.
        pay(self.unlinked, self.invoice_c)
        run = import_archive(self.export(), MERGE)
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual((tally(run, PAYMENTS).created, tally(run, PAYMENTS).unchanged), (0, 5))

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

    def test_a_merge_never_undoes_a_no_invoice_decision_taken_here(self):
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
        self.assertEqual(
            self.manual.fingerprint, InvoicePayment.objects.get(invoice=self.invoice_a).transaction.fingerprint
        )
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
        self.assertEqual(
            InvoicePayment.objects.get(invoice=self.invoice_a).transaction.fingerprint, self.manual.fingerprint
        )
        self.assertIn(
            "rapproché par son fichier : n° 20260701-12.30 ici, n° T-101 dans l'archive", bank_report(run).notes
        )


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
            bank_report(run).skipped,
            [f"Opération {self.income.fingerprint[:12]}… : « operation_date » : valeur manquante"],
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
        self.assertEqual(
            bank_report(run).skipped, [f"Opération {self.income.fingerprint[:12]}… : en double dans l'archive"]
        )
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
        # Never "rules" any more: « Règles de la banque » reads each of its
        # lists as « not said » when absent (test_bank_rules_section.py).
        for name, value in (("transactions", {}), ("aliases", None), ("aliases", ["QNORD"])):
            with self.subTest(name=name, value=value):

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


class UnreadableMomentTests(BankData, TestCase):
    """Under « Remplacer » a record whose compared fields differ is written
    whole, its moment included - a moment that is never compared. One the
    section cannot read is that record's reason: skipped and said, the record
    here kept as it was, and never a 500 on the preview."""

    #: The moment the archive gives → the reason said after the field's name.
    MOMENTS = {None: "valeur manquante", "hier": "date illisible (« hier »)"}

    def assert_skipped_as_previewed(self, change, skipped):
        """Preview, then confirm, `change` under « Remplacer »: both say
        exactly `skipped` and nothing else, and nothing is written."""
        reader = self.forged(change)
        before = db_fingerprint()
        preview = import_archive(reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        run = import_archive(reader, REPLACE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual((bank_report(run).skipped, bank_report(run).conflicts), ([skipped], []))
        self.assertEqual(db_fingerprint(), before)
        self.assertFalse(run.affected())
        return run

    def test_a_payer_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    terminal = next(item for item in payload["income_payers"] if item["key"] == "TERMINAL EXEMPLE")
                    terminal.update(source="cash", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Payeur retenu « TERMINAL EXEMPLE » : « created_at » : {reason}"
                )
                self.assertEqual(payers(), {"TERMINAL EXEMPLE": TILL_CARD, "ASSOCIATION EXEMPLE": NOT_A_SALE})
                self.assertEqual(IncomePayer.objects.get(key="TERMINAL EXEMPLE").created_at, PAYER_MOMENT)
                counted = tally(run, PAYERS)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 1))

    def test_a_treasury_point_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    self.checkpoint_record(payload, self.july.date).update(balance="-20.40", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Point de trésorerie du 31/07/2026 : « created_at » : {reason}"
                )
                july = TreasuryCheckpoint.objects.get(pk=self.july.pk)
                self.assertEqual((july.balance, july.created_at), (Decimal("-120.40"), CHECKPOINT_MOMENT))
                counted = tally(run, CHECKPOINTS)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 1))

    def test_a_treasury_adjustment_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    self.adjustment_record(payload, self.fees).update(reason="Frais du mois", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Ajustement de trésorerie du 31/07/2026 (-35,00 €) : « created_at » : {reason}"
                )
                fees = TreasuryAdjustment.objects.get(pk=self.fees.pk)
                self.assertEqual((fees.reason, fees.created_at), ("Frais non relevés", ADJUSTMENT_MOMENT))
                counted = tally(run, ADJUSTMENTS)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 1))

    def test_a_line_whose_import_moment_cannot_be_read_is_kept_with_its_links(self):
        # The archive also says the line pays nothing: written, it would lose
        # its link. The field is compared whatever the line's sign.
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    self.record(payload, self.manual).update(income_source="other", imported_at=moment, payments=[])
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Opération du 02/07/2026 (EPICERIE LILAS, -12,30 €) : « imported_at » : {reason}"
                )
                manual = BankTransaction.objects.get(pk=self.manual.pk)
                self.assertEqual((manual.income_source, manual.imported_at), (AUTOMATIC, self.manual.imported_at))
                self.assertEqual(manual.payments.get().invoice, self.invoice_a)
                operations = tally(run, OPERATIONS)
                self.assertEqual(
                    (operations.created, operations.updated, operations.deleted, operations.unchanged), (0, 0, 0, 5)
                )
                payments = tally(run, PAYMENTS)
                self.assertEqual((payments.created, payments.deleted, payments.unchanged), (0, 0, 3))


class ImportMakesNoDecisionTests(BankData, TestCase):
    def test_no_automatic_matching_runs(self):
        reader = self.export()
        wipe_bank()
        with mock.patch("bank.reconcile.reconcile") as matching:
            run = import_archive(reader, MERGE)
        matching.assert_not_called()
        self.assertEqual(InvoicePayment.objects.count(), 4)
        self.assertIn(RECONCILE_NOTE, bank_report(run).notes)

    def test_a_line_keeps_what_it_was_imported_as_whatever_the_rules_say(self):
        """An import writes rows; it reads nothing again. A line's kind,
        payee and card date are what the rules said when its statement was
        imported - recognised again here, by the rules this database holds
        now, they would be something else, in silence."""
        card = make_line(date(2026, 7, 22), "CAFE EXEMPLE", "-4.20")
        BankTransaction.objects.filter(pk=card.pk).update(
            label="FACTURE CARTE DU 200726 CAFE EXEMPLE CARTE 4970XXXXXXXX1234", kind=BankTransaction.Kind.OTHER
        )
        stored = sorted(BankTransaction.objects.values_list("fingerprint", "kind", "counterparty", "card_date"))
        reader = self.export()
        wipe_bank()
        with (
            mock.patch("bank.recognition.describe") as describe,
            mock.patch("bank.recognition.load") as load,
        ):
            run = import_archive(reader, MERGE)
        describe.assert_not_called()
        load.assert_not_called()
        self.assertEqual(tally(run, OPERATIONS).created, 7)
        self.assertEqual(
            sorted(BankTransaction.objects.values_list("fingerprint", "kind", "counterparty", "card_date")), stored
        )
        # The seeded rule reads that label as a card payment: the line keeps
        # what it was stored as.
        self.assertEqual(BankTransaction.objects.get(fingerprint=card.fingerprint).kind, BankTransaction.Kind.OTHER)

    def test_no_gap_is_worked_out_nor_settled(self):
        """An import writes the points and adjustments the archive holds,
        and nothing else: the two points of BankData disagree, and no
        adjustment is made to settle them - « Trésorerie » asks a person."""
        reader = self.export()
        wipe_bank()
        with (
            mock.patch("bank.treasury.compute") as compute,
            mock.patch("bank.treasury.load") as load,
        ):
            run = import_archive(reader, MERGE)
        compute.assert_not_called()
        load.assert_not_called()
        self.assertEqual((tally(run, CHECKPOINTS).created, tally(run, ADJUSTMENTS).created), (2, 2))
        self.assertEqual(set(adjustments()), {self.fees.reference, self.found.reference})


class ClearTests(BankData, RulesData, TestCase):
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
        self.assertEqual(deleted, {OPERATIONS: 6, PAYMENTS: 4, ALIASES: 1, PAYERS: 2, CHECKPOINTS: 2, ADJUSTMENTS: 2})
        self.assertEqual(bank_report(run).notes, [section.CLEAR_NOTE, TREASURY_CLEAR_NOTE])
        self.assertEqual(run.affected(), {"banque"})
        # The invoices and suppliers the bank pointed at are not the bank's.
        self.assertEqual(self.grocer.invoices.count(), 3)
        self.assertTrue(Supplier.objects.filter(pk=self.hardware.pk).exists())

    def test_the_formats_and_the_rules_stay(self):
        # « Règles de la banque »'s, the seeded ones included: « Effacer » of
        # « Banque » takes none of them, and says nothing of them - neither
        # on the Effacer tab before (its clear_note is the treasury's) nor
        # after.
        rules = BankRulesSection().snapshot()
        run = run_clear({"banque"}, preview=False)
        self.assertEqual(BankRulesSection().snapshot(), rules)
        self.assertEqual(
            (StatementFormat.objects.count(), OperationRule.objects.count(), IgnoreRule.objects.count()),
            (2, len(rules["operation_rules"]), 2),
        )
        notes = bank_report(run).notes
        self.assertNotIn(bank_rules.FORMAT_CLEAR_NOTE, notes)
        self.assertNotIn(bank_rules.RECOGNITION_CLEAR_NOTE, notes)
        self.assertIsNone(run.section(bank_rules.KEY))
        for word in ("règle", "format"):
            with self.subTest(word=word):
                self.assertNotIn(word, registry.INFO["banque"].clear_note)
                self.assertNotIn(word, registry.INFO["banque"].description)

    def test_the_treasury_goes_and_the_report_says_what_that_costs(self):
        # No statement brings a balance typed back: the Effacer tab says it
        # goes before the clear, the report where it comes back from after.
        run = run_clear({"banque"}, preview=False)
        self.assertEqual((checkpoints(), adjustments()), ({}, {}))
        self.assertIn(TREASURY_CLEAR_NOTE, bank_report(run).notes)
        self.assertIn("sauvegarde", TREASURY_CLEAR_NOTE)
        self.assertIn("points et les ajustements de trésorerie", registry.INFO["banque"].clear_note)
        self.assertIn("la sauvegarde", registry.INFO["banque"].clear_note)
        self.assertIn("points et ajustements de trésorerie", registry.INFO["banque"].description)
        # Nothing to take, nothing said.
        run = run_clear({"banque"}, preview=False)
        self.assertNotIn(TREASURY_CLEAR_NOTE, bank_report(run).notes)

    def test_adjustments_alone_say_the_treasurys_note(self):
        # Points deleted on the page, adjustments left counting nowhere:
        # they go too, and are said.
        TreasuryCheckpoint.objects.all().delete()
        run = run_clear({"banque"}, preview=False)
        self.assertEqual((tally(run, CHECKPOINTS).deleted, tally(run, ADJUSTMENTS).deleted), (0, 2))
        self.assertIn(TREASURY_CLEAR_NOTE, bank_report(run).notes)

    def test_clearing_its_rules_leaves_the_bank(self):
        lines = BankSection().snapshot()
        run = run_clear({bank_rules.KEY}, preview=False)
        self.assertEqual(BankSection().snapshot(), lines)
        self.assertIsNone(run.section("banque"))

    def test_clearing_the_bank_clears_only_the_bank(self):
        # Neither requires the other: « Banque » only recommends its rules.
        self.assertEqual(registry.closure({"banque"}, "clear"), {"banque"})
        self.assertEqual(registry.closure({bank_rules.KEY}, "clear"), {bank_rules.KEY})
