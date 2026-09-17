"""Teach each shop what its checked tickets print that names it.

    python manage.py learn_shop_identifiers --dry-run
    python manage.py learn_shop_identifiers

A ticket checked on the review page teaches its shop its SIREN, phone and web
site (receipts.learn_identifiers); tickets checked before that did not. This
goes through them once, shop by shop, so that a torn header on the next one no
longer leaves it without a shop. Only what enough of a shop's tickets print,
and no other supplier's documents do, is learned.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from invoices.identifiers import describe
from invoices.models import Invoice, Supplier
from invoices.receipts import learn_identifiers


class Command(BaseCommand):
    help = "Apprend à chaque enseigne les identifiants (SIREN, téléphone, site) imprimés sur ses tickets vérifiés."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        checked = defaultdict(list)
        for ticket in Invoice.objects.exclude(reviewed_at=None).exclude(ocr_text="").order_by("pk"):
            checked[ticket.supplier_id].append(ticket.ocr_text)
        learned: dict[str, list[str]] = {}
        with transaction.atomic():
            for supplier in Supplier.objects.filter(pk__in=checked).order_by("name"):
                new = learn_identifiers(supplier, *checked[supplier.pk])
                if new:
                    learned[supplier.name] = new
            if dry_run:
                transaction.set_rollback(True)
        verb = "apprendrait" if dry_run else "a appris"
        if not learned:
            self.stdout.write("Rien de nouveau à apprendre.")
        for name, identifiers in sorted(learned.items()):
            self.stdout.write(f"{name} {verb} : " + ", ".join(describe(identifier) for identifier in identifiers))
