"""The bar's treasury (« Trésorerie », Banque's fourth tab): the balance of
the account a person read for one day (`TreasuryCheckpoint`, « point de
trésorerie »), and the signed amounts a person makes to settle two points
the imported operations between them do not explain (`TreasuryAdjustment`,
« ajustement »). bank/treasury.py works every other day's balance out of
them and the operations; an adjustment counts there and nowhere else - it is
no `BankTransaction`, so « Dépenses », « Entrées d'argent » and the
reconciliation never read it.

Two empty tables, so nothing existing changes meaning: every operation is
read as before. No foreign key to anything. Reversing drops both tables, and
every point and adjustment typed with them.
"""

from django.db import migrations, models

import bank.models


class Migration(migrations.Migration):
    dependencies = [
        ("bank", "0007_statement_formats"),
    ]

    operations = [
        migrations.CreateModel(
            name="TreasuryAdjustment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "reference",
                    models.CharField(
                        default=bank.models.new_reference,
                        editable=False,
                        max_length=16,
                        unique=True,
                        verbose_name="référence",
                    ),
                ),
                ("date", models.DateField(verbose_name="date")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="montant")),
                ("reason", models.CharField(blank=True, max_length=255, verbose_name="raison")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["date", "pk"],
            },
        ),
        migrations.CreateModel(
            name="TreasuryCheckpoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(unique=True, verbose_name="date")),
                ("balance", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="solde")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["date"],
            },
        ),
    ]
