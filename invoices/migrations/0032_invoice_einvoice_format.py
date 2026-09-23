"""Electronic invoicing: what a document was read from, and what an e-mail
source takes as the invoice.

`Invoice.einvoice_format` is additive and blank for every document already
filed - none of the PDFs in the folder carries an embedded XML (measured
read-only, 22/09), so there is nothing to backfill and nothing to guess at.
It is a stored field rather than a look at `parse_checks` because the lists
that must leave an electronic invoice out of the ticket queue - and put one
whose totals disagree in « Documents à corriger » - are database queries
(invoices/workspace.py).

`EmailInvoiceSource.attachment_pattern` widens from `\\.pdf$` to
`\\.(pdf|xml)$`: since 1 September 2026 a supplier may attach the XML on its
own, and it is the legal invoice as much as a PDF is. **Only the exact
default is rewritten** - a pattern someone tuned by hand says something this
migration does not know, and is left exactly as it is.
"""

from django.db import migrations, models

OLD_DEFAULT = r"(?i)\.pdf$"
NEW_DEFAULT = r"(?i)\.(pdf|xml)$"


def widen(apps, schema_editor):
    apps.get_model("invoices", "EmailInvoiceSource").objects.filter(
        attachment_pattern=OLD_DEFAULT
    ).update(attachment_pattern=NEW_DEFAULT)


def narrow(apps, schema_editor):
    apps.get_model("invoices", "EmailInvoiceSource").objects.filter(
        attachment_pattern=NEW_DEFAULT
    ).update(attachment_pattern=OLD_DEFAULT)


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0031_invoice_vat_table_typed"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="einvoice_format",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AlterField(
            model_name="emailinvoicesource",
            name="attachment_pattern",
            field=models.CharField(
                blank=True,
                default=NEW_DEFAULT,
                help_text=(
                    "Expression régulière testée sur le nom de la pièce jointe "
                    "(PDF ou XML : une facture électronique peut arriver seule)."
                ),
                max_length=200,
            ),
        ),
        migrations.RunPython(widen, narrow),
    ]
