"""The rules that recognise what an operation of the statement is
(`OperationRule`, « Reconnaissance des opérations »), seeded with exactly
what the code recognised before - so nothing existing changes meaning:

* at import, `bank/statements.py` read a card payment (« FACTURE CARTE DU
  ddmmyy <payee> CARTE 1234XXXXXXXX5678 »), a direct debit (« PRLV SEPA
  [B2B ]<payee> ECH/ »), and a transfer (a label starting « VIR », its payee
  after « /BEN » or « /FRM »), in that order;
* on « Entrées d'argent », `bank/income.py` read a card terminal's payout
  by « TOTAL ENCAISSE <gross> EURO(S) » in the label, then cash and cheques
  by the operation type (« VERSEMENT ESPECES », « REMISE CHEQUE », case and
  accents aside).

Those words were one bank's and one terminal's, written in the code; they
are rows now, which another bank's statement replaces on the page. The
kinds, payees and card dates already stored are left as they are, and every
credit reads as before (bank/tests/test_recognition.py replays the old
readings against these rows).

The data is written here, not imported from the app: a migration must
replay the same way whatever the code has become since (returnables/0002,
invoices/0007). `get_or_create` by name, so a rule somebody already made
under that name is left as it is. Reversing does nothing: the table goes
with the CreateModel.
"""

from django.db import migrations, models

#: (position, name, meaning, searched, pattern), in the order they are asked.
RULES = [
    (
        1,
        "Paiement par carte (FACTURE CARTE)",
        "card_payment",
        "label",
        r"FACTURE CARTE DU (?P<jour>[0-9]{2})(?P<mois>[0-9]{2})(?P<annee>[0-9]{2}) (?P<tiers>.*?)\s+CARTE\s+[0-9]{4}X+[0-9]{4}",
    ),
    (2, "Prélèvement (PRLV SEPA)", "debit", "label", r"^PRLV SEPA (?:B2B )?(?P<tiers>.*?) ECH/"),
    (3, "Virement émis (/BEN)", "transfer", "label", r"^VIR.*?/BEN (?P<tiers>.*?) /REFDO"),
    (4, "Virement reçu (/FRM)", "transfer", "label", r"^VIR.*?/FRM (?P<tiers>.*?) /"),
    (5, "Autre virement (VIR)", "transfer", "label", r"^VIR"),
    (
        6,
        "Versement carte (TOTAL ENCAISSE)",
        "payout",
        "label",
        r"TOTAL\s+ENCAISS[EÉ]\s+(?P<encaisse>(?:[0-9]{1,3}(?:\s[0-9]{3})+|[0-9]+)(?:[.,][0-9]+)?)\s+EUROS?\b",
    ),
    (7, "Dépôt d'espèces (VERSEMENT ESPECES)", "cash", "bank_type", r"VERSEMENT ESPECES"),
    (8, "Remise de chèques (REMISE CHEQUE)", "cheque", "bank_type", r"REMISE CHEQUE"),
]


def seed(apps, schema_editor):
    OperationRule = apps.get_model("bank", "OperationRule")
    for position, name, meaning, searched, pattern in RULES:
        OperationRule.objects.get_or_create(
            name=name,
            defaults={
                "position": position,
                "meaning": meaning,
                "searched": searched,
                "pattern": pattern,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("bank", "0005_income_sources"),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True, verbose_name="nom")),
                (
                    "meaning",
                    models.CharField(
                        choices=[
                            ("card_payment", "Paiement par carte"),
                            ("debit", "Prélèvement"),
                            ("transfer", "Virement"),
                            ("other_operation", "Autre opération"),
                            ("payout", "Versement de carte (TPE)"),
                            ("cash", "Dépôt d'espèces"),
                            ("cheque", "Remise de chèques"),
                            ("voucher", "Titres-restaurant"),
                            ("credit", "Avoir"),
                            ("not_a_sale", "Pas une vente"),
                        ],
                        max_length=20,
                        verbose_name="signifie",
                    ),
                ),
                (
                    "searched",
                    models.CharField(
                        choices=[("label", "Libellé"), ("bank_type", "Type d'opération")],
                        default="label",
                        max_length=10,
                        verbose_name="cherché dans",
                    ),
                ),
                ("pattern", models.CharField(max_length=300, verbose_name="motif")),
                ("position", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True, verbose_name="active")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["position", "name"],
            },
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
