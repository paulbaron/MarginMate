"""recipes 0020 (« Factures de vente »): every document saved before it is
given the key « Données » already knew it by - its content fingerprint and
its rank among identical documents (`recipes.models.legacy_key`) - so an
archive written before the migration still finds it.

The key is the risky step. One AddField(unique=True, default=new_sale_key)
gives every existing row the SAME value - Django works a callable default
out once for a table rebuild - and the migration then fails on the unique
index as soon as two documents exist. Only a replay proves the steps in
their order: MigrationReplayTests takes the test database back to the
migration before it (0019_tillformat), writes documents there through the
historical models, and migrates forward.

Invented data throughout.
"""

import importlib
from datetime import date
from decimal import Decimal

from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from recipes.models import SaleDocument, legacy_key
from tests.factories import make_recipe, make_stock_type
from transfer.sections.sales import fingerprint

BEFORE = ("recipes", "0019_tillformat")
AFTER = ("recipes", "0020_sale_invoices")


def migration():
    return importlib.import_module("recipes.migrations.0020_sale_invoices")


class LegacyKeyTests(SimpleTestCase):
    """The migration carries a FROZEN copy of the fingerprint (a migration
    replays the same whatever the code becomes): it must key a document
    exactly as the live code - « Données »' fingerprint, then legacy_key -
    does, or an archive older than the migration would not find it."""

    def test_the_migration_keys_a_document_as_the_live_code_does(self):
        lines = [
            ("recipe", "Coupe exemple", Decimal("10.0000"), None),
            ("article", "Fût exemple 30 L", Decimal("1.0000"), Decimal("150.00")),
        ]
        # Spelt otherwise, read the same: the fingerprint folds names and
        # writes every number at its field's places.
        spelt_otherwise = [("recipe", "  COUPE   exemple ", Decimal("2"), Decimal("8.5"))]
        documents = [
            (11, "F-1", date(2026, 3, 5), "", lines),
            (12, "F-1", date(2026, 3, 5), "", lines),
            (13, "", date(2026, 3, 6), "Mariage Exemple", spelt_otherwise),
        ]

        keys = migration().legacy_keys(documents)

        same = fingerprint("F-1", date(2026, 3, 5), "", lines)
        third = fingerprint("", date(2026, 3, 6), "Mariage Exemple", [("recipe", "Coupe exemple", "2", "8.50")])
        self.assertEqual(keys, {11: legacy_key(same, 0), 12: legacy_key(same, 1), 13: legacy_key(third, 0)})
        self.assertEqual(len(set(keys.values())), 3)

    def test_it_follows_the_till_s_formats_and_the_bank_s_treasury(self):
        """It follows recipes 0019_tillformat (written as 0019, it was
        renumbered after GitHub's main's) and the bank's treasury: its link
        table names `bank.BankTransaction`, and no bank migration depends on
        recipes."""
        self.assertEqual(
            set(migration().Migration.dependencies), {("recipes", "0019_tillformat"), ("bank", "0008_treasury")}
        )

    def test_it_says_what_going_back_does(self):
        self.assertIn("Going back", migration().__doc__)


class MigrationReplayTests(TransactionTestCase):
    serialized_rollback = True

    def test_documents_saved_before_get_the_key_their_content_gives(self):
        executor = MigrationExecutor(connections["default"])
        try:
            executor.migrate([BEFORE])
            historical = executor.loader.project_state([BEFORE]).apps
            Document = historical.get_model("recipes", "SaleDocument")
            Line = historical.get_model("recipes", "SaleDocumentLine")
            # Neither table moves in 0020: the live models write them.
            coupe = make_recipe(name="Coupe exemple", selling_price_ttc="9.00")
            keg = make_stock_type(name="Fût exemple 30 L")
            made = []
            for reference, note in (("F-1", ""), ("F-1", ""), ("", "Mariage Exemple")):
                document = Document.objects.create(reference=reference, sold_on=date(2026, 3, 5), note=note)
                Line.objects.create(document_id=document.pk, recipe_id=coupe.pk, quantity=Decimal("10"))
                Line.objects.create(
                    document_id=document.pk, stock_type_id=keg.pk, quantity=Decimal("1"), unit_price_ttc=Decimal("150")
                )
                made.append(document.pk)

            executor.loader.build_graph()
            executor.migrate([AFTER])

            lines = [
                ("recipe", "Coupe exemple", Decimal("10"), None),
                ("article", "Fût exemple 30 L", Decimal("1"), Decimal("150")),
            ]
            same = fingerprint("F-1", date(2026, 3, 5), "", lines)
            third = fingerprint("", date(2026, 3, 5), "Mariage Exemple", lines)
            self.assertEqual(
                dict(SaleDocument.objects.values_list("pk", "key")),
                {made[0]: legacy_key(same, 0), made[1]: legacy_key(same, 1), made[2]: legacy_key(third, 0)},
            )
            # Nothing else changes meaning: each counts, as it did.
            self.assertEqual(set(SaleDocument.objects.values_list("counting", flat=True)), {"counted"})
            # And a document made now draws a random key of its own.
            self.assertRegex(SaleDocument.objects.create(sold_on=date(2026, 3, 6)).key, r"\A[0-9a-f]{16}\Z")
        finally:
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes())
