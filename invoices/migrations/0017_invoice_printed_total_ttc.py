from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0016_invoiceline_printed_ttc"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="printed_total_ttc",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
    ]
