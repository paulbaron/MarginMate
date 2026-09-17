from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import forms
from django.utils import timezone

from common import BlankRowTolerantForm

from .models import EmailInvoiceSource, Invoice, InvoiceType, ShopItemPrice, Supplier
from .parsers import LLM_PARSER_KEY, PARSER_REGISTRY


class InvoiceUploadForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all(), label="Fournisseur")
    source_file = forms.FileField(label="Fichier PDF")

    def clean_source_file(self):
        uploaded = self.cleaned_data["source_file"]
        if not uploaded.name.lower().endswith(".pdf"):
            raise forms.ValidationError("Seuls les fichiers PDF sont acceptés.")
        return uploaded


MANUAL_INVOICE_ATTACHMENT_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")
EARLIEST_DOCUMENT_DATE = date(2000, 1, 1)


def check_document_date(value: date | None) -> date:
    """An invoice or ticket is dated, and dated between 2000 and today: every
    stock valuation and the bank match place it by that date, and one filed
    without it sat outside all of them. A future date is a misread year."""
    if value is None:
        raise forms.ValidationError("Saisissez la date du document.")
    today = timezone.localdate()
    if not EARLIEST_DOCUMENT_DATE <= value <= today:
        raise forms.ValidationError(
            f"Date impossible : entre le {EARLIEST_DOCUMENT_DATE:%d/%m/%Y} et aujourd'hui ({today:%d/%m/%Y})."
        )
    return value


class ManualInvoiceForm(forms.ModelForm):
    source_file = forms.FileField(
        label="Justificatif (PDF ou image)",
        required=False,
        help_text="Optionnel - photo ou scan de la facture, pour garder une trace de sa provenance.",
    )

    class Meta:
        model = Invoice
        fields = ["supplier", "invoice_number", "invoice_date"]
        labels = {"supplier": "Fournisseur", "invoice_number": "N° facture", "invoice_date": "Date"}
        widgets = {"invoice_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # blank=True on the model (a parsed invoice's date is best-effort),
        # but a manual entry has no PDF to fall back to guessing a date from
        # - required here so FIFO valuation/stock-take history stays
        # chronologically meaningful.
        self.fields["invoice_date"].required = True

    def clean_invoice_date(self):
        return check_document_date(self.cleaned_data.get("invoice_date"))

    def clean_source_file(self):
        uploaded = self.cleaned_data.get("source_file")
        if uploaded and not uploaded.name.lower().endswith(MANUAL_INVOICE_ATTACHMENT_EXTENSIONS):
            raise forms.ValidationError("Seuls les fichiers PDF, JPG ou PNG sont acceptés.")
        return uploaded


class ManualInvoiceLineForm(BlankRowTolerantForm):
    product_name = forms.CharField(label="Produit", max_length=255)
    # Negative for a refund - a deposit crate or pallet given back, printed
    # "1-" / "15,00-" by Metro - with a negative amount to match (clean).
    quantity = forms.IntegerField(label="Quantité")
    total_ht = forms.DecimalField(label="Total (HT)", max_digits=12, decimal_places=2)
    # Pre-filled, since almost every line is 20% - which means a row where
    # the user typed nothing still submits a VAT rate. That must not make an
    # otherwise-empty row look filled in, or a blank trailing row (and any
    # row removed client-side) blocks the save. See BlankRowTolerantFormMixin.
    vat_rate = forms.DecimalField(
        label="TVA (%)", max_digits=5, decimal_places=2, min_value=Decimal("0"), initial=Decimal("20")
    )

    # The stored line a row shows, when correcting an invoice: it keeps what
    # the form doesn't (see importing.corrected_line).
    line_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    bookkeeping_fields = ("vat_rate", "line_id")
    #: The amount field the sign check reads.
    total_field = "total_ht"

    def clean(self):
        cleaned = super().clean()
        quantity, total = cleaned.get("quantity"), cleaned.get(self.total_field)
        if quantity == 0:
            self.add_error("quantity", "Une quantité ne peut pas être nulle.")
        elif quantity is not None and total is not None and total != 0 and (quantity < 0) != (total < 0):
            # A positive count at a negative price is stock worth less than
            # nothing: the FIFO valuation's worst known failure.
            self.add_error(
                self.total_field,
                "Un retour a une quantité et un montant négatifs, un achat les deux positifs.",
            )
        return cleaned


class BaseManualInvoiceLineFormSet(forms.BaseFormSet):
    def clean(self):
        if any(self.errors):
            return
        has_line = any(form.cleaned_data and not form.cleaned_data.get("DELETE") for form in self.forms)
        if not has_line:
            raise forms.ValidationError("Ajoutez au moins un produit.")


ManualInvoiceLineFormSet = forms.formset_factory(
    ManualInvoiceLineForm, formset=BaseManualInvoiceLineFormSet, extra=1, can_delete=True
)


CENTS = Decimal("0.01")
DOCUMENT_RECEIPT = "receipt"
DOCUMENT_INVOICE = "invoice"


def _to_ht(amount: Decimal, rate: Decimal) -> Decimal:
    return (amount / (Decimal("1") + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


def _to_ttc(amount: Decimal, rate: Decimal) -> Decimal:
    return (amount * (Decimal("1") + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


def plain_volume(value: Decimal) -> Decimal | None:
    """A stored weight as a field shows it: 4.184, 3 - never 3.000, nor the
    "1E+1" Decimal.normalize() makes of 10. None for none."""
    if not value:
        return None
    return value.quantize(Decimal("1")) if value == value.to_integral_value() else value.normalize()


def line_initial(line, document: str) -> dict:
    """A stored line as the correction page shows it.

    Both amounts, HT and TTC, before any promotion: a ticket's as it printed
    them (`printed_ttc`), with the line's share of a promotion beside them;
    a supplier invoice's HT as stored, its TTC worked out. A receipt line
    kept from before promotions were kept apart shows its cost as it is,
    with no promotion. See LineCorrectionForm.amounts."""
    rate = line.vat_rate
    if document == DOCUMENT_RECEIPT and line.printed_ttc is not None:
        total_ttc = line.printed_ttc
        total_ht = line.total_ht + line.discount if line.discount_ttc else line.total_ht
    else:
        total_ttc, total_ht = _to_ttc(line.total_ht, rate), line.total_ht
    return {
        "line_id": line.pk,
        "product_name": line.raw_name,
        "read_as": line.read_as,
        "quantity": line.quantity,
        "total_volume": plain_volume(line.total_volume),
        "total_ht": total_ht,
        "total_ttc": total_ttc,
        "discount_ttc": line.discount_ttc or None,
        "vat_rate": (rate * Decimal("100")).quantize(CENTS),
        "amount_source": "ttc" if document == DOCUMENT_RECEIPT else "ht",
    }


class LineCorrectionForm(BlankRowTolerantForm):
    """One row of the correction page, the same for a ticket and a supplier
    invoice.

    The amount is shown both ways - HT and TTC, each following the other as
    it is typed - and whichever was typed last (`amount_source`, set by the
    page) is the one kept; the other is worked out from it with the line's
    rate. A ticket starts from its TTC, as printed; an invoice from its HT.
    A ticket's promotion sits beside its printed amount (`discount_ttc`)
    rather than inside it, so both can be checked against the photo."""

    product_name = forms.CharField(label="Produit", max_length=255)
    # Negative for a refund, with a negative amount to match (clean).
    quantity = forms.IntegerField(label="Qté")
    # Kilos of a weighed item, litres of a measured one.
    total_volume = forms.DecimalField(
        label="Poids / volume",
        required=False,
        max_digits=12,
        decimal_places=3,
        widget=forms.NumberInput(attrs={"step": "0.001", "placeholder": "kg / L"}),
    )
    total_ht = forms.DecimalField(
        label="Montant HT",
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "HT"}),
    )
    total_ttc = forms.DecimalField(
        label="Montant TTC",
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "TTC"}),
    )
    discount_ttc = forms.DecimalField(
        label="Remise TTC",
        required=False,
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "remise"}),
    )
    vat_rate = forms.DecimalField(label="TVA (%)", max_digits=5, decimal_places=2, min_value=Decimal("0"))
    amount_source = forms.ChoiceField(choices=(("ht", "HT"), ("ttc", "TTC")), required=False, widget=forms.HiddenInput)
    # What OCR read on the ticket, carried through so a corrected line keeps
    # it (InvoiceLine.read_as).
    read_as = forms.CharField(required=False, max_length=255, widget=forms.HiddenInput)
    # The stored line a row shows: it keeps what the form doesn't
    # (importing.corrected_line).
    line_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    bookkeeping_fields = ("vat_rate", "amount_source", "read_as", "line_id")

    def __init__(self, *args, document: str = DOCUMENT_INVOICE, **kwargs):
        super().__init__(*args, **kwargs)
        self.document = document
        # A ticket's lines are food unless it says otherwise; a supplier's
        # invoice is mostly drink.
        self.fields["vat_rate"].initial = Decimal("5.5") if document == DOCUMENT_RECEIPT else Decimal("20")
        self.fields["amount_source"].initial = self.default_source
        if document != DOCUMENT_RECEIPT:
            del self.fields["discount_ttc"]
        # The rows have one heading for all of them: each box still says what
        # it is, to a screen reader and - once the rows wrap - on screen.
        self.fields["quantity"].widget.attrs.setdefault("placeholder", "qté")
        self.fields["vat_rate"].widget.attrs.setdefault("placeholder", "TVA %")
        self.fields["product_name"].widget.attrs.setdefault("placeholder", "Produit")
        if document == DOCUMENT_RECEIPT:
            self.fields["total_volume"].widget.attrs["placeholder"] = "kg"
        for field in self.fields.values():
            if not field.widget.is_hidden:
                field.widget.attrs.setdefault("aria-label", field.label)

    @property
    def default_source(self) -> str:
        return "ttc" if self.document == DOCUMENT_RECEIPT else "ht"

    def _shown(self, name: str) -> Decimal | None:
        """A box as the page draws it - what was typed, or the stored line."""
        if name not in self.fields:
            return None
        value = self[name].value()
        try:
            return Decimal(str(value).strip().replace(",", ".")) if value not in (None, "") else None
        except InvalidOperation:
            return None

    @property
    def unit_price_hint(self) -> str:
        """"soit 0.39 € TTC l'unité" under a row of several: an amount for
        the whole line reads like a price each, and a wrong count only shows
        once divided. The page's script works it out the same way as typed."""
        quantity = self._shown("quantity")
        basis = "ttc" if self.document == DOCUMENT_RECEIPT else "ht"
        amount = self._shown(f"total_{basis}")
        if quantity is None or abs(quantity) <= 1 or amount is None:
            return ""
        each = (amount / quantity).quantize(CENTS, rounding=ROUND_HALF_UP)
        discount = self._shown("discount_ttc") or Decimal("0")
        if not discount:
            return f"soit {each} € {basis.upper()} l'unité"
        net = ((amount - discount) / quantity).quantize(CENTS, rounding=ROUND_HALF_UP)
        return f"soit {net} € TTC l'unité après remise ({each} € avant)"

    def _source(self) -> str:
        source = self.cleaned_data.get("amount_source") or self.default_source
        other = "ht" if source == "ttc" else "ttc"
        if self.cleaned_data.get(f"total_{source}") is None and self.cleaned_data.get(f"total_{other}") is not None:
            return other
        return source

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        quantity = cleaned.get("quantity")
        field = "total_ht" if self._source() == "ht" else "total_ttc"
        amount = cleaned.get(field)
        if amount is None:
            self.add_error("total_ttc", "Saisissez le montant de la ligne, HT ou TTC.")
            return cleaned
        if quantity == 0:
            self.add_error("quantity", "Une quantité ne peut pas être nulle.")
        elif quantity is not None and amount != 0 and (quantity < 0) != (amount < 0):
            # A positive count at a negative price is stock worth less than
            # nothing: the FIFO valuation's worst known failure.
            self.add_error(field, "Un retour a une quantité et un montant négatifs, un achat les deux positifs.")
        discount = cleaned.get("discount_ttc") or Decimal("0")
        if discount and discount > abs(self._printed_ttc()):
            self.add_error("discount_ttc", "La remise dépasse le montant de la ligne.")
        return cleaned

    def _rate(self) -> Decimal:
        return self.cleaned_data["vat_rate"] / Decimal("100")

    def _printed_ttc(self) -> Decimal:
        if self._source() == "ttc":
            return self.cleaned_data["total_ttc"]
        return _to_ttc(self.cleaned_data["total_ht"], self._rate())

    def untouched(self, stored) -> bool:
        """Whether the row still shows the stored line's amounts as the page
        drew them: saved as it is, nothing about its money moves."""
        if stored is None:
            return False
        shown = line_initial(stored, self.document)
        same = all(self.cleaned_data.get(field) == shown[field] for field in ("total_ht", "total_ttc", "vat_rate"))
        return same and (self.cleaned_data.get("discount_ttc") or None) == shown["discount_ttc"]

    def amounts(self, stored=None) -> dict:
        """What the row stores: `total_ht` after any promotion and, for a
        ticket, its printed amount and share of the promotion.

        A row left as drawn keeps the stored figures to the cent. A TTC typed
        is converted with the line's rate - and a total saved untouched comes
        back to its HT: the rounding on the way out is under half a cent once
        divided back."""
        if self.untouched(stored):
            return {
                "total_ht": stored.total_ht,
                "printed_ttc": stored.printed_ttc,
                "discount_ttc": stored.discount_ttc,
                "discount": stored.discount,
            }
        rate = self._rate()
        if self._source() == "ht":
            before = self.cleaned_data["total_ht"]
        else:
            before = _to_ht(self.cleaned_data["total_ttc"], rate)
        if self.document != DOCUMENT_RECEIPT:
            return {"total_ht": before, "printed_ttc": None}
        printed = self._printed_ttc()
        discount = self.cleaned_data.get("discount_ttc") or Decimal("0")
        if not discount:
            return {"total_ht": before, "printed_ttc": printed, "discount_ttc": Decimal("0"), "discount": Decimal("0")}
        net = _to_ht(printed - discount, rate)
        return {"total_ht": net, "printed_ttc": printed, "discount_ttc": discount, "discount": before - net}

    def volume(self, stored=None):
        """The weight typed - or None, for a row still showing the stored
        one: corrected_line then scales it with the count."""
        typed = self.cleaned_data.get("total_volume")
        if stored is not None and typed == plain_volume(stored.total_volume):
            return None
        return typed or Decimal("0")

    @property
    def read_hint(self) -> str:
        """The reading, when it isn't what the row now says - shown under it."""
        read = " ".join(str(self["read_as"].value() or "").split())
        name = " ".join(str(self["product_name"].value() or "").split())
        return read if read.upper() != name.upper() else ""


# No spare row. A blank line under the real ones is a convenience when
# creating a record and reads as a bug when correcting a saved one - a
# receipt whose lines you are checking against a photo shows an empty row
# where an item might have been missed, which is exactly the doubt this
# screen exists to remove. The page adds the first row itself when there is
# nothing to show. See the formset notes in CLAUDE.md.
LineCorrectionFormSet = forms.formset_factory(
    LineCorrectionForm, formset=BaseManualInvoiceLineFormSet, extra=0, can_delete=True
)


class InvoiceTypeForm(forms.ModelForm):
    class Meta:
        model = InvoiceType
        fields = ["name", "supplier", "parser_key", "is_active"]
        labels = {"name": "Nom", "supplier": "Fournisseur", "parser_key": "Parseur", "is_active": "Actif"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "— Saisie manuelle —")] + [
            (key, key) for key in sorted(PARSER_REGISTRY) if key != LLM_PARSER_KEY
        ]
        self.fields["parser_key"] = forms.ChoiceField(choices=choices, required=False, label="Parseur")


class EmailInvoiceSourceForm(forms.ModelForm):
    test_start_date = forms.DateField(
        required=False, label="Tester à partir du", widget=forms.DateInput(attrs={"type": "date"})
    )
    test_end_date = forms.DateField(
        required=False, label="Tester jusqu'au", widget=forms.DateInput(attrs={"type": "date"})
    )

    class Meta:
        model = EmailInvoiceSource
        fields = ["sender_pattern", "subject_pattern", "body_pattern", "attachment_pattern"]
        labels = {
            "sender_pattern": "Expéditeur (regex)",
            "subject_pattern": "Objet (regex)",
            "body_pattern": "Contenu (regex)",
            "attachment_pattern": "Pièce jointe (regex)",
        }


from .ocr import IMAGE_EXTENSIONS  # noqa: E402 - kept next to the only thing using it

RECEIPT_EXTENSIONS = (".pdf",) + IMAGE_EXTENSIONS


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A file field that keeps every file the browser sent.

    Django's own FileField deliberately returns just one, so a plain
    `multiple` attribute silently imports the last photo of a batch and
    discards the rest - which looks exactly like an upload that worked.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data]
        return [single(data, initial)]


class ReceiptBatchUploadForm(forms.Form):
    """Photos of till receipts, several at a time.

    No supplier field: the shop is read off each receipt's own header (see
    receipts.detect_parser). Asking for it per file is the friction that
    ends with a shoebox of unentered receipts.
    """

    files = MultipleFileField(
        label="Photos de tickets",
        required=False,
        help_text="Des fichiers, ou un dossier entier : PDF, JPG, PNG, WebP ou TIFF.",
    )

    def clean_files(self):
        """Keep the receipts, set the rest aside by name.

        A folder carries whatever else is in it - Thumbs.db, desktop.ini, a
        note - and refusing the whole selection over one of those is how a
        folder upload stops being usable. They are listed as ignored on the
        batch page instead. Only a selection with nothing usable is refused.
        """
        uploads = [upload for upload in self.cleaned_data["files"] if upload]
        accepted = [upload for upload in uploads if upload.name.lower().endswith(RECEIPT_EXTENSIONS)]
        self.ignored_names = [upload.name for upload in uploads if upload not in accepted]
        if not accepted:
            raise forms.ValidationError("Aucun PDF ni aucune photo dans la sélection.")
        return accepted


NEW_SHOP = "new"


class ReceiptShopForm(forms.Form):
    """The shop a ticket belongs to, when its header said nothing known: one
    of the suppliers, or a new shop - named, and with the text its tickets
    print at the top, so the next ones are recognised."""

    supplier = forms.CharField(
        label="Enseigne", error_messages={"required": "Choisissez l'enseigne du ticket."}
    )
    new_name = forms.CharField(label="Nom de la nouvelle enseigne", required=False, max_length=255)
    new_header = forms.CharField(label="Texte en tête de ses tickets", required=False, max_length=100)

    def clean_supplier(self):
        value = self.cleaned_data["supplier"].strip()
        if value == NEW_SHOP:
            return NEW_SHOP
        supplier = (
            Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).filter(pk=value).first() if value.isdigit() else None
        )
        if supplier is None:
            raise forms.ValidationError("Enseigne inconnue.")
        return supplier

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("supplier") == NEW_SHOP and not " ".join(cleaned.get("new_name", "").split()):
            self.add_error("new_name", "Donnez un nom à la nouvelle enseigne.")
        return cleaned

    def error_text(self) -> str:
        return " ".join(error for errors in self.errors.values() for error in errors)

    def shop(self, ignoring=()):
        """(supplier, created). Creating one raises ValueError for the
        operator (receipts.create_shop)."""
        from .receipts import create_shop

        supplier = self.cleaned_data["supplier"]
        if supplier != NEW_SHOP:
            return supplier, False
        return create_shop(self.cleaned_data["new_name"], self.cleaned_data.get("new_header", ""), ignoring), True


class DocumentHeaderForm(forms.Form):
    """The document's date and total, on the correction page: the OCR can
    miss either, and a ticket typed in from its photo has neither until it is
    given them. The date is required (check_document_date); the total is what
    the lines are checked against as they are typed, and left blank keeps the
    one read."""

    invoice_date = forms.DateField(
        label="Date",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        error_messages={"required": "Saisissez la date du document."},
    )
    printed_total_ttc = forms.DecimalField(
        label="Total payé (TTC)",
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "Total TTC"}),
    )

    def clean_invoice_date(self):
        return check_document_date(self.cleaned_data.get("invoice_date"))


class ShopItemPriceForm(forms.ModelForm):
    """Teach the app what an unnamed "Article divers" line was.

    The price is the key, so it is not editable once saved - changing it
    would silently re-point every future receipt at a different product.
    Correcting a mistake means deleting the entry and adding the right one.
    """

    class Meta:
        model = ShopItemPrice
        fields = ["unit_price_ttc", "label", "valid_from"]
        labels = {
            "unit_price_ttc": "Prix unitaire TTC",
            "label": "Produit",
            "valid_from": "À partir du",
        }
        widgets = {"valid_from": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, supplier=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.supplier = supplier

    def clean(self):
        """The model's uniqueness involves the shop, which is not a field
        here, so Django does not check it: a price already known reached the
        database and failed there, as a server error."""
        cleaned = super().clean()
        price, valid_from = cleaned.get("unit_price_ttc"), cleaned.get("valid_from")
        if self.supplier is not None and price is not None:
            known = ShopItemPrice.objects.filter(
                supplier=self.supplier, unit_price_ttc=price, valid_from=valid_from
            ).first()
            if known is not None:
                since = f" à partir du {valid_from:%d/%m/%Y}" if valid_from else ""
                raise forms.ValidationError(
                    f"{price} € est déjà retenu chez {self.supplier.name}{since} : « {known.label} ». "
                    "Pour le changer, oubliez-le dans la liste des prix connus, puis retenez le bon."
                )
        return cleaned
