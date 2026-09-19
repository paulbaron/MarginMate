"""Put the number a document prints in place of the one that stood in for it.

    python manage.py refresh_document_numbers --dry-run
    python manage.py refresh_document_numbers

Where the reader found no number, a document was filed under a stand-in:
its date and total ("20260410-327.20", receipt_base.compose_invoice_number),
or a payment reference - a long digit run in the bill's detail. Eau de
Paris' bills print « N° 2026100000001 DU 10 AVRIL 2026 » lines below the
word « facture », which the reader now reads (DOCUMENT_NUMBER_RES); filed
under stand-ins, its portal's list - printing the real numbers - never
recognised an invoice already imported. This reads each such document's
number again from its stored text. A number that is one is never touched,
nor one another document of the supplier already holds (said), nor a
document read by a supplier's own reader.
"""

import re
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from invoices.models import Invoice
from invoices.parsers import LLM_PARSER_KEY
from invoices.parsers.generic_receipt import _ticket_number
from invoices.receipts import has_own_reader

MADE_UP_RE = re.compile(r"\d{8}-\d+\.\d{2}")
DIGIT_RUN_RE = re.compile(r"\d{18,26}")


def stands_in(number: str) -> bool:
    """A number the reader made up, or a long digit run it fell back on."""
    return not number or bool(MADE_UP_RE.fullmatch(number) or DIGIT_RUN_RE.fullmatch(number))


class Command(BaseCommand):
    help = "Remplace les numéros de documents inventés (date-total, référence de paiement) par le numéro imprimé."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        renumbered: Counter = Counter()
        taken: list[str] = []
        with transaction.atomic():
            documents = (
                Invoice.objects.exclude(supplier__parser_key=LLM_PARSER_KEY)
                .select_related("supplier")
                .order_by("pk")
            )
            for invoice in documents:
                text = invoice.document_text
                if not text or not stands_in(invoice.invoice_number) or has_own_reader(invoice.supplier):
                    continue
                printed = _ticket_number(text, invoice.invoice_date)
                if not printed or stands_in(printed) or printed == invoice.invoice_number:
                    continue
                if Invoice.objects.filter(supplier=invoice.supplier, invoice_number=printed).exclude(pk=invoice.pk).exists():
                    taken.append(f"{invoice.supplier.name} n° {printed} (document {invoice.pk})")
                    continue
                invoice.invoice_number = printed
                invoice.save(update_fields=["invoice_number"])
                renumbered[invoice.supplier.name] += 1
            if dry_run:
                transaction.set_rollback(True)
        if dry_run:
            self.stdout.write("(essai : rien n'est enregistré)")
        if not renumbered:
            self.stdout.write("Aucun numéro à reprendre.")
        for name, count in sorted(renumbered.items()):
            self.stdout.write(f"{name} : {count} document(s) prennent le numéro qu'ils impriment.")
        for what in taken:
            self.stdout.write(f"Laissé : {what} - ce numéro est déjà celui d'un autre document.")
