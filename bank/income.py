"""Everything that CAME IN on the account over a window, beside what the till
says it was paid over the same days.

**This is « Dépenses » the other way round, and it is not « Marges ».**
Dépenses counts what the bank took; this counts what the bank received, by
the date it received it. Marges counts what the till SOLD against what was
invoiced; this sets what the till was PAID - card, cash, cheque, as its
tickets record them (`recipes.PosDailyPayment`) - against what reached the
account. The owner's question (27/09): does the money that came in match
what was sold?

What the statement shows, and how each line is recognised - by the till
rules a person edits (`bank.recognition`, « Reconnaissance des opérations »),
said on the page, because what the application recognises has to be visible:

* **A card payout**: a credit a « Versement de carte (TPE) » rule finds. The
  terminal's provider pays the card takings in one transfer, and the line's
  amount is what arrived - net of its commission. Where the rule reads the
  GROSS printed in the label (`(?P<encaisse>…)`), the commission is
  gross − net, said as it is, even when it comes out odd (a net above the
  gross is a negative commission, printed so, never corrected). Recognised
  by words, never by the provider's name: a name is the one thing a new
  contract changes.
* **Cash and cheques deposited**, meal vouchers, an « Avoir », no sale at
  all: whatever a rule of that meaning finds (the owner's bank says its
  deposits in the operation type).
* **Everything else** is « Autres entrées »: a private party paying an
  event, a partner's contribution, a supplier's refund, a direct debit
  returned. A person names them with the SAME free-text
  `BankTransaction.category` « Dépenses » uses - blank is « Sans catégorie »,
  listed first - and the datalist offers the words already typed on credits
  only, never the spending ones.

**A person can say what a credit is in the till** (« En caisse », on every
credit of the page): card, cash, cheque, « Avoir », meal vouchers, or no sale
at all (`models.IncomeSource`), for a credit no rule recognises - or one a
rule reads wrong. A choice is kept two ways (`reading_of`, in this order):

* on the LINE (`BankTransaction.income_source`), for that line alone - it
  beats everything, the rules included;
* then the rules, where one RECOGNISES the line - « Pas une vente » included:
  what the line itself prints is data about it;
* then its PAYER (`IncomePayer`, keyed by `payer_key`), « retenir pour ce
  payeur »: every credit of that payer the rules do not recognise, past and
  future, follows - the one click that teaches the page a new terminal. Read
  when the page is drawn, never written onto the lines, so « Oublier » puts
  them back. A payer never un-recognises a line: the provider prints the
  bar's own name as the payee of its payouts, and « Pas une vente » retained
  for a transfer from the bar's other account under that name moved every
  payout of the statement out of the card figures (review, 01/10/2026).

**A credit paying a « facture de vente »** (recipes.SaleDocumentPayment) that
counts off the till reads « Facture de vente » (`SALE`, `BY_SALE`) - derived
from the links (recipes.sale_payments.read_links), never stored, no « En
caisse » choice and no payer's. It comes after a payer retained for a source
the till compares (card, cash, cheques, vouchers, « Avoir »: a terminal
recognised by its payer « Carte » and linked to an invoice is still a payout
- the harm « a payer never un-recognises a line » was written against,
reached by a link) and before a payer retained « Pas une vente ». A
« Déjà comptée par la caisse » invoice's credit is no sale: that money is
the till's own takings, and reads as the till took it.

A card credit whose label prints no gross - a payout rule reading none, or
a card said by a person where no rule reads a gross on the line - counts the
amount received as its gross (a bank's own terminal pays the gross and takes
its fee apart): its commission is UNKNOWN, never 0 - left out of the
commission and its rate, and the page says how many payouts are counted that
way.

**How a payout is linked to the till's card sales.** Measured on the real
statement (the measurement stays out of a public repository; the rules do
not): a payout lands one to three days after the sale, Monday pays Friday and
Saturday, and over a year the till's card payments and the payouts' gross
agree closely - but a single payout often equals no run of consecutive till
days exactly, because the provider's batches do not follow the till's
service days and a payment after midnight moves. So
the link is a RUNNING BALANCE (`Balance`), « ventes carte pas encore
versées », computed over the WHOLE history so a window cannot change it: it
sits near a steady level (the last few days not yet paid) and a card sale
never paid - a ticket settled in reality by transfer, a payout missing -
shows as a permanent step. An exact run (`exact_runs`) is an annotation,
« même montant au centime », never a claim that a payout paid given days
when the amounts do not say so.

Pure of request and template: a `DateRange` in, an `IncomeReport` out, in
`QUERIES` queries whatever the history holds.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from django.db import transaction
from django.db.models import Max, Min

from common import DateRange, search_key
from recipes.integration import TILL_REIMPORT
from recipes.models import PosDailyPayment, PosProductDailyQuantity
from recipes.sale_payments import Allocation, read_links

from . import matching, recognition
from .models import BankTransaction, IncomePayer, IncomeSource
from .spending import NO_CATEGORY

ZERO = Decimal("0")
HUNDRED = Decimal("100")
RATE_PLACES = Decimal("0.01")

#: Where a credit came from - one vocabulary for the report, the page,
#: « Banque »'s « Entrées » tab and what a person chooses
#: (`models.IncomeSource`, whose values these are).
CARD = IncomeSource.CARD.value
CASH = IncomeSource.CASH.value
CHEQUE = IncomeSource.CHEQUE.value
CREDIT = IncomeSource.CREDIT.value
VOUCHER = IncomeSource.VOUCHER.value
OTHER = IncomeSource.OTHER.value
AUTOMATIC = IncomeSource.AUTOMATIC.value
#: A credit paying a sales invoice that counts off the till: « Facture de
#: vente ». Read from the links (recipes.sale_payments.read_links), never
#: stored: not an IncomeSource member - it is no « En caisse » choice,
#: nothing to offer in the menu or to retain for a payer.
SALE = "sale"
#: Every value a person may choose for a line - AUTOMATIC included, which
#: hands the line back to its payer and the rules.
VALUES = frozenset(value for value, _label in IncomeSource.choices)
#: Every value that IS a source: what a line or a payer can hold.
CHOSEN = VALUES - {AUTOMATIC}
SOURCES = {
    CARD: "Versements carte",
    CASH: "Espèces déposées",
    CHEQUE: "Chèques remis",
    CREDIT: "Acomptes (avoirs)",
    VOUCHER: "Titres-restaurant remboursés",
    SALE: "Factures de vente",
    OTHER: "Autres entrées",
}
#: What one line of each is called on « Banque ».
ONE = {
    CARD: "Versement carte",
    CASH: "Dépôt d'espèces",
    CHEQUE: "Remise de chèques",
    CREDIT: "Acompte (avoir en caisse)",
    VOUCHER: "Remboursement de titres-restaurant",
    SALE: "Facture de vente",
}
#: The till's methods the account can see, and the source each is compared
#: with, in the till's own order (`PosDailyPayment.ORDER`).
BANK_SIDE = {
    PosDailyPayment.CARD: CARD,
    PosDailyPayment.CASH: CASH,
    PosDailyPayment.CHEQUE: CHEQUE,
    PosDailyPayment.MEAL_VOUCHER: VOUCHER,
    PosDailyPayment.CREDIT: CREDIT,
}

#: How a credit's source was decided - said on the page, since a
#: recognition nobody can see is one nobody can correct. BY_SALE: the sales
#: invoice it pays.
BY_LINE, BY_PAYER, BY_RULE, BY_SALE = "line", "payer", "rule", "sale"
#: `IncomePayer.key`'s width, asked of the model rather than written twice.
PAYER_KEY_MAX = IncomePayer._meta.get_field("key").max_length

#: A payout pays sales of the days BEFORE it: an exact run is looked for in
#: [T − RUN_DAYS, T − 1]. Eight, so Monday's payout still reaches back past
#: a long weekend - and never further, where some run of days always adds up
#: to anything.
RUN_DAYS = 8
#: Where the balance's starting day is looked for, before the first payout
#: of the statement: « Monday pays Friday and Saturday », and a week covers
#: the longest gap measured.
ANCHOR_DAYS = 7

#: What `income_for` costs, whatever the history holds: the payers retained,
#: the recognition rules, the credits, the sale links and the lines of the
#: documents they pay (recipes.sale_payments.read_links), the till's
#: payments, the till's days of the window, the till's last day, and the
#: statement's first and last day (one aggregate). A test holds it - an N+1
#: here is one query per day of the year.
QUERIES = 9

#: The sources a till row is compared with - « Autres entrées » is compared
#: with nothing, so nothing of it can be « before the till ».
COMPARED = (CARD, CASH, CHEQUE, VOUCHER, CREDIT)


def automatic_source(line: BankTransaction, rules: recognition.Rules | None) -> recognition.TillReading | None:
    """What the first till rule finding the credit says it is - None where
    none does, and where no rules are given (`recognition.load`, read once
    by the caller: never a query here)."""
    if rules is None:
        return None
    return recognition.till_reading(rules, line.label or "", line.bank_type or "")


def payer_key(line: BankTransaction) -> str:
    """Who paid a credit, as `IncomePayer.key` holds it: `matching.alias_key`
    of the payee the bank prints, else of the label's words without their
    digits (`matching.payee_of`: « … N° 000123 DU 05/06/26 » is the same key
    next month). Cut to the column, the same way wherever it is asked. Empty
    where nothing names anybody - and an empty key is never retained."""
    return payer_key_of(line.counterparty, line.label)


def payer_key_of(counterparty: str, label: str) -> str:
    """`payer_key` of a credit known by its two columns - a link's, read
    across it (recipes.sale_payments.LinkFact)."""
    # Stripped first: a counterparty of spaces alone (an archive's, a hand
    # edit) is no payee, and taken for one it named nobody.
    key = matching.alias_key(matching.payee_of((counterparty or "").strip(), label or ""))
    return key[:PAYER_KEY_MAX].rstrip()


class Reading(NamedTuple):
    """What a credit is, who said so, and - where a till rule said so - what
    that rule read (its name, a payout's gross)."""

    source: str
    how: str
    till: recognition.TillReading | None = None


def reading_of(
    line: BankTransaction,
    payers: dict[str, str] | None = None,
    payer: str | None = None,
    rules: recognition.Rules | None = None,
    sold: bool = False,
) -> Reading:
    """What a credit is, and who said so - the line's own choice, else the
    first till rule that recognises it (`rules`; « Pas une vente » is a
    recognition too), else a payer retained for a source the till compares
    (`payers`: {payer key: source}), else « Facture de vente » when it pays
    a sales invoice that counts off the till (`sold`), else a payer retained
    « Pas une vente », else « Autres entrées ». A stored value that is no
    source (written by hand in the database) is passed over, never raised
    on. `payer` is the line's key when the caller has it already."""
    if line.income_source in CHOSEN:
        return Reading(line.income_source, BY_LINE)
    recognised = automatic_source(line, rules)
    if recognised is not None:
        return Reading(recognised.source, BY_RULE, recognised)
    learnt = payers.get(payer_key(line) if payer is None else payer) if payers else None
    if learnt in COMPARED:
        return Reading(learnt, BY_PAYER)
    if sold:
        return Reading(SALE, BY_SALE)
    if learnt in CHOSEN:
        return Reading(learnt, BY_PAYER)
    return Reading(OTHER, BY_RULE)


def follows_its_payer(
    line: BankTransaction, rules: recognition.Rules | None = None, *, sold: bool = False, holds: str | None = None
) -> bool:
    """Whether a retained payer decides this credit: nothing chosen on the
    line, no till rule of `rules` recognising it (`reading_of`) - and, for a
    credit paying a sales invoice that counts off the till (`sold`), a payer
    holding (`holds`) a source the till compares: one retained « Pas une
    vente » never reaches it, the sale comes first."""
    if line.income_source in CHOSEN or automatic_source(line, rules) is not None:
        return False
    return holds in COMPARED or not sold


def source_of(
    line: BankTransaction, payers: dict[str, str] | None = None, rules: recognition.Rules | None = None
) -> str:
    """CARD, CASH, CHEQUE, VOUCHER, CREDIT or OTHER (`reading_of`)."""
    return reading_of(line, payers, rules=rules).source


class SaleRef(NamedTuple):
    """A sales invoice a credit pays, as the pages name it."""

    pk: int
    #: « n° FV-12 », « sans numéro ».
    label: str
    #: Its document counts off the till: the credit may read « Facture de
    #: vente » (`SALE`). A « Déjà comptée par la caisse » one does not.
    counts_off_till: bool
    #: What the credit gives it (recipes.sale_payments.allocate).
    share: Decimal
    #: Its page - set by the view that draws it, "" otherwise.
    url: str = ""


def sale_refs(allocation: Allocation, credit_pk: int) -> tuple[SaleRef, ...]:
    """The sales invoices `credit_pk` pays, oldest first - pure, from the
    links read once (recipes.sale_payments.read_links)."""
    return tuple(
        SaleRef(fact.document_pk, fact.number, fact.counts_off_till, allocation.share(credit_pk, fact.document_pk))
        for fact in allocation.of_credit(credit_pk)
    )


def is_sold(sales) -> bool:
    """Whether a credit paying `sales` reads « Facture de vente »: it pays at
    least one invoice that counts off the till."""
    return any(ref.counts_off_till for ref in sales)


@dataclass(frozen=True)
class Entry:
    """One credit, as this page reads it: where it came from, who said so,
    and for a payout what the provider says it collected."""

    line: BankTransaction
    source: str
    #: A payout's gross: what its label prints - else, a terminal printing
    #: none, the amount received (`gross_from_amount`). None on anything
    #: but a payout.
    gross: Decimal | None = None
    #: The label printed no gross: `gross` is the amount received, and the
    #: commission is unknown - never 0, which would read as « no fee ».
    gross_from_amount: bool = False
    #: BY_LINE, BY_PAYER or BY_RULE.
    how: str = BY_RULE
    #: `payer_key` of the line, worked out once.
    payer: str = ""
    #: The name of the till rule that recognised it (BY_RULE), « » for none.
    rule: str = ""
    #: The sales invoices it pays, whatever it reads as (`sale_refs`).
    sales: tuple[SaleRef, ...] = ()

    @property
    def day(self) -> date:
        return self.line.operation_date

    @property
    def net(self) -> Decimal:
        """What arrived on the account."""
        return self.line.amount

    @property
    def commission(self) -> Decimal | None:
        """Gross − net, as it comes: odd (negative) is said, not mended.
        None where the label printed no gross: nothing says what was kept."""
        if self.gross is None or self.gross_from_amount:
            return None
        return self.gross - self.net

    @property
    def commission_rate(self) -> Decimal | None:
        return rate(self.commission, self.gross)

    @property
    def choice(self) -> str:
        """What the page's « En caisse » menu shows chosen: the line's own
        choice, its payer's, else « Automatique » - a sale included, which no
        menu offers."""
        return AUTOMATIC if self.how in (BY_RULE, BY_SALE) else self.source

    @property
    def how_label(self) -> str:
        """Who said what this credit is, in the page's words."""
        if self.how == BY_LINE:
            return "choisi pour cette entrée"
        if self.how == BY_PAYER:
            return "payeur retenu"
        if self.how == BY_SALE:
            sold = [ref for ref in self.sales if ref.counts_off_till]
            return f"facture de vente {sold[0].label}" if len(sold) == 1 else f"{len(sold)} factures de vente"
        if self.rule:
            return f"règle « {self.rule} »"
        return "non reconnue"

    @property
    def remember_by_default(self) -> bool:
        """« retenir pour ce payeur » drawn ticked: on a credit its payer
        decides, and on one nothing recognised - the transfer of a terminal
        the rules do not know, where retaining is the point. Unticked where
        the rules or the line itself decided: changing one of those is an
        exception, and its payer's other credits are no business of it."""
        if not self.payer or self.how == BY_SALE:
            return False
        return self.how == BY_PAYER or (self.how == BY_RULE and not self.rule)

    @property
    def category(self) -> str:
        """What a person typed on it, « Sans catégorie » for nothing - read
        for « Autres entrées » only: a payout or a deposit is named by the
        rules above, whatever was typed on it."""
        return self.line.category.strip() or NO_CATEGORY

    @property
    def unnamed(self) -> bool:
        """« Autres entrées » nobody named - the work. Never a credit a person
        linked to a sales invoice: it says what it is."""
        return self.source == OTHER and not self.line.category.strip() and not self.sales

    @property
    def name(self) -> str:
        """What « Banque » calls it on its « Entrées » tab."""
        return ONE.get(self.source) or self.category


def entry_for(
    line: BankTransaction,
    payers: dict[str, str] | None = None,
    rules: recognition.Rules | None = None,
    sales: tuple[SaleRef, ...] = (),
) -> Entry:
    """One credit read whole. `payers` ({payer key: source}, `known_payers`),
    `rules` (`recognition.load`) and `sales` (`sale_refs`, the sales invoices
    it pays) are read once by the caller: pure, no query, whatever the
    number of lines. A payout's gross is what the rule that recognised it
    reads; a card said by the line or its payer takes the gross the first
    payout rule reads on it (`recognition.printed_gross`) - and with none,
    the amount received, its commission unknown."""
    payer = payer_key(line)
    sales = tuple(sales)
    source, how, till = reading_of(line, payers, payer, rules, is_sold(sales))
    rule = till.rule if till is not None else ""
    if source != CARD:
        return Entry(line, source, how=how, payer=payer, rule=rule, sales=sales)
    if till is not None:
        gross = till.gross
    elif rules is not None:
        gross = recognition.printed_gross(rules, line.label or "", line.bank_type or "")
    else:
        gross = None
    if gross is None:
        return Entry(line, source, line.amount, gross_from_amount=True, how=how, payer=payer, rule=rule, sales=sales)
    return Entry(line, source, gross, how=how, payer=payer, rule=rule, sales=sales)


def known_payers() -> dict[str, str]:
    """{payer key: source} of every payer retained - one query, for a page
    reading many credits."""
    return dict(IncomePayer.objects.values_list("key", "source"))


class SourceRefused(ValueError):
    """A choice « En caisse » cannot record, with the sentence to say."""


#: The refusals, said as they are.
NOT_A_CREDIT = "Seule une entrée d'argent se compte en caisse."
UNKNOWN_CHOICE = "Choix inconnu : rien n'a changé."


@dataclass
class SourceChange:
    """What `set_source` did, for the message saying so."""

    entry: Entry
    #: The payer's key: « » where nothing names one.
    payer: str = ""
    #: The payer now holds the choice / no longer holds any.
    remembered: bool = False
    forgotten: bool = False
    #: The payer's OTHER credits that follow it now, and those whose own
    #: choice beats it - counted only when the payer changed. Those a rule
    #: recognises are neither: a payer never reaches them.
    followers: int = 0
    kept: int = 0


def set_source(line: BankTransaction, value, *, remember: bool) -> SourceChange:
    """Say what a credit is in the till.

    `remember` (« retenir pour ce payeur »): the payer holds the choice -
    AUTOMATIC forgets it - and the line follows its payer like every other
    credit of it, so one decision answers for all of them and « Oublier »
    undoes it whole. A line the rules recognise is one no payer reaches
    (`reading_of`): it keeps the choice as its own, or a payer « Carte »
    retained from a payout reading something else would leave that payout
    as it was. So does a line paying a sales invoice (`sold`) given a value
    the till does not compare: the sale comes before such a payer.
    Otherwise the choice is the line's alone, beating everything; AUTOMATIC
    hands the line back to the rules, its payer and its invoice.

    Settles nothing in `reconcile`'s sense: a credit pays no invoice, and
    `settled_by_hand` is left alone. Refused (`SourceRefused`) on a line that
    is not a credit, or a value the menu does not offer - the menu posts
    exact values, so nothing is trimmed or guessed.
    """
    if line.amount <= 0:
        raise SourceRefused(NOT_A_CREDIT)
    if not isinstance(value, str) or value not in VALUES:
        raise SourceRefused(UNKNOWN_CHOICE)
    # The rules and the sale links, read once - for this line and every
    # other credit of its payer below.
    rules = recognition.load()
    links = read_links()
    key = payer_key(line)
    recognised = automatic_source(line, rules)
    sold = is_sold(sale_refs(links, line.pk))
    remembered = forgotten = False
    held = None
    with transaction.atomic():
        if remember and key:
            if value == AUTOMATIC:
                held = IncomePayer.objects.filter(key=key).values_list("source", flat=True).first()
                forgotten = IncomePayer.objects.filter(key=key).delete()[0] > 0
            else:
                IncomePayer.objects.update_or_create(key=key, defaults={"source": value})
                remembered = True
            # The line follows its payer - unless a rule recognises it as
            # something else, or it pays a sales invoice that a value the
            # till does not compare would not reach: only its own choice can
            # say otherwise there.
            reached = (
                value == AUTOMATIC
                or (recognised is None and (not sold or value in COMPARED))
                or (recognised is not None and value == recognised.source)
            )
            line.income_source = AUTOMATIC if reached else value
        else:
            line.income_source = value
        line.save(update_fields=["income_source"])
    change = SourceChange(entry_for(line, known_payers(), rules, sale_refs(links, line.pk)), key, remembered, forgotten)
    if remembered or forgotten:
        # What the payer holds now, or held: a source the till compares
        # reaches a credit paying a sales invoice, any other does not.
        holds = value if remembered else held
        others = BankTransaction.objects.filter(amount__gt=0).exclude(pk=line.pk)
        # The rules read the label and the operation type only, both fetched
        # here: a deferred field read in the loop would be a query a credit.
        for other in others.only("pk", "counterparty", "label", "bank_type", "income_source"):
            # What a rule recognises no payer reaches, chosen or not - nor
            # what a sale reads before this payer.
            if payer_key(other) != key or automatic_source(other, rules) is not None:
                continue
            if holds not in COMPARED and is_sold(sale_refs(links, other.pk)):
                continue
            if other.income_source in CHOSEN:
                change.kept += 1
            else:
                change.followers += 1
    return change


def forget_payer(payer: IncomePayer) -> int:
    """« Oublier »: the payer no longer decides. Returns how many of its
    credits go back to « Autres entrées » - those with a choice of their own
    keep it, those a rule recognises never followed it, and nor did those a
    sale reads before a payer the till does not compare."""
    rules = recognition.load()
    links = read_links()
    credits = BankTransaction.objects.filter(amount__gt=0).exclude(income_source__in=CHOSEN)
    back = sum(
        1
        for line in credits.only("pk", "counterparty", "label", "bank_type", "income_source")
        if payer_key(line) == payer.key
        and follows_its_payer(line, rules, sold=is_sold(sale_refs(links, line.pk)), holds=payer.source)
    )
    payer.delete()
    return back


def rate(part: Decimal | None, whole: Decimal | None) -> Decimal | None:
    """`part` as a percentage of `whole`, to the hundredth - None where the
    whole is nothing: a share of nothing is not 0 %."""
    if part is None or whole is None or whole <= 0:
        return None
    return (part / whole * HUNDRED).quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class CategoryTotal:
    """« Autres entrées » of one name."""

    name: str
    amount: Decimal = ZERO
    count: int = 0


@dataclass
class TillMethod:
    """What the till was paid by one means of payment over the window."""

    method: str
    amount: Decimal = ZERO
    payments: int = 0

    @property
    def label(self) -> str:
        return PosDailyPayment.label_for(self.method)


@dataclass
class MethodRow:
    """One means of payment: what the till says it was paid, and what came
    in on the account for it. `till` is None for a row the till knows
    nothing of (« Autres entrées »), `bank` None for one the account cannot
    see (an « Avoir » paid days before)."""

    key: str
    label: str
    till: Decimal | None = None
    till_payments: int = 0
    bank: Decimal | None = None
    bank_count: int = 0
    #: The card row only: what the payouts' gross became on the account,
    #: the commission of those printing their gross, and how many printed
    #: none (counted at the amount received).
    net: Decimal | None = None
    commission: Decimal | None = None
    from_amount: int = 0
    note: str = ""
    #: Of `till`, what was taken on days before the statement's first line:
    #: the account cannot show it, whatever happened to it.
    uncovered: Decimal = ZERO
    #: Of `bank`, what arrived before the first till day whose payments
    #: were read: the till cannot show what it paid.
    bank_uncovered: Decimal = ZERO
    #: Of `bank`, what its credits gave sales invoices that count off the
    #: till (a counted invoice paid at the terminal, in a cheque deposit):
    #: a sale the till never rang, inside the Écart - said, never taken out.
    sales_inside: Decimal = ZERO

    @property
    def difference(self) -> Decimal | None:
        """What came in less what the till took, where both sides exist -
        over the days BOTH sides cover. The columns show the whole window;
        compared whole, a till history reaching back past the statement
        read as card takings that never arrived (and « tout » is where
        Banque's stat lands). The page says how much each side holds that
        the other cannot see."""
        if self.till is None or self.bank is None:
            return None
        return (self.bank - self.bank_uncovered) - (self.till - self.uncovered)


@dataclass
class Month:
    """One month of the window, both sides side by side. `label` is the
    page's (the view names months in French)."""

    first_day: date
    takings: Decimal = ZERO
    card_sold: Decimal = ZERO
    payouts_gross: Decimal = ZERO
    #: Of the payouts printing their gross; `payouts` and `from_amount`
    #: (those printing none) say how much of the month that covers - all of
    #: it unknown is « inconnue » on the page, never 0,00 €.
    commission: Decimal = ZERO
    payouts: int = 0
    from_amount: int = 0
    net: Decimal = ZERO
    cash_sold: Decimal = ZERO
    cash_deposited: Decimal = ZERO
    label: str = ""


@dataclass
class PayoutRow:
    """One payout of the window, with what the balance says after it."""

    entry: Entry
    #: The first and last till day of an exact run, or None.
    run: tuple[date, date] | None = None
    #: « Ventes carte pas encore versées » after this payout. None for a
    #: payout the balance does not count (dated before the till's history).
    pending: Decimal | None = None


@dataclass
class PayerRow:
    """A payer retained, and what follows it: its credits whose own choice
    does not beat it, over the window and over the whole history."""

    payer: IncomePayer
    count: int = 0
    total: Decimal = ZERO
    count_all: int = 0

    @property
    def source_label(self) -> str:
        return str(IncomeSource(self.payer.source).label) if self.payer.source in CHOSEN else self.payer.source


@dataclass
class Balance:
    """The running balance « ventes carte pas encore versées », over the
    whole history.

    `anchor` is the day it counts from: the day, in the week before the
    statement's first payout, from which the card sold up to that payout
    comes closest to what it paid. `pending` is keyed by a payout's pk.
    `reason` says why there is none, and is empty when there is one.
    """

    anchor: date | None = None
    first_payout: date | None = None
    pending: dict[int, Decimal] = field(default_factory=dict)
    reason: str = ""


#: Why there is no balance - the page prints these.
NO_CARD_DAYS = (
    "aucun paiement par carte n'est lu en caisse. Les moyens de paiement se relisent depuis les exports "
    "déjà téléchargés : manage.py laddition_backfill_payments."
)
#: NO_CARD_DAYS where that command is not this espace's to run: it runs on the
#: server, named in the platform owner's espace only (recipes/integration.py).
#: Chosen by the view (`views._balance_reason`), which knows the espace.
NO_CARD_DAYS_HOSTED = f"aucun paiement par carte n'est lu en caisse : {TILL_REIMPORT}."
NO_PAYOUT = "aucun versement carte sur le relevé après le premier jour de caisse lu."


@dataclass
class IncomeReport:
    window: DateRange
    #: Every credit of the window.
    received_total: Decimal = ZERO
    received_count: int = 0
    #: {source: (total, count)} - what arrived, net, from each source.
    by_source: dict[str, tuple[Decimal, int]] = field(default_factory=dict)
    payouts: list[PayoutRow] = field(default_factory=list)
    others: list[Entry] = field(default_factory=list)
    other_categories: list[CategoryTotal] = field(default_factory=list)
    #: The window's cash and cheque deposits, « Avoir » and meal vouchers:
    #: every credit of the window is in exactly one of `payouts`, `others`
    #: and this list, each with its « En caisse » choice.
    other_means: list[Entry] = field(default_factory=list)
    #: Every payer retained, whatever the window.
    payers: list[PayerRow] = field(default_factory=list)
    #: What went wrong with the till rules while the credits were read
    #: (`recognition.Rules.problems`), in French: a rule that recognised
    #: nothing has to be said, or its credits sit in « Autres entrées »
    #: unexplained.
    rule_problems: list[str] = field(default_factory=list)
    #: The till rules the credits were read with, in their order - what the
    #: page states it recognises, without reading the rules a second time.
    till_rules: tuple = ()

    #: The till over the same days. `takings` counts the days whose money
    #: was read (`revenue_read`); `unread_revenue_days` says how many were not.
    takings: Decimal = ZERO
    till: list[TillMethod] = field(default_factory=list)
    till_total: Decimal = ZERO
    #: Payments less takings, on the days both are read - « dont pourboires
    #: et trop-perçus ». A ticket's payments are its total plus its
    #: overpayment, so this is what the tickets say was left on top.
    tips: Decimal = ZERO
    tips_days: int = 0

    rows: list[MethodRow] = field(default_factory=list)
    months: list[Month] = field(default_factory=list)

    #: Till days of the window with sales and no means of payment read.
    days_without_payments: list[date] = field(default_factory=list)
    #: Till days of the window with a product whose money was never read.
    unread_revenue_days: list[date] = field(default_factory=list)
    #: Where each side starts and stops, over the whole history.
    last_till_day: date | None = None
    first_payment_day: date | None = None
    last_payment_day: date | None = None
    first_statement_day: date | None = None
    last_statement_day: date | None = None

    #: {till method: amount} the till took, inside the window, on days
    #: before the statement's first line - and the first and last such day.
    #: Nothing is counted when there is no statement at all: there is no
    #: edge then, only an empty account (the page says « aucun relevé »).
    till_before_statement: dict[str, Decimal] = field(default_factory=dict)
    till_before_from: date | None = None
    till_before_to: date | None = None
    #: {source: amount} that arrived, inside the window, before the first
    #: till day whose payments were read - CARD at the gross, the figure
    #: its row compares - and the first and last such day.
    bank_before_till: dict[str, Decimal] = field(default_factory=dict)
    bank_before_from: date | None = None
    bank_before_to: date | None = None
    #: {compared source: Σ the shares of sales invoices counting off the till
    #: that the window's credits read as that source carry} - only where
    #: there are some (`MethodRow.sales_inside`).
    sales_inside: dict[str, Decimal] = field(default_factory=dict)

    balance: Balance = field(default_factory=Balance)

    @property
    def till_before_statement_total(self) -> Decimal:
        return sum(self.till_before_statement.values(), ZERO)

    @property
    def till_card_before_statement(self) -> Decimal:
        return self.till_before_statement.get(PosDailyPayment.CARD, ZERO)

    @property
    def bank_before_till_total(self) -> Decimal:
        return sum(self.bank_before_till.values(), ZERO)

    @property
    def covered_since(self) -> date | None:
        """The first day both sides can speak of: the later of the
        statement's first line and the till's first day of payments read.
        None while either side holds nothing."""
        if self.first_statement_day is None or self.first_payment_day is None:
            return None
        return max(self.first_statement_day, self.first_payment_day)

    def source_total(self, source: str) -> Decimal:
        return self.by_source.get(source, (ZERO, 0))[0]

    def source_count(self, source: str) -> int:
        return self.by_source.get(source, (ZERO, 0))[1]

    @property
    def card_net(self) -> Decimal:
        return self.source_total(CARD)

    @property
    def card_count(self) -> int:
        return self.source_count(CARD)

    @property
    def card_gross(self) -> Decimal:
        """The payouts' gross - the amount received for those printing none."""
        return sum((row.entry.gross for row in self.payouts), ZERO)

    @property
    def card_printed_gross(self) -> Decimal:
        """The gross of the payouts printing it: what a commission is of."""
        return sum((row.entry.gross for row in self.payouts if not row.entry.gross_from_amount), ZERO)

    @property
    def card_commission(self) -> Decimal | None:
        """Of the payouts printing their gross: nothing says what the others
        kept, and counted as 0 they would make the rate look lower. None
        where not one payout of the window printed it - 0,00 € would read
        as « no fee »."""
        known = [row.entry.commission for row in self.payouts if row.entry.commission is not None]
        if not known and self.card_from_amount:
            return None
        return sum(known, ZERO)

    @property
    def card_commission_rate(self) -> Decimal | None:
        return rate(self.card_commission, self.card_printed_gross)

    @property
    def card_from_amount(self) -> int:
        """Payouts printing no gross, counted at the amount received."""
        return sum(1 for row in self.payouts if row.entry.gross_from_amount)

    @property
    def cash_total(self) -> Decimal:
        return self.source_total(CASH)

    @property
    def cash_count(self) -> int:
        return self.source_count(CASH)

    @property
    def cheque_total(self) -> Decimal:
        return self.source_total(CHEQUE)

    @property
    def cheque_count(self) -> int:
        return self.source_count(CHEQUE)

    @property
    def others_total(self) -> Decimal:
        return self.source_total(OTHER)

    @property
    def unnamed_others(self) -> int:
        return sum(1 for one in self.others if one.unnamed)

    @property
    def last_payout(self) -> PayoutRow | None:
        """The window's last payout the balance counts - the headline."""
        counted = [row for row in self.payouts if row.pending is not None]
        return counted[-1] if counted else None

    @property
    def balance_points(self) -> list[tuple[date, Decimal]]:
        """The balance over the window, one point per payout DAY (the last
        payout of a day says where the day ended): a line chart has one
        value per date."""
        points: dict[date, Decimal] = {}
        for row in self.payouts:
            if row.pending is not None:
                points[row.entry.day] = row.pending
        return sorted(points.items())

    @property
    def till_read(self) -> bool:
        """Whether the till says anything about the window at all."""
        return bool(self.till) or self.takings != 0


def income_for(window: DateRange) -> IncomeReport:
    """Everything that came in over `window` (bank lines by
    `operation_date`, till days by `sold_on`, both ends included; an empty
    window is all of it), beside the till over the same days - and the
    running balance of card sales not yet paid, which is computed over the
    whole history and only SHOWN for the window."""
    report = IncomeReport(window=window)

    # The payers retained, read once: every credit below is read against
    # them in Python (`entry_for`), never one query a line.
    retained = list(IncomePayer.objects.all())
    payers = {payer.key: payer.source for payer in retained}
    # The till rules, the same way: one query.
    rules = recognition.load()
    # Every credit, the whole history: the balance and the exact runs are
    # worked out from the first payout on, so that a window cannot change
    # them. Windowed here, in Python, with the one definition of « in the
    # window » that is not SQL (`DateRange.holds`).
    credits = list(BankTransaction.objects.filter(amount__gt=0).order_by("operation_date", "pk"))
    # The sales invoices they pay, read once: two queries, whatever the links.
    links = read_links()
    entries = [entry_for(line, payers, rules, sale_refs(links, line.pk)) for line in credits]
    # After every credit: a rule found too slow on one of them is said too.
    report.rule_problems = rules.problems
    report.till_rules = rules.till
    report.payers = _payer_rows(retained, entries, window)
    payments = list(PosDailyPayment.objects.values_list("sold_on", "method", "amount", "payments"))
    # Where each side starts and stops - read before the loops below, which
    # set apart what one side holds on days the other cannot see. The
    # statement's ends are one aggregate over every line, debits included:
    # a debit on a day says the statement covers that day.
    statement = BankTransaction.objects.aggregate(first=Min("operation_date"), last=Max("operation_date"))
    report.first_statement_day, report.last_statement_day = statement["first"], statement["last"]
    report.first_payment_day = min((row[0] for row in payments), default=None)
    report.last_payment_day = max((row[0] for row in payments), default=None)
    card_days: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for sold_on, method, amount, _count in payments:
        if method == PosDailyPayment.CARD:
            card_days[sold_on] += amount
    card_days = dict(card_days)
    payouts = [entry for entry in entries if entry.source == CARD]
    report.balance = running_balance(card_days, payouts)
    runs = exact_runs(card_days, payouts)

    months: dict[date, Month] = {}

    def month_of(day: date) -> Month:
        first = day.replace(day=1)
        found = months.get(first)
        if found is None:
            found = months[first] = Month(first)
        return found

    by_source: dict[str, list] = defaultdict(lambda: [ZERO, 0])
    categories: dict[str, CategoryTotal] = {}
    bank_before: dict[str, Decimal] = defaultdict(lambda: ZERO)
    bank_before_days: list[date] = []
    sales_inside: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for entry in entries:
        if not window.holds(entry.day):
            continue
        report.received_total += entry.net
        report.received_count += 1
        by_source[entry.source][0] += entry.net
        by_source[entry.source][1] += 1
        # A sale the till never rang, inside a payout or a deposit the till
        # compares: its row says how much (one pass, no query).
        if entry.source in COMPARED:
            for ref in entry.sales:
                if ref.counts_off_till and ref.share:
                    sales_inside[entry.source] += ref.share
        # Money in before the till's first day of payments read pays sales
        # the till never read: compared, it reads as money from nowhere.
        if report.first_payment_day is not None and entry.day < report.first_payment_day and entry.source in COMPARED:
            amount = entry.gross if entry.source == CARD else entry.net
            if amount:
                bank_before[entry.source] += amount
                bank_before_days.append(entry.day)
        month = month_of(entry.day)
        if entry.source == CARD:
            report.payouts.append(PayoutRow(entry, runs.get(entry.line.pk), report.balance.pending.get(entry.line.pk)))
            month.payouts_gross += entry.gross
            month.net += entry.net
            month.payouts += 1
            # An unknown commission adds nothing, and is counted: a month of
            # them alone is « inconnue », never a 0 that reads as « no fee ».
            if entry.commission is not None:
                month.commission += entry.commission
            else:
                month.from_amount += 1
        elif entry.source == OTHER:
            report.others.append(entry)
            category = categories.setdefault(entry.category, CategoryTotal(entry.category))
            category.amount += entry.net
            category.count += 1
        else:
            report.other_means.append(entry)
            if entry.source == CASH:
                month.cash_deposited += entry.net
    report.by_source = {key: (value[0], value[1]) for key, value in by_source.items()}
    report.sales_inside = dict(sales_inside)
    report.bank_before_till = dict(bank_before)
    if bank_before_days:
        report.bank_before_from, report.bank_before_to = min(bank_before_days), max(bank_before_days)
    report.other_categories = _ordered(categories.values())
    # The unnamed first - they are the work - then the biggest.
    report.others.sort(key=lambda one: (not one.unnamed, -one.net, one.day, one.line.pk))

    # The till. Its payments per day and method, and its takings per day.
    till: dict[str, TillMethod] = {}
    paid_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    till_before: dict[str, Decimal] = defaultdict(lambda: ZERO)
    till_before_days: list[date] = []
    for sold_on, method, amount, count in payments:
        if not window.holds(sold_on):
            continue
        one = till.setdefault(method, TillMethod(method))
        one.amount += amount
        one.payments += count
        paid_by_day[sold_on] += amount
        # Taken on a day before the statement's first line: whatever became
        # of it, the account cannot show it. Counted apart so the Écart is
        # taken over the days both sides cover, and said on the page.
        if report.first_statement_day is not None and sold_on < report.first_statement_day and amount:
            till_before[method] += amount
            till_before_days.append(sold_on)
        month = month_of(sold_on)
        if method == PosDailyPayment.CARD:
            month.card_sold += amount
        elif method == PosDailyPayment.CASH:
            month.cash_sold += amount
    report.till = sorted(till.values(), key=lambda one: PosDailyPayment.sort_key(one.method))
    report.till_total = sum((one.amount for one in report.till), ZERO)
    # Methods that net to nothing over those days are no edge to speak of.
    report.till_before_statement = {method: amount for method, amount in till_before.items() if amount}
    if till_before_days:
        report.till_before_from, report.till_before_to = min(till_before_days), max(till_before_days)

    taken_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    unread: set[date] = set()
    rows = window.limit(PosProductDailyQuantity.objects.all(), "sold_on").values_list(
        "sold_on", "revenue_ttc", "revenue_read"
    )
    for sold_on, revenue_ttc, revenue_read in rows:
        # A day nobody read the money of holds 0,00 € it never took: it is
        # counted apart and named at the top, never read as a quiet day.
        if not revenue_read:
            unread.add(sold_on)
            continue
        taken_by_day[sold_on] += revenue_ttc
    # A row is one till product on one day: added up by day first, then each
    # day into the total and its month - the same sums, since every amount
    # has the column's two places and none of them rounds, for a month
    # looked up once a day rather than once a row.
    for sold_on, taken in taken_by_day.items():
        report.takings += taken
        month_of(sold_on).takings += taken
    sold_days = set(taken_by_day) | unread
    report.days_without_payments = sorted(day for day in sold_days if day not in paid_by_day)
    report.unread_revenue_days = sorted(unread)
    # Tips on the days both sides are read whole - a day half-read would
    # count its unread takings as tips.
    both = [day for day in paid_by_day if day in taken_by_day and day not in unread]
    report.tips = sum((paid_by_day[day] - taken_by_day[day] for day in both), ZERO)
    report.tips_days = len(both)

    report.rows = _method_rows(report, till)
    report.months = _months(months)

    report.last_till_day = PosProductDailyQuantity.objects.aggregate(last=Max("sold_on"))["last"]
    return report


def _payer_rows(retained: list[IncomePayer], entries: list[Entry], window: DateRange) -> list[PayerRow]:
    """Each payer retained, with the credits it decides - counted off the
    entries already read, no query."""
    rows = {payer.key: PayerRow(payer) for payer in retained}
    for entry in entries:
        if entry.how != BY_PAYER:
            continue
        row = rows.get(entry.payer)
        if row is None:  # pragma: no cover - a payer reading is a retained key
            continue
        row.count_all += 1
        if window.holds(entry.day):
            row.count += 1
            row.total += entry.net
    return list(rows.values())


def running_balance(card_days: dict[date, Decimal], payouts: list[Entry]) -> Balance:
    """« Ventes carte pas encore versées » after each payout, over the whole
    history.

    F is the till's first card day, and only a payout dated after it counts:
    before, the till says nothing about what it paid. T1 is the first such
    payout's date and G1 what the payouts of that day collected. The balance
    counts the card sold from an anchor A - the day in [max(F, T1 − 7), T1 −
    1] whose card sold up to T1 comes closest to G1, the latest on a tie -
    so it starts from what the statement's first payout says it paid, not
    from a guess. After each payout i: card sold on A ≤ d < T_i, less every
    gross paid up to and including i.

    A payout that never came, or a card ticket paid in reality by transfer,
    is a step the balance never comes back down from - which is the point:
    that is money the page cannot find on the account.
    """
    days = sorted(card_days)
    if not days:
        return Balance(reason=NO_CARD_DAYS)
    first = days[0]
    counted = [payout for payout in payouts if payout.day > first]
    if not counted:
        return Balance(reason=NO_PAYOUT)
    sold_before = _card_sold_before(days, card_days)

    t1 = counted[0].day
    g1 = sum((payout.gross for payout in counted if payout.day == t1), ZERO)
    anchor, best = None, None
    day = t1 - timedelta(days=1)
    lowest = max(first, t1 - timedelta(days=ANCHOR_DAYS))
    # From the latest day back, a strictly better gap only: a tie keeps the
    # latest day, which counts the fewest days the payout may not have paid.
    while day >= lowest:
        gap = abs(sold_before(t1) - sold_before(day) - g1)
        if best is None or gap < best:
            anchor, best = day, gap
        day -= timedelta(days=1)

    balance = Balance(anchor=anchor, first_payout=t1)
    paid = ZERO
    for payout in counted:
        paid += payout.gross
        balance.pending[payout.line.pk] = sold_before(payout.day) - sold_before(anchor) - paid
    return balance


def _card_sold_before(days: list[date], card_days: dict[date, Decimal]):
    """card sold on every day strictly before `day`, as a function - one
    running sum, bisected, rather than a sum per payout."""
    running = [ZERO]
    for day in days:
        running.append(running[-1] + card_days[day])

    def sold_before(day: date) -> Decimal:
        return running[bisect_left(days, day)]

    return sold_before


def exact_runs(card_days: dict[date, Decimal], payouts: list[Entry]) -> dict[int, tuple[date, date]]:
    """{payout pk: (first day, last day)} for the payouts whose gross equals,
    to the cent, the card sold on a run of consecutive card days.

    Consecutive means adjacent in the sorted list of days that sold by card
    - a day the bar was shut does not break a run - within [T − RUN_DAYS,
    T − 1], no day already claimed by an earlier payout. The run ending
    latest wins, then the shortest; its days are claimed. Payouts are taken
    in date order over the whole history, so a window never changes which
    days a payout's run holds. Nothing found is None, and the page says « — »:
    two amounts equal to the cent is what this says, and nothing beyond it.
    """
    days = sorted(day for day, amount in card_days.items() if amount != 0)
    claimed: set[date] = set()
    found: dict[int, tuple[date, date]] = {}
    for payout in payouts:
        low = bisect_left(days, payout.day - timedelta(days=RUN_DAYS))
        high = bisect_right(days, payout.day - timedelta(days=1))
        run = None
        for end in range(high - 1, low - 1, -1):
            total = ZERO
            for start in range(end, low - 1, -1):
                if days[start] in claimed:
                    break
                total += card_days[days[start]]
                if total == payout.gross:
                    run = (start, end)
                    break
            if run is not None:
                break
        if run is not None:
            start, end = run
            claimed.update(days[start : end + 1])
            found[payout.line.pk] = (days[start], days[end])
    return found


#: What each row of the comparison says beside its figures - said, never
#: judged: a difference between the till and the account has reasons the
#: page cannot see.
NOTES = {
    CARD: (
        "Un versement arrive 1 à 3 jours après la vente : aux bords de la période, quelques jours de ventes "
        "carte passent d'un côté ou de l'autre. Le solde « ventes carte pas encore versées » dit s'il manque "
        "de l'argent."
    ),
    CASH: "La différence est gardée en caisse ou payée en liquide : la page ne sait pas laquelle.",
    CHEQUE: "Un chèque se remet quand on passe à la banque, pas le jour de la vente.",
    CREDIT: (
        "Réglé par un acompte encaissé avant, souvent par virement : choisissez « Avoir » sur ce virement pour "
        "le compter ici."
    ),
    VOUCHER: (
        "Remboursés plus tard par l'émetteur, nets de sa commission : choisissez « Titres-restaurant » sur ses "
        "virements pour les compter ici."
    ),
    PosDailyPayment.UNREAD: "Tickets dont la caisse n'a pas pu lire les paiements, comptés à leur total.",
    PosDailyPayment.UNPAID: "Tickets sans paiement enregistré, comptés à leur total.",
    SALE: "Payées hors caisse, sur facture : comparées à rien.",
    OTHER: "Ce que la caisse ne voit pas : un virement reçu, un apport, un remboursement.",
}


def _method_rows(report: IncomeReport, till: dict[str, TillMethod]) -> list[MethodRow]:
    """« Ce que la caisse a encaissé, et ce qui est arrivé sur le compte »:
    one row per means of payment, those the account can see first
    (`BANK_SIDE`: meal vouchers and « Avoir » once a credit is said to be
    one), then the till's others, then « Factures de vente » (compared with
    nothing), then « Autres entrées ». A compared row says how much of what
    arrived paid sales invoices the till never rang (`sales_inside`)."""

    def paid(method: str) -> TillMethod:
        return till.get(method) or TillMethod(method)

    card = paid(PosDailyPayment.CARD)
    cash = paid(PosDailyPayment.CASH)
    cheque = paid(PosDailyPayment.CHEQUE)

    def edges(method: str, source: str) -> dict:
        """What each side of a row holds on days the other cannot see -
        left out of its Écart (`MethodRow.difference`)."""
        return {
            "uncovered": report.till_before_statement.get(method, ZERO),
            "bank_uncovered": report.bank_before_till.get(source, ZERO),
        }

    rows = [
        MethodRow(
            CARD,
            "Carte",
            card.amount,
            card.payments,
            report.card_gross,
            report.card_count,
            net=report.card_net,
            commission=report.card_commission,
            from_amount=report.card_from_amount,
            note=NOTES[CARD],
            **edges(PosDailyPayment.CARD, CARD),
        ),
        MethodRow(
            CASH,
            "Espèces",
            cash.amount,
            cash.payments,
            report.cash_total,
            report.cash_count,
            note=NOTES[CASH],
            **edges(PosDailyPayment.CASH, CASH),
        ),
    ]
    if cheque.payments or report.cheque_count:
        rows.append(
            MethodRow(
                CHEQUE,
                "Chèques",
                cheque.amount,
                cheque.payments,
                report.cheque_total,
                report.cheque_count,
                note=NOTES[CHEQUE],
                **edges(PosDailyPayment.CHEQUE, CHEQUE),
            )
        )
    # Meal vouchers and « Avoir »: the till's, and - once a person said
    # which credits they are - the account's. Until then the account holds
    # nothing it knows to be theirs (« — », never 0: an Écart of the whole
    # till figure would accuse a transfer filed under « Autres entrées »).
    for method in (PosDailyPayment.MEAL_VOUCHER, PosDailyPayment.CREDIT):
        source = BANK_SIDE[method]
        one = paid(method)
        counted = report.source_count(source)
        if method not in till and not counted:
            continue
        rows.append(
            MethodRow(
                source,
                PosDailyPayment.label_for(method),
                one.amount,
                one.payments,
                report.source_total(source) if counted else None,
                counted,
                note=NOTES[source],
                **edges(method, source),
            )
        )
    for row in rows:
        row.sales_inside = report.sales_inside.get(row.key, ZERO)
    seen = set(BANK_SIDE)
    for one in report.till:
        if one.method in seen:
            continue
        rows.append(
            MethodRow(
                one.method,
                one.label,
                one.amount,
                one.payments,
                note=NOTES.get(one.method, ""),
                uncovered=report.till_before_statement.get(one.method, ZERO),
            )
        )
    if report.source_count(SALE):
        rows.append(
            MethodRow(
                SALE,
                "Factures de vente",
                bank=report.source_total(SALE),
                bank_count=report.source_count(SALE),
                note=NOTES[SALE],
            )
        )
    if report.others:
        rows.append(
            MethodRow(
                OTHER, "Autres entrées", bank=report.others_total, bank_count=len(report.others), note=NOTES[OTHER]
            )
        )
    return rows


def _months(months: dict[date, Month]) -> list[Month]:
    """The window's months, from the first holding anything on either side
    to the last - a month between them with nothing at all included, since
    « nothing came in in March » is an answer and a missing row is not. The
    ends stop at the data: a window reaching back to before the bar opened
    is not a column of empty years."""
    if not months:
        return []
    ordered = []
    day, last = min(months), max(months)
    while day <= last:
        ordered.append(months.get(day) or Month(day))
        day = (day + timedelta(days=32)).replace(day=1)
    return ordered


def _ordered(categories) -> list[CategoryTotal]:
    """« Sans catégorie » first - the work to do - then the biggest, by name
    on a tie: the order « Dépenses » gives its own."""
    return [one for one in categories if one.name == NO_CATEGORY] + sorted(
        (one for one in categories if one.name != NO_CATEGORY),
        key=lambda one: (-one.amount, search_key(one.name), one.name),
    )


def known_categories() -> list[str]:
    """The categories already typed on CREDITS, for the datalist - never the
    spending ones (`spending.known_categories` reads debits only): a word
    for money that went out offered for money that came in files an income
    under a spending's name."""
    names = set(BankTransaction.objects.filter(amount__gt=0).exclude(category="").values_list("category", flat=True))
    return sorted(names, key=lambda name: (search_key(name), name))
