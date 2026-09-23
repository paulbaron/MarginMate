"""The three margins, over one « du … au … » window.

The owner asked for three figures and they answer three different questions,
so nothing here blends them:

* **la marge réelle** - everything that came in against everything that was
  INVOICED over the window, goods and charges alike. It is the only one that
  answers « ai-je gagné de l'argent ce mois-ci ».
* **la marge produits** - everything that came in against what the recipes
  sold actually consumed, plus the articles flagged « compter dans la marge
  produits » (the paper towels: no recipe eats them, so what was bought of
  them over the window is the only measure there is). It answers « est-ce que
  je vends assez cher ».
* **les marges par catégorie**, on both dimensions the till already stores:
  `PosProduct.category` (Bières, Cocktails, Planches…) and
  `PosProduct.typology` (the owner's « food, drinks »).

Four decisions run through all of it.

**HT is the headline, TTC is beside it.** The till takes TTC and the
invoices charge HT; VAT is not the bar's money, and comparing the two raw
would overstate every margin on this page. Every amount here is a `Money`,
which is both, and every margin is worked out on the HT.

**A recipe with variations costs a RANGE** (« vodka OU gin »), so a margin
built on it is a range too. Read off the per-group extremes by
`Recipe.summary` - never by enumerating variations, which are the cartesian
product of the choice groups and reach a million on twenty either/ors.

**Coverage is not optional.** Only a part of what the till sells has a
recipe behind it - the food and the coffee have none - and revenue with no
cost prints a 100 % margin. So every unit is counted twice: once as sold,
once as costed, and `coverage`, `revenue_uncosted` and `top_uncosted` say
what the gap is. A slice with no costed unit at all has NO margin here; it
returns None rather than a number that reads as profit.

**A gap is said, never absorbed.** A day whose money was never read
(`revenue_read`), a TTC whose rate the export did not state, an invoice with
no date, a sale typed by hand with no price on it: each is counted on its
own field so the page can name it. Taken as zero, every one of them reads as
money the bar lost.

**The real margin can be read « sans » something** - without the equipment,
without the charges - and is then drawn TWICE, the global one untouched and
the second beside it. Every invoiced euro has exactly one place (a supplier
of charges, an article, or « à classer »), so leaving a place out takes that
money out and nothing else; the revenue never moves, because a purchase
belongs to no till category. See `SpendGroup` and `_where_it_went`.

**The products margin's own box is read here too**, every article under its
category (`CountableCategory`), with what was bought of it over the window -
counted by the same code that counts the ticked ones, so what the page says
a box adds is what the margin moves by. See `_read_the_flagged_articles`.

Pure: a `DateRange` and the keys left out in, a `MarginReport` out. No
request, no template.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from django.db.models import Prefetch, Sum

from common import DateRange, is_id
from inventory.models import MovementKind, StockMovement, StockType
from invoices.models import Invoice, InvoiceLine, Supplier
from recipes.models import (
    PosProductDailyQuantity,
    Recipe,
    RecipeIngredient,
    RecipeSale,
    SaleDocumentLine,
    variation_scope,
)

ZERO = Decimal("0")
CENTS = Decimal("0.01")
HUNDRED = Decimal("100")

#: What the L'Addition import writes on RecipeSale (recipes/tasks.py). Any
#: other source is a sale somebody typed in, which carries no price anywhere.
TILL_SOURCE = "laddition"

#: How many uncosted till products the report names. The list exists to be
#: acted on - a recipe written, a product ignored - and a page that lists two
#: hundred of them is a page nobody works through.
TOP_UNCOSTED = 12

#: What a till product with a BLANK category or typology is filed under.
#: Not « Sans catégorie »: the till already prints a category of its own
#: called « _Sans catégorie » (and a typology called « N/D »), and two rows
#: an underscore apart, meaning two different things, is a page nobody can
#: read. These say « the export said nothing here », which is what they are.
NO_CATEGORY = "Catégorie non renseignée"
NO_TYPOLOGY = "Typologie non renseignée"

NO_RECIPE = "aucune recette"
RECIPE_WITHOUT_COST = "recette sans coût"
#: A recipe whose cost is only PARTLY known - an article in it has never been
#: invoiced, so it prices at 0 and the recipe comes out cheaper than it is.
#: Read as costed, that gap prints as margin at « 100 % chiffré », which is
#: the one thing this page exists not to do.
INGREDIENT_WITHOUT_PRICE = "ingrédient sans prix"

# -- « sans … »: the keys of what the second real margin leaves out ----------

#: What `?sans=` carries, one key per thing that can be left out. Text rather
#: than bare ids because an article category is not a row anywhere: it is a
#: free string on the article (« Matériel », one of them blank), so its name
#: IS its key, accents, spaces and all - it goes out through `urlencode` and
#: must come back exactly as it went. The prefixes keep a category called
#: « 12 » from ever reading as article 12.
CHARGES_KEY = "charges"
TO_CLASSIFY_KEY = "a-classer"
SUPPLIER_PREFIX = "fournisseur:"
CATEGORY_PREFIX = "categorie:"
ARTICLE_PREFIX = "article:"

CHARGES_NAME = "Charges"
#: The goods lines no article claims yet - the same « à classer » as the
#: review queue of « Produits & charges ».
TO_CLASSIFY_NAME = "Sans article (à classer)"


def supplier_key(supplier_id: int) -> str:
    """One supplier of charges."""
    return f"{SUPPLIER_PREFIX}{supplier_id}"


def category_key(name: str) -> str:
    """One article category, by its name as stored - the blank one is
    `categorie:` and nothing after it."""
    return f"{CATEGORY_PREFIX}{name}"


def article_key(article_id: int) -> str:
    """One article (a `StockType`)."""
    return f"{ARTICLE_PREFIX}{article_id}"


def _cents(value: Decimal) -> Decimal:
    """Money to the cent, half away from zero - what the rest of the app
    rounds with (InvoiceLine, the charge reading)."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _percent(part: Decimal, whole: Decimal) -> Decimal | None:
    """`part` as a percentage of `whole`, or None when there is no whole to
    speak of.

    Two denominators are refused rather than divided:

    * **zero** - nothing over nothing is not 0 %; printed under an empty
      window it says the bar lost everything it took;
    * **negative** - a window that only refunded has -8,75 € of margin on
      -7,50 € of revenue, which works out to +116 %. A loss shown as a gain
      is worse than no figure at all.
    """
    if whole <= 0:
        return None
    return part / whole * HUNDRED


@dataclass(frozen=True)
class Money:
    """One amount, both ways. Never a float - see CLAUDE.md, every number on
    the invoice → cost → margin path is a Decimal."""

    ht: Decimal = ZERO
    ttc: Decimal = ZERO

    def __add__(self, other: "Money") -> "Money":
        return Money(self.ht + other.ht, self.ttc + other.ttc)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.ht - other.ht, self.ttc - other.ttc)


@dataclass
class Slice:
    """One category, one typology, or the whole - what it took, what it
    cost, and how much of it is actually costed.

    `cost_ht_low` is every recipe sold at its cheapest variation and
    `cost_ht_high` at its dearest, so the margins swap over: the smallest
    margin comes from the dearest cost.
    """

    name: str
    revenue: Money = Money()
    cost_ht_low: Decimal = ZERO
    cost_ht_high: Decimal = ZERO
    units: int = 0
    costed_units: int = 0
    #: The part of `revenue` whose till product has no costed recipe. Stated
    #: beside the margin: a slice 80 % of whose money is uncosted has a
    #: margin about the other 20 % and nothing else.
    revenue_uncosted: Money = Money()
    #: The part of `revenue.ttc` the export stated no VAT rate for, and which
    #: is therefore in no `revenue.ht`. Its cost IS counted, so the slice
    #: prints a cost against a revenue of 0,00 € - a loss - and only this
    #: says why.
    revenue_without_rate_ttc: Decimal = ZERO

    @property
    def is_costed(self) -> bool:
        """Whether anything at all behind this slice has a known cost. False
        means there is no margin to state - not a margin of 100 %."""
        return self.costed_units > 0

    @property
    def margin_ht_low(self) -> Decimal | None:
        return None if not self.is_costed else self.revenue.ht - self.cost_ht_high

    @property
    def margin_ht_high(self) -> Decimal | None:
        return None if not self.is_costed else self.revenue.ht - self.cost_ht_low

    @property
    def margin_percent_low(self) -> Decimal | None:
        margin = self.margin_ht_low
        return None if margin is None else _percent(margin, self.revenue.ht)

    @property
    def margin_percent_high(self) -> Decimal | None:
        margin = self.margin_ht_high
        return None if margin is None else _percent(margin, self.revenue.ht)

    @property
    def coverage(self) -> Decimal | None:
        """The share of what was rung up that has a cost behind it, 0 to 1.
        None when nothing was rung up: there is no share of nothing."""
        if not self.units:
            return None
        return Decimal(self.costed_units) / Decimal(self.units)

    @property
    def revenue_coverage(self) -> Decimal | None:
        """The same share in MONEY, which is what the margin beside it is a
        percentage of - the page's headline coverage asks the same question,
        and two figures under one label were 33 points apart. None when
        there is no positive revenue to take a share of."""
        if self.revenue.ht <= 0:
            return None
        return (self.revenue.ht - self.revenue_uncosted.ht) / self.revenue.ht


@dataclass
class UncostedProduct:
    """A till product whose sales have no cost behind them - the row the page
    turns into an action: write the recipe, or say it will never have one."""

    name: str
    units: int = 0
    revenue: Money = Money()
    reason: str = NO_RECIPE
    #: An « ignoré » till product (the coffee, the food) is uncosted on
    #: purpose. It still weighs on the coverage - its revenue is real and its
    #: cost is not - so it is listed, marked, rather than hidden.
    ignored: bool = False


@dataclass
class ExtraProduct:
    """An article flagged « compter dans la marge produits », and what was
    bought of it over the window."""

    name: str
    ht: Decimal = ZERO
    ttc: Decimal = ZERO


@dataclass
class CountableArticle:
    """One article of « Articles comptés dans la marge produits »: its box,
    and what ticking it would add to the products margin's cost."""

    pk: int
    name: str
    #: `StockType.count_in_products_margin`, as stored.
    ticked: bool = False
    #: What was bought of it over the window - exactly what the products
    #: margin counts for it once ticked, since both are read by
    #: `_bought_over`. Signed: a deposit given back takes it down.
    bought: Money = Money()
    #: Whether anything at all was bought of it over the window. « 0,00 € »
    #: and « rien acheté » are two different answers: a purchase and a
    #: return can net to nothing.
    was_bought: bool = False
    #: A recipe uses it, so what it costs is already counted as the recipe
    #: consumes it. Ticked as well, it is paid for twice.
    in_recipe: bool = False

    @property
    def counted_twice(self) -> bool:
        return self.ticked and self.in_recipe


@dataclass
class CountableCategory:
    """One article category on that panel - the unit the owner ticks by -
    and its articles."""

    #: As stored: what the panel's form posts back, the blank one included.
    name: str
    articles: list[CountableArticle] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or NO_CATEGORY

    @property
    def ticked(self) -> int:
        return sum(1 for article in self.articles if article.ticked)

    @property
    def all_ticked(self) -> bool:
        return self.ticked == len(self.articles)

    @property
    def none_ticked(self) -> bool:
        return self.ticked == 0

    @property
    def state(self) -> str:
        """« aucun », « 3 sur 70 », « tous » - in words, because a box drawn
        half-ticked says nothing of how many. An article classified into a
        category after it was ticked whole arrives unticked, and « 31 sur
        32 » is how that is seen."""
        if self.none_ticked:
            return "aucun"
        if self.all_ticked:
            return "tous"
        return f"{self.ticked} sur {len(self.articles)}"

    @property
    def bought_ht(self) -> Decimal:
        """What ticking every one of them would add, HT."""
        return sum((article.bought.ht for article in self.articles), start=ZERO)

    @property
    def counted_ht(self) -> Decimal:
        """What its ticked articles already add, HT."""
        return sum((article.bought.ht for article in self.articles if article.ticked), start=ZERO)

    @property
    def in_recipes(self) -> int:
        return sum(1 for article in self.articles if article.in_recipe)

    @property
    def counted_twice(self) -> int:
        return sum(1 for article in self.articles if article.counted_twice)

    @property
    def opened(self) -> bool:
        """Drawn unfolded when it holds an article counted twice: the mark
        is where the tick is, and folded both would be out of sight."""
        return self.counted_twice > 0


@dataclass
class SpendPart:
    """One place an invoiced euro lands, and the smallest thing that can be
    left out: one supplier of charges, one article - or « à classer », the
    goods lines no article claims yet, which is its own group too."""

    key: str
    name: str
    money: Money = Money()
    #: Its share of everything invoiced over the window, 0-100. None when
    #: nothing positive was invoiced: there is no share of nothing.
    share: Decimal | None = None
    #: Its OWN box is unticked - its key is in the selection.
    unticked: bool = False
    #: Its money is out of the second margin, by its own box or by its
    #: group's. The two differ on purpose: an article keeps its own box
    #: under a category left out, so ticking the category back brings it
    #: back without a second click.
    left_out: bool = False


@dataclass
class SpendGroup:
    """A line of « Ce qui a été facturé »: « Charges » and its suppliers, one
    article category and its articles, or « à classer » on its own."""

    key: str
    name: str
    money: Money = Money()
    share: Decimal | None = None
    members: list[SpendPart] = field(default_factory=list)
    unticked: bool = False

    @property
    def member_noun(self) -> str:
        """What its rows are: « 3 fournisseurs », « 70 articles »."""
        return "fournisseur" if self.key == CHARGES_KEY else "article"

    @property
    def opened(self) -> bool:
        """Drawn unfolded when a box inside it is unticked: folded, the only
        box saying something is out would be out of sight, under a category
        whose own box, still ticked, says nothing is."""
        return any(part.unticked for part in self.members)


@dataclass
class Exclusion:
    """One thing the second margin leaves out, named in words."""

    key: str
    name: str
    #: What it takes out over the window, on its own. 0,00 € is an answer:
    #: « Consignes » left out over a month none were bought.
    money: Money = Money()
    #: Something of it WAS invoiced over the window. An article bought and
    #: given back takes out 0,00 € too, and « rien de facturé » under it
    #: would be false: only a key with no line at all is said that way.
    invoiced: bool = False
    #: The group left out with it that already holds it - an article inside
    #: « Matériel » left out too, a supplier inside « Charges ». Its money is
    #: taken out once, and the page names only the group.
    within: str | None = None
    #: That group's key: « remettre » on the group puts this back with it,
    #: since the page names the group alone.
    within_key: str | None = None


@dataclass
class MarginReport:
    window: DateRange
    #: What the till rang up (PosProductDailyQuantity), days inside the
    #: window, both ends included.
    revenue_till: Money = Money()
    #: Sales made off the till (SaleDocument), dated inside it.
    revenue_documents: Money = Money()
    revenue: Money = Money()
    #: The part of `revenue.ttc` with no HT behind it: a till line whose rate
    #: the export did not state, and a sale document line on an article,
    #: which carries no rate at all. Never converted at an assumed rate.
    revenue_without_rate_ttc: Decimal = ZERO

    spend_goods: Money = Money()
    spend_charges: Money = Money()
    spend: Money = Money()
    #: Invoices with no date at all. With a window they are in none of it and
    #: `undated_in_spend` is False; over all time there is no window to be
    #: outside of, so they are part of `spend` and it is True. Counted either
    #: way, because a spending nobody can place is the page's to declare.
    undated_invoices: int = 0
    undated_spend: Money = Money()
    undated_in_spend: bool = False

    #: How many invoices `spend` is made of. « Facturé 0,00 € » and
    #: « facturé 0,00 €, 0 facture » are two different months: one bought
    #: nothing, the other has not been scanned yet - and without this the
    #: second reads as a 100 % real margin.
    invoice_count: int = 0

    #: Where `spend` went, place by place: Charges first, then every article
    #: category, biggest first, then « à classer ». With nothing left out
    #: they add up to `spend` to the cent, HT and TTC - by construction, see
    #: `_where_it_went`.
    spend_groups: list[SpendGroup] = field(default_factory=list)
    #: What the second real margin leaves out, in the order it was asked,
    #: keys the page did not know already dropped. Empty: there is no
    #: second margin, only the global one.
    exclusions: list[Exclusion] = field(default_factory=list)
    #: What those take out of `spend`, each place counted once.
    spend_left_out: Money = Money()

    #: What the recipes sold consumed, HT: every one at its cheapest
    #: variation, and every one at its dearest.
    cogs_low: Decimal = ZERO
    cogs_high: Decimal = ZERO
    #: Servings costed off the till (a sale document's recipe lines). They
    #: are in the cogs and in no unit count, so a note dividing the cogs by
    #: `costed_units` alone prices a serving at several times what it costs.
    document_costed_units: Decimal = ZERO
    #: The flagged articles, bought over the window. One number, not a range:
    #: an invoice charges what it charges.
    extra_products_ht: Decimal = ZERO
    extra_products_ttc: Decimal = ZERO
    extra_products: list[ExtraProduct] = field(default_factory=list)
    #: How many articles are ticked at all. « Rien de coché » and « coché,
    #: rien acheté sur la période » print the same empty table otherwise.
    flagged_articles: int = 0
    #: Articles ticked AND used in a recipe, by name. Their cost is counted
    #: twice - once as what the recipe consumed, once as what was bought -
    #: and the page has no way to know it unless this is asked for.
    flagged_in_recipes: list[str] = field(default_factory=list)
    #: Every article, by category (alphabetical, the blank one last), with
    #: its box and what ticking it would add - the panel the owner ticks
    #: from, a whole category at a time.
    countable: list[CountableCategory] = field(default_factory=list)

    by_category: list[Slice] = field(default_factory=list)
    by_typology: list[Slice] = field(default_factory=list)

    units: int = 0
    costed_units: int = 0
    revenue_uncosted: Money = Money()
    #: Days inside the window holding at least one till row whose money was
    #: never read. Their units and their cost are counted and their revenue
    #: is not, so the margin reads LOW until the backfill has run - which is
    #: the safe direction, and this is what lets the page say why.
    unread_days: int = 0
    unread_units: int = 0
    #: Units sold on a recipe by hand (RecipeSale, any source but the till).
    #: In no revenue and in no cost here: nothing anywhere says what they
    #: were sold for, and costing them alone would read as a loss.
    hand_typed_units: int = 0
    top_uncosted: list[UncostedProduct] = field(default_factory=list)
    #: How many till products `top_uncosted` is a top OF, and what they took
    #: in all. Twelve rows on a page with many more behind them is a list a reader
    #: works to the end believing the hole is closed.
    uncosted_products: int = 0
    uncosted_revenue: Money = Money()

    # -- la marge réelle --------------------------------------------------

    @property
    def real_margin_ht(self) -> Decimal:
        return self.revenue.ht - self.spend.ht

    @property
    def real_margin_ttc(self) -> Decimal:
        return self.revenue.ttc - self.spend.ttc

    @property
    def real_margin_percent(self) -> Decimal | None:
        return _percent(self.real_margin_ht, self.revenue.ht)

    # -- la marge réelle, sans ce qu'on a décoché ----------------------------
    # The same revenue, always: what is left out is a COST. A purchase
    # belongs to no till category, so there is no sale to take out with it.

    @property
    def spend_kept(self) -> Money:
        return self.spend - self.spend_left_out

    @property
    def kept_margin_ht(self) -> Decimal:
        return self.revenue.ht - self.spend_kept.ht

    @property
    def kept_margin_ttc(self) -> Decimal:
        return self.revenue.ttc - self.spend_kept.ttc

    @property
    def kept_margin_percent(self) -> Decimal | None:
        return _percent(self.kept_margin_ht, self.revenue.ht)

    # -- la marge produits ------------------------------------------------

    @property
    def is_costed(self) -> bool:
        """Whether any of what was sold has a cost behind it at all. False
        means the products margin cannot be stated - every figure it would
        print is the revenue itself."""
        return self.cogs_high > 0

    @property
    def products_margin_ht_low(self) -> Decimal | None:
        if not self.is_costed:
            return None
        return self.revenue.ht - self.cogs_high - self.extra_products_ht

    @property
    def products_margin_ht_high(self) -> Decimal | None:
        if not self.is_costed:
            return None
        return self.revenue.ht - self.cogs_low - self.extra_products_ht

    @property
    def products_margin_percent_low(self) -> Decimal | None:
        margin = self.products_margin_ht_low
        return None if margin is None else _percent(margin, self.revenue.ht)

    @property
    def products_margin_percent_high(self) -> Decimal | None:
        margin = self.products_margin_ht_high
        return None if margin is None else _percent(margin, self.revenue.ht)

    # -- what the margins are worth ---------------------------------------

    @property
    def coverage(self) -> Decimal | None:
        """The share of the till's units that has a cost behind it, 0 to 1 -
        the figure every recipe-based number on this page has to be read
        against. None when the till rang nothing up."""
        if not self.units:
            return None
        return Decimal(self.costed_units) / Decimal(self.units)

    @property
    def revenue_coverage(self) -> Decimal | None:
        """The same thing in money rather than in units, over the till AND
        the sale documents: a category selling four planches at 18 € weighs
        far more here than in the unit count."""
        if self.revenue.ht <= 0:
            return None
        return (self.revenue.ht - self.revenue_uncosted.ht) / self.revenue.ht


def margins_for(window: DateRange, left_out: Iterable[str] = ()) -> MarginReport:
    """The three margins over `window`, both ends included - and the real
    one a second time without the places `left_out` names (`?sans=`, keys
    made by `supplier_key` and its siblings; one the page does not know is
    ignored).

    One pass over the till's days, one over the sale documents, one over the
    recipes they sold, one over the invoices and their lines, three for the
    articles (every article, every recipe line naming one, every purchase)
    and at most one per KIND of key left out.
    Nothing here is per-row: see `_recipe_costs` for why the recipes are the
    part that has to be watched.
    """
    report = MarginReport(window=window)

    till_rows = _till_rows(window)
    document_lines = _document_lines(window)
    sold_recipes = {row[_RECIPE_OF_ROW] for row in till_rows if row[_RECIPE_OF_ROW]}
    sold_recipes |= {line.recipe_id for line in document_lines if line.recipe_id}
    costs, why_not = _recipe_costs(sold_recipes)

    _read_the_till(report, till_rows, costs, why_not)
    _read_the_documents(report, document_lines, costs)
    _read_the_invoices(report, window)
    _leave_out(report, _resolve(left_out))
    _read_the_flagged_articles(report, window)

    report.revenue = report.revenue_till + report.revenue_documents
    report.hand_typed_units = (
        window.limit(RecipeSale.objects.exclude(source=TILL_SOURCE), "sold_on").aggregate(
            total=Sum("quantity")
        )["total"]
        or 0
    )
    return report


# -- the till ---------------------------------------------------------------

#: The columns one pass over the till's days needs. `product__recipe_id` and
#: the two TAG_ columns come along so nothing below asks a PosProduct row a
#: question of its own - that is one query per row, and this table holds
#: 16 000 of them.
_TILL_COLUMNS = (
    "product_id",
    "product__name",
    "product__category",
    "product__typology",
    "product__recipe_id",
    "product__ignored",
    "sold_on",
    "quantity",
    "revenue_ttc",
    "revenue_ht",
    "revenue_without_rate_ttc",
    "revenue_read",
)

#: Where the recipe sits in a row of `_TILL_COLUMNS`, read by name so adding
#: a column cannot silently move it.
_RECIPE_OF_ROW = _TILL_COLUMNS.index("product__recipe_id")


def _till_rows(window: DateRange) -> list[tuple]:
    return list(
        window.limit(PosProductDailyQuantity.objects.all(), "sold_on").values_list(*_TILL_COLUMNS)
    )


def _document_lines(window: DateRange) -> list:
    return list(
        window.limit(SaleDocumentLine.objects.all(), "document__sold_on")
        .select_related("recipe", "stock_type")
    )


def _recipe_costs(recipe_ids: set[int]) -> tuple[dict[int, tuple[Decimal, Decimal]], dict[int, str]]:
    """({recipe id: (cheapest, dearest) HT per SERVING}, {recipe id: why
    not}) for the recipes that were sold.

    Two rules from CLAUDE.md meet here, and this page breaks both if it is
    written the obvious way. `Recipe.summary` is linear in INGREDIENTS: a
    recipe's variations are the cartesian product of its choice groups, so
    twenty either/ors is a million of them and enumerating is hopeless. And
    `Recipe.choice_groups()` builds its own queryset, so a prefetch at the
    call site buys nothing unless the ingredients are handed to it - hence
    `summary(list(recipe.ingredients.all()))` inside a `variation_scope()`,
    which is what keeps the sub-recipes from being read once per question
    asked. Written without either, the two other pages of this app reached
    290 queries.

    Three recipes are left OUT rather than costed, each for the same reason:
    a cost that is only partly known reads as margin.

    * one whose dearest variation costs nothing - every article in it has
      been invoiced never (`StockType.current_unit_cost_ht` is 0 with no
      movement behind it);
    * one where only SOME article has never been invoiced: it prices at 0
      and the others do not, so the recipe comes out cheaper than it is and
      « 100 % chiffré » beside it is false. All or nothing, as above;
    * one that yields nothing - there is no per-serving cost to divide out.

    And what is costed is the cost of **one serving**, not of one whole
    preparation. `summary` prices a full run of the recipe; a syrup made ten
    glasses at a time costs its batch there. The variance engine has divided
    by `yield_quantity` since it was written (inventory/variance.py), so a
    cogs that does not puts two pages of this app a factor of ten apart over
    the same sale. Note the direction is not always the flattering one: the
    validator allows a yield below 1, which costs MORE per serving.
    """
    if not recipe_ids:
        return {}, {}
    recipes = Recipe.objects.filter(pk__in=recipe_ids).prefetch_related(
        "ingredients__stock_type__movements", "ingredients__sub_recipe"
    )
    costs: dict[int, tuple[Decimal, Decimal]] = {}
    uncosted: dict[int, str] = {}
    with variation_scope():
        for recipe in recipes:
            ingredients = list(recipe.ingredients.all())
            cost_range = recipe.summary(ingredients)["cost_range"]
            if cost_range is None or cost_range[1] <= 0 or not recipe.yield_quantity:
                uncosted[recipe.pk] = RECIPE_WITHOUT_COST
                continue
            if not _every_ingredient_priced(recipe, ingredients, set()):
                uncosted[recipe.pk] = INGREDIENT_WITHOUT_PRICE
                continue
            costs[recipe.pk] = (
                cost_range[0] / recipe.yield_quantity,
                cost_range[1] / recipe.yield_quantity,
            )
    return costs, uncosted


def _every_ingredient_priced(recipe: Recipe, ingredients: list, being_read: set[int]) -> bool:
    """Whether every article this recipe reaches has a price behind it, its
    sub-recipes' included.

    An alternative counts like anything else: a group offering « vodka OU
    gin » where the gin has never been invoiced prices its cheapest
    variation at 0, so the range this page prints has a made-up end.

    A sub-recipe's own ingredients come through `choice_groups()`, which
    inside the caller's `variation_scope` is served from the memo
    `summary()` has already filled - asked for them directly
    (`sub.ingredients.all()`) it is one query per recipe REACHING that
    sub-recipe, which is the N+1 CLAUDE.md has a section about.
    """
    if recipe.pk in being_read:
        return False  # a cycle - Recipe.unit_cost_bounds prices one at 0 too
    if not ingredients:
        return False
    being_read.add(recipe.pk)
    try:
        for ingredient in ingredients:
            if ingredient.sub_recipe_id:
                sub = ingredient.sub_recipe
                inside = [line for group in sub.choice_groups() for line in group]
                if not _every_ingredient_priced(sub, inside, being_read):
                    return False
            elif ingredient.stock_type.current_unit_cost_ht <= 0:
                return False
        return True
    finally:
        being_read.discard(recipe.pk)


def _read_the_till(report: MarginReport, rows: list[tuple], costs: dict, why_not: dict) -> None:
    """The till's revenue, units and both category dimensions, in one pass.

    The report's own totals are then read back off `by_category` rather than
    accumulated beside it: one definition, so a headline can never disagree
    with the list under it (CLAUDE.md, « a badge and the list it stands for
    share one definition »).
    """
    categories: dict[str, Slice] = {}
    typologies: dict[str, Slice] = {}
    uncosted: dict[int, UncostedProduct] = {}
    unread_days: set[date] = set()

    for (
        product_id,
        name,
        category,
        typology,
        recipe_id,
        ignored,
        sold_on,
        quantity,
        revenue_ttc,
        revenue_ht,
        without_rate_ttc,
        revenue_read,
    ) in rows:
        cost_range = costs.get(recipe_id) if recipe_id else None
        # A day nobody has read the money of holds 0,00 € it never took.
        # Its units and its cost still count - the stock did leave the shelf
        # - and `unread_days` is what stops the page reading the result as a
        # month of pure loss.
        money = Money(revenue_ht, revenue_ttc) if revenue_read else Money()
        if not revenue_read:
            unread_days.add(sold_on)
            report.unread_units += quantity
        else:
            report.revenue_without_rate_ttc += without_rate_ttc

        for holder, key, fallback in (
            (categories, category, NO_CATEGORY),
            (typologies, typology, NO_TYPOLOGY),
        ):
            slice_ = holder.setdefault(key or fallback, Slice(name=key or fallback))
            slice_.units += quantity
            slice_.revenue += money
            if revenue_read:
                slice_.revenue_without_rate_ttc += without_rate_ttc
            if cost_range is None:
                slice_.revenue_uncosted += money
                continue
            slice_.costed_units += quantity
            slice_.cost_ht_low += cost_range[0] * quantity
            slice_.cost_ht_high += cost_range[1] * quantity

        if cost_range is None:
            entry = uncosted.setdefault(
                product_id,
                UncostedProduct(
                    name=name,
                    reason=NO_RECIPE if recipe_id is None else why_not.get(recipe_id, RECIPE_WITHOUT_COST),
                    ignored=ignored,
                ),
            )
            entry.units += quantity
            entry.revenue += money

    report.by_category = _ordered(categories)
    report.by_typology = _ordered(typologies)
    report.unread_days = len(unread_days)
    for slice_ in report.by_category:
        report.revenue_till += slice_.revenue
        report.units += slice_.units
        report.costed_units += slice_.costed_units
        report.cogs_low += slice_.cost_ht_low
        report.cogs_high += slice_.cost_ht_high
        report.revenue_uncosted += slice_.revenue_uncosted
    report.uncosted_products = len(uncosted)
    report.uncosted_revenue = sum(
        (entry.revenue for entry in uncosted.values()), start=Money()
    )
    report.top_uncosted = sorted(
        uncosted.values(),
        # By the money first, since that is what the margin is short of; a
        # product whose days are all unread has none to sort by and falls
        # back on how many of it went out.
        key=lambda entry: (-entry.revenue.ht, -entry.units, entry.name),
    )[:TOP_UNCOSTED]


def _ordered(slices: dict[str, Slice]) -> list[Slice]:
    """Biggest earner first, then the busiest, then by name - a stable order
    whatever the data, so two readings of one window list alike."""
    return sorted(slices.values(), key=lambda slice_: (-slice_.revenue.ht, -slice_.units, slice_.name))


# -- sales made off the till -------------------------------------------------


def _read_the_documents(report: MarginReport, lines: list, costs: dict) -> None:
    """SaleDocument's own lines: a tab settled by hand, a private event.

    Their HT is worked out per VAT RATE over the whole window rather than per
    line, the same rule the till import follows: a receipt rounds per line,
    a VAT return does not, and three coupes at 3,50 € are 9,55 € HT, not
    9,54 €.

    **An article sold as itself is income no margin can be stated on.** It
    carries no VAT rate anywhere - a recipe has one, an article does not - so
    its money stays TTC, is declared in `revenue_without_rate_ttc` and never
    reaches `revenue.ht`. Its purchase price is therefore left out of the
    cogs too: taken in, that cost comes off an HT its revenue never joined,
    and two bottles bought at 3 € and sold at 10 € each printed a products
    margin of **-6,00 €** - a profitable sale shown as a loss. Both sides out
    or neither; the amount is named at the foot of the page instead.

    They hold no till category, so they are in no slice; they are in
    `revenue_documents` and in the cogs, and the page says so.
    """
    # Keyed by the recipe's own `_vat_divisor` rather than by its rate: a
    # rate of exactly -1 makes (1 + rate) zero, and dividing by it here is
    # the same ZeroDivisionError the model already guards every page that
    # merely LISTS a recipe against (a row written by a raw update, past the
    # form's validators). One definition, and this page cannot be the one
    # that reintroduces it.
    ttc_by_rate: dict[Decimal, Decimal] = {}
    uncosted_ttc_by_rate: dict[Decimal, Decimal] = {}
    without_rate = ZERO
    uncosted_without_rate = ZERO

    for line in lines:
        total_ttc = line.total_ttc
        if line.recipe_id:
            divisor = line.recipe._vat_divisor
            ttc_by_rate[divisor] = ttc_by_rate.get(divisor, ZERO) + total_ttc
            cost_range = costs.get(line.recipe_id)
            if cost_range is None:
                uncosted_ttc_by_rate[divisor] = uncosted_ttc_by_rate.get(divisor, ZERO) + total_ttc
                continue
            report.cogs_low += cost_range[0] * line.quantity
            report.cogs_high += cost_range[1] * line.quantity
            report.document_costed_units += line.quantity
            continue

        # An article sold as itself: its money has no HT, so its cost has
        # nowhere to be taken off. Uncosted revenue, both ways.
        without_rate += total_ttc
        uncosted_without_rate += total_ttc

    report.revenue_documents = Money(_ht_per_rate(ttc_by_rate), _total(ttc_by_rate) + without_rate)
    report.revenue_uncosted += Money(
        _ht_per_rate(uncosted_ttc_by_rate), _total(uncosted_ttc_by_rate) + uncosted_without_rate
    )
    report.revenue_without_rate_ttc += without_rate


def _total(by_rate: dict[Decimal, Decimal]) -> Decimal:
    return sum(by_rate.values(), start=ZERO)


def _ht_per_rate(by_rate: dict[Decimal, Decimal]) -> Decimal:
    """The TTC accumulated per rate, taken back to HT once per rate and
    rounded there. Keyed by `Recipe._vat_divisor`, which is never zero."""
    return sum((_cents(total / divisor) for divisor, total in by_rate.items()), start=ZERO)


# -- what was invoiced -------------------------------------------------------


def _read_the_invoices(report: MarginReport, window: DateRange) -> None:
    """« Toutes les dépenses »: every invoice DATED in the window, goods and
    charges told apart by `Supplier.expenses_only`.

    What was invoiced, not what was paid and not what was consumed - which is
    what the owner asked the real margin for. Each document is added at its
    own two totals, rounded to the cent BEFORE they are summed: a page lists
    the documents it foots, and twelve bills printed at 35,99 € must not add
    up to 431,86 € under twelve lines saying 35,99 €.

    The same pass puts each document's money in its places
    (`_where_it_went`) - the breakdown the page's selector is drawn from,
    read off the very totals `spend` is, so the two cannot disagree.
    """
    groups: dict[str, SpendGroup] = {}
    parts: dict[str, SpendPart] = {}
    for invoice in _invoices(window.limit(Invoice.objects.all(), "invoice_date")):
        # Each total read once: `total_ttc` walks the lines.
        total_ttc = invoice.total_ttc
        whole = Money(_cents(invoice.total_ht), _cents(total_ttc))
        target = "spend_charges" if invoice.supplier.expenses_only else "spend_goods"
        setattr(report, target, getattr(report, target) + whole)
        # How many documents the figure is made of: a month nobody has
        # scanned yet spends 0,00 € exactly like a month that bought
        # nothing, and reads as a 100 % real margin.
        report.invoice_count += 1

        for place, amount in _where_it_went(invoice, whole, total_ttc).items():
            group = groups.get(place.group_key)
            if group is None:
                group = groups[place.group_key] = SpendGroup(key=place.group_key, name=place.group_name)
            group.money += amount
            if place.key == place.group_key:
                continue  # « à classer » is its own only place
            part = parts.get(place.key)
            if part is None:
                part = parts[place.key] = SpendPart(key=place.key, name=place.name)
                group.members.append(part)
            part.money += amount
    report.spend = report.spend_goods + report.spend_charges
    report.spend_groups = _in_order(groups)
    for group in report.spend_groups:
        group.share = _percent(group.money.ht, report.spend.ht)
        for part in group.members:
            part.share = _percent(part.money.ht, report.spend.ht)

    # An undated document is in no window (DateRange.holds(None) is False),
    # but « toutes les dépenses » over all time has no window to be outside
    # of - so with no dates typed those invoices ARE the spending, and
    # `undated_in_spend` says which of the two the figures mean.
    report.undated_in_spend = not window
    for invoice in _invoices(Invoice.objects.filter(invoice_date__isnull=True)):
        report.undated_invoices += 1
        report.undated_spend += _invoice_money(invoice)


def _invoices(queryset):
    # Invoice.total_ht/total_ttc add their lines up in Python (SQLite's own
    # arithmetic is not exact decimal): without the prefetch that is two
    # queries per invoice, on every invoice the bar has ever had. The lines
    # come with their product and its article in the same query, because
    # `_where_it_went` asks every line whose it is - prefetched as
    # `lines__product__stock_type` it would be three queries, and asked
    # line by line one per line, over a year of invoices.
    return queryset.select_related("supplier").prefetch_related(
        Prefetch("lines", queryset=InvoiceLine.objects.select_related("product__stock_type"))
    )


def _invoice_money(invoice: Invoice) -> Money:
    return Money(_cents(invoice.total_ht), _cents(invoice.total_ttc))


class _Place(NamedTuple):
    """Where a line's money lands (`key`), and the group of the table it is
    drawn under (`group_key`). « à classer » is both."""

    key: str
    name: str
    group_key: str
    group_name: str


_TO_CLASSIFY = _Place(TO_CLASSIFY_KEY, TO_CLASSIFY_NAME, TO_CLASSIFY_KEY, TO_CLASSIFY_NAME)


def _charge_place(supplier: Supplier) -> _Place:
    return _Place(supplier_key(supplier.pk), supplier.name, CHARGES_KEY, CHARGES_NAME)


def _line_place(line: InvoiceLine) -> _Place:
    """A goods line lands on its article, drawn under the article's category
    as it is TODAY. A line whose product no article claims yet is « à
    classer » - and so is a « poste de charge » product on a goods
    supplier's document, which has no article either: the Charges group is
    the documents of the suppliers of charges and nothing else, so that it
    always equals `spend_charges`."""
    article = line.product.stock_type
    if article is None:
        return _TO_CLASSIFY
    return _Place(
        article_key(article.pk),
        article.name,
        category_key(article.category),
        article.category or NO_CATEGORY,
    )


def _where_it_went(invoice: Invoice, whole: Money, total_ttc: Decimal) -> dict[_Place, Money]:
    """`whole` - the invoice's own two totals, to the cent, exactly as
    `spend` adds them - split over its places so that they add back up to
    it, to the cent, HT and TTC.

    * A charge's document goes WHOLE to its supplier: its postes are how a
      bill was read, not what the owner leaves out.
    * A document with no line at all is all adjustment: whole on its
      supplier (a charge) or on « à classer » (goods), since nothing says
      which article it was for.
    * **The duty adjustment** (`reconciliation_adjustment`: UBA's excise
      duties printed once for the whole invoice, or a receipt's HT
      rounding) belongs to the invoice, not to a line. It is spread over
      the lines **pro rata to their HT**, and only over the lines BOUGHT
      (a positive HT): a deposit given back is a negative line, and a signed
      pro rata would put a negative share of the beer's duty on the returned
      keg and more than the whole duty on the beer. Only when no line is
      positive do they all weigh by their size; when every line is nil,
      nothing is spread and the adjustment is all remainder (below).
    * Each line's TTC is counted exactly as `Invoice.total_ttc` counts it:
      from what a receipt printed when every line kept its printed amount,
      otherwise from each line's HT at its rate - the adjustment's TTC being
      whatever `total_ttc` adds on top of those, spread like its HT.

    **The remainder goes on the largest place.** Rounded one by one, the
    shares can miss the invoice's own total by a centime or two - three
    shares of 7,31 € of duty come to 7,32 € - and a receipt paid at its
    printed total carries the few centimes between that total and its
    printed lines. Whatever separates the rounded places from `whole` is
    put on the place with the most HT (the first of them on a tie), so that
    with nothing left out the second margin IS the first, to the cent.
    """
    if invoice.supplier.expenses_only:
        return {_charge_place(invoice.supplier): whole}
    lines = list(invoice.lines.all())
    if not lines:
        return {_TO_CLASSIFY: whole}

    printed = all(line.printed_ttc is not None for line in lines)
    lines_ttc = [line.total_ttc if printed else line.total_ht * (Decimal("1") + line.vat_rate) for line in lines]
    adjustment_ht = invoice.reconciliation_adjustment
    # A printed receipt's `total_ttc` leaves its adjustment out (it is HT
    # rounding the printed amounts never lost), and what it adds on top of
    # the lines is the printed total's own centimes: remainder, not duty.
    adjustment_ttc = ZERO if printed else total_ttc - sum(lines_ttc, start=ZERO)
    weights = [line.total_ht if line.total_ht > 0 else ZERO for line in lines]
    if not any(weights):
        weights = [abs(line.total_ht) for line in lines]
    weight_total = sum(weights, start=ZERO)

    raw: dict[_Place, list[Decimal]] = {}
    for line, ttc, weight in zip(lines, lines_ttc, weights):
        ht = line.total_ht
        if weight_total:
            ht += adjustment_ht * weight / weight_total
            ttc += adjustment_ttc * weight / weight_total
        amounts = raw.setdefault(_line_place(line), [ZERO, ZERO])
        amounts[0] += ht
        amounts[1] += ttc

    placed = {place: Money(_cents(ht), _cents(ttc)) for place, (ht, ttc) in raw.items()}
    largest = max(raw, key=lambda place: abs(raw[place][0]))
    placed[largest] += whole - sum(placed.values(), start=Money())
    return placed


def _in_order(groups: dict[str, SpendGroup]) -> list[SpendGroup]:
    """Charges first - the owner's own example of what to leave out - then
    the article categories, biggest first, then « à classer » last: it is
    what is still to be done, not a category. Inside a group, the biggest
    first too."""
    charges = groups.pop(CHARGES_KEY, None)
    to_classify = groups.pop(TO_CLASSIFY_KEY, None)
    ordered = sorted(groups.values(), key=lambda group: (-group.money.ht, group.name))
    ordered = ([charges] if charges else []) + ordered + ([to_classify] if to_classify else [])
    for group in ordered:
        group.members.sort(key=lambda part: (-part.money.ht, part.name))
    return ordered


# -- « sans … » ---------------------------------------------------------------


def known_left_out(keys: Iterable[str]) -> list[str]:
    """`keys` as the page will read them: each one it knows, once, in the
    order given, in its own spelling (`article:0012` is `article:12`). What
    « Recalculer » redirects to, so the address never keeps a key the page
    has already dropped."""
    return [exclusion.key for exclusion in _resolve(keys)]


def _resolve(keys: Iterable[str]) -> list[Exclusion]:
    """The keys of `?sans=`, named - and every one this page does not know
    dropped, never raised on: they come from a query string, so a stale
    bookmark, a hand-typed URL and an article deleted since the page was
    drawn all land here.

    Known means: « charges » and « à classer », always; a supplier that
    exists and is a supplier OF CHARGES (a goods supplier's money is its
    articles', and leaving it out would be a second, contradicting
    selector); an article that exists; a category some article carries -
    in the window or not, since « sans Consignes » over a month none were
    bought is still the owner's question. One query per KIND of key, however
    many there are.
    """
    asked: list[tuple[str, object]] = []
    supplier_ids: set[int] = set()
    article_ids: set[int] = set()
    categories: set[str] = set()
    for key in keys:
        if not isinstance(key, str):
            continue
        if key in (CHARGES_KEY, TO_CLASSIFY_KEY):
            asked.append((key, key))
            continue
        kind, separator, rest = key.partition(":")
        kind += separator
        # `common.is_id`, not int(): « ² » is a digit to str.isdigit() and no
        # int, and past eighteen digits SQLite's integer overflows.
        if kind == SUPPLIER_PREFIX and is_id(rest):
            supplier_ids.add(int(rest))
            asked.append((kind, int(rest)))
        elif kind == ARTICLE_PREFIX and is_id(rest):
            article_ids.add(int(rest))
            asked.append((kind, int(rest)))
        elif kind == CATEGORY_PREFIX:
            # Everything after the first colon, as stored - a category may
            # hold a colon of its own.
            categories.add(rest)
            asked.append((kind, rest))

    suppliers = (
        dict(Supplier.objects.filter(pk__in=supplier_ids, expenses_only=True).values_list("pk", "name"))
        if supplier_ids
        else {}
    )
    articles = (
        {pk: (name, category) for pk, name, category in StockType.objects.filter(pk__in=article_ids).values_list("pk", "name", "category")}
        if article_ids
        else {}
    )
    known_categories = (
        set(StockType.objects.filter(category__in=categories).order_by().values_list("category", flat=True).distinct())
        if categories
        else set()
    )

    chosen: dict[str, Exclusion] = {}
    parent: dict[str, str] = {}
    for kind, value in asked:
        if kind == CHARGES_KEY:
            exclusion = Exclusion(key=CHARGES_KEY, name=CHARGES_NAME)
        elif kind == TO_CLASSIFY_KEY:
            exclusion = Exclusion(key=TO_CLASSIFY_KEY, name=TO_CLASSIFY_NAME)
        elif kind == SUPPLIER_PREFIX and value in suppliers:
            exclusion = Exclusion(key=supplier_key(value), name=suppliers[value])
            parent[exclusion.key] = CHARGES_KEY
        elif kind == ARTICLE_PREFIX and value in articles:
            name, category = articles[value]
            exclusion = Exclusion(key=article_key(value), name=name)
            parent[exclusion.key] = category_key(category)
        elif kind == CATEGORY_PREFIX and value in known_categories:
            exclusion = Exclusion(key=category_key(value), name=value or NO_CATEGORY)
        else:
            continue
        chosen.setdefault(exclusion.key, exclusion)

    for exclusion in chosen.values():
        holder = chosen.get(parent.get(exclusion.key, ""))
        if holder is not None:
            exclusion.within = holder.name
            exclusion.within_key = holder.key
    return list(chosen.values())


def _leave_out(report: MarginReport, exclusions: list[Exclusion]) -> None:
    """Take `exclusions` out of the spending: each PLACE once, however many
    keys reach it (« Charges » and one of its suppliers; « Matériel » and
    one of its articles), and ticks on the table that say so."""
    report.exclusions = exclusions
    # Every place's money, and the places each key of the table stands for:
    # a group is its members, « à classer » is itself, a place is itself.
    money_of: dict[str, Money] = {}
    places_of: dict[str, list[str]] = {}
    for group in report.spend_groups:
        if group.members:
            places_of[group.key] = [part.key for part in group.members]
            for part in group.members:
                money_of[part.key] = part.money
                places_of[part.key] = [part.key]
        else:
            money_of[group.key] = group.money
            places_of[group.key] = [group.key]

    chosen = {exclusion.key for exclusion in exclusions}
    out: set[str] = set()
    for exclusion in exclusions:
        # A key with nothing in the window has no place, and takes out 0,00 €.
        places = places_of.get(exclusion.key, [])
        exclusion.money = sum((money_of[key] for key in places), start=Money())
        exclusion.invoiced = bool(places)
        out.update(places)
    report.spend_left_out = sum((money_of[key] for key in out), start=Money())

    for group in report.spend_groups:
        group.unticked = group.key in chosen
        for part in group.members:
            part.unticked = part.key in chosen
            part.left_out = part.key in out


# -- the paper towels --------------------------------------------------------

#: One pass over every purchase, with the columns that price it.
#: `StockMovement.effective_date` is a PROPERTY, so narrowing the window on it
#: over model instances would be a query per movement - the dates are read
#: here and the day chosen below.
_MOVEMENT_COLUMNS = (
    "stock_type_id",
    "quantity",
    "unit_cost_ht",
    "invoice_line__total_ht",
    "invoice_line__vat_rate",
    "invoice_line__printed_ttc",
    "invoice_line__discount_ttc",
    "occurred_on",
    "invoice_line__invoice__invoice_date",
    "created_at",
)


def _read_the_flagged_articles(report: MarginReport, window: DateRange) -> None:
    """The articles flagged « compter dans la marge produits », at what was
    BOUGHT of them over the window - and every other article beside them,
    with what ticking it would add (`report.countable`).

    No recipe consumes them, so there is nothing to take out of stock per
    sale: the purchases are the only measure there is, and the page has to
    say that is what the figure means (`_bought_over` says how they are
    counted and dated).

    **One reading for both**: the cost the products margin counts and the
    figure the panel prints beside a box come out of the same pass, so what
    a box promises is what the margin moves by, to the cent. Asked of the
    ticked articles alone, the panel would have needed a second reading of
    the purchases - one that could drift from this one.

    Three queries whatever the number of articles: the articles, the recipe
    lines naming one, the purchases.
    """
    articles = list(
        StockType.objects.order_by().values_list("pk", "name", "category", "count_in_products_margin")
    )
    # An article ticked AND used in a recipe is paid for twice - once as what
    # the recipe consumed, once as what was bought. Its own help text says
    # not to do it and nothing checked; asked of every article at once, the
    # panel can mark the one about to be ticked as well as the one that is.
    in_recipes = set(
        RecipeIngredient.objects.filter(stock_type__isnull=False)
        .order_by()
        .values_list("stock_type_id", flat=True)
        .distinct()
    )
    bought = _bought_over(window)

    flagged = [(pk, name) for pk, name, _category, ticked in articles if ticked]
    report.flagged_articles = len(flagged)
    report.flagged_in_recipes = sorted(name for pk, name in flagged if pk in in_recipes)
    report.extra_products = sorted(
        (ExtraProduct(name=name, ht=bought[pk].ht, ttc=bought[pk].ttc) for pk, name in flagged if pk in bought),
        key=lambda entry: (-entry.ht, entry.name),
    )
    report.extra_products_ht = sum((entry.ht for entry in report.extra_products), start=ZERO)
    report.extra_products_ttc = sum((entry.ttc for entry in report.extra_products), start=ZERO)

    categories: dict[str, CountableCategory] = {}
    for pk, name, category, ticked in articles:
        holder = categories.setdefault(category, CountableCategory(name=category))
        holder.articles.append(
            CountableArticle(
                pk=pk,
                name=name,
                ticked=ticked,
                bought=bought.get(pk, Money()),
                was_bought=pk in bought,
                in_recipe=pk in in_recipes,
            )
        )
    # Alphabetical, a setting being looked for rather than money being
    # ranked (the amounts change with the window; the list should not), and
    # as a person reads it: SQLite's ORDER BY is by byte, which files
    # « Épicerie » after « Vins » and « abricot » after « Zeste ». The blank
    # category last, like « à classer » in the breakdown above it.
    for holder in categories.values():
        holder.articles.sort(key=lambda article: (_reading_order(article.name), article.name))
    report.countable = sorted(
        categories.values(), key=lambda holder: (not holder.name, _reading_order(holder.name), holder.name)
    )


def _reading_order(name: str) -> str:
    """`name` as an alphabetical list files it: accents and case aside."""
    decomposed = unicodedata.normalize("NFD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _bought_over(window: DateRange) -> dict[int, Money]:
    """{article id: what was bought of it over the window} - every article
    with at least one purchase in it, signed (a deposit given back is a
    negative purchase and takes it down).

    Counted exactly the way « Produits & charges » counts a purchase - the
    invoice line's own `total_ht`, never quantity times the unit cost, which
    is that amount divided by the quantity and stored to four decimals, then
    multiplied back (2 000 pailles charged 24,64 € came back 24,60 €).

    **Dated by its INVOICE**, like the real margin's spending, and only by
    the movement's own day when there is no invoice behind it (a correction
    typed by hand). The two margins are read side by side on one page: dated
    by the delivery instead, an article invoiced on 25/02 and received on
    10/03 was in February on « Facturé » and in March on « Achats des
    articles cochés », with nothing anywhere saying so. Note this is
    deliberately NOT `StockMovement.effective_date`, which « Produits &
    charges » sums a row by: that page is about the shelf, this one about
    the bill.
    """
    found: dict[int, Money] = {}
    values = StockMovement.objects.filter(kind=MovementKind.PURCHASE).values_list(*_MOVEMENT_COLUMNS)
    for (
        stock_type_id,
        quantity,
        unit_cost_ht,
        line_total_ht,
        vat_rate,
        printed_ttc,
        discount_ttc,
        occurred_on,
        invoice_date,
        created_at,
    ) in values:
        day = invoice_date or occurred_on or (created_at.date() if created_at else None)
        if not window.holds(day):
            continue
        ht = line_total_ht if line_total_ht is not None else quantity * unit_cost_ht
        if printed_ttc is not None:
            ttc = printed_ttc - discount_ttc
        elif line_total_ht is not None:
            ttc = line_total_ht * (vat_rate + Decimal("1"))
        else:
            # A movement with no invoice line behind it - a correction typed
            # by hand - has no rate to work a TTC out of. Its HT counts and
            # its TTC is left alone rather than assumed, which is what
            # catalogue_context does with the same row.
            ttc = ZERO
        found[stock_type_id] = found.get(stock_type_id, Money()) + Money(ht, ttc)
    return found
