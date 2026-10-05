from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class UnitChoices(models.TextChoices):
    LITRE = "L", "Litre"
    UNIT = "UNIT", "Unité"
    KILOGRAM = "KG", "Kilogramme"


#: How much of what's bought is assumed never to reach a glass, for a stock
#: item that hasn't been given its own figure. Ten per cent is a bar rule of
#: thumb, not a measurement - which is exactly why it's editable per item.
DEFAULT_LOSS_PERCENT = Decimal("10")


def loss_fraction(percent) -> Decimal:
    """A loss percentage as a 0-1 fraction, clamped to the range that means
    anything.

    Clamped rather than trusted: the field's validators only run on a form,
    and a negative allowance would let an item be credited with covering MORE
    sales than it was ever bought - which reads as stock that isn't missing.
    """
    if percent is None:
        return DEFAULT_LOSS_PERCENT / Decimal("100")
    return min(max(percent, Decimal("0")), Decimal("100")) / Decimal("100")


class StockType(models.Model):
    """A "type" of stock the bar tracks, e.g. Vodka, Gin, Orange Juice.

    Several supplier-specific Products (Sobieski 70CL, Wyborowa 70CL, ...) can
    all point to the same StockType. Current stock quantity/value is derived
    from the StockMovement ledger rather than stored, so it can never drift
    out of sync with what was actually received (or, later, sold/used).
    """

    name = models.CharField(max_length=255, unique=True)
    unit = models.CharField(max_length=4, choices=UnitChoices.choices)
    category = models.CharField(max_length=255, blank=True)
    # How much of what's bought never reaches a glass: over-pouring, the last
    # centilitres in a bottle, a keg's foam, a dropped crate. It's a
    # per-item property (a draught beer loses far more than a bottle of
    # syrup), which is why it lives here rather than as one global setting.
    #
    # Used when attributing an ambiguous "vodka OU gin" sale to a real
    # bottle: an alternative is only assumed to have covered a sale while it
    # still has stock left ABOVE this allowance - see
    # variance.allocate_choices.
    loss_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=DEFAULT_LOSS_PERCENT,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Part des achats perdue avant la vente (débordement, fonds de bouteille…). 10 % par défaut.",
    )
    # « Compter dans la marge produits »: an article no recipe consumes but
    # that every sale costs all the same - the paper towels, the cups, the
    # straws. There is no recipe to take them out of stock, so the products
    # margin counts what was BOUGHT of them over the window; that is the only
    # measure there is, and the page says so rather than letting the figure
    # read as consumption.
    #
    # Off by default, and meant for articles NO recipe uses: one counted here
    # and in a recipe is paid for twice, and a margin a few points too low is
    # exactly the kind of quietly wrong money this app exists to catch.
    count_in_products_margin = models.BooleanField(
        "compter dans la marge produits",
        default=False,
        # The help text says both halves out loud, because the page it feeds
        # cannot: what the figure MEANS (purchases, not consumption - there
        # is no recipe to take these out of stock as a sale is rung up) and
        # the mistake it invites (an article already in a recipe, paid for
        # twice, and a margin a few points too low with nothing saying why).
        help_text=(
            "Coché, ce qui en a été acheté sur la période compte dans la marge produits : "
            "l'essuie-tout, les gobelets, les pailles ne sont dans aucune recette, donc rien "
            "ne les sort du stock quand une vente est tapée et leurs achats sont la seule "
            "mesure qu'il y ait. À ne pas cocher pour un article qui sert déjà dans une "
            "recette : il serait compté deux fois, et la page Marges le dit."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def loss_fraction(self) -> Decimal:
        """This item's loss allowance as a 0-1 fraction."""
        return loss_fraction(self.loss_percent)

    @property
    def current_quantity(self) -> Decimal:
        # Sum("quantity") - same SQLite float-precision reasoning as
        # current_value_ht below: even a single-column SUM() isn't exact
        # decimal arithmetic once there are enough rows (verified: some
        # stock types were off by a few thousandths after dozens of
        # movements). Summing the fetched values in Python is exact.
        return sum((movement.quantity for movement in self.movements.all()), start=Decimal("0"))

    @property
    def current_value_ht(self) -> Decimal:
        # Multiplying at the SQL level (Sum(F("quantity") * F("unit_cost_ht")))
        # goes through SQLite's own arithmetic for that multiplication, which
        # isn't true decimal - confirmed it silently produces slightly wrong
        # totals even for one single movement (6.000 * 1.2183 came back as
        # 7.31 instead of the exact 7.3098). Multiplying in Python with
        # Decimal instead is exact.
        #
        # Iterating .all() rather than .values_list() so that a caller which
        # prefetch_related("movements") is actually served from that cache -
        # values_list() always issues its own query, which made every page
        # costing a list of stock types (the recipe list, a recipe's
        # ingredient breakdown) run one extra query PER stock type.
        return sum(
            (movement.quantity * movement.unit_cost_ht for movement in self.movements.all()),
            start=Decimal("0"),
        )

    @property
    def current_value_ttc(self) -> Decimal:
        """Sum of each movement's own invoice line total including VAT - not
        current_value_ht times one blended rate, since different products in
        the same stock type can carry different VAT rates. Same SQLite
        float-precision reasoning as current_value_ht above applies here."""
        values = self.movements.filter(invoice_line__isnull=False).values_list(
            "invoice_line__total_ht", "invoice_line__vat_rate"
        )
        return sum((total_ht * (vat_rate + Decimal("1")) for total_ht, vat_rate in values), start=Decimal("0"))

    @property
    def current_unit_cost_ht(self) -> Decimal:
        """Average cost per unit across whatever stock remains - used by
        recipes/models.py to cost an ingredient. Deliberately the same
        average the rest of this page is built on (current_value_ht /
        current_quantity), not a "latest price" or FIFO cost - there's no
        existing concept of ordering movements by "used first" in this app,
        and an average is the simplest thing that's already consistent with
        every other number already shown for a stock type.

        Worked out once per PREFETCHED list of movements: costing recipes
        asks each of their stock items for it some twenty-five times (every
        bound of every group, recipe and sub-recipe), and each ask added the
        whole ledger up again - 2 100 sums for 81 stock items to draw the
        products page. The answer is kept beside the very list it was added
        up from and served only while that list is still the one prefetched:
        a new prefetch, a refresh_from_db or a movement added through the
        manager replaces or drops it, and the sum is done again. Without a
        prefetch every ask reads the ledger, as it always did."""
        rows = self._prefetched_movements()
        memo = self.__dict__.get("_unit_cost_memo")
        if rows is not None and memo is not None and memo[0] is rows:
            return memo[1]
        quantity = self.current_quantity
        cost = (self.current_value_ht / quantity) if quantity else Decimal("0")
        if rows is not None:
            self._unit_cost_memo = (rows, cost)
        return cost

    def _prefetched_movements(self) -> list | None:
        """The list `self.movements.all()` iterates when a
        prefetch_related("movements") filled it, else None."""
        prefetched = getattr(self, "_prefetched_objects_cache", {}).get("movements")
        return getattr(prefetched, "_result_cache", None)


class Product(models.Model):
    """A specific product exactly as it appears on one supplier's invoices,
    e.g. "SOBIESKI VODKA 70CL" from Metro. Products with no stock_type yet are
    waiting in the review queue.
    """

    supplier = models.ForeignKey("invoices.Supplier", on_delete=models.PROTECT, related_name="products")
    raw_name = models.CharField(max_length=255)
    ean = models.CharField(max_length=32, blank=True)
    stock_type = models.ForeignKey(StockType, null=True, blank=True, on_delete=models.SET_NULL, related_name="products")
    # Set when reviewing/assigning the product. `unit` says what
    # invoice_line's quantity actually counts for this product: UNIT means
    # "quantity" is a count of discrete items (bottles, packs, ...); L/KG
    # means the meaningful amount is invoice_line.total_volume (a measured
    # volume/weight, e.g. a variable-weight cut of meat). `stock_equivalent`
    # then converts one of that into the stock type's own unit - e.g. unit=
    # UNIT (1 bottle) with stock_equivalent=0.7 for a Vodka stock type in
    # litres, or unit=KG with stock_equivalent=1 for a product already
    # measured in kg.
    unit = models.CharField(max_length=4, choices=UnitChoices.choices, default=UnitChoices.UNIT)
    stock_equivalent = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("1"),
        help_text="Quantité de l'unité du type de stock contenue dans une « unité » de ce produit.",
    )
    # A pre-fill for the review form (see product_matching_rules.py) - a
    # hint the user still has to confirm via the normal assign flow, never
    # applied automatically. None until "Appliquer les règles" has matched
    # this product; cleared once the product is actually assigned.
    ai_suggestion = models.JSONField(null=True, blank=True, default=None)
    # A charge, not an article: a charge item a supplier of charges files its
    # documents on - the supplier itself, or the rent and the provisions it
    # names (invoices.Supplier.expenses_only, invoices/charges.py). It has no
    # stock type and never will, so it waits in no queue and reaches no
    # stock page.
    is_expense = models.BooleanField("poste de charge", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["raw_name"]
        constraints = [
            models.UniqueConstraint(fields=["supplier", "raw_name"], name="unique_product_per_supplier"),
        ]

    def __str__(self):
        return self.raw_name

    @property
    def needs_review(self) -> bool:
        return self.stock_type_id is None and not self.is_expense


class MovementKind(models.TextChoices):
    PURCHASE = "PURCHASE", "Achat"
    # A loss you already know about: a broken bottle, a drink offered or
    # poured for staff, a spill. Recording it keeps the shelf count honest
    # AND keeps it out of the variance report's "unexplained" figure - which
    # is the number worth acting on (see inventory/variance.py).
    LOSS = "LOSS", "Perte connue"
    # A correction to the ledger itself ("the opening count was wrong"),
    # rather than something that physically happened.
    CORRECTION = "CORRECTION", "Correction"


class StockMovement(models.Model):
    """Append-only stock ledger entry. Positive quantity = stock received,
    negative = stock that left without being sold (see MovementKind).
    """

    stock_type = models.ForeignKey(StockType, on_delete=models.CASCADE, related_name="movements")
    kind = models.CharField(max_length=12, choices=MovementKind.choices, default=MovementKind.PURCHASE)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost_ht = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    invoice_line = models.OneToOneField(
        "invoices.InvoiceLine", null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_movement"
    )
    note = models.CharField(max_length=255, blank=True)
    # When the movement actually happened, which is not always when it was
    # typed in - a loss noticed on Monday may have happened on Saturday, and
    # the variance report puts it in the window it really belongs to.
    occurred_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.quantity} {self.stock_type.unit} of {self.stock_type}"

    @property
    def effective_date(self):
        """The date this movement counts against when slicing a period.

        `occurred_on` when given; otherwise the invoice's own date, which is
        when the stock really arrived - not when the PDF happened to be
        imported, which can be weeks later and would drop the delivery into
        the wrong stock-take window."""
        if self.occurred_on:
            return self.occurred_on
        if self.invoice_line_id and self.invoice_line.invoice.invoice_date:
            return self.invoice_line.invoice.invoice_date
        return self.created_at.date() if self.created_at else None


class StockTake(models.Model):
    """A dated physical stock count - "here's what I actually have on the
    shelf right now" - as opposed to StockMovement's running ledger of what
    was bought. Each line is valued from that product's own real purchase
    history (see services.value_counted_quantity), frozen at the moment the
    count is saved so a later invoice correction can't silently rewrite a
    past count's reported value.
    """

    taken_at = models.DateTimeField()
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"Inventaire du {self.taken_at:%d/%m/%Y}"

    @property
    def total_value_ht(self) -> Decimal:
        return sum(self.lines.values_list("value_ht", flat=True), start=Decimal("0"))

    @property
    def has_shortfall(self) -> bool:
        return self.lines.filter(has_shortfall=True).exists()


class GapFillEntry(models.Model):
    """One amount typed on « Combler les écarts » and the sales proposed for
    it - a working list, entry after entry, until it is cleared.

    Each entry is planned ON TOP of the ones before it (their sales are
    played through the engine as if already rung up), so 7 € and then 7 €
    again fill what is still behind rather than proposing the same glasses
    twice. What was proposed is kept as it was proposed (`lines`): the owner
    may already have rung it up, and a later invoice or count must not
    rewrite a list he is holding. `sales_seen` is every serving sold from
    `sales_from` on, as known then (`sales_up_to` the last day of sales):
    once that moves, sales from this list may be in them, and the page says
    to clear it.

    Never exported by « Données »: it is a scratch list, and it goes with
    its stock take.
    """

    stock_take = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name="gap_fill_entries")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    # Plan.reason when the total is short of the amount (gap_planner).
    reason = models.CharField(max_length=32, blank=True)
    # [{"recipe": pk, "name", "till", "till_price" (str or None), "count",
    # "price" (str), "fills": [article names]}], in the order shown.
    lines = models.JSONField(default=list, blank=True)
    sales_up_to = models.DateField(null=True, blank=True)
    # Every serving sold from `sales_from` on, then (gaps.servings_from): a
    # day imported again with more sales leaves `sales_up_to` as it was, not
    # this. `sales_from` is the day before the entry was made - the till
    # files a sale rung after midnight under the day its service began.
    sales_from = models.DateField(null=True, blank=True)
    sales_seen = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.amount} € le {self.created_at:%d/%m/%Y %H:%M}"

    @property
    def remainder(self) -> Decimal:
        return self.amount - self.total

    @property
    def sales(self) -> int:
        from .gaps import entry_rows

        return sum(row.count for row in entry_rows(self))


class GapExclusion(models.Model):
    """An article, or a whole category of articles, left out of « Combler
    les écarts » - the owner's choice, kept for the espace until he takes it
    back (01/10/2026: « exclure des articles/catégories de ces écarts, et que
    cela reste en mémoire »).

    Exactly one of the two: `stock_type` for one article, `category` for
    every article filed under that category name - those classified into it
    later included, which is what excluding a category means. A blank
    category ("") is the articles with none. An article left out is ignored
    whole: no gap to fill, no limit on a sale, out of the lists and of the
    average (gaps.gaps_since). Never exported by « Données »: like the list,
    it belongs to this page. An article merged into another or deleted takes
    its exclusion with it.
    """

    stock_type = models.OneToOneField(
        StockType, null=True, blank=True, on_delete=models.CASCADE, related_name="gap_exclusion"
    )
    category = models.CharField(max_length=255, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(stock_type__isnull=False) & models.Q(category__isnull=True))
                | (models.Q(stock_type__isnull=True) & models.Q(category__isnull=False)),
                name="gap_exclusion_article_or_category",
            ),
        ]

    def __str__(self):
        return f"Catégorie « {self.category} »" if self.stock_type_id is None else str(self.stock_type)


class GapFillSetting(models.Model):
    """« Combler les écarts »' own settings for the espace: one row (pk 1),
    absent until the owner changes something.

    `sold_within_months`: only the recipes sold over the last that many
    months are proposed (the owner, 01/10/2026: « ne proposer que des
    recettes ayant été vendues il y a moins de X temps » - some recipes are
    off the menu). Counted back from today, whatever the count the gaps run
    from: right after a count nothing has been sold since it, yet the menu
    has not changed. None, the default, is the recipes sold since that count.
    Never exported by « Données »: like the exclusions, it belongs to this
    page (gaps.gaps_since)."""

    SINGLETON_PK = 1
    #: Ten years: a typed duration is refused past it.
    MAX_MONTHS = 120

    sold_within_months = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="gap_fill_setting_one_row"),
            models.CheckConstraint(
                condition=models.Q(sold_within_months__isnull=True)
                | models.Q(sold_within_months__gte=1, sold_within_months__lte=120),
                name="gap_fill_setting_months_in_range",
            ),
        ]

    @classmethod
    def current(cls) -> "GapFillSetting":
        """The stored row, else the defaults unsaved: a page drawn writes
        nothing."""
        return cls.objects.filter(pk=cls.SINGLETON_PK).first() or cls(pk=cls.SINGLETON_PK)


class ShoppingSetting(models.Model):
    """« Prévoir les courses »' own settings for the espace: one row (pk 1),
    absent until the owner changes something.

    `threshold_percent`: a line is proposed from that chance on (« Proposer
    un article à partir de … % de chances »), and the page shows the lines
    from half of it under « Peut-être ». `memory_months`: how fast a habit
    fades - a visit that many months old counts half (« Mémoire des
    habitudes »). `use_till`: whether the till's sales say how much of the
    last purchase is used up (« Tenir compte des ventes de la caisse »);
    off, no till query is made. The ranges are the form's and the
    database's (check constraints below). Never exported by « Données »:
    like the exclusions, it belongs to this page (inventory/shopping.py)."""

    SINGLETON_PK = 1
    #: Inclusive bounds, as the form reads them and the database checks them.
    THRESHOLD_RANGE = (10, 60)
    MEMORY_RANGE = (2, 24)

    threshold_percent = models.PositiveSmallIntegerField(default=25)
    memory_months = models.PositiveSmallIntegerField(default=6)
    use_till = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="shopping_setting_one_row"),
            models.CheckConstraint(
                condition=models.Q(threshold_percent__gte=10, threshold_percent__lte=60),
                name="shopping_setting_threshold_in_range",
            ),
            models.CheckConstraint(
                condition=models.Q(memory_months__gte=2, memory_months__lte=24),
                name="shopping_setting_memory_in_range",
            ),
        ]

    @classmethod
    def current(cls) -> "ShoppingSetting":
        """The stored row, else the defaults unsaved: a page drawn writes
        nothing."""
        return cls.objects.filter(pk=cls.SINGLETON_PK).first() or cls(pk=cls.SINGLETON_PK)


class ShoppingExclusion(models.Model):
    """An article, or a whole category of articles, never proposed by
    « Prévoir les courses » - everywhere, or (an article only) at one store
    (« Pas ici »): the owner's choice, kept for the espace until he takes it
    back.

    `stock_type` for one article, `category` for every article filed under
    that category name - those classified into it later included; a blank
    category ("") is the articles with none. Exactly one of the two, and a
    category is always left out everywhere: `supplier` is set only beside
    an article. An article is left out once everywhere and once at each
    store; excluding it everywhere deletes its store rows on the page, the
    database does not ask it. Never exported by « Données »: like the
    setting, it belongs to this page. CASCADE on both: the « Données »
    clears delete articles and suppliers, and an article merged into another
    or deleted takes its rows with it (like GapExclusion)."""

    stock_type = models.ForeignKey(
        StockType, null=True, blank=True, on_delete=models.CASCADE, related_name="shopping_exclusions"
    )
    category = models.CharField(max_length=255, null=True, blank=True, unique=True)
    supplier = models.ForeignKey(
        "invoices.Supplier", null=True, blank=True, on_delete=models.CASCADE, related_name="shopping_exclusions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(stock_type__isnull=False) & models.Q(category__isnull=True))
                | (models.Q(stock_type__isnull=True) & models.Q(category__isnull=False)),
                name="shopping_exclusion_article_or_category",
            ),
            models.CheckConstraint(
                condition=models.Q(category__isnull=True) | models.Q(supplier__isnull=True),
                name="shopping_exclusion_category_everywhere",
            ),
            models.UniqueConstraint(
                fields=["stock_type"],
                condition=models.Q(supplier__isnull=True),
                name="shopping_exclusion_article_once",
            ),
            models.UniqueConstraint(
                fields=["stock_type", "supplier"], name="shopping_exclusion_article_once_per_store"
            ),
        ]

    def __str__(self):
        if self.stock_type_id is None:
            return f"Catégorie « {self.category} »"
        if self.supplier_id is None:
            return str(self.stock_type)
        return f"{self.stock_type} (chez {self.supplier})"


class ShoppingList(models.Model):
    """One store's shopping list (« Liste de courses »), shared by every login
    of the espace: OPEN while `finished_at` is empty - at most one per store -
    then kept as it was, read-only. Made by the first item added (a page drawn
    writes nothing: inventory/shopping_lists.py). Who: usernames, like
    ReceiptBatch.sent_by. Never exported by « Données ». CASCADE with its
    store: a supplier deleted (its page, « Données ») takes its lists."""

    supplier = models.ForeignKey("invoices.Supplier", on_delete=models.CASCADE, related_name="shopping_lists")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.CharField(max_length=150, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    finished_by = models.CharField(max_length=150, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["supplier"],
                condition=models.Q(finished_at__isnull=True),
                name="shopping_list_one_open_per_store",
            ),
        ]

    def __str__(self):
        return f"Liste {self.supplier}"

    @property
    def is_open(self) -> bool:
        return self.finished_at is None


class ShoppingListItem(models.Model):
    """One line of a ShoppingList: an article (`stock_type`) or a free text
    (`label` only).

    `label` is the article's name when it was added (refreshed by a merge),
    shown once the article is gone (SET_NULL). `quantity` counts
    `product_name` when `unit` is "" (« 24 » of « BIERE … X24 ») - or, with no
    product, whatever the label names (a free text) - and the article's own
    unit otherwise (L, KG, UNIT: « 2 L »). `pack_size` is the product's
    colisage when the forecast knew it (« 1 colis de 24 », a hint). Ticked
    (bought) when `checked_at` is set. `added_at` is a default, not
    auto_now_add: a carry-over to the next list copies it.

    `item_size` is how much of `size_unit` (the article's unit when added)
    one counted item holds (0.7 for a 70 cl bottle). It is set only when
    `unit` is "" and the number counts items: a product's, or the article's
    usual format. It is a snapshot, like `StockTakeLine.unit`: a change of
    the usual product, of the article's unit, or the article deleted leaves
    « 3 bouteilles de 70 cl » meaning what it meant. No foreign key to the
    product: `product_name` and `item_size` already say what one would be
    read for.

    One item per article per list; free texts are kept apart by the page
    (their search_key), never by the database - an article deleted turns its
    items into free texts, which must never trip a constraint. No ordering:
    every query says (`added_at`, `pk`)."""

    shopping_list = models.ForeignKey(ShoppingList, on_delete=models.CASCADE, related_name="items")
    stock_type = models.ForeignKey(
        StockType, null=True, blank=True, on_delete=models.SET_NULL, related_name="shopping_list_items"
    )
    label = models.CharField(max_length=255)
    product_name = models.CharField(max_length=255, blank=True)
    pack_size = models.PositiveIntegerField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit = models.CharField(max_length=4, choices=UnitChoices.choices, blank=True)
    item_size = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    size_unit = models.CharField(max_length=4, choices=UnitChoices.choices, blank=True)
    note = models.CharField(max_length=200, blank=True)
    added_at = models.DateTimeField(default=timezone.now)
    added_by = models.CharField(max_length=150, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_by = models.CharField(max_length=150, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="shopping_list_item_quantity_positive"),
            models.CheckConstraint(condition=~models.Q(label=""), name="shopping_list_item_has_a_label"),
            models.CheckConstraint(
                condition=models.Q(unit__in=["", *UnitChoices.values]), name="shopping_list_item_unit_known"
            ),
            models.CheckConstraint(
                condition=models.Q(pack_size__isnull=True) | models.Q(pack_size__gt=1),
                name="shopping_list_item_pack_of_several",
            ),
            models.UniqueConstraint(fields=["shopping_list", "stock_type"], name="shopping_list_item_article_once"),
            # Both or neither, the size above 0, only beside a number of
            # items. `item_size__isnull=False` is not redundant: NULL > 0 is
            # NULL, which a CHECK lets through, so without it a `size_unit`
            # with no size passed beside `unit` "".
            models.CheckConstraint(
                condition=(models.Q(item_size__isnull=True) & models.Q(size_unit=""))
                | (
                    models.Q(item_size__isnull=False)
                    & models.Q(item_size__gt=0)
                    & models.Q(size_unit__in=UnitChoices.values)
                    & models.Q(unit="")
                ),
                name="shopping_list_item_size_of_an_item",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def name(self) -> str:
        """The article's live name (a rename shows), else the label kept."""
        return self.stock_type.name if self.stock_type_id is not None else self.label


class StockTakeLine(models.Model):
    """One counted product OR stock type within a StockTake - exactly one of
    the two (see the CheckConstraint below): a specific product when you
    know exactly which bottle/pack you're looking at (counted_quantity is
    then how many of THAT product, not the stock type's own unit - see
    services.value_counted_quantity), or a stock type directly when it's
    easier to just say "how much Vodka" without pinning down which brand
    (counted_quantity then is already in the stock type's own unit).

    value_ht/has_shortfall are computed once by value_counted_quantity() /
    value_counted_stock_type_quantity() and stored, not recomputed on every
    view, so this line keeps reporting what the count was actually worth on
    the day it was taken."""

    stock_take = models.ForeignKey(StockTake, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.PROTECT, related_name="stock_take_lines"
    )
    stock_type = models.ForeignKey(
        StockType, null=True, blank=True, on_delete=models.PROTECT, related_name="stock_take_lines"
    )
    counted_quantity = models.DecimalField(max_digits=10, decimal_places=4)
    # What counted_quantity is expressed in. For a stock_type line this is
    # always that stock type's own unit (no real choice). For a product
    # line it's a real choice: UNIT to count discrete bottles/packs, or the
    # product's stock type's own unit to enter an amount measured directly
    # (e.g. "roughly 0.3L left in an open bottle") - see
    # services.value_counted_quantity. Stored (not re-derived) so a saved
    # count keeps meaning what it meant on the day it was taken.
    unit = models.CharField(max_length=4, choices=UnitChoices.choices)
    value_ht = models.DecimalField(max_digits=10, decimal_places=2)
    # True when the count exceeds everything this product's purchase
    # history can account for - the shortfall portion is still valued (at
    # the oldest known price) so the total isn't understated, but this
    # flags it for a human to notice the mismatch (miscount, or stock
    # bought before this system tracked invoices).
    has_shortfall = models.BooleanField(default=False)
    shortfall_quantity = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(product__isnull=False, stock_type__isnull=True)
                    | models.Q(product__isnull=True, stock_type__isnull=False)
                ),
                name="stocktakeline_exactly_one_source",
            ),
            models.UniqueConstraint(
                fields=["stock_take", "product"],
                name="unique_product_per_stock_take",
                condition=models.Q(product__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["stock_take", "stock_type"],
                name="unique_stock_type_per_stock_take",
                condition=models.Q(stock_type__isnull=False),
            ),
        ]

    def __str__(self):
        return f"{self.counted_quantity} x {self.source_name}"

    @property
    def source_name(self) -> str:
        return self.product.raw_name if self.product_id else self.stock_type.name


class StockTakeLineSource(models.Model):
    """One FIFO "slice" of a StockTakeLine's valuation: how much of one
    specific invoice line contributed to that line's price, and at what
    per-unit cost - so a count's value isn't just a number, it's traceable
    back to the actual purchases it was priced from. Frozen alongside
    value_ht (see StockTakeLine) rather than recomputed, for the same
    reason: a later invoice correction shouldn't silently rewrite what a
    past count was reported as being worth.

    The shortfall portion of a line (see has_shortfall/shortfall_quantity)
    has no source row of its own - it's an extrapolation at the oldest
    known price, not something actually drawn from that invoice line.
    """

    stock_take_line = models.ForeignKey(StockTakeLine, related_name="sources", on_delete=models.CASCADE)
    invoice_line = models.ForeignKey("invoices.InvoiceLine", on_delete=models.PROTECT, related_name="+")
    quantity_used = models.DecimalField(max_digits=10, decimal_places=4)
    unit_cost_ht = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
        ordering = ["-invoice_line__invoice__invoice_date"]

    def __str__(self):
        return f"{self.quantity_used} @ {self.unit_cost_ht} from {self.invoice_line}"


class SuggestionJob(models.Model):
    """Historical record of "Suggérer avec l'IA" runs from when stock-item
    naming suggestions came from a local Ollama model instead of the
    hardcoded rules in product_matching_rules.py (see git history for that
    code if it's ever worth revisiting - too slow and not reliable enough in
    practice, per real usage). Nothing creates new rows here anymore; kept
    only so old job history stays queryable rather than deleting it via a
    migration nobody asked for.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Terminé"
        FAILED = "FAILED", "Échoué"
        CANCELLED = "CANCELLED", "Annulé"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    total = models.IntegerField(default=0)
    processed = models.IntegerField(default=0)
    succeeded = models.IntegerField(default=0)
    failed = models.IntegerField(default=0)
    log = models.TextField(blank=True)
    # Checked between streamed chunks of the in-flight Ollama call (not just
    # between batches) so cancelling actually drops the connection and stops
    # the model generating - not just "stop starting new work".
    cancel_requested = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def append_log(self, message: str):
        self.log = f"{self.log}{message}\n" if self.log else f"{message}\n"
        self.save(update_fields=["log"])
