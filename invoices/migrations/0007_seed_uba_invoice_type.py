from django.db import migrations

# Reproduces the old hardcoded invoices/scrapers/uba_email.py search exactly:
# FROM "Notifications@uba.paris" SUBJECT "Facture" - IMAP's own FROM/SUBJECT
# search is a case-insensitive substring match, hence the (?i) flags below.
SENDER_PATTERN = r"(?i)Notifications@uba\.paris"
SUBJECT_PATTERN = r"(?i)Facture"


def seed_uba_invoice_type(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    InvoiceType = apps.get_model("invoices", "InvoiceType")
    EmailInvoiceSource = apps.get_model("invoices", "EmailInvoiceSource")

    uba = Supplier.objects.filter(code="UBA").first()
    if uba is None:
        return

    invoice_type, _ = InvoiceType.objects.get_or_create(
        supplier=uba,
        source_kind="EMAIL",
        defaults={"name": "UBA - Factures", "parser_key": "UBA", "is_active": True},
    )
    EmailInvoiceSource.objects.get_or_create(
        invoice_type=invoice_type,
        defaults={
            "sender_pattern": SENDER_PATTERN,
            "subject_pattern": SUBJECT_PATTERN,
            "body_pattern": "",
            "attachment_pattern": r"(?i)\.pdf$",
        },
    )


def remove_uba_invoice_type(apps, schema_editor):
    InvoiceType = apps.get_model("invoices", "InvoiceType")
    InvoiceType.objects.filter(supplier__code="UBA", source_kind="EMAIL").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0006_scrapejob_kind_scrapejob_test_matches_invoicetype_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_uba_invoice_type, remove_uba_invoice_type),
    ]
