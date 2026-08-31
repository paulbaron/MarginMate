from django import forms

from .models import Supplier


class InvoiceUploadForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all())
    source_file = forms.FileField(label="Fichier PDF")

    def clean_source_file(self):
        uploaded = self.cleaned_data["source_file"]
        if not uploaded.name.lower().endswith(".pdf"):
            raise forms.ValidationError("Seuls les fichiers PDF sont acceptés.")
        return uploaded
