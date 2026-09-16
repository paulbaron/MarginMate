from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0013_help_text_fr'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktype',
            name='loss_percent',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('10'),
                help_text='Part des achats perdue avant la vente (débordement, fonds de bouteille…). 10 % par défaut.',
                max_digits=5,
                validators=[
                    django.core.validators.MinValueValidator(Decimal('0')),
                    django.core.validators.MaxValueValidator(Decimal('100')),
                ],
            ),
        ),
    ]
