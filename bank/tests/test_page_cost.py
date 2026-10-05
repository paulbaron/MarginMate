"""What the bank pages cost - and every shortcut that brought it down
printing exactly what it replaced, on the edges the page's data does not
happen to hold.

Measured on a copy of a year of statements (01/10/2026): an ignore rule
written « .*WORD.* » was searched sixty times slower than « WORD », a
queryset made per bank line for its payments was a tenth of « Banque », and
`{% url %}` and the pick-list's five variables a row were most of a long tab.
"""

import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext, override_script_prefix
from django.urls import reverse

from bank import income, reconcile, views
from bank.models import BankTransaction, IgnoreRule, TreasuryAdjustment, TreasuryCheckpoint
from bank.rules import check, compile_rules, ignoring_rule, searcher
from common import DateRange
from invoices.models import Invoice, Supplier
from recipes.models import PosProduct, PosProductDailyQuantity
from returnables.patterns import PatternError
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_sale_document,
    make_sale_line,
    make_sale_payment,
    make_supplier,
)

#: Patterns with a leading « .* » - and the shapes around it that must not
#: be taken for one - against labels with the edges a search has.
PATTERNS = (
    ".*URSSAF.*",
    ".*urssaf",
    ".*.*FRAIS",
    ".*PRET|DGFIP",
    ".*|URSSAF",
    ".*",
    ".*^PRLV",
    ".*$",
    ".*\\bSEPA\\b.*",
    ".*(SEPA) \\1",
    ".*(?<=PRLV )SEPA",
    ".*?CARTE",
    ".*+CARTE",
    ".*\\.*X",
    ".*[.*]",
    ".*ECH/\\d{6}",
    ".*{x",
    "\\.*URSSAF",
    "(.*)URSSAF",
    "URSSAF.*",
)
LABELS = (
    "PRLV SEPA URSSAF D ILE DE FRANCE ECH/180826",
    "PRLV SEPA SEPA ECH/090726",
    "",
    "FRAIS\nURSSAF",
    "frais tenue de compte",
    "FACTURE CARTE DU 150726 CARTE 4974",
    "a.*b",
    "x{x",
    "DGFIP",
    "...X",
)


#: Of `PATTERNS`, those the guard refuses as written (`rules.check`): an
#: empty label matches them, or a « { » is no count.
REFUSED = (".*", ".*|URSSAF", ".*$", ".*{x")


class SearcherTests(SimpleTestCase):
    def test_it_finds_exactly_what_the_pattern_as_written_finds(self):
        for pattern in PATTERNS:
            if pattern in REFUSED:
                continue
            written = check(pattern)
            searched = searcher(pattern)
            # And what `re` found with its flags then: the guard adds
            # MULTILINE, which only a label holding a line break could tell,
            # and the import folds every space and break of a label.
            before = re.compile(pattern, re.IGNORECASE)
            for label in LABELS:
                with self.subTest(pattern=pattern, label=label):
                    self.assertEqual(bool(searched.search(label)), bool(written.search(label)))
                    if "\n" not in label:
                        self.assertEqual(bool(searched.search(label)), bool(before.search(label)))

    def test_a_leading_greedy_dot_star_is_not_searched(self):
        """What makes it quick: « .* » ran to the end of the label and back
        at every position."""
        self.assertEqual(searcher(".*URSSAF.*").pattern, "URSSAF.*")
        self.assertEqual(searcher(".*.*FRAIS").pattern, "FRAIS")
        for kept in (".*?CARTE", ".*+CARTE", "\\.*URSSAF", "(.*)URSSAF"):
            with self.subTest(pattern=kept):
                self.assertEqual(searcher(kept).pattern, kept)

    def test_what_the_guard_refuses_as_written_is_refused_whatever_its_rest(self):
        """The pattern as written is what is checked: « .*{x » leaves « {x »,
        « .* » nothing at all."""
        for pattern in (*REFUSED, ".*(", ".*)", ".**"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(PatternError):
                    check(pattern)
                with self.assertRaises(PatternError):
                    searcher(pattern)

    def test_the_rules_answer_as_before(self):
        class Rule:
            def __init__(self, pattern):
                self.pattern = pattern

        rules = [Rule(".*URSSAF.*"), Rule(".*("), Rule("PRET"), Rule(".*SEPA")]
        compiled = compile_rules(rules)
        self.assertEqual([rule.pattern for rule, _matcher in compiled.rules], [".*URSSAF.*", "PRET", ".*SEPA"])
        self.assertEqual([rule.pattern for rule, _sentence in compiled.invalid], [".*("])
        self.assertIs(ignoring_rule("PRLV SEPA URSSAF", compiled), rules[0])
        self.assertIs(ignoring_rule("ECHEANCE PRET SEPA", compiled), rules[2])
        self.assertIs(ignoring_rule("PRLV SEPA EXEMPLE", compiled), rules[3])
        self.assertIsNone(ignoring_rule("FACTURE CARTE", compiled))


class ByPkTests(SimpleTestCase):
    NAMES = (
        "bank:bank_line_action",
        "bank:invoice_search",
        "invoices:invoice_detail",
        "bank:income_source",
        "bank:treasury_point",
        "bank:treasury_adjustment",
        "bank:sale_document_search",
        "recipes:sale_document_update",
    )
    PKS = (1, 7, 10, 99, 123456789, int(views.URL_PLACEHOLDER), 10**20)

    def test_it_gives_what_reverse_gives(self):
        for name in self.NAMES:
            address = views._by_pk(name)
            for pk in self.PKS:
                with self.subTest(name=name, pk=pk):
                    self.assertEqual(address(pk), reverse(name, args=[pk]))

    def test_under_a_script_prefix_too(self):
        with override_script_prefix("/sous-dossier/"):
            for name in self.NAMES:
                with self.subTest(name=name):
                    self.assertEqual(views._by_pk(name)(42), reverse(name, args=[42]))


#: The pick-list's option as the template printed it from its variables,
#: before `views._choice_label` worded it once a page.
OPTION_AS_IT_WAS = Template(
    '{% load assets %}<option value="{{ invoice.pk }}">{{ invoice.supplier.name }} n° '
    '{{ invoice.invoice_number|default:invoice.pk }} · {{ invoice.invoice_date|date:"d/m/Y"|default:"sans date" }}'
    " · {{ total|money }} €</option>"
)
OPTION_NOW = Template('<option value="{{ value }}">{{ label }}</option>')


class ChoiceLabelTests(TestCase):
    def assertSameOption(self, invoice, total):
        invoice = Invoice.objects.select_related("supplier").get(pk=invoice.pk)
        old = OPTION_AS_IT_WAS.render(Context({"invoice": invoice, "total": total}))
        value, label = views.localize(invoice.pk), views._choice_label(invoice, total)
        self.assertEqual(OPTION_NOW.render(Context({"value": value, "label": label})), old)

    def test_the_option_reads_as_it_did(self):
        awkward = make_supplier(name='L\'Épicerie & <Fils> "Exemple"')
        plain = make_invoice(supplier=awkward, invoice_date=date(2026, 7, 9), invoice_number="FA&2026/12 <b>")
        bare = make_invoice(supplier=awkward, invoice_date=date(2026, 7, 9))
        Invoice.objects.filter(pk=bare.pk).update(invoice_number="", invoice_date=None)
        ancient = make_invoice(supplier=awkward, invoice_date=date(1, 1, 1))
        for invoice in (plain, bare, ancient):
            for total in (Decimal("0.00"), Decimal("12.30"), Decimal("-7.05"), Decimal("1234567.89")):
                with self.subTest(invoice=invoice.pk, total=total):
                    self.assertSameOption(invoice, total)


#: The invoices whose lines a query reads.
LINES_OF = re.compile(r'FROM "invoices_invoiceline" .*"invoices_invoiceline"\."invoice_id" IN \(([^)]*)\)')
#: Bank lines read by their pks (« id IN (...) », or « id = ... OR ... » for a few).
LINES_BY_PK = re.compile(r'FROM "bank_banktransaction" WHERE \(?"bank_banktransaction"\."id" (IN|=)')


def debit(day, label, amount, n, **fields):
    return BankTransaction.objects.create(
        operation_date=day,
        bank_type="PRLV SEPA",
        kind=BankTransaction.Kind.DEBIT,
        label=label,
        counterparty=fields.pop("counterparty", "EXEMPLE"),
        amount=-Decimal(amount),
        fingerprint=f"page-cost-{n}",
        **fields,
    )


class Statement:
    def setUp(self):
        self.supplier = Supplier.objects.get(code="METRO")
        IgnoreRule.objects.create(pattern=".*LOYER.*", description="Loyer")
        self.made = 0

    def statement(self, count):
        """`count` of each kind: a linked debit (every other invoice paid
        twice), an open one with unpaid invoices beside it, one a rule
        covers, one marked by hand, and a credit - and a typed sale document
        a credit pays, an electronic one a credit's amount matches. Sale
        documents and links on EVERY call: Django skips a prefetch on an
        empty list, and a fixture holding them on one side only would count
        different queries for nothing."""
        for _ in range(count):
            self.made += 1
            n = self.made
            day = date(2026, 1, 1) + timedelta(days=n)
            paid = make_invoice(supplier=self.supplier, invoice_date=day)
            for _line in range(3):
                make_invoice_line(invoice=paid, product=make_product(supplier=self.supplier), total_ht="10.00")
            reconcile.link(debit(day, f"PRLV SEPA METRO {n}", "36.00", f"l{n}"), [paid])
            if n % 2:
                reconcile.link(debit(day, f"PRLV SEPA METRO BIS {n}", "36.00", f"b{n}"), [paid])
            for _unpaid in range(2):
                unpaid = make_invoice(supplier=self.supplier, invoice_date=day)
                make_invoice_line(invoice=unpaid, product=make_product(supplier=self.supplier), total_ht="5.00")
            debit(day, f"PRLV SEPA METRO OUVERTE {n}", "6.00", f"o{n}")
            debit(day, f"PRLV SEPA LOYER {n}", "900.00", f"r{n}")
            debit(day, f"PRLV SEPA DIVERS {n}", "3.00", f"m{n}", no_invoice=True, settled_by_hand=True)
            BankTransaction.objects.create(
                operation_date=day,
                bank_type="VIREMENT",
                kind=BankTransaction.Kind.TRANSFER,
                label=f"VIR SEPA EXEMPLE {n}",
                amount=Decimal("50.00"),
                fingerprint=f"page-cost-c{n}",
            )
            typed = make_sale_document(reference=f"FV-COUT-{n}", customer=SALE_CUSTOMER, sold_on=day)
            make_sale_line(typed, label="Location de salle", unit_price_ttc="70.00")
            make_sale_payment(typed, sale_credit(day, "70.00", f"s{n}"))
            read = make_sale_document(
                reference=f"FE-COUT-{n}",
                customer=SALE_CUSTOMER,
                sold_on=day,
                stated_total_ttc="120.00",
                einvoice_format="CII",
                einvoice_type_code="380",
                seller_name="Bar des tests",
            )
            make_sale_line(read, label="Formule cocktail", quantity="1", total_ht="100.00", vat_rate="0.20")
            sale_credit(day, "120.00", f"e{n}")


#: The customer of the sale documents `Statement` makes, and its credits'
#: payer as the bank prints it.
SALE_CUSTOMER = "Exemple Événements SARL"
SALE_PAYER = "EXEMPLE EVENEMENTS SARL"


def sale_credit(day, amount, n) -> BankTransaction:
    return BankTransaction.objects.create(
        operation_date=day,
        bank_type="VIREMENT",
        kind=BankTransaction.Kind.TRANSFER,
        label=f"VIR SEPA RECU /FRM {SALE_PAYER} /REF {n}",
        counterparty=SALE_PAYER,
        amount=Decimal(amount),
        fingerprint=f"page-cost-{n}",
    )


class BankPageQueriesTests(Statement, TestCase):
    """Every tab of « Banque » costs the same number of queries whatever the
    statement holds: the payments are one query for every line, what a link
    shows besides is three for the rows on screen, the unpaid invoices two."""

    VIEWS = ("a-traiter", "rapprochees", "sans-facture", "toutes", "entrees", "par-beneficiaire")

    def queries(self, view):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("bank:bank_home"), {"vue": view})
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries), response

    def test_three_times_the_statement_costs_no_more_queries(self):
        self.statement(2)
        few = {view: self.queries(view)[0] for view in self.VIEWS}
        self.statement(4)
        for view in self.VIEWS:
            with self.subTest(view=view):
                count, response = self.queries(view)
                self.assertEqual(count, few[view])
        # And the rows were drawn: six debits each paying an invoice, three
        # of whose invoices another debit pays too.
        _count, response = self.queries("rapprochees")
        self.assertContains(response, "Aussi réglée par l'opération du", count=6)

    def test_the_operations_tab_reads_no_line_of_an_invoice_already_paid(self):
        """« Sans facture » shows no link: what a link shows - the invoice's
        lines, the other lines paying it - is not read for it."""
        self.statement(3)
        paid = set(Invoice.objects.filter(payments__isnull=False).values_list("pk", flat=True))
        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("bank:bank_home"), {"vue": "a-traiter"})
        read = set()
        for query in captured.captured_queries:
            asked = LINES_OF.search(query["sql"])
            if asked:
                read |= {int(pk) for pk in asked.group(1).split(",")}
        self.assertTrue(read)  # the unpaid invoices' lines, for their totals
        self.assertFalse(read & paid)

    def test_an_invoice_s_lines_are_read_for_their_total_alone(self):
        """A link and the pick-list read of each line the columns its total is
        made of (`reconcile.total_lines`): its name, quantity, volume and
        costs were most of what a tab loaded, for nothing."""
        self.statement(2)
        for view in ("a-traiter", "rapprochees", "toutes"):
            with self.subTest(view=view), CaptureQueriesContext(connection) as captured:
                self.client.get(reverse("bank:bank_home"), {"vue": view})
            lines = [
                query["sql"] for query in captured.captured_queries if 'FROM "invoices_invoiceline"' in query["sql"]
            ]
            self.assertTrue(lines)
            for sql in lines:
                self.assertNotIn('"raw_name"', sql)

    def test_the_other_lines_paying_a_linked_invoice_come_with_their_payments(self):
        """One query for the payments of the linked invoices and the lines
        making them, rather than one for each."""
        self.statement(2)
        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("bank:bank_home"), {"vue": "rapprochees"})
        by_pk = [query["sql"] for query in captured.captured_queries if LINES_BY_PK.search(query["sql"])]
        self.assertEqual(by_pk, [])


class RoundedTotalTests(TestCase):
    """`reconcile.rounded_total` on an invoice `unpaid_invoices` loaded - its
    lines in a list, a few columns each - is the invoice's own `total_ttc`
    rounded, on every branch of `Invoice.total_ttc_of`, and costs no query."""

    def assertSameTotal(self, invoice):
        loaded = {each.pk: each for each in reconcile.unpaid_invoices()}[invoice.pk]
        with self.assertNumQueries(0):
            total = reconcile.rounded_total(loaded)
        # Read the plain way: every column, through the manager.
        self.assertEqual(total, reconcile.rounded_total(Invoice.objects.get(pk=invoice.pk)))

    def test_every_branch_of_the_total(self):
        supplier = make_supplier()
        printed = make_invoice(supplier=supplier, printed_total_ttc=Decimal("12.01"))
        make_invoice_line(invoice=printed, total_ht="5.00", vat_rate="0.055", printed_ttc="5.28", discount_ttc="0.10")
        make_invoice_line(invoice=printed, total_ht="5.70", vat_rate="0.20", printed_ttc="6.84")
        from_ht = make_invoice(supplier=supplier, reconciliation_adjustment=Decimal("1.50"))
        make_invoice_line(invoice=from_ht, total_ht="10.00", vat_rate="0.20", taxes="2.00")
        make_invoice_line(invoice=from_ht, total_ht="33.33", vat_rate="0.055")
        stated = make_invoice(supplier=supplier, printed_total_ttc=Decimal("229.39"), einvoice_format="ubl")
        make_invoice_line(invoice=stated, total_ht="169.00", vat_rate="0.20")
        make_invoice_line(invoice=stated, total_ht="25.20", vat_rate="0.055")
        empty = make_invoice(supplier=supplier)
        Invoice.objects.filter(pk=empty.pk).update(invoice_date=None)
        for invoice in (printed, from_ht, stated, empty):
            with self.subTest(invoice=invoice.pk):
                self.assertSameTotal(invoice)


def old_choices(row, invoices, totals):
    """The pick-list as `views._fill` chose it before 04/10/2026: every
    unpaid invoice of the page tested against the row's dates, the window
    sorted whole - the oracle the quicker choice is held to."""
    first, last = reconcile.choices_window(row.line)
    near = [invoice for invoice in invoices if invoice.invoice_date is None or first <= invoice.invoice_date <= last]
    due = row.line.amount_due
    near.sort(
        key=lambda invoice: (
            abs(totals[invoice.pk] - due),
            abs((invoice.invoice_date - row.line.paid_on).days) if invoice.invoice_date else views.UNDATED_LAST,
        )
    )
    return [views.localize(invoice.pk) for invoice in near[: views.MAX_CHOICES]], max(len(near) - views.MAX_CHOICES, 0)


class PickListTests(TestCase):
    """The pick-list offers the same fifteen invoices, in the same order and
    with the same « de plus », as when every row sorted every unpaid invoice
    of the page - ties on the amount and the day, undated invoices, the
    window's edges - without reading every one of them for every row."""

    def test_the_same_options_in_the_same_order(self):
        supplier = make_supplier()
        paid_on = date(2026, 6, 15)
        # Each day of the window's edges and inside it, four invoices: two
        # of 10,00 (a tie on everything), 11,00 and 9,00.
        for offset in (-181, -180, -179, -30, -3, 0, 3, 7, 8):
            for amount in ("10.00", "11.00", "10.00", "9.00"):
                invoice = make_invoice(supplier=supplier, invoice_date=paid_on + timedelta(days=offset))
                make_invoice_line(invoice=invoice, total_ht=amount)
        for _undated in range(3):
            undated = make_invoice(supplier=supplier)
            make_invoice_line(invoice=undated, total_ht="10.00")
            Invoice.objects.filter(pk=undated.pk).update(invoice_date=None)
        shifts = ((0, "10.00"), (0, "12.00"), (-60, "10.00"), (3, "9.00"), (200, "10.00"), (-400, "9.00"))
        lines = [
            debit(paid_on + timedelta(days=shift), f"PRLV SEPA EXEMPLE {n}", amount, f"pick{n}")
            for n, (shift, amount) in enumerate(shifts)
        ]
        rows = [views.Row(line, views.TODO) for line in lines[:-1]] + [views.Row(lines[-1], views.LINKED)]
        views._fill(rows)

        start, _end = reconcile.search_window(lines)
        invoices = list(reconcile.unpaid_invoices(start, max(line.paid_on for line in lines) + timedelta(days=7)))
        totals = {invoice.pk: reconcile.rounded_total(invoice) for invoice in invoices}
        for row in rows:
            with self.subTest(line=row.line.label):
                chosen, more = old_choices(row, invoices, totals)
                self.assertEqual([value for value, _label in row.choices], chosen)
                self.assertEqual(row.more_choices, more)
        self.assertEqual(rows[0].more_choices, 7 * 4 + 3 - views.MAX_CHOICES)
        self.assertEqual(len(rows[-1].choices), 3)  # the undated alone

    def test_a_row_reads_the_invoices_near_its_dates_not_every_one(self):
        """Over years of unpaid invoices - paid in cash, never on the bank -
        every row testing every one was rows x invoices."""
        supplier = make_supplier()
        for n in range(120):
            invoice = make_invoice(supplier=supplier, invoice_date=date(2022, 1, 1) + timedelta(days=15 * n))
            make_invoice_line(invoice=invoice, total_ht="10.00")
        lines = [
            debit(date(2022, 1, 1) + timedelta(days=60 * n), f"PRLV SEPA EXEMPLE {n}", "10.00", f"r{n}")
            for n in range(30)
        ]
        rows = [views.Row(line, views.LINKED) for line in lines]
        reads = Counter()

        class Counted:
            """An invoice, counting how often its date is read."""

            def __init__(self, invoice):
                self.invoice = invoice

            def __getattr__(self, name):
                return getattr(self.invoice, name)

            @property
            def invoice_date(self):
                reads["date"] += 1
                return self.invoice.invoice_date

        unpaid = reconcile.unpaid_invoices
        with mock.patch.object(
            reconcile, "unpaid_invoices", lambda start, end: [Counted(i) for i in unpaid(start, end)]
        ):
            views._fill(rows)
        self.assertTrue(all(row.choices for row in rows))
        self.assertLess(reads["date"], len(rows) * 120)


class ProposalsAndRulesQueriesTests(Statement, TestCase):
    def test_proposals_cost_no_more_queries(self):
        self.statement(2)
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("bank:proposals"))
        self.statement(4)
        with self.assertNumQueries(len(few.captured_queries)):
            self.client.get(reverse("bank:proposals"))

    def test_the_rules_page_costs_no_more_queries_and_counts_the_linked(self):
        IgnoreRule.objects.create(pattern="METRO", description="Trop large")
        self.statement(2)
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("bank:rule_list"))
        self.statement(4)
        with self.assertNumQueries(len(few.captured_queries)):
            response = self.client.get(reverse("bank:rule_list"))
        found = {rule.description: matches for rule, matches in response.context["rules"]}
        # Six debits pay an invoice, three more pay one of those again, and
        # six are still open.
        self.assertEqual((found["Trop large"].count, found["Trop large"].linked), (15, 9))
        self.assertEqual((found["Loyer"].count, found["Loyer"].linked), (6, 0))


class TreasuryPageQueriesTests(TestCase):
    """« Trésorerie » costs the same whatever the history holds: the
    treasury is `treasury.QUERIES` queries, and every row's addresses one
    reverse for the page (`views._by_pk`)."""

    def history(self, start, count):
        """`count` days four days apart, each with an operation of +5,00, a
        balance typed the day after it 10,00 above the one before - 4,00
        more than the operation and an adjustment of +1,00 explain: a gap to
        resolve each - and that adjustment."""
        for n in range(start, start + count):
            day = date(2026, 1, 1) + timedelta(days=4 * n)
            BankTransaction.objects.create(
                operation_date=day, label=f"VIR EXEMPLE {n}", amount=Decimal("5.00"), fingerprint=f"treso-cost-{n}"
            )
            TreasuryCheckpoint.objects.create(date=day + timedelta(days=1), balance=Decimal(10 * n))
            TreasuryAdjustment.objects.create(date=day + timedelta(days=1), amount=Decimal("1.00"))

    def test_more_history_costs_no_more_queries(self):
        url = reverse("bank:treasury")
        self.history(0, 3)
        with CaptureQueriesContext(connection) as few:
            self.client.get(url, {"tout": "1"})
        self.history(3, 6)
        with self.assertNumQueries(len(few.captured_queries)):
            response = self.client.get(url, {"tout": "1"})
        # Every gap drawn but the last, waiting for its statement.
        self.assertContains(response, "Ajouter un ajustement de +4.00 €", count=7)
        self.assertContains(response, 'data-label="Solde"', count=9)


class TakingsByMonthTests(TestCase):
    """« Entrées d'argent » adds the till's money up by day before the month:
    the same totals as row by row, unread days apart, refunds included."""

    def test_months_and_the_total_add_up_as_the_rows_do(self):
        products = [PosProduct.objects.create(name=f"Produit exemple {n}") for n in range(3)]
        rows = [
            (date(2026, 5, 31), 0, "12.30", True),
            (date(2026, 5, 31), 1, "0.07", True),
            (date(2026, 5, 31), 2, "-4.50", True),
            (date(2026, 6, 1), 0, "1000.01", True),
            (date(2026, 6, 1), 1, "0.00", True),
            (date(2026, 6, 2), 0, "99.99", False),
            (date(2026, 6, 30), 2, "3.33", True),
        ]
        for day, which, amount, read in rows:
            PosProductDailyQuantity.objects.create(
                product=products[which], sold_on=day, quantity=1, revenue_ttc=Decimal(amount), revenue_read=read
            )
        report = income.income_for(DateRange())
        by_month = {month.first_day: month.takings for month in report.months}
        self.assertEqual(by_month, {date(2026, 5, 1): Decimal("7.87"), date(2026, 6, 1): Decimal("1003.34")})
        self.assertEqual(report.takings, Decimal("1011.21"))
        self.assertEqual(report.unread_revenue_days, [date(2026, 6, 2)])
