from django import forms

from .models import IgnoreRule


class IgnoreRuleForm(forms.ModelForm):
    class Meta:
        model = IgnoreRule
        fields = ["pattern", "description"]
        labels = {"pattern": "Motif", "description": "Nom"}
        help_texts = {
            "pattern": (
                "Expression régulière cherchée dans le libellé complet de l'opération, sans tenir compte des "
                "majuscules. Ex : ECHEANCE PRET, URSSAF, DGFIP|MALAKOFF."
            ),
            "description": "Pour s'y retrouver : « Prêt », « Cotisations sociales »…",
        }
