"""Read again, from its own XML, every electronic invoice filed at a total it
does not state.

    python manage.py tenant <espace> reread_einvoices --dry-run
    python manage.py tenant <espace> reread_einvoices

An electronic invoice states its total (BT-112), and that is what it is
filed at. Two bugs filed some at another (01/10/2026): a supplier ticked
« charges », or a document moved to such a supplier, was read again by the
ticket reader from the summary the XML reader writes - OVH's FR80644402,
22,62 €, at 20,00 € (« 20.00 % » read as its total), Total Energie's
114005409336, 228,07 €, at 0,05 €; and a credit note stating its amounts
negative was signed twice - OVH's AFR1176742, -3,92 €, at +3,27 €.

A document is read again (receipts.reread_document, « Relire le document »)
only when its stored total is not the one its XML states: a person who
corrected its lines left that total alone, and is not undone here. One whose
file or XML is gone is said and left.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from common import group_thousands
from invoices import einvoice
from invoices.importing import InvoiceLinesInUseError, LineTooWideError
from invoices.models import Invoice
from invoices.receipts import RereadError, reread_document

CENT = Decimal("0.01")


def stated_total(invoice: Invoice) -> Decimal | None:
    """The total the invoice's XML states, or None when it cannot be read."""
    try:
        data = einvoice.document_xml(invoice.source_file.path) if invoice.source_file else None
        return einvoice.read(data).printed_total_ttc if data else None
    except (OSError, ValueError):  # EInvoiceError is a ValueError
        return None


class Command(BaseCommand):
    help = "Relit depuis leur XML les factures électroniques enregistrées à un autre total que le leur."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        reread = 0
        with transaction.atomic():
            for invoice in Invoice.objects.exclude(einvoice_format="").select_related("supplier").order_by("pk"):
                stated = stated_total(invoice)
                described = f"{invoice.supplier.name} n° {invoice.invoice_number or invoice.pk}"
                if stated is None:
                    self.stdout.write(f"Laissé : {described} - son XML ne se lit plus.")
                    continue
                stored = invoice.printed_total_ttc
                if stored is not None and abs(stored - stated) <= CENT:
                    continue
                try:
                    reread_document(invoice)
                except (RereadError, InvoiceLinesInUseError, LineTooWideError) as exc:
                    self.stdout.write(f"Laissé : {described} - {exc}")
                    continue
                self.stdout.write(f"{described} : {group_thousands(stored)} € -> {group_thousands(stated)} €")
                reread += 1
            if dry_run:
                transaction.set_rollback(True)
        if dry_run:
            self.stdout.write("(essai : rien n'est enregistré)")
        if not reread:
            self.stdout.write("Aucune facture électronique à relire.")
