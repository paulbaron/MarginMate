"""« utilise les accès du serveur »'s help no longer names the AI reading,
removed on 04/10/2026 (invoices/0038): « Metro, la boîte aux lettres,
L'Addition et les portails du fichier .env. »

The words of the admin only: no column changes, no row is read or written,
and going back puts the old words back.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_pushdevice"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="uses_server_integrations",
            field=models.BooleanField(
                default=False,
                help_text="Metro, la boîte aux lettres, L'Addition et les portails du fichier .env.",
                verbose_name="utilise les accès du serveur",
            ),
        ),
    ]
