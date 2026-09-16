"""The four shops whose photographed till receipts have dedicated parsers.

Seeded rather than typed in because the `parser_key` has to match the
registry key exactly (invoices/parsers/registry.py) - a typo there silently
falls back to "no parser", which imports the receipt with no lines at all
and looks like a parsing failure rather than a configuration one.

`is_scrapable` is False for all four: these arrive as photos taken in the
shop, not as emails or downloads, so there is nothing to go and fetch.
"""

from django.db import migrations

SHOPS = [
    {"code": "FRANPRIX", "name": "Franprix", "parser_key": "FRANPRIX", "is_scrapable": False},
    {"code": "MONOPRIX", "name": "Monoprix", "parser_key": "MONOPRIX", "is_scrapable": False},
    {"code": "SABBH", "name": "Sabbh Oriental", "parser_key": "SABBH", "is_scrapable": False},
    {"code": "WINGSENG", "name": "Wing Seng", "parser_key": "WINGSENG", "is_scrapable": False},
]


def seed_shops(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    for data in SHOPS:
        Supplier.objects.get_or_create(code=data["code"], defaults=data)


def remove_shops(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    # Only the ones that never got used - a supplier with invoices attached
    # is protected by Invoice.supplier's PROTECT anyway, and deleting real
    # purchase history on a migration rollback would be indefensible.
    Supplier.objects.filter(code__in=[shop["code"] for shop in SHOPS], invoices__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0011_invoice_ocr_confidence_invoice_ocr_text_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_shops, remove_shops),
    ]
