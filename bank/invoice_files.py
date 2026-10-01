"""« Télécharger les factures »: every document a spending of the period
paid, in one zip - for the accountant, a month at a time (the owner,
01/10/2026).

The period is Banque's own (its month, or its free dates, on the date the
bank booked the operation), so the zip holds exactly the invoices the
operations on screen are linked to. Debits only: an income line never
settles an invoice (views.bank_line_action). Each file is named the way it
is named everywhere else (invoices/filenames.py): « Darty 11€55 01_10_2026.pdf ».

A document with no file - typed by hand, or its file gone from the disk -
cannot go in; it is listed by name in « Factures sans fichier.txt » inside
the zip rather than dropped in silence, and the page says how many there are.
"""

from __future__ import annotations

import shutil
import zipfile

from accounts.views import open_stored
from invoices.filenames import UniqueNames, clean, download_name
from invoices.models import Invoice

#: The list, inside the zip, of the documents it could not hold.
MISSING_LIST = "Factures sans fichier.txt"


def paid_by(lines) -> list[Invoice]:
    """The documents the DEBITS among `lines` (a BankTransaction queryset)
    paid, each once however many lines paid it, oldest first. Two queries:
    the invoices with their supplier, then their lines (`total_ttc`, which
    the name carries, is added up from them)."""
    return list(
        Invoice.objects.filter(payments__transaction__in=lines.filter(amount__lt=0))
        .distinct()
        .select_related("supplier")
        .prefetch_related("lines")
        .order_by("invoice_date", "pk")
    )


def write_zip(invoices, handle) -> list[Invoice]:
    """Every file of `invoices` into a zip written to `handle`, under its
    download name, one file at a time; returns the documents it could not
    hold (no file, or the file gone from the disk)."""
    missing = []
    names = UniqueNames()
    with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        for invoice in invoices:
            source = open_stored(invoice.source_file.name) if invoice.source_file else None
            if source is None:
                missing.append(invoice)
                continue
            with source, archive.open(names.take(download_name(invoice)), "w") as target:
                shutil.copyfileobj(source, target)
        if missing:
            archive.writestr(MISSING_LIST, missing_text(missing))
    return missing


def missing_text(invoices) -> str:
    """One line per document the zip could not hold: its supplier, number,
    date and total, and why - the accountant has to ask for it."""
    lines = ["Factures rattachées à une dépense de la période, sans fichier enregistré :", ""]
    for invoice in invoices:
        number = f" n° {invoice.invoice_number}" if invoice.invoice_number else ""
        day = invoice.invoice_date.strftime("%d/%m/%Y") if invoice.invoice_date else "sans date"
        lines.append(f"- {invoice.supplier.name}{number}, {day}, {invoice.total_ttc:.2f} € TTC")
    return "\r\n".join(lines) + "\r\n"


def zip_name(label: str) -> str:
    """« Factures juin 2026.zip »."""
    return f"{clean(f'Factures {label}')}.zip"
