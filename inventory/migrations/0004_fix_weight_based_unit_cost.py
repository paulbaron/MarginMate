from django.db import migrations


def fix_unit_cost(apps, schema_editor):
    """Movements created from a line with a measured total_volume (a
    variable-weight product) were storing unit_cost_ht = invoice_line's price
    per item count instead of price per kg/L - the two only match by
    coincidence. Recompute them as total_ht / total_volume, matching the
    corrected logic in inventory/services.py.
    """
    StockMovement = apps.get_model("inventory", "StockMovement")
    for movement in StockMovement.objects.select_related("invoice_line", "stock_type").filter(
        invoice_line__isnull=False, invoice_line__total_volume__gt=0
    ).exclude(stock_type__unit="UNIT"):
        line = movement.invoice_line
        correct_unit_cost = line.total_ht / line.total_volume
        if movement.unit_cost_ht != correct_unit_cost:
            movement.unit_cost_ht = correct_unit_cost
            movement.save(update_fields=["unit_cost_ht"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0003_product_stock_equivalent_product_unit_label"),
    ]

    operations = [
        migrations.RunPython(fix_unit_cost, migrations.RunPython.noop),
    ]
