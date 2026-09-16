"""Bank statement lines, and which invoices each one paid.

A spending line on the statement is the ground truth of what was really
paid. Linking it to its invoice shows the two things nothing else can: an
invoice that was never imported (money left, nothing to show for it), and a
receipt the OCR misread (the ticket says 13,02, the bank says 13,06).
"""

import re

from django.core.exceptions import ValidationError
from django.db import models


class BankTransaction(models.Model):
    class Kind(models.TextChoices):
        CARD = "CARD", "Carte"
        DEBIT = "DEBIT", "Prélèvement"
        TRANSFER = "TRANSFER", "Virement"
        OTHER = "OTHER", "Autre"

    account = models.CharField(max_length=40, blank=True)
    operation_date = models.DateField()
    value_date = models.DateField(null=True, blank=True)
    # When the card was used ("FACTURE CARTE DU 150726") - a day or three
    # before the bank books it, and the date the receipt carries.
    card_date = models.DateField(null=True, blank=True)
    bank_type = models.CharField(max_length=80, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    label = models.TextField()
    # Who was paid, as the bank spells it: the card merchant, the direct
    # debit's creditor, a transfer's beneficiary.
    counterparty = models.CharField(max_length=255, blank=True)
    # Negative when money went out.
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # The same operation in two overlapping exports is imported once.
    fingerprint = models.CharField(max_length=64, unique=True)
    # Rent, salaries, taxes, a loan: nothing to link.
    no_invoice = models.BooleanField(default=False)
    # A person decided this line - linked it, unlinked it, or said there is
    # no invoice - so the automatic pass never touches it again.
    settled_by_hand = models.BooleanField(default=False)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-operation_date", "-id"]

    def __str__(self):
        return f"{self.operation_date:%d/%m/%Y} {self.counterparty or self.label[:40]} {self.amount}"

    @property
    def amount_due(self):
        return -self.amount

    @property
    def paid_on(self):
        return self.card_date or self.operation_date


class InvoicePayment(models.Model):
    """One invoice, paid by one bank line. A line can pay several - one debit
    for two deliveries and a returned deposit - but an invoice is paid once."""

    class Method(models.TextChoices):
        AUTO = "AUTO", "Automatique"
        MANUAL = "MANUAL", "À la main"

    transaction = models.ForeignKey(BankTransaction, on_delete=models.CASCADE, related_name="payments")
    invoice = models.OneToOneField("invoices.Invoice", on_delete=models.CASCADE, related_name="payment")
    method = models.CharField(max_length=10, choices=Method.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["invoice__invoice_date", "id"]


class CounterpartyAlias(models.Model):
    """A payee name the bank gives a supplier that its own name doesn't
    contain ("SUMUP *BK PREM") - learnt when a person links such a line."""

    supplier = models.ForeignKey("invoices.Supplier", on_delete=models.CASCADE, related_name="bank_aliases")
    # As bank.matching.alias_key writes it.
    name = models.CharField(max_length=255)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["supplier", "name"], name="unique_supplier_bank_alias")]

    def __str__(self):
        return f"{self.name} = {self.supplier}"


class IgnoreRule(models.Model):
    """Payments that never have an invoice - a loan, URSSAF, salaries - by a
    pattern on their label.

    A line it matches counts as "pas de facture attendue" rather than as a
    missing invoice, and the automatic pass leaves it alone. Applied when the
    page is drawn rather than stored on the lines, so pausing or deleting a
    rule puts its payments straight back. A line that already has its invoice
    is never hidden by one.
    """

    pattern = models.CharField(
        "motif",
        max_length=255,
        help_text="Expression régulière cherchée dans le libellé complet, sans tenir compte des majuscules.",
    )
    description = models.CharField("nom", max_length=255, blank=True)
    is_active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["description", "pattern"]

    def __str__(self):
        return self.description or self.pattern

    def clean(self):
        try:
            regex = re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValidationError({"pattern": f"Expression régulière invalide : {exc}."}) from None
        # ".*", "URSSAF|" or "^" match an empty label, so every payment: one
        # typo would hide everything still missing its invoice.
        if regex.search("") is not None:
            raise ValidationError({"pattern": "Ce motif correspond à n'importe quelle opération : précisez-le."})
