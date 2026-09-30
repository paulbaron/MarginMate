"""Does the seller's invoice refund what the slip says was taken back?

Read at page time, never stored: nothing about a slip is written on an
invoice, and no field is added to Invoice or InvoiceLine (a restore that
recreates invoices under new pks, and « Effacer factures », would break a
stored link). It is not an InvoiceParser either: it reads what is already
filed.

`check_many(slips)` answers for many slips in TWO queries whatever their
number (when handed a Board's `index` with the lines loaded): the candidate
invoices - the slips' suppliers', dated within [delivery − 7, delivery + 45]
days, one query over the union of those windows - then their NEGATIVE lines
(`total_ht < 0`). Everything else is done in Python:

- A reference (a delivery-note number) is searched as a whole token in the
  invoice's `source_text`: `(?<![0-9A-Za-z])REF(?![0-9A-Za-z])`, so « 610001 »
  is not found inside « 6100012 ». One with fewer than 4 letters or digits is
  not searched at all (« référence trop courte pour être cherchée »): « 12 »
  is on every invoice.
- Slips and invoices are joined wherever a slip's reference is on an invoice;
  each connected group is compared ONCE: all its slips' lines against all its
  invoices' negative lines. One ticket can list two delivery notes whose
  refund sits on one of the two invoices, and one monthly invoice can refund
  several slips - compared slip by slip, every one of those would read as a
  gap.
- Only the slips that COUNT take part (comparison.effective_slips): a ticket
  and its replacement are not added up.
- Lines are grouped by their folded designation (`search_key`, spaces
  collapsed). Each negative invoice line goes to the LONGEST slip designation
  its own name starts with - the ticket prints the first 20 characters of
  the invoice's designation - and never to two; a key shorter than 3
  characters never pairs. A negative line no slip line claims (a full keg
  returned, a discount) is listed as « autres avoirs de la facture », never
  counted as a difference.
- Counts are compared as |Σ quantity| on each side, amounts as |Σ| to the
  cent: deposits carry no VAT, so HT is what the slip prints.

Every state is said, never folded into a gap: « pas de référence lue »,
« date de livraison non lue », « référence trop courte pour être
cherchée », « facture pas encore reçue » (the slip arrives the day of the
delivery, the invoice later), « facture sans texte lisible » (invoices in
the window but none with text to search), « BL n° X sur plusieurs factures
(…) : à vérifier » (no ✓ and no gap: a duplicate import, a credit note
repeating it), « remboursé sur la facture n° … ✓ », « écart avec la facture
n° … : … » with both figures. A slip with no line gets its ✓ (« rien repris
sur le bon, rien remboursé ») only when the invoice refunds nothing either:
refunding other returnables is « rien repris sur le bon, mais la facture n° …
rembourse d'autres consignes (non comparées) : à vérifier » (OTHERS_ONLY,
amber) - the empty part corrected to kegs by a replacement not received yet,
a keg returned full.

A delivery date at the calendar's ends (a damaged archive) cuts its window
there (`comparison.shifted`) rather than overflowing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q

from common import plain_number, search_key
from invoices.models import Invoice, InvoiceLine
from returnables.comparison import CENT, SlipIndex, euros, shifted
from returnables.reading import line_amount

#: The window an invoice of the slip's supplier is looked for in.
WINDOW_BEFORE = timedelta(days=7)
WINDOW_AFTER = timedelta(days=45)
#: A reference with fewer letters and digits is not searched.
MIN_REFERENCE_ALNUMS = 4
#: A folded designation shorter than this never pairs with an invoice line.
MIN_KEY_CHARS = 3

NO_REFERENCE = "no_reference"
NO_DATE = "no_date"
TOO_SHORT = "too_short"
SUPERSEDED = "superseded"
NO_INVOICE = "no_invoice"
NO_TEXT = "no_text"
SEVERAL = "several"
OTHERS_ONLY = "others_only"
SAME = "same"
DIFFERS = "differs"

#: The status-pill class suffix of each state.
CSS = {
    SAME: "COMPLETE",
    DIFFERS: "ERROR",
    SEVERAL: "pending",
    OTHERS_ONLY: "pending",
    NO_INVOICE: "pending",
    NO_TEXT: "pending",
    NO_REFERENCE: "ignored",
    NO_DATE: "ignored",
    TOO_SHORT: "ignored",
    SUPERSEDED: "ignored",
}

NO_REFERENCE_LABEL = "pas de référence lue"
NO_DATE_LABEL = "date de livraison non lue"
TOO_SHORT_LABEL = "référence trop courte pour être cherchée"
NO_INVOICE_LABEL = "facture pas encore reçue"
NO_TEXT_LABEL = "facture sans texte lisible"

_TOKEN = re.compile(r"[0-9A-Za-z]+")


def key(text: str) -> str:
    """A designation folded for comparison: accents and case dropped
    (common.search_key), spaces collapsed."""
    return " ".join(search_key(text or "").split())


def _alnums(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def searchable(reference: str) -> bool:
    return _alnums(reference) >= MIN_REFERENCE_ALNUMS


def reference_pattern(reference: str):
    """The whole-token search of a reference (escaped: it is captured text)."""
    return re.compile(r"(?<![0-9A-Za-z])" + re.escape(reference) + r"(?![0-9A-Za-z])")


@dataclass(frozen=True)
class InvoiceRef:
    """An invoice as a check names it."""

    pk: int
    number: str
    invoice_date: date | None

    @property
    def label(self) -> str:
        if self.number:
            return f"n° {self.number}"
        return f"du {self.invoice_date:%d/%m/%Y}" if self.invoice_date else "sans numéro"


def _invoices_label(refs) -> str:
    refs = list(refs)
    if len(refs) == 1:
        return f"la facture {refs[0].label}"
    return "les factures " + ", ".join(ref.label for ref in refs)


@dataclass
class CheckRow:
    """One designation: on the slip(s), then refunded on the invoice(s)."""

    designation: str
    key: str
    slip_quantity: int
    invoice_quantity: Decimal
    slip_amount: Decimal | None
    invoice_amount: Decimal

    @property
    def agrees(self) -> bool:
        if self.slip_quantity != self.invoice_quantity:
            return False
        return self.slip_amount is None or self.slip_amount.quantize(CENT) == self.invoice_amount.quantize(CENT)

    @property
    def sentence(self) -> str:
        slip = f"{self.slip_quantity}"
        if self.slip_amount is not None:
            slip += f" ({euros(self.slip_amount)})"
        text = (
            f"{self.designation} — bon : {slip} \N{MIDDLE DOT} "
            f"facture : {plain_number(self.invoice_quantity)} ({euros(self.invoice_amount)})"
        )
        return f"{text} \N{CHECK MARK}" if self.agrees else text


@dataclass
class OtherCredit:
    """A negative invoice line no slip line claims - listed, never a gap."""

    raw_name: str
    quantity: Decimal
    total_ht: Decimal
    invoice: InvoiceRef

    @property
    def sentence(self) -> str:
        return (
            f"{self.raw_name} : {plain_number(self.quantity)} ({euros(self.total_ht)}) — facture {self.invoice.label}"
        )


@dataclass
class InvoiceCheck:
    """What the invoice check says of one slip. `label` is the sentence;
    `ok` True (✓), False (a gap) or None (no verdict: said why)."""

    state: str
    label: str
    invoices: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    others: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    slips: list = field(default_factory=list)

    @property
    def ok(self) -> bool | None:
        return {SAME: True, DIFFERS: False}.get(self.state)

    @property
    def css(self) -> str:
        return CSS[self.state]

    @property
    def differing(self) -> list:
        return [row for row in self.rows if not row.agrees]


class _Invoice:
    """A candidate invoice, with its text cut into whole tokens once."""

    def __init__(self, row: dict):
        self.pk = row["pk"]
        self.supplier_id = row["supplier_id"]
        self.date = row["invoice_date"]
        self.text = row["source_text"] or ""
        self.ref = InvoiceRef(self.pk, row["invoice_number"] or "", self.date)
        self._tokens = None

    @property
    def readable(self) -> bool:
        return bool(self.text.strip())

    def holds(self, reference: str, patterns: dict) -> bool:
        if not self.readable:
            return False
        if _TOKEN.fullmatch(reference):
            # Letters and digits only: the whole-token search is exactly
            # « is it one of the text's maximal runs of letters and digits ».
            if self._tokens is None:
                self._tokens = set(_TOKEN.findall(self.text))
            return reference in self._tokens
        if reference not in patterns:
            patterns[reference] = reference_pattern(reference)
        return patterns[reference].search(self.text) is not None


def _window(delivery: date) -> tuple:
    """[delivery − 7, delivery + 45] days, cut at the calendar's ends."""
    return shifted(delivery, -WINDOW_BEFORE.days), shifted(delivery, WINDOW_AFTER.days)


def _merged(spans) -> list:
    merged = []
    for start, end in sorted(spans):
        if merged and start <= shifted(merged[-1][1], 1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class _Groups:
    """Union-find over slips ("s", pk) and invoices ("i", pk)."""

    def __init__(self):
        self.parent = {}

    def find(self, node):
        self.parent.setdefault(node, node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, one, other):
        self.parent[self.find(one)] = self.find(other)


def _compare(slips, invoices, negative_lines, hits) -> InvoiceCheck:
    """One connected group: its slips' lines against its invoices' negative
    lines."""
    invoice_refs = sorted((invoice.ref for invoice in invoices), key=lambda ref: (ref.invoice_date or date.min, ref.pk))
    slip_pks = {info.pk for info in slips}
    for (pk, reference), found in sorted(hits.items(), key=lambda item: (item[0][0], item[0][1])):
        if pk in slip_pks and len(found) > 1:
            names = ", ".join(invoice.ref.label for invoice in sorted(found, key=lambda one: (one.date, one.pk)))
            return InvoiceCheck(
                SEVERAL,
                f"BL n° {reference} sur plusieurs factures ({names}) : à vérifier",
                invoices=invoice_refs,
                slips=slips,
            )

    groups = {}
    for info in slips:
        for line in info.lines or []:
            folded = key(line.designation)
            group = groups.get(folded)
            if group is None:
                group = groups[folded] = {"designation": line.designation, "quantity": 0, "amount": Decimal("0")}
            group["quantity"] += line.quantity
            amount = line_amount(line)
            if amount is None or group["amount"] is None:
                group["amount"] = None
            else:
                group["amount"] += amount
    keys = sorted((folded for folded in groups if len(folded) >= MIN_KEY_CHARS), key=len, reverse=True)
    refunded = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    others = []
    by_pk = {invoice.pk: invoice for invoice in invoices}
    for invoice_id, raw_name, quantity, total_ht in negative_lines:
        folded = key(raw_name)
        claimed = next((candidate for candidate in keys if folded.startswith(candidate)), None)
        if claimed is None:
            others.append(OtherCredit(raw_name, quantity, total_ht, by_pk[invoice_id].ref))
            continue
        refunded[claimed][0] += quantity
        refunded[claimed][1] += total_ht
    rows = []
    for folded, group in groups.items():
        quantity, amount = refunded.get(folded, (Decimal("0"), Decimal("0")))
        rows.append(
            CheckRow(
                designation=group["designation"],
                key=folded,
                slip_quantity=abs(group["quantity"]),
                invoice_quantity=abs(quantity),
                slip_amount=abs(group["amount"]) if group["amount"] is not None else None,
                invoice_amount=abs(amount),
            )
        )
    where = _invoices_label(invoice_refs)
    if not rows and others:
        # Nothing on the slip, yet the invoice refunds returnables (spec §1: an
        # empty part corrected to kegs by a replacement not received yet, a
        # keg returned full): « rien remboursé » would be false, and other
        # credits are never a gap - no verdict, said.
        return InvoiceCheck(
            OTHERS_ONLY,
            f"rien repris sur le bon, mais {where} rembourse d'autres consignes (non comparées) : à vérifier",
            invoices=invoice_refs,
            rows=rows,
            others=others,
            slips=slips,
        )
    if all(row.agrees for row in rows):
        label = (
            f"remboursé sur {where} \N{CHECK MARK}"
            if rows
            else f"rien repris sur le bon, rien remboursé sur {where} \N{CHECK MARK}"
        )
        return InvoiceCheck(SAME, label, invoices=invoice_refs, rows=rows, others=others, slips=slips)
    differing = " ; ".join(row.sentence for row in rows if not row.agrees)
    return InvoiceCheck(
        DIFFERS, f"écart avec {where} : {differing}", invoices=invoice_refs, rows=rows, others=others, slips=slips
    )


def check_many(slips, *, index: SlipIndex | None = None) -> dict:
    """{slip pk: InvoiceCheck} for `slips` (Slip rows or comparison.SlipInfo).
    Hand it a Board's `index` (its lines loaded) and it costs the two
    invoice queries only; alone, it also reads the slips of their formats
    (one values() query) and the lines it needs (one) - a fixed number
    either way."""
    slips = list(slips)
    if not slips:
        return {}
    format_ids = {slip.format_id for slip in slips}
    if index is None or not format_ids <= index.format_ids:
        index = SlipIndex.load(format_ids | (index.format_ids if index is not None else set()))
    superseded = index.superseded

    results, nodes = {}, {}
    for slip in slips:
        info = index.infos.get(slip.pk)
        if info is None:
            results[slip.pk] = InvoiceCheck(NO_REFERENCE, NO_REFERENCE_LABEL)
            continue
        replaced = superseded.get(info.pk)
        if replaced is not None:
            results[info.pk] = InvoiceCheck(
                SUPERSEDED, f"{replaced.pill} : la facture est vérifiée sur le {replaced.final.label}"
            )
            continue
        if not info.references:
            results[info.pk] = InvoiceCheck(NO_REFERENCE, NO_REFERENCE_LABEL)
            continue
        if info.delivery_date is None:
            results[info.pk] = InvoiceCheck(NO_DATE, NO_DATE_LABEL)
            continue
        wanted = [reference for reference in info.references if searchable(reference)]
        notes = [
            f"référence « {reference} » trop courte pour être cherchée"
            for reference in info.references
            if not searchable(reference)
        ]
        if not wanted:
            results[info.pk] = InvoiceCheck(TOO_SHORT, TOO_SHORT_LABEL, notes=notes)
            continue
        nodes[info.pk] = (info, wanted, notes)
    asked = dict(nodes)
    if not asked:
        return results

    # 1. The candidate invoices: one query over the union of the windows.
    spans = defaultdict(list)
    for info, _wanted, _notes in asked.values():
        spans[info.supplier_id].append(_window(info.delivery_date))
    query = None
    for supplier_id, windows in spans.items():
        for start, end in _merged(windows):
            part = Q(supplier_id=supplier_id, invoice_date__range=(start, end))
            query = part if query is None else query | part
    by_supplier = defaultdict(list)
    for row in (
        Invoice.objects.filter(query)
        .order_by("invoice_date", "pk")
        .values("pk", "invoice_number", "invoice_date", "source_text", "supplier_id")
    ):
        by_supplier[row["supplier_id"]].append(_Invoice(row))

    # The other slips that count and may share those invoices.
    for info in index.effective():
        invoices = by_supplier.get(info.supplier_id)
        if info.pk in nodes or not invoices or info.delivery_date is None:
            continue
        start, end = _window(info.delivery_date)
        if end < invoices[0].date or start > invoices[-1].date:
            continue
        wanted = [reference for reference in info.references if searchable(reference)]
        if wanted:
            nodes[info.pk] = (info, wanted, [])

    # 2. Who is on which invoice.
    groups, hits, candidates, compiled = _Groups(), {}, {}, {}
    for pk, (info, wanted, _notes) in nodes.items():
        start, end = _window(info.delivery_date)
        found_in = [invoice for invoice in by_supplier.get(info.supplier_id, []) if start <= invoice.date <= end]
        candidates[pk] = found_in
        for reference in wanted:
            found = [invoice for invoice in found_in if invoice.holds(reference, compiled)]
            if found:
                hits[(pk, reference)] = found
                for invoice in found:
                    groups.union(("s", pk), ("i", invoice.pk))
    linked = {pk for pk, _reference in hits}

    members = defaultdict(lambda: {"slips": [], "invoices": {}})
    for pk in linked:
        members[groups.find(("s", pk))]["slips"].append(nodes[pk][0])
    for found in hits.values():
        for invoice in found:
            members[groups.find(("i", invoice.pk))]["invoices"][invoice.pk] = invoice
    wanted_roots = {groups.find(("s", pk)) for pk in asked if pk in linked}

    # 3. Their negative lines (one query), and the slips' lines.
    invoice_ids = [pk for root in wanted_roots for pk in members[root]["invoices"]]
    negative = defaultdict(list)
    if invoice_ids:
        for invoice_id, raw_name, quantity, total_ht in (
            InvoiceLine.objects.filter(invoice_id__in=invoice_ids, total_ht__lt=0)
            .order_by("invoice_id", "pk")
            .values_list("invoice_id", "raw_name", "quantity", "total_ht")
        ):
            negative[invoice_id].append((invoice_id, raw_name, quantity, total_ht))
        index.ensure_lines(info.pk for root in wanted_roots for info in members[root]["slips"])

    compared = {}
    for pk, (info, _wanted, notes) in asked.items():
        if pk not in linked:
            found_in = candidates[pk]
            unreadable = [invoice for invoice in found_in if not invoice.readable]
            extra = []
            if unreadable:
                count = len(unreadable)
                extra.append(
                    f"{count} facture{'s' if count > 1 else ''} de la période sans texte lisible : "
                    "le BL n'a pas pu y être cherché"
                )
            if found_in and len(unreadable) == len(found_in):
                results[pk] = InvoiceCheck(
                    NO_TEXT, NO_TEXT_LABEL, invoices=[invoice.ref for invoice in found_in], notes=notes + extra
                )
            else:
                results[pk] = InvoiceCheck(NO_INVOICE, NO_INVOICE_LABEL, notes=notes + extra)
            continue
        root = groups.find(("s", pk))
        if root not in compared:
            member = members[root]
            component_slips = sorted(member["slips"], key=lambda one: one.key)
            invoices = sorted(member["invoices"].values(), key=lambda one: (one.date, one.pk))
            lines = [line for invoice in invoices for line in negative[invoice.pk]]
            compared[root] = _compare(component_slips, invoices, lines, hits)
        results[pk] = replace(compared[root], notes=compared[root].notes + notes)
    return results
