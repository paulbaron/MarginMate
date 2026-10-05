"""What the bank has paid of each « facture de vente »: the links read once,
the allocation, and the state a page says (spec §6.0, §5.5).

A link (`SaleDocumentPayment`) says « this credit pays this document » -
never how much. One transfer may pay two invoices, a cheque deposit of
1 200 € may hold one customer's 300 € cheque, a deposit and its balance are
two credits on one invoice. Summing each linked credit's WHOLE amount read
« 1 200 € reçus » on a 300 € invoice, and gave a document part-paid by a
shared credit a wrong remaining due - which the matcher then read. So one
pure allocator (`allocate`) answers it, and every reader goes through it:
the « Ventes » tab's pill, the document's « Règlement », Banque's gap and
suggestions (bank/sale_reconcile.py), the payer history and « Entrées
d'argent » (bank/income.py).

**Oldest first**: the credits by their day, each spread over ITS documents
by theirs, each taking at most what it still asks - the Code civil's
default when the payer says nothing (art. 1342-10: the oldest debt, all else
equal). What is left of a credit goes to no document, and Banque says so.

Read through `read_links` - two queries, whatever the links (no bank import
here: the bank's columns are read across the link) - never a property
reading `self.lines.all()` per document.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Subquery

from common import format_money

from .models import SaleDocument, SaleDocumentLine, SaleDocumentPayment, document_to_pay

ZERO = Decimal("0")

#: The pill of a document's payment state - the tab's « Règlement » column
#: and the document's « Règlement ».
PILL_PAID = "Réglée"
PILL_PART_PAID = "Réglée en partie"
PILL_UNPAID = "Non réglée"
PILL_CREDIT_NOTE = "Avoir"
PILL_NOTHING_DUE = "—"
#: Its sentence (spec §5.5): the amounts the allocation gives THIS document,
#: never a credit's whole amount.
CREDIT_NOTE_SAID = "Avoir : rien à recevoir."
NOTHING_DUE_SAID = "Rien à recevoir."
UNPAID_SAID = "Non réglée : {due} € à recevoir."
UNPAID_ONE_LINKED_SAID = "Non réglée : {due} € à recevoir - l'entrée rattachée règle d'abord d'autres factures."
UNPAID_LINKED_SAID = "Non réglée : {due} € à recevoir - les entrées rattachées règlent d'abord d'autres factures."
PART_PAID_SAID = "Réglée en partie : {paid} € reçus sur {to_pay} €."
PAID_SAID = "Réglée : {paid} € reçus."
#: Said after the sentence, before its full stop, when the invoice deducts
#: a deposit from its total.
PREPAID_SAID = " (acompte de {prepaid} € déjà déduit par la facture)."


@dataclass(frozen=True)
class LinkFact:
    """One link, with what every reader needs of its two ends."""

    payment_pk: int
    method: str
    credit_pk: int
    credit_day: date
    #: Positive: a credit. (A debit is never linked - bank.sale_reconcile
    #: refuses it - and one put there by hand gives nothing.)
    credit_amount: Decimal
    counterparty: str
    label: str
    bank_type: str
    #: The line's own « En caisse » choice, "" for automatic.
    income_source: str
    settled_by_hand: bool
    document_pk: int
    reference: str
    customer: str
    sold_on: date
    counting: str
    #: What the document asks of the bank (`document_to_pay`).
    to_pay: Decimal

    @property
    def number(self) -> str:
        """« n° FV-12 », « sans numéro »: how a document is named in a line."""
        return f"n° {self.reference}" if self.reference else "sans numéro"

    @property
    def document_label(self) -> str:
        """« n° FV-12 · Exemple SARL · 05/03/2026 » - SaleDocument.label."""
        return " · ".join([self.number, *([self.customer] if self.customer else []), f"{self.sold_on:%d/%m/%Y}"])

    @property
    def counts_off_till(self) -> bool:
        """Its document is a sale the till never rang (« Compte dans les
        marges et le stock », « Acompte »): its credit reads « Facture de
        vente » on « Entrées d'argent ». A « Déjà comptée par la caisse »
        invoice's credit is money the till took."""
        return self.counting != SaleDocument.Counting.TILL

    @property
    def method_label(self) -> str:
        """« Automatique » / « À la main »."""
        return str(dict(SaleDocumentPayment.Method.choices).get(self.method, self.method))


@dataclass(frozen=True)
class Allocation:
    """Every link, and what each credit gives each of its documents."""

    facts: tuple[LinkFact, ...] = ()
    #: (credit pk, document pk) → what that credit gives that document.
    shares: dict[tuple[int, int], Decimal] = field(default_factory=dict)
    #: document pk → Σ its shares, for every linked document (0 included).
    paid: dict[int, Decimal] = field(default_factory=dict)
    #: credit pk → its amount less Σ its shares (≥ 0): what goes to none.
    left: dict[int, Decimal] = field(default_factory=dict)
    by_document: dict[int, list[LinkFact]] = field(default_factory=dict)
    by_credit: dict[int, list[LinkFact]] = field(default_factory=dict)

    def share(self, credit_pk: int, document_pk: int) -> Decimal:
        return self.shares.get((credit_pk, document_pk), ZERO)

    def due(self, document_pk: int, to_pay: Decimal) -> Decimal:
        """What `document_pk` still asks: `to_pay` less what it was given,
        never below 0 (a credit note asks nothing)."""
        return max(to_pay - self.paid.get(document_pk, ZERO), ZERO)

    def of_document(self, pk: int) -> list[LinkFact]:
        """Its links, credits in the allocation's order."""
        return list(self.by_document.get(pk, ()))

    def of_credit(self, pk: int) -> list[LinkFact]:
        """Its links, documents in the allocation's order."""
        return list(self.by_credit.get(pk, ()))

    def linked(self, document_pk: int) -> bool:
        return document_pk in self.by_document


def _credit_order(fact: LinkFact) -> tuple:
    return fact.credit_day, fact.credit_pk


def _document_order(fact: LinkFact) -> tuple:
    return fact.sold_on, fact.document_pk


def allocate(facts) -> Allocation:
    """Credits in (day, pk) order; each spread over ITS documents in
    (sold_on, pk) order, each taking at most what it still asks - `to_pay`
    less what earlier credits gave it; nothing for a document asking nothing,
    a credit note -; what is left of a credit goes to no document."""
    facts = tuple(facts)
    by_credit: dict[int, list[LinkFact]] = defaultdict(list)
    by_document: dict[int, list[LinkFact]] = defaultdict(list)
    for fact in sorted(facts, key=lambda one: (_credit_order(one), _document_order(one))):
        by_credit[fact.credit_pk].append(fact)
        by_document[fact.document_pk].append(fact)
    shares: dict[tuple[int, int], Decimal] = {}
    paid: dict[int, Decimal] = defaultdict(lambda: ZERO)
    left: dict[int, Decimal] = {}
    for credit_pk, links in by_credit.items():
        remaining = max(links[0].credit_amount, ZERO)
        for fact in links:
            share = min(max(fact.to_pay - paid[fact.document_pk], ZERO), remaining)
            shares[(credit_pk, fact.document_pk)] = share
            paid[fact.document_pk] += share
            remaining -= share
        left[credit_pk] = remaining
    return Allocation(facts, shares, dict(paid), left, dict(by_document), dict(by_credit))


#: What `read_links` reads of a linked document's lines: what
#: `SaleDocumentLine.total_ttc` reads, and nothing more.
TOTAL_FIELDS = ("document", "total_ht", "vat_rate", "unit_price_ttc", "quantity", "recipe", "recipe__selling_price_ttc")


def read_links() -> Allocation:
    """Every link, allocated - two queries, always (both plain queries, never
    a prefetch Django would skip on an empty list): the links with their two
    ends' columns, read across the link (no bank import here), and the lines
    of every linked document, for what each asks. A bar issues tens of sales
    invoices a year: read whole."""
    rows = SaleDocumentPayment.objects.order_by("pk").values_list(
        "pk",
        "method",
        "transaction_id",
        "transaction__operation_date",
        "transaction__amount",
        "transaction__counterparty",
        "transaction__label",
        "transaction__bank_type",
        "transaction__income_source",
        "transaction__settled_by_hand",
        "document_id",
        "document__reference",
        "document__customer",
        "document__sold_on",
        "document__counting",
        "document__stated_total_ttc",
        "document__payable_ttc",
        "document__prepaid_ttc",
        "document__adjustment_ht",
        "document__adjustment_vat_rate",
    )
    rows = list(rows)
    lines: dict[int, list[SaleDocumentLine]] = defaultdict(list)
    linked = SaleDocumentLine.objects.filter(
        document_id__in=Subquery(SaleDocumentPayment.objects.values("document_id"))
    ).select_related("recipe")
    for line in linked.only(*TOTAL_FIELDS).order_by():
        lines[line.document_id].append(line)
    facts = []
    for row in rows:
        (pk, method, credit, day, amount, counterparty, label, bank_type, source, by_hand) = row[:10]
        (document, reference, customer, sold_on, counting, stated, payable, prepaid, adjustment, rate) = row[10:]
        facts.append(
            LinkFact(
                payment_pk=pk,
                method=method,
                credit_pk=credit,
                credit_day=day,
                credit_amount=amount,
                counterparty=counterparty,
                label=label,
                bank_type=bank_type,
                income_source=source,
                settled_by_hand=by_hand,
                document_pk=document,
                reference=reference,
                customer=customer,
                sold_on=sold_on,
                counting=counting,
                to_pay=document_to_pay(
                    stated, payable, prepaid, lines[document], adjustment_ht=adjustment, adjustment_vat_rate=rate
                ),
            )
        )
    return allocate(facts)


@dataclass(frozen=True)
class PaymentState:
    """What a page says of one document's payment."""

    pill: str
    #: Its pill's class (« status-linked » …), "" for no pill (« — »).
    css: str
    sentence: str
    #: What the allocation gives it, and what it asks.
    paid: Decimal
    to_pay: Decimal

    @property
    def received(self) -> bool:
        """Something was received for it: « X € reçus » under the pill."""
        return self.paid > 0


def _euros(value: Decimal) -> str:
    return format_money(value)


def payment_state(document: SaleDocument, allocation: Allocation, lines) -> PaymentState:
    """`document`'s state (spec §5.5), its money read off `lines` - a list
    the caller has (a prefetched `line_list`, the page's lines): never a
    query here. Its received amount is what the allocation gives IT."""
    to_pay = document.to_pay_of(lines)
    paid = allocation.paid.get(document.pk, ZERO) if document.pk is not None else ZERO
    if to_pay < 0:
        pill, css, said = PILL_CREDIT_NOTE, "status-ignored", CREDIT_NOTE_SAID
    elif to_pay == 0:
        pill, css, said = PILL_NOTHING_DUE, "", NOTHING_DUE_SAID
    elif paid >= to_pay:
        pill, css, said = PILL_PAID, "status-linked", PAID_SAID.format(paid=_euros(paid))
    elif paid > 0:
        pill, css = PILL_PART_PAID, "status-NEEDS_REVIEW"
        said = PART_PAID_SAID.format(paid=_euros(paid), to_pay=_euros(to_pay))
    else:
        pill, css = PILL_UNPAID, "status-ignored"
        credits = len(allocation.of_document(document.pk)) if document.pk is not None else 0
        sentence = UNPAID_SAID if not credits else UNPAID_ONE_LINKED_SAID if credits == 1 else UNPAID_LINKED_SAID
        said = sentence.format(due=_euros(to_pay - paid))
    if document.prepaid_ttc is not None and document.prepaid_ttc > 0:
        said = said.removesuffix(".") + PREPAID_SAID.format(prepaid=_euros(document.prepaid_ttc))
    return PaymentState(pill, css, said, paid, to_pay)


@dataclass(frozen=True)
class LinkedCredit:
    """A credit paying the document, as its « Règlement » lists it: what it
    gives this document, and the other documents it pays."""

    fact: LinkFact
    share: Decimal
    others: tuple[LinkFact, ...] = ()


def payment_context(document: SaleDocument, allocation: Allocation, access, lines) -> dict:
    """The « Règlement » section of `document`'s page: its state for anyone
    who opens the page; with « Banque » (`access.allows("bank")` - the owner
    always) the credits paying it, each with its share. What could pay it
    and the credit search are the bank's (bank/sale_reconcile.py), added by
    the view - recipes reaches bank only from a function."""
    sees_bank = access.allows("bank")
    links = []
    if sees_bank:
        links = [
            LinkedCredit(
                fact,
                allocation.share(fact.credit_pk, document.pk),
                tuple(other for other in allocation.of_credit(fact.credit_pk) if other.document_pk != document.pk),
            )
            for fact in allocation.of_document(document.pk)
        ]
    return {"state": payment_state(document, allocation, lines), "sees_bank": sees_bank, "links": links}
