"""« Listes de courses »: what the list pages and « Prévoir les courses »
share, testable without a request. It never imports the views (they import
it).

The model: a `ShoppingList` per store, shared by every login of the espace,
OPEN while `finished_at` is empty and kept read-only once finished; its
`ShoppingListItem`s name an article (its name kept in `label`) or a free text,
with a quantity counting the store's product (`unit` "") or the article's own
unit.

- **One open list per store, made by the first item** (`open_list_for`, under
  the database's partial unique constraint): a page drawn writes nothing, and
  two requests making it at once end with one list - the loser reads the
  winner's. **An open list with no item is no list in progress**: emptied by
  « Retirer », it keeps no store reachable (`store_of`), and its next first
  item starts it again (`add_item`: its `created_at` and `created_by` are
  that item's).
- **Adding is idempotent** (`add_item`): the same article, or a free text
  reading the same (`entries.same_name`), already on the open list to buy is
  answered as « already there » (ALREADY), unchanged - a double submit adds
  nothing. **A ticked (bought) one is put back to buy** (RELISTED): unticked,
  the figures asked for written over its own - its sizes included. The add
  looks and writes in ONE transaction, and checks again, in the savepoint
  that writes, that the list is still open: a list finished meanwhile sends
  it to the store's current open list - never onto a finished one. Under
  production's IMMEDIATE mode the write lock is taken before the look, so
  two adds of one free text, or an add and a « Courses terminées », follow
  one another.
- **What is written fits its column** (`fits`, `size_fits`): a quantity wider
  than (10, 3), or a size wider than (10, 4), is stored by SQLite without a
  word and makes the row unreadable for good, so `add_item` refuses it
  (`QuantityTooWide`; a size is a ValueError: only code can make one) and a
  merge keeps the two lines rather than write their sum.
- **A tick names the WANTED state** (`set_ticked`), never a toggle: two phones
  ticking two items both land, a second tick of a ticked item keeps the first
  one's who and when, and a finished list refuses it.
- **Finishing carries the rest over** (`finish`): the list is closed once - a
  double submit finds it closed and carries nothing twice - then, when asked,
  a copy of each unticked item goes to the store's next open list (an
  article already there skipped), its `added_at` and sizes kept. The finished
  list keeps every item as it was.
- **A merge keeps every line** (`carry_on_merge`, from
  `services.merge_stock_types`): an item of the merged article names the
  target; beside a target item counting the same thing - its unit, its
  product, the size of one item and its unit -, the two make one (unless
  their sum would not fit); beside one counting something else (70 cl
  beside 1 L), it becomes a free text.
- **Who did what never shows an address** (`display_names`): a first name, a
  role in THIS espace, or « un ancien membre ».

**Bottles or litres** (the owner: « généralement on parle en bouteilles »;
SPEC_UNITS §2.2-§2.7). An item may count ITEMS of a known size: `item_size`
of `size_unit` each (0.7 L for a 70 cl bottle), set only beside `unit` "",
and a snapshot - a change of the usual product, of the article's unit, or
the article deleted leave « 3 bouteilles de 70 cl » meaning what it meant.

- **What one item of an article is at a store** (`article_item`, one rule,
  pure; `article_item_of` reads what it needs): an item of the usual
  product there when the usual purchase counts one (its size
  `entries.item_size`; none when it is weighed); else, for an article in
  litres or kilos, the format it is most bought in anywhere, weighed
  products left out (`variance.typical_item_sizes(discrete_only=True)`);
  else none - a UNIT article's unit already counts pieces.
- **The units an entry offers** (`entry_units`): the stock take's values -
  `entries.ITEMS` (« UNIT »), and the article's own L or KG. A product
  entry follows the stock take's rule (`entries.product_units`); an article
  counts items when an item is known, by default where its usual purchase
  counts a product, or - never bought there - for the units of
  ITEMS_BY_DEFAULT (litres: « on parle en bouteilles »).
- **What is stored** (`entry_figures`, the card's `card_figures`): the
  number typed counts the unit chosen and is never converted; nothing typed,
  the store's usual figure in that unit.
- **The card's items** (`card_item`, one rule for its units, labels and
  figures): an item counting items keeps its own; one counting a measure
  takes the item known now - or, naming another product, that product
  found again with its size, else a bare number of it -, so the label says
  what the save stores.
- **The words** (`quantity_words`, `size_words`, `unit_label`): « 3
  bouteilles de 70 cl », « 1 fût de 30 L », « 1 pack de 4.5 L » (a product
  whose name prints a count times a size it holds more than), « 2 paquets
  de 500 g », and « 24 » for a UNIT size of 1 - the piece the article
  counts.
- **The add menu** (`list_entries`): every article and the store's
  classified products, each with its units and their labels, read through
  the very chain the add goes through, in seven queries whatever the
  history; and the names the add also accepts (`list_aliases`), each kept
  only where the add's own resolver names that very entry.
"""

from __future__ import annotations

import enum
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TypeGuard

from django.db import IntegrityError, transaction
from django.db.models import Case, DateTimeField, Exists, F, OuterRef, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import Membership
from common import is_id, plain_number, read_amount, search_key
from invoices.models import Supplier
from invoices.parsers import LLM_PARSER_KEY

from . import entries
from .entries import ARTICLE_KIND, ITEMS, PRODUCT_KIND, Entry, EntryUnits, same_name
from .models import (
    MovementKind,
    Product,
    ShoppingList,
    ShoppingListItem,
    StockMovement,
    StockType,
    UnitChoices,
)
from .quantity_extraction import COUNT_X_SIZE_RE, SIZE_X_COUNT_RE, SPACED_SIZE_X_COUNT_RE, VOLUME_UNITS
from .services import is_discrete_count, product_counting_ratios
from .variance import typical_item_sizes

#: An article's unit as the pages write it after a quantity: « 3 L », « 12 u. ».
UNIT_SYMBOLS = {UnitChoices.LITRE: "L", UnitChoices.KILOGRAM: "kg", UnitChoices.UNIT: "u."}
#: An item's noun by the unit its size is in: (singular, plural). An item in
#: litres over KEG_FROM is a keg or a bag-in-box (« fût »): « 1 bouteille de
#: 30 L » would be wrong. One whose product's name prints a count of several
#: times a size it holds more than (a carton of six 75 cl bottles, a pack of
#: 24 cans) is a pack, whatever its size (`_nouns`).
ITEM_NOUNS: dict[str, tuple[str, str]] = {
    UnitChoices.LITRE: ("bouteille", "bouteilles"),
    UnitChoices.KILOGRAM: ("paquet", "paquets"),
    UnitChoices.UNIT: ("paquet", "paquets"),
}
KEG_NOUNS, KEG_FROM = ("fût", "fûts"), Decimal("3")
PACK_NOUNS = ("pack", "packs")
#: A measure as a unit select offers it.
UNIT_WORDS: dict[str, str] = {UnitChoices.LITRE: "litres", UnitChoices.KILOGRAM: "kg", UnitChoices.UNIT: "unités"}
#: An article in litres whose item size nobody knows: the litres are all there is.
NO_FORMAT_WORDS = "litres (format inconnu)"
#: The add form's one option with no JavaScript (and a free text's): the
#: entry's default unit.
USUAL_WORD = "habituelle"
#: The units whose articles are entered in items by default at a store they
#: were never bought at (the owner: « on parle en bouteilles »).
ITEMS_BY_DEFAULT = frozenset({UnitChoices.LITRE})
#: The units an article is measured in, rather than counted.
MEASURED = frozenset({UnitChoices.LITRE, UnitChoices.KILOGRAM})
#: ShoppingListItem.quantity's decimal places and digits.
QUANTITY_PLACES, QUANTITY_DIGITS = 3, 10
#: ShoppingListItem.label's and .note's lengths.
LABEL_MAX = 255
NOTE_MAX = 200
#: What a posted pack size (`colis`) may be, both ends included: a pack of
#: several (the item's check constraint), four digits at most.
PACK_RANGE = (2, 9999)
#: How many finished lists the lists' page shows (« Terminées »).
RECENT_FINISHED = 10
#: The first quantity the column cannot hold (10 digits, 3 places: 10 000 000).
QUANTITY_LIMIT = Decimal(10) ** (QUANTITY_DIGITS - QUANTITY_PLACES)
#: Between two notes joined by a merge.
NOTE_SEPARATOR = " · "
#: What a list says for the viewer's own name.
YOU = "Vous"
#: Who did what, for a login of this espace with no first name, by its
#: role; and for any other login (removed, another bar's, none).
OWNER_WORD = "le gérant"
MEMBER_WORD = "un employé"
GONE_WORD = "un ancien membre"
#: A number of units that is no whole number of packs (`pack_words`).
BY_THE_UNIT = "à l'unité"
#: What `add_item` says of a quantity it refuses (`QuantityTooWide`).
QUANTITY_TOO_WIDE = "« {name} » : quantité trop grande, rien n'a été ajouté."
#: How many times an add looks for the store's open list again when the one
#: it read was finished before it could write. Under production's IMMEDIATE
#: transactions the first look always holds; this bounds the other modes.
ADD_ATTEMPTS = 3

_QUANTUM = Decimal(1).scaleb(-QUANTITY_PLACES)


class AddOutcome(enum.Enum):
    """What `add_item` did with what it was asked to add."""

    #: A new item.
    ADDED = "added"
    #: A ticked (bought) item of it put back to buy, the figures asked for
    #: written over its own.
    RELISTED = "relisted"
    #: An item of it already there, left as it was: still to buy - or
    #: ticked, when the add was told not to put it back (`relist` false).
    ALREADY = "already"


class QuantityTooWide(ValueError):
    """A quantity the item's column cannot hold (`fits`): nothing written.
    Its message is French, naming what was being added."""


@dataclass(frozen=True)
class Figures:
    """What an item counts, as it is stored: `quantity` of the product
    `product_name` when `unit` is "" (or of whatever a free text names), else
    of the article's own unit. `product_id` is the forecast form's to post
    (an item keeps the name only); `pack_size` the product's colisage, a hint.

    `item_size` is how much of `size_unit` (the article's unit when added)
    one counted item holds - 0.7 for a 70 cl bottle -, set only with `unit`
    "" and both or neither (the item's check constraint): the number then
    counts bottles, kegs or packs of that size (`quantity_words`). Both
    default to none, so every figure written before reads the same."""

    quantity: Decimal
    unit: str
    product_id: int | None
    product_name: str
    pack_size: int | None
    item_size: Decimal | None = None
    size_unit: str = ""


@dataclass(frozen=True)
class ItemOf:
    """What one item (« une bouteille ») of an article is at a store
    (`article_item`): an item of the store's usual product (`product_id`,
    its name and pack), or of the format the article is most bought in
    anywhere (no product). `size` is in `unit`, the article's; None - and
    `unit` "" - when the usual product is weighed: a bare number of it."""

    size: Decimal | None
    unit: str
    product_id: int | None
    product_name: str
    pack_size: int | None


@dataclass(frozen=True)
class ListEntry:
    """One entry of a store's add menu: what the datalist offers (`name`, the
    stock take's: `entries.product_display_name` or
    `entries.stock_type_entry_name`) and what the page's island says of it -
    its units in the order offered, the default, and each unit's French
    label."""

    name: str
    kind: str  # entries.PRODUCT_KIND | entries.ARTICLE_KIND
    units: EntryUnits
    labels: tuple[tuple[str, str], ...]  # (value, label), in units.choices' order

    def as_data(self) -> dict:
        """The island's shape, the stock take's (`stock_take_entry_lookup`)
        but its availability: {"kind", "unit_choices": [[value, label]],
        "default_unit"}."""
        return {
            "kind": self.kind,
            "unit_choices": [[value, label] for value, label in self.labels],
            "default_unit": self.units.default,
        }


@dataclass(frozen=True)
class Finished:
    """What `finish` did: `ticked` of `total` items bought, `carried` copied
    to the next list."""

    ticked: int
    total: int
    carried: int


# --------------------------------------------------------------------- the stores
def _bought_at_the_store():
    """A positive PURCHASE movement of an invoice line of the outer
    supplier."""
    return StockMovement.objects.filter(
        kind=MovementKind.PURCHASE, quantity__gt=0, invoice_line__invoice__supplier=OuterRef("pk")
    )


def _offered() -> Q:
    return Q(expenses_only=False) & ~Q(parser_key=LLM_PARSER_KEY) & Q(Exists(_bought_at_the_store()))


def offered_stores() -> QuerySet[Supplier]:
    """The stores a list may be started for, in one query: bought at, neither
    a supplier of charges nor the AI pseudo-supplier - « Prévoir les
    courses »' own rule (shopping_data.offered_stores)."""
    return Supplier.objects.filter(_offered())


def _holds_items():
    """An item of the outer list."""
    return ShoppingListItem.objects.filter(shopping_list=OuterRef("pk"))


def store_of(asked) -> Supplier | None:
    """The store `asked` (an id read from a request) names: one offered, or
    one with a list in progress - an open list holding an item: a store whose
    documents went elsewhere keeps such a list reachable, an emptied one
    does not. Anything else None, with no query for what is no id. ONE
    query."""
    if not is_id(asked):
        return None
    in_progress_here = ShoppingList.objects.filter(
        Exists(_holds_items()), supplier=OuterRef("pk"), finished_at__isnull=True
    )
    return Supplier.objects.filter(Q(pk=int(asked)) & (_offered() | Q(Exists(in_progress_here)))).first()


# --------------------------------------------------------------------- the open list
def open_list_of(store_id: int) -> ShoppingList | None:
    """The store's open list, if it has one. Reads only."""
    return ShoppingList.objects.filter(supplier_id=store_id, finished_at__isnull=True).first()


def _open_list(store_id: int, by: str) -> ShoppingList:
    shopping_list = open_list_of(store_id)
    if shopping_list is not None:
        return shopping_list
    try:
        # A savepoint: the IntegrityError of a list made meanwhile leaves the
        # caller's transaction usable.
        with transaction.atomic():
            return ShoppingList.objects.create(supplier_id=store_id, created_by=by)
    except IntegrityError:
        made = open_list_of(store_id)
        if made is None:
            raise
        return made


def open_list_for(store, by: str) -> ShoppingList:
    """The store's open list, else a new one made by `by` - another request
    making it meanwhile, its list is returned, never a second one."""
    return _open_list(store.pk, by)


# --------------------------------------------------------------------- the figures
def _quantized(quantity: Decimal) -> Decimal:
    """Three places, half up: a median of two purchases can carry a fourth."""
    return Decimal(quantity).quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def fits(quantity) -> bool:
    """Whether `quantity` can be stored as an item's quantity: a number
    (Decimal or int) above 0, exact to QUANTITY_PLACES and under
    QUANTITY_LIMIT - checked on the figure as it will be stored, so after
    any rounding (9 999 999,9995 rounds up to 10 000 000). Never truncated:
    SQLite keeps a wider figure without a word and every later read of the
    row raises (« A figure wider than the column behind it »)."""
    if isinstance(quantity, bool) or not isinstance(quantity, Decimal | int):
        return False
    quantity = Decimal(quantity)
    return quantity.is_finite() and 0 < quantity < QUANTITY_LIMIT and quantity == quantity.quantize(_QUANTUM)


def size_fits(size) -> bool:
    """Whether `size` can be stored as an item's size (`item_size`, 10
    digits, 4 places): a number (Decimal or int) above 0, exact to
    entries.SIZE_PLACES and under entries.SIZE_LIMIT - what
    `entries.quantized_size` gives. The size's twin of `fits`."""
    if isinstance(size, bool) or not isinstance(size, Decimal | int):
        return False
    size = Decimal(size)
    return size.is_finite() and 0 < size < entries.SIZE_LIMIT and size == size.quantize(entries.SIZE_QUANTUM)


def _size_problem(figures: Figures) -> str | None:
    """Why `figures`' size could not be stored (the item's check
    constraint, and its column), or None: a bug of the caller's, said in
    English."""
    if figures.item_size is None:
        return f"a size unit {figures.size_unit!r} with no size" if figures.size_unit else None
    if not size_fits(figures.item_size):
        return f"a size {figures.item_size!r} its column cannot hold"
    if figures.size_unit not in UnitChoices.values:
        return f"a size in {figures.size_unit!r}, no unit the app knows"
    if figures.unit:
        return f"a size beside a quantity of {figures.unit!r}: only a number of items has one"
    return None


def _pack(packs) -> int | None:
    """A forecast's (packs, colisage) as an item's pack size: the colisage
    when it is a whole pack of several a form could post (PACK_RANGE), else
    None."""
    if not packs:
        return None
    size = Decimal(packs[1])
    if size != size.to_integral_value():
        return None
    low, high = PACK_RANGE
    return int(size) if low <= size <= high else None


def line_figures(line, sizes: Mapping[int, Decimal] | None = None) -> Figures:
    """A forecast line (`shopping.Line`) as an item counts it: its product's
    units, a typed horizon's multiplier included, when the store's usual
    product is known - with the size of one, in the article's unit, when
    `sizes` ({product: size}, `entries.product_item_sizes`) knows it; else
    the article's units, never a size. A misread purchase can make it wider
    than an item's column: given as it is, `add_item` refuses it."""
    units = line.total_product_units
    if units is not None:
        size = None
        if sizes and line.product_id is not None:
            size = entries.quantized_size(sizes.get(line.product_id))
        return Figures(
            _quantized(units),
            "",
            line.product_id,
            line.product_name,
            _pack(line.packs),
            size,
            line.unit if size is not None else "",
        )
    return Figures(_quantized(line.total_qty), line.unit, None, "", None)


def figures_of_usual(usual, article) -> Figures:
    """One usual purchase (`shopping.UsualPurchase`) of `article` as an item
    counts it: its product's units when it counts them, else the article's
    own units - three places, half up. `usual_figures`' rule, apart so that
    the add menu's store-wide reading (`list_entries`) converts the same."""
    if usual.product_units is not None:
        return Figures(_quantized(usual.product_units), "", usual.product_id, usual.product_name, _pack(usual.packs))
    return Figures(_quantized(usual.qty), article.unit, None, "", None)


def usual_figures(today, store, article) -> Figures | None:
    """What one purchase of `article` at `store` usually is, as an item counts
    it (the forecast's figures with no typed horizon); None when it was never
    bought there. Like `line_figures`, it may be wider than an item's
    column, which `add_item` refuses."""
    from .shopping_data import usual_purchase_at

    usual = usual_purchase_at(today, store.pk, article)
    return None if usual is None else figures_of_usual(usual, article)


# --------------------------------------------------------------------- what one item is
def _counts_a_product(usual: Figures | None) -> TypeGuard[Figures]:
    """Whether the store's usual purchase counts its product (`unit` "")."""
    return usual is not None and not usual.unit and usual.product_id is not None


def article_item(article, usual: Figures | None, sizes: Mapping[int, Decimal], typical) -> ItemOf | None:
    """What one item (« une bouteille ») of `article` is at a store - THE
    rule, which the add menu and the add both go through, so what the select
    says is what gets stored. Reads nothing.

    1. The store's usual purchase (`usual`, `usual_figures`' answer) counts
       a product: an item is one of it, its size `sizes[product]`
       (`entries.item_size`) - none for a weighed product: a bare number.
    2. Otherwise an article in litres or kilos: the format it is most bought
       in anywhere (`typical`, `typical_item_sizes(..., discrete_only=True)`),
       with no product.
    3. Otherwise none: a UNIT article's unit already counts pieces.

    Every size goes through `entries.quantized_size` (four places, half up,
    0 < size < 10^6): one no column holds is no size."""
    if _counts_a_product(usual):
        size = entries.quantized_size(sizes.get(usual.product_id))
        return ItemOf(
            size,
            article.unit if size is not None else "",
            usual.product_id,
            usual.product_name,
            usual.pack_size,
        )
    if article.unit in MEASURED:
        size = entries.quantized_size(typical)
        if size is not None:
            return ItemOf(size, article.unit, None, "", None)
    return None


def article_item_of(article, usual: Figures | None) -> ItemOf | None:
    """`article_item` for one article, what it needs read: the usual
    product's size when `usual` counts one (two queries,
    `entries.product_item_sizes`); else, for an article in litres or kilos,
    its most-bought format (`typical_item_sizes`, three queries); nothing
    for a UNIT article. `usual` is `usual_figures`' answer, read by the
    caller."""
    if _counts_a_product(usual):
        return article_item(article, usual, entries.product_item_sizes([usual.product_id]), None)
    if article.unit in MEASURED:
        typical = typical_item_sizes([article.pk], discrete_only=True).get(article.pk)
        return article_item(article, usual, {}, typical)
    return None


# --------------------------------------------------------------------- an entry's units
def entry_units(entry: Entry, *, usual: Figures | None, item: ItemOf | None, discrete: bool) -> EntryUnits:
    """The units `entry` offers and the one taken when none is chosen
    (SPEC_UNITS §2.3). A product follows the stock take's rule
    (`entries.product_units`, `discrete` from
    `services.is_discrete_count`). An article counts its own unit, and items
    first when one is known (`item`, `article_item`'s): by default where the
    store's usual purchase (`usual`) counts a product, else its measure as
    today; never bought there, items for the units of ITEMS_BY_DEFAULT. A
    UNIT article counts items alone."""
    if entry.kind == PRODUCT_KIND:
        return entries.product_units(_product_of(entry), is_discrete=discrete)
    article = entry.article
    if item is None:
        return entries.article_units(article)
    by_default = _counts_a_product(usual) if usual is not None else article.unit in ITEMS_BY_DEFAULT
    return entries.article_units(article, items=True, items_by_default=by_default)


def _product_of(entry: Entry) -> Product:
    """A product entry's product: `entries.EntryResolver` never makes one
    without."""
    if entry.product is None:
        raise ValueError(f"A product entry of {entry.article.name!r} names no product.")
    return entry.product


def entry_labels(entry: Entry, units: EntryUnits, *, item: ItemOf | None, product_size) -> tuple[tuple[str, str], ...]:
    """Each unit `units` offers with its French label (`unit_label`), in
    their order: an item of a product entry is one of it (`product_size`,
    `entries.item_size`), an article's is `item` - each named after what it
    is (a bottle, a keg, a pack: its product's name)."""
    article = entry.article
    if entry.kind == PRODUCT_KIND:
        size, size_unit = product_size, article.unit if product_size is not None else ""
        product_name = _product_of(entry).raw_name
    elif item is not None:
        size, size_unit, product_name = item.size, item.unit, item.product_name
    else:
        size, size_unit, product_name = None, "", ""
    return tuple(
        (value, unit_label(value, article_unit=article.unit, size=size, size_unit=size_unit, product_name=product_name))
        for value in units.choices
    )


def entry_figures(
    entry: Entry | None,
    unit: str,
    quantity: Decimal | None,
    *,
    usual: Figures | None,
    item: ItemOf | None,
    product_size,
) -> Figures:
    """What is stored for `entry` counted in `unit` (SPEC_UNITS §2.4):
    `quantity` as typed, or with nothing typed the store's usual figure in
    that unit - never converted. `usual` is `usual_figures`' answer for the
    entry's article, `item` `article_item`'s, `product_size` the product
    entry's `entries.item_size`. `entry` None is a free text, which counts
    no unit.

    A unit the entry does not offer raises ValueError: the caller checks
    the posted unit against `entry_units` first."""
    if entry is None:
        if unit:
            raise ValueError(f"A free text counts no unit, not {unit!r}.")
        return Figures(_typed_or(quantity, Decimal(1)), "", None, "", None)
    article = entry.article
    if entry.kind == PRODUCT_KIND:
        return _product_figures(_product_of(entry), article, unit, quantity, usual, product_size)
    if unit == ITEMS:
        return _article_items(article, quantity, usual, item)
    if unit == article.unit:
        return Figures(_typed_or(quantity, _usual_measure(usual, item)), article.unit, None, "", None)
    raise ValueError(f"{article.name!r} offers no unit {unit!r}.")


def _typed_or(quantity: Decimal | None, default: Decimal) -> Decimal:
    return quantity if quantity is not None else default


def _product_figures(product, article, unit: str, quantity, usual: Figures | None, product_size) -> Figures:
    """A product entry: items of it - the store's usual count and pack when
    the usual purchase counts this very product, else 1 and no pack - its
    size known or not; or its article's measure, the product kept as a hint
    (the stock take's « mesuré directement »): with nothing typed, the usual
    count times the size of one when it is the usual product here and its
    size is known (the article entry's `_usual_measure`), else 1."""
    its_own = usual if _counts_a_product(usual) and usual.product_id == product.pk else None
    if unit == ITEMS:
        return Figures(
            _typed_or(quantity, its_own.quantity if its_own is not None else Decimal(1)),
            "",
            product.pk,
            product.raw_name,
            its_own.pack_size if its_own is not None else None,
            product_size,
            article.unit if product_size is not None else "",
        )
    if unit == article.unit:
        usual_measure = (
            _quantized(its_own.quantity * product_size)
            if its_own is not None and product_size is not None
            else Decimal(1)
        )
        return Figures(_typed_or(quantity, usual_measure), article.unit, product.pk, product.raw_name, None)
    raise ValueError(f"{product.raw_name!r} offers no unit {unit!r}.")


def _article_items(article, quantity, usual: Figures | None, item: ItemOf | None) -> Figures:
    """An article counted in items: of its usual product, of its format, or
    - a UNIT article with no item - its own units, as today."""
    if item is None:
        if article.unit != UnitChoices.UNIT:
            raise ValueError(f"{article.name!r} has no item to count: no {ITEMS!r}.")
        return Figures(_typed_or(quantity, usual.quantity if usual is not None else Decimal(1)), ITEMS, None, "", None)
    if item.product_id is not None:
        default = usual.quantity if _counts_a_product(usual) else Decimal(1)
        return Figures(
            _typed_or(quantity, default),
            "",
            item.product_id,
            item.product_name,
            item.pack_size,
            item.size,
            item.unit if item.size is not None else "",
        )
    return Figures(_typed_or(quantity, Decimal(1)), "", None, "", None, item.size, item.unit)


def _usual_measure(usual: Figures | None, item: ItemOf | None) -> Decimal:
    """An article's usual purchase at the store in its own unit: as bought
    when bought by measure; its usual items times the size of one, three
    places half up, when it counts items of a known size; else 1."""
    if usual is not None and usual.unit:
        return usual.quantity
    if _counts_a_product(usual) and item is not None and item.size is not None:
        return _quantized(usual.quantity * item.size)
    return Decimal(1)


# --------------------------------------------------------------------- the card
def _present(item) -> str:
    """The unit an item counts now, as a select offers it: ITEMS for a
    number of items (`unit` ""), else its own."""
    return item.unit or ITEMS


def card_item(item, item_now: ItemOf | None, own: ItemOf | None = None) -> ItemOf | None:
    """What the card's items option counts for `item` - THE rule its units
    (`card_units`), its labels (`card_labels`) and what it stores
    (`card_figures`) go through, so the label always says what the save
    stores. None for a free text, which counts what it names.

    - The item counts items now: its own terms (its size, its product, its
      pack).
    - It counts a measure and names a product the item known now
      (`item_now`, `article_item`'s for its article at the store) does not -
      another product of the store, or `item_now` a format or nothing: that
      product, found again at the store with its size (`own`, the caller's
      read); not found, a bare number of it - when something is known now,
      or for a UNIT article, whose unit counts pieces anyway; else nothing:
      no items to offer.
    - Otherwise `item_now`: the usual product's bottles here, or the format
      the article is most bought in; None when nothing is known."""
    if item.stock_type_id is None:
        return None
    if _present(item) == ITEMS:
        return ItemOf(item.item_size, item.size_unit, None, item.product_name, item.pack_size)
    if item.product_name and (item_now is None or item_now.product_name != item.product_name):
        if own is not None:
            return own
        if item_now is None and item.stock_type.unit != UnitChoices.UNIT:
            return None
        return ItemOf(None, "", None, item.product_name, item.pack_size)
    return item_now


def card_units(item, item_now: ItemOf | None, own: ItemOf | None = None) -> EntryUnits | None:
    """The card's units for `item` (SPEC_UNITS §2.6), its present terms
    selected; None for a free text, which keeps its number. Items when the
    card knows what one is (`card_item`), the article's unit, and the item's
    own measure when the article's unit was edited since - saving the card
    unchanged is never refused. A UNIT article counts items alone."""
    if item.stock_type_id is None:
        return None
    article = item.stock_type
    present = _present(item)
    choices: list[str] = []
    if article.unit == UnitChoices.UNIT or card_item(item, item_now, own) is not None:
        choices.append(ITEMS)
    if article.unit not in choices:
        choices.append(article.unit)
    if present not in choices:
        choices.append(present)
    return EntryUnits(tuple(choices), present)


def card_labels(
    item, units: EntryUnits | None, item_now: ItemOf | None, own: ItemOf | None = None
) -> tuple[tuple[str, str], ...]:
    """Each of `units` (`card_units`') with its label: items say what
    `card_item` counts - the item's own size when it counts items now, the
    size of what saving them stores otherwise, « unités » for a bare number;
    the measures, whether a size is known."""
    if units is None:
        return ()
    article = item.stock_type
    target = card_item(item, item_now, own)
    if target is not None:
        size, size_unit, product_name = target.size, target.unit, target.product_name
    else:
        size, size_unit, product_name = None, "", ""
    known = size if size is not None else (item_now.size if item_now is not None else None)
    labels = []
    for value in units.choices:
        if value == ITEMS:
            label = unit_label(
                value, article_unit=article.unit, size=size, size_unit=size_unit, product_name=product_name
            )
        else:
            label = unit_label(value, article_unit=article.unit, size=known)
        labels.append((value, label))
    return tuple(labels)


def card_figures(
    item, unit: str | None, quantity: Decimal, item_now: ItemOf | None, own: ItemOf | None = None
) -> Figures:
    """What the card stores for `item` posted with `unit` (SPEC_UNITS §2.6),
    the number never converted:

    - no unit, or its present terms: only the quantity changes;
    - items, or one measure, to a measure: that measure, the product's name
      kept, the packs and the sizes cleared;
    - a measure to items: what `card_item` counts - the item known now, or
      the item's own product (found again with its size, `own`, else a bare
      number of it); a UNIT article with neither counts its own units.

    A unit `card_units` does not offer - any for a free text - raises
    ValueError: the caller refuses it first."""
    units = card_units(item, item_now, own)
    if units is None:
        if unit:
            raise ValueError(f"A free text counts no unit, not {unit!r}.")
        return _kept(item, quantity)
    asked = unit or units.default
    if asked not in units.choices:
        raise ValueError(f"The card offers no unit {asked!r} for {item.label!r}.")
    if asked == _present(item):
        return _kept(item, quantity)
    if asked != ITEMS:
        return Figures(quantity, asked, None, item.product_name, None)
    target = card_item(item, item_now, own)
    if target is not None:
        return Figures(
            quantity,
            "",
            target.product_id,
            target.product_name,
            target.pack_size,
            target.size,
            target.unit if target.size is not None else "",
        )
    return Figures(quantity, UnitChoices.UNIT, None, "", None)


def _kept(item, quantity: Decimal) -> Figures:
    """`item`'s own terms, `quantity` in place of its number."""
    return Figures(quantity, item.unit, None, item.product_name, item.pack_size, item.item_size, item.size_unit)


# --------------------------------------------------------------------- the add menu
def _menu_order(name: str) -> tuple[str, str]:
    return search_key(name), name


def _bought_here(store):
    """A positive PURCHASE movement of the outer article at `store`."""
    return StockMovement.objects.filter(
        stock_type=OuterRef("pk"),
        kind=MovementKind.PURCHASE,
        quantity__gt=0,
        invoice_line__invoice__supplier_id=store.pk,
    )


def list_entries(today, store) -> list[ListEntry]:
    """The add form's menu at `store`: every article, and the store's
    classified products (another store's product is none here), each with
    the units it offers and their labels - through the chain the add goes
    through (`figures_of_usual`, `article_item`, `entry_units`,
    `entry_labels`), so what the page says is what gets stored.

    The articles bought here first, then the store's products, then the
    other articles; each group in `search_key` order of the entry's name.

    Seven queries whatever the history: the articles (bought here or not),
    the store's classified products, their ratios, ONE scan of the store's
    purchases (`shopping_data.usual_purchases_at`), and the formats of the
    articles in litres or kilos that no product counts here
    (`typical_item_sizes`, three - none when there is none)."""
    from .shopping_data import usual_purchases_at

    articles = list(StockType.objects.annotate(here=Exists(_bought_here(store))).order_by())
    products = list(
        Product.objects.filter(supplier_id=store.pk, stock_type__isnull=False)
        .select_related("supplier", "stock_type")
        .only(
            "id",
            "raw_name",
            "unit",
            "stock_equivalent",
            "supplier",
            "supplier__name",
            "stock_type",
            "stock_type__name",
            "stock_type__unit",
        )
        .order_by()
    )
    ratios = product_counting_ratios([product.pk for product in products])
    here = [article for article in articles if article.here]
    # The usual purchases of every article bought here, in one scan; a
    # product bought here is one of the store's.
    read = usual_purchases_at(today, store.pk, here, {product.pk: product.raw_name for product in products})
    usuals = {article.pk: figures_of_usual(read[article.pk], article) for article in here if article.pk in read}
    sizes = {}
    for product in products:
        size = entries.item_size(product, ratios)
        if size is not None:
            sizes[product.pk] = size
    measured_alone = [
        article.pk for article in articles if article.unit in MEASURED and not _counts_a_product(usuals.get(article.pk))
    ]
    typical = typical_item_sizes(measured_alone, discrete_only=True) if measured_alone else {}

    def of_article(article) -> ListEntry:
        usual = usuals.get(article.pk)
        item = article_item(article, usual, sizes, typical.get(article.pk))
        entry = Entry(ARTICLE_KIND, article, None)
        units = entry_units(entry, usual=usual, item=item, discrete=False)
        labels = entry_labels(entry, units, item=item, product_size=None)
        return ListEntry(entries.stock_type_entry_name(article), ARTICLE_KIND, units, labels)

    def of_product(product) -> ListEntry:
        entry = Entry(PRODUCT_KIND, product.stock_type, product)
        units = entry_units(entry, usual=None, item=None, discrete=is_discrete_count(ratios, product.pk))
        labels = entry_labels(entry, units, item=None, product_size=sizes.get(product.pk))
        return ListEntry(entries.product_display_name(product), PRODUCT_KIND, units, labels)

    def ordered(found: list[ListEntry]) -> list[ListEntry]:
        return sorted(found, key=lambda entry: _menu_order(entry.name))

    return [
        *ordered([of_article(article) for article in here]),
        *ordered([of_product(product) for product in products]),
        *ordered([of_article(article) for article in articles if not article.here]),
    ]


def list_aliases(store, menu: list[ListEntry]) -> dict[str, dict[str, str]]:
    """The names the add form accepts beside its menu's (`menu`,
    `list_entries`), each to the menu name it resolves to - for the unit
    select to follow a name typed the way the lists always took it (« vodka
    exemple », a product's raw name): {"exact": {name: menu name},
    "folded": {same_name key: menu name}}.

    Never a second rule: every alias is answered by the add's own resolver
    (`entries.EntryResolver`, scoped to the store, forgiving) and kept only
    when it names the very entry its menu name does.
    - Exact: each article's own name, each of the store's products' raw
      name, as `clean_text` leaves it.
    - Folded (`same_name`): those and the menu names, grouped by the key
      they fold to; a key is kept when its group holds ONE article (an
      article wins over a product, `EntryResolver`'s rule 4 before 5), or no
      article and ONE product - two articles reading alike, or two
      products, leave it out, as the resolver finds neither.
    So any name the select follows, the add resolves to the entry the
    select's units are; a name it does not know keeps « habituelle ». Two
    queries (the resolver's), whatever the menu."""
    resolver = entries.EntryResolver(supplier_id=store.pk)
    names = {listed.name for listed in menu}
    exact: dict[str, str] = {}
    groups: dict[str, dict[str, set[str]]] = {}
    for listed in menu:
        entry = resolver.resolve(listed.name)
        if entry is None or entry.kind != listed.kind:
            continue
        own = entry.product.raw_name if entry.product is not None else entry.article.name
        if own not in names and clean_text(own) == own and resolver.resolve(own, forgiving=True) == entry:
            exact[own] = listed.name
        for candidate in (listed.name, own):
            group = groups.setdefault(same_name(candidate), {ARTICLE_KIND: set(), PRODUCT_KIND: set()})
            group[listed.kind].add(listed.name)
    folded: dict[str, str] = {}
    for key, group in groups.items():
        articles, products = group[ARTICLE_KIND], group[PRODUCT_KIND]
        found = articles if articles else products
        if not key or len(found) != 1:
            continue
        (name,) = found
        if resolver.resolve(key, forgiving=True) == resolver.resolve(name):
            folded[key] = name
    return {"exact": exact, "folded": folded}


# --------------------------------------------------------------------- what is typed
def clean_text(typed) -> str:
    """A typed name or note: every control character a space, the spaces
    collapsed. "" for what is no text."""
    if not isinstance(typed, str):
        return ""
    spaced = "".join(" " if unicodedata.category(character) == "Cc" else character for character in typed)
    return " ".join(spaced.split())


def read_quantity(typed) -> Decimal | None:
    """A typed quantity, exact to QUANTITY_PLACES and fitting the column, and
    above 0; else None. « 1,500 » is 1.5 (read_number's rule): a quantity,
    not an amount."""
    quantity = read_amount(typed, QUANTITY_PLACES, digits=QUANTITY_DIGITS)
    return quantity if quantity is not None and quantity > 0 else None


# --------------------------------------------------------------------- the writes
def _stands_for(item: ShoppingListItem) -> tuple:
    """What an item of a list is told apart by: its article, else its free
    text read with case, accents and spacing ignored."""
    if item.stock_type_id is not None:
        return ("article", item.stock_type_id)
    return ("text", same_name(item.label))


def _listed(shopping_list: ShoppingList, stock_type, label: str) -> ShoppingListItem | None:
    """The item already standing for what is being added: the same article,
    or a free text reading the same - one still to buy before a ticked one
    (an article deleted can leave two lines reading alike)."""
    if stock_type is not None:
        return shopping_list.items.filter(stock_type=stock_type).first()
    key = same_name(label)
    free_texts = shopping_list.items.filter(stock_type__isnull=True).order_by("added_at", "pk")
    alike = [item for item in free_texts if same_name(item.label) == key]
    return next((item for item in alike if item.checked_at is None), alike[0] if alike else None)


def _still_open(shopping_list: ShoppingList) -> bool:
    return ShoppingList.objects.filter(pk=shopping_list.pk, finished_at__isnull=True).exists()


def _start_again_if_empty(shopping_list: ShoppingList, by: str) -> None:
    """An open list holding no item - emptied by « Retirer » - is started
    again by the item about to be added: its start and who started it are
    that item's, never a first item long gone."""
    now = timezone.now()
    if ShoppingList.objects.filter(~Exists(_holds_items()), pk=shopping_list.pk).update(created_at=now, created_by=by):
        shopping_list.created_at, shopping_list.created_by = now, by


def _found(
    item: ShoppingListItem, shopping_list: ShoppingList, *, figures: Figures, note: str, relist: bool
) -> tuple[ShoppingListItem, AddOutcome] | None:
    """(item, outcome) for the item already standing for what is added: to
    buy, ALREADY, unchanged; ticked, put back - unticked, `figures` written
    over its own (its unit, product and sizes together, as the check
    constraint wants them) and `note` when one is typed, in ONE UPDATE of an
    item still ticked on an open list -, RELISTED (ALREADY, left bought,
    without `relist`). None when its list was finished meanwhile, or the
    item unticked or removed meanwhile: the caller looks again."""
    if item.checked_at is None or not relist:
        return (item, AddOutcome.ALREADY) if _still_open(shopping_list) else None
    changes = {
        "checked_at": None,
        "checked_by": "",
        "quantity": figures.quantity,
        "unit": figures.unit,
        "product_name": figures.product_name,
        "pack_size": figures.pack_size,
        "item_size": figures.item_size,
        "size_unit": figures.size_unit,
    }
    if note:
        changes["note"] = note
    put_back = ShoppingListItem.objects.filter(
        pk=item.pk, shopping_list__finished_at__isnull=True, checked_at__isnull=False
    ).update(**changes)
    if not put_back:
        return None
    item.refresh_from_db()
    return item, AddOutcome.RELISTED


def _add_to(
    shopping_list: ShoppingList, *, by: str, label: str, figures: Figures, stock_type, note: str, relist: bool
) -> tuple[ShoppingListItem, AddOutcome] | None:
    """`add_item` on one open list as read; None when it was finished before
    the write (the caller looks for the store's open list again)."""
    already = _listed(shopping_list, stock_type, label)
    if already is not None:
        return _found(already, shopping_list, figures=figures, note=note, relist=relist)
    try:
        # A savepoint: the IntegrityError of the same article added
        # meanwhile leaves the caller's transaction usable.
        with transaction.atomic():
            # Checked again where it is written, as every other write of a
            # list is filtered on an open one: a « Courses terminées » between
            # the look and here sends the add to the store's current list.
            if not _still_open(shopping_list):
                return None
            _start_again_if_empty(shopping_list, by)
            item = ShoppingListItem.objects.create(
                shopping_list=shopping_list,
                stock_type=stock_type,
                label=label,
                product_name=figures.product_name,
                pack_size=figures.pack_size,
                quantity=figures.quantity,
                unit=figures.unit,
                item_size=figures.item_size,
                size_unit=figures.size_unit,
                note=note,
                added_by=by,
            )
    except IntegrityError:
        already = _listed(shopping_list, stock_type, label)
        if already is None:
            raise
        return _found(already, shopping_list, figures=figures, note=note, relist=relist)
    return item, AddOutcome.ADDED


def add_item(
    store, *, by: str, label: str, figures: Figures, stock_type=None, note: str = "", relist: bool = True
) -> tuple[ShoppingListItem, AddOutcome]:
    """(item, outcome): `label` with its `figures` on the store's open list -
    made by this first item when there is none, started again by it when
    emptied (its `created_at` and `created_by` this add's). Already there -
    the same article, or a free text reading the same -: still to buy,
    (that item, ALREADY), unchanged, so a double submit adds nothing;
    ticked (bought), (that item, RELISTED), put back to buy: unticked, the
    quantity, unit, product, pack and sizes of `figures` written over its
    own, and `note` when one is typed - who added it and when kept. With
    `relist` false a ticked item is left bought: ALREADY. So too when another
    request adds the same article meanwhile.

    One transaction looks and writes: under production's IMMEDIATE mode it
    holds the write lock from its start, so two adds of one free text, or
    an add and a `finish`, follow one another. The savepoint that writes
    checks again that the list is still open, and a list finished meanwhile
    sends the add to the store's current open list - the one carried over,
    or a new one -, never onto a finished list.

    A quantity the column cannot hold (`fits`) raises QuantityTooWide (a
    ValueError, its message French, naming `label`) before anything is read
    or written: a caller's transaction stays usable. So does a size the
    column or the check constraint cannot hold (`size_fits`, both or
    neither, only beside `unit` ""), as a plain ValueError: only code makes
    one - every size goes through `entries.quantized_size` first."""
    if not fits(figures.quantity):
        raise QuantityTooWide(QUANTITY_TOO_WIDE.format(name=label))
    problem = _size_problem(figures)
    if problem is not None:
        raise ValueError(f"« {label} »: {problem}; nothing was written.")
    with transaction.atomic():
        for _attempt in range(ADD_ATTEMPTS):
            done = _add_to(
                open_list_for(store, by),
                by=by,
                label=label,
                figures=figures,
                stock_type=stock_type,
                note=note,
                relist=relist,
            )
            if done is not None:
                return done
    # Lists finished under the add again and again: under IMMEDIATE
    # transactions it cannot happen; refused rather than written anywhere.
    raise RuntimeError(f"The open list of store {store.pk} was finished {ADD_ATTEMPTS} times under one add.")


def set_ticked(item_pk: int, wanted: bool, by: str, now) -> bool:
    """The item ticked (bought) or not, as WANTED - never a toggle - in ONE
    UPDATE of an item of an open list. Ticked again, it keeps the first
    tick's who and when. False when the list was finished meanwhile, or the
    item went."""
    rows = ShoppingListItem.objects.filter(pk=item_pk, shopping_list__finished_at__isnull=True)
    if not wanted:
        return bool(rows.update(checked_at=None, checked_by=""))
    # Every SET reads the row as it was: `checked_by` is written only where
    # no tick was.
    return bool(
        rows.update(
            checked_at=Coalesce(F("checked_at"), Value(now, output_field=DateTimeField())),
            checked_by=Case(When(checked_at__isnull=True, then=Value(by)), default=F("checked_by")),
        )
    )


def finish(shopping_list: ShoppingList, *, keep: bool, by: str, now) -> Finished | None:
    """The list finished by `by` at `now`; with `keep`, a copy of each
    unticked item - figures and sizes, note, who added it and when, never a
    tick - on the store's next open list, in their order, an article already
    there skipped (and a free text reading the same). None when it was
    finished already: a double submit carries nothing twice. One
    transaction."""
    with transaction.atomic():
        closed = ShoppingList.objects.filter(pk=shopping_list.pk, finished_at__isnull=True).update(
            finished_at=now, finished_by=by
        )
        if not closed:
            return None
        items = list(ShoppingListItem.objects.filter(shopping_list_id=shopping_list.pk).order_by("added_at", "pk"))
        unticked = [item for item in items if item.checked_at is None]
        carried = 0
        if keep and unticked:
            following = _open_list(shopping_list.supplier_id, by)
            there = {_stands_for(item) for item in following.items.all()}
            copies = [
                ShoppingListItem(
                    shopping_list=following,
                    stock_type_id=item.stock_type_id,
                    label=item.label,
                    product_name=item.product_name,
                    pack_size=item.pack_size,
                    quantity=item.quantity,
                    unit=item.unit,
                    item_size=item.item_size,
                    size_unit=item.size_unit,
                    note=item.note,
                    added_at=item.added_at,
                    added_by=item.added_by,
                )
                for item in unticked
                if _stands_for(item) not in there
            ]
            ShoppingListItem.objects.bulk_create(copies)
            carried = len(copies)
        return Finished(ticked=len(items) - len(unticked), total=len(items), carried=carried)


def _joined_notes(kept: str, added: str) -> str:
    if not added or added == kept:
        return kept
    if not kept:
        return added
    joined = f"{kept}{NOTE_SEPARATOR}{added}"
    return joined if len(joined) <= NOTE_MAX else joined[: NOTE_MAX - 1] + "…"


def _counted_thing(item: ShoppingListItem) -> tuple:
    """What an item's number counts: its unit, its product, and the size of
    one item and its unit - two items count the same thing only when all
    four agree (70 cl bottles are no litre ones)."""
    return item.unit, item.product_name, item.item_size, item.size_unit


def carry_on_merge(source: StockType, target: StockType) -> None:
    """The items of `source`, merged into `target`, list by list - the
    finished ones included: no target item on that list, the item now names
    the target (its label the target's name, everything else kept, its
    sizes included); a target item counting the same thing (`_counted_thing`:
    same unit, same product, same size of one item), one item - quantities
    added, notes joined, the target's pack, ticked only if both were; a
    target item counting something else - 70 cl bottles beside litre ones,
    or the same thing when the two quantities added would not fit the
    column (`fits`): stored, the row could never be read again -, the
    source's becomes a free text holding its name - both lines stay,
    nothing is lost. Before `source.delete()` (services.merge_stock_types,
    in its transaction)."""
    twins = {item.shopping_list_id: item for item in ShoppingListItem.objects.filter(stock_type=target)}
    for item in ShoppingListItem.objects.filter(stock_type=source).order_by("pk"):
        twin = twins.get(item.shopping_list_id)
        if twin is None:
            item.stock_type = target
            item.label = target.name
            item.save(update_fields=["stock_type", "label"])
        elif _counted_thing(twin) == _counted_thing(item) and fits(twin.quantity + item.quantity):
            twin.label = target.name
            twin.quantity += item.quantity
            twin.note = _joined_notes(twin.note, item.note)
            if twin.checked_at is None or item.checked_at is None:
                twin.checked_at, twin.checked_by = None, ""
            twin.save(update_fields=["label", "quantity", "note", "checked_at", "checked_by"])
            item.delete()
        else:
            item.stock_type = None
            item.label = source.name
            item.save(update_fields=["stock_type", "label"])


# --------------------------------------------------------------------- the words
def _as_size(size, size_unit: str) -> Decimal | None:
    """`size` as the size of an item said in words: a positive number in a
    unit with nouns - not a UNIT size of exactly 1, the piece the article
    counts (the beer's « 24 »). None otherwise."""
    if size is None or size_unit not in ITEM_NOUNS:
        return None
    size = Decimal(str(size))
    if not size.is_finite() or size <= 0:
        return None
    if size_unit == UnitChoices.UNIT and size == 1:
        return None
    return size


#: Where a product's name prints a count and the size of one, the groups
#: holding (count, size, its unit): « 6X75CL », « 25CLX24 », « 33 CL X 24 » -
#: quantity_extraction's own patterns.
_COUNT_AND_SIZE = ((COUNT_X_SIZE_RE, 1, 2, 3), (SIZE_X_COUNT_RE, 3, 1, 2), (SPACED_SIZE_X_COUNT_RE, 3, 1, 2))


def _printed_volumes(product_name: str) -> list[tuple[int, Decimal]]:
    """Each count and size of one, in litres, that `product_name` prints
    (« ROSE EXEMPLE CARTON 6X75CL »: (6, 0.75)); volumes only."""
    name = product_name.upper()
    found = []
    for pattern, count_at, size_at, unit_at in _COUNT_AND_SIZE:
        for match in pattern.finditer(name):
            factor = VOLUME_UNITS.get(match.group(unit_at))
            if factor is None:
                continue
            try:
                one = Decimal(match.group(size_at).replace(",", "."))
            except InvalidOperation:
                continue
            found.append((int(match.group(count_at)), one * factor))
    return found


def _is_pack(product_name: str, size: Decimal) -> bool:
    """Whether one item of `product_name`, `size` litres, is a pack: its name
    prints a count of several times a size, and one item holds more than
    that size - « ROSE EXEMPLE CARTON 6X75CL » bought by the 4.5 L carton.
    « GIN EXEMPLE 70CL X6 » counted by the 70 cl bottle is a bottle sold by
    six, not a pack."""
    if not product_name:
        return False
    return any(count > 1 and 0 < one < size for count, one in _printed_volumes(product_name))


def _nouns(size: Decimal, size_unit: str, product_name: str = "") -> tuple[str, str]:
    """(singular, plural) for an item of `size` `size_unit` - what the item
    is, read off its product's name when it has one: in litres a pack
    (`_is_pack`), else a bottle up to KEG_FROM litres and a keg (or a
    bag-in-box) above; a packet in kilos or pieces."""
    if size_unit == UnitChoices.LITRE:
        if _is_pack(product_name, size):
            return PACK_NOUNS
        if size > KEG_FROM:
            return KEG_NOUNS
    return ITEM_NOUNS[size_unit]


def size_words(size, unit: str) -> str:
    """One item's size: « 70 cl », « 37.5 cl » and « 1.5 L » for litres (cl
    under 1 L), « 500 g » and « 1 kg » for kilos, « 50 u. » for pieces.
    Numbers as every quantity on these pages (`plain_number`: a dot)."""
    size = Decimal(str(size))
    if unit == UnitChoices.LITRE:
        return f"{plain_number(size * 100)} cl" if size < 1 else f"{plain_number(size)} L"
    if unit == UnitChoices.KILOGRAM:
        return f"{plain_number(size * 1000)} g" if size < 1 else f"{plain_number(size)} kg"
    symbol = UNIT_SYMBOLS.get(unit, "")
    return f"{plain_number(size)} {symbol}" if symbol else plain_number(size)


def quantity_words(quantity, unit: str, item_size=None, size_unit: str = "", product_name: str = "") -> str:
    """« 24 », « 2 L », « 1.5 kg », « 12 u. »: a quantity and the article's
    unit - none when it counts the product (or a free text). Counting items
    of a known size, the items and their size: « 1 bouteille de 70 cl »,
    « 3 bouteilles de 70 cl », « 1 fût de 30 L », « 1 pack de 4.5 L » (the
    product `product_name` bought by the carton, `_nouns`), « 2 paquets de
    500 g » - singular below 2, the French rule; never for a UNIT size of
    1."""
    number = plain_number(quantity)
    if unit:
        symbol = UNIT_SYMBOLS.get(unit, "")
        return f"{number} {symbol}" if symbol else number
    size = _as_size(item_size, size_unit)
    if size is None:
        return number
    singular, plural = _nouns(size, size_unit, product_name)
    noun = singular if Decimal(str(quantity)) < 2 else plural
    return f"{number} {noun} de {size_words(size, size_unit)}"


def unit_label(value: str, *, article_unit: str, size=None, size_unit: str = "", product_name: str = "") -> str:
    """A unit as a select offers it, plural: items of a size « bouteilles de
    70 cl », « fûts de 30 L », « packs de 4.5 L », « paquets de 1 kg »,
    « paquets de 50 u. » - `size` in `size_unit`, the article's unit unless
    said, the noun what an item of `product_name` is (`_nouns`) -, or
    « unités » for items of no size (or a UNIT size of 1); a measure
    « litres », « kg », and for an article in litres with no item size known
    NO_FORMAT_WORDS."""
    if value == ITEMS:
        unit = size_unit or article_unit
        known = _as_size(size, unit)
        if known is None:
            return UNIT_WORDS[UnitChoices.UNIT]
        return f"{_nouns(known, unit, product_name)[1]} de {size_words(known, unit)}"
    if value == UnitChoices.LITRE and value == article_unit and size is None:
        return NO_FORMAT_WORDS
    return UNIT_WORDS.get(value, value)


def counted_unit_words(unit: str, item_size=None, size_unit: str = "", product_name: str = "") -> str:
    """What ONE of an item's number is, the number left out: « L », « kg »,
    « u. » for a unit; « bouteilles de 70 cl », « packs de 4.5 L » for items
    of a size (`unit_label`'s words); "" for a bare number - the card of a
    free text that still counts something says it (views._card_counts)."""
    if unit:
        return UNIT_SYMBOLS.get(unit, "")
    unit_of_size = size_unit if size_unit in ITEM_NOUNS else ""
    if _as_size(item_size, unit_of_size) is None:
        return ""
    return unit_label(
        ITEMS, article_unit=unit_of_size, size=item_size, size_unit=unit_of_size, product_name=product_name
    )


def pack_words(quantity, pack_size) -> str:
    """« 3 colis de 24 » when the quantity is a whole number of packs, else
    « à l'unité · colis de 24 » - never a bare « colis de 24 » under a
    number of units, which read as that many packs (« 2 » beside it bought
    two cartons for two bottles); "" with no pack of several."""
    if not pack_size or pack_size <= 1:
        return ""
    quantity = Decimal(str(quantity))
    size = plain_number(pack_size)
    if quantity > 0 and quantity % pack_size == 0:
        return f"{plain_number(quantity // pack_size)} colis de {size}"
    return f"{BY_THE_UNIT} · colis de {size}"


def run_order(items) -> list:
    """The tick page's order: unticked first, then ticked; each in the order
    they were added."""
    return sorted(items, key=lambda item: (item.checked_at is not None, item.added_at, item.pk))


def display_names(usernames, me: str, tenant_id) -> dict[str, str]:
    """Each username as a list says it, never an address - the owner's is
    his login, an employee's his contact, and every employee given the lists
    reads them: « Vous » for `me`; else, for a login of the espace
    `tenant_id`, its first name, or by its role when it has none (a signup
    sets none) OWNER_WORD or MEMBER_WORD; else GONE_WORD - a login removed,
    or another bar's, which an address reused would otherwise name. ONE
    query on the accounts database, none when every name is `me` or blank
    (or no espace is given)."""
    wanted = {name for name in usernames if name}
    others = {name for name in wanted if name != me}
    found: dict[str, str] = {}
    if others and tenant_id is not None:
        members = Membership.objects.filter(tenant_id=tenant_id, user__username__in=others)
        for username, first_name, role in members.values_list("user__username", "user__first_name", "role"):
            found[username] = first_name.strip() or (OWNER_WORD if role == Membership.Role.OWNER else MEMBER_WORD)
    return {name: YOU if name == me else found.get(name, GONE_WORD) for name in wanted}
