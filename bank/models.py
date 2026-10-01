"""Bank statement lines, and which invoices each one paid.

A spending line on the statement is the ground truth of what was really
paid. Linking it to its invoice shows the two things nothing else can: an
invoice that was never imported (money left, nothing to show for it), and a
receipt the OCR misread (the ticket says 13,02, the bank says 13,06).
"""

import re

from django.core.exceptions import ValidationError
from django.db import models


class IncomeSource(models.TextChoices):
    """What a credit is in the till, as « Entrées d'argent » reads it - the
    one vocabulary of `BankTransaction.income_source`, `IncomePayer.source`,
    bank/income.py and the page's « En caisse » menu.

    AUTOMATIC is « nobody said » on the line: the page's rules decide where
    they recognise it (the label's « TOTAL ENCAISSE … EUROS », the bank's
    deposit types), else its payer retained (`IncomePayer`). It is a member,
    not just a blank, because the « Données » archive checks every value it
    reads against the choices, and a blank line would be refused there.
    """

    AUTOMATIC = "", "Automatique"
    CARD = "card", "Carte"
    CASH = "cash", "Espèces"
    CHEQUE = "cheque", "Chèque"
    #: The till's « Avoir »: in a bar, mostly a deposit paid beforehand,
    #: often by transfer, for a private event.
    CREDIT = "credit", "Avoir"
    VOUCHER = "voucher", "Titres-restaurant"
    #: Money the till never saw: a contribution, a refund, a private party.
    OTHER = "other", "Pas une vente"


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
    # What this spending was FOR, typed by a person on « Dépenses », for the
    # lines no invoice explains. Free text with a datalist of what already
    # exists, like StockType.category: a list of categories to administer is
    # one more thing to keep up to date, and the words a person types are the
    # ones they will look for. Blank means nobody has said - which the page
    # counts and lists first, never folds into « Autres ».
    category = models.CharField(max_length=255, blank=True)
    # What this CREDIT is in the till, said by a person on « Entrées d'argent »
    # for this line alone - a card payout from a terminal whose label the
    # page does not recognise, a deposit for a private event the till took
    # as an « Avoir ». Blank: the page's rules where they recognise the line,
    # else its payer (`IncomePayer`), decide. Never set on a debit; a statement imported again
    # never touches it (`reconcile.import_statement` only adds lines).
    income_source = models.CharField(max_length=10, blank=True, default="", choices=IncomeSource.choices)
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
    """One invoice paid by one bank line - and NEITHER side is exclusive.

    A line pays several invoices (one debit for two deliveries and a
    returned deposit) and an invoice is paid by several lines (settled in
    two goes, or a debit split). Both are rare and both are real, and the
    invoice side used to be a OneToOneField: a person could not record what
    had happened, which is the one thing this table is for.

    The pair is what cannot repeat: the same invoice twice on the same line
    says nothing and would count that invoice twice in every figure the page
    adds up. The constraint refuses it rather than trusting every caller.

    Only a person makes the unusual ones. `reconcile.reconcile` still links
    an invoice nothing pays yet and nothing else - see there.
    """

    class Method(models.TextChoices):
        AUTO = "AUTO", "Automatique"
        MANUAL = "MANUAL", "À la main"

    transaction = models.ForeignKey(BankTransaction, on_delete=models.CASCADE, related_name="payments")
    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=10, choices=Method.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["invoice__invoice_date", "id"]
        constraints = [
            models.UniqueConstraint(fields=["transaction", "invoice"], name="unique_transaction_invoice_payment")
        ]


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


class IncomePayer(models.Model):
    """A payer whose credits are all one thing in the till - learnt when a
    person says what one of them is on « Entrées d'argent » and leaves
    « retenir pour ce payeur » ticked.

    A payment terminal is recognised by its provider's transfers, and a new
    terminal is a new label the page's rules do not know: one choice on one
    of its transfers is then enough for every transfer it ever sent and will
    send. Read when the page is drawn, never written onto the lines, so
    « Oublier » puts them straight back. It decides only what the rules do
    not recognise, and a line a person chose on its own
    (`BankTransaction.income_source`) beats it (`bank.income.reading_of`).
    """

    #: `bank.income.payer_key` of a line: `matching.alias_key` of who the
    #: bank says paid, else of the label's words without their digits.
    key = models.CharField(max_length=255, unique=True)
    source = models.CharField(max_length=10, choices=[choice for choice in IncomeSource.choices if choice[0]])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} = {self.get_source_display()}"


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
    #: What the payments this rule catches count as on « Dépenses ». The
    #: loan, the URSSAF and the salaries are the same spending every month,
    #: and typing their category one line at a time for ever is work a rule
    #: already knows how to do. Optional: a rule may well say « nothing to
    #: link » without claiming to say what the money was for.
    category = models.CharField(
        "catégorie",
        max_length=255,
        blank=True,
        help_text="Ce que ces dépenses comptent comme sur « Dépenses ». Facultatif.",
    )
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
