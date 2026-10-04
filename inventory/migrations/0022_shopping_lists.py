"""« Listes de courses »: one shopping list per store, shared by the espace's
logins (`ShoppingList`, open while `finished_at` is empty - at most one per
store), and its lines (`ShoppingListItem`: an article or a free text, a
quantity, a note, ticked once bought). inventory/shopping_lists.py fills
them; « Données » never exports them.

Two empty tables, so nothing existing changes meaning. A store deleted takes
its lists (CASCADE); an article deleted leaves its items as free texts
holding its name (SET_NULL). Reversing drops both tables, and every list
written with them.
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0021_shopping_forecast"),
        ("invoices", "0037_auto_gather"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShoppingList",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("created_by", models.CharField(blank=True, max_length=150)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("finished_by", models.CharField(blank=True, max_length=150)),
                (
                    "supplier",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shopping_lists",
                        to="invoices.supplier",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("finished_at__isnull", True)),
                        fields=("supplier",),
                        name="shopping_list_one_open_per_store",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ShoppingListItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("label", models.CharField(max_length=255)),
                ("product_name", models.CharField(blank=True, max_length=255)),
                ("pack_size", models.PositiveIntegerField(blank=True, null=True)),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=10)),
                (
                    "unit",
                    models.CharField(
                        blank=True, choices=[("L", "Litre"), ("UNIT", "Unité"), ("KG", "Kilogramme")], max_length=4
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=200)),
                ("added_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("added_by", models.CharField(blank=True, max_length=150)),
                ("checked_at", models.DateTimeField(blank=True, null=True)),
                ("checked_by", models.CharField(blank=True, max_length=150)),
                (
                    "shopping_list",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="items", to="inventory.shoppinglist"
                    ),
                ),
                (
                    "stock_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="shopping_list_items",
                        to="inventory.stocktype",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("quantity__gt", 0)), name="shopping_list_item_quantity_positive"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("label", ""), _negated=True), name="shopping_list_item_has_a_label"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("unit__in", ["", "L", "UNIT", "KG"])), name="shopping_list_item_unit_known"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("pack_size__isnull", True), ("pack_size__gt", 1), _connector="OR"),
                        name="shopping_list_item_pack_of_several",
                    ),
                    models.UniqueConstraint(
                        fields=("shopping_list", "stock_type"), name="shopping_list_item_article_once"
                    ),
                ],
            },
        ),
    ]
