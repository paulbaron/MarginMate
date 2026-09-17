"""Read the tickets still waiting to be checked again, with today's parsers.

    python manage.py reread_receipts --dry-run
    python manage.py reread_receipts

A parser fix reaches tickets imported before it without their photos: each
one is parsed again from its stored reading, and the new lines are kept only
where they add up to the printed total and the old ones did not (see
receipts.reread_receipt). Checked tickets are never touched.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from invoices.receipts import pending_receipts, reread_receipt


class Command(BaseCommand):
    help = "Relit les tickets à vérifier depuis leur texte enregistré, quand la nouvelle lecture tombe juste."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        reread = []
        with transaction.atomic():
            for invoice in pending_receipts().select_related("supplier").order_by("pk"):
                if reread_receipt(invoice):
                    reread.append(invoice)
            if dry_run:
                transaction.set_rollback(True)
        verb = "seraient relus" if dry_run else "relus"
        self.stdout.write(f"{len(reread)} ticket(s) {verb} : " + ", ".join(str(invoice.pk) for invoice in reread))
