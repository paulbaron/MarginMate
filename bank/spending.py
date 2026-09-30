"""Everything that LEFT the account over a window, by category.

**This is not « Marges ».** Marges counts what was INVOICED, by invoice
date, and answers « have I made money ». This counts what the bank
took, by the date it took it, invoice or no invoice, and answers « where did
the money go ». Two bases, two figures, and each page has to say which it
is or a reader will take one for the other. Income is not spending and is
nowhere in here.

The base is the statement, so **the categories add back up to what left the
account, to the cent**. Everything below follows from that one rule:

* **A line an invoice explains takes the categories of what that invoice
  bought** - read through `margins.computation.where_it_went`, never worked
  out again here. A goods line lands on its article's category, a charge's
  document whole on its supplier (a charge has no article, and its supplier
  IS what it is: the rent, the electricity, the phone), a goods line no
  article claims on « Sans article (à classer) ». Copied rather than shared,
  the two pages would hold half a definition each of « where an invoice's
  money went », and a figure with two definitions is this codebase's oldest
  sin.
* **The invoices give the shape, the bank gives the amount.** They no longer
  have to add up to the line (bank/reconcile.py), so where they cost MORE
  than the debit - an invoice settled in two goes, a debit split - every
  place is scaled down pro rata to what the line really paid. Counted whole
  on both lines, that invoice would be bought twice, and the page would say
  the bar spent money it never spent. Where they cost LESS, the difference
  is money that left the account with nothing to explain it: on a line that
  has invoices it goes to « Sans catégorie » whatever that line or a rule
  calls itself, and `uninvoiced` says how much of it there is. Named after
  the line instead, it would sit in the pie under a heading no row on this
  page can explain or correct - the list below only offers the lines with
  no invoice at all - whereas « Sans catégorie » is the one label the page
  reconciles (`unsaid_beyond_the_list`) and tells a reader to act on.
* **A line no invoice explains takes the category a person typed** on it, or
  the one an active `IgnoreRule` carries (a loan, the URSSAF, the salaries -
  the same spending every month, which a rule already recognises). What a
  person typed wins: a rule is a guess about a label, and the page says
  which of the two named each line, since a rule edited next month must not
  read as somebody's decision.
* **Nothing is forced.** A line nobody has categorised is « Sans
  catégorie » - counted, listed first, and never folded into « Autres ».
  Dropped from the pie it would make every other slice look bigger than it
  is, which is the one thing a pie must not do.

A category is its NAME, not a key: a person typing « Bières » on a debit
means the same thing as the article category « Bières », and two slices of
one name is a chart nobody can read. That is also why « Sans catégorie »
(nobody said) and « Catégorie non renseignée » (an article whose category is
blank) are deliberately two different words for two different silences.

**A category can be left out of the pie** (the owner, 27/09: the VAT paid
over is no money the bar spent on anything, and with it in, every other
wedge reads smaller than it is). What is left out is a VIEW carried in the
address (`?sans=`, by name), never a setting, and it touches THE PIE and
nothing else: the category stays a row of the table and stays in `total`,
because the page's whole argument is that its categories add back up to
what left the account. It is no wedge, never folded into « Autres », and the
shares are worked out over what remains - `left_out_total` is the third
figure, beside `given_back`, that reconciles what the pie draws with what
left the account.

Pure of request and template: a `DateRange` in, a `SpendingReport` out.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Prefetch

from common import PIE_COLORS, DateRange
from inventory.models import StockType
from invoices.models import Supplier
from margins.computation import (
    CHARGES_KEY,
    TO_CLASSIFY_KEY,
    TO_CLASSIFY_NAME,
    Place,
    cents,
    lines_prefetch,
    where_it_went,
)

from .models import BankTransaction, IgnoreRule, InvoicePayment
from .rules import compile_rules, ignoring_rule

ZERO = Decimal("0")
HUNDRED = Decimal("100")
#: What a share is rounded to - the figure the page prints, so the legend and
#: the table hold the same number and the wedges come to a hundred exactly.
SHARE_PLACES = Decimal("0.1")

#: What nobody has said. Deliberately NOT « Catégorie non renseignée », which
#: « Marges » already uses for an article whose own category is blank: two
#: different silences, and one word for both is a table nobody can read.
NO_CATEGORY = "Sans catégorie"
#: The pie's tail. It says how many categories it holds; the table beside the
#: pie still lists every one of them, because a pie is a shape and a table is
#: the figures.
OTHERS = "Autres"
#: A category thinner than this share of the pie goes into « Autres ». The
#: rule used to be « as many wedges as the palette has colours », which on a
#: real month made « Autres » one of the biggest slices on the page while
#: holding nothing anyone wanted to look up (owner, 24/09). A share says what
#: a count cannot: a wedge under half a per cent is a hairline nobody can
#: read, and that is the only thing the tail is for.
SMALLEST_SLICE = Decimal("0.5")
#: The column's own width, asked of the model rather than written twice:
#: SQLite stores a longer string without a word, and every read of it
#: afterwards is somebody's problem.
CATEGORY_MAX = BankTransaction._meta.get_field("category").max_length

#: How many debits « Dépenses sans facture » draws before it asks for a
#: narrower period. Each row is a form with a text field in it, and this is
#: the page's work list - so the count of what is left out is said beside it,
#: never a silent cut.
LIST_SIZE = 60

#: How a line got its category, for the page to say so.
BY_HAND, BY_RULE, UNSAID = "main", "regle", "rien"
#: The three, as « Dépenses » names them on its chips - one vocabulary for
#: the value stored on a Spending, the query parameter and the label, so a
#: reader's URL and the page cannot mean two different things. The order is
#: the order the chips are drawn in: the work first.
KINDS = {
    UNSAID: "À classer",
    BY_HAND: "Classées à la main",
    BY_RULE: "Classées par règle",
}
#: The query parameter that chooses one.
KIND_PARAM = "classement"
#: What an empty list says, per kind. Here rather than in the template, so
#: the page never spells a kind's stored value out: branching on « main » in
#: HTML is a second place that knows the vocabulary, and the day one of these
#: values changes it would quietly fall through and tell a reader looking at
#: « Classées à la main » that there is nothing left to classify.
EMPTY = {
    UNSAID: "Aucune dépense à classer sur cette période : toutes portent déjà un nom.",
    BY_HAND: "Aucune dépense classée à la main sur cette période.",
    BY_RULE: "Aucune dépense classée par une règle sur cette période.",
}

#: What `spending_for` costs, whatever the number of operations: the lines,
#: their payments, those payments' invoice lines, and the ignore rules.
#: A test holds it - an N+1 here is one query per debit of the year.
QUERIES = 4


@dataclass
class Category:
    """One slice's worth of spending: what left the account for it."""

    name: str
    amount: Decimal = ZERO
    #: How many bank lines put money here - not how many invoices.
    operations: int = 0
    #: How many of those an ignore rule named rather than a person.
    by_rule: int = 0
    #: Of what the pie draws, None for a category it does not draw.
    share: Decimal | None = None
    #: Left out of the pie by the reader (`?sans=`). Still a row of the
    #: table and still in `total`: only the drawing loses it.
    left_out: bool = False

    @property
    def drawn(self) -> bool:
        """Whether the pie draws it: money in it, and nobody left it out."""
        return self.amount > 0 and not self.left_out


@dataclass
class Slice:
    """One wedge of the pie. `held` is what « Autres » stands for."""

    name: str
    amount: Decimal
    share: Decimal
    color: str
    held: int = 0


@dataclass
class Spending:
    """One debit no invoice explains, and what it counts as.

    Listed whether or not it has a category, so a name typed by mistake can
    be typed again: named once, a line that dropped off the page would leave
    its own typo with nowhere to fix it. `source` is who said it, which the
    page prints - a rule edited next month must not read as a decision.
    """

    line: BankTransaction
    name: str
    source: str
    rule: IgnoreRule | None = None

    @property
    def unsaid(self) -> bool:
        return self.source == UNSAID

    @property
    def by_rule(self) -> bool:
        return self.source == BY_RULE

    @property
    def by_hand(self) -> bool:
        """A category a person typed on this line. It beats the rule's, so
        this is also the only kind the page cannot explain by pointing at
        something else - hence the view that shows them."""
        return self.source == BY_HAND


@dataclass
class SpendingReport:
    window: DateRange
    #: What left the account over the window - every category added up.
    total: Decimal = ZERO
    operations: int = 0
    categories: list[Category] = field(default_factory=list)
    slices: list[Slice] = field(default_factory=list)
    #: What the pie draws: the categories with money in them that nobody
    #: left out. Equal to `total` unless something was given back or left
    #: out (below): `total == drawn_total + given_back + left_out_total`,
    #: always, and the page says so in a sentence.
    drawn_total: Decimal = ZERO
    #: A category that came out at or below zero over the window - a keg
    #: given back, a credit note - and that the reader did not leave out. It
    #: cannot be a wedge of a pie, so it is listed instead of being dropped,
    #: and this is part of what reconciles the two totals on screen. The
    #: stat « Déduit par les factures » reads `deducted`, which does not
    #: depend on what the pie leaves out.
    given_back: Decimal = ZERO
    #: The categories the reader left out of the pie (`?sans=`), as the page
    #: understood them (`left_out_names`): cleaned, once each, in the order
    #: asked. A name with nothing in the window stays - it is a view about
    #: names, valid over another period - and the page says it has nothing
    #: here.
    left_out: list[str] = field(default_factory=list)
    #: What those categories hold over the window, whatever its sign.
    left_out_total: Decimal = ZERO
    #: Debited beyond the invoices linked to those debits: the sign that an
    #: invoice is missing, not that a category is wrong.
    uninvoiced: Decimal = ZERO
    #: The debits no invoice explains, the unnamed ones first - the work the
    #: page exists to make possible.
    without_invoice: list[Spending] = field(default_factory=list)
    #: Which of the three kinds the list is narrowed to (BY_HAND, BY_RULE,
    #: UNSAID), or "" for all of them. It narrows THE LIST and nothing else:
    #: every figure on the page is read off the whole window, and a page
    #: whose argument is that its categories add back up to what left the
    #: account must not appear to change the money when a work list is
    #: narrowed.
    kind: str = ""

    @property
    def selected(self) -> list[Spending]:
        """`without_invoice` narrowed to `kind`, in the same order.

        Narrowed HERE, before the cap below. After it, « classées à la main »
        would draw an empty table under a chip counting them: the list sorts
        the unnamed first, so on a real statement every hand-typed debit sat
        behind more unnamed ones than the cap draws, and the rows the page
        drew were every one of them unnamed. Not one hand-typed debit was
        reachable at any period, which is what this view is for.
        """
        if not self.kind:
            return self.without_invoice
        return [one for one in self.without_invoice if one.source == self.kind]

    @property
    def counts(self) -> dict[str, int]:
        """How many debits of each kind there ARE - over the whole window,
        never what fitted under the cap. A chip counting the page would say
        « 0 » next to the very rows it exists to reach."""
        tally = {"": len(self.without_invoice), BY_HAND: 0, BY_RULE: 0, UNSAID: 0}
        for one in self.without_invoice:
            tally[one.source] += 1
        return tally

    @property
    def listed(self) -> list[Spending]:
        """What the page draws of `selected`. Every figure above is read off
        the WHOLE list, so the cap is a page weight and never an amount:
        over « tout l'historique » a multi-year statement draws hundreds of
        text fields, which is the lesson `SALES_PAGE_SIZE` exists for. The
        unnamed and the biggest are first, so what a cap leaves out is the
        work worth least."""
        return self.selected[:LIST_SIZE]

    @property
    def not_listed(self) -> int:
        """What the cap left out OF THIS VIEW - « 5 de plus » has to mean
        five more of what the reader is looking at, not of a list they are
        not."""
        return max(len(self.selected) - LIST_SIZE, 0)

    @property
    def uncategorised(self) -> list[Spending]:
        return [one for one in self.without_invoice if one.unsaid]

    @property
    def uncategorised_total(self) -> Decimal:
        return sum((one.line.amount_due for one in self.uncategorised), ZERO)

    @property
    def unsaid_total(self) -> Decimal:
        """What the « Sans catégorie » ROW says - the figure in the table.

        Read off the categories rather than added up from the list below,
        because the two are not the same thing and a page that prints one
        under the other's label is a page nobody checks twice.
        """
        row = next((one for one in self.categories if one.name == NO_CATEGORY), None)
        return row.amount if row is not None else ZERO

    @property
    def unsaid_beyond_the_list(self) -> Decimal:
        """What « Sans catégorie » holds that the list below does not: the
        part of a debit its own invoices did not cover, where nothing named
        that line. Said on the page, or the two figures differ under one
        label with nothing explaining it."""
        return self.unsaid_total - self.uncategorised_total

    @property
    def deducted(self) -> Decimal:
        """What « Déduit par les factures » says: every category that came
        out at or below zero, left out of the pie or not.

        Not `given_back`, which is the part of it the pie's sentence still
        has to account for once the left-out categories are counted apart.
        The stat is about the money, and nothing a reader leaves out of a
        drawing may move a figure about the money - read off `given_back`,
        leaving « Consignes » out of the pie over a month a keg came back
        would make the stat vanish."""
        return sum((one.amount for one in self.categories if one.amount <= 0), ZERO)

    @property
    def nothing_left_to_draw(self) -> bool:
        """Every category the pie would have drawn is left out of it: no pie,
        and the page says why rather than leaving a hole where it was."""
        return not self.slices and any(one.left_out and one.amount > 0 for one in self.categories)


def spending_for(window: DateRange, kind: str = "", left_out=()) -> SpendingReport:
    """Every debit of `window`, in its categories.

    `kind` narrows the work list at the foot of the page to the debits named
    one way (BY_HAND, BY_RULE, UNSAID) and changes nothing else - see
    `SpendingReport.kind`. Anything it does not recognise is every kind:
    it arrives from a query string, so a stale bookmark and a typed URL both
    land here, and an empty page under a filter nobody can see reads as a
    page that has broken.

    `left_out` names the categories the pie leaves out (`?sans=`, read
    through `left_out_names`), and it changes THE PIE alone: every category
    stays in the table and in `total`, the left-out ones marked, with no
    share, and the shares and « Autres » are worked out over what remains.
    It costs no query - a name is compared with the names already built.
    """
    report = SpendingReport(window=window, kind=kind if kind in KINDS else "", left_out=left_out_names(left_out))
    rules = compile_rules(IgnoreRule.objects.filter(is_active=True))
    categories: dict[str, Category] = {}
    # One reading per invoice, not per payment: an invoice settled in two
    # goes is on two lines, and `where_it_went` walks all of its own lines.
    read: dict[int, dict[str, Decimal]] = {}

    for line in _lines(window):
        due = line.amount_due
        report.total += due
        report.operations += 1
        rule = ignoring_rule(line.label, rules)
        own, source = _own_category(line, rule)
        payments = list(line.payments.all())

        parts: dict[str, Decimal] = {}
        invoiced = ZERO
        for payment in payments:
            for name, amount in _read_invoice(payment.invoice, read).items():
                parts[name] = parts.get(name, ZERO) + amount
                invoiced += amount
        if parts and invoiced > due:
            parts = _scaled(parts, due, invoiced)
        elif invoiced != due:
            # On a line NOTHING invoiced, the difference is the whole debit
            # and it takes the name the line carries. On a line that has
            # invoices, what they do not cover is money nothing explains,
            # and it is « Sans catégorie » even when a rule or a person
            # named the line: that part is drawn in the pie, and only this
            # label has a figure on the page that reconciles it and a stat
            # that points at where the missing invoice is found.
            name = NO_CATEGORY if payments else own
            parts[name] = parts.get(name, ZERO) + (due - invoiced)
            if payments:
                report.uninvoiced += due - invoiced

        for name, amount in parts.items():
            category = categories.get(name)
            if category is None:
                category = categories[name] = Category(name)
            category.amount += amount
            category.operations += 1
            # Only a line with no invoice at all was named by a rule; on one
            # with invoices the rule says nothing about where the money went.
            if not payments and name == own and source == BY_RULE:
                category.by_rule += 1
        if not payments:
            report.without_invoice.append(Spending(line, own, source, rule))

    report.categories = _ordered(categories.values())
    # Marked before anything is added up: `drawn` asks it. Every category is
    # then exactly one of drawn, left out, or given back, so the three add
    # back up to `total` - which the page's sentence under the pie says.
    # Compared CLEANED on both sides: the names asked arrive through
    # `clean_category`, and a name stored in another shape (a double space
    # on a rule written before its form cleaned it, an article category
    # only trimmed) could otherwise never be left out - the reader unticks
    # it and the pie goes on drawing it.
    asked = set(report.left_out)
    for one in report.categories:
        one.left_out = clean_category(one.name) in asked
    report.drawn_total = sum((one.amount for one in report.categories if one.drawn), ZERO)
    report.left_out_total = sum((one.amount for one in report.categories if one.left_out), ZERO)
    report.given_back = sum((one.amount for one in report.categories if not one.drawn and not one.left_out), ZERO)
    _share_out(report.categories, report.drawn_total)
    report.slices = _slices(report.categories, report.drawn_total)
    # The unnamed first - that is the work - and inside each half the biggest
    # first, since the one worth naming is the one that moves the pie.
    report.without_invoice.sort(
        key=lambda one: (not one.unsaid, -one.line.amount_due, one.line.operation_date, one.line.pk)
    )
    return report


def known_categories() -> list[str]:
    """Every category name that already exists anywhere, for the datalist.

    The same « free text with a datalist » as `StockType.category`: a list of
    categories to administer is one more thing to keep up to date, and a
    person looking for where the water bill went will type the word they
    already use. Offering the article categories and the suppliers of charges
    beside the typed ones is what keeps a hand-typed spending landing in the
    slice it belongs to instead of beside it.
    """
    names = set(StockType.objects.exclude(category="").values_list("category", flat=True))
    names |= set(Supplier.objects.filter(expenses_only=True).values_list("name", flat=True))
    # Typed on a DEBIT only. The same field names a credit on « Entrées
    # d'argent », and a word for money that came in (« Privatisation »,
    # « Apport ») offered here would file a spending under an income.
    names |= set(BankTransaction.objects.filter(amount__lt=0).exclude(category="").values_list("category", flat=True))
    names |= set(IgnoreRule.objects.exclude(category="").values_list("category", flat=True))
    return sorted(names, key=lambda name: (_reading_order(name), name))


def left_out_names(values) -> list[str]:
    """The categories `?sans=` leaves out of the pie, as the page reads them:
    each cleaned the way a category is stored (`clean_category`), the empty
    ones dropped, once each, in the order asked.

    They arrive from a query string, so a stale bookmark, a hand-typed URL
    and a tampered form all land here: a NUL, a name wider than the column
    or the same name twice is a view the page cannot draw, never a 500. Not
    checked against the categories that exist: a name with nothing in this
    window is still the reader's question over another one, and the page
    says it has nothing here rather than dropping it.
    """
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        name = clean_category(value)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def clean_category(value) -> str:
    """One category as it is stored: printable, trimmed, and never wider
    than its own column.

    It arrives from a text input, which is to say from anywhere, so both
    guards are somebody's 500. A string wider than the column SQLite stores
    without a word, and Django hands it back on every read afterwards. A NUL
    or another control character raises on the INSERT itself - « A string
    literal cannot contain NUL » - which is a traceback where a tampered
    form deserves a page.
    """
    printable = "".join(letter for letter in (value or "") if letter.isprintable() or letter.isspace())
    return " ".join(printable.split())[:CATEGORY_MAX]


def set_category(line: BankTransaction, value) -> str:
    """Say what a spending was for. Returns what was stored, « » for none.

    Deliberately NOT a decision in `reconcile`'s sense: it leaves
    `settled_by_hand` alone. What the money was for says nothing about
    whether its invoice is still to be found, and set here, naming a
    spending would quietly take its line out of the automatic pass for ever.
    """
    line.category = clean_category(value)
    line.save(update_fields=["category"])
    return line.category


def _lines(window: DateRange):
    """The window's debits, with everything `where_it_went` will ask of them
    already loaded - four queries for the page rather than one per debit."""
    lines = window.limit(BankTransaction.objects.filter(amount__lt=0), "operation_date")
    return lines.prefetch_related(
        Prefetch(
            "payments",
            queryset=InvoicePayment.objects.select_related("invoice__supplier").prefetch_related(
                lines_prefetch("invoice__lines")
            ),
        )
    )


def _read_invoice(invoice, read: dict) -> dict[str, Decimal]:
    parts = read.get(invoice.pk)
    if parts is None:
        parts = read[invoice.pk] = {}
        for place, money in where_it_went(invoice).items():
            name = _category_name(place)
            parts[name] = parts.get(name, ZERO) + money.ttc
    return parts


def _category_name(place: Place) -> str:
    """Where « Marges » puts an invoice's money, read as a spending category.

    A goods line's category is its ARTICLE's category - the group it is drawn
    under there - because a pie of one slice per article is a pie of two
    hundred slices. A charge's is its SUPPLIER rather than the « Charges »
    umbrella: the rent, the electricity and the phone are three different
    questions, and one slice holding all of them answers none of them.
    """
    if place.key == TO_CLASSIFY_KEY:
        return TO_CLASSIFY_NAME
    if place.group_key == CHARGES_KEY:
        return place.name
    return place.group_name


def kind_of(line: BankTransaction) -> str:
    """Which of the three ways this line is named, as it stands now.

    One rule with the page's own (`_own_category`), so « elle passe dans … »
    after a category is typed cannot name a list the line does not land in.
    Its two queries are on an action, never on a list.
    """
    rule = ignoring_rule(line.label, compile_rules(IgnoreRule.objects.filter(is_active=True)))
    return _own_category(line, rule)[1]


def _own_category(line: BankTransaction, rule) -> tuple[str, str]:
    """What the line itself says it was for, and who said it."""
    typed = line.category.strip()
    if typed:
        return typed, BY_HAND
    if rule is not None and rule.category.strip():
        return rule.category.strip(), BY_RULE
    return NO_CATEGORY, UNSAID


def _scaled(parts: dict[str, Decimal], due: Decimal, invoiced: Decimal) -> dict[str, Decimal]:
    """`parts` brought down to what the line actually paid.

    The remainder goes on the largest place, the way `_where_it_went` puts an
    invoice's own rounding there: three places scaled by a third come to a
    centime over, and a line that does not add up to its own debit is a
    column that does not add up to the statement.
    """
    scaled = {name: cents(amount * due / invoiced) for name, amount in parts.items()}
    largest = max(parts, key=lambda name: abs(parts[name]))
    scaled[largest] += due - sum(scaled.values(), ZERO)
    return scaled


def _share_out(categories: list[Category], drawn_total: Decimal) -> None:
    """Each drawn category's share of the pie, rounded to the figure the page
    prints, the remainder put back on the largest.

    An unrounded division prints three equal thirds as « 33.3 % » three
    times, and a reader adding the legend gets 99,9 % of a pie that is by
    construction the whole of what was drawn. Rounded here rather than in
    the template, because « Autres » is the sum of the shares it folds in
    and the legend has to agree with the table beside it. Same rule as
    `_scaled` above: round every part, give the rounding back to the largest.
    """
    drawn = [one for one in categories if one.drawn]
    if drawn_total <= 0 or not drawn:
        return
    for one in drawn:
        one.share = (one.amount / drawn_total * HUNDRED).quantize(SHARE_PLACES, rounding=ROUND_HALF_UP)
    largest = max(drawn, key=lambda one: one.amount)
    largest.share += HUNDRED - sum((one.share for one in drawn), ZERO)


def _ordered(categories) -> list[Category]:
    """« Sans catégorie » first - it is the work to do, and a table that
    buries it under thirty named rows is a table nobody acts on - then the
    biggest first, by name on a tie."""
    return [one for one in categories if one.name == NO_CATEGORY] + sorted(
        (one for one in categories if one.name != NO_CATEGORY),
        key=lambda one: (-one.amount, _reading_order(one.name), one.name),
    )


def _slices(categories: list[Category], drawn_total: Decimal) -> list[Slice]:
    """The pie: the categories with money in them that nobody left out,
    biggest first, and « Autres » holding only what is thinner than
    `SMALLEST_SLICE`.

    Every category big enough to see gets its own wedge, however many that
    is - a named wedge is something a reader can act on, and « Autres » is
    not. « Sans catégorie » keeps its own whatever its size: it is the one
    the page exists to get rid of. Left out, it is out like any other - the
    owner's choice, which the page names above the pie; a left-out category
    is never folded into « Autres » either, since « Autres » is a share of
    what IS drawn.
    """
    if drawn_total <= 0:
        return []
    drawable = [one for one in categories if one.drawn]
    first = [one for one in drawable if one.name == NO_CATEGORY]
    rest = [one for one in drawable if one.name != NO_CATEGORY]
    tail = [one for one in rest if one.share < SMALLEST_SLICE]
    rest = [one for one in rest if one.share >= SMALLEST_SLICE]
    # One category alone below the bar is named, not hidden: « Autres
    # (1 catégorie) » is a wedge that says strictly less than the name it
    # replaced, at the same size.
    if len(tail) == 1:
        rest, tail = rest + tail, []
    pieces = [
        Slice(one.name, one.amount, one.share, PIE_COLORS[index % len(PIE_COLORS)])
        for index, one in enumerate(first + rest)
    ]
    if tail:
        # The shares it folds in, added up rather than divided again: the
        # legend and the table beside it are then the same figures, and the
        # wedges still come to a hundred per cent exactly.
        pieces.append(
            Slice(
                OTHERS,
                sum((one.amount for one in tail), ZERO),
                sum((one.share for one in tail), ZERO),
                PIE_COLORS[len(pieces) % len(PIE_COLORS)],
                held=len(tail),
            )
        )
    # A pie closes on itself, so the last wedge touches the first: past the
    # palette's length the cycle would draw them the same colour, and two
    # neighbours of one colour read as one wedge. Nudged rather than
    # re-cycled, so every other slice keeps the colour its legend shows.
    if len(pieces) > len(PIE_COLORS) and pieces[-1].color == pieces[0].color:
        pieces[-1] = replace(pieces[-1], color=PIE_COLORS[-2])
    return pieces


def _reading_order(name: str) -> str:
    """`name` as an alphabetical list files it: accents and case aside."""
    decomposed = unicodedata.normalize("NFD", name)
    return "".join(letter for letter in decomposed if not unicodedata.combining(letter)).casefold()
