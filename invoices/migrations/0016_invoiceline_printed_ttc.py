from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0015_invoiceline_read_as"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoiceline",
            name="printed_ttc",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
    ]
