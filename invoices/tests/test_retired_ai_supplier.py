"""invoices/0037: the AI reading is gone (04/10/2026), and so is its
pseudo-supplier « Autre (analyse IA) » - code OTHER, reader key LLM, seeded by
invoices/0002 - wherever nothing names it. Where something does - a document,
a product, a payee name learnt, a known price, a line of its history, a slip
format, a pickup, another supplier's history - it stays as an ordinary
supplier, its name and every row naming it as they are. No other supplier is
touched, and going back changes nothing.

Run the way the repository runs a data migration: its function, on the
models as they stand (`django.apps.apps`). Data invented."""

import importlib
from datetime import date
from decimal import Decimal

from django.apps import apps
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.models import ForeignObjectRel
from django.test import TestCase

from bank.models import CounterpartyAlias
from invoices.models import InvoiceType, ShopItemPrice, Supplier, SupplierChange
from returnables.models import Pickup
from returnables.tests.support import make_format
from tests.factories import make_invoice, make_product, make_supplier

MIGRATION = importlib.import_module("invoices.migrations.0037_retire_ai_reading_supplier")
SEED = importlib.import_module("invoices.migrations.0002_seed_suppliers")
NODE = ("invoices", "0037_retire_ai_reading_supplier")


def seeded_ai_supplier() -> Supplier:
    """The pseudo-supplier exactly as 0002 seeded it (its own literals)."""
    (data,) = [supplier for supplier in SEED.SUPPLIERS if supplier["code"] == "OTHER"]
    return Supplier.objects.create(**data)


def retire() -> None:
    MIGRATION.retire_ai_supplier(apps, None)


def relations_to(model) -> set[tuple[str, str]]:
    """Every (model, field) pointing at `model`, hidden ones included."""
    return {
        (relation.related_model._meta.label_lower, relation.field.name)
        for relation in model._meta.get_fields(include_hidden=True)
        if isinstance(relation, ForeignObjectRel)
    }


class NewDatabaseTests(TestCase):
    def test_a_database_migrated_from_scratch_holds_no_ai_supplier(self):
        """The test database is migrated as the _template and every new
        espace are: 0002 seeds it, 0037 takes it away - nothing names it."""
        self.assertFalse(Supplier.objects.filter(code="OTHER").exists())
        self.assertFalse(Supplier.objects.filter(parser_key="LLM").exists())
        self.assertTrue(Supplier.objects.filter(code__in=["METRO", "UBA"]).count() == 2)


class RetireTests(TestCase):
    def setUp(self):
        self.ai = seeded_ai_supplier()
        self.others = list(Supplier.objects.exclude(pk=self.ai.pk).order_by("pk").values())

    def assert_the_others_untouched(self):
        self.assertEqual(list(Supplier.objects.exclude(pk=self.ai.pk).order_by("pk").values()), self.others)

    def assert_kept_as_an_ordinary_supplier(self):
        kept = Supplier.objects.get(pk=self.ai.pk)
        self.assertEqual((kept.code, kept.name, kept.parser_key), ("OTHER", "Autre (analyse IA)", ""))
        self.assertEqual(kept.is_scrapable, False)

    def test_named_by_nothing_it_is_deleted(self):
        retire()
        self.assertFalse(Supplier.objects.filter(pk=self.ai.pk).exists())
        self.assert_the_others_untouched()

    def test_a_document_filed_under_it_keeps_it_as_an_ordinary_supplier(self):
        invoice = make_invoice(supplier=self.ai, invoice_number="FA-ESSAI-1")
        retire()
        self.assert_kept_as_an_ordinary_supplier()
        invoice.refresh_from_db()
        self.assertEqual(invoice.supplier_id, self.ai.pk)
        self.assert_the_others_untouched()

    def test_any_row_naming_it_keeps_it_cascade_and_set_null_included(self):
        """A CASCADE would have taken the row with it in silence, and a
        SET_NULL emptied it: every relation is asked, the hidden
        SupplierChange.other_supplier too."""
        elsewhere = make_supplier(code="EPICERIE_ESSAI", name="Épicerie Essai")
        makers = {
            "un produit": lambda: make_product(supplier=self.ai, raw_name="ARTICLE ESSAI"),
            "une source": lambda: InvoiceType.objects.create(name="Source essai", supplier=self.ai),
            "un nom de payeur appris": lambda: CounterpartyAlias.objects.create(supplier=self.ai, name="PAYEUR ESSAI"),
            "un prix connu": lambda: ShopItemPrice.objects.create(
                supplier=self.ai, unit_price_ttc=Decimal("0.70"), label="Article essai"
            ),
            "son historique": lambda: SupplierChange.objects.create(
                supplier=self.ai, kind=SupplierChange.Kind.RENAMED, summary="Nom : essai."
            ),
            "l'historique d'un autre": lambda: SupplierChange.objects.create(
                supplier=elsewhere, other_supplier=self.ai, kind=SupplierChange.Kind.TYPES, summary="Source essai."
            ),
            "un format de bon": lambda: make_format(name="Format essai", supplier=self.ai),
            "une reprise": lambda: Pickup.objects.create(date=date(2026, 9, 1), supplier=self.ai),
        }
        for what, make in makers.items():
            with self.subTest(what=what):
                row = make()
                retire()
                self.assert_kept_as_an_ordinary_supplier()
                row.refresh_from_db()
                # Still there, still naming it.
                self.assertIn(self.ai.pk, {getattr(row, "supplier_id", None), getattr(row, "other_supplier_id", None)})
                type(row).objects.filter(pk=row.pk).delete()
                Supplier.objects.filter(pk=self.ai.pk).update(parser_key="LLM")

    def test_running_it_again_changes_nothing(self):
        make_invoice(supplier=self.ai)
        retire()
        retire()
        self.assert_kept_as_an_ordinary_supplier()


class NothingElseTests(TestCase):
    def test_another_supplier_with_the_ai_reader_key_is_left_as_it_is(self):
        other = make_supplier(code="FOURNISSEUR_ESSAI", name="Fournisseur Essai", parser_key="LLM")
        retire()
        other.refresh_from_db()
        self.assertEqual(other.parser_key, "LLM")

    def test_a_supplier_coded_other_with_another_reader_is_left_as_it_is(self):
        """Named by nothing, and not deleted: it is no seed of 0002."""
        other = Supplier.objects.create(code="OTHER", name="Other Essai", parser_key="")
        retire()
        self.assertEqual(Supplier.objects.get(pk=other.pk).name, "Other Essai")


class MigrationShapeTests(TestCase):
    def test_it_reverses_to_nothing(self):
        (operation,) = MIGRATION.Migration.operations
        self.assertIs(operation.code, MIGRATION.retire_ai_supplier)
        self.assertIs(operation.reverse_code, MIGRATION.migrations.RunPython.noop)

    def test_it_reads_0002_s_own_literals(self):
        seeded = next(supplier for supplier in SEED.SUPPLIERS if supplier["code"] == "OTHER")
        self.assertEqual((MIGRATION.CODE, MIGRATION.AI_READER_KEY), (seeded["code"], seeded["parser_key"]))

    def test_the_model_it_walks_knows_every_relation_to_a_supplier(self):
        """What the migration sees is the state its dependencies build - the
        apps whose models point at Supplier must come before it, or a
        relation is never asked and a CASCADE takes its rows."""
        state = MigrationLoader(connection).project_state(NODE, at_end=False)
        walked = relations_to(state.apps.get_model("invoices", "Supplier"))
        self.assertEqual(walked, relations_to(Supplier))
        self.assertIn(("invoices.supplierchange", "other_supplier"), walked)
