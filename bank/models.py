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

    AUTOMATIC is « nobody said » on the line: the first active till rule of
    « Reconnaissance des opérations » that recognises the credit decides
    (`OperationRule`, `bank.recognition.till_reading`), else its payer
    retained (`IncomePayer`). It is a member, not just a blank, because the
    « Données » archive checks every value it reads against the choices, and
    a blank line would be refused there.
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
    # for this line alone - a card payout from a terminal whose label no till
    # rule recognises, a deposit for a private event the till took as an
    # « Avoir ». Blank: the first active till rule of « Reconnaissance des
    # opérations » that recognises the line (`OperationRule`), else its payer
    # (`IncomePayer`), decides. Never set on a debit; a statement imported
    # again never touches it (`reconcile.import_statement` only adds lines).
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

    A payment terminal pays out by its provider's transfers, and a new
    terminal is a new label no till rule of « Reconnaissance des opérations »
    knows: one choice on one of its transfers is then enough for every
    transfer it ever sent and will send. Read when the page is drawn, never
    written onto the lines, so « Oublier » puts them straight back. It
    decides only what no till rule recognises (« Pas une vente » included),
    and a line a person chose on its own (`BankTransaction.income_source`)
    beats it (`bank.income.reading_of`).
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


class OperationRule(models.Model):
    """How an operation of the statement is recognised: a pattern searched in
    its label or its operation type, and what the operation is when found.

    Nothing about a bank's words is written in the code: the owner's bank
    is seeded (migration 0006) as rules like any other, and another bank's
    statement is read by editing them on « Reconnaissance des opérations ».
    Two questions, each answered by the first active rule of its kind in
    their order - `bank.recognition` has the rules of reading:

    * what the operation IS (`KIND_MEANINGS`): decided at import and stored on
      the line (`BankTransaction.kind`, `counterparty`, `card_date`);
    * what a credit is in the TILL (`TILL_MEANINGS`): read whenever « Entrées
      d'argent » is drawn, never stored - a card terminal's payout, with or
      without the gross it collected printed in its label, a deposit...
    """

    class Meaning(models.TextChoices):
        # What the operation is - stored at import.
        CARD_PAYMENT = "card_payment", "Paiement par carte"
        DEBIT = "debit", "Prélèvement"
        TRANSFER = "transfer", "Virement"
        OTHER_OPERATION = "other_operation", "Autre opération"
        # What a credit is in the till - read when « Entrées d'argent » is drawn.
        PAYOUT = "payout", "Versement de carte (TPE)"
        CASH = "cash", "Dépôt d'espèces"
        CHEQUE = "cheque", "Remise de chèques"
        VOUCHER = "voucher", "Titres-restaurant"
        CREDIT = "credit", "Avoir"
        NOT_A_SALE = "not_a_sale", "Pas une vente"

    class Searched(models.TextChoices):
        LABEL = "label", "Libellé"
        BANK_TYPE = "bank_type", "Type d'opération"

    #: Unique whatever its case and accents (`bank.recognition.name_key`, the
    #: form's check): the « Données » archive keys a rule by it, and the
    #: pages name a rule by it.
    name = models.CharField("nom", max_length=100, unique=True)
    meaning = models.CharField("signifie", max_length=20, choices=Meaning.choices)
    searched = models.CharField("cherché dans", max_length=10, choices=Searched.choices, default=Searched.LABEL)
    pattern = models.CharField("motif", max_length=300)
    #: The first rule of its kind that finds its pattern decides: the order is
    #: part of what a rule says. Rules of one position are asked by name, never
    #: by id: an id is this database's, and a « Données » import gives new ones.
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        # Imported here: bank.recognition reads this module's choices.
        from returnables.patterns import PatternError

        from .recognition import check

        try:
            check(self.meaning, self.searched, self.pattern)
        except PatternError as error:
            raise ValidationError({"pattern": error.message}) from None


class StatementFormat(models.Model):
    """How one bank lays out its CSV export: the encoding, the separator,
    which column holds what, how dates and amounts are printed, where the
    account number is (`bank.statements.parse_statement` reads a file with
    one; « Format du relevé »).

    The owner's bank is seeded (migration 0007) exactly as the code read it
    before, so every fingerprint already stored is the one the same file
    gives again; another bank's export is read by a format of its own. The
    columns are counted from 1, as a person reads them off the file.
    """

    class Encoding(models.TextChoices):
        AUTO = "auto", "Automatique (UTF-8, sinon Windows-1252)"
        UTF8 = "utf-8", "UTF-8"
        CP1252 = "cp1252", "Windows-1252"
        LATIN1 = "iso-8859-1", "ISO-8859-1"
        UTF16 = "utf-16", "UTF-16"

    class Delimiter(models.TextChoices):
        SEMICOLON = ";", "Point-virgule ( ; )"
        COMMA = ",", "Virgule ( , )"
        TAB = "\t", "Tabulation"
        PIPE = "|", "Barre verticale ( | )"

    class DateFormat(models.TextChoices):
        DAY_MONTH_YEAR = "dd/mm/yyyy", "jj/mm/aaaa"
        DAY_MONTH_SHORT_YEAR = "dd/mm/yy", "jj/mm/aa"
        DAY_MONTH_YEAR_DASHES = "dd-mm-yyyy", "jj-mm-aaaa"
        DAY_MONTH_YEAR_DOTS = "dd.mm.yyyy", "jj.mm.aaaa"
        ISO = "yyyy-mm-dd", "aaaa-mm-jj"
        MONTH_DAY_YEAR = "mm/dd/yyyy", "mm/jj/aaaa"

    class DecimalMark(models.TextChoices):
        COMMA = ",", "Virgule (1 234,56)"
        POINT = ".", "Point (1,234.56)"

    #: Unique whatever its case and accents (`bank.recognition.name_key`):
    #: the import form and the « Données » archive name a format by it.
    name = models.CharField("nom", max_length=100, unique=True)
    #: The first format is the one an import uses when nobody chooses.
    position = models.PositiveIntegerField(default=0)
    encoding = models.CharField("encodage", max_length=12, choices=Encoding.choices, default=Encoding.AUTO)
    delimiter = models.CharField("séparateur", max_length=2, choices=Delimiter.choices, default=Delimiter.SEMICOLON)
    date_format = models.CharField(
        "format des dates", max_length=12, choices=DateFormat.choices, default=DateFormat.DAY_MONTH_YEAR
    )
    decimal_mark = models.CharField(
        "séparateur décimal", max_length=1, choices=DecimalMark.choices, default=DecimalMark.COMMA
    )
    date_column = models.PositiveSmallIntegerField("colonne de la date")
    #: One column or several, joined by a space: « 4 » or « 3, 4 ».
    label_columns = models.CharField("colonnes du libellé", max_length=50)
    #: The amount is ONE signed column, or a column of debits and one of
    #: credits (either may be missing) - `bank.statements.check_format`.
    amount_column = models.PositiveSmallIntegerField("colonne du montant", null=True, blank=True)
    debit_column = models.PositiveSmallIntegerField("colonne des débits", null=True, blank=True)
    credit_column = models.PositiveSmallIntegerField("colonne des crédits", null=True, blank=True)
    value_date_column = models.PositiveSmallIntegerField("colonne de la date de valeur", null=True, blank=True)
    bank_type_column = models.PositiveSmallIntegerField("colonne du type d'opération", null=True, blank=True)
    #: Searched in the lines above the first operation; the whole match, or
    #: its `(?P<compte>…)`, is the account - part of every fingerprint.
    account_pattern = models.CharField("motif du numéro de compte", max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        # Imported here: bank.statements reads this module's choices.
        from .statements import FormatError, check_format

        try:
            check_format(self)
        except FormatError as error:
            raise ValidationError({error.field: error.message}) from None


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
