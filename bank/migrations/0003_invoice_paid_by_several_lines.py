"""An invoice may be paid by several bank lines.

`InvoicePayment.invoice` was a OneToOneField, which is what refused a second
line on an invoice settled in two goes. It becomes a ForeignKey; what cannot
repeat is the PAIR, and the constraint says so.

Nothing is lost: every row a one-to-one allowed is a pair that appears once,
so the new constraint holds over the existing table without touching a row.

**Going back is not safe once an invoice has been paid by two lines**, and
it fails half way. `migrate bank 0002` un-applies 0004 first - dropping the
`category` columns and every category typed into them - and only then tries
to put the OneToOne back, where the second payment raises « UNIQUE
constraint failed: new__bank_invoicepayment.invoice_id ». The schema is then
at « 0003 applied, 0004 gone » with the categories lost for a rollback that
never completed. Take the second link off every such invoice first (« Délier »
on the bank page), or restore the backup taken before applying these.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bank", "0002_ignore_rules"),
        ("invoices", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoicepayment",
            name="invoice",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="invoices.invoice"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicepayment",
            constraint=models.UniqueConstraint(
                fields=("transaction", "invoice"), name="unique_transaction_invoice_payment"
            ),
        ),
    ]
