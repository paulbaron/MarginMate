"""The layouts of a bank's CSV export (`StatementFormat`, « Format du
relevé »), seeded with exactly the one the code read before - so nothing
existing changes meaning, and every fingerprint already stored is the one the
same file gives again:

* encoding: UTF-8 (with or without its byte order mark), else Windows-1252;
* « ; » between the cells; a row is an operation when its 1st cell is a
  dd/mm/yyyy date; the label is the 4th cell, its spaces collapsed; the
  amount the 6th, French decimals, signed; the value date the 5th; the
  operation type the 2nd (the 3rd, a short type, was never read);
* the account: « **** » and digits, in a line above the first operation.

That layout was one bank's (BNP Paribas'), written in bank/statements.py;
it is a row now, which another bank's format stands beside on the page
(bank/tests/test_statement_formats.py replays the old reading against it).

The data is written here, not imported from the app: a migration must
replay the same way whatever the code has become since (returnables/0002,
bank/0006). `get_or_create` by name; reversing does nothing - the table goes
with the CreateModel.
"""

from django.db import migrations, models

#: The owner's bank's export, as bank/statements.py read it.
NAME = "BNP Paribas (CSV)"
FORMAT = {
    "position": 1,
    "encoding": "auto",
    "delimiter": ";",
    "date_format": "dd/mm/yyyy",
    "decimal_mark": ",",
    "date_column": 1,
    "bank_type_column": 2,
    "label_columns": "4",
    "value_date_column": 5,
    "amount_column": 6,
    "account_pattern": r"\*{2,}[0-9]+",
}


def seed(apps, schema_editor):
    StatementFormat = apps.get_model("bank", "StatementFormat")
    StatementFormat.objects.get_or_create(name=NAME, defaults=FORMAT)


class Migration(migrations.Migration):
    dependencies = [
        ("bank", "0006_operation_rules"),
    ]

    operations = [
        migrations.CreateModel(
            name="StatementFormat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True, verbose_name="nom")),
                ("position", models.PositiveIntegerField(default=0)),
                (
                    "encoding",
                    models.CharField(
                        choices=[
                            ("auto", "Automatique (UTF-8, sinon Windows-1252)"),
                            ("utf-8", "UTF-8"),
                            ("cp1252", "Windows-1252"),
                            ("iso-8859-1", "ISO-8859-1"),
                            ("utf-16", "UTF-16"),
                        ],
                        default="auto",
                        max_length=12,
                        verbose_name="encodage",
                    ),
                ),
                (
                    "delimiter",
                    models.CharField(
                        choices=[
                            (";", "Point-virgule ( ; )"),
                            (",", "Virgule ( , )"),
                            ("\t", "Tabulation"),
                            ("|", "Barre verticale ( | )"),
                        ],
                        default=";",
                        max_length=2,
                        verbose_name="séparateur",
                    ),
                ),
                (
                    "date_format",
                    models.CharField(
                        choices=[
                            ("dd/mm/yyyy", "jj/mm/aaaa"),
                            ("dd/mm/yy", "jj/mm/aa"),
                            ("dd-mm-yyyy", "jj-mm-aaaa"),
                            ("dd.mm.yyyy", "jj.mm.aaaa"),
                            ("yyyy-mm-dd", "aaaa-mm-jj"),
                            ("mm/dd/yyyy", "mm/jj/aaaa"),
                        ],
                        default="dd/mm/yyyy",
                        max_length=12,
                        verbose_name="format des dates",
                    ),
                ),
                (
                    "decimal_mark",
                    models.CharField(
                        choices=[(",", "Virgule (1 234,56)"), (".", "Point (1,234.56)")],
                        default=",",
                        max_length=1,
                        verbose_name="séparateur décimal",
                    ),
                ),
                ("date_column", models.PositiveSmallIntegerField(verbose_name="colonne de la date")),
                ("label_columns", models.CharField(max_length=50, verbose_name="colonnes du libellé")),
                (
                    "amount_column",
                    models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="colonne du montant"),
                ),
                (
                    "debit_column",
                    models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="colonne des débits"),
                ),
                (
                    "credit_column",
                    models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="colonne des crédits"),
                ),
                (
                    "value_date_column",
                    models.PositiveSmallIntegerField(
                        blank=True, null=True, verbose_name="colonne de la date de valeur"
                    ),
                ),
                (
                    "bank_type_column",
                    models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="colonne du type d'opération"),
                ),
                (
                    "account_pattern",
                    models.CharField(blank=True, max_length=300, verbose_name="motif du numéro de compte"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["position", "name"],
            },
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
