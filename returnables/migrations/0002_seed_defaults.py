"""The usual returnable types, and UBA's slip, seeded in every database - the
test one, the _template every new tenant is copied from, and each tenant.

The data is written here, not imported from the app: a migration must
replay the same way whatever the code has become since (the precedent is
invoices/0007). Everything is `get_or_create` by name, so a type or a
format somebody already made under that name is left as it is. UBA is
looked up by its code, as invoices/0002 seeds it; a database without it
gets the types only.

The patterns were checked against every real slip of the owner's (read-only,
29/09: every returnables part found, every line read and classified, every
quantity × price = amount, every total, the replacements and the re-sends
recognised). returnables/tests/test_models.py pins what is written.
Reversing does nothing: seeded rows may since carry counts and slips, and
the tables go with 0001 anyway.
"""

from django.db import migrations

#: (name, position, patterns) - shown exactly as written here.
TYPES = [
    ("Fûts", 1, r"F[ÛU]TS?\b"),
    ("Caisses verre", 2, r"CAISSE|CASIER|JUS|SODA|\bEAUX?\b|\d+\s*CL\b"),
    ("Bouteilles CO2", 3, r"CO2|\bGAZ\b"),
]

FORMAT_NAME = "UBA \N{EM DASH} bon du livreur"
SUPPLIER_CODE = "UBA"

#: Every pattern of UBA's slip (the driver's ticket, e-mailed after each
#: delivery). Multi-line values are several patterns, tried in order.
FORMAT = {
    "sender_pattern": r"mphone@uba\.paris",
    "subject_pattern": r"^\s*Livraison du\b",
    "attachment_pattern": r"(?i)\.pdf$",
    "section_start": r"^REPRISE VIDE\s*$",
    "section_end": r"^FACTURE\(S\)/BL DU JOUR",
    "line_pattern": (
        r"^(?P<designation>.+?)\s+(?P<quantite>-?\d+)\s+x\s+(?P<prix>-?\d+(?:[.,]\d+)?)"
        r"\s+=\s+(?P<montant>-?\d+(?:[.,]\d+)?)\s*$"
    ),
    "date_patterns": "\n".join(  # noqa: FLY002 - one pattern per line, as the field stores them
        [
            r"BL No:\s*\d+\s+du\s+(?P<date>\d{2}/\d{2}/\d{4})",
            r"^Le\s+(?P<date>\d{2}/\d{2}/\d{4})",
        ]
    ),
    "printed_patterns": r"^Le\s+(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<heure>\d{2}:\d{2}(?::\d{2})?)",
    "number_patterns": r"Ticket No\s*:\s*0*(?P<numero>\d+)",
    "reference_patterns": r"BL No:\s*(?P<reference>\d+)",
    "replaces_pattern": r"ANNULE ET REMPLACE",
    "total_patterns": r"Deconsigne\s*:\s*(?P<total>-?\d+(?:[.,]\d+)?)",
    "remarks_start": r"^ANOMALIES\s*$",
    "remarks_end": r"^Merci de Votre Commande",
}


def seed(apps, schema_editor):
    ReturnableType = apps.get_model("returnables", "ReturnableType")
    SlipFormat = apps.get_model("returnables", "SlipFormat")
    Supplier = apps.get_model("invoices", "Supplier")

    for name, position, slip_patterns in TYPES:
        ReturnableType.objects.get_or_create(
            name=name, defaults={"position": position, "is_active": True, "slip_patterns": slip_patterns}
        )

    uba = Supplier.objects.filter(code=SUPPLIER_CODE).first()
    if uba is None:
        return
    SlipFormat.objects.get_or_create(name=FORMAT_NAME, defaults={"supplier": uba, "is_active": True, **FORMAT})


class Migration(migrations.Migration):
    dependencies = [
        ("returnables", "0001_initial"),
        ("invoices", "0002_seed_suppliers"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
