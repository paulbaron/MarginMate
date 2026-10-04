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
  reading the same (`search_key`), already on the open list to buy is
  answered as « already there » (ALREADY), unchanged - a double submit adds
  nothing. **A ticked (bought) one is put back to buy** (RELISTED): unticked,
  the figures asked for written over its own. The add looks and writes in ONE
  transaction, and checks again, in the savepoint that writes, that the list
  is still open: a list finished meanwhile sends it to the store's current
  open list - never onto a finished one. Under production's IMMEDIATE mode
  the write lock is taken before the look, so two adds of one free text, or
  an add and a « Courses terminées », follow one another.
- **What is written fits its column** (`fits`): a quantity wider than
  (10, 3) is stored by SQLite without a word and makes the row unreadable
  for good, so `add_item` refuses it (`QuantityTooWide`) and a merge keeps
  the two lines rather than write their sum.
- **A tick names the WANTED state** (`set_ticked`), never a toggle: two phones
  ticking two items both land, a second tick of a ticked item keeps the first
  one's who and when, and a finished list refuses it.
- **Finishing carries the rest over** (`finish`): the list is closed once - a
  double submit finds it closed and carries nothing twice - then, when asked,
  a copy of each unticked item goes to the store's next open list (an
  article already there skipped), its `added_at` kept. The finished list keeps
  every item as it was.
- **A merge keeps every line** (`carry_on_merge`, from
  `services.merge_stock_types`): an item of the merged article names the
  target; beside a target item counting the same thing, the two make one
  (unless their sum would not fit); beside one counting something else, it
  becomes a free text.
- **Who did what never shows an address** (`display_names`): a first name, a
  role in THIS espace, or « un ancien membre ».
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Case, DateTimeField, Exists, F, OuterRef, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import Membership
from common import is_id, plain_number, read_amount, search_key
from invoices.models import Supplier

from .models import MovementKind, ShoppingList, ShoppingListItem, StockMovement, StockType, UnitChoices

#: An article's unit as the pages write it after a quantity: « 3 L », « 12 u. ».
UNIT_SYMBOLS = {UnitChoices.LITRE: "L", UnitChoices.KILOGRAM: "kg", UnitChoices.UNIT: "u."}
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
    (an item keeps the name only); `pack_size` the product's colisage, a hint."""

    quantity: Decimal
    unit: str
    product_id: int | None
    product_name: str
    pack_size: int | None


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
    return Q(expenses_only=False) & Q(Exists(_bought_at_the_store()))


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


def line_figures(line) -> Figures:
    """A forecast line (`shopping.Line`) as an item counts it: its product's
    units, a typed horizon's multiplier included, when the store's usual
    product is known; else the article's units. A misread purchase can make
    it wider than an item's column: given as it is, `add_item` refuses it."""
    units = line.total_product_units
    if units is not None:
        return Figures(_quantized(units), "", line.product_id, line.product_name, _pack(line.packs))
    return Figures(_quantized(line.total_qty), line.unit, None, "", None)


def usual_figures(today, store, article) -> Figures | None:
    """What one purchase of `article` at `store` usually is, as an item counts
    it (the forecast's figures with no typed horizon); None when it was never
    bought there. Like `line_figures`, it may be wider than an item's
    column, which `add_item` refuses."""
    from .shopping_data import usual_purchase_at

    usual = usual_purchase_at(today, store.pk, article)
    if usual is None:
        return None
    if usual.product_units is not None:
        return Figures(_quantized(usual.product_units), "", usual.product_id, usual.product_name, _pack(usual.packs))
    return Figures(_quantized(usual.qty), article.unit, None, "", None)


# --------------------------------------------------------------------- what is typed
def _same(text: str) -> str:
    """What two names are compared by: case, accents and spacing ignored."""
    return search_key(" ".join(text.split()))


def find_article(typed: str) -> StockType | None:
    """The article `typed` names: its exact name, else the ONE article that
    reads the same with case, accents and spacing ignored. None (a free text)
    when none does, or several."""
    exact = StockType.objects.filter(name=typed).first()
    if exact is not None:
        return exact
    key = _same(typed)
    if not key:
        return None
    alike = [article for article in StockType.objects.order_by("pk") if _same(article.name) == key]
    return alike[0] if len(alike) == 1 else None


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
    return ("text", _same(item.label))


def _listed(shopping_list: ShoppingList, stock_type, label: str) -> ShoppingListItem | None:
    """The item already standing for what is being added: the same article,
    or a free text reading the same - one still to buy before a ticked one
    (an article deleted can leave two lines reading alike)."""
    if stock_type is not None:
        return shopping_list.items.filter(stock_type=stock_type).first()
    key = _same(label)
    free_texts = shopping_list.items.filter(stock_type__isnull=True).order_by("added_at", "pk")
    alike = [item for item in free_texts if _same(item.label) == key]
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
    over its own and `note` when one is typed, in ONE UPDATE of an item still
    ticked on an open list -, RELISTED (ALREADY, left bought, without
    `relist`). None when its list was finished meanwhile, or the item
    unticked or removed meanwhile: the caller looks again."""
    if item.checked_at is None or not relist:
        return (item, AddOutcome.ALREADY) if _still_open(shopping_list) else None
    changes = {
        "checked_at": None,
        "checked_by": "",
        "quantity": figures.quantity,
        "unit": figures.unit,
        "product_name": figures.product_name,
        "pack_size": figures.pack_size,
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
    quantity, unit, product and pack of `figures` written over its own, and
    `note` when one is typed - who added it and when kept. With `relist`
    false a ticked item is left bought: ALREADY. So too when another request
    adds the same article meanwhile.

    One transaction looks and writes: under production's IMMEDIATE mode it
    holds the write lock from its start, so two adds of one free text, or an
    add and a `finish`, follow one another. The savepoint that writes checks
    again that the list is still open, and a list finished meanwhile sends
    the add to the store's current open list - the one carried over, or a
    new one -, never onto a finished list.

    A quantity the column cannot hold (`fits`) raises QuantityTooWide (a
    ValueError, its message French, naming `label`) before anything is read
    or written: a caller's transaction stays usable."""
    if not fits(figures.quantity):
        raise QuantityTooWide(QUANTITY_TOO_WIDE.format(name=label))
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
    unticked item - figures, note, who added it and when, never a tick - on
    the store's next open list, in their order, an article already there
    skipped (and a free text reading the same). None when it was finished
    already: a double submit carries nothing twice. One transaction."""
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


def carry_on_merge(source: StockType, target: StockType) -> None:
    """The items of `source`, merged into `target`, list by list - the
    finished ones included: no target item on that list, the item now names
    the target (its label the target's name, everything else kept); a target
    item counting the same thing (same unit, same product), one item -
    quantities added, notes joined, the target's pack, ticked only if both
    were; a target item counting something else - or the same thing, when
    the two quantities added would not fit the column (`fits`): stored, the
    row could never be read again -, the source's becomes a free text
    holding its name - both lines stay, nothing is lost. Before
    `source.delete()` (services.merge_stock_types, in its transaction)."""
    twins = {item.shopping_list_id: item for item in ShoppingListItem.objects.filter(stock_type=target)}
    for item in ShoppingListItem.objects.filter(stock_type=source).order_by("pk"):
        twin = twins.get(item.shopping_list_id)
        if twin is None:
            item.stock_type = target
            item.label = target.name
            item.save(update_fields=["stock_type", "label"])
        elif (twin.unit, twin.product_name) == (item.unit, item.product_name) and fits(twin.quantity + item.quantity):
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
def quantity_words(quantity, unit: str) -> str:
    """« 24 », « 2 L », « 1.5 kg », « 12 u. »: a quantity and the article's
    unit - none when it counts the product (or a free text)."""
    number = plain_number(quantity)
    symbol = UNIT_SYMBOLS.get(unit, "") if unit else ""
    return f"{number} {symbol}" if symbol else number


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


def article_choices(store) -> tuple[list[str], list[str]]:
    """(the names of the articles bought at the store, the others), each in
    search_key order - the list page's menu, the store's own first. ONE
    query."""
    bought_here = StockMovement.objects.filter(
        stock_type=OuterRef("pk"),
        kind=MovementKind.PURCHASE,
        quantity__gt=0,
        invoice_line__invoice__supplier_id=store.pk,
    )
    here, others = [], []
    for name, at_the_store in StockType.objects.annotate(here=Exists(bought_here)).values_list("name", "here"):
        (here if at_the_store else others).append(name)
    return sorted(here, key=_menu_order), sorted(others, key=_menu_order)


def _menu_order(name: str) -> tuple[str, str]:
    return search_key(name), name
