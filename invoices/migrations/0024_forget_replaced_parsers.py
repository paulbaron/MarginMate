"""The wine growers' two parsers are gone: the one reader reads their
invoices, line for line, on every document filed (measured against what is
filed today: quantities, unit prices, rates, dates and numbers all the same).

A key pointing at a parser that no longer exists resolves to nothing
everywhere, so nothing breaks - but it would still be offered as a document
type's parser on screen, so it is forgotten here.
"""

from django.db import migrations

REPLACED = ("DEPOIVRE", "PLOUFILS")


def forget(apps, schema_editor):
    apps.get_model("invoices", "Supplier").objects.filter(parser_key__in=REPLACED).update(parser_key="")
    apps.get_model("invoices", "InvoiceType").objects.filter(parser_key__in=REPLACED).update(parser_key="")


class Migration(migrations.Migration):
    dependencies = [("invoices", "0023_supplier_expenses_only")]

    operations = [migrations.RunPython(forget, migrations.RunPython.noop)]
