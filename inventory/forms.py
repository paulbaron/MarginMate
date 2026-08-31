from django import forms

from .models import StockType


class StockTypeForm(forms.ModelForm):
    class Meta:
        model = StockType
        fields = ["name", "unit", "category"]
        widgets = {
            "category": forms.TextInput(attrs={"list": "category-datalist", "autocomplete": "off"}),
        }
