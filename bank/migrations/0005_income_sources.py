"""What a credit is in the till, said on the line or learnt for its payer.

« Entrées d'argent » recognised a card payout by its label's words alone
(« TOTAL ENCAISSE … EUROS »), and a payment terminal from another provider
prints other words. `BankTransaction.income_source` holds what a person said
one credit is - card, cash, cheque, « Avoir », meal vouchers, or no sale at
all - and `IncomePayer` what they said every credit of one payer is.

Blank everywhere and no payer at first, so nothing existing changes meaning:
every credit is read by the page's rules exactly as before.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bank", "0004_spending_categories"),
    ]

    operations = [
        migrations.AddField(
            model_name="banktransaction",
            name="income_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Automatique"),
                    ("card", "Carte"),
                    ("cash", "Espèces"),
                    ("cheque", "Chèque"),
                    ("credit", "Avoir"),
                    ("voucher", "Titres-restaurant"),
                    ("other", "Pas une vente"),
                ],
                default="",
                max_length=10,
            ),
        ),
        migrations.CreateModel(
            name="IncomePayer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=255, unique=True)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("card", "Carte"),
                            ("cash", "Espèces"),
                            ("cheque", "Chèque"),
                            ("credit", "Avoir"),
                            ("voucher", "Titres-restaurant"),
                            ("other", "Pas une vente"),
                        ],
                        max_length=10,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["key"],
            },
        ),
    ]
