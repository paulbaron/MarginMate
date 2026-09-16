from django.db import migrations


def reset_total_quantity(apps, schema_editor):
    """PosProduct.total_quantity used to be a running counter that
    sync_pos_products bumped by the FULL window total on every import, with
    no memory of which dates an earlier import already covered - so any
    product touched by more than one overlapping import (routine now, via
    pos_products_backfill) is inflated by however many times that happened,
    and there is no way to recover the true figure from the corrupted number
    alone. Zeroing it here is safe: the next import (of any range covering
    this product) rebuilds it correctly from PosProductDailyQuantity, which
    is idempotent per day - see that model's docstring.
    """
    PosProduct = apps.get_model("recipes", "PosProduct")
    PosProduct.objects.exclude(total_quantity=0).update(total_quantity=0)


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0011_posproductdailyquantity"),
    ]

    operations = [
        migrations.RunPython(reset_total_quantity, migrations.RunPython.noop),
    ]
