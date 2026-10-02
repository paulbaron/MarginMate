"""« Banque » (§7.7, §10.2): the bank's lines, their links to invoices, the
ignore rules, the recognition rules (« Reconnaissance des opérations »), the
statement formats (« Format du relevé »), the payee names learnt and the
payers retained on « Entrées d'argent ».

What these guard is the owner's decisions: a link made by hand, a line
unlinked, a line declared « pas de facture », what a credit is in the till
(« En caisse », on the line or for its payer), how his bank's operations are
recognised and how its export is laid out. Nothing rebuilds them, so a round
trip must bring every one back exactly, a merge must never overwrite one,
and a link whose invoice is not here must be said, never guessed.

Every test database holds the recognition rules bank/0006 seeds (`SEEDED`)
and the format bank/0007 seeds (`SEEDED_FORMAT`): an « empty » bank still
reads and recognises the owner's bank.

Every name, amount and label below is invented.
"""

import hashlib
import importlib
import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest import mock

import regex
from django.test import TestCase
from django.urls import reverse

from bank import recognition, reconcile
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
)
from common import DateRange
from invoices.deletion import delete_invoice
from invoices.models import Invoice, Supplier
from returnables.tests.test_patterns import NeverCompile
from tests.factories import make_invoice, make_supplier
from transfer import keys, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections import bank as section
from transfer.sections.bank import (
    ALIASES,
    ENTITIES,
    FORMAT_CLEAR_NOTE,
    FORMATS,
    OPERATIONS,
    PAYERS,
    PAYMENTS,
    RECOGNITION,
    RECOGNITION_CLEAR_NOTE,
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
# What a credit is in the till (« En caisse »).
AUTOMATIC, TILL_CARD, TILL_CASH = IncomeSource.AUTOMATIC, IncomeSource.CARD, IncomeSource.CASH
TILL_CHEQUE, TILL_CREDIT, VOUCHER, NOT_A_SALE = (
    IncomeSource.CHEQUE,
    IncomeSource.CREDIT,
    IncomeSource.VOUCHER,
    IncomeSource.OTHER,
)
PAYER_MOMENT = datetime(2026, 7, 28, 11, 5, 30, 125000, tzinfo=UTC)
RECOGNITION_MOMENT = datetime(2026, 7, 21, 9, 15, 45, 500000, tzinfo=UTC)
#: (position, name, meaning, searched, pattern) of the rules bank/0006
#: seeds into every database, this test's included.
SEEDED = importlib.import_module("bank.migrations.0006_operation_rules").RULES
#: A rule a person typed for another bank's payment terminal, which prints
#: no gross (BankData).
TERMINAL_RULE = "Versement TPE (EXEMPLE PAY)"
FORMAT_MOMENT = datetime(2026, 7, 22, 16, 40, 5, 750000, tzinfo=UTC)
#: The name and the layout of the format bank/0007 seeds into every
#: database, this test's included.
_SEEDED_FORMATS = importlib.import_module("bank.migrations.0007_statement_formats")
SEEDED_FORMAT, SEEDED_LAYOUT = _SEEDED_FORMATS.NAME, _SEEDED_FORMATS.FORMAT
#: A format a person typed for another bank's export (BankData): cells
#: split by tabulations, ISO dates, decimal points, the label over two
#: columns, the debits and the credits apart, the account in a header line.
TAB_FORMAT = "Banque exemple (tabulations)"
TAB_LAYOUT = {
    "position": 2,
    "encoding": "utf-8",
    "delimiter": "\t",
    "date_format": "yyyy-mm-dd",
    "decimal_mark": ".",
    "date_column": 1,
    "value_date_column": 2,
    "label_columns": "3, 4",
    "debit_column": 5,
    "credit_column": 6,
    "account_pattern": r"COMPTE (?P<compte>[0-9]{5,})",
}
#: What separates an amount's thousands in the report (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"

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


def make_rule(pattern, description="", is_active=True, category="") -> IgnoreRule:
    rule = IgnoreRule.objects.create(pattern=pattern, description=description, is_active=is_active, category=category)
    IgnoreRule.objects.filter(pk=rule.pk).update(created_at=datetime(2026, 7, 20, 8, 30, tzinfo=UTC))
    rule.refresh_from_db()
    return rule


def make_payer(key, source) -> IncomePayer:
    """A payer retained on « Entrées d'argent » (`key` in the form
    `bank.income.payer_key` makes), made at a moment in the past: a round
    trip that forgot to restore it would show today's instead."""
    payer = IncomePayer.objects.create(key=key, source=source)
    IncomePayer.objects.filter(pk=payer.pk).update(created_at=PAYER_MOMENT)
    payer.refresh_from_db()
    return payer


def make_operation_rule(name, meaning, pattern, *, searched="label", position=9, is_active=True) -> OperationRule:
    """A recognition rule as « Reconnaissance des opérations » saves it
    (checked by the model as the form is), made at a moment in the past: a
    round trip that forgot to restore it would show today's instead."""
    rule = OperationRule(
        name=name, meaning=meaning, searched=searched, pattern=pattern, position=position, is_active=is_active
    )
    rule.full_clean()
    rule.save()
    OperationRule.objects.filter(pk=rule.pk).update(created_at=RECOGNITION_MOMENT)
    rule.refresh_from_db()
    return rule


def make_statement_format(name, **layout) -> StatementFormat:
    """A statement format as « Format du relevé » saves it (checked by the
    model as the form is), made at a moment in the past: a round trip that
    forgot to restore it would show today's instead."""
    fmt = StatementFormat(name=name, **layout)
    fmt.full_clean()
    fmt.save()
    StatementFormat.objects.filter(pk=fmt.pk).update(created_at=FORMAT_MOMENT)
    fmt.refresh_from_db()
    return fmt


def wipe_bank() -> None:
    """Everything the section holds, as its clear leaves it - the seeded
    recognition rules and statement format included: an archive importing
    into it has to bring them back."""
    InvoicePayment.objects.all().delete()
    BankTransaction.objects.all().delete()
    CounterpartyAlias.objects.all().delete()
    IgnoreRule.objects.all().delete()
    OperationRule.objects.all().delete()
    StatementFormat.objects.all().delete()
    IncomePayer.objects.all().delete()


def statement_formats() -> list[tuple]:
    """Every field of every statement format, its moment included, in the
    order an import offers them (the first is the default)."""
    return list(StatementFormat.objects.order_by("position", "name").values_list(*section.FORMAT_FIELDS))


def recognition_rules() -> list[tuple]:
    """(position, name, meaning, searched, pattern, is_active, created_at)
    of every recognition rule, in the order they are asked."""
    return list(
        OperationRule.objects.order_by("position", "name").values_list(
            "position", "name", "meaning", "searched", "pattern", "is_active", "created_at"
        )
    )


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
    page lets a person take, and the two ways « Entrées d'argent » lets one
    say what a credit is in the till: on the line, and for its payer."""

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
        # A rule that also says what its payments count as on « Dépenses »,
        # and one that only says there is nothing to link.
        make_rule("URSSAF", "Cotisations", category="Cotisations sociales")
        make_rule("PRET LOCAL", "Prêt du local", is_active=False)
        # Two payers retained: a payment terminal whose payouts print no
        # « TOTAL ENCAISSE », and a contribution that is no sale.
        make_payer("TERMINAL EXEMPLE", TILL_CARD)
        make_payer("ASSOCIATION EXEMPLE", NOT_A_SALE)
        # Beside the seeded rules, one typed for another bank's terminal,
        # whose payouts print no gross. It recognises none of the lines
        # above: what they are was decided when they were imported.
        self.terminal_rule = make_operation_rule(TERMINAL_RULE, "payout", r"EXEMPLE PAY REMISE")
        # Beside the seeded format, one typed for another bank's export. None
        # of the lines above is read again with it: their fingerprints are
        # what their statements gave when they were imported.
        self.tab_format = make_statement_format(TAB_FORMAT, **TAB_LAYOUT)

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
        # Nothing imported, nothing decided - and the owner's bank read and
        # recognised all the same, by the format bank/0007 and the rules
        # bank/0006 seed.
        self.assertEqual(BankSection().count(), {**dict.fromkeys(ENTITIES, 0), RECOGNITION: len(SEEDED), FORMATS: 1})

    def test_the_recognition_rules_have_a_count_of_their_own(self):
        # « règles » are the « sans facture » ones: one word for two lists
        # read as one on the page.
        self.assertEqual(ENTITIES, (OPERATIONS, PAYMENTS, RULES, RECOGNITION, FORMATS, ALIASES, PAYERS))
        self.assertNotEqual(RECOGNITION, RULES)
        self.assertEqual(len(set(ENTITIES)), len(ENTITIES))


class ExportTests(BankData, TestCase):
    def test_counts(self):
        expected = {
            OPERATIONS: 6,
            PAYMENTS: 4,
            RULES: 2,
            RECOGNITION: len(SEEDED) + 1,
            FORMATS: 2,
            ALIASES: 1,
            PAYERS: 2,
        }
        self.assertEqual(BankSection().count(), expected)
        self.assertEqual(self.export().section("banque").counts, expected)

    def test_the_statement_formats_are_exported_in_the_order_an_import_offers_them(self):
        # Every field but the id, the first the default - and the tab comes
        # out of the JSON as the tab it was.
        payload = self.export().section("banque").payload()
        formats = payload["statement_formats"]
        self.assertEqual([item["name"] for item in formats], [SEEDED_FORMAT, TAB_FORMAT])
        for item in formats:
            self.assertEqual(set(item), set(section.FORMAT_FIELDS))
        seeded = formats[0]
        self.assertEqual({name: seeded[name] for name in SEEDED_LAYOUT}, SEEDED_LAYOUT)
        self.assertEqual((seeded["debit_column"], seeded["credit_column"]), (None, None))
        self.assertEqual(
            formats[1],
            {
                "name": TAB_FORMAT,
                **TAB_LAYOUT,
                "amount_column": None,
                "bank_type_column": None,
                "created_at": "2026-07-22T16:40:05.750000+00:00",
            },
        )
        self.assertEqual(formats[1]["delimiter"], "\t")

    def test_no_statement_format_is_an_empty_list_never_a_missing_one(self):
        # Absent, the list reads as an archive written before the formats
        # existed (« not said »), and « Remplacer » would keep this
        # database's.
        StatementFormat.objects.all().delete()
        reader = self.export()
        self.assertEqual(reader.section("banque").payload()["statement_formats"], [])
        self.assertEqual(reader.section("banque").counts[FORMATS], 0)

    def test_the_recognition_rules_are_exported_in_the_order_they_are_asked(self):
        payload = self.export().section("banque").payload()
        self.assertEqual(
            [
                (item["position"], item["name"], item["meaning"], item["searched"], item["pattern"])
                for item in payload["operation_rules"]
            ],
            [*SEEDED, (9, TERMINAL_RULE, "payout", "label", r"EXEMPLE PAY REMISE")],
        )
        self.assertEqual(
            payload["operation_rules"][-1],
            {
                "name": TERMINAL_RULE,
                "meaning": "payout",
                "searched": "label",
                "pattern": r"EXEMPLE PAY REMISE",
                "position": 9,
                "is_active": True,
                "created_at": "2026-07-21T09:15:45.500000+00:00",
            },
        )

    def test_no_recognition_rule_is_an_empty_list_never_a_missing_one(self):
        # Absent, the list reads as an archive written before the rules
        # existed (« not said »), and « Remplacer » would keep this
        # database's.
        OperationRule.objects.all().delete()
        reader = self.export()
        self.assertEqual(reader.section("banque").payload()["operation_rules"], [])
        self.assertEqual(reader.section("banque").counts[RECOGNITION], 0)

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
        self.assertEqual(
            payload["rules"],
            [
                {
                    "pattern": "URSSAF",
                    "description": "Cotisations",
                    "is_active": True,
                    "category": "Cotisations sociales",
                    "created_at": "2026-07-20T08:30:00+00:00",
                },
                {
                    "pattern": "PRET LOCAL",
                    "description": "Prêt du local",
                    "is_active": False,
                    "category": "",
                    "created_at": "2026-07-20T08:30:00+00:00",
                },
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

    def assert_recognition_back(self, rules):
        # In their order, each with the moment it was made: the seeded ones
        # with their database's, the one typed with its own.
        self.assertEqual(recognition_rules(), rules)
        self.assertEqual(OperationRule.objects.get(name=TERMINAL_RULE).created_at, RECOGNITION_MOMENT)

    def assert_formats_back(self, formats):
        # Every field, in their order, each with the moment it was made, the
        # tab a tab.
        self.assertEqual(statement_formats(), formats)
        tab = StatementFormat.objects.get(name=TAB_FORMAT)
        self.assertEqual((tab.delimiter, tab.created_at), ("\t", FORMAT_MOMENT))
        self.assertEqual(StatementFormat.objects.first().name, SEEDED_FORMAT)

    def test_merge(self):
        rules, formats = recognition_rules(), statement_formats()
        before, after = round_trip({"banque"}, MERGE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_decisions_back()
        self.assert_recognition_back(rules)
        self.assert_formats_back(formats)

    def test_replace(self):
        rules, formats = recognition_rules(), statement_formats()
        before, after = round_trip({"banque"}, REPLACE, after_clear=self.assert_empty)
        self.assertEqual(after, before)
        self.assert_decisions_back()
        self.assert_recognition_back(rules)
        self.assert_formats_back(formats)

    def test_formats_of_one_position_keep_their_order(self):
        # The first by (position, name) is the one an import uses when
        # nobody chooses - never the first by id, which an import gives anew.
        make_statement_format("Banque exemple B", position=0, date_column=1, label_columns="2", amount_column=3)
        make_statement_format("Banque exemple A", position=0, date_column=1, label_columns="2", amount_column=3)
        formats = statement_formats()
        self.assertEqual(StatementFormat.objects.first().name, "Banque exemple A")
        round_trip({"banque"}, REPLACE)
        self.assertEqual(statement_formats(), formats)
        self.assertEqual(StatementFormat.objects.first().name, "Banque exemple A")

    def test_rules_of_one_position_keep_their_order(self):
        # Rules at one position are asked by name - never by id, which an
        # import gives anew - and must still be once created again.
        make_operation_rule("Virement exemple (B)", "transfer", r"^EXEMPLE B", position=3)
        make_operation_rule("Virement exemple (A)", "transfer", r"^EXEMPLE A", position=3)
        rules = recognition_rules()
        round_trip({"banque"}, REPLACE)
        self.assertEqual(recognition_rules(), rules)
        self.assertEqual(
            [rule[1] for rule in rules if rule[0] == 3],
            ["Virement exemple (A)", "Virement exemple (B)", "Virement émis (/BEN)"],
        )

    def test_rules_of_one_position_keep_their_order_when_one_of_them_is_still_here(self):
        # The restore that brings back one of two rules of one position, the
        # other still here: by id, the one created again came last (review,
        # 01/10/2026); by name, the reading is the archive's.
        make_operation_rule("Virement exemple (A)", "transfer", r"^EXEMPLE", position=3)
        make_operation_rule("Virement exemple (B)", "transfer", r"^EXEMPLE 1", position=3)
        reading = recognition.describe(recognition.load(), "EXEMPLE 123").rule
        self.assertEqual(reading, "Virement exemple (A)")
        archive = export_archive({"banque"})
        OperationRule.objects.filter(name="Virement exemple (A)").delete()
        import_archive(archive, {"banque": REPLACE})
        self.assertEqual(recognition.describe(recognition.load(), "EXEMPLE 123").rule, reading)

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
        expected = {
            OPERATIONS: 6,
            PAYMENTS: 4,
            RULES: 2,
            RECOGNITION: len(SEEDED) + 1,
            FORMATS: 2,
            ALIASES: 1,
            PAYERS: 2,
        }
        for entity, number in expected.items():
            with self.subTest(entity=entity):
                counted = report.tallies[entity]
                self.assertEqual(
                    (counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, number)
                )
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
        IncomePayer.objects.update(created_at=datetime(2026, 9, 1, 12, 3, tzinfo=UTC))
        OperationRule.objects.update(created_at=datetime(2026, 9, 1, 12, 4, tzinfo=UTC))
        StatementFormat.objects.update(created_at=datetime(2026, 9, 1, 12, 5, tzinfo=UTC))
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
        self.assertEqual(
            (operations.created, operations.updated, operations.deleted, operations.unchanged), (1, 0, 0, 4)
        )
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
        self.assertEqual(
            BankTransaction.objects.get(fingerprint=self.income.fingerprint).imported_at, self.income.imported_at
        )
        self.assertEqual(InvoicePayment.objects.get(invoice=self.invoice_b).method, AUTO)
        self.assertTrue(IgnoreRule.objects.filter(pattern="PRET LOCAL", is_active=False).exists())
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
    """What a spending was FOR - on the line and on the rule that recognises
    it - is a decision like any other here: a statement imported again brings
    the line back and not one word of what a person said about it.
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

    def test_a_rules_category_comes_back_with_it(self):
        reader = self.export()
        wipe_bank()
        import_archive(reader, MERGE)
        self.assertEqual(IgnoreRule.objects.get(pattern="URSSAF").category, "Cotisations sociales")

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


class RecognitionTests(BankData, TestCase):
    """The recognition rules moved on after the export: one said otherwise
    here, one only here, one only in the archive. Merged like the payers:
    one changed here is a conflict, kept."""

    CHEQUES = "Remise de chèques (REMISE CHEQUE)"
    MEAL_VOUCHERS = "Titres-restaurant (EXEMPLE TR)"

    def setUp(self):
        super().setUp()
        self.before = BankSection().snapshot()
        self.rules = recognition_rules()
        self.reader = self.export()
        OperationRule.objects.filter(pk=self.terminal_rule.pk).update(pattern=r"EXEMPLE PAY VIREMENT")
        make_operation_rule(self.MEAL_VOUCHERS, "voucher", r"EXEMPLE TR", position=10)
        self.cheques = OperationRule.objects.get(name=self.CHEQUES)
        self.cheques.delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        counted = tally(run, RECOGNITION)
        self.assertEqual(
            (counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 0, 0, len(SEEDED) - 1)
        )
        self.assertEqual(
            bank_report(run).conflicts,
            [f"Règle de reconnaissance « {TERMINAL_RULE} » : différente dans l'archive (motif) — gardée telle quelle"],
        )
        self.assertEqual(OperationRule.objects.get(name=TERMINAL_RULE).pattern, r"EXEMPLE PAY VIREMENT")
        self.assertTrue(OperationRule.objects.filter(name=self.MEAL_VOUCHERS).exists())
        # Only in the archive: created as it was, in its place, with its moment.
        cheques = OperationRule.objects.get(name=self.CHEQUES)
        self.assertEqual(
            (cheques.position, cheques.meaning, cheques.searched, cheques.pattern, cheques.created_at),
            (8, "cheque", "bank_type", "REMISE CHEQUE", self.cheques.created_at),
        )
        # Nothing else of the bank moved, and a merge that only creates needs
        # no safety export.
        self.assertEqual(tally(run, OPERATIONS).unchanged, 6)
        self.assertFalse(run.affected())

    def test_replace_makes_the_rules_exactly_the_archives(self):
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, RECOGNITION)
        self.assertEqual(
            (counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 1, 1, len(SEEDED) - 1)
        )
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(recognition_rules(), self.rules)
        self.assertEqual(BankSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {"banque"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                preview = import_archive(self.reader, strategy, preview=True)
                self.assertEqual(db_fingerprint(), before)
                confirmed = import_archive(self.reader, strategy)
                self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_an_empty_list_forgets_every_rule_under_replace_only(self):
        # Said empty, the archive recognises nothing: « Remplacer » forgets
        # every rule, « Fusionner » adds nothing and forgets nothing.
        def change(payload):
            payload["operation_rules"] = []
            return payload

        reader = self.forged(change)
        here = OperationRule.objects.count()
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, RECOGNITION).deleted, 0)
        self.assertEqual(OperationRule.objects.count(), here)
        run = import_archive(reader, REPLACE)
        self.assertEqual(tally(run, RECOGNITION).deleted, here)
        self.assertFalse(OperationRule.objects.exists())


class RecognitionOrderAndNameTests(BankData, TestCase):
    """The order is part of what a rule says (the first of its kind that
    finds its pattern decides), and a rule is its name whatever its case and
    accents (`bank.recognition.name_key`)."""

    TRANSFERS = "Autre virement (VIR)"
    DEBITS = "Prélèvement (PRLV SEPA)"

    def setUp(self):
        super().setUp()
        self.rules = recognition_rules()
        self.reader = self.export()

    def test_a_rule_moved_here_is_a_conflict_and_replace_puts_it_back(self):
        # Asked first, « ^VIR » would take every transfer before the rules
        # reading their payee.
        OperationRule.objects.filter(name=self.TRANSFERS).update(position=1)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [f"Règle de reconnaissance « {self.TRANSFERS} » : différente dans l'archive (ordre) — gardée telle quelle"],
        )
        self.assertEqual(OperationRule.objects.get(name=self.TRANSFERS).position, 1)
        import_archive(self.reader, REPLACE)
        self.assertEqual(recognition_rules(), self.rules)

    def test_a_rule_spelt_otherwise_here_is_the_same_rule(self):
        OperationRule.objects.filter(name=self.DEBITS).update(name="PRELEVEMENT  (prlv sepa)")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [f"Règle de reconnaissance « {self.DEBITS} » : différente dans l'archive (nom) — gardée telle quelle"],
        )
        self.assertEqual((tally(run, RECOGNITION).created, OperationRule.objects.count()), (0, len(SEEDED) + 1))
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 1, 0))
        self.assertEqual(recognition_rules(), self.rules)

    def test_a_second_rule_of_one_name_here_is_one_too_many_under_replace(self):
        # The page refuses it; made some other way, the archive's rule is
        # the first of that name, and « Remplacer » leaves one.
        OperationRule.objects.create(
            name="prélèvement (PRLV SEPA)", meaning="debit", pattern=r"^PRLV", position=20, is_active=False
        )
        run = import_archive(self.reader, MERGE)
        self.assertEqual(tally(run, RECOGNITION).deleted, 0)
        self.assertEqual(OperationRule.objects.count(), len(SEEDED) + 2)
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 0, 1))
        self.assertEqual(recognition_rules(), self.rules)


#: A format valid on its own, which the archive's records below change.
ONE_COLUMN_LAYOUT = {
    "encoding": "auto",
    "delimiter": ";",
    "date_format": "dd/mm/yyyy",
    "decimal_mark": ",",
    "date_column": 1,
    "label_columns": "2",
    "amount_column": 3,
}


class StatementFormatTests(BankData, TestCase):
    """The statement formats moved on after the export: one said otherwise
    here, one only here, one only in the archive. Merged like the
    recognition rules: one changed here is a conflict, kept."""

    LOCAL = "Banque locale (CSV)"

    def setUp(self):
        super().setUp()
        self.before = BankSection().snapshot()
        self.formats = statement_formats()
        self.reader = self.export()
        StatementFormat.objects.filter(pk=self.tab_format.pk).update(decimal_mark=",")
        make_statement_format(self.LOCAL, position=3, **ONE_COLUMN_LAYOUT)
        self.seeded = StatementFormat.objects.get(name=SEEDED_FORMAT)
        self.seeded.delete()

    def test_merge_adds_what_is_missing_and_keeps_what_differs(self):
        run = import_archive(self.reader, MERGE)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 0, 0, 0))
        self.assertEqual(
            bank_report(run).conflicts,
            [f"Format de relevé « {TAB_FORMAT} » : différent dans l'archive (séparateur décimal) — gardé tel quel"],
        )
        self.assertEqual(StatementFormat.objects.get(pk=self.tab_format.pk).decimal_mark, ",")
        self.assertTrue(StatementFormat.objects.filter(name=self.LOCAL).exists())
        # Only in the archive: created as it was, in its place, with its moment.
        seeded = StatementFormat.objects.get(name=SEEDED_FORMAT)
        self.assertEqual({name: getattr(seeded, name) for name in SEEDED_LAYOUT}, SEEDED_LAYOUT)
        self.assertEqual(seeded.created_at, self.seeded.created_at)
        self.assertEqual(StatementFormat.objects.first(), seeded)
        # Nothing else of the bank moved, and a merge that only creates needs
        # no safety export.
        self.assertEqual(tally(run, OPERATIONS).unchanged, 6)
        self.assertFalse(run.affected())

    def test_replace_makes_the_formats_exactly_the_archives(self):
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (1, 1, 1, 0))
        self.assertEqual(bank_report(run).conflicts, [])
        self.assertEqual(statement_formats(), self.formats)
        self.assertEqual(BankSection().snapshot(), self.before)
        self.assertEqual(run.affected(), {"banque"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                preview = import_archive(self.reader, strategy, preview=True)
                self.assertEqual(db_fingerprint(), before)
                confirmed = import_archive(self.reader, strategy)
                self.assertEqual(preview.outcome(), confirmed.outcome())

    def test_an_empty_list_forgets_every_format_under_replace_only(self):
        # Said empty, the archive reads no statement: « Remplacer » forgets
        # every format, « Fusionner » adds nothing and forgets nothing.
        def change(payload):
            payload["statement_formats"] = []
            return payload

        reader = self.forged(change)
        here = StatementFormat.objects.count()
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, FORMATS).deleted, 0)
        self.assertEqual(StatementFormat.objects.count(), here)
        run = import_archive(reader, REPLACE)
        self.assertEqual(tally(run, FORMATS).deleted, here)
        self.assertFalse(StatementFormat.objects.exists())


class FormatOrderAndNameTests(BankData, TestCase):
    """The order is part of what a format says (the first is the one an
    import uses when nobody chooses), and a format is its name whatever its
    case and accents (`bank.recognition.name_key`)."""

    def setUp(self):
        super().setUp()
        self.formats = statement_formats()
        self.reader = self.export()

    def test_a_format_moved_here_is_a_conflict_and_replace_puts_it_back(self):
        # First here, the tab format is what an import reads a file with.
        StatementFormat.objects.filter(pk=self.tab_format.pk).update(position=0)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [f"Format de relevé « {TAB_FORMAT} » : différent dans l'archive (ordre) — gardé tel quel"],
        )
        self.assertEqual(StatementFormat.objects.first().name, TAB_FORMAT)
        import_archive(self.reader, REPLACE)
        self.assertEqual(statement_formats(), self.formats)
        self.assertEqual(StatementFormat.objects.first().name, SEEDED_FORMAT)

    def test_a_format_spelt_otherwise_here_is_the_same_format(self):
        StatementFormat.objects.filter(name=SEEDED_FORMAT).update(name="bnp  PARIBAS (csv)")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            bank_report(run).conflicts,
            [f"Format de relevé « {SEEDED_FORMAT} » : différent dans l'archive (nom) — gardé tel quel"],
        )
        self.assertEqual((tally(run, FORMATS).created, StatementFormat.objects.count()), (0, 2))
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 1, 0))
        self.assertEqual(statement_formats(), self.formats)

    def test_a_second_format_of_one_name_here_is_one_too_many_under_replace(self):
        # The page refuses it; made some other way, the archive's format is
        # the first of that name, and « Remplacer » leaves one.
        StatementFormat.objects.create(name="BNP PARIBAS (CSV)", position=20, **ONE_COLUMN_LAYOUT)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(tally(run, FORMATS).deleted, 0)
        self.assertEqual(StatementFormat.objects.count(), 3)
        run = import_archive(self.reader, REPLACE)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.created, counted.updated, counted.deleted), (0, 0, 1))
        self.assertEqual(statement_formats(), self.formats)


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


def written_before_formats(payload) -> dict:
    """banque.json as an archive written before bank/0007 holds it: no
    statement formats."""
    del payload["statement_formats"]
    return payload


def written_before_recognition(payload) -> dict:
    """banque.json as an archive written before bank/0006 holds it: no
    recognition rules - nor the statement formats, which came after them."""
    del payload["operation_rules"]
    return written_before_formats(payload)


def written_before_payers(payload) -> dict:
    """banque.json as an archive written before « En caisse » holds it: no
    payers retained, no line saying what it is in the till - and, older
    still than the recognition rules, none of those."""
    del payload["income_payers"]
    for record in payload["transactions"]:
        del record["income_source"]
    return written_before_recognition(payload)


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
        # Nor any recognition rule or statement format: nothing said,
        # nothing made.
        self.assertFalse(OperationRule.objects.exists())
        self.assertFalse(StatementFormat.objects.exists())
        counted = tally(run, FORMATS)
        self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 0))

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


class OldRecognitionArchiveTests(BankData, TestCase):
    """An archive written before bank/0006 says nothing of the recognition
    rules - which is not « recognise nothing »: whatever this database's
    rules say, merged or replaced, they stay as they are."""

    def setUp(self):
        super().setUp()
        self.reader = self.forged(written_before_recognition)
        # Moved on since: one changed, one added, one deleted.
        OperationRule.objects.filter(pk=self.terminal_rule.pk).update(pattern=r"EXEMPLE PAY VIREMENT")
        make_operation_rule("Titres-restaurant (EXEMPLE TR)", "voucher", r"EXEMPLE TR", position=10)
        OperationRule.objects.filter(name="Remise de chèques (REMISE CHEQUE)").delete()

    def test_neither_strategy_touches_the_rules_here(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                preview = import_archive(self.reader, strategy, preview=True)
                run = import_archive(self.reader, strategy)
                self.assertEqual(preview.outcome(), run.outcome())
                report = bank_report(run)
                self.assertEqual((report.conflicts, report.skipped), ([], []))
                self.assertEqual([note for note in report.notes if "champ inconnu" in note], [])
                counted = tally(run, RECOGNITION)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 0))
                self.assertEqual(db_fingerprint(), before)
                self.assertFalse(run.affected())


class OldFormatArchiveTests(BankData, TestCase):
    """An archive written before bank/0007 says nothing of the statement
    formats - which is not « read no statement »: whatever this database's
    formats say, merged or replaced, they stay as they are, and nothing is
    said about them."""

    def setUp(self):
        super().setUp()
        self.reader = self.forged(written_before_formats)
        # Moved on since: one changed, one added, one deleted.
        StatementFormat.objects.filter(pk=self.tab_format.pk).update(date_format="dd.mm.yyyy")
        make_statement_format("Banque locale (CSV)", position=3, **ONE_COLUMN_LAYOUT)
        StatementFormat.objects.filter(name=SEEDED_FORMAT).delete()

    def test_neither_strategy_touches_the_formats_here(self):
        for strategy in (MERGE, REPLACE):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                preview = import_archive(self.reader, strategy, preview=True)
                run = import_archive(self.reader, strategy)
                self.assertEqual(preview.outcome(), run.outcome())
                report = bank_report(run)
                self.assertEqual((report.conflicts, report.skipped), ([], []))
                self.assertEqual([note for note in report.notes if "champ inconnu" in note], [])
                counted = tally(run, FORMATS)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 0))
                self.assertEqual(db_fingerprint(), before)
                self.assertFalse(run.affected())

    def test_the_formats_after_a_wipe_stay_none(self):
        # Nothing said is nothing made: the page then refuses an import until
        # a format is typed - never a format guessed for an archive.
        wipe_bank()
        run = import_archive(self.reader, MERGE)
        self.assertFalse(StatementFormat.objects.exists())
        self.assertEqual(tally(run, FORMATS).created, 0)
        self.assertEqual(tally(run, OPERATIONS).created, 6)


class RecognitionCheckTests(BankData, TestCase):
    """What the archive says of the recognition rules, read before it is
    trusted: the model's own check runs on every rule written - created, or
    replaced - and a pattern the guard of `returnables.patterns` refuses is
    never compiled."""

    TRAP = "Prélèvement piégé"

    def change_record(self, name, **fields):
        """This database's export with the rule `name` said otherwise."""

        def change(payload):
            next(item for item in payload["operation_rules"] if item["name"] == name).update(fields)
            return payload

        return self.forged(change)

    def adding(self, *records) -> ArchiveReader:
        def change(payload):
            payload["operation_rules"] += list(records)
            return payload

        return self.forged(change)

    def test_rules_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        for value in ({}, "Carte", [TERMINAL_RULE], [{"name": "Carte exemple"}, 3]):
            with self.subTest(value=value):

                def change(payload, value=value):
                    payload["operation_rules"] = value
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                with self.assertRaisesMessage(ArchiveError, "« operation_rules » n'est pas une liste d'objets"):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)

    def test_a_rule_it_cannot_read_is_skipped_with_its_reason_and_the_others_come(self):
        first = dict(zip(("position", "name", "meaning", "searched", "pattern"), SEEDED[0], strict=True))
        refused = "motif refusé — "
        cases = [
            ({"meaning": "debit", "pattern": "PRLV"}, "sans nom"),
            ({"name": "  ", "meaning": "debit", "pattern": "PRLV"}, "sans nom"),
            ({"name": 7, "meaning": "debit", "pattern": "PRLV"}, "sans nom"),
            # The same name whatever its case, accents and spaces.
            (dict(first, name="PAIEMENT PAR CARTE  (facture carte)"), "en double dans l'archive"),
            (
                {"name": "Tout", "meaning": "other_operation", "pattern": "X|"},
                f"{refused}le motif accepte une ligne vide : il trouverait quelque chose sur n'importe quelle ligne",
            ),
            (
                {"name": "Prélèvement exemple", "meaning": "debit", "pattern": r"PRLV (?P<encaisse>[0-9]+)"},
                f"{refused}le groupe (?P<encaisse>…) ne sert à rien ; seuls (?P<tiers>…) servent à « Prélèvement »",
            ),
            ({"name": "Motif vide", "meaning": "debit", "pattern": ""}, f"{refused}le motif est vide"),
            (
                {"name": "Dépôt exemple", "meaning": "depot", "pattern": "DEPOT"},
                "« meaning » : valeur inconnue (« depot »)",
            ),
            (
                {"name": "Montant exemple", "meaning": "cash", "searched": "amount", "pattern": "ESPECES"},
                "« searched » : valeur inconnue (« amount »)",
            ),
            ({"name": "Sans sens", "pattern": "SENS"}, "« meaning » : valeur manquante"),
            ({"name": "Sans motif", "meaning": "debit"}, "« pattern » : valeur manquante"),
            ({"name": "Motif long", "meaning": "debit", "pattern": "P" * 301}, "« pattern » : plus de 300 caractères"),
            (
                {"name": "Ordre négatif", "meaning": "debit", "pattern": "NEGATIF", "position": -1},
                "« position » : nombre positif attendu (« -1 »)",
            ),
            # Past what SQLite holds: Django's own validator, said in French.
            (
                {"name": "Ordre énorme", "meaning": "debit", "pattern": "ENORME", "position": 2**63},
                "« position » : valeur refusée",
            ),
            # What SQLite still holds, and Django's validator lets through, but
            # past `MAX_POSITION`: the page's « last + 1 » would overflow.
            (
                {"name": "Ordre presque énorme", "meaning": "debit", "pattern": "PRESQUE", "position": 2**63 - 1},
                "« position » : valeur refusée",
            ),
            (
                {"name": "Active peut-être", "meaning": "debit", "pattern": "PEUT-ETRE", "is_active": "oui"},
                "« is_active » : oui ou non attendu (« oui »)",
            ),
            ({"name": "R" * 101, "meaning": "debit", "pattern": "LONG"}, "« name » : plus de 100 caractères"),
        ]
        reader = self.adding(*(record for record, _reason in cases))
        wipe_bank()
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped,
            [
                "Règle de reconnaissance sans nom"
                if reason == "sans nom"
                else f"Règle de reconnaissance « {record['name']} » : {reason}"
                for record, reason in cases
            ],
        )
        # Every rule it could read came, the first of two of one name kept.
        self.assertEqual(tally(run, RECOGNITION).created, len(SEEDED) + 1)
        self.assertEqual(OperationRule.objects.get(name=first["name"]).position, first["position"])

    def test_a_pattern_that_is_no_regular_expression_is_refused(self):
        reader = self.adding({"name": "Loyer exemple", "meaning": "debit", "pattern": "LOYER ("})
        run = import_archive(reader, MERGE)
        (skipped,) = bank_report(run).skipped
        self.assertTrue(skipped.startswith("Règle de reconnaissance « Loyer exemple » : motif refusé — "), skipped)
        self.assertFalse(OperationRule.objects.filter(name="Loyer exemple").exists())

    def test_a_pattern_the_guard_refuses_is_never_compiled(self):
        # Created, or replacing this database's: refused before `regex`
        # could freeze the machine compiling it (returnables/patterns.py).
        for pattern, reason in (
            (r"(?:x{65535}){65535}", "répétition trop grande"),
            (r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
            (r"a{e<=1}", "accolade"),
        ):
            for strategy, reader in (
                (MERGE, self.adding({"name": self.TRAP, "meaning": "debit", "pattern": pattern})),
                (REPLACE, self.change_record(TERMINAL_RULE, pattern=pattern)),
            ):
                with self.subTest(pattern=pattern, strategy=strategy):
                    never = NeverCompile()
                    with mock.patch.object(regex, "compile", new=never):
                        run = import_archive(reader, strategy)
                    self.assertEqual(never.calls, [])
                    (skipped,) = bank_report(run).skipped
                    self.assertIn(" : motif refusé — ", skipped)
                    self.assertIn(reason, skipped)
                    self.assertFalse(OperationRule.objects.filter(name=self.TRAP).exists())
                    self.assertEqual(OperationRule.objects.get(pk=self.terminal_rule.pk).pattern, r"EXEMPLE PAY REMISE")

    def test_replace_checks_the_pattern_it_writes_and_keeps_the_rule_when_refused(self):
        # The pattern is no key: « Remplacer » writes the archive's over this
        # database's, and checks it as one it creates.
        reader = self.change_record(TERMINAL_RULE, pattern=r"EXEMPLE|", position=12)
        before = db_fingerprint()
        preview = import_archive(reader, REPLACE, preview=True)
        run = import_archive(reader, REPLACE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(
            bank_report(run).skipped,
            [
                (
                    f"Règle de reconnaissance « {TERMINAL_RULE} » : motif refusé — le motif accepte une ligne vide : "
                    "il trouverait quelque chose sur n'importe quelle ligne"
                )
            ],
        )
        self.assertEqual(db_fingerprint(), before)
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, len(SEEDED)))

    def test_replace_keeps_a_rule_whose_record_it_cannot_read(self):
        reader = self.change_record(TERMINAL_RULE, meaning="tpe")
        run = import_archive(reader, REPLACE)
        self.assertEqual(
            bank_report(run).skipped,
            [f"Règle de reconnaissance « {TERMINAL_RULE} » : « meaning » : valeur inconnue (« tpe »)"],
        )
        self.assertEqual(OperationRule.objects.get(pk=self.terminal_rule.pk).meaning, "payout")
        counted = tally(run, RECOGNITION)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, len(SEEDED)))

    def test_an_unknown_field_of_a_rule_is_said_once(self):
        def change(payload):
            for item in payload["operation_rules"]:
                item["humeur"] = "calme"
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(bank_report(run).notes.count("champ inconnu ignoré : règles de reconnaissance › humeur"), 1)
        self.assertEqual(bank_report(run).skipped, [])

    def test_a_position_past_the_bound_is_refused_and_the_page_still_saves_after_one_at_it(self):
        """As for the formats: a new rule comes at the highest + 1, rules
        sharing a position are made distinct by + 1 (`views._saved`,
        `_move`), so a position the page cannot put another after is
        refused. One at `MAX_POSITION` is taken, and the page still saves
        and moves."""

        def made(name, position):
            return {"name": name, "meaning": "debit", "pattern": name.upper(), "position": position}

        reader = self.adding(
            made("Ordre presque énorme", 2**63 - 1),
            made("Ordre au plus haut A", section.MAX_POSITION),
            made("Ordre au plus haut B", section.MAX_POSITION),
        )
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped,
            ["Règle de reconnaissance « Ordre presque énorme » : « position » : valeur refusée"],
        )
        response = self.client.post(
            reverse("bank:recognition"),
            {
                "action": "enregistrer",
                "name": "Règle après",
                "meaning": "debit",
                "searched": "label",
                "pattern": "APRES",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OperationRule.objects.get(name="Règle après").position, section.MAX_POSITION + 1)
        first = OperationRule.objects.get(name="Ordre au plus haut A")
        response = self.client.post(reverse("bank:recognition_rule", args=[first.pk]), {"action": "descendre"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(OperationRule.objects.filter(position__gte=section.MAX_POSITION).values_list("name", flat=True)),
            ["Ordre au plus haut B", "Ordre au plus haut A", "Règle après"],
        )


#: What Django's own validators say, in English: never on a report.
DJANGO_ENGLISH = r"Ensure this value|This field|is not a valid|already exists"


class FormatCheckTests(BankData, TestCase):
    """What the archive says of the statement formats, read before it is
    trusted: the model's own check (`bank.statements.check_format`) runs on
    every format written - created, or replaced - its refusal said in
    French after the field it names, and an account pattern the guard of
    `returnables.patterns` refuses is never compiled."""

    TRAP = "Format piégé"

    def change_record(self, name, **fields):
        """This database's export with the format `name` said otherwise."""

        def change(payload):
            next(item for item in payload["statement_formats"] if item["name"] == name).update(fields)
            return payload

        return self.forged(change)

    def adding(self, *records) -> ArchiveReader:
        def change(payload):
            payload["statement_formats"] += list(records)
            return payload

        return self.forged(change)

    def test_formats_that_are_no_list_of_objects_refuse_the_archive_before_anything_is_written(self):
        for value in ({}, "BNP", [TAB_FORMAT], [{"name": "Banque exemple"}, 3]):
            with self.subTest(value=value):

                def change(payload, value=value):
                    payload["statement_formats"] = value
                    return payload

                reader = self.forged(change)
                before = db_fingerprint()
                with self.assertRaisesMessage(ArchiveError, "« statement_formats » n'est pas une liste d'objets"):
                    import_archive(reader, REPLACE)
                self.assertEqual(db_fingerprint(), before)

    def test_a_format_it_cannot_read_is_skipped_with_its_reason_and_the_others_come(self):
        def made(name, **fields):
            return {"name": name, **ONE_COLUMN_LAYOUT, **fields}

        refused = "format refusé — "
        without_labels = {name: value for name, value in made("Sans libellé").items() if name != "label_columns"}
        cases = [
            (dict(ONE_COLUMN_LAYOUT), "sans nom"),
            (made("  "), "sans nom"),
            (made(7), "sans nom"),
            # The same name whatever its case, accents and spaces.
            (made("bnp  PARIBAS (csv)"), "en double dans l'archive"),
            (made("Encodage exemple", encoding="latin-9"), "« encoding » : valeur inconnue (« latin-9 »)"),
            (made("Séparateur exemple", delimiter=":"), "« delimiter » : valeur inconnue (« : »)"),
            (made("Dates exemple", date_format="jj/mm/aaaa"), "« date_format » : valeur inconnue (« jj/mm/aaaa »)"),
            (made("Décimales exemple", decimal_mark=";"), "« decimal_mark » : valeur inconnue (« ; »)"),
            (made("Sans date", date_column=None), "« date_column » : valeur manquante"),
            (without_labels, "« label_columns » : valeur manquante"),
            (made("Date en texte", date_column="1"), "« date_column » : nombre entier attendu (« 1 »)"),
            (made("Date oui", date_column=True), "« date_column » : nombre entier attendu (« True »)"),
            (made("Montant négatif", amount_column=-3), "« amount_column » : nombre positif attendu (« -3 »)"),
            (made("Colonne zéro", date_column=0), f"{refused}colonne de la date : un numéro de colonne de 1 à 50"),
            # Past what SQLite holds, where Django's own validator refuses it
            # too: the check's own French, never Django's « Ensure this value
            # is less than or equal to … » beside it.
            (
                made("Colonne énorme", date_column=2**63),
                f"{refused}colonne de la date : un numéro de colonne de 1 à 50",
            ),
            (
                made("Libellé vide", label_columns=" "),
                f"{refused}colonnes du libellé : indiquez au moins une colonne pour le libellé",
            ),
            (
                made("Libellé illisible", label_columns="2, x"),
                f"{refused}colonnes du libellé : « x » n'est pas un numéro de colonne (de 1 à 50)",
            ),
            (
                made("Colonne deux fois", label_columns="1"),
                f"{refused}colonnes du libellé : la colonne 1 sert deux fois : pour la date et pour le libellé",
            ),
            (
                made("Sans montant", amount_column=None),
                f"{refused}colonne du montant : indiquez la colonne du montant, ou celles des débits et des crédits",
            ),
            (
                made("Montant deux fois", debit_column=4),
                (
                    f"{refused}colonne du montant : un montant signé OU des débits et des crédits : pas les deux "
                    "(laissez l'un vide)"
                ),
            ),
            (
                made("Compte exemple", account_pattern=r"COMPTE (?P<numero>[0-9]+)"),
                (
                    f"{refused}motif du numéro de compte : le groupe (?P<numero>…) ne sert à rien ; seul "
                    "(?P<compte>…) est lu"
                ),
            ),
            (made("Ordre négatif", position=-1), "« position » : nombre positif attendu (« -1 »)"),
            # Past what SQLite holds: Django's own validator, said in French.
            (made("Ordre énorme", position=2**63), "« position » : valeur refusée"),
            # What SQLite still holds, and Django's validator lets through, but
            # past `MAX_POSITION`: the page's « last + 1 » would overflow.
            (made("Ordre presque énorme", position=2**63 - 1), "« position » : valeur refusée"),
            (made("N" * 101), "« name » : plus de 100 caractères"),
            (made("Libellé long", label_columns="1" * 51), "« label_columns » : plus de 50 caractères"),
            (made("Motif long", account_pattern="C" * 301), "« account_pattern » : plus de 300 caractères"),
            # Debits alone, no credit column: a format the check takes.
            (made("Débits seuls", amount_column=None, debit_column=3), None),
        ]
        reader = self.adding(*(record for record, _reason in cases))
        wipe_bank()
        run = import_archive(reader, MERGE)
        skipped = bank_report(run).skipped
        self.assertEqual(
            skipped,
            [
                "Format de relevé sans nom"
                if reason == "sans nom"
                else f"Format de relevé « {record['name']} » : {reason}"
                for record, reason in cases
                if reason is not None
            ],
        )
        for reason in skipped:
            self.assertNotRegex(reason, DJANGO_ENGLISH)
        # Every format it could read came, the first of two of one name kept.
        self.assertEqual(tally(run, FORMATS).created, 3)
        seeded = StatementFormat.objects.get(name=SEEDED_FORMAT)
        self.assertEqual({name: getattr(seeded, name) for name in SEEDED_LAYOUT}, SEEDED_LAYOUT)
        self.assertEqual(StatementFormat.objects.get(name="Débits seuls").debit_column, 3)

    def test_an_account_pattern_that_is_no_regular_expression_is_refused(self):
        reader = self.adding({"name": "Banque exemple", **ONE_COLUMN_LAYOUT, "account_pattern": "COMPTE ("})
        run = import_archive(reader, MERGE)
        (skipped,) = bank_report(run).skipped
        self.assertTrue(
            skipped.startswith("Format de relevé « Banque exemple » : format refusé — motif du numéro de compte : "),
            skipped,
        )
        self.assertNotRegex(skipped, DJANGO_ENGLISH)
        self.assertFalse(StatementFormat.objects.filter(name="Banque exemple").exists())

    def test_an_account_pattern_the_guard_refuses_is_never_compiled(self):
        # Created, or replacing this database's: refused before `regex`
        # could freeze the machine compiling it (returnables/patterns.py).
        for pattern, reason in (
            (r"(?:x{65535}){65535}", "répétition trop grande"),
            (r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
            (r"a{e<=1}", "accolade"),
        ):
            for strategy, reader in (
                (MERGE, self.adding({"name": self.TRAP, **ONE_COLUMN_LAYOUT, "account_pattern": pattern})),
                (REPLACE, self.change_record(TAB_FORMAT, account_pattern=pattern)),
            ):
                with self.subTest(pattern=pattern, strategy=strategy):
                    never = NeverCompile()
                    with mock.patch.object(regex, "compile", new=never):
                        run = import_archive(reader, strategy)
                    self.assertEqual(never.calls, [])
                    (skipped,) = bank_report(run).skipped
                    self.assertIn(" : format refusé — motif du numéro de compte : ", skipped)
                    self.assertIn(reason, skipped)
                    self.assertFalse(StatementFormat.objects.filter(name=self.TRAP).exists())
                    self.assertEqual(
                        StatementFormat.objects.get(pk=self.tab_format.pk).account_pattern,
                        TAB_LAYOUT["account_pattern"],
                    )

    def test_replace_checks_the_format_it_writes_and_keeps_it_when_refused(self):
        # The columns are no key: « Remplacer » writes the archive's over this
        # database's, and checks them as a format it creates.
        reader = self.change_record(TAB_FORMAT, amount_column=7, position=12)
        before = db_fingerprint()
        preview = import_archive(reader, REPLACE, preview=True)
        run = import_archive(reader, REPLACE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(
            bank_report(run).skipped,
            [
                (
                    f"Format de relevé « {TAB_FORMAT} » : format refusé — colonne du montant : un montant signé OU "
                    "des débits et des crédits : pas les deux (laissez l'un vide)"
                )
            ],
        )
        self.assertEqual(db_fingerprint(), before)
        counted = tally(run, FORMATS)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, 1))

    def test_replace_keeps_a_format_whose_record_it_cannot_read(self):
        reader = self.change_record(TAB_FORMAT, encoding="latin-9")
        run = import_archive(reader, REPLACE)
        self.assertEqual(
            bank_report(run).skipped,
            [f"Format de relevé « {TAB_FORMAT} » : « encoding » : valeur inconnue (« latin-9 »)"],
        )
        self.assertEqual(StatementFormat.objects.get(pk=self.tab_format.pk).encoding, "utf-8")
        counted = tally(run, FORMATS)
        self.assertEqual((counted.updated, counted.deleted, counted.unchanged), (0, 0, 1))

    def test_an_unknown_field_of_a_format_is_said_once(self):
        def change(payload):
            for item in payload["statement_formats"]:
                item["humeur"] = "calme"
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(bank_report(run).notes.count("champ inconnu ignoré : formats de relevé › humeur"), 1)
        self.assertEqual(bank_report(run).skipped, [])

    def test_a_position_past_the_bound_is_refused_and_the_page_still_saves_after_one_at_it(self):
        """A position SQLite holds that the page cannot put another after -
        a new format comes at the highest + 1, and formats sharing one are
        made distinct by + 1 (`views._saved`, `_swapped`) - is refused: taken,
        every « Nouveau format » after it was a 500 (OverflowError). One at
        `MAX_POSITION` is taken, and the page still saves and moves."""
        reader = self.adding(
            {"name": "Ordre presque énorme", **ONE_COLUMN_LAYOUT, "position": 2**63 - 1},
            {"name": "Ordre au plus haut A", **ONE_COLUMN_LAYOUT, "position": section.MAX_POSITION},
            {"name": "Ordre au plus haut B", **ONE_COLUMN_LAYOUT, "position": section.MAX_POSITION},
        )
        run = import_archive(reader, MERGE)
        self.assertEqual(
            bank_report(run).skipped, ["Format de relevé « Ordre presque énorme » : « position » : valeur refusée"]
        )
        response = self.client.post(
            reverse("bank:statement_formats"), {"action": "enregistrer", "name": "Banque après", **ONE_COLUMN_LAYOUT}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StatementFormat.objects.get(name="Banque après").position, section.MAX_POSITION + 1)
        first = StatementFormat.objects.get(name="Ordre au plus haut A")
        response = self.client.post(reverse("bank:statement_format", args=[first.pk]), {"action": "descendre"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(StatementFormat.objects.filter(position__gte=section.MAX_POSITION).values_list("name", flat=True)),
            ["Ordre au plus haut B", "Ordre au plus haut A", "Banque après"],
        )


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

    def test_a_rule_matching_every_label_is_refused(self):
        def change(payload):
            payload["rules"].append({"pattern": "URSSAF|", "description": "trop large", "is_active": True})
            return payload

        reader = self.forged(change)
        run = import_archive(reader, MERGE)
        self.assertIn(
            (
                "Règle « URSSAF| » : Motif : le motif accepte une ligne vide : il trouverait quelque chose sur "
                "n'importe quelle ligne."
            ),
            bank_report(run).skipped,
        )
        self.assertFalse(IgnoreRule.objects.filter(pattern="URSSAF|").exists())

    def test_a_rule_that_is_no_regular_expression_is_refused(self):
        def change(payload):
            payload["rules"].append({"pattern": "LOYER (", "description": "", "is_active": True})
            return payload

        run = import_archive(self.forged(change), MERGE)
        self.assertEqual(bank_report(run).skipped, ["Règle « LOYER ( » : Motif : parenthèse non fermée (position 7)."])
        self.assertFalse(IgnoreRule.objects.filter(pattern="LOYER (").exists())

    def test_a_count_past_what_re_can_hold_is_refused_on_the_preview_and_the_import(self):
        """`re.compile` raised OverflowError on it, not re.error: the preview
        of « Données » itself was a 500, and so was the import."""
        pattern = "A{4294967296}"

        def change(payload):
            payload["rules"].append({"pattern": pattern, "description": "énorme", "is_active": True})
            return payload

        reader = self.forged(change)
        before = db_fingerprint()
        preview = import_archive(reader, MERGE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        run = import_archive(reader, MERGE)
        self.assertEqual(preview.outcome(), run.outcome())
        self.assertEqual(bank_report(run).skipped, [f"Règle « {pattern} » : Motif : motif invalide."])
        self.assertFalse(IgnoreRule.objects.filter(pattern=pattern).exists())

    def test_a_pattern_the_guard_refuses_is_never_compiled(self):
        """An archive carries one bar's rules into another: a foreign pattern
        is refused before `regex` could freeze the machine compiling it."""
        for pattern, reason in (
            (r"(?:x{65535}){65535}", "répétition trop grande"),
            (r"a{e<=1}", "accolade"),
        ):
            with self.subTest(pattern=pattern):

                def change(payload, pattern=pattern):
                    payload["rules"].append({"pattern": pattern, "description": "piégée", "is_active": True})
                    return payload

                reader = self.forged(change)
                never = NeverCompile()
                with mock.patch.object(regex, "compile", new=never):
                    run = import_archive(reader, MERGE)
                self.assertEqual(never.calls, [])
                (skipped,) = bank_report(run).skipped
                self.assertTrue(skipped.startswith(f"Règle « {pattern} » : Motif : "), skipped)
                self.assertIn(reason, skipped)
                self.assertFalse(IgnoreRule.objects.filter(pattern=pattern).exists())

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

    def test_a_rule_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    urssaf = next(item for item in payload["rules"] if item["pattern"] == "URSSAF")
                    urssaf.update(description="Cotisations du trimestre", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(change, f"Règle « URSSAF » : « created_at » : {reason}")
                rule = IgnoreRule.objects.get(pattern="URSSAF")
                self.assertEqual(
                    (rule.description, rule.created_at), ("Cotisations", datetime(2026, 7, 20, 8, 30, tzinfo=UTC))
                )
                counted = tally(run, RULES)
                self.assertEqual((counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, 1))

    def test_a_recognition_rule_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    terminal = next(item for item in payload["operation_rules"] if item["name"] == TERMINAL_RULE)
                    terminal.update(pattern=r"EXEMPLE PAY VIREMENT", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Règle de reconnaissance « {TERMINAL_RULE} » : « created_at » : {reason}"
                )
                rule = OperationRule.objects.get(pk=self.terminal_rule.pk)
                self.assertEqual((rule.pattern, rule.created_at), (r"EXEMPLE PAY REMISE", RECOGNITION_MOMENT))
                counted = tally(run, RECOGNITION)
                self.assertEqual(
                    (counted.created, counted.updated, counted.deleted, counted.unchanged), (0, 0, 0, len(SEEDED))
                )

    def test_a_statement_format_whose_moment_cannot_be_read_is_kept_as_it_was(self):
        for moment, reason in self.MOMENTS.items():
            with self.subTest(moment=moment):

                def change(payload, moment=moment):
                    tab = next(item for item in payload["statement_formats"] if item["name"] == TAB_FORMAT)
                    tab.update(decimal_mark=",", created_at=moment)
                    return payload

                run = self.assert_skipped_as_previewed(
                    change, f"Format de relevé « {TAB_FORMAT} » : « created_at » : {reason}"
                )
                fmt = StatementFormat.objects.get(pk=self.tab_format.pk)
                self.assertEqual((fmt.decimal_mark, fmt.created_at), (".", FORMAT_MOMENT))
                counted = tally(run, FORMATS)
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
        imported - recognised again here, by rules changed since (or by
        none, the bank wiped), they would be something else, in silence."""
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
        self.assertEqual(
            deleted,
            {
                OPERATIONS: 6,
                PAYMENTS: 4,
                RULES: 2,
                RECOGNITION: len(SEEDED) + 1,
                FORMATS: 2,
                ALIASES: 1,
                PAYERS: 2,
            },
        )
        self.assertIn(section.CLEAR_NOTE, bank_report(run).notes)
        self.assertEqual(run.affected(), {"banque"})
        # The invoices and suppliers the bank pointed at are not the bank's.
        self.assertEqual(self.grocer.invoices.count(), 3)
        self.assertTrue(Supplier.objects.filter(pk=self.hardware.pk).exists())

    def test_the_recognition_rules_go_and_the_report_says_what_that_costs(self):
        # The seeded ones too: until they are back, a statement imported
        # recognises nothing. The Effacer tab says it before the clear.
        run = run_clear({"banque"}, preview=False)
        self.assertFalse(OperationRule.objects.exists())
        self.assertIn(RECOGNITION_CLEAR_NOTE, bank_report(run).notes)
        self.assertIn("Reconnaissance des opérations", RECOGNITION_CLEAR_NOTE)
        self.assertIn("règles de reconnaissance", registry.INFO["banque"].clear_note)
        self.assertIn("la sauvegarde", registry.INFO["banque"].clear_note)
        # Nothing to take, nothing said.
        run = run_clear({"banque"}, preview=False)
        self.assertNotIn(RECOGNITION_CLEAR_NOTE, bank_report(run).notes)

    def test_the_statement_formats_go_and_the_report_says_what_that_costs(self):
        # The seeded one too: until one is back, an import is refused. The
        # Effacer tab says it before the clear.
        run = run_clear({"banque"}, preview=False)
        self.assertFalse(StatementFormat.objects.exists())
        self.assertEqual(tally(run, FORMATS).deleted, 2)
        self.assertIn(FORMAT_CLEAR_NOTE, bank_report(run).notes)
        self.assertIn("Format du relevé", FORMAT_CLEAR_NOTE)
        self.assertIn("sauvegarde", FORMAT_CLEAR_NOTE)
        self.assertIn("formats de relevé", registry.INFO["banque"].clear_note)
        self.assertIn("formats de relevé", registry.INFO["banque"].description)
        # Nothing to take, nothing said.
        run = run_clear({"banque"}, preview=False)
        self.assertNotIn(FORMAT_CLEAR_NOTE, bank_report(run).notes)

    def test_the_formats_alone_say_their_note(self):
        # Every line and rule already gone: the formats' note is the one said.
        OperationRule.objects.all().delete()
        BankTransaction.objects.all().delete()
        run = run_clear({"banque"}, preview=False)
        notes = bank_report(run).notes
        self.assertIn(FORMAT_CLEAR_NOTE, notes)
        self.assertNotIn(RECOGNITION_CLEAR_NOTE, notes)
        self.assertNotIn(section.CLEAR_NOTE, notes)

    def test_clearing_the_bank_clears_only_the_bank(self):
        self.assertEqual(registry.closure({"banque"}, "clear"), {"banque"})
