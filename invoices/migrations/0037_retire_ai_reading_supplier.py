"""The AI reading is gone (04/10/2026): its pseudo-supplier « Autre (analyse
IA) », seeded by invoices/0002 with the code OTHER and the reader key LLM,
goes with it - in every espace and in the _template.

Deleted only where NOTHING names it: no row of any table with a key to it,
whatever that key does on a deletion - a CASCADE would take its rows with it
in silence (its learned payee names, its known prices, its history). Where
something does, it stays as an ordinary supplier: its reader key emptied, its
name and every row naming it as they are, so nothing is lost. No other
supplier is touched. Going back changes nothing: the old code runs without it.
"""

from django.db import migrations
from django.db.models import ForeignObjectRel

#: The pseudo-supplier invoices/0002 seeded (its literals, not today's code).
CODE = "OTHER"
AI_READER_KEY = "LLM"


def named_by_something(supplier) -> bool:
    """Whether any row of any table names `supplier`: every relation that
    points at Supplier - a key, a one-to-one, a many-to-many, hidden ones
    included (SupplierChange.other_supplier has no reverse name, and its
    SET_NULL would empty it) - read off the historical model, and any
    many-to-many Supplier holds itself."""
    meta = type(supplier)._meta
    for relation in meta.get_fields(include_hidden=True):
        if not isinstance(relation, ForeignObjectRel):
            continue
        if relation.related_model._base_manager.filter(**{relation.field.name: supplier}).exists():
            return True
    return any(getattr(supplier, field.name).exists() for field in meta.many_to_many)


def retire_ai_supplier(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    for supplier in Supplier._base_manager.filter(code=CODE, parser_key=AI_READER_KEY):
        if named_by_something(supplier):
            supplier.parser_key = ""
            supplier.save(update_fields=["parser_key"])
        else:
            supplier.delete()


class Migration(migrations.Migration):
    # The latest migration, on 04/10/2026, of every app whose models point
    # at Supplier, so the historical model knows every relation it is walked
    # for. Never add one once this has shipped: a dependency added to a
    # migration an espace has applied stops `migrate` there
    # (InconsistentMigrationHistory).
    dependencies = [
        ("invoices", "0036_receiptbatch_sent_by"),
        ("bank", "0009_statement_format_file_type"),
        ("inventory", "0020_gap_fill_setting"),
        ("returnables", "0002_seed_defaults"),
    ]

    operations = [
        migrations.RunPython(retire_ai_supplier, migrations.RunPython.noop),
    ]
