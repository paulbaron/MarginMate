from decimal import Decimal

from django import forms

from common import BlankRowTolerantForm

from .models import EmailInvoiceSource, Invoice, InvoiceType, Supplier
from .parsers import PARSER_REGISTRY


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


class InvoiceTypeForm(forms.ModelForm):
    class Meta:
        model = InvoiceType
        fields = ["name", "supplier", "parser_key", "is_active"]
        labels = {"name": "Nom", "supplier": "Fournisseur", "parser_key": "Parseur", "is_active": "Actif"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "— Analyse IA automatique —")] + [
            (key, key) for key in sorted(PARSER_REGISTRY) if key != "LLM"
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
