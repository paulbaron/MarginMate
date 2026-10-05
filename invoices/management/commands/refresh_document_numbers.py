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

A till's count of the day filed bare - Wing Seng's « 000172 », from before
its padding zeros stopped counting (generic_receipt.DAILY_COUNT_DIGITS) -
takes its date the same way (« 000172-20251119 »): bare, a second photo of
that ticket would no longer be recognised, since it now reads dated. Only
when the text prints that very number after « Ticket »: a short number read
any other way is the document's own. So does Franprix's store, till and
count of the day (« R1 007418-02 317 », « 007418-02-317-20250323 »). And a
number a dot cut short was filed as the document's date (« 20250314 » for
« 20250314.38604 »): it takes the whole number.
"""

import re
from collections import Counter
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from invoices.models import Invoice
from invoices.parsers.generic_receipt import DAILY_COUNT_DIGITS, _ticket_number
from invoices.receipts import has_own_reader

MADE_UP_RE = re.compile(r"\d{8}-\d+\.\d{2}")
DIGIT_RUN_RE = re.compile(r"\d{18,26}")
# generic_receipt.STORE_TILL_RE's number, filed bare.
STORE_TILL_COUNT_RE = re.compile(r"\d{5,6}-\d{2}-\d{2,4}")


def stands_in(number: str) -> bool:
    """A number the reader made up, or a long digit run it fell back on."""
    return not number or bool(MADE_UP_RE.fullmatch(number) or DIGIT_RUN_RE.fullmatch(number))


def bare_count(number: str) -> bool:
    """A number short enough to be a till's count of the day, undated."""
    return (number.isdigit() and len(number.lstrip("0")) <= DAILY_COUNT_DIGITS) or bool(
        STORE_TILL_COUNT_RE.fullmatch(number)
    )


def is_day(number: str, day: date | None) -> bool:
    """The document's own date, all its number was read as."""
    return day is not None and number == f"{day:%Y%m%d}"


def completes(number: str, printed: str, day: date | None) -> bool:
    """`printed` is `number` made whole: a count of the day with its date
    (« 000172-20251119 »), or a date with the rest of the number the dot cut
    off (« 20250314.38604 »)."""
    if day is None:
        return False
    return printed == f"{number}-{day:%Y%m%d}" or (is_day(number, day) and printed.startswith(f"{number}."))


class Command(BaseCommand):
    help = (
        "Remplace les numéros de documents inventés (date-total, référence de paiement) par le numéro imprimé, "
        "et date les numéros de ticket du jour."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        renumbered: Counter = Counter()
        taken: list[str] = []
        with transaction.atomic():
            documents = Invoice.objects.select_related("supplier").order_by("pk")
            for invoice in documents:
                text = invoice.document_text
                number = invoice.invoice_number
                day = invoice.invoice_date
                if (
                    not text
                    or not (stands_in(number) or bare_count(number) or is_day(number, day))
                    or has_own_reader(invoice.supplier)
                ):
                    continue
                printed = _ticket_number(text, day)
                if not printed or stands_in(printed) or printed == number:
                    continue
                if not stands_in(number) and not completes(number, printed, day):
                    continue
                if (
                    Invoice.objects.filter(supplier=invoice.supplier, invoice_number=printed)
                    .exclude(pk=invoice.pk)
                    .exists()
                ):
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
