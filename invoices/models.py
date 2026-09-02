from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone


class Supplier(models.Model):
    """A vendor invoices come from. ``parser_key`` points at an entry in the
    parser registry (invoices/parsers/registry.py); leave it blank to always
    fall back to the LLM-based generic parser for this supplier.
    """

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    parser_key = models.CharField(max_length=32, blank=True)
    is_scrapable = models.BooleanField(
        default=False, help_text="Whether 'Gather new invoices' knows how to fetch this supplier automatically."
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Invoice(models.Model):
    class Status(models.TextChoices):
        IMPORTED = "IMPORTED", "Importée"
        NEEDS_REVIEW = "NEEDS_REVIEW", "À vérifier"
        COMPLETE = "COMPLETE", "Complète"
        ERROR = "ERROR", "Erreur"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="invoices")
    invoice_number = models.CharField(max_length=100, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    source_file = models.FileField(upload_to="invoices/%Y/%m/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IMPORTED)
    error_message = models.TextField(blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    # The gap between the supplier's own printed grand total (Montant HT +
    # Droits) and the sum of what we could actually attribute to individual
    # lines - e.g. UBA prints several separate duty categories (ACCISE,
    # REGIE, VIG. SECU, ...) as one invoice-level total, but only some of
    # them show up in a per-product column, so summing lines alone slightly
    # understates the true cost. Added into total_ht below purely so the
    # invoice's own total reconciles to the penny with what was actually
    # billed - never attributed to any individual product's own price,
    # since there's no reliable way to know which product it belongs to.
    reconciliation_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ["-invoice_date", "-imported_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "invoice_number"],
                condition=~Q(invoice_number=""),
                name="unique_supplier_invoice_number",
            ),
        ]

    def __str__(self):
        return f"{self.supplier} {self.invoice_number or self.pk} ({self.invoice_date})"

    @property
    def total_ht(self):
        lines_total = self.lines.aggregate(total=Sum("total_ht"))["total"] or 0
        return lines_total + self.reconciliation_adjustment

    @property
    def needs_review_count(self):
        return self.lines.filter(product__stock_type__isnull=True).count()


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="invoice_lines")
    raw_name = models.CharField(max_length=255)
    quantity = models.IntegerField(default=1)
    # The supplier's own "packs per line" multiplier (Metro's "Colisage"),
    # already folded into `quantity` (quantity = colisage * qty bought) -
    # shown separately in the review queue since whether a pack size was
    # already applied to `quantity` or still needs to be applied by hand via
    # the stock_equivalent factor isn't always obvious from quantity alone.
    colisage = models.IntegerField(default=1)
    total_volume = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit_cost_ht = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    total_ht = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    taxes = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    category = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.raw_name} x{self.quantity}"


class ScrapeJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Terminé"
        FAILED = "FAILED", "Échoué"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    log = models.TextField(blank=True)
    # {"METRO": {"label": "Metro", "found": 3, "imported": 2}, "UBA": {...}}
    progress = models.JSONField(default=dict, blank=True)
    invoices_found = models.IntegerField(default=0)
    invoices_created = models.IntegerField(default=0)
    range_start = models.DateField(null=True, blank=True)
    range_end = models.DateField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def append_log(self, message: str):
        # Timestamped so a slow run can actually be diagnosed after the fact
        # (which specific step took how long) instead of just knowing the
        # whole thing felt slow.
        elapsed = (timezone.now() - self.started_at).total_seconds()
        line = f"[+{elapsed:6.1f}s] {message}"
        self.log = f"{self.log}{line}\n" if self.log else f"{line}\n"
        self.save(update_fields=["log"])

    def update_progress(self, supplier_code: str, label: str = "", **counts):
        entry = self.progress.setdefault(supplier_code, {"label": label, "found": 0, "imported": 0})
        if label:
            entry["label"] = label
        entry.update(counts)
        self.save(update_fields=["progress"])
