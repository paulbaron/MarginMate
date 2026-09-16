"""Record the file hash of receipts imported before hashes existed.

`receipts.import_receipt` skips a file whose SHA-256 is already on an
invoice, before any OCR runs - that is what makes scanning the same folder
again cheap. Receipts imported earlier have no hash yet, so without this the
first re-scan of their folder would recognise every one of them again, only
to refuse each as a duplicate by ticket number at the very end.

Receipts only: a digital invoice is never re-scanned from a folder. A file
that has gone missing from media/ is left without a hash - there is nothing
to match it against.
"""

import hashlib

from django.db import migrations


def backfill(apps, schema_editor):
    Invoice = apps.get_model("invoices", "Invoice")
    receipts = Invoice.objects.filter(source_sha256="").exclude(source_file="").exclude(source_file=None)
    for invoice in receipts.iterator():
        if not invoice.parse_checks and not invoice.ocr_text:
            continue
        digest = hashlib.sha256()
        try:
            with invoice.source_file.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
        except (OSError, ValueError):
            continue
        invoice.source_sha256 = digest.hexdigest()
        invoice.save(update_fields=["source_sha256"])


class Migration(migrations.Migration):
    dependencies = [("invoices", "0013_folder_scan")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
