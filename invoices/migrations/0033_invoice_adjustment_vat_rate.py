"""The VAT rate a document-level charge carries, when the document says.

`Invoice.adjustment_vat_rate` is additive and null for everything already
filed, which is exactly right: null means « nothing stated it », and
`Invoice.adjustment_ttc` then deduces the rate from the lines the way it
always has. Only an EN 16931 invoice fills it in, from BT-96/BT-103.

Nothing is backfilled and nothing is guessed at. The rate on a UBA PDF's
duty is not stated anywhere in the document, so writing one now would be
this migration inventing a figure - which is the one thing CLAUDE.md says a
migration must never do.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0032_invoice_einvoice_format"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="adjustment_vat_rate",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=5, null=True),
        ),
    ]
