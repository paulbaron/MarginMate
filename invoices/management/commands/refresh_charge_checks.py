"""Give charge documents their own checks back.

    python manage.py refresh_charge_checks --dry-run
    python manage.py refresh_charge_checks

A charge fetched by a portal or the mailbox went through the ticket reader,
then was filed by the charge reading - and the ticket reader's checks, about
lines that reading replaced, were stored over the charge's own
(importing.charge_state). Eau de Paris' water bills, filed at exactly what
they charge, waited in « À vérifier » under « 5.5 % supposé » and « lignes
310,15 € HT / ticket 303,28 € ». This puts the charge's checks and state
back on every such document nobody validated by hand; its lines and totals
are not touched.
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from invoices.importing import charge_state
from invoices.models import Invoice
from invoices.receipts import DATE_CHECK

CHARGE_CHECKS = {"Total de la charge", DATE_CHECK}


class Command(BaseCommand):
    help = "Remet aux documents de charges leurs propres contrôles (à la place de ceux de la lecture en ticket)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        refreshed: Counter = Counter()
        settled: Counter = Counter()
        with transaction.atomic():
            documents = Invoice.objects.filter(supplier__expenses_only=True, reviewed_at__isnull=True).select_related(
                "supplier"
            )
            for invoice in documents:
                if not {check.get("label") for check in invoice.parse_checks or ()} - CHARGE_CHECKS:
                    continue
                was_waiting = invoice.status == Invoice.Status.NEEDS_REVIEW
                if charge_state(invoice, invoice.printed_total_ttc):
                    refreshed[invoice.supplier.name] += 1
                    if was_waiting and invoice.status != Invoice.Status.NEEDS_REVIEW:
                        settled[invoice.supplier.name] += 1
            if dry_run:
                transaction.set_rollback(True)
        if dry_run:
            self.stdout.write("(essai : rien n'est enregistré)")
        if not refreshed:
            self.stdout.write("Aucun document de charges à reprendre.")
        for name, count in sorted(refreshed.items()):
            self.stdout.write(
                f"{name} : {count} document(s) repris"
                + (f", dont {settled[name]} qui ne sont plus à vérifier" if settled[name] else "")
                + "."
            )
