"""An item counted in bottles keeps the size of one: `item_size` (in
`size_unit`, the article's unit when added), set only when the number counts
items (`unit` ""). No data moves: an item already on a list reads as before.
Reversing drops the two columns: an item counted in bottles of an article
then reads as a bare number.

The constraint holds both or neither, the size above 0, only beside a number
of items. Its second branch says `item_size` is not null on its own: NULL > 0
is NULL, which a CHECK lets through, so without it a `size_unit` with no size
passed beside `unit` "".
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0022_shopping_lists"),
    ]

    operations = [
        migrations.AddField(
            model_name="shoppinglistitem",
            name="item_size",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="shoppinglistitem",
            name="size_unit",
            field=models.CharField(
                blank=True, choices=[("L", "Litre"), ("UNIT", "Unité"), ("KG", "Kilogramme")], max_length=4
            ),
        ),
        migrations.AddConstraint(
            model_name="shoppinglistitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("item_size__isnull", True), ("size_unit", "")),
                    models.Q(
                        ("item_size__isnull", False),
                        ("item_size__gt", 0),
                        ("size_unit__in", ["L", "UNIT", "KG"]),
                        ("unit", ""),
                    ),
                    _connector="OR",
                ),
                name="shopping_list_item_size_of_an_item",
            ),
        ),
    ]
