"""The kind of file a statement format reads (`StatementFormat.file_type`,
« Type de fichier » on « Format du relevé »): a CSV laid out in columns, as
every format did until now, an OFX / QFX file or a CAMT.053 (XML ISO 20022)
one - the last two say themselves where each datum is, so a format of them
names no column.

Schema only, no RunPython: every format already stored - the owner's « BNP
Paribas (CSV) » included - becomes `csv` by the new column's default, which
is exactly how it reads today, and keeps every other value (SQLite rebuilds
the table with its rows). The date column may now be blank and the label
columns empty (a CSV still requires both: `bank.statements.check_format`),
and « auto »'s label says it reads UTF-16 too, as it did since 0007.

Going back fails while a format of another kind holds no date column: delete
those first (or restore the backup taken before `migrate_tenants`).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bank", "0008_treasury"),
    ]

    operations = [
        migrations.AddField(
            model_name="statementformat",
            name="file_type",
            field=models.CharField(
                choices=[
                    ("csv", "CSV (colonnes)"),
                    ("ofx", "OFX / QFX (Money)"),
                    ("camt053", "CAMT.053 (XML ISO 20022)"),
                ],
                default="csv",
                max_length=10,
                verbose_name="type de fichier",
            ),
        ),
        migrations.AlterField(
            model_name="statementformat",
            name="date_column",
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="colonne de la date"),
        ),
        migrations.AlterField(
            model_name="statementformat",
            name="encoding",
            field=models.CharField(
                choices=[
                    ("auto", "Automatique (UTF-16 ou UTF-8 selon le fichier, sinon Windows-1252)"),
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
        migrations.AlterField(
            model_name="statementformat",
            name="label_columns",
            field=models.CharField(blank=True, max_length=50, verbose_name="colonnes du libellé"),
        ),
    ]
