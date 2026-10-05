"""A sale document and its lines as the sales invoices made them
(recipes/models.py, migration recipes 0019): what a document counts in, the
total it states, a line tied to nothing, the quantity a line consumes, and
the bank credit that pays a document.

Every figure is the money's own - a total the tab, the bank and « Marges »
all read - so each rule is pinned on its own, with its zero, its blank and
its negative.

Invented data throughout: « Exemple Événements SARL », « Mariage Exemple »,
every number and every amount are made up for these tests.
"""

import hashlib
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.test import SimpleTestCase, TestCase

from bank.models import BankTransaction, InvoicePayment
from inventory.models import UnitChoices
from recipes import models as recipe_models
from recipes.models import (
    Recipe,
    SaleDocument,
    SaleDocumentLine,
    SaleDocumentPayment,
    legacy_key,
    new_sale_key,
    vat_divisor,
)
from tests.factories import make_recipe, make_sale_document, make_sale_line, make_stock_type

#: A key as a document carries it: 16 hexadecimal characters.
KEY = r"\A[0-9a-f]{16}\Z"


def credit(fingerprint: str = "vente-exemple-1", amount: str = "150.00") -> BankTransaction:
    """A credit of the statement - a transfer from a customer."""
    return BankTransaction.objects.create(
        operation_date=date(2026, 3, 12),
        label="VIR SEPA EXEMPLE EVENEMENTS",
        amount=Decimal(amount),
        fingerprint=fingerprint,
    )


class SaleDocumentModelTests(TestCase):
    def test_a_document_typed_by_hand_counts_and_states_nothing(self):
        """What every document saved before recipes 0019 becomes: it counts
        as before, has no file, and states no total, no rate, no figure of
        an electronic invoice."""
        document = make_sale_document()
        document.refresh_from_db()

        self.assertEqual(document.counting, "counted")
        self.assertTrue(document.counts)
        self.assertFalse(document.source_file)
        self.assertEqual(document.source_sha256, "")
        self.assertEqual(document.adjustment_ht, Decimal("0"))
        self.assertIsNone(document.stated_total_ttc)
        self.assertIsNone(document.stated_total_ht)
        self.assertEqual(document.einvoice_checks, [])
        self.assertFalse(document.is_einvoice)
        self.assertFalse(document.states_its_ht)

    def test_the_three_countings_say_what_the_page_says(self):
        self.assertEqual(
            SaleDocument.Counting.choices,
            [
                ("counted", "Compte dans les marges et le stock"),
                ("till", "Déjà comptée par la caisse"),
                ("deposit", "Acompte : la facture finale la comptera"),
            ],
        )
        for counting in (SaleDocument.Counting.TILL, SaleDocument.Counting.DEPOSIT):
            with self.subTest(counting=counting):
                self.assertFalse(make_sale_document(counting=counting).counts)

    def test_the_database_refuses_a_counting_it_does_not_know(self):
        with transaction.atomic(), self.assertRaisesMessage(IntegrityError, "saledocument_counting_known"):
            make_sale_document(counting="maybe")

    def test_each_document_has_a_key_of_its_own(self):
        one, other = make_sale_document(), make_sale_document()

        self.assertRegex(one.key, KEY)
        self.assertRegex(new_sale_key(), KEY)
        self.assertNotEqual(one.key, other.key)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            make_sale_document(key=one.key)

    def test_a_legacy_key_is_its_content_and_its_rank(self):
        """What « Données » knew a document by before recipes 0019 - its
        content fingerprint, and its rank among identical documents - cut
        to a key: the same document, the same key, in the database migrated
        and in every older archive of it."""
        fingerprint = "0f" * 32

        self.assertEqual(legacy_key(fingerprint, 0), hashlib.sha256(f"{fingerprint}#0".encode()).hexdigest()[:16])
        self.assertEqual(legacy_key(fingerprint, 0), legacy_key(fingerprint, 0))
        self.assertNotEqual(legacy_key(fingerprint, 0), legacy_key(fingerprint, 1))
        self.assertRegex(legacy_key(fingerprint, 1), KEY)

    def test_with_no_stated_total_the_total_is_its_lines_rounded_half_up(self):
        """0,1 of a coffee at 0,25 € is 0,025 €: 0,03 € (half away from
        zero, the house's rounding), never 0,02 € (half to even)."""
        document = make_sale_document()
        make_sale_line(document, label="Café exemple", quantity="0.1", unit_price_ttc="0.25")

        self.assertEqual(document.total_ttc, Decimal("0.03"))

    def test_a_stated_total_wins_over_its_lines(self):
        document = make_sale_document(stated_total_ttc="1500.00")
        make_sale_line(document, label="Cocktails", quantity="30", unit_price_ttc="9.00")

        self.assertEqual(document.total_ttc, Decimal("1500.00"))

    def test_with_neither_a_line_nor_a_total_it_is_nothing(self):
        self.assertEqual(make_sale_document().total_ttc, Decimal("0"))

    def test_what_is_left_to_pay(self):
        """The invoice's own « reste à payer » (BT-115) when it states one,
        else its total less what was already paid (BT-113)."""
        document = make_sale_document(stated_total_ttc="1500.00")
        self.assertEqual(document.to_pay, Decimal("1500.00"))

        document.prepaid_ttc = Decimal("500.00")
        self.assertEqual(document.to_pay, Decimal("1000.00"))

        document.payable_ttc = Decimal("999.99")
        self.assertEqual(document.to_pay, Decimal("999.99"))

        credit_note = make_sale_document(stated_total_ttc="-120.00")
        self.assertEqual(credit_note.to_pay, Decimal("-120.00"))

    def test_the_lines_differ_from_the_total_past_a_cent_a_line(self):
        document = make_sale_document(stated_total_ttc="20.02")
        lines = [
            make_sale_line(document, label="Cocktail exemple", quantity="1", unit_price_ttc="10.00"),
            make_sale_line(document, label="Planche exemple", quantity="1", unit_price_ttc="10.00"),
        ]

        self.assertFalse(document.lines_differ_of(lines))
        for stated in ("20.03", "19.97", "15.00"):
            with self.subTest(stated=stated):
                document.stated_total_ttc = Decimal(stated)
                self.assertTrue(document.lines_differ_of(lines))
        self.assertFalse(document.lines_differ_of([]))
        document.stated_total_ttc = None
        self.assertFalse(document.lines_differ_of(lines))

    def test_the_forms_reading_a_list_ask_the_database_nothing(self):
        """What a reader of many documents calls: a property reading
        `self.lines.all()` on a `to_attr` prefetch is a query per document."""
        made = make_sale_document(prepaid_ttc="5.00", einvoice_type_code="380")
        make_sale_line(made, label="Cocktail exemple", quantity="2", unit_price_ttc="9.00")
        document = SaleDocument.objects.prefetch_related(
            Prefetch("lines", queryset=SaleDocumentLine.objects.order_by("id"), to_attr="line_list")
        ).get(pk=made.pk)

        with self.assertNumQueries(0):
            self.assertEqual(document.total_ttc_of(document.line_list), Decimal("18.00"))
            self.assertEqual(SaleDocument.lines_ttc_of(document.line_list), Decimal("18.00"))
            self.assertEqual(document.to_pay_of(document.line_list), Decimal("13.00"))
            self.assertFalse(document.lines_differ_of(document.line_list))
            self.assertEqual(document.kind_label_of(Decimal("18.00")), "Facture")

    def test_what_states_its_ht(self):
        """An electronic invoice states its HT whole (BT-109), and so does a
        document typed with « Total HT » beside « Total TTC »."""
        self.assertTrue(make_sale_document(einvoice_format="CII").states_its_ht)
        self.assertTrue(make_sale_document(stated_total_ttc="120.00", stated_total_ht="100.00").states_its_ht)
        self.assertFalse(make_sale_document(stated_total_ttc="120.00").states_its_ht)
        self.assertFalse(make_sale_document().states_its_ht)

    def test_its_label_names_it_for_the_bank(self):
        document = make_sale_document(
            reference="F-2026-014", customer="Exemple Événements SARL", sold_on=date(2026, 3, 5)
        )

        self.assertEqual(document.label, "n° F-2026-014 · Exemple Événements SARL · 05/03/2026")
        self.assertEqual(make_sale_document(sold_on=date(2026, 3, 6)).label, "sans numéro · 06/03/2026")
        self.assertEqual(make_sale_document(reference="F-9", sold_on=date(2026, 3, 7)).label, "n° F-9 · 07/03/2026")

    def test_its_kind(self):
        document = make_sale_document()
        for type_code, total, kind in (
            ("", Decimal("10"), "Facture"),
            ("380", Decimal("10"), "Facture"),
            ("386", Decimal("10"), "Facture d'acompte"),
            ("381", Decimal("-10"), "Avoir"),
            ("261", Decimal("-10"), "Avoir"),
            ("396", Decimal("-10"), "Avoir"),
            # The code says it whatever the sign, and a negative total says
            # it whatever the code: a deposit refunded is money going back.
            ("381", Decimal("10"), "Avoir"),
            ("", Decimal("-10"), "Avoir"),
            ("386", Decimal("-10"), "Avoir"),
            ("751", Decimal("10"), "Type 751"),
        ):
            with self.subTest(type_code=type_code, total=total):
                document.einvoice_type_code = type_code
                self.assertEqual(document.kind_label_of(total), kind)

    def test_its_kind_reads_its_own_total(self):
        document = make_sale_document()
        make_sale_line(document, label="Remise exemple", quantity="-1", unit_price_ttc="50.00")

        self.assertEqual(document.kind_label, "Avoir")

    def test_how_it_reads_in_a_sentence(self):
        self.assertEqual(
            str(make_sale_document(sold_on=date(2026, 3, 5), reference="T-1")), "Vente du 05/03/2026 (n° T-1)"
        )
        self.assertEqual(str(make_sale_document(sold_on=date(2026, 3, 5))), "Vente du 05/03/2026")


class FreeLineTests(TestCase):
    """A line tied to nothing - « Location de salle », « Frais de service » -
    is the sales invoices' new line: money with no recipe and no article
    behind it, which consumes nothing. Before recipes 0019 nothing could
    hold one, and the tab named every line by its recipe or its article."""

    def setUp(self):
        self.document = make_sale_document()
        self.recipe = make_recipe(name="Coupe exemple", selling_price_ttc="9.00", vat_rate="0.20")
        self.keg = make_stock_type(name="Fût exemple 30 L", unit=UnitChoices.LITRE)

    def line(self, **fields) -> SaleDocumentLine:
        """A line kept in memory: what clean() and the properties read."""
        fields.setdefault("quantity", Decimal("1"))
        return SaleDocumentLine(document=self.document, **fields)

    def test_each_shape_the_database_refuses_names_its_constraint(self):
        for constraint, fields in (
            ("saledocumentline_at_most_one_source", {"recipe": self.recipe, "stock_type": self.keg}),
            ("saledocumentline_untied_has_a_label", {"label": ""}),
            (
                "saledocumentline_ht_has_its_rate",
                {"label": "Location de salle", "total_ht": Decimal("100.00")},
            ),
            (
                "saledocumentline_consumed_needs_a_source",
                {"label": "Location de salle", "consumed_quantity": Decimal("1")},
            ),
            ("saledocumentline_rebuilt_is_untied", {"recipe": self.recipe, "rebuilt": True}),
            ("saledocumentline_rebuilt_is_untied", {"stock_type": self.keg, "rebuilt": True}),
        ):
            with self.subTest(constraint=constraint, fields=fields):
                with transaction.atomic(), self.assertRaisesMessage(IntegrityError, constraint):
                    self.line(**fields).save()

    def test_what_the_database_accepts(self):
        for fields in (
            {"label": "Location de salle", "unit_price_ttc": Decimal("300.00")},
            {"label": "Location de salle", "total_ht": Decimal("250.00"), "vat_rate": Decimal("0.2000")},
            {"recipe": self.recipe, "consumed_quantity": Decimal("30")},
            {"stock_type": self.keg, "consumed_quantity": Decimal("0")},
            {"label": "Ventes à 20 %", "total_ht": Decimal("80.00"), "vat_rate": Decimal("0.2000"), "rebuilt": True},
        ):
            with self.subTest(fields=fields):
                self.line(**fields).save()

    def test_clean_says_each_refusal_in_french(self):
        for fields, said in (
            (
                {"recipe": self.recipe, "stock_type": self.keg},
                ["Choisissez une recette ou un article, pas les deux."],
            ),
            ({"label": ""}, ["Donnez un libellé à une ligne sans recette ni article."]),
            ({"label": "   "}, ["Donnez un libellé à une ligne sans recette ni article."]),
            (
                {"label": "Location de salle", "consumed_quantity": Decimal("2")},
                ["Quantité consommée : seulement pour une ligne reliée à une recette ou un article."],
            ),
            (
                {"recipe": self.recipe, "rebuilt": True},
                ["Une ligne reconstituée depuis la table de TVA ne se relie pas."],
            ),
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError) as caught:
                    self.line(**fields).clean()
                self.assertEqual(caught.exception.messages, said)

    def test_clean_accepts_every_shape_the_database_keeps(self):
        for fields in (
            {"label": "Location de salle"},
            {"recipe": self.recipe},
            {"recipe": self.recipe, "label": "Coupes de champagne", "consumed_quantity": Decimal("30")},
            {"stock_type": self.keg, "consumed_quantity": Decimal("30")},
            {"label": "Ventes à 20 %", "rebuilt": True},
        ):
            with self.subTest(fields=fields):
                self.line(**fields).clean()

    def test_a_free_line_is_named_by_its_label_and_has_no_unit(self):
        line = make_sale_line(self.document, label="Location de salle", unit_price_ttc="300.00")
        line.refresh_from_db()

        self.assertFalse(line.is_tied)
        self.assertEqual(line.source_name, "Location de salle")
        self.assertEqual(line.shown_name, "Location de salle")
        self.assertEqual(line.unit_display, "")
        self.assertEqual(str(line), "1.0000 x Location de salle")

    def test_a_tied_line_shows_the_invoice_s_own_words(self):
        line = self.line(recipe=self.recipe, label="Coupes de champagne (soirée)")

        self.assertTrue(line.is_tied)
        self.assertEqual(line.source_name, "Coupe exemple")
        self.assertEqual(line.shown_name, "Coupes de champagne (soirée)")
        self.assertEqual(line.unit_display, "Unité")
        self.assertEqual(self.line(recipe=self.recipe).shown_name, "Coupe exemple")
        self.assertEqual(self.line(stock_type=self.keg).unit_display, "Litre")
        self.assertEqual(self.line(stock_type=self.keg).source_name, "Fût exemple 30 L")

    def test_a_stated_line_is_its_ht_at_its_rate_unrounded(self):
        """An electronic invoice states no line TTC (EN 16931): it is its
        HT (BT-131) at its rate (BT-152), and its own price, typed or not,
        never comes into it."""
        line = self.line(
            label="Planches", total_ht=Decimal("100.15"), vat_rate=Decimal("0.1000"), unit_price_ttc=Decimal("1")
        )

        self.assertEqual(line.total_ttc, Decimal("110.165"))

    def test_a_typed_line_is_its_price_times_its_quantity(self):
        self.assertEqual(
            self.line(recipe=self.recipe, quantity=Decimal("3"), unit_price_ttc=Decimal("8.00")).total_ttc,
            Decimal("24.00"),
        )
        self.assertEqual(
            self.line(label="Remise", quantity=Decimal("1"), unit_price_ttc=Decimal("-20.00")).total_ttc,
            Decimal("-20.00"),
        )

    def test_a_recipe_line_saved_before_falls_back_on_the_menu(self):
        self.assertEqual(self.line(recipe=self.recipe, quantity=Decimal("2")).total_ttc, Decimal("18.00"))

    def test_with_no_price_anywhere_a_line_is_nothing(self):
        preparation = make_recipe(name="Sirop exemple", selling_price_ttc=None)
        for fields in ({"stock_type": self.keg}, {"label": "Location de salle"}, {"recipe": preparation}):
            with self.subTest(fields=fields):
                self.assertEqual(self.line(quantity=Decimal("2"), **fields).total_ttc, Decimal("0"))

    def test_the_consumption_is_the_consumed_quantity_else_the_invoiced_one(self):
        self.assertEqual(self.line(stock_type=self.keg, consumed_quantity=Decimal("30")).consumption, Decimal("30"))
        self.assertEqual(self.line(stock_type=self.keg, quantity=Decimal("2")).consumption, Decimal("2"))
        # 0 is a quantity typed - nothing left the stock - never « blank ».
        self.assertEqual(self.line(stock_type=self.keg, consumed_quantity=Decimal("0")).consumption, Decimal("0"))

    def test_the_rate_is_the_line_s_own_else_the_recipe_s(self):
        self.assertEqual(self.line(recipe=self.recipe, vat_rate=Decimal("0.1000")).rate, Decimal("0.1000"))
        self.assertEqual(self.line(recipe=self.recipe).rate, Decimal("0.20"))
        self.assertIsNone(self.line(stock_type=self.keg).rate)
        self.assertIsNone(self.line(label="Location de salle").rate)
        self.assertEqual(self.line(label="Location de salle", vat_rate=Decimal("0.2000")).rate, Decimal("0.2000"))

    def test_the_divisor_is_one_plus_the_rate_or_none(self):
        self.assertEqual(self.line(recipe=self.recipe).divisor, Decimal("1.20"))
        self.assertEqual(self.line(stock_type=self.keg, vat_rate=Decimal("0.0550")).divisor, Decimal("1.0550"))
        self.assertIsNone(self.line(stock_type=self.keg).divisor)

    def test_a_price_of_its_own(self):
        """The recipe's menu price is not the line's own: an article needs a
        price of its own to come into the margins."""
        self.assertTrue(self.line(stock_type=self.keg, unit_price_ttc=Decimal("150.00")).has_own_price)
        self.assertTrue(
            self.line(stock_type=self.keg, total_ht=Decimal("125.00"), vat_rate=Decimal("0.2")).has_own_price
        )
        self.assertTrue(self.line(stock_type=self.keg, unit_price_ttc=Decimal("0")).has_own_price)
        self.assertFalse(self.line(recipe=self.recipe).has_own_price)
        self.assertFalse(self.line(stock_type=self.keg).has_own_price)

    def test_the_sign_of_the_money(self):
        self.assertEqual(self.line(recipe=self.recipe).money_sign, 1)
        self.assertEqual(self.line(recipe=self.recipe, quantity=Decimal("-2")).money_sign, -1)
        self.assertEqual(self.line(stock_type=self.keg).money_sign, 0)
        # A stated HT says it, whatever the quantity.
        self.assertEqual(
            self.line(
                label="Remise", quantity=Decimal("1"), total_ht=Decimal("-60.00"), vat_rate=Decimal("0.2")
            ).money_sign,
            -1,
        )

    def test_a_consumption_against_the_money_disagrees(self):
        """Stock leaving on a refund, or coming back on a sale: the mirror
        shape, never costed and never accepted."""
        agreeing = (
            self.line(recipe=self.recipe, quantity=Decimal("2")),
            self.line(recipe=self.recipe, quantity=Decimal("-2")),
            self.line(stock_type=self.keg, unit_price_ttc=Decimal("150.00"), consumed_quantity=Decimal("30")),
            self.line(stock_type=self.keg, unit_price_ttc=Decimal("150.00"), consumed_quantity=Decimal("0")),
            # No money to disagree with.
            self.line(stock_type=self.keg, consumed_quantity=Decimal("-30")),
        )
        for line in agreeing:
            with self.subTest(line=line.consumed_quantity):
                self.assertTrue(line.consumption_agrees)
        mirrors = (
            self.line(recipe=self.recipe, quantity=Decimal("-1"), total_ht=Decimal("60.00"), vat_rate=Decimal("0.2")),
            self.line(stock_type=self.keg, unit_price_ttc=Decimal("150.00"), consumed_quantity=Decimal("-30")),
        )
        for line in mirrors:
            with self.subTest(line=line.quantity):
                self.assertFalse(line.consumption_agrees)


class VatDivisorTests(SimpleTestCase):
    def test_no_rate_has_no_divisor(self):
        self.assertIsNone(vat_divisor(None))

    def test_a_rate_divides_by_one_plus_it(self):
        self.assertEqual(vat_divisor(Decimal("0.20")), Decimal("1.20"))
        self.assertEqual(vat_divisor(Decimal("0")), Decimal("1"))

    def test_minus_one_reads_one_never_zero(self):
        """A rate of exactly -1, written by a raw update past the validators,
        must not be able to 500 every page that divides by it."""
        self.assertEqual(vat_divisor(Decimal("-1")), Decimal("1"))

    def test_a_recipe_divides_through_it_unchanged(self):
        recipe = Recipe(name="Limonade exemple", selling_price_ttc=Decimal("5.50"), vat_rate=Decimal("0.10"))
        with mock.patch.object(recipe_models, "vat_divisor", wraps=vat_divisor) as divisor:
            self.assertEqual(recipe._vat_divisor, Decimal("1.10"))
        divisor.assert_called_once_with(Decimal("0.10"))
        self.assertEqual(recipe.selling_price_ht, Decimal("5.00"))
        recipe.vat_rate = Decimal("-1")
        self.assertEqual(recipe._vat_divisor, Decimal("1"))


class SaleDocumentPaymentModelTests(TestCase):
    """A credit of the statement pays a sales invoice: never an
    InvoicePayment, whose invoice is a PURCHASE every spending figure reads."""

    def setUp(self):
        self.document = make_sale_document(reference="F-1", customer="Mariage Exemple", stated_total_ttc="150.00")
        self.credit = credit()

    def link(self, document=None, credit_line=None, method=SaleDocumentPayment.Method.MANUAL):
        return SaleDocumentPayment.objects.create(
            document=document or self.document, transaction=credit_line or self.credit, method=method
        )

    def test_a_document_and_a_credit_are_linked_once(self):
        self.link()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.link(method=SaleDocumentPayment.Method.AUTO)

    def test_neither_side_is_exclusive(self):
        """A deposit then the balance are two credits on one invoice; one
        transfer can pay two invoices."""
        other_document = make_sale_document(reference="F-2", stated_total_ttc="80.00")
        other_credit = credit(fingerprint="vente-exemple-2", amount="70.00")
        self.link()
        self.link(credit_line=other_credit)
        self.link(document=other_document)

        self.assertEqual(self.document.bank_payments.count(), 2)
        self.assertEqual(self.credit.sale_payments.count(), 2)

    def test_each_side_reads_its_links_by_its_own_name(self):
        link = self.link()

        self.assertEqual(list(self.document.bank_payments.all()), [link])
        self.assertEqual(list(self.credit.sale_payments.all()), [link])

    def test_deleting_the_document_takes_its_links_and_leaves_the_credit(self):
        self.link()
        self.document.delete()

        self.assertFalse(SaleDocumentPayment.objects.exists())
        self.assertTrue(BankTransaction.objects.filter(pk=self.credit.pk).exists())

    def test_deleting_the_credit_takes_its_links_and_leaves_the_document(self):
        self.link()
        self.credit.delete()

        self.assertFalse(SaleDocumentPayment.objects.exists())
        self.assertTrue(SaleDocument.objects.filter(pk=self.document.pk).exists())

    def test_a_credit_s_payments_are_still_purchases_only(self):
        """`reconcile.open_lines`, `views.classify` and `invoice_files.paid_by`
        read `payments` as purchase invoices."""
        self.assertIs(BankTransaction._meta.get_field("payments").related_model, InvoicePayment)
        self.assertIs(BankTransaction._meta.get_field("sale_payments").related_model, SaleDocumentPayment)
        self.assertIs(SaleDocument._meta.get_field("bank_payments").related_model, SaleDocumentPayment)

    def test_the_methods_are_the_purchase_links_own(self):
        self.assertEqual(SaleDocumentPayment.Method.choices, InvoicePayment.Method.choices)
