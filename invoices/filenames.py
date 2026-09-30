"""The name a document's file is downloaded under, wherever it is taken
from: « Darty 11€55 01_10_2026.pdf » - the supplier, the total TTC with the
euro sign for a decimal point, and the date with underscores (the owner,
01/10/2026). The stored file keeps its own name (`invoices/2026/10/…`); only
what the browser is told to save changes.

One definition for every door out: the document's own file route
(`invoices:invoice_file`, the PDF frame, « Voir le PDF », « Télécharger »)
and the zip of Banque (`bank:invoice_files`).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import PurePosixPath

#: What Windows refuses in a file name, and the control characters.
FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
CENT = Decimal("0.01")
#: Said in place of the date of a document nobody dated.
NO_DATE = "sans date"
#: A supplier with no name left once cleaned.
NO_SUPPLIER = "Fournisseur"


def clean(text: str) -> str:
    """`text` fit for a file name on any system: forbidden characters as
    spaces, runs of spaces as one, no dot or space at either end (Windows
    drops a trailing one, and a leading dot hides the file elsewhere)."""
    text = FORBIDDEN.sub(" ", text or "")
    return " ".join(text.split()).strip(" .")


def amount_words(total: Decimal) -> str:
    """11,55 € as « 11€55 »: rounded to the cent, a minus kept for a credit
    note."""
    total = Decimal(total).quantize(CENT, rounding=ROUND_HALF_UP)
    sign = "-" if total < 0 else ""
    cents = int(abs(total) * 100)
    return f"{sign}{cents // 100}€{cents % 100:02d}"


def download_name(invoice) -> str:
    """« Darty 11€55 01_10_2026.pdf »: the file's own extension, lower case -
    a ticket's photo stays a « .jpg »."""
    supplier = clean(invoice.supplier.name) or NO_SUPPLIER
    day = invoice.invoice_date.strftime("%d_%m_%Y") if invoice.invoice_date else NO_DATE
    extension = PurePosixPath(invoice.source_file.name).suffix.lower() if invoice.source_file else ""
    return f"{supplier} {amount_words(invoice.total_ttc)} {day}{extension or '.pdf'}"


class UniqueNames:
    """Hands each name out once: a second « Metro 115€26 25_08_2026.pdf »
    comes back as « Metro 115€26 25_08_2026 (2).pdf » - two deliveries of
    one amount on one day are two files, and a zip holding one name twice
    unpacks one of them. Compared without case, as Windows compares them."""

    def __init__(self):
        self.taken: set[str] = set()

    def take(self, name: str) -> str:
        path = PurePosixPath(name)
        candidate, number = name, 1
        while candidate.lower() in self.taken:
            number += 1
            candidate = f"{path.stem} ({number}){path.suffix}"
        self.taken.add(candidate.lower())
        return candidate
