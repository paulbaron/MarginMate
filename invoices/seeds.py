"""What a new espace keeps of the suppliers the seed migrations install.

The seed migrations write the same suppliers into every database - the test
one, the `_template` every new espace is copied from, and each espace:
Metro and UBA (invoices/0002, which also seeded the AI reading's « Autre
(analyse IA) », removed by 0038 where nothing named it), UBA's mailbox source
« UBA - Factures » (0007), the four shops whose tills are configured in the
code (0012: Franprix, Monoprix, Sabbh Oriental, Wing Seng) and UBA's slip
format « UBA — bon du livreur » (returnables/0002).

Three of them are the bar the app was first written for: UBA, a Paris
wholesaler (with its mailbox source and its slip format), and two local
shops, Sabbh Oriental and Wing Seng. A new espace that is not the owner's
starts without them (`forget_original_bar_suppliers`, one of
accounts.provisioning.HOSTED_ESPACE_STEPS); Metro (not fetching),
Franprix, Monoprix and the returnable types stay. Their readers and
tills stay in the code: a till answers only where its supplier exists
(receipts.configured_tills), a shop created from a ticket never takes one of
those codes (receipts.create_shop), and a supplier brought back under one -
a « Données » archive of the original bar - is read as before.

The template, the seed migrations and « Données »'s `SEEDED_SUPPLIERS` /
`SEEDED_SOURCE` are left as they are (but for OTHER, which 0038 takes out
of every database): a new espace holds a subset of the seeds, so it is
still a new database there. The owner's espace was adopted,
never provisioned: nothing here reaches it.
"""

from __future__ import annotations

from django.db import transaction

#: The codes of the original bar's own suppliers (invoices/0002 and 0012).
ORIGINAL_BAR_SUPPLIERS = ("UBA", "SABBH", "WINGSENG")


def forget_original_bar_suppliers() -> None:
    """Delete, in the bound espace and in one transaction, the original
    bar's suppliers with their slip formats and invoice sources - PROTECTed,
    so they go first (a source takes its mailbox search with it). Raises
    ProtectedError, deleting nothing, when one is in use: never on a fresh
    copy of the template, and prepare_tenant then removes the espace."""
    from returnables.models import SlipFormat

    from .models import InvoiceType, Supplier

    with transaction.atomic():
        SlipFormat.objects.filter(supplier__code__in=ORIGINAL_BAR_SUPPLIERS).delete()
        InvoiceType.objects.filter(supplier__code__in=ORIGINAL_BAR_SUPPLIERS).delete()
        Supplier.objects.filter(code__in=ORIGINAL_BAR_SUPPLIERS).delete()
