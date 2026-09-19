"""Teach each shop what its documents print that names it.

    python manage.py learn_shop_identifiers --dry-run
    python manage.py learn_shop_identifiers

A ticket checked on the review page teaches its shop its SIREN, phone and web
site (receipts.learn_identifiers), and so does a digital invoice as it is
imported; the documents filed before that did not. This goes through them
once, shop by shop, so that a torn header - or a supplier's invoice arriving
in the import folder - no longer leaves a document without a shop. Only what
enough of a shop's documents print, and no other supplier's does, is learned.

The digital invoices are read for their text first (`source_text`), which is
what makes the customer's own SIREN - printed on every supplier's invoice -
name nobody.
"""

import os
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from invoices.identifiers import describe
from invoices.models import Invoice, Supplier
from invoices.receipts import document_text, learn_identifiers


class Command(BaseCommand):
    help = "Apprend à chaque enseigne les identifiants (SIREN, téléphone, site) imprimés sur ses documents."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, dry_run=False, **options):
        learned: dict[str, list[str]] = {}
        with transaction.atomic():
            read = self.read_documents()
            texts = defaultdict(list)
            # Not a document whose supplier is in doubt (Invoice.supplier_doubt):
            # it is nobody's until a person says whose.
            checked = (
                Invoice.objects.filter(Q(reviewed_at__isnull=False) | ~Q(source_text=""))
                .filter(supplier_doubt="")
                .order_by("pk")
            )
            for document in checked:
                if document.document_text:
                    texts[document.supplier_id].append(document.document_text)
            for supplier in Supplier.objects.filter(pk__in=texts).order_by("name"):
                new = learn_identifiers(supplier, *texts[supplier.pk])
                if new:
                    learned[supplier.name] = new
            if dry_run:
                transaction.set_rollback(True)
        if dry_run:
            self.stdout.write("(essai : rien n'est enregistré)")
        if read:
            self.stdout.write(f"{read} facture(s) numérique(s) lue(s) pour ce qu'elles impriment.")
        verb = "apprendrait" if dry_run else "a appris"
        if not learned:
            self.stdout.write("Rien de nouveau à apprendre.")
        for name, identifiers in sorted(learned.items()):
            self.stdout.write(f"{name} {verb} : " + ", ".join(describe(identifier) for identifier in identifiers))

    def read_documents(self) -> int:
        """Keep what the digital invoices already filed print (`source_text`),
        for those imported before it was kept. A photo has its reading
        already; a file that has gone, or carries no text, is left alone."""
        read = 0
        waiting = Invoice.objects.filter(ocr_text="", source_text="").exclude(source_file="")
        for invoice in waiting.only("pk", "source_file").iterator():
            path = os.path.join(settings.MEDIA_ROOT, invoice.source_file.name)
            if not os.path.exists(path):
                continue
            text = document_text(path)
            if not text:
                continue
            read += 1
            Invoice.objects.filter(pk=invoice.pk).update(source_text=text)
        return read
