"""Bank statement lines, and which invoices each one paid.

A spending line on the statement is the ground truth of what was really
paid. Linking it to its invoice shows the two things nothing else can: an
invoice that was never imported (money left, nothing to show for it), and a
receipt the OCR misread (the ticket says 13,02, the bank says 13,06).
"""

import secrets
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from common import format_money


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
    """How one bank's export is read (`bank.statements.parse_statement`;
    « Format du relevé »): the kind of file (`file_type`) and its encoding -
    and, for a CSV, the separator, which column holds what, how dates and
    amounts are printed, where the account number is. An OFX or a CAMT.053
    file says itself where each datum is: its columns are blank.

    The owner's bank is seeded (migration 0007) exactly as the code read it
    before, so every fingerprint already stored is the one the same file
    gives again; another bank's export is read by a format of its own. The
    columns are counted from 1, as a person reads them off the file.
    """

    class FileType(models.TextChoices):
        CSV = "csv", "CSV (colonnes)"
        OFX = "ofx", "OFX / QFX (Money)"
        CAMT053 = "camt053", "CAMT.053 (XML ISO 20022)"

    class Encoding(models.TextChoices):
        AUTO = "auto", "Automatique (UTF-16 ou UTF-8 selon le fichier, sinon Windows-1252)"
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
    #: Every format stored before migration 0009 is a CSV, as it always read.
    file_type = models.CharField("type de fichier", max_length=10, choices=FileType.choices, default=FileType.CSV)
    encoding = models.CharField("encodage", max_length=12, choices=Encoding.choices, default=Encoding.AUTO)
    delimiter = models.CharField("séparateur", max_length=2, choices=Delimiter.choices, default=Delimiter.SEMICOLON)
    date_format = models.CharField(
        "format des dates", max_length=12, choices=DateFormat.choices, default=DateFormat.DAY_MONTH_YEAR
    )
    decimal_mark = models.CharField(
        "séparateur décimal", max_length=1, choices=DecimalMark.choices, default=DecimalMark.COMMA
    )
    #: Required of a CSV (`bank.statements.check_format`); blank for a file
    #: that says where each datum is.
    date_column = models.PositiveSmallIntegerField("colonne de la date", null=True, blank=True)
    #: One column or several, joined by a space: « 4 » or « 3, 4 ».
    label_columns = models.CharField("colonnes du libellé", max_length=50, blank=True)
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
        # Through the guard of returnables.patterns, as every typed pattern
        # is: `re.compile` raised OverflowError on « A{4294967296} » (a 500
        # on the form and on « Données »'s preview), and compiled what could
        # freeze the machine. ".*", "URSSAF|" or "^" match an empty label,
        # so every payment, and are refused there: one typo would hide
        # everything still missing its invoice. Imported here: bank.rules
        # reads bank.recognition, which reads this module.
        from returnables.patterns import PatternError

        from .rules import check

        try:
            check(self.pattern)
        except PatternError as error:
            raise ValidationError({"pattern": error.message}) from None


#: What `TreasuryCheckpoint.clean` and `TreasuryAdjustment.clean` say, on the
#: field (French: LANGUAGE_CODE is en-us, and « Données » shows them). The
#: bounds are bank.statements' - a statement's days, the (12, 2) column.
TREASURY_DATE_OUT_OF_BOUNDS = "Date hors limites : entre le {first:%d/%m/%Y} et le {last:%d/%m/%Y}."
TREASURY_BALANCE_TOO_BIG = "Solde hors limites : {limit} € au plus, en plus ou en moins."
TREASURY_AMOUNT_TOO_BIG = "Montant hors limites : {limit} € au plus, en plus ou en moins."
TREASURY_AMOUNT_ZERO = "Un ajustement de 0 € ne change rien : tapez un montant."


def _treasury_refusals(day, amount, amount_field: str, too_big: str, bounds) -> dict:
    """The French refusals both treasury models share, by field: the day
    outside a statement's (2000-2099), the amount wider than its (12, 2)
    column. `bounds` is bank.statements' (FIRST_DAY, LAST_DAY, MAX_AMOUNT).
    A value its field could not read - None, text, a NaN: `full_clean` calls
    `clean` even then - is left to the field's own refusal."""
    first, last, limit = bounds
    refusals = {}
    if isinstance(day, date) and not isinstance(day, datetime) and not first <= day <= last:
        refusals["date"] = TREASURY_DATE_OUT_OF_BOUNDS.format(first=first, last=last)
    if isinstance(amount, Decimal) and amount.is_finite() and abs(amount) > limit:
        refusals[amount_field] = too_big.format(limit=format_money(limit))
    return refusals


class TreasuryCheckpoint(models.Model):
    """A balance of the account a person read for one day - « point de
    trésorerie », typed on « Trésorerie ».

    **The balance at the END of that day**, every operation booked that day
    included (by `BankTransaction.operation_date`, the date every Banque page
    reads): what the bank shows once the day is over. Every other day's
    balance is worked out from it by the operations between
    (`bank.treasury`, which has the rules of reading), so the typed figure is
    exact on its own day and the next day reads it plus that day's
    movements. With several accounts imported it is their TOTAL.

    One per day: two readings of one day are one reading corrected, and the
    page replaces a balance only when asked to (its date's uniqueness is
    refused in French by the code that saves). Signed - an overdraft is
    negative. No foreign key: a point stands whatever lines are imported,
    cleared or imported again, and that is exactly what lets two points
    disagree, which the page then asks to resolve.
    """

    date = models.DateField("date", unique=True)
    #: Signed: an overdraft is negative.
    balance = models.DecimalField("solde", max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date:%d/%m/%Y} {self.balance}"

    def clean(self):
        # Imported here: bank.statements imports this module (StatementFormat).
        from .statements import FIRST_DAY, LAST_DAY, MAX_AMOUNT

        bounds = (FIRST_DAY, LAST_DAY, MAX_AMOUNT)
        refusals = _treasury_refusals(self.date, self.balance, "balance", TREASURY_BALANCE_TOO_BIG, bounds)
        if refusals:
            raise ValidationError(refusals)


def new_reference() -> str:
    """A `TreasuryAdjustment`'s reference: 16 random hex characters. A module
    function, so migration bank/0008 names it rather than freezing one value."""
    return secrets.token_hex(8)


class TreasuryAdjustment(models.Model):
    """A signed amount, dated on one day, that the imported operations do not
    carry - « ajustement », made by a person on « Trésorerie » to settle two
    points the operations between them do not explain (money that moved
    with no line on the statement, a figure nobody can find).

    **It counts in the treasury ONLY**, and only while it lies between two
    points (after the first point's day, up to the last's): left outside by a
    point deleted, or brought by « Données », it counts nowhere and the page
    lists it « ne compte pas » (`bank.treasury`, which has the rules).

    **It is not a `BankTransaction`**, on purpose: a line would need a
    fingerprint the statement never gives, would be counted as a debit or a
    credit by « Dépenses », « Entrées d'argent » and the reconciliation, and
    would be offered invoices to pay. None of those pages reads this table.
    No foreign key either: it names its day, not the two points it settled -
    a point deleted or moved leaves it to be judged by the rule above.
    """

    #: Its natural key - what « Données » names it by (an id is this
    #: database's).
    reference = models.CharField("référence", max_length=16, unique=True, default=new_reference, editable=False)
    date = models.DateField("date")
    #: Signed, as `BankTransaction.amount`: negative when money went out.
    amount = models.DecimalField("montant", max_digits=12, decimal_places=2)
    #: Never « motif », which means a regular expression on the bank's pages.
    reason = models.CharField("raison", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "pk"]

    def __str__(self):
        return f"{self.date:%d/%m/%Y} {self.amount}"

    def clean(self):
        # Imported here: bank.statements imports this module (StatementFormat).
        from .statements import FIRST_DAY, LAST_DAY, MAX_AMOUNT

        bounds = (FIRST_DAY, LAST_DAY, MAX_AMOUNT)
        refusals = _treasury_refusals(self.date, self.amount, "amount", TREASURY_AMOUNT_TOO_BIG, bounds)
        if isinstance(self.amount, Decimal) and self.amount == 0:
            refusals.setdefault("amount", TREASURY_AMOUNT_ZERO)
        if refusals:
            raise ValidationError(refusals)
