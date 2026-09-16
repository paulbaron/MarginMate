from decimal import ROUND_HALF_UP, Decimal

from django import forms

from common import BlankRowTolerantForm

from .models import EmailInvoiceSource, Invoice, InvoiceType, ShopItemPrice, Supplier
from .parsers import LLM_PARSER_KEY, PARSER_REGISTRY


class InvoiceUploadForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all())
    source_file = forms.FileField(label="Fichier PDF")

    def clean_source_file(self):
        uploaded = self.cleaned_data["source_file"]
        if not uploaded.name.lower().endswith(".pdf"):
            raise forms.ValidationError("Seuls les fichiers PDF sont acceptés.")
        return uploaded


MANUAL_INVOICE_ATTACHMENT_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")


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

    def clean_source_file(self):
        uploaded = self.cleaned_data.get("source_file")
        if uploaded and not uploaded.name.lower().endswith(MANUAL_INVOICE_ATTACHMENT_EXTENSIONS):
            raise forms.ValidationError("Seuls les fichiers PDF, JPG ou PNG sont acceptés.")
        return uploaded


class ManualInvoiceLineForm(BlankRowTolerantForm):
    product_name = forms.CharField(label="Produit", max_length=255)
    quantity = forms.IntegerField(label="Quantité", min_value=1)
    total_ht = forms.DecimalField(label="Total (HT)", max_digits=12, decimal_places=2, min_value=Decimal("0"))
    # Pre-filled, since almost every line is 20% - which means a row where
    # the user typed nothing still submits a VAT rate. That must not make an
    # otherwise-empty row look filled in, or a blank trailing row (and any
    # row removed client-side) blocks the save. See BlankRowTolerantFormMixin.
    vat_rate = forms.DecimalField(
        label="TVA (%)", max_digits=5, decimal_places=2, min_value=Decimal("0"), initial=Decimal("20")
    )

    bookkeeping_fields = ("vat_rate",)


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


class ReceiptLineForm(ManualInvoiceLineForm):
    # What OCR read on the ticket, carried through the page untouched so a
    # corrected line keeps it: the reading stays a name its product is known
    # by (InvoiceLine.read_as), and a misreading attached by hand to the
    # right product is recognised on the next ticket. Bookkeeping, so a row
    # carrying nothing else is still a blank row.
    read_as = forms.CharField(required=False, max_length=255, widget=forms.HiddenInput)
    # The ticket prints TTC, so that is what is typed and checked against
    # the photo - converting each price in one's head to check it was the
    # hard part. The line is still stored HT (cleaned_total_ht); the
    # inherited HT field goes.
    total_ht = None
    total_ttc = forms.DecimalField(
        label="Total (TTC)",
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "Total TTC"}),
    )

    # What the screen worked out from HT for a line whose ticket amount is not
    # known (a promotion spread onto it, an old import). Posted back so that
    # saving it untouched does not make a derived figure pass for a printed one.
    computed_ttc = forms.DecimalField(required=False, max_digits=12, decimal_places=2, widget=forms.HiddenInput)

    bookkeeping_fields = ("vat_rate", "read_as", "computed_ttc")

    def cleaned_total_ht(self) -> Decimal:
        """The line's HT total, from the TTC typed and the line's own rate.
        A total saved untouched comes back to the cent it was stored at: the
        rounding on the way out is under half a cent once divided back."""
        rate = self.cleaned_data["vat_rate"] / Decimal("100")
        total_ttc = self.cleaned_data["total_ttc"]
        return (total_ttc / (Decimal("1") + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def printed_ttc(self) -> Decimal | None:
        """The amount to keep as the ticket's: what was typed - unless it is
        the figure worked out from HT for this line, left as it was."""
        typed = self.cleaned_data["total_ttc"]
        computed = self.cleaned_data.get("computed_ttc")
        return None if computed is not None and typed == computed else typed

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
ReceiptLineFormSet = forms.formset_factory(
    ReceiptLineForm, formset=BaseManualInvoiceLineFormSet, extra=0, can_delete=True
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


class ReceiptShopForm(forms.Form):
    """The shop a ticket whose header was unreadable came from."""

    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.exclude(parser_key=LLM_PARSER_KEY),
        label="Enseigne",
        error_messages={"required": "Choisissez l'enseigne du ticket.", "invalid_choice": "Enseigne inconnue."},
    )


class ReceiptDateForm(forms.Form):
    """The ticket's date, on the review screen: the OCR can miss it, and a
    ticket typed in from its photo has none until it is given one."""

    invoice_date = forms.DateField(
        label="Date du ticket",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )


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
                    "Pour le changer, supprimez-le dans Admin → Prix connus des tickets, puis retenez le bon."
                )
        return cleaned
