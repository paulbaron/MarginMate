"""What a spending was for, on the line and on the rule that recognises it.

« Dépenses » puts every debit of a period in a category: the ones an invoice
explains take the categories of what that invoice bought, and the ones no
invoice explains take a category a person types. `BankTransaction.category`
holds that; `IgnoreRule.category` lets a recurring payment (a loan, the
URSSAF, the salaries) categorise itself instead of being typed every month.

Both are blank everywhere until somebody fills one in, so nothing existing
changes meaning: a line with no category reads as « sans catégorie », which
is exactly what it was before this field existed.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bank", "0003_invoice_paid_by_several_lines"),
    ]

    operations = [
        migrations.AddField(
            model_name="banktransaction",
            name="category",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="ignorerule",
            name="category",
            field=models.CharField(
                blank=True,
                help_text="Ce que ces dépenses comptent comme sur « Dépenses ». Facultatif.",
                max_length=255,
                verbose_name="catégorie",
            ),
        ),
    ]
