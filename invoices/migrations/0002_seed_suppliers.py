from django.db import migrations

SUPPLIERS = [
    {"code": "METRO", "name": "Metro", "parser_key": "METRO", "is_scrapable": True},
    {"code": "UBA", "name": "UBA (Le Dipsomaniac)", "parser_key": "UBA", "is_scrapable": True},
    {"code": "OTHER", "name": "Autre (analyse IA)", "parser_key": "LLM", "is_scrapable": False},
]


def seed_suppliers(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    for data in SUPPLIERS:
        Supplier.objects.get_or_create(code=data["code"], defaults=data)


def remove_suppliers(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    Supplier.objects.filter(code__in=[s["code"] for s in SUPPLIERS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_suppliers, remove_suppliers),
    ]
