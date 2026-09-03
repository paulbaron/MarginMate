from decimal import Decimal

from django.db import models


class UnitChoices(models.TextChoices):
    LITRE = "L", "Litre"
    UNIT = "UNIT", "Unité"
    KILOGRAM = "KG", "Kilogramme"


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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

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
        every other number already shown for a stock type."""
        quantity = self.current_quantity
        return (self.current_value_ht / quantity) if quantity else Decimal("0")


class Product(models.Model):
    """A specific product exactly as it appears on one supplier's invoices,
    e.g. "SOBIESKI VODKA 70CL" from Metro. Products with no stock_type yet are
    waiting in the review queue.
    """

    supplier = models.ForeignKey("invoices.Supplier", on_delete=models.PROTECT, related_name="products")
    raw_name = models.CharField(max_length=255)
    ean = models.CharField(max_length=32, blank=True)
    stock_type = models.ForeignKey(
        StockType, null=True, blank=True, on_delete=models.SET_NULL, related_name="products"
    )
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
        return self.stock_type_id is None


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
                fields=["stock_take", "product"], name="unique_product_per_stock_take", condition=models.Q(product__isnull=False)
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
