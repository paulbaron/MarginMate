"""Give receipts imported before lines kept their printed amounts those amounts
back, and the ticket's printed total with them.

    python manage.py restore_printed_ttc --dry-run
    python manage.py restore_printed_ttc

Each receipt's stored reading (`Invoice.ocr_text`) is parsed again by its
shop's own reader. Run once on 2026-09-16; kept as a command, not in the
request path, because it trusts today's parsers to read old text the way the
import did - worth a --dry-run first after any parser change.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from invoices.models import Invoice
from invoices.parsers import ticket_parser_for
from invoices.parsers.base import PdfPage


def restore_printed_ttc(invoice: Invoice) -> int:
    """Restore one receipt; returns how many lines got their printed amount.

    A line takes the amount of a reading with the same count, rate and HT -
    not the same name, which a price list or a person may have changed. A
    line whose HT no longer matches any reading (corrected since) is left to
    be worked out from HT, and so is every line of a ticket that no longer
    parses. The printed total is only filled in where none was stored.
    """
    parser = ticket_parser_for(invoice.supplier.code)
    if parser is None or not invoice.ocr_text:
        return 0
    try:
        parsed = parser.parse_pages([PdfPage(text=invoice.ocr_text)])
    except Exception:  # noqa: BLE001 - an unreadable old reading restores nothing
        return 0

    if invoice.printed_total_ttc is None and parsed.printed_total_ttc is not None:
        invoice.printed_total_ttc = parsed.printed_total_ttc
        invoice.save(update_fields=["printed_total_ttc"])

    readings = [line for line in parsed.lines if line.printed_ttc is not None]
    restored = 0
    for line in invoice.lines.filter(printed_ttc__isnull=True):
        reading = next(
            (
                reading
                for reading in readings
                if (reading.quantity, reading.vat_rate, reading.total_ht)
                == (line.quantity, line.vat_rate, line.total_ht)
            ),
            None,
        )
        if reading is None:
            continue
        readings.remove(reading)
        line.printed_ttc = reading.printed_ttc
        line.save(update_fields=["printed_ttc"])
        restored += 1
    return restored


class Command(BaseCommand):
    help = "Rend aux tickets importés avant leur montant TTC imprimé ligne par ligne, et leur total imprimé."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Compter sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        receipts = Invoice.objects.exclude(ocr_text="").select_related("supplier")
        lines = changed_totals = 0
        with transaction.atomic():
            for invoice in receipts:
                before = invoice.total_ttc
                lines += restore_printed_ttc(invoice)
                if Invoice.objects.get(pk=invoice.pk).total_ttc != before:
                    changed_totals += 1
            if dry_run:
                transaction.set_rollback(True)
        verb = "seraient" if dry_run else "ont été"
        self.stdout.write(
            f"{receipts.count()} tickets : {lines} ligne(s) {verb} complétée(s), "
            f"{changed_totals} total(aux) {verb} modifié(s)."
        )
