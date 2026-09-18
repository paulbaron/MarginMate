import re
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Value
from django.db.models.functions import Concat
from django.utils import timezone

from common import JobLogMixin


class Supplier(models.Model):
    """A vendor invoices come from. ``parser_key`` points at an entry in the
    parser registry (invoices/parsers/registry.py); blank, its PDFs are filed
    empty to be typed in - and its tickets read like any shop's (see
    parsers.is_ticket_shop).
    """

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    parser_key = models.CharField(max_length=32, blank=True)
    is_scrapable = models.BooleanField(
        default=False,
        help_text="Si « Récupérer les nouvelles factures » sait aller les chercher tout seul."
    )
    # What a shop no reader was configured for prints at the top of its
    # tickets: an import finds it there (receipts.detect_parser), before the
    # configured shops - a person said so.
    ticket_header = models.CharField(
        "texte d'en-tête des tickets",
        max_length=100,
        blank=True,
        help_text="Imprimé en haut de ses tickets (nom, rue…) : un ticket qui le porte est rangé chez ce fournisseur.",
    )
    # What its documents print that names it whatever their layout - SIREN,
    # phone, web site (invoices/identifiers.py) - learned from the ones a
    # person filed or checked under it (receipts.learn_identifiers). Found on
    # a document no header was recognised on, and on no other supplier's
    # list, it files the document here.
    ticket_identifiers = models.JSONField("identifiants lus sur ses documents", default=list, blank=True)
    # A supplier whose documents are charges, not goods: a phone
    # subscription, the rent, the water. There is no product behind a rent,
    # so its documents are filed as one line per VAT rate (importing.
    # expense_lines) on a product of its own that never reaches the stock
    # pages - and what they cost is shown on "Produits" in a fold apart.
    expenses_only = models.BooleanField(
        "factures de charges",
        default=False,
        help_text="Abonnement, loyer, eau… : une ligne par taux de TVA, aucun produit à classer.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class InvoiceType(models.Model):
    """One recognizable "kind" of invoice this app knows how to gather and
    parse - e.g. "UBA - Factures". Separate from Supplier because a single
    supplier could in principle send more than one distinguishable invoice
    format (each needing its own matching rule and/or parser), and because
    the matching rule itself lives in a separate, source-kind-specific
    model (see EmailInvoiceSource) - a future website-based source would
    need an unrelated shape (login/selectors, not regex patterns).
    """

    class SourceKind(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        WEBSITE = "WEBSITE", "Site web"  # reserved - not yet selectable in the UI

    name = models.CharField(max_length=255)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="invoice_types")
    # Same convention as Supplier.parser_key (see its docstring) - kept
    # separate rather than reusing Supplier.parser_key so one supplier can
    # have two invoice types needing two different parsers.
    parser_key = models.CharField(max_length=32, blank=True)
    source_kind = models.CharField(max_length=10, choices=SourceKind.choices, default=SourceKind.EMAIL)
    is_active = models.BooleanField(
        default=True,
        help_text="Inclure ce type dans « Récupérer les nouvelles factures »."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class EmailInvoiceSource(models.Model):
    """How to recognize an InvoiceType's emails in the shared invoice
    mailbox (see settings.INVOICE_EMAIL_ADDRESS) and which attachment to
    treat as the invoice. All patterns are real regexes (not IMAP's own
    crude substring search - see invoices/scrapers/generic_email.py for
    why), tested against the From header, the Subject, the decoded text
    body, and each attachment's filename respectively. subject_pattern and
    body_pattern blank means "match anything"; sender_pattern is required
    since matching every email in the inbox would defeat the point.
    """

    invoice_type = models.OneToOneField(InvoiceType, on_delete=models.CASCADE, related_name="email_source")
    sender_pattern = models.CharField(max_length=500, help_text="Expression régulière testée sur l'expéditeur.")
    subject_pattern = models.CharField(
        max_length=500, blank=True, help_text="Expression régulière testée sur l'objet (vide = tous)."
    )
    body_pattern = models.CharField(
        max_length=500, blank=True, help_text="Expression régulière testée sur le contenu (vide = tous)."
    )
    attachment_pattern = models.CharField(
        max_length=200,
        blank=True,
        default=r"(?i)\.pdf$",
        help_text="Expression régulière testée sur le nom de la pièce jointe.",
    )

    def __str__(self):
        return f"Source email de {self.invoice_type}"

    def clean(self):
        errors = {}
        for field_name in ("sender_pattern", "subject_pattern", "body_pattern", "attachment_pattern"):
            value = getattr(self, field_name)
            if not value:
                continue
            try:
                re.compile(value)
            except re.error as exc:
                errors[field_name] = f"Expression régulière invalide : {exc}"
        if errors:
            raise ValidationError(errors)


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
    # A photographed receipt's printed total - what was paid. See total_ttc.
    printed_total_ttc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Everything below is only populated for photographed till receipts (see
    # invoices/parsers/receipt_base.py). A digital PDF needs none of it: if
    # the layout matched, the numbers are the numbers.
    #
    # A photo is different. OCR can misread a digit and produce a perfectly
    # well-formed wrong price, so a receipt parser checks its own arithmetic
    # against the totals the ticket itself prints and stores the verdict
    # here. `parse_checks` is a list of {"label", "passed", "detail"} - see
    # parsers.base.ParseCheck - and the review screen shows it beside the
    # photo so a human can see *why* a receipt was flagged.
    ocr_text = models.TextField(blank=True)
    # What a digital document carries as text, kept for what it says about
    # its sender (invoices/identifiers.py) - a receipt keeps `ocr_text`
    # instead, and only that one makes a document a receipt.
    source_text = models.TextField(blank=True)
    ocr_confidence = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    parse_checks = models.JSONField(default=list, blank=True)
    # The VAT table the document prints, as [[rate, base HT, tax], ...] with
    # the rate as a fraction. Stored because it is one of the three things a
    # check compares - the lines, the printed total, this - and the only one
    # a person could not correct: every check that mentioned it was a
    # warning nobody could answer. Typed on the review screen like the rest.
    vat_breakdown = models.JSONField(default=list, blank=True)
    preview_image = models.ImageField(upload_to="receipts/%Y/%m/", blank=True, null=True)
    # Set when a person has actually looked at the photo and accepted the
    # lines. Distinct from status=COMPLETE, which only means every product
    # was recognised - a receipt can be COMPLETE and still misread.
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # SHA-256 of the file exactly as it was uploaded. A folder of receipt
    # photos gets scanned again and again as new ones land in it; this is
    # what lets the ones already imported be skipped before any OCR runs,
    # rather than each costing seconds to recognise only to be refused as a
    # duplicate afterwards.
    source_sha256 = models.CharField(max_length=64, blank=True, db_index=True)

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
        """Added up in Python from the lines, like total_ttc: the invoice list
        prefetches them, so showing both totals costs two queries for the
        whole page rather than one per invoice per total."""
        return self.lines_total_ht + self.reconciliation_adjustment

    @property
    def total_ttc(self):
        """Summed per line, not total_ht times one rate: a single invoice
        mixes 20% spirits with 5.5% food, and any blended rate would be
        wrong for both.

        The reconciliation adjustment takes VAT too. It is duty the lines
        don't carry (or a receipt's HT rounding), and duty is part of the VAT
        base: added flat, every UBA total fell five or six cents short of
        what the bank actually debited for it.

        A receipt whose every line kept its printed amount is added up from
        those, never from HT (ten pitas at 0,70 came to 7,01), and its
        adjustment is left out: in HT it puts back cents the division by
        (1 + rate) rounded away, which the printed amounts never lost. When
        those amounts are within the parser's tolerance of the printed total,
        the printed total is the answer - it is what was paid, and a cent the
        OCR misread must not make the bank payment unmatchable.
        """
        from .parsers.receipt_base import RECONCILIATION_TOLERANCE

        lines = list(self.lines.all())
        if lines and all(line.printed_ttc is not None for line in lines):
            printed = sum((line.total_ttc for line in lines), start=Decimal("0"))
            paid = self.printed_total_ttc
            if paid is not None and abs(paid - printed) <= RECONCILIATION_TOLERANCE:
                return paid
            return printed
        # Otherwise all from HT: the adjustment covers every line's rounding,
        # so mixing in printed amounts would count some of it twice.
        total = sum(
            (line.total_ht * (Decimal("1") + line.vat_rate) for line in lines),
            start=Decimal("0"),
        )
        return total + self.reconciliation_adjustment * (Decimal("1") + self._adjustment_vat_rate(lines))

    @staticmethod
    def _adjustment_vat_rate(lines) -> Decimal:
        """The rate of the goods the adjustment belongs with: the lines that
        carry duty when some do, otherwise all of them - by largest HT share
        when their rates are mixed."""
        weights = {}
        for line in [line for line in lines if line.taxes] or lines:
            weights[line.vat_rate] = weights.get(line.vat_rate, Decimal("0")) + abs(line.total_ht)
        return max(weights, key=weights.get) if weights else Decimal("0")

    @property
    def lines_total_ht(self):
        """What the lines alone come to, before the reconciliation
        adjustment - the two are shown side by side when they differ."""
        return sum((line.total_ht for line in self.lines.all()), start=Decimal("0"))

    @property
    def needs_review_count(self):
        """How many of its lines wait for a stock item - never a charge's
        postes, which are not products (see Product.needs_review)."""
        return self.lines.filter(product__stock_type__isnull=True, product__is_expense=False).count()

    @property
    def is_receipt(self):
        """A photographed till receipt rather than a digital invoice."""
        return bool(self.parse_checks) or bool(self.ocr_text)

    @property
    def waiting_check(self) -> bool:
        """Whether it is in the queue of documents to check - the same rule
        as `receipts.pending_receipts`, since a row that says "À vérifier"
        over an empty queue is a row nobody can act on. A charge is never in
        it: there is nothing to type on a rent."""
        return bool(self.parse_checks) and self.reviewed_at is None and not self.supplier.expenses_only

    @property
    def review_state(self) -> dict:
        """What this document's state is called, wherever it is listed: the
        pill's class (which colours it) and its label."""
        if self.waiting_check:
            return {"css": self.Status.NEEDS_REVIEW, "label": "À vérifier"}
        if self.supplier.expenses_only:
            return (
                {"css": self.Status.NEEDS_REVIEW, "label": "Total à vérifier"}
                if self.status == self.Status.NEEDS_REVIEW
                else {"css": self.Status.COMPLETE, "label": "Charge"}
            )
        if self.parse_checks:
            return {"css": self.Status.COMPLETE, "label": "Vérifié"}
        if self.status == self.Status.NEEDS_REVIEW:
            return {"css": self.status, "label": "Produits à classer"}
        return {"css": self.status, "label": self.get_status_display()}

    @property
    def document_text(self):
        """What the document says, however it was read."""
        return self.ocr_text or self.source_text

    @property
    def failed_checks(self):
        return [check for check in self.parse_checks if not check.get("passed")]

    @property
    def ocr_confidence_percent(self):
        """69, not 0.69 - the stored value is a fraction, and the review
        screen rendered it straight through `floatformat:0`, which turned
        every receipt's confidence into "1%"."""
        if self.ocr_confidence is None:
            return None
        return self.ocr_confidence * Decimal("100")

    @property
    def receipt_verified(self):
        """The parse proved itself against the ticket's own printed totals.

        Not the same as "correct" - it is the strongest statement the
        machine can make on its own, and it is what decides whether a
        receipt needs a human to look at the photo.
        """
        return bool(self.parse_checks) and not self.failed_checks

    @property
    def needs_receipt_review(self):
        return self.is_receipt and self.reviewed_at is None and not self.receipt_verified


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="invoice_lines")
    raw_name = models.CharField(max_length=255)
    # A count, or a measure when the document sells by it: 0,82 m² of
    # plywood, 2,5 m of cable. To the thousandth.
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
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
    # The product name exactly as OCR read it off a receipt photo, kept when
    # the review screen corrects `raw_name`. Each reading is one more name
    # its product is known by (inventory.matching.known_readings): one till
    # label comes back as BAGUETTE BLANC, BLAND, BLAVD or AGUETTE BLANC, and
    # the next ticket is matched against all of them. Blank for digital
    # invoices, typed lines and "Article divers" - a price is not a name.
    read_as = models.CharField(max_length=255, blank=True)
    # The amount tax included as the receipt printed it (or as typed on the
    # review screen, which is in TTC). Needed because HT to the cent does not
    # always convert back: 7,00 at 5.5% is 6,64 HT, which is 7,01. Null for
    # digital invoices, which are in HT, and for receipt lines a promotion was
    # folded into before promotions were kept apart (`discount_ttc`);
    # `total_ttc` then works it out from HT.
    printed_ttc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # The line's share of a promotion, tax included, off `printed_ttc`: a
    # ticket prints the loaf at 0,49 and "3 pour 2 - 0,49" in a block of its
    # own, and the review screen shows both as printed. `total_ht` is what the
    # line cost after it.
    discount_ttc = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        from common import plain_number

        return f"{self.raw_name} x{plain_number(self.quantity)}"

    @property
    def total_ttc(self):
        """What the line cost, tax included: as printed, less its share of a
        promotion - or worked out from HT when nothing was printed."""
        if self.printed_ttc is not None:
            return self.printed_ttc - self.discount_ttc
        return self.total_ht * (Decimal("1") + self.vat_rate)

    @property
    def vat_percent(self):
        """20 rather than 0.200 - the stored rate is a fraction, and every
        page that showed it raw made people read it as a currency amount."""
        return self.vat_rate * Decimal("100")


class ShopItemPrice(models.Model):
    """What a shop's unnamed receipt line actually was, keyed by its price.

    Some tills print no product names at all - every line on a Sabbh Oriental
    ticket reads "Article divers", and the only thing telling a lemon from a
    bunch of mint is what it cost. This is the operator's own answer to that,
    built up one price at a time from the review screen.

    **Keyed on the unit price, not the line total.** 0,70 EUR shows up as
    3pcs/2,10, 6pcs/4,20, 7pcs/4,90 and 11pcs/7,70 across five receipts:
    keying on the total would need a new entry for every quantity ever
    bought, while the unit price needs one per product. The price is stored
    tax-inclusive because that is what the receipt prints and what the
    operator reads off the photo.

    `valid_from` exists because shop prices move. An entry with no date is
    the standing answer; a dated one takes over from that date on, so a
    price change is recorded rather than overwriting what older invoices
    were priced with.
    """

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="item_prices")
    unit_price_ttc = models.DecimalField(
        max_digits=10, decimal_places=2, help_text="Prix unitaire TTC tel qu'imprimé sur le ticket."
    )
    label = models.CharField(max_length=255, help_text="Le produit correspondant à ce prix.")
    valid_from = models.DateField(
        null=True,
        blank=True,
        help_text="À partir de quand ce prix s'applique. Vide = depuis toujours.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["supplier", "unit_price_ttc", "-valid_from"]
        constraints = [
            # Two constraints rather than one: SQLite treats NULLs as
            # distinct, so a single UniqueConstraint over a nullable
            # valid_from would let unlimited undated duplicates through.
            models.UniqueConstraint(
                fields=["supplier", "unit_price_ttc", "valid_from"],
                condition=~Q(valid_from=None),
                name="unique_shop_item_price_dated",
            ),
            models.UniqueConstraint(
                fields=["supplier", "unit_price_ttc"],
                condition=Q(valid_from=None),
                name="unique_shop_item_price_undated",
            ),
        ]

    def __str__(self):
        return f"{self.supplier} {self.unit_price_ttc} EUR -> {self.label}"


def label_for_unit_price(supplier, unit_price_ttc, on_date=None):
    """The product name recorded for `unit_price_ttc`, or "" if none is.

    Returns the most recent entry that had already taken effect on
    `on_date`; undated entries are the fallback. Never guesses at a nearby
    price - a 0,70 mapping must not answer for a 0,75 line, because that is
    how one product's costs quietly become another's.
    """
    candidates = supplier.item_prices.filter(unit_price_ttc=unit_price_ttc)
    if on_date is not None:
        candidates = candidates.filter(Q(valid_from=None) | Q(valid_from__lte=on_date))
    # ordering puts dated entries (most recent first) ahead of undated ones.
    best = candidates.order_by(models.F("valid_from").desc(nulls_last=True)).first()
    return best.label if best else ""


class ScrapeJob(JobLogMixin):
    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Terminé"
        FAILED = "FAILED", "Échoué"
        CANCELLED = "CANCELLED", "Annulé"

    class Kind(models.TextChoices):
        GATHER = "GATHER", "Récupération"
        TEST = "TEST", "Test de motif"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GATHER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Set by the "Annuler" button (see views.cancel_gather) while the job is
    # still PENDING/RUNNING - the background thread can't be killed outright
    # (no safe way to force-stop a plain Python thread), so this is checked
    # cooperatively between the job's own work units (before each source,
    # and between IMAP batches within one email scan - see tasks.py) and
    # the thread stops itself and sets status to CANCELLED once it notices.
    cancel_requested = models.BooleanField(default=False)
    log = models.TextField(blank=True)
    # {"METRO": {"label": "Metro", "found": 3, "imported": 2}, "type-4": {...}}
    progress = models.JSONField(default=dict, blank=True)
    # Only populated for kind=TEST - [{"sender", "subject", "date", "attachments": [...]}, ...],
    # the matches a pattern test found, shown inline instead of imported. See
    # views.invoice_type_form / tasks.test_email_pattern_task.
    test_matches = models.JSONField(default=list, blank=True)
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
        self.last_heartbeat = timezone.now()
        self.save(update_fields=["log", "last_heartbeat"])

    def update_progress(self, supplier_code: str, label: str = "", **counts):
        entry = self.progress.setdefault(supplier_code, {"label": label, "found": 0, "imported": 0})
        if label:
            entry["label"] = label
        entry.update(counts)
        self.save(update_fields=["progress"])


class ReceiptBatch(JobLogMixin):
    """A batch of receipt photos imported in the background - typically a
    whole folder. See invoices/receipt_batches.py.

    `results` has one entry per file, in upload order:

        {"name": "Franprix 13.06EUR.pdf", "stored": "receipt_batches/12/0003.pdf",
         "status": "pending" | "ok" | "duplicate" | "unrecognised" | "error"
                   | "ignored" | "cancelled",
         "message": "...", "invoice_id": 42, "shop": "Franprix",
         "total": "13.06", "date": "2026-07-15", "verified": true}

    One JSON list rather than a table: it only exists to be shown on the
    batch page. The batch's thread and the requests choosing a shop both
    change it, one entry at a time (receipt_batches.RESULTS_LOCK).
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Terminé"
        FAILED = "FAILED", "Échoué"
        CANCELLED = "CANCELLED", "Annulé"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Checked between files: a thread can't be stopped safely from outside,
    # so the job stops itself (same as ScrapeJob.cancel_requested).
    cancel_requested = models.BooleanField(default=False)
    log = models.TextField(blank=True)
    results = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    #: A receipt takes seconds, and a running batch beats every 15 s
    #: (receipt_batches._Heartbeat): quiet for this long, it is dead - the
    #: dev server restarted, most likely. Ten minutes, the default, is how
    #: long a dead import used to keep showing "En cours".
    STALE_AFTER = timedelta(seconds=90)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Lot de tickets du {self.started_at:%d/%m/%Y %H:%M}"

    def append_log(self, message: str):
        elapsed = (timezone.now() - self.started_at).total_seconds()
        line = f"[+{elapsed:6.1f}s] {message}\n"
        self.log = f"{self.log}{line}"
        # Appended in the database: the batch's thread and a request choosing
        # a shop both write here, each from its own copy of the log.
        # Not a heartbeat: the reaper writes here too, and the line saying a
        # batch is dead must not make it look alive (can_resume). A running
        # batch beats on its own - after every file, and from its _Heartbeat.
        ReceiptBatch.objects.filter(pk=self.pk).update(log=Concat("log", Value(line), output_field=models.TextField()))

    def _count(self, *statuses) -> int:
        return sum(1 for entry in self.results if entry.get("status") in statuses)

    @property
    def to_read(self) -> int:
        """Files that are receipts to recognise - everything but the ignored."""
        return len(self.results) - self._count("ignored")

    @property
    def read(self) -> int:
        return self.to_read - self._count("pending")

    @property
    def progress_percent(self) -> int:
        return round(100 * self.read / self.to_read) if self.to_read else 100

    @property
    def imported_count(self) -> int:
        return self._count("ok")

    @property
    def verified_count(self) -> int:
        return sum(1 for entry in self.results if entry.get("status") == "ok" and entry.get("verified"))

    @property
    def duplicate_count(self) -> int:
        return self._count("duplicate")

    @property
    def failed_count(self) -> int:
        """Files that went nowhere: broken, or no shop recognised and the file
        no longer kept to name it (awaiting_shop_count is the rest)."""
        return self._count("error") + sum(
            1 for entry in self.results if entry["status"] == "unrecognised" and not entry.get("kept")
        )

    @property
    def pending_count(self) -> int:
        return self._count("pending")

    @property
    def awaiting_shop_count(self) -> int:
        """Files no shop was recognised on, kept for the operator to name it."""
        return sum(1 for entry in self.results if entry["status"] == "unrecognised" and entry.get("kept"))

    @property
    def can_resume(self) -> bool:
        """Files left unread by a run that died. Not while it may still be
        running: a batch heard from within STALE_AFTER could be one whose
        machine has just woken up, and resuming it would read files twice."""
        if self.is_active or not self.pending_count:
            return False
        since = self.last_heartbeat or self.started_at
        return timezone.now() - since > self.STALE_AFTER

    @property
    def ignored_count(self) -> int:
        return self._count("ignored")
