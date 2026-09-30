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
from dataclasses import dataclass, field

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


@dataclass
class Written:
    """What went into the zip, and what could not."""

    names: list[str] = field(default_factory=list)
    missing: list[Invoice] = field(default_factory=list)


def write_zip(invoices, handle) -> Written:
    """Every file of `invoices` into a zip written to `handle`, under its
    download name, one file in memory at a time."""
    written = Written()
    names = UniqueNames()
    with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        for invoice in invoices:
            if not invoice.source_file:
                written.missing.append(invoice)
                continue
            try:
                source = invoice.source_file.open("rb")
            except OSError:
                written.missing.append(invoice)
                continue
            with source:
                name = names.take(download_name(invoice))
                with archive.open(name, "w") as target:
                    shutil.copyfileobj(source, target)
            written.names.append(name)
        if written.missing:
            archive.writestr(MISSING_LIST, missing_text(written.missing))
    return written


def missing_text(invoices) -> str:
    """One line per document the zip could not hold: its supplier, number,
    date and total, and why - the accountant has to ask for it."""
    lines = ["Ces factures sont rattachées à une dépense de la période, mais leur fichier n'est pas enregistré :", ""]
    for invoice in invoices:
        number = f" n° {invoice.invoice_number}" if invoice.invoice_number else ""
        day = invoice.invoice_date.strftime("%d/%m/%Y") if invoice.invoice_date else "sans date"
        lines.append(f"- {invoice.supplier.name}{number}, {day}, {invoice.total_ttc:.2f} € TTC")
    return "\r\n".join(lines) + "\r\n"


def zip_name(label: str) -> str:
    """« Factures juin 2026.zip »."""
    return f"{clean(f'Factures {label}')}.zip"
