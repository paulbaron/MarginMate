"""The AI reading is gone (04/10/2026): its pseudo-supplier « Autre (analyse
IA) », seeded by invoices/0002 with the code OTHER and the reader key LLM,
goes with it - in every espace and in the _template.

Deleted only where NOTHING names it: no row of any table with a key to it,
whatever that key does on a deletion - a CASCADE would take its rows with it
in silence (its learned payee names, its known prices, its history) - nor a
reminder « repris par » it (notifications.Reminder.skip_supplier_id, a plain
id: deleted under it, the reminder's condition would never match again and
it would be sent every time). Where
something does, it stays as an ordinary supplier: its reader key emptied, its
name and every row naming it as they are, so nothing is lost. No other
supplier is touched. Going back changes nothing: the old code runs without it.
"""

from django.db import migrations
from django.db.models import ForeignObjectRel

#: The pseudo-supplier invoices/0002 seeded (its literals, not today's code).
CODE = "OTHER"
AI_READER_KEY = "LLM"


#: The plain ids naming a supplier on 04/10/2026, no key behind them:
#: (app, model, field).
PLAIN_IDS = (("notifications", "Reminder", "skip_supplier_id"),)


def named_by_something(apps, supplier) -> bool:
    """Whether any row of any table names `supplier`: every relation that
    points at Supplier - a key, a one-to-one, a many-to-many, hidden ones
    included (SupplierChange.other_supplier has no reverse name, and its
    SET_NULL would empty it) - read off the historical model, any
    many-to-many Supplier holds itself, and the plain ids of PLAIN_IDS."""
    meta = type(supplier)._meta
    for relation in meta.get_fields(include_hidden=True):
        if not isinstance(relation, ForeignObjectRel):
            continue
        if relation.related_model._base_manager.filter(**{relation.field.name: supplier}).exists():
            return True
    if any(getattr(supplier, field.name).exists() for field in meta.many_to_many):
        return True
    return any(
        apps.get_model(app, model)._base_manager.filter(**{field: supplier.pk}).exists()
        for app, model, field in PLAIN_IDS
    )


def retire_ai_supplier(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    for supplier in Supplier._base_manager.filter(code=CODE, parser_key=AI_READER_KEY):
        if named_by_something(apps, supplier):
            supplier.parser_key = ""
            supplier.save(update_fields=["parser_key"])
        else:
            supplier.delete()


class Migration(migrations.Migration):
    # The latest migration, on 04/10/2026, of every app whose models point
    # at Supplier, so the historical model knows every relation it is walked
    # for (GitHub's main brought inventory 0021's ShoppingExclusion, then
    # inventory 0022's ShoppingList), and of every app holding a plain id of
    # one (PLAIN_IDS: GitHub's main's notifications 0001, whose reminders
    # hold no key). Never add one once this has shipped: a dependency added
    # to a migration an espace has applied stops `migrate` there
    # (InconsistentMigrationHistory).
    dependencies = [
        ("invoices", "0037_auto_gather"),
        ("bank", "0009_statement_format_file_type"),
        ("inventory", "0022_shopping_lists"),
        ("returnables", "0002_seed_defaults"),
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(retire_ai_supplier, migrations.RunPython.noop),
    ]
