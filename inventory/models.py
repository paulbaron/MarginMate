from decimal import Decimal

from django.db import models
from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce


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
        return self.movements.aggregate(total=Coalesce(Sum("quantity"), Decimal("0")))["total"]

    @property
    def current_value_ht(self) -> Decimal:
        total = self.movements.aggregate(
            total=Coalesce(
                Sum(F("quantity") * F("unit_cost_ht"), output_field=DecimalField(max_digits=14, decimal_places=4)),
                Decimal("0"),
            )
        )["total"]
        return total

    @property
    def current_value_ttc(self) -> Decimal:
        """Sum of each movement's own invoice line total including VAT - not
        current_value_ht times one blended rate, since different products in
        the same stock type can carry different VAT rates."""
        total = self.movements.filter(invoice_line__isnull=False).aggregate(
            total=Coalesce(
                Sum(
                    F("invoice_line__total_ht") * (F("invoice_line__vat_rate") + Decimal("1")),
                    output_field=DecimalField(max_digits=14, decimal_places=4),
                ),
                Decimal("0"),
            )
        )["total"]
        return total


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
        help_text="How much of the stock type's unit is in one 'unit' of this product.",
    )
    # An AI-generated pre-fill for the review form (see ai_suggestions.py) -
    # a hint the user still has to confirm via the normal assign flow, never
    # applied automatically. None until "Suggérer avec l'IA" has run for
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


class StockMovement(models.Model):
    """Append-only stock ledger entry. Positive quantity = stock received.

    Future features (deducting stock from recipe sales, manual corrections)
    are meant to plug in as additional movements - with a negative quantity
    and no invoice_line - without needing any change to this model.
    """

    stock_type = models.ForeignKey(StockType, on_delete=models.CASCADE, related_name="movements")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost_ht = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    invoice_line = models.OneToOneField(
        "invoices.InvoiceLine", null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_movement"
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.quantity} {self.stock_type.unit} of {self.stock_type}"


class SuggestionJob(models.Model):
    """Tracks one run of "Suggérer avec l'IA" (see ai_suggestions.py). Runs in
    a background thread since local LLM inference for ~10 products already
    takes tens of seconds - blocking the request the way an early version did
    reproduces exactly the "looks hung, no feedback" problem already fixed
    once for invoice gathering (see invoices.ScrapeJob).
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
