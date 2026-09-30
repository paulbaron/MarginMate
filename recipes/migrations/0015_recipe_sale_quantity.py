"""How much of a recipe goes out with each sale.

Purely additive, and the default is what both readers of `yield_quantity`
already assumed - `margins.computation._recipe_costs` and
`inventory.variance.recipe_usage_terms` both divided the batch by the yield
and called it one sale. At `sale_quantity = 1` the new arithmetic
(`batch * sale_quantity / yield_quantity`) is that same division, so every
recipe already filed keeps the cost and the consumption it has, to the last
decimal. Measured read-only before applying: every recipe in « Unité », and
nearly all of them yielding exactly 1.

Going back is safe too: the column is dropped and nothing else read it.
"""

from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0014_alter_posproduct_total_quantity_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="recipe",
            name="sale_quantity",
            field=models.DecimalField(
                decimal_places=4,
                default=Decimal("1"),
                help_text="Ce qu'une vente prélève sur une préparation complète, dans l'unité produite. Un cocktail vend 1 pour 1 produit ; une terrine de 1,6 kg vendue en parts de 150 g vend 0,15.",
                max_digits=10,
                validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))],
            ),
        ),
    ]
