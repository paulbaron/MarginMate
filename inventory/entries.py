"""What can be typed for a count or a purchase, and in which unit: the entry
vocabulary, the matcher and the units shared by the stock take's rows
(`forms`) and the shopping lists' add form and card (`shopping_lists`,
`views`). It never imports the views or the forms; of this app it imports
`models`, `services` and `variance` only.

- **The vocabulary.** A product is entered as « RAW NAME — Store »
  (`product_display_name`), an article as « Name (article) »
  (`stock_type_entry_name`); the old suffix « (type de stock) » still reads
  (`current_entry_name`: a count kept in the browser from before 19/09 says
  it). Two names are « the same » with case, accents and spacing ignored
  (`same_name`). None is longer than ENTRY_MAX, read off the columns.
- **The matcher.** `EntryResolver` loads the classified products and the
  articles once - two queries - and answers every name typed with no query
  more: the stock take's rows (every store's products), or one store's add
  form (`supplier_id`: that store's products only, so another store's
  product is no product there). `resolve(typed)` is the stock take's two
  exact rules; `resolve(typed, forgiving=True)` the shopping lists' six, in
  this order:
  1. a product of the store by its display name, exactly;
  2. an article by its entry name (or the old one), exactly;
  3. an article whose suffix is written otherwise (« vodka EXEMPLE
     (Article) »): the suffix taken off, then rule 4 on the rest;
  4. an article by its name alone: the exact name, else the ONE article
     reading the same (`find_article`, the lists' habit « biere exemple »);
  5. a product of the store by its raw name, exactly, else the ONE whose raw
     or display name reads the same;
  6. nothing: a free text.
  An article always wins over a product reading alike (4 before 5). Only a
  classified product is ever a product: one « à classer » counts nothing.
- **The units.** The values posted are the stock take's: `ITEMS`
  (`UnitChoices.UNIT`, « bouteilles/packs ») and the article's own `L` or
  `KG`. A product of a UNIT article counts items alone; any other product
  items or the article's measure, items by default unless it is weighed
  (`product_units`, the stock take's rule). An article counts its own unit,
  and items too when the caller knows what one is (`article_units`).
- **One item's size**, in the article's unit (0.7 for a 70 cl bottle of a
  vodka in litres): `variance.stock_units_per_item`, which a count and a
  purchase both convert with, to four places half up (`item_size`). None
  for a weighed product (`services.is_discrete_count` false: its « item » is
  whatever a weighing gave) and for what the shopping list's column cannot
  hold (`quantized_size`: 0 < size < 10^6).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from common import search_key
from invoices.models import Supplier

from .models import Product, StockType, UnitChoices
from .services import first_purchase_dates, is_discrete_count, product_counting_ratios
from .variance import stock_units_per_item

#: What an entry is, as the stock take's island ships it (`kind`).
PRODUCT_KIND = "product"
ARTICLE_KIND = "stock_type"
#: « bouteilles/packs »: the stock take's own value for counting items.
ITEMS = UnitChoices.UNIT
#: ShoppingListItem.item_size's column: (SIZE_DIGITS, SIZE_PLACES).
SIZE_PLACES, SIZE_DIGITS = 4, 10
SIZE_LIMIT = Decimal(10) ** (SIZE_DIGITS - SIZE_PLACES)
SIZE_QUANTUM = Decimal(1).scaleb(-SIZE_PLACES)

STOCK_TYPE_ENTRY_SUFFIX = " (article)"
# What the suffix read before a StockType became an « article » (19/09). A
# count in progress is kept in the browser as it was typed (the draft net of
# stock_take_form.html), so an entry carrying it has to keep resolving.
OLD_STOCK_TYPE_ENTRY_SUFFIXES = (" (type de stock)",)
#: Between a product's raw name and its store's, in its entry name.
PRODUCT_ENTRY_SEPARATOR = " — "


def _longest(model, field: str) -> int:
    return model._meta.get_field(field).max_length


#: The longest name the vocabulary makes, read off the columns: a product's
#: raw name at a store (« RAW — Enseigne »), longer than any article's with
#: either suffix. A menu name is never longer, so a form field taking one
#: never cuts it (the shopping list's add form; a longer text names nothing).
ENTRY_MAX = max(
    _longest(Product, "raw_name") + len(PRODUCT_ENTRY_SEPARATOR) + _longest(Supplier, "name"),
    _longest(StockType, "name") + max(map(len, (STOCK_TYPE_ENTRY_SUFFIX, *OLD_STOCK_TYPE_ENTRY_SUFFIXES))),
)


# --------------------------------------------------------------------- the names
def product_display_name(product: Product) -> str:
    return f"{product.raw_name}{PRODUCT_ENTRY_SEPARATOR}{product.supplier.name}"


def stock_type_entry_name(stock_type: StockType) -> str:
    return f"{stock_type.name}{STOCK_TYPE_ENTRY_SUFFIX}"


def current_entry_name(name: str) -> str:
    """`name` with an old stock-item suffix put back to today's one."""
    for old in OLD_STOCK_TYPE_ENTRY_SUFFIXES:
        if name.endswith(old):
            return name[: -len(old)] + STOCK_TYPE_ENTRY_SUFFIX
    return name


def is_stock_type_entry(name: str) -> bool:
    return current_entry_name(name).endswith(STOCK_TYPE_ENTRY_SUFFIX)


def same_name(text: str) -> str:
    """What two names are compared by: case, accents and spacing ignored."""
    return search_key(" ".join(text.split()))


def _suffix_key(text: str) -> str:
    """A suffix with case, accents and EVERY space ignored: « ( Article ) »
    is « (article) »."""
    return search_key("".join(text.split()))


#: Every article suffix as `_suffix_key` reads it: « (article) », « (typedestock) ».
_SUFFIX_KEYS = frozenset(_suffix_key(suffix) for suffix in (STOCK_TYPE_ENTRY_SUFFIX, *OLD_STOCK_TYPE_ENTRY_SUFFIXES))


def _without_suffix(text: str) -> str | None:
    """`text` without an article suffix written in any case, accents or
    spacing (« Vodka exemple (Article) », « Vodka exemple(article) »,
    « Vodka exemple ( article ) »); None when it carries none."""
    opening = text.rfind("(")
    if opening <= 0 or _suffix_key(text[opening:]) not in _SUFFIX_KEYS:
        return None
    return text[:opening].strip()


class _Articles:
    """Articles by exact name and by `same_name`: `find_article`'s rule,
    looked up rather than walked when one resolver answers many names."""

    def __init__(self, articles: Iterable[StockType]):
        self.by_name: dict[str, StockType] = {}
        self.by_key: dict[str, list[StockType]] = {}
        for article in articles:
            self.by_name.setdefault(article.name, article)
            self.by_key.setdefault(same_name(article.name), []).append(article)

    def find(self, typed: str) -> StockType | None:
        exact = self.by_name.get(typed)
        if exact is not None:
            return exact
        key = same_name(typed)
        alike = self.by_key.get(key, []) if key else []
        return alike[0] if len(alike) == 1 else None


def find_article(typed: str, articles: Iterable[StockType]) -> StockType | None:
    """The article `typed` names among `articles`: its exact name, else the
    ONE article that reads the same with case, accents and spacing ignored.
    None (a free text) when none does, or several. Reads nothing. The rule's
    reference, for a caller holding articles: `EntryResolver` (rule 4) runs
    the same `_Articles` over the ones it loaded."""
    return _Articles(articles).find(typed)


# --------------------------------------------------------------------- the matcher
@dataclass(frozen=True)
class Entry:
    """What a name typed resolved to: an article, or one of its products."""

    kind: str  # PRODUCT_KIND | ARTICLE_KIND
    article: StockType  # a product's: product.stock_type (loaded with it)
    product: Product | None


def _product_entry(product: Product) -> Entry:
    return Entry(PRODUCT_KIND, product.stock_type, product)


def _article_entry(article: StockType) -> Entry:
    return Entry(ARTICLE_KIND, article, None)


class EntryResolver:
    """Turns the text typed into a stock-take row - or into a shopping
    list's add form - back into the Product or StockType it names.

    Loaded once and shared by every row of a formset. Resolving one row at a
    time meant a query per row, so a 400-line inventory spent 400 queries
    just deciding what the user had typed, before a single line was valued.

    `supplier_id` scopes the products to one store (the shopping lists);
    None is every classified product (the stock take). The articles are
    always all of them.

    It also answers "did this exist yet", which is the same question asked of
    every row against the same date - see first_purchase_dates.
    """

    def __init__(self, *, supplier_id: int | None = None):
        self._supplier_id = supplier_id
        self._products = None
        self._product_ids = None
        self._stock_types = None
        self._first_purchases = None
        self._articles = None
        self._products_by_raw = None
        self._products_by_key = None

    def _load(self):
        if self._products is not None:
            return
        # A product's suggestion and its supplier's identifiers are never
        # read from here: decoded for every product, they were a third of
        # what loading took - on every row priced live as it is typed.
        products = Product.objects.select_related("supplier", "stock_type").filter(stock_type__isnull=False)
        if self._supplier_id is not None:
            products = products.filter(supplier_id=self._supplier_id)
        products = list(
            products.defer(
                "ai_suggestion",
                "supplier__ticket_identifiers",
                "supplier__refused_identifiers",
                "supplier__typed_identifiers",
            )
        )
        self._products = {product_display_name(product): product for product in products}
        self._product_ids = [product.id for product in products]
        self._stock_types = {stock_type_entry_name(st): st for st in StockType.objects.all()}

    def _first_purchase_dates(self) -> dict:
        """Read the first time a row is judged against a date - a row priced
        live (value_stock_take_line) never is."""
        if self._first_purchases is None:
            self._load()
            self._first_purchases = first_purchase_dates(self._product_ids)
        return self._first_purchases

    def product(self, name: str) -> Product | None:
        self._load()
        return self._products.get(name)

    def stock_type(self, name: str) -> StockType | None:
        self._load()
        return self._stock_types.get(current_entry_name(name))

    def first_purchase(self, product: Product):
        """When this product was first delivered, or None if nothing dated
        says - in which case there is no ground to call it too new."""
        return self._first_purchase_dates().get(product.id)

    def stock_type_first_purchase(self, stock_type: StockType):
        """The earliest delivery of ANY product under this stock item: the
        stock item existed from the moment its first bottle arrived,
        whichever brand that was."""
        first_purchases = self._first_purchase_dates()
        dates = [
            first_purchases[product.id]
            for product in self._products.values()
            if product.stock_type_id == stock_type.id and product.id in first_purchases
        ]
        return min(dates) if dates else None

    def resolve(self, typed: str, *, forgiving: bool = False) -> Entry | None:
        """What `typed` names (the module's rules; its spaces around taken
        off, as the stock take's save does), or None: a free text. Not
        forgiving, the two exact rules only - the stock take's. No query
        once loaded, whatever is typed."""
        text = typed.strip() if isinstance(typed, str) else ""
        if not text:
            return None
        self._load()
        product = self._products.get(text)
        if product is not None:
            return _product_entry(product)
        article = self._stock_types.get(current_entry_name(text))
        if article is not None:
            return _article_entry(article)
        if not forgiving:
            return None
        named = _without_suffix(text)
        article = self._article_named(text if named is None else named)
        if article is not None:
            return _article_entry(article)
        product = self._product_named(text)
        return None if product is None else _product_entry(product)

    def _article_named(self, typed: str) -> StockType | None:
        if self._articles is None:
            self._articles = _Articles(self._stock_types.values())
        return self._articles.find(typed)

    def _product_named(self, typed: str) -> Product | None:
        """Rule 5: the raw name exactly, else the ONE product whose raw or
        display name reads the same."""
        if self._products_by_raw is None:
            self._products_by_raw, self._products_by_key = {}, {}
            for display, product in self._products.items():
                self._products_by_raw.setdefault(product.raw_name, []).append(product)
                for key in {same_name(product.raw_name), same_name(display)}:
                    self._products_by_key.setdefault(key, []).append(product)
        exact = self._products_by_raw.get(typed, [])
        if exact:
            return exact[0] if len(exact) == 1 else None
        key = same_name(typed)
        alike = self._products_by_key.get(key, []) if key else []
        return alike[0] if len(alike) == 1 else None


# --------------------------------------------------------------------- the units
@dataclass(frozen=True)
class EntryUnits:
    """The units an entry may be counted in, in the order offered, and the
    one taken when none is chosen."""

    choices: tuple[str, ...]  # UnitChoices values
    default: str


def product_units(product: Product, *, is_discrete: bool) -> EntryUnits:
    """A product of a UNIT article counts items only; any other counts items
    or its article's measure (« mesuré directement »: a bottle half empty),
    items by default unless it is weighed - `is_discrete`, from
    `services.is_discrete_count`. The stock take's rule."""
    unit = product.stock_type.unit
    if unit == UnitChoices.UNIT:
        return EntryUnits((ITEMS,), ITEMS)
    return EntryUnits((ITEMS, unit), ITEMS if is_discrete else unit)


def article_units(article: StockType, *, items: bool = False, items_by_default: bool = False) -> EntryUnits:
    """An article counts its own unit; with `items` (the caller knows what
    one item of it is) items first, then its unit - items by default only
    with `items_by_default`. A UNIT article counts items, whatever is asked.
    The stock take passes neither flag."""
    unit = article.unit
    if unit == UnitChoices.UNIT:
        return EntryUnits((ITEMS,), ITEMS)
    if items:
        return EntryUnits((ITEMS, unit), ITEMS if items_by_default else unit)
    return EntryUnits((unit,), unit)


# --------------------------------------------------------------------- one item's size
def quantized_size(size) -> Decimal | None:
    """`size` to SIZE_PLACES places, half up, or None unless it is a finite
    number strictly between 0 and SIZE_LIMIT once rounded: what
    ShoppingListItem.item_size (10, 4) holds, and nothing it would hold
    wrong (a figure wider than its column is unreadable for good)."""
    if size is None or isinstance(size, bool):
        return None
    try:
        value = size if isinstance(size, Decimal) else Decimal(size)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or not 0 < value < SIZE_LIMIT:
        return None
    quantized = value.quantize(SIZE_QUANTUM, rounding=ROUND_HALF_UP)
    return quantized if 0 < quantized < SIZE_LIMIT else None


def item_size(product: Product, ratios: Mapping[int, set]) -> Decimal | None:
    """How much of its article's unit one item of `product` holds (0.7000 for
    a 70 cl bottle of a vodka in litres), from `ratios`
    (`services.product_counting_ratios`, read by the caller) - no query.
    None for a weighed product, or for a size no column holds."""
    if not is_discrete_count(ratios, product.pk):
        return None
    return quantized_size(stock_units_per_item(product, ratios))


def product_item_sizes(product_ids) -> dict[int, Decimal]:
    """{product_id: item_size} for many products, in two queries (the
    products' unit and factor, their ratios); the products with no size
    are left out."""
    ids = [product_id for product_id in product_ids if product_id is not None]
    if not ids:
        return {}
    products = list(Product.objects.filter(pk__in=ids).order_by().only("id", "unit", "stock_equivalent"))
    ratios = product_counting_ratios([product.pk for product in products])
    sizes = {}
    for product in products:
        size = item_size(product, ratios)
        if size is not None:
            sizes[product.pk] = size
    return sizes
