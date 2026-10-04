"""Every employee already invited opens « Liste de courses » (the owner,
04/10/2026: every existing employee gets it; a new one has it ticked -
accounts.access.DEFAULT_AREAS). On the central accounts database: every
MEMBER of every espace. The owner's rows are left as they are (an owner opens
everything). Reversing changes nothing: a version without the area passes
the key over (accounts.access.Access.of)."""

from django.db import migrations

AREA = "shopping"
#: accounts.access.AREAS as this migration found them, in their order: the
#: pages are stored in the page's order (members._ordered), the new key at
#: its place.
AREAS_IN_0005 = [
    "invoices_add",
    "stock_takes",
    "returnables",
    "shopping",
    "invoices",
    "stock_gaps",
    "products",
    "recipes",
    "bank",
    "margins",
    "staff",
]


def _known(key) -> bool:
    """An area this migration knows - never a TypeError on a value that is
    no key (a list, a dict: unhashable)."""
    return isinstance(key, str) and key in AREAS_IN_0005


def give_every_employee_the_shopping_lists(apps, schema_editor):
    """Each MEMBER's pages gain AREA, in AREAS' order; a key no area has, or
    a value that is no key, stays where it was, after the known ones
    (Access.of passes them over). Pages that are no list opened nothing: they
    become [AREA]. One already holding AREA is left as it is - run twice, it
    changes nothing more."""
    Membership = apps.get_model("accounts", "Membership")
    alias = schema_editor.connection.alias
    for pk, pages in Membership.objects.using(alias).filter(role="member").values_list("pk", "pages"):
        listed = pages if isinstance(pages, list) else []
        if AREA in listed:
            continue
        known = {key for key in listed if _known(key)} | {AREA}
        rest = [key for key in listed if not _known(key)]
        Membership.objects.using(alias).filter(pk=pk).update(
            pages=[key for key in AREAS_IN_0005 if key in known] + rest
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_pushdevice"),
    ]

    operations = [
        migrations.RunPython(give_every_employee_the_shopping_lists, migrations.RunPython.noop),
    ]
